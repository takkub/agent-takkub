"""Guard: every .claude/agents/*.md must carry the pip-editable-install rule
(#202).

Companion to `test_agent_role_files_have_host_destructive_guard.py`. Root
incident (2026-08-14): a `backend` pane ran `pip install -e .` from inside its
own `--isolation worktree` checkout, which rewrote the SHARED venv's
`__editable__*.pth` to point at that worktree's `src/`. Once the Lead removed
the worktree after merging, the main tree's `.venv` broke entirely
(`ModuleNotFoundError`) — and while the pane was still alive, every other
process sharing that venv (including a `qa` full-suite run mid-flight)
silently imported code from the wrong worktree.

Unlike the browser-driver rule, there is no allowlisted role here: no pane
legitimately needs to reinstall the package into a venv every other pane
shares, so every role file carries the exact same prohibition.

Two enforcement layers have to stay in sync, and this pins both:

* `pane_guard.py` — a real `PreToolUse` block, but **claude panes only**.
  Covered by `tests/test_pane_guard.py`.
* the role files here — the only enforcement a non-claude pane ever sees
  (#103 multi-provider), so the prose is load-bearing, not decoration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

AGENTS_DIR = Path(__file__).parent.parent / ".claude" / "agents"
ROLE_FILES = sorted(AGENTS_DIR.glob("*.md"))
SECTION = "ห้าม pip install -e"


@pytest.mark.parametrize("role_file", ROLE_FILES, ids=lambda p: p.name)
class TestRoleFilePipEditableGuard:
    def test_has_section(self, role_file: Path) -> None:
        content = role_file.read_text(encoding="utf-8")
        assert SECTION in content, f"{role_file.name} is missing a '{SECTION}' section"

    def test_names_the_dangerous_commands(self, role_file: Path) -> None:
        """Naming the exact flags is what makes the rule searchable by the
        agent mid-task — a vague prohibition alone doesn't match
        `pip install -e .`."""
        content = role_file.read_text(encoding="utf-8").lower()
        for token in ("pip install -e", "--editable"):
            assert token in content, f"{role_file.name} never mentions {token!r}"

    def test_names_the_safe_alternative(self, role_file: Path) -> None:
        """A prohibition with no safe alternative just leaves the agent
        guessing (or retrying the same dangerous route)."""
        content = role_file.read_text(encoding="utf-8")
        assert "pytest" in content, (
            f"{role_file.name} never names pytest as the reinstall-free alternative"
        )

    def test_no_role_is_allowlisted(self, role_file: Path) -> None:
        """No role legitimately reinstalls the shared venv mid-task — every
        role file must carry the prohibition, never a grant."""
        content = role_file.read_text(encoding="utf-8")
        assert "ห้ามรัน `pip install -e" in content, (
            f"{role_file.name} is missing the prohibition wording"
        )
