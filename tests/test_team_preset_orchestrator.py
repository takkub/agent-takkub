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

    def test_pair_blocks_qa_allows_reviewer(self, orch: Orchestrator) -> None:
        team_preset.set_current("pair", TEST_PROJECT)
        with patch.object(orch, "spawn") as spawn_mock:
            ok, _ = orch.assign("qa", cwd="/web", task="test", project=TEST_PROJECT)
        assert ok is False
        spawn_mock.assert_not_called()

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
        team_preset.set_current("full", TEST_PROJECT)
        pane = _make_lead()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})[LEAD.name] = pane
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
        send_mock.call_args.args[-1] if send_mock.call_args.args else None
        # task text was prefixed with the [system] notice somewhere in the call
        assert send_mock.called


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
    def test_lead_may_implement_lifts_deny_list(self, tmp_path, monkeypatch):
        import agent_takkub.config as config
        from agent_takkub import lead_context as lc_mod

        pj = tmp_path / "projects.json"
        pj.write_text(
            json.dumps(
                {
                    "active": TEST_PROJECT,
                    "projects": {TEST_PROJECT: {"paths": {"api": str(tmp_path / "api")}}},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "PROJECTS_JSON", pj)
        runtime = tmp_path / "runtime"
        monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
        monkeypatch.setattr(lc_mod, "RUNTIME_DIR", runtime)

        team_preset.set_current("solo-lead", TEST_PROJECT)
        result = lc_mod.render_lead_settings(TEST_PROJECT)
        data = json.loads(result.read_text(encoding="utf-8"))
        assert data["permissions"]["deny"] == []
        assert "Edit" in data["permissions"]["allow"]

    def test_full_preset_keeps_deny_list(self, tmp_path, monkeypatch):
        import agent_takkub.config as config
        from agent_takkub import lead_context as lc_mod

        pj = tmp_path / "projects.json"
        pj.write_text(
            json.dumps(
                {
                    "active": TEST_PROJECT,
                    "projects": {TEST_PROJECT: {"paths": {"api": str(tmp_path / "api")}}},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "PROJECTS_JSON", pj)
        runtime = tmp_path / "runtime"
        monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
        monkeypatch.setattr(lc_mod, "RUNTIME_DIR", runtime)

        team_preset.set_current("full", TEST_PROJECT)
        result = lc_mod.render_lead_settings(TEST_PROJECT)
        data = json.loads(result.read_text(encoding="utf-8"))
        assert data["permissions"]["deny"] != []
        assert "Edit" not in data["permissions"]["allow"]
