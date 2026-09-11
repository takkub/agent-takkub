"""#512 — Orchestrator-level team preset integration:
- assign() rejects a role the project's team preset roster doesn't cover
- --team override (assign --role lead --team ...) sets the override + notice
- set_team_preset() / clear_team_preset_override() broadcast to that
  project's Lead pane only (not every project's)
- render_lead_settings() lifts the deny-list when the preset lets Lead edit
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import team_preset
from agent_takkub.orchestrator import LEAD, Orchestrator

TEST_PROJECT = "teampresettest"
OTHER_PROJECT = "otherproject"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda project: project or TEST_PROJECT),
    )
    o = Orchestrator()
    o.shutdown_timers()
    return o


@pytest.fixture(autouse=True)
def _isolate_team_preset(tmp_path, monkeypatch):
    monkeypatch.setattr(team_preset, "_BASE_DIR", tmp_path)


def _make_lead() -> MagicMock:
    lead = MagicMock()
    lead.session = MagicMock()
    lead.session.is_alive = True
    return lead


class TestAssignRejectsPresetGovernedRole:
    def test_solo_lead_blocks_backend_spawn(self, orch: Orchestrator) -> None:
        team_preset.set_current("solo-lead", TEST_PROJECT)
        with patch.object(orch, "spawn") as spawn_mock:
            ok, msg = orch.assign("backend", cwd="/api", task="fix bug", project=TEST_PROJECT)
        assert ok is False
        assert "team preset" in msg
        spawn_mock.assert_not_called()

    def test_full_preset_allows_backend_spawn(self, orch: Orchestrator) -> None:
        team_preset.set_current("full", TEST_PROJECT)
        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready"),
        ):
            ok, _ = orch.assign("backend", cwd="/api", task="fix bug", project=TEST_PROJECT)
        assert ok is True

    def test_pair_allows_reviewer_and_qa_alias(self, orch: Orchestrator) -> None:
        """qa is #513's legacy alias for reviewer --mode e2e — with pair's
        checker=reviewer, assign("qa") must resolve through the alias and
        spawn, same as assign("reviewer") itself (live-test repro fix)."""
        team_preset.set_current("pair", TEST_PROJECT)
        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready"),
        ):
            ok, _ = orch.assign("qa", cwd="/web", task="test", project=TEST_PROJECT)
        assert ok is True

        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready"),
        ):
            ok, _ = orch.assign("reviewer", cwd="/web", task="review", project=TEST_PROJECT)
        assert ok is True

    def test_providers_and_shell_never_blocked(self, orch: Orchestrator) -> None:
        team_preset.set_current("solo-lead", TEST_PROJECT)
        for role in ("codex", "gemini", "shell", "critic"):
            with (
                patch.object(orch, "spawn", return_value=(True, "spawned")),
                patch.object(orch, "_send_when_ready"),
            ):
                ok, _ = orch.assign(role, cwd="/web", task="x", project=TEST_PROJECT)
            assert ok is True, f"{role} should never be gated by team preset"


class TestAssignTeamOverride:
    def test_team_override_only_valid_for_lead_role(self, orch: Orchestrator) -> None:
        ok, msg = orch.assign(
            "backend", cwd="/api", task="x", project=TEST_PROJECT, team="solo-lead"
        )
        assert ok is False
        assert "lead" in msg

    def test_team_override_rejects_unknown_preset(self, orch: Orchestrator) -> None:
        ok, _msg = orch.assign(
            LEAD.name, cwd=None, task="do the thing", project=TEST_PROJECT, team="nope"
        )
        assert ok is False

    def test_team_override_sets_project_override_and_prefixes_task(
        self, orch: Orchestrator
    ) -> None:
        """No live Lead pane yet (fresh spawn) — override applies immediately,
        it will be picked up by render_lead_settings at that upcoming spawn."""
        team_preset.set_current("full", TEST_PROJECT)
        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready") as send_mock,
        ):
            ok, _ = orch.assign(
                LEAD.name, cwd=None, task="fix a typo", project=TEST_PROJECT, team="solo-lead"
            )
        assert ok is True
        assert team_preset.active_override(TEST_PROJECT) == "solo-lead"
        # the effective preset for the project is now solo-lead (override wins)
        assert team_preset.current(TEST_PROJECT)["preset"] == "solo-lead"
        # standing preset untouched
        assert team_preset.current_preset_id(TEST_PROJECT) == "full"
        # task text was prefixed with the [system] notice somewhere in the call
        assert send_mock.called

    def test_team_override_blocked_when_lead_already_running(self, orch: Orchestrator) -> None:
        """#510/#512 M7: a live Lead pane keeps the edit-permission guard it
        was spawned with — set_override only takes effect at Lead's NEXT
        spawn, so accepting the override here would silently lie to the
        operator about it governing "this task". Must refuse and say so."""
        team_preset.set_current("full", TEST_PROJECT)
        pane = _make_lead()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})[LEAD.name] = pane
        with (
            patch.object(orch, "spawn") as spawn_mock,
            patch.object(orch, "_send_when_ready") as send_mock,
        ):
            ok, msg = orch.assign(
                LEAD.name, cwd=None, task="fix a typo", project=TEST_PROJECT, team="solo-lead"
            )
        assert ok is False
        assert "restart" in msg.lower() or "Lead" in msg
        # override must NOT have been set — nothing silently half-applied
        assert team_preset.active_override(TEST_PROJECT) is None
        assert team_preset.current_preset_id(TEST_PROJECT) == "full"
        spawn_mock.assert_not_called()
        send_mock.assert_not_called()

    def test_team_override_proceeds_when_lead_pane_exists_but_dead(
        self, orch: Orchestrator
    ) -> None:
        """A registered-but-not-alive Lead pane (previously closed) is the
        same as no pane — the override should apply normally."""
        team_preset.set_current("full", TEST_PROJECT)
        dead_pane = MagicMock()
        dead_pane.session = None
        orch._panes_by_project.setdefault(TEST_PROJECT, {})[LEAD.name] = dead_pane
        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready"),
        ):
            ok, _ = orch.assign(
                LEAD.name, cwd=None, task="fix a typo", project=TEST_PROJECT, team="solo-lead"
            )
        assert ok is True
        assert team_preset.active_override(TEST_PROJECT) == "solo-lead"


class TestAssignAutoPresetSuggestion:
    """#510/#512 M2 (review 2026-09-07): a fresh Lead assign with no --team,
    on a project still at the default "auto" standing preset with no active
    override, must actually consult routing_planner.suggest_team_size and
    surface it to Lead — advisory only, nothing enforced."""

    def test_auto_preset_prepends_suggestion_to_task(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert team_preset.current_preset_id(TEST_PROJECT) == "auto"
        monkeypatch.setattr(
            "agent_takkub.routing_planner.suggest_team_size",
            lambda task, context=None: ("solo-lead", "งานเดี่ยว scope ชัด — ทำเองได้"),
        )
        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready") as send_mock,
        ):
            ok, _ = orch.assign(LEAD.name, cwd=None, task="fix a typo", project=TEST_PROJECT)
        assert ok is True
        sent_task = send_mock.call_args.args[1]
        assert "auto team-preset suggestion" in sent_task
        assert "solo-lead" in sent_task
        assert sent_task.endswith("fix a typo")
        # advisory only — the standing preset itself is untouched
        assert team_preset.current_preset_id(TEST_PROJECT) == "auto"
        assert team_preset.active_override(TEST_PROJECT) is None

    def test_non_auto_standing_preset_skips_suggestion(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        team_preset.set_current("full", TEST_PROJECT)
        sugg_mock = MagicMock(return_value=("solo-lead", "x"))
        monkeypatch.setattr("agent_takkub.routing_planner.suggest_team_size", sugg_mock)
        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready") as send_mock,
        ):
            ok, _ = orch.assign(LEAD.name, cwd=None, task="fix a typo", project=TEST_PROJECT)
        assert ok is True
        assert send_mock.call_args.args[1] == "fix a typo"
        sugg_mock.assert_not_called()

    def test_active_override_skips_suggestion(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        team_preset.set_override("pair", TEST_PROJECT)
        sugg_mock = MagicMock(return_value=("solo-lead", "x"))
        monkeypatch.setattr("agent_takkub.routing_planner.suggest_team_size", sugg_mock)
        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready") as send_mock,
        ):
            ok, _ = orch.assign(LEAD.name, cwd=None, task="fix a typo", project=TEST_PROJECT)
        assert ok is True
        assert send_mock.call_args.args[1] == "fix a typo"
        sugg_mock.assert_not_called()

    def test_explicit_team_override_skips_suggestion(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--team on this same assign call takes the `if team is not None`
        branch instead — the two are mutually exclusive, not stacked."""
        sugg_mock = MagicMock(return_value=("solo-lead", "x"))
        monkeypatch.setattr("agent_takkub.routing_planner.suggest_team_size", sugg_mock)
        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready") as send_mock,
        ):
            ok, _ = orch.assign(
                LEAD.name, cwd=None, task="fix a typo", project=TEST_PROJECT, team="pair"
            )
        assert ok is True
        sugg_mock.assert_not_called()
        assert "team preset (งานนี้)" in send_mock.call_args.args[1]

    def test_non_lead_role_skips_suggestion(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        team_preset.set_current("full", TEST_PROJECT)  # allow backend to spawn
        sugg_mock = MagicMock(return_value=("solo-lead", "x"))
        monkeypatch.setattr("agent_takkub.routing_planner.suggest_team_size", sugg_mock)
        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready"),
        ):
            orch.assign("backend", cwd="/api", task="fix bug", project=TEST_PROJECT)
        sugg_mock.assert_not_called()


