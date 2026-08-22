"""Tests for the #313 SpawnTargetCorrupt retry/backoff path in spawn_engine.

`PtySession.spawn()` is fully mocked throughout — nothing here spawns a real
process or a native pty backend, so there's no risk of the OS hard-error
dialog these tests are a reaction to. See
`docs/audit/2026-08-20-issue-313-spawn-deadlock.md` for the reproduction
that isn't safe to automate, and `test_pty_backend_target_validation.py`
for the pre-flight header check these retries exist downstream of.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub._pty_backend import SpawnTargetCorrupt
from agent_takkub.orchestrator import Orchestrator
from agent_takkub.spawn_engine import _CORRUPT_SPAWN_MAX_RETRIES

TEST_PROJECT = "corruptspawntest"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _make_orchestrator(qapp, monkeypatch):
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda p: p or TEST_PROJECT),
    )
    o = Orchestrator()
    o.shutdown_timers()
    return o


def _make_pane(role: str):
    pane = MagicMock()
    pane.role = MagicMock()
    pane.role.name = role
    pane.session = None
    pane.state = "empty"
    pane._transcript_path = None
    return pane


def _spawn_with_scripted_pty(orch, role, monkeypatch, tmp_path, spawn_side_effects):
    """Drives orch.spawn(role) with a PtySession whose .spawn() raises/succeeds
    per `spawn_side_effects` (consumed one per real spawn attempt), and a
    QTimer.singleShot that runs its callback immediately instead of on a real
    timer — so a retry chain resolves synchronously within the test."""
    from agent_takkub import pane_tools_policy as ptp
    from agent_takkub import shared_dev_tools as sdt

    monkeypatch.setattr(sdt, "SHARED_MCP_FILE", tmp_path / "shared-mcp.json")
    monkeypatch.setattr(ptp, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")

    def _immediate_single_shot(_delay_ms, callback):
        callback()

    with (
        patch.object(orch, "_is_spawn_blocked", return_value=False),
        patch.object(orch, "_final_gate_clear", return_value=True),
        # CI runners have no claude installed anywhere — without this stub the
        # claude branch dies at find_claude_executable() before ever reaching
        # the mocked PtySession (run 32337550027, all 3 OSes; dev machines
        # always pass because the locator falls back to absolute install
        # paths, not just PATH). Same stub pattern as test_hook_wiring.py.
        patch("agent_takkub.orchestrator.find_claude_executable", return_value="claude"),
        patch("agent_takkub.orchestrator.PtySession") as mock_pty_cls,
        patch("agent_takkub.orchestrator.QTimer.singleShot", side_effect=_immediate_single_shot),
        patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
        patch("agent_takkub.orchestrator.inject_user_profile_env"),
    ):
        mock_pty = MagicMock()
        mock_pty.spawn.side_effect = list(spawn_side_effects)
        mock_pty_cls.return_value = mock_pty
        for pane in orch._panes_by_project.get(TEST_PROJECT, {}).values():
            pane.attach_session = MagicMock()

        ok, msg = orch.spawn(role, project=TEST_PROJECT)

    return ok, msg, mock_pty


class TestClaudeBranchCorruptRetry:
    """The inline claude spawn branch (role defaults to the claude provider)."""

    def test_recovers_after_one_corrupt_attempt(self, qapp, monkeypatch, tmp_path):
        orch = _make_orchestrator(qapp, monkeypatch)
        pane = _make_pane("backend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}

        ok, msg, mock_pty = _spawn_with_scripted_pty(
            orch,
            "backend",
            monkeypatch,
            tmp_path,
            [SpawnTargetCorrupt("mid-write"), None],
        )

        assert ok is True, msg
        assert mock_pty.spawn.call_count == 2, "must have retried exactly once"
        ekey = f"{TEST_PROJECT}::backend"
        assert orch._pane_state[ekey].corrupt_spawn_retries == 0, "resets on success"

    def test_gives_up_after_max_retries(self, qapp, monkeypatch, tmp_path):
        # Each retry is scheduled via QTimer.singleShot, which the test stub
        # (_immediate_single_shot) runs synchronously — so the *outermost*
        # orch.spawn() call here still returns the immediate "deferred,
        # retrying" response from its own first attempt (exactly like the
        # existing _toctou_redefer pattern: scheduling a retry is itself a
        # synchronous success). The actual give-up only happens several
        # attempts deep in that synchronous retry chain, so it's observed
        # here via the settled pane-state counter and the logged events,
        # not the outermost call's own return value.
        orch = _make_orchestrator(qapp, monkeypatch)
        pane = _make_pane("backend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}

        # The claude branch lives inside the giant Orchestrator.spawn()
        # method, which rebinds _log_event locally via
        # _from_orch("_log_event") (see spawn_engine._from_orch) — that
        # reads orchestrator's module dict first, so patch it there rather
        # than spawn_engine's own module-level import (same pattern
        # test_spawn_codex_argv.py's TestMcpHandshakeLogging uses).
        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            "agent_takkub.orchestrator._log_event",
            lambda event, **kw: events.append((event, kw)),
        )

        always_corrupt = [SpawnTargetCorrupt("mid-write")] * (_CORRUPT_SPAWN_MAX_RETRIES + 1)
        ok, msg, mock_pty = _spawn_with_scripted_pty(
            orch, "backend", monkeypatch, tmp_path, always_corrupt
        )

        assert ok is True, msg  # the outermost call's own attempt just deferred
        assert mock_pty.spawn.call_count == _CORRUPT_SPAWN_MAX_RETRIES + 1
        ekey = f"{TEST_PROJECT}::backend"
        assert orch._pane_state[ekey].corrupt_spawn_retries == _CORRUPT_SPAWN_MAX_RETRIES + 1

        retries = [kw for name, kw in events if name == "spawn_target_corrupt_retry"]
        assert len(retries) == _CORRUPT_SPAWN_MAX_RETRIES
        giveups = [
            kw
            for name, kw in events
            if name == "spawn_native_failed" and kw.get("err", "").startswith("SpawnTargetCorrupt")
        ]
        assert len(giveups) == 1

    def test_generic_failure_is_unaffected(self, qapp, monkeypatch, tmp_path):
        """A non-SpawnTargetCorrupt failure must still fail immediately, with
        no retry — the retry path is specific to this one exception type."""
        orch = _make_orchestrator(qapp, monkeypatch)
        pane = _make_pane("backend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}

        ok, msg, mock_pty = _spawn_with_scripted_pty(
            orch, "backend", monkeypatch, tmp_path, [FileNotFoundError("nope")]
        )

        assert ok is False
        assert mock_pty.spawn.call_count == 1
        assert "gave up" not in msg


class TestNonClaudeBranchCorruptRetry:
    """_launch_session (shell/gemini/codex tail) hits the same guard."""

    def test_recovers_after_one_corrupt_attempt(self, qapp, monkeypatch, tmp_path):
        from agent_takkub.provider_config import GEMINI

        orch = _make_orchestrator(qapp, monkeypatch)
        pane = _make_pane("gemini")
        orch._panes_by_project[TEST_PROJECT] = {"gemini": pane}

        with (
            patch(
                "agent_takkub.provider_config.effective_provider_for",
                return_value=GEMINI,
            ),
            # ProviderSpec is frozen — can't patch custom_discovery_fn on the
            # instance; pin shutil.which instead (same technique as
            # test_provider_project_scope.py). CI runners have no agy anywhere,
            # so without this the spawn dies at spec discovery before ever
            # reaching the mocked PtySession (CI run 32338427010, all 3 OSes).
            patch(
                "shutil.which",
                side_effect=lambda n, *a, **kw: "agy" if str(n).startswith("agy") else None,
            ),
        ):
            ok, msg, mock_pty = _spawn_with_scripted_pty(
                orch,
                "gemini",
                monkeypatch,
                tmp_path,
                [SpawnTargetCorrupt("mid-write"), None],
            )

        assert ok is True, msg
        assert mock_pty.spawn.call_count == 2
        ekey = f"{TEST_PROJECT}::gemini"
        assert orch._pane_state[ekey].corrupt_spawn_retries == 0
