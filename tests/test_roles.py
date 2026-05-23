"""Tests for the default role registry."""

from __future__ import annotations

from agent_takkub import roles


class TestDefaults:
    def test_lead_is_column_zero(self) -> None:
        assert roles.LEAD.column == 0
        assert roles.LEAD.row == 0
        assert roles.LEAD.name == "lead"

    def test_default_teammates_registry(self) -> None:
        assert len(roles.DEFAULT_TEAMMATES) == 10
        names = {r.name for r in roles.DEFAULT_TEAMMATES}
        assert names == {
            "frontend",
            "backend",
            "mobile",
            "devops",
            "gemini",
            "qa",
            "reviewer",
            "codex",
            "critic",
            "shell",
        }
        # Designer was retired from defaults but the agent file
        # `.claude/agents/designer.md` is preserved for custom add.
        # Critic (Design Critic) replaces designer with a post-QA visual
        # review workflow (shots → gemini → propose) rather than the old
        # Figma-to-code spec.
        assert "designer" not in names

    def test_default_columns_assigned(self) -> None:
        cols = {r.name: r.column for r in roles.DEFAULT_TEAMMATES}
        assert cols["frontend"] == 1
        assert cols["backend"] == 1
        assert cols["codex"] == 1
        assert cols["gemini"] == 2
        assert cols["reviewer"] == 2
        assert cols["critic"] == 2

    def test_critic_slot_below_reviewer(self) -> None:
        critic = roles.by_name("critic")
        assert critic is not None
        assert critic.column == 2
        assert critic.row == 3
        assert critic.label == "Design Critic"

    def test_shell_slot_below_critic(self) -> None:
        shell = roles.by_name("shell")
        assert shell is not None
        assert shell.column == 2
        assert shell.row == 4
        assert shell.label == "Shell"

    def test_gemini_slot_takes_old_designer_position(self) -> None:
        # Gemini replaces designer at col=2 row=0 - the top-right slot
        # right next to qa/reviewer.
        gemini = roles.by_name("gemini")
        assert gemini is not None
        assert gemini.column == 2
        assert gemini.row == 0
        assert gemini.label == "Gemini"


class TestByName:
    def test_known_role(self) -> None:
        r = roles.by_name("backend")
        assert r is not None
        assert r.label == "Backend"

    def test_unknown_role_returns_none(self) -> None:
        assert roles.by_name("data-eng") is None

    def test_case_insensitive(self) -> None:
        assert roles.by_name("FRONTEND") is not None
        assert roles.by_name("  Frontend  ") is not None

    def test_lead_is_findable(self) -> None:
        assert roles.by_name("lead") is roles.LEAD