class TestSetTeamPresetBroadcast:
    def test_broadcasts_only_to_that_projects_lead(self, orch: Orchestrator) -> None:
        lead_a = _make_lead()
        lead_b = _make_lead()
        orch._panes_by_project[TEST_PROJECT] = {LEAD.name: lead_a}
        orch._panes_by_project[OTHER_PROJECT] = {LEAD.name: lead_b}

        ok, _ = orch.set_team_preset("solo-lead", project=TEST_PROJECT)
        assert ok is True
        assert lead_a.session.write.called
        assert not lead_b.session.write.called

    def test_rejects_unknown_preset(self, orch: Orchestrator) -> None:
        ok, _msg = orch.set_team_preset("nonsense", project=TEST_PROJECT)
        assert ok is False

    def test_clear_override_restores_standing_and_notifies(self, orch: Orchestrator) -> None:
        team_preset.set_current("full", TEST_PROJECT)
        team_preset.set_override("solo-lead", TEST_PROJECT)
        lead = _make_lead()
        orch._panes_by_project[TEST_PROJECT] = {LEAD.name: lead}

        ok, _ = orch.clear_team_preset_override(project=TEST_PROJECT)
        assert ok is True
        assert team_preset.current(TEST_PROJECT)["preset"] == "full"
        assert lead.session.write.called


