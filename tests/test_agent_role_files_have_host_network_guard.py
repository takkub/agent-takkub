"""Guard: every .claude/agents/*.md must carry the host-network-change rule
(#400).

Companion to `test_agent_role_files_have_host_destructive_guard.py`. Root
incident: a pane ran `netsh wlan connect` to test a networking change and
switched the host's active Wi-Fi network, dropping the user (and every other
pane's live sessions) off the internet with zero warning.

Unlike the browser-driver rule, there is no allowlisted role here: the host
machine's network belongs to the user at the keyboard, never to a sandboxed
pane, so every role file carries the exact same prohibition.

Two enforcement layers have to stay in sync, and this pins both:

* `pane_guard.py` — a real `PreToolUse` block, but **claude panes only**.
  Covered by `tests/test_pane_guard.py::TestHostNetworkDenied`.
* the role files here — the only enforcement a non-claude pane ever sees
  (#103 multi-provider), so the prose is load-bearing, not decoration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

AGENTS_DIR = Path(__file__).parent.parent / ".claude" / "agents"
ROLE_FILES = sorted(AGENTS_DIR.glob("*.md"))
SECTION = "ห้ามเปลี่ยน network ของเครื่อง host"


@pytest.mark.parametrize("role_file", ROLE_FILES, ids=lambda p: p.name)
class TestRoleFileHostNetworkGuard:
    def test_has_section(self, role_file: Path) -> None:
        content = role_file.read_text(encoding="utf-8")
        assert SECTION in content, f"{role_file.name} is missing a '{SECTION}' section"

    def test_names_the_dangerous_commands(self, role_file: Path) -> None:
        """Naming the exact commands is what makes the rule searchable by the
        agent mid-task — a vague prohibition alone doesn't match `netsh wlan
        connect` or `networksetup -setairportpower`."""
        content = role_file.read_text(encoding="utf-8").lower()
        for cmd in ("netsh", "ipconfig", "networksetup", "ifconfig"):
            assert cmd in content, f"{role_file.name} never mentions {cmd}"

    def test_names_the_safe_alternative(self, role_file: Path) -> None:
        """A prohibition with no safe alternative just leaves the agent
        guessing (or retrying the same dangerous route)."""
        content = role_file.read_text(encoding="utf-8")
        assert "มือถือ" in content, f"{role_file.name} never names the second-device alternative"

    def test_no_role_is_allowlisted(self, role_file: Path) -> None:
        """Unlike the browser-driver rule, this one has no permission
        variant — every role file must carry the prohibition, never a grant."""
        content = role_file.read_text(encoding="utf-8")
        assert SECTION in content, f"{role_file.name} is missing the prohibition wording"
