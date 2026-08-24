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
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from agent_takkub.obsidian_boundary import is_indexable
from agent_takkub.obsidian_metadata import content_hash
from agent_takkub.vault_mirror import _resolve_vault_dir

from . import openviking_adapter as adapter

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


def _load_state(project: str | None) -> dict[str, str]:
    path = _state_path(project)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(project: str | None, state: dict[str, str]) -> None:
    path = _state_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


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
        if state.get(rel) == h:
            skipped += 1
            continue
        try:
            ok = adapter.add_resource(path)
        except Exception:
            _log.exception("indexing.index_vault: add_resource raised for %s (fail-open)", rel)
            ok = False
        if ok:
            state[rel] = h
            added += 1
        else:
            failed += 1

    _save_state(project, state)
    return IndexResult(ok=True, added=added, skipped=skipped, failed=failed, total=len(state))


def index_status(project: str | None) -> dict:
    state = _load_state(project)
    h = adapter.health()
    return {
        "enabled": adapter.enabled(),
        "mode": adapter.mode(),
        "health_ok": h.ok,
        "healthy": h.healthy,
        "version": h.version,
        "known_version": h.known_version,
        "indexed_count": len(state),
    }


__all__ = ["IndexResult", "index_status", "index_vault"]
