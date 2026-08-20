"""Guard: every .claude/agents/*.md must carry the git-commit-is-Lead-only
rule (#314).

Companion to `test_agent_role_files_have_pip_editable_guard.py`. Root
incident: an ~8h overnight session where `backend` and a custom `admin`
role self-committed on a task instruction of "commit เอง" while `frontend`
in the same session refused the identical instruction — every role file
carried the same prohibition in prose, so the difference was how
convincingly each task text argued past it, not policy. That is exactly
the "prose alone is a suggestion, not enforcement" gap `browser_driver`
(#169-era) closed for the MCP tool policy; `pane_guard.git_lead_only`
closes it here.

Two enforcement layers have to stay in sync, and this pins both:

* `pane_guard.py` — a real `PreToolUse` block, but **claude panes only**.
  Covered by `tests/test_pane_guard.py::TestGitLeadOnlyDenied` +
  `TestGitLeadOnlyWorktreeCarveOut`.
* the role files here — the only enforcement a non-claude pane ever sees
  (#103 multi-provider), so the prose is load-bearing, not decoration.

Unlike the browser-driver rule, there is no allowlisted role here: no
teammate role legitimately commits on the shared tree, so every role file
carries the exact same prohibition (Lead's own CLAUDE.md is not one of
these files — Lead is exempt by role, not by an opt-out written here).
"""

from __future__ import annotations

from pathlib import Path

import pytest

AGENTS_DIR = Path(__file__).parent.parent / ".claude" / "agents"
ROLE_FILES = sorted(AGENTS_DIR.glob("*.md"))


@pytest.mark.parametrize("role_file", ROLE_FILES, ids=lambda p: p.name)
class TestRoleFileGitCommitGuard:
    def test_names_the_dangerous_commands(self, role_file: Path) -> None:
        """Naming the exact subcommands is what makes the rule searchable by
        the agent mid-task — a vague "only Lead handles version control"
        alone doesn't match a specific `git commit -m ...` it's about to
        run. `rebase`/`merge`/`checkout` are named without a leading `git`
        prefix in one file's compact style (qa.md); the bare command names
        alone are enough to satisfy the same searchability contract."""
        content = role_file.read_text(encoding="utf-8")
        for token in ("git commit", "git push", "git reset --hard", "git branch -D"):
            assert token in content, f"{role_file.name} never mentions {token!r}"
        content_lower = content.lower()
        for token in ("rebase", "merge", "checkout"):
            assert token in content_lower, f"{role_file.name} never mentions {token!r}"

    def test_names_the_safe_alternative(self, role_file: Path) -> None:
        """A prohibition with no safe alternative just leaves the agent
        guessing (or retrying the same commit a different way)."""
        content = role_file.read_text(encoding="utf-8")
        assert "takkub done" in content, (
            f"{role_file.name} never names `takkub done` as the report-and-hand-off route"
        )

    def test_no_role_is_allowlisted(self, role_file: Path) -> None:
        """No teammate role legitimately commits on the shared tree — every
        role file must carry the prohibition, never a grant."""
        content = role_file.read_text(encoding="utf-8")
        assert "only Lead" in content, (
            f"{role_file.name} is missing the 'only Lead handles version control' wording"
        )

    def test_claude_hard_block_disclosed(self, role_file: Path) -> None:
        """#103: a non-claude pane only ever sees this prose — the role file
        has to say so, not leave it implied, so a codex/gemini/opencode/
        kimi/cursor pane knows prose is the *only* thing stopping it (a
        claude pane blocked by pane_guard doesn't need to know that to obey
        the rule, but it doesn't hurt either)."""
        content = role_file.read_text(encoding="utf-8")
        assert "pane_guard.py" in content, (
            f"{role_file.name} doesn't disclose the claude-only hard-block gap (#103)"
        )
