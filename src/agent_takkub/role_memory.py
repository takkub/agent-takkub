"""Per-(role × project) learned memory.

Each teammate role accumulates its own project-specific knowledge across runs in
``runtime/role-memory/<project>/<role>.md``: conventions, gotchas, key decisions,
plus role-specific notes (qa: test login / accounts / flows). The orchestrator
injects a pointer into the teammate's spawn prompt telling it to READ the file
before working and APPEND concise learnings when it discovers something
non-obvious — so e.g. frontend-on-PMS grows into its project instead of starting
cold on every spawn.

Cockpit-managed and gitignored (lives under ``runtime/``). Lead is intentionally
excluded — it owns the project-wide ``MEMORY.md`` instead.

Seeding is best-effort and never raises: a filesystem failure just means the
pointer isn't injected for that spawn (the pane still works, it just doesn't have
a learned-notes file yet).
"""

from __future__ import annotations

import logging
import pathlib
import re

from .config import RUNTIME_DIR

_log = logging.getLogger(__name__)

ROLE_MEMORY_DIR = RUNTIME_DIR / "role-memory"

# Sections every role's notes start with.
_BASE_SECTIONS = """## Conventions / patterns
- (ว่าง — เติมเมื่อเรียนรู้)

## Gotchas / pitfalls
-

## Key decisions / เหตุผล
-
"""

# Extra sections seeded per base role (appended after the base sections).
_ROLE_SECTIONS: dict[str, str] = {
    "qa": """## Test login & accounts
> ⚠️ plaintext — single-user cockpit, gitignored. ใช้ throwaway / test account เท่านั้น
-

## Known flows (ขั้นตอนไปถึงแต่ละหน้า)
-

## Flaky / known-failing
-
""",
    "frontend": """## Components & structure
-

## Build / dev server (รันยังไง)
-

## Styling / UI conventions
-
""",
    "backend": """## Endpoints & schema
-

## Migrations / DB
-

## Local run
-
""",
    "mobile": """## App structure / navigation
-

## Build / run (iOS / Android)
-
""",
    "devops": """## Services / compose / ports
-

## Deploy / CI
-
""",
    "reviewer": """## Recurring review issues ที่นี่
-

## Risky areas
-
""",
    "critic": """## Design system / tokens
-

## Recurring UX issues
-
""",
    "designer": """## Design system / tokens
-

## Recurring UX issues
-
""",
}


def _safe(name: str) -> str:
    """Sanitize a project / role name into ONE safe path segment.

    Dots are dropped (not just other separators) so a ``..`` can never survive as
    a parent-dir-traversal segment, even if a caller bypasses the upstream
    validate_name guard. ``my.proj`` → ``my_proj``; ``..`` → ``__``.
    """
    return re.sub(r"[^A-Za-z0-9_-]", "_", name) or "default"


def role_memory_path(project: str, base_role: str) -> pathlib.Path:
    """The ``runtime/role-memory/<project>/<role>.md`` path for this (project, role)."""
    return ROLE_MEMORY_DIR / _safe(project) / f"{_safe(base_role)}.md"


def _seed(project: str, base_role: str) -> str:
    header = (
        f"# {base_role} — learned notes · project: {project}\n\n"
        f"> สิ่งที่ **{base_role} เรียนรู้เกี่ยวกับโปรเจคนี้** สะสมข้ามรอบงาน (cockpit per-role memory).\n"
        "> อ่านก่อนเริ่มงาน · **append** สิ่งที่ไม่ obvious เมื่อเจอ (bullet สั้น กระชับ).\n"
        '> อย่าซ้ำกับ code / git / โปรเจค MEMORY.md — เก็บเฉพาะ "ความรู้ที่ต้องเสียเวลาค้นใหม่".\n\n'
    )
    extra = _ROLE_SECTIONS.get(base_role, "")
    body = _BASE_SECTIONS + (("\n" + extra) if extra else "")
    return header + body


def ensure_role_memory(project: str, base_role: str) -> pathlib.Path | None:
    """Return this (project, role)'s learned-memory path, seeding it if missing.

    Existing files are never overwritten (the role's accumulated learnings are
    preserved). Best-effort: returns None on any filesystem error so the caller
    can simply skip the spawn-prompt injection.
    """
    path = role_memory_path(project, base_role)
    try:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_seed(project, base_role), encoding="utf-8")
        return path
    except OSError as e:
        _log.warning("ensure_role_memory: %s/%s: %s", project, base_role, e)
        return None
