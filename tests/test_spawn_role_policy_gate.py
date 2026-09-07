"""#510/#512 H6 regression: is_role_enabled/can_spawn must be enforced at the
actual spawn() boundary, not only by callers (assign/cli_server/pipeline_executor)
that check before invoking spawn(). Session restore, stuck recovery, and the
deferred-spawn/queue-drain retries in spawn_engine.py all call self.spawn()
directly and bypassed both policies before this fix.

These tests exercise the REAL pipeline_config.is_role_enabled and
team_preset.can_spawn (isolated to a tmp store, never mocked) against the REAL
Orchestrator.spawn() — only the native process-launch dependencies (PtySession,
find_claude_executable, env builders) are mocked, matching the pattern in
test_spawn_gate.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import pipeline_config, team_preset
from agent_takkub.orchestrator import Orchestrator

TEST_PROJECT = "roletest"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture(autouse=True)
def _isolate_policy_stores(tmp_path, monkeypatch):
    """Real stores, redirected to tmp files — never mock is_role_enabled/can_spawn."""
    monkeypatch.setattr(pipeline_config, "_PATH", tmp_path / "pipelines.json")
    monkeypatch.setattr(pipeline_config, "_BASE_DIR", tmp_path / "pipeline_projects")
    monkeypatch.setattr(team_preset, "_BASE_DIR", tmp_path / "team")


def _make_orchestrator(qapp, monkeypatch):
    monkeypatch.setattr(Orchestrator, "_resolve_project", staticmethod(lambda p: p or TEST_PROJECT))
    o = Orchestrator()
    o.shutdown_timers()
    return o


def _make_dead_pane(role: str = "backend"):
    pane = MagicMock()
    pane.role = MagicMock()
    pane.role.name = role
    pane.session = None
    pane.state = "empty"
    pane.deferred_spawn = False
    return pane


def _disable_role(role: str) -> None:
    state = pipeline_config.load(TEST_PROJECT)
    state["rolesEnabled"][role] = False
    pipeline_config.save(state, TEST_PROJECT)


class TestRoleDisabledBlocksRealSpawnBoundary:
    def test_disabled_role_blocks_spawn_no_native_launch(self, qapp, monkeypatch):
        _disable_role("backend")
        assert pipeline_config.is_role_enabled("backend", TEST_PROJECT) is False

        orch = _make_orchestrator(qapp, monkeypatch)
        pane = _make_dead_pane("backend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}

        with (
            patch.object(orch, "_is_spawn_blocked", return_value=False),
            patch("agent_takkub.orchestrator.PtySession") as mock_pty_cls,
            patch("agent_takkub.orchestrator.find_claude_executable", return_value="claude"),
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
            patch("agent_takkub.orchestrator._build_lead_env", return_value={}),
        ):
            pane.attach_session = MagicMock()
            ok, msg = orch.spawn("backend", project=TEST_PROJECT)

        assert ok is False
        assert "backend" in msg
        assert not mock_pty_cls.called
        pane.attach_session.assert_not_called()

    def test_disabled_role_via_shard_suffix_also_blocked(self, qapp, monkeypatch):
        """qa#2 must read as disabled when the base role qa is off (#510 shard rule)."""
        _disable_role("qa")

        orch = _make_orchestrator(qapp, monkeypatch)
        pane = _make_dead_pane("qa#2")
        orch._panes_by_project[TEST_PROJECT] = {"qa#2": pane}

        with (
            patch.object(orch, "_is_spawn_blocked", return_value=False),
            patch("agent_takkub.orchestrator.PtySession") as mock_pty_cls,
            patch("agent_takkub.orchestrator.find_claude_executable", return_value="claude"),
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
            patch("agent_takkub.orchestrator._build_lead_env", return_value={}),
        ):
            pane.attach_session = MagicMock()
            ok, _msg = orch.spawn("qa#2", project=TEST_PROJECT)

        assert ok is False
        assert not mock_pty_cls.called
        pane.attach_session.assert_not_called()

    def test_enabled_role_still_spawns_normally(self, qapp, monkeypatch):
        """Sanity: the new check must not block a role nobody disabled."""
        orch = _make_orchestrator(qapp, monkeypatch)
        pane = _make_dead_pane("backend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}

        with (
            patch.object(orch, "_is_spawn_blocked", return_value=False),
            patch("agent_takkub.orchestrator.PtySession") as mock_pty_cls,
            patch("agent_takkub.orchestrator.find_claude_executable", return_value="claude"),
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
            patch("agent_takkub.orchestrator._build_lead_env", return_value={}),
        ):
            mock_pty = MagicMock()
            mock_pty.is_alive = True
            mock_pty_cls.return_value = mock_pty
            pane.attach_session = MagicMock()
            ok, _msg = orch.spawn("backend", project=TEST_PROJECT)

        assert ok is True


class TestTeamPresetExclusionBlocksRealSpawnBoundary:
    def test_solo_lead_excluded_role_blocks_spawn(self, qapp, monkeypatch):
        team_preset.set_current("solo-lead", TEST_PROJECT)
        ok_policy, _ = team_preset.can_spawn("backend", TEST_PROJECT)
        assert ok_policy is False

        orch = _make_orchestrator(qapp, monkeypatch)
        pane = _make_dead_pane("backend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}

        with (
            patch.object(orch, "_is_spawn_blocked", return_value=False),
            patch("agent_takkub.orchestrator.PtySession") as mock_pty_cls,
            patch("agent_takkub.orchestrator.find_claude_executable", return_value="claude"),
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
            patch("agent_takkub.orchestrator._build_lead_env", return_value={}),
        ):
            pane.attach_session = MagicMock()
            ok, msg = orch.spawn("backend", project=TEST_PROJECT)

        assert ok is False
        assert "team preset" in msg
        assert not mock_pty_cls.called
        pane.attach_session.assert_not_called()

    def test_lead_role_never_blocked_by_team_preset(self, qapp, monkeypatch):
        from agent_takkub.roles import LEAD

        team_preset.set_current("solo-lead", TEST_PROJECT)

        orch = _make_orchestrator(qapp, monkeypatch)
        pane = _make_dead_pane(LEAD.name)
        orch._panes_by_project[TEST_PROJECT] = {LEAD.name: pane}

        with (
            patch.object(orch, "_is_spawn_blocked", return_value=False),
            patch("agent_takkub.orchestrator.PtySession") as mock_pty_cls,
            patch("agent_takkub.orchestrator.find_claude_executable", return_value="claude"),
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
            patch("agent_takkub.orchestrator._build_lead_env", return_value={}),
        ):
            mock_pty = MagicMock()
            mock_pty.is_alive = True
            mock_pty_cls.return_value = mock_pty
            pane.attach_session = MagicMock()
            ok, _msg = orch.spawn(LEAD.name, project=TEST_PROJECT)

        assert ok is True


class TestAlreadyAlivePaneReconnectNeverBlocked:
    """An already-running pane must be reusable regardless of a role/preset
    change made after it was spawned — only NEW session launches are gated."""

    def test_disabled_role_alive_pane_still_reconnects(self, qapp, monkeypatch):
        _disable_role("backend")

        orch = _make_orchestrator(qapp, monkeypatch)
        pane = _make_dead_pane("backend")
        alive = MagicMock()
        alive.is_alive = True
        pane.session = alive
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}

        ok, msg = orch.spawn("backend", project=TEST_PROJECT)

        assert ok is True
        assert "already running" in msg


class TestBlockedThroughIndirectSpawnRoutes:
    """H6's actual regression: routes that call self.spawn() directly
    (deferred-spawn retry, queue drain, session restore, stuck recovery) must
    inherit the same gate — there is no separate check to add per-route."""

    def test_retry_deferred_spawn_after_gate_clear_still_blocked(self, qapp, monkeypatch):
        _disable_role("backend")

        orch = _make_orchestrator(qapp, monkeypatch)
        pane = _make_dead_pane("backend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}

        timer_calls = []
        with (
            patch(
                "agent_takkub.orchestrator.QTimer.singleShot",
                side_effect=lambda d, fn: timer_calls.append((d, fn)),
            ),
            patch.object(orch, "_is_spawn_blocked", return_value=False),
            patch("agent_takkub.orchestrator.PtySession") as mock_pty_cls,
            patch("agent_takkub.orchestrator.find_claude_executable", return_value="claude"),
            patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
            patch("agent_takkub.orchestrator._build_lead_env", return_value={}),
        ):
            pane.attach_session = MagicMock()
            orch._retry_deferred_spawn("backend", None, TEST_PROJECT, False, 0)
            # Fire the 35ms re-entrant spawn() call the retry scheduled.
            assert timer_calls, "retry must schedule the follow-up spawn() call"
            follow_up = timer_calls[-1][1]
            follow_up()

        assert not mock_pty_cls.called, "disabled role must not reach native spawn via retry"
        pane.attach_session.assert_not_called()

    def test_direct_spawn_call_mirrors_restore_and_recovery_call_shape(self, qapp, monkeypatch):
        """restore_teammates/_auto_recover_stuck call self.spawn(role, cwd=...,
        project=..., _from_auto_respawn=...) with no policy pre-check of their
        own — this is that exact call shape, proving the fix covers it."""
        _disable_role("backend")

        orch = _make_orchestrator(qapp, monkeypatch)
        pane = _make_dead_pane("backend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}

        with (
            patch.object(orch, "_is_spawn_blocked", return_value=False),
            patch("agent_takkub.orchestrator.PtySession") as mock_pty_cls,
            patch("agent_takkub.orchestrator.find_claude_executable", return_value="claude"),
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
            patch("agent_takkub.orchestrator._build_lead_env", return_value={}),
        ):
            pane.attach_session = MagicMock()
            ok, _msg = orch.spawn(
                "backend", cwd=None, project=TEST_PROJECT, _from_auto_respawn=True
            )

        assert ok is False
        assert not mock_pty_cls.called
        pane.attach_session.assert_not_called()
