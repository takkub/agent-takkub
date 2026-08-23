"""Project identity resolution — issue #365 phase 8 (Obsidian hardening),
master plan §4 fix 3: "`project_id` identity ต้องมาจาก V2 registry ตัวเดียว
ไม่นิยามซ้ำ" (project_id must come from the one V2 registry, never
re-defined elsewhere).

`config.load_projects()` is already that single registry reader — the V2
dual-write mirror under `TAKKUB_V2_AUTHORITY` when on and migrated, V1
`projects.json` otherwise (#362 piece 1, merged). This module adds nothing
on top of it beyond "what is *this* project's canonical id": every known
project's dict key IS its id already (`ProjectMigrationStep`/`steps_v1.py`
uses the same key verbatim as the V2 `projects/<id>/project.json` folder
name), so resolving identity means looking a caller's string up against
that registry instead of re-deriving it from a raw/mutable display string
the way each Obsidian writer used to.

Leaf module: only imports `.config`, no orchestrator/UI — any future
consumer (doctor, an OpenViking adapter) can import this without pulling
in Qt.
"""

from __future__ import annotations

from . import config


def resolve_project_id(name: str) -> str:
    """Return the canonical ``project_id`` for *name*.

    Resolution order:
    1. Exact match against a key in ``config.load_projects()["projects"]``
       (the case already registered).
    2. A case-insensitive match against those same keys, so callers that
       pass a differently-cased display string still land on the one
       registered id.
    3. ``config.validate_name(name, "project")``'s normalised slug — a
       deterministic fallback for a project that exists on disk but isn't
       registered in either V1 or V2 yet (brand new, or the registry
       failed to read), so this never returns ``None``.

    Raises ``ValueError`` for a *name* ``validate_name`` itself would
    reject (path traversal, empty, …) — the same contract every other
    project-name consumer in this codebase already follows.
    """
    raw = (name or "").strip()
    normalised = config.validate_name(raw, "project")
    projects = config.load_projects().get("projects")
    if isinstance(projects, dict):
        if raw in projects:
            return raw
        for key in projects:
            if isinstance(key, str) and key.lower() == normalised:
                return key
    return normalised
