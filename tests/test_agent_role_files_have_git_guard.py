"""Guard: every .claude/agents/*.md must contain a Version control section
with explicit git commit and git push prohibitions.

This test suite was added after the backend agent violated the no-commit rule
(commit 9b3ee54). The guard ensures new role files can't be added without the
hard rule, and existing files can't have it accidentally removed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

AGENTS_DIR = Path(__file__).parent.parent / ".claude" / "agents"
ROLE_FILES = sorted(AGENTS_DIR.glob("*.md"))


@pytest.mark.parametrize("role_file", ROLE_FILES, ids=lambda p: p.name)
class TestRoleFileGitGuard:
    def test_has_version_control_section(self, role_file: Path) -> None:
        content = role_file.read_text(encoding="utf-8")
        assert "Version control" in content, (
            f"{role_file.name} is missing a 'Version control' section"
        )

    def test_forbids_git_commit(self, role_file: Path) -> None:
        content = role_file.read_text(encoding="utf-8")
        assert "git commit" in content, (
            f"{role_file.name} does not mention 'git commit' prohibition"
        )

    def test_forbids_git_push(self, role_file: Path) -> None:
        content = role_file.read_text(encoding="utf-8")
        assert "git push" in content, f"{role_file.name} does not mention 'git push' prohibition"

    def test_has_prohibition_marker(self, role_file: Path) -> None:
        content = role_file.read_text(encoding="utf-8")
        # Thai "ห้าม" or English "NEVER" — both acceptable
        has_marker = "ห้าม" in content or "NEVER" in content
        assert has_marker, f"{role_file.name} has no prohibition marker (ห้าม / NEVER)"
