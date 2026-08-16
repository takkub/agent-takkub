"""Guard: every .claude/agents/*.md must carry the host-destructive-kill
rule (#169).

Companion to `test_agent_role_files_have_browser_guard.py`. Root incident
(2026-07-08): a `frontend` pane ran `taskkill /F /T /IM node.exe` to clear a
stuck dev-server port and killed every node process on the box, including
other panes' Claude Code processes — `takkub list` came back with nothing but
`lead`.

Unlike the browser-driver rule, there is no allowlisted role here: no pane
legitimately needs to kill processes host-wide, so every role file carries
the exact same prohibition.

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
SECTION = "Never kill a process by name"


@pytest.mark.parametrize("role_file", ROLE_FILES, ids=lambda p: p.name)
class TestRoleFileHostDestructiveGuard:
    def test_has_section(self, role_file: Path) -> None:
        content = role_file.read_text(encoding="utf-8")
        assert SECTION in content, f"{role_file.name} is missing a '{SECTION}' section"

    def test_names_the_dangerous_commands(self, role_file: Path) -> None:
        """Naming the exact commands is what makes the rule searchable by the
        agent mid-task — a vague prohibition alone doesn't match `taskkill
        /IM node.exe`."""
        content = role_file.read_text(encoding="utf-8").lower()
        for cmd in ("taskkill", "pkill", "killall", "stop-process"):
            assert cmd in content, f"{role_file.name} never mentions {cmd}"

    def test_names_the_pid_alternative(self, role_file: Path) -> None:
        """A prohibition with no safe alternative just leaves the agent
        guessing (or retrying the same dangerous route)."""
        content = role_file.read_text(encoding="utf-8")
        assert "PID" in content, f"{role_file.name} never names the PID-targeted alternative"

    def test_no_role_is_allowlisted(self, role_file: Path) -> None:
        """Unlike the browser-driver rule, this one has no permission
        variant — every role file must carry the prohibition, never a grant."""
        content = role_file.read_text(encoding="utf-8")
        assert "Never kill a process by name" in content, (
            f"{role_file.name} is missing the prohibition wording"
        )
