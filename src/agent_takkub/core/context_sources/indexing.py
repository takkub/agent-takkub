"""`takkub ov index`/`status` (issue #372, rollout step E — "Obsidian/
resource indexing"). Pushes the SAME allowlisted docs `resource_source.py`
already reads locally into the OpenViking sidecar's own knowledge base, so
`openviking_source.py`'s `/search/find` calls can find vault content too —
`resource_source.py` itself never needs this to have run (it reads the
vault directly), this only extends what the SIDECAR knows about.

State is a per-project `{rel_path: content_hash}` map under
``DATA_HOME/openviking/index/<project>.json`` — deliberately NOT written
into the Second Brain (`ไม่ dual-write Brain`, task's own constraint): this
is indexing bookkeeping, not a memory/fact, and Brain's write path
(`core.brain.facade.submit`) is never touched by this module.

A second file, ``_registry.json`` in the same directory, is the local
scope-metadata store issue #372's follow-up (`02_OPENVIKING_STRICT_
SCOPE.md`) adds: every resource pushed to the sidecar is recorded there —
keyed by the exact ``path`` string handed to `openviking_adapter.
add_resource` — with the full metadata set that spec requires
(workspace_id/project_id/source/kind/resource_id/trust/updated_at,
reusing `obsidian_metadata.NoteMetadata`). `openviking_source.py` looks a
search hit's `uri` up here rather than trusting anything OpenViking itself
returns — see that module's docstring for why.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from agent_takkub.obsidian_boundary import is_indexable
from agent_takkub.obsidian_metadata import TRUST_CURATED, NoteMetadata, content_hash
from agent_takkub.vault_mirror import _resolve_vault_dir

from . import openviking_adapter as adapter
from .base import GLOBAL_PROJECT_ID, WORKSPACE_ID
from .resource_source import resolve_vault_project_id

_log = logging.getLogger(__name__)

# Larger than resource_source's own read-cap: indexing is an explicit,
# infrequent `takkub ov index` run (not on the hot context-build path), so
# it can afford to look at bigger files.
_MAX_FILE_BYTES = 500_000


def _state_dir() -> Path:
    from agent_takkub import config

    return config.DATA_HOME / "openviking" / "index"


def _state_path(project: str | None) -> Path:
    return _state_dir() / f"{project or '_global'}.json"


def _registry_path() -> Path:
    return _state_dir() / "_registry.json"


def _last_sync_path() -> Path:
    return _state_dir() / "_last_sync.json"


def _load_json_dict(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_json_dict(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _load_state(project: str | None) -> dict[str, str]:
    return _load_json_dict(_state_path(project))


def _save_state(project: str | None, state: dict[str, str]) -> None:
    _save_json_dict(_state_path(project), state)


def resource_metadata_for_uri(uri: str) -> dict | None:
    """Best-effort local lookup: the scope metadata Takkub itself attached
    to a resource at index time (`_resource_metadata`), keyed by the exact
    ``path`` string handed to OpenViking's ingest call. Never raises — an
    unreadable/missing registry is just "nothing known about this uri",
    the same fail-closed signal as an uri that was never indexed at all."""
    meta = _load_json_dict(_registry_path()).get(uri)
    return meta if isinstance(meta, dict) else None


def _resource_metadata(rel: str, text: str) -> dict:
    project_id = resolve_vault_project_id(rel) or GLOBAL_PROJECT_ID
    meta = NoteMetadata.new(
        project_id=project_id, source="ov-index", kind="doc", trust=TRUST_CURATED, text=text
    )
    return {
        "workspace_id": WORKSPACE_ID,
        "project_id": meta.project_id,
        "source": meta.source,
        "kind": meta.kind,
        "resource_id": meta.knowledge_id,
        "trust": meta.trust,
        "updated_at": meta.updated_at,
    }


def _save_last_sync(project: str | None) -> None:
    try:
        data = _load_json_dict(_last_sync_path())
        data[project or "_global"] = time.time()
        _save_json_dict(_last_sync_path(), data)
    except Exception:
        _log.debug("indexing._save_last_sync failed (best-effort)", exc_info=True)


def _load_last_sync(project: str | None) -> float | None:
    value = _load_json_dict(_last_sync_path()).get(project or "_global")
    return value if isinstance(value, (int, float)) else None


@dataclass(frozen=True, slots=True)
class IndexResult:
    ok: bool
    added: int = 0
    skipped: int = 0
    failed: int = 0
    total: int = 0
    reason: str = ""


def index_vault(project: str | None) -> IndexResult:
    """Incremental: a doc whose `content_hash` (obsidian_metadata's own
    identity-independent hash) hasn't changed since the last run is skipped
    without a network call. A single file failing (unreadable, or the
    sidecar rejecting it) never aborts the run — it's just counted."""
    if not adapter.enabled():
        return IndexResult(ok=False, reason="TAKKUB_OPENVIKING_ENABLED != 1")
    vault = _resolve_vault_dir()
    if vault is None:
        return IndexResult(ok=False, reason="no Obsidian vault configured (TAKKUB_VAULT_DIR)")

    state = _load_state(project)
    registry = _load_json_dict(_registry_path())
    added = skipped = failed = 0
    try:
        paths = sorted(vault.rglob("*.md"))
    except OSError as exc:
        return IndexResult(ok=False, reason=f"could not walk vault: {exc}")

    for path in paths:
        try:
            rel = path.relative_to(vault).as_posix()
        except ValueError:
            continue
        if not is_indexable(rel):
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                failed += 1
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            failed += 1
            continue
        h = content_hash(text)
        key = str(path)
        if state.get(rel) == h:
            if key not in registry:
                registry[key] = _resource_metadata(rel, text)
            skipped += 1
            continue
        try:
            ok = adapter.add_resource(path)
        except Exception:
            _log.exception("indexing.index_vault: add_resource raised for %s (fail-open)", rel)
            ok = False
        if ok:
            state[rel] = h
            registry[key] = _resource_metadata(rel, text)
            added += 1
        else:
            failed += 1

    _save_state(project, state)
    _save_json_dict(_registry_path(), registry)
    _save_last_sync(project)
    return IndexResult(ok=True, added=added, skipped=skipped, failed=failed, total=len(state))


def reset_state(project: str | None) -> None:
    """Drop the incremental `{rel_path: content_hash}` cache for *project* so
    the next `index_vault` re-pushes every allowlisted doc, even ones whose
    hash hasn't changed — the Settings UI's "Re-index" action (as opposed to
    "Sync", which stays incremental) for when the sidecar's own knowledge
    base was reset externally and drifted from this cache."""
    _save_state(project, {})


def index_status(project: str | None) -> dict:
    state = _load_state(project)
    started = time.monotonic()
    h = adapter.health()
    latency_ms = (time.monotonic() - started) * 1000
    return {
        "enabled": adapter.enabled(),
        "mode": adapter.mode(),
        "health_ok": h.ok,
        "healthy": h.healthy,
        "version": h.version,
        "known_version": h.known_version,
        "health_error": h.error,
        "health_latency_ms": latency_ms,
        "indexed_count": len(state),
        "strict_project_scope": True,
        "last_sync": _load_last_sync(project),
    }


__all__ = [
    "IndexResult",
    "index_status",
    "index_vault",
    "reset_state",
    "resource_metadata_for_uri",
]
