"""Tests for the default role registry."""

from __future__ import annotations

from agent_takkub import roles


class TestDefaults:
    def test_lead_is_column_zero(self) -> None:
        assert roles.LEAD.column == 0
        assert roles.LEAD.row == 0
        assert roles.LEAD.name == "lead"

    def test_default_teammates_registry(self) -> None:
        assert len(roles.DEFAULT_TEAMMATES) == 8
        names = {r.name for r in roles.DEFAULT_TEAMMATES}
        assert names == {
            "frontend",
            "backend",
            "mobile",
            "devops",
            "designer",
            "qa",
            "reviewer",
            "codex",
        }

    def test_default_columns_assigned(self) -> None:
        # column 1 = middle (dev roles incl. codex), column 2 = right (support)
        cols = {r.name: r.column for r in roles.DEFAULT_TEAMMATES}
        assert cols["frontend"] == 1
        assert cols["backend"] == 1
        assert cols["codex"] == 1
        assert cols["designer"] == 2
        assert cols["reviewer"] == 2


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
