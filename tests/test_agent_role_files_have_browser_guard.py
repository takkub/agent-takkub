"""Guard: every .claude/agents/*.md must carry the browser-driver rule.

Companion to `test_agent_role_files_have_git_guard.py`, added after a
`frontend` pane was caught running `npx --yes playwright` (2026-07-23) — the
MCP tool policy had denied it the browser MCP, but nothing stopped it taking
the shell route.

Two enforcement layers have to stay in sync, and this pins both:

* `pane_guard.py` — a real `PreToolUse` block, but **claude panes only**
  (Claude Code hooks don't exist for codex / gemini-agy / opencode / kimi /
  cursor). Covered by `tests/test_pane_guard.py`.
* the role files here — the only enforcement a non-claude pane ever sees
  (#103 multi-provider), so the prose is load-bearing, not decoration.

Roles in `pane_guard.BROWSER_ROLES` get the *permission* half of the section
instead of the prohibition; they still must carry the whole-disk-scan rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub import pane_guard

AGENTS_DIR = Path(__file__).parent.parent / ".claude" / "agents"
ROLE_FILES = sorted(AGENTS_DIR.glob("*.md"))
SECTION = "## Browser & เครื่องมือหนัก"


def _is_browser_role(role_file: Path) -> bool:
    return role_file.stem in pane_guard.BROWSER_ROLES


@pytest.mark.parametrize("role_file", ROLE_FILES, ids=lambda p: p.name)
class TestRoleFileBrowserGuard:
    def test_has_browser_section(self, role_file: Path) -> None:
        content = role_file.read_text(encoding="utf-8")
        assert SECTION in content, f"{role_file.name} is missing a '{SECTION}' section"

    def test_names_the_browser_drivers(self, role_file: Path) -> None:
        """Naming the packages is what makes the rule searchable by the agent
        mid-task — 'don't drive a browser' alone doesn't match `npx playwright`.

        Browser roles only need `playwright` (the tool they're handed); the
        denied roles need the whole family spelled out, since the point is to
        pre-empt every substitute the pane might reach for next."""
        content = role_file.read_text(encoding="utf-8").lower()
        required = ("playwright",) if _is_browser_role(role_file) else ("playwright", "puppeteer")
        for pkg in required:
            assert pkg in content, f"{role_file.name} never mentions {pkg}"

    def test_forbids_whole_disk_scan(self, role_file: Path) -> None:
        content = role_file.read_text(encoding="utf-8")
        assert "ห้ามสแกนทั้งไดรฟ์" in content, (
            f"{role_file.name} is missing the whole-disk-scan prohibition"
        )

    def test_permission_matches_pane_guard(self, role_file: Path) -> None:
        """The prose and `pane_guard.BROWSER_ROLES` must agree: a role told it
        may drive a browser while the hook blocks it would deadlock the pane
        (and vice versa — a role told 'no' that the hook lets through is the
        exact hole this whole change closes)."""
        content = role_file.read_text(encoding="utf-8")
        if _is_browser_role(role_file):
            assert "ได้สิทธิ์ขับ browser" in content, (
                f"{role_file.name} is in pane_guard.BROWSER_ROLES but its role "
                "file does not grant browser access"
            )
            assert "ห้ามติดตั้งหรือรัน browser driver เอง" not in content, (
                f"{role_file.name} is a browser role but carries the prohibition"
            )
        else:
            assert "ห้ามติดตั้งหรือรัน browser driver เอง" in content, (
                f"{role_file.name} is not in pane_guard.BROWSER_ROLES but its "
                "role file never forbids driving a browser"
            )
            assert "ได้สิทธิ์ขับ browser" not in content, (
                f"{role_file.name} is not a browser role but grants access"
            )

    def test_non_browser_roles_point_at_qa(self, role_file: Path) -> None:
        """A denial with no hand-off just makes the pane retry another way."""
        if _is_browser_role(role_file):
            pytest.skip("browser role — no hand-off needed")
        content = role_file.read_text(encoding="utf-8")
        assert "takkub done" in content and "qa" in content, (
            f"{role_file.name} forbids browser work without naming the qa hand-off"
        )


def test_every_browser_role_has_a_role_file() -> None:
    """`pane_guard.BROWSER_ROLES` may not name a role with no prompt behind
    it — a non-claude pane would then get no rule at all."""
    stems = {p.stem for p in ROLE_FILES}
    missing = pane_guard.BROWSER_ROLES - stems
    assert not missing, f"BROWSER_ROLES names roles with no .claude/agents file: {missing}"
