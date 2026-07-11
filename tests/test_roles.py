"""Tests for the default role registry."""

from __future__ import annotations

import pytest

from agent_takkub import roles


@pytest.fixture
def clean_custom_registry():
    """Isolate roles._CUSTOM (the A6 runtime custom-role registry) per test."""
    saved = dict(roles._CUSTOM)
    roles._CUSTOM.clear()
    yield
    roles._CUSTOM.clear()
    roles._CUSTOM.update(saved)


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


class TestCustomRoles:
    def test_register_role_resolves_via_by_name(self, clean_custom_registry) -> None:
        r = roles.Role("data-eng", "Data Eng", "#112233", column=1, row=5)
        roles.register_role(r)
        assert roles.by_name("data-eng") is r

    def test_unregistered_custom_role_still_none(self, clean_custom_registry) -> None:
        assert roles.by_name("data-eng") is None

    def test_custom_roles_accessor_returns_registered(self, clean_custom_registry) -> None:
        r = roles.Role("data-eng", "Data Eng", "#112233", column=1, row=5)
        roles.register_role(r)
        assert roles.custom_roles() == (r,)

    def test_builtin_roles_take_priority_over_custom(self, clean_custom_registry) -> None:
        # Can't actually happen via custom_roles.validate_role_name (blocks
        # the collision), but by_name's own lookup order should still favor
        # ALL_DEFAULT if it were ever attempted.
        shadow = roles.Role("backend", "Shadow Backend", "#000000", column=1, row=99)
        roles.register_role(shadow)
        assert roles.by_name("backend") is roles.by_name("backend")
        assert roles.by_name("backend").label == "Backend"

    def test_unregister_role_forgets_it(self, clean_custom_registry) -> None:
        r = roles.Role("data-eng", "Data Eng", "#112233", column=1, row=5)
        roles.register_role(r)
        roles.unregister_role("data-eng")
        assert roles.by_name("data-eng") is None
        assert roles.custom_roles() == ()

    def test_unregister_role_unknown_name_is_a_noop(self, clean_custom_registry) -> None:
        roles.unregister_role("never-registered")  # must not raise


class TestAllRoleNames:
    """all_role_names() is the single source of truth every UI/config
    surface (pipeline_config.valid_roles, pane_tools_dialog.matrix_roles,
    pane_tools_policy.known_roles_base, ...) is meant to derive from."""

    def test_include_lead_true_matches_all_default(self, clean_custom_registry) -> None:
        assert roles.all_role_names() == tuple(r.name for r in roles.ALL_DEFAULT)

    def test_include_lead_false_excludes_lead_only(self, clean_custom_registry) -> None:
        names = roles.all_role_names(include_lead=False)
        assert "lead" not in names
        assert names == tuple(r.name for r in roles.DEFAULT_TEAMMATES)

    def test_picks_up_a_freshly_registered_custom_role(self, clean_custom_registry) -> None:
        r = roles.Role("data-eng", "Data Eng", "#112233", column=1, row=5)
        roles.register_role(r)
        assert "data-eng" in roles.all_role_names()
        assert "data-eng" in roles.all_role_names(include_lead=False)

    def test_forgets_an_unregistered_custom_role(self, clean_custom_registry) -> None:
        r = roles.Role("data-eng", "Data Eng", "#112233", column=1, row=5)
        roles.register_role(r)
        roles.unregister_role("data-eng")
        assert "data-eng" not in roles.all_role_names()