class TestRenderLeadSettingsPresetAware:
    def test_lead_may_implement_lifts_deny_list(self, tmp_path, monkeypatch, seed_projects):
        from agent_takkub import config
        from agent_takkub import lead_context as lc_mod

        runtime = tmp_path / "runtime"
        monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
        monkeypatch.setattr(lc_mod, "RUNTIME_DIR", runtime)
        seed_projects(
            tmp_path,
            {TEST_PROJECT: {"paths": {"api": str(tmp_path / "api")}}},
            active=TEST_PROJECT,
        )

        team_preset.set_current("solo-lead", TEST_PROJECT)
        result = lc_mod.render_lead_settings(TEST_PROJECT)
        data = json.loads(result.read_text(encoding="utf-8"))
        assert data["permissions"]["deny"] == []
        assert "Edit" in data["permissions"]["allow"]

    def test_full_preset_keeps_deny_list(self, tmp_path, monkeypatch, seed_projects):
        from agent_takkub import config
        from agent_takkub import lead_context as lc_mod

        runtime = tmp_path / "runtime"
        monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
        monkeypatch.setattr(lc_mod, "RUNTIME_DIR", runtime)
        seed_projects(
            tmp_path,
            {TEST_PROJECT: {"paths": {"api": str(tmp_path / "api")}}},
            active=TEST_PROJECT,
        )

        team_preset.set_current("full", TEST_PROJECT)
        result = lc_mod.render_lead_settings(TEST_PROJECT)
        data = json.loads(result.read_text(encoding="utf-8"))
        assert data["permissions"]["deny"] != []
        assert "Edit" not in data["permissions"]["allow"]
