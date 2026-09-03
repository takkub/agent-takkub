"""Guard: every .claude/agents/*.md must carry the test-placement
conventions section (#478).

Companion to `test_agent_role_files_have_pip_editable_guard.py` and the
other `test_agent_role_files_have_*_guard.py` files — same shape, same
reason: a role file is the only enforcement a non-claude pane ever sees
(#103 multi-provider), so the prose is load-bearing, not decoration.

Root motivation (user directive 2026-09-03, "อยากให้เป็นระบบ ระเบียบเรียบร้อย"):
without a stated convention, panes scattered test files inconsistently
(ad-hoc `__tests__/` here, a loose `test.py` at repo root there, stray
screenshots outside the designated artifacts folder) — every specialist
role needs the same placement rules stated up front, not re-derived per
task.

Two places carry this content and both are pinned here:

* `.claude/agents/*.md` (16 files) — read natively by a **claude** pane,
  including the 5 "claude standing in for <provider>" slot files
  (codex.md/gemini.md/opencode.md/kimi.md/cursor.md).
* `codex_agents_md.CODEX_AGENTS_MD` — the single shared cheatsheet
  planted as `AGENTS.md` for a **real** non-claude provider (codex,
  gemini-agy, opencode, kimi, cursor all share `context_strategy=
  "agents_md_file"`, see `provider_spec.py`) — this is the only file a
  real non-claude CLI ever reads, so it must carry the same convention
  even though it isn't a `.claude/agents/*.md` role file itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

AGENTS_DIR = Path(__file__).parent.parent / ".claude" / "agents"
ROLE_FILES = sorted(AGENTS_DIR.glob("*.md"))
SECTION = "กติกาวางเทส (test placement conventions"


@pytest.mark.parametrize("role_file", ROLE_FILES, ids=lambda p: p.name)
class TestRoleFileTestConventionsGuard:
    def test_has_section(self, role_file: Path) -> None:
        content = role_file.read_text(encoding="utf-8")
        assert SECTION in content, f"{role_file.name} is missing a '{SECTION}' section"

    def test_names_python_and_node_placement(self, role_file: Path) -> None:
        """Vague "write tests" doesn't tell a pane WHERE to put the file —
        pin the exact per-language pattern so it's searchable mid-task."""
        content = role_file.read_text(encoding="utf-8")
        for token in ("tests/test_<module>.py", "<file>.spec.ts", "<file>.test.ts"):
            assert token in content, f"{role_file.name} never mentions {token!r}"

    def test_forbids_inventing_a_new_test_convention(self, role_file: Path) -> None:
        """A pane that invents its own test folder on an unfamiliar project
        fragments the project's own convention — it must follow what
        already exists, or say so and move on rather than deciding alone."""
        content = role_file.read_text(encoding="utf-8")
        assert "ห้ามสร้างโฟลเดอร์" in content, (
            f"{role_file.name} is missing the no-new-test-convention rule"
        )

    def test_forbids_scratch_files_in_repo(self, role_file: Path) -> None:
        content = role_file.read_text(encoding="utf-8")
        assert "ห้ามทิ้งไฟล์ scratch ใน repo" in content, (
            f"{role_file.name} is missing the no-scratch-files-in-repo rule"
        )


class TestCodexAgentsMdTestConventionsGuard:
    """`codex_agents_md.CODEX_AGENTS_MD` is what a REAL non-claude
    provider reads (planted as `AGENTS.md` via `ensure_agents_md`,
    refreshed unconditionally on every spawn — see that module's
    docstring) — the parametrized class above only covers claude
    standing in for that provider's slot, a different code path."""

    def test_has_section(self) -> None:
        from agent_takkub.codex_agents_md import CODEX_AGENTS_MD

        assert "Test placement conventions" in CODEX_AGENTS_MD

    def test_names_python_and_node_placement(self) -> None:
        from agent_takkub.codex_agents_md import CODEX_AGENTS_MD

        for token in ("tests/test_<module>.py", "<file>.spec.ts", "<file>.test.ts"):
            assert token in CODEX_AGENTS_MD, f"CODEX_AGENTS_MD never mentions {token!r}"

    def test_forbids_scratch_files_in_repo(self) -> None:
        from agent_takkub.codex_agents_md import CODEX_AGENTS_MD

        assert "Never leave scratch files in the repo" in CODEX_AGENTS_MD
