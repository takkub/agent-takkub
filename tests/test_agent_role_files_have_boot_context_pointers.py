"""Guard: every `docs/...` pointer a role file (or root CLAUDE.md) names
actually resolves to a real file (#516 token diet).

Companion to the other `test_agent_role_files_have_*_guard.py` files, but
for the opposite failure mode: those guard against a safety section being
diluted away; this one guards against an on-demand doc a diet role file
points into silently rotting (renamed/deleted) while the pointer stays —
which would leave a pane reading a dead link mid-task with no fallback.
Generic over every `.claude/agents/*.md` file and root `CLAUDE.md`, so it
keeps working as more roles get dieted onto the same on-demand-doc pattern.

Absolute per-role token ceilings live in `tests/test_boot_context_ceiling.py`
against `docs/audit/boot-context-baseline.json` (backend's #516 measurement
tool) — not duplicated here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
AGENTS_DIR = REPO_ROOT / ".claude" / "agents"
CHECKED_FILES = (REPO_ROOT / "CLAUDE.md", *sorted(AGENTS_DIR.glob("*.md")))

_DOC_POINTER = re.compile(r"`(docs/[A-Za-z0-9_./-]+\.md)(#[A-Za-z0-9_-]+)?`")


@pytest.mark.parametrize("path", CHECKED_FILES, ids=lambda p: p.name)
def test_every_named_doc_pointer_resolves(path: Path) -> None:
    content = path.read_text(encoding="utf-8")
    pointers = {m.group(1) for m in _DOC_POINTER.finditer(content)}
    missing = [p for p in pointers if not (REPO_ROOT / p).is_file()]
    assert not missing, f"{path.name} points at doc(s) that don't exist: {missing}"


def test_at_least_one_role_file_was_dieted_onto_the_pointer_pattern() -> None:
    """Sanity check the regex itself isn't silently matching nothing —
    would otherwise let every file above pass vacuously."""
    hits = sum(1 for path in CHECKED_FILES if _DOC_POINTER.search(path.read_text(encoding="utf-8")))
    assert hits > 0, "no role file names a docs/*.md pointer — regex likely broken"
