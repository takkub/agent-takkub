"""version.json — records the app version, the storage schema version, and
each provider's last-detected CLI version, under `core.storage.paths.core_home()`
(no new home declared, per storage/paths.py's own rule).

Reuses `core.models.version.ComponentVersion` as the record shape instead of
a bespoke one: `component` values used here are `"app"`, `"storage_schema"`,
and `"adapter:<provider>"` for each provider's detected CLI build.

Atomic write: same read-modify-`os.replace` shape as
`core.storage.jsonl_store` (never a partial doc visible to a concurrent
reader) — the whole document is small (one JSON object), so a full rewrite
per call is the right tradeoff, same as jsonl_store's own `append()`.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from agent_takkub import __version__ as APP_VERSION

from ..models.version import ComponentVersion
from ..storage.paths import core_home

STORAGE_SCHEMA_VERSION = 1
_FILE_NAME = "version.json"


def version_doc_path() -> Path:
    return core_home() / _FILE_NAME


def _to_dict(cv: ComponentVersion) -> dict:
    return {
        "id": cv.id,
        "component": cv.component,
        "version": cv.version,
        "released_at": cv.released_at,
        "user_id": cv.user_id,
        "workspace_id": cv.workspace_id,
        "project_id": cv.project_id,
    }


def _from_dict(d: dict) -> ComponentVersion:
    return ComponentVersion(
        id=str(d.get("id", "")),
        component=str(d.get("component", "")),
        version=str(d.get("version", "")),
        released_at=d.get("released_at"),
        user_id=d.get("user_id"),
        workspace_id=d.get("workspace_id"),
        project_id=d.get("project_id"),
    )


def read_version_doc(path: Path | None = None) -> list[ComponentVersion]:
    """Missing/corrupt file reads as empty — fail-open, matches
    `legacy_reader.read_json`'s contract."""
    p = path or version_doc_path()
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    records = raw.get("components", []) if isinstance(raw, dict) else []
    if not isinstance(records, list):
        return []
    return [_from_dict(r) for r in records if isinstance(r, dict)]


def write_version_doc(components: list[ComponentVersion], path: Path | None = None) -> None:
    p = path or version_doc_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "app_version": APP_VERSION,
        "storage_schema_version": STORAGE_SCHEMA_VERSION,
        "updated_at": time.time(),
        "components": [_to_dict(c) for c in components],
    }
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))
    os.replace(tmp, p)


def record_component(component: str, version: str, *, path: Path | None = None) -> ComponentVersion:
    """Read-modify-write: upsert one component's version, leaving the rest
    of the document untouched."""
    p = path or version_doc_path()
    existing = {c.component: c for c in read_version_doc(p)}
    updated = ComponentVersion(
        id=component, component=component, version=version, released_at=time.time()
    )
    existing[component] = updated
    write_version_doc(list(existing.values()), p)
    return updated
