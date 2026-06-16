"""M5#23: `_launch_session` — the common ConPTY launch tail extracted from the
shell / gemini / codex spawn branches.

These pin the provider-specific drift that the extraction had to preserve:
  - codex_exit=True  → stamps codex_spawn_ts + wires _on_codex_exit
  - codex_exit=False → wires the stale-guarded _on_session_exit
  - auto_trust=True  → calls _auto_trust after attach (gemini/codex; NOT shell)
  - success / failure messages carry the provider label
A mocked PtySession stands in for the native ConPTY so no real process spawns.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import Orchestrator

TEST_PROJECT = "launchtest"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(qapp, monkeypatch):
    monkeypatch.setattr(Orchestrator, "_resolve_project", staticmethod(lambda p: p or TEST_PROJECT))
    o = Orchestrator()
    o._idle_watchdog.stop()
    # Neutralise collaborators the tail touches so we observe wiring, not effects.
    o._auto_trust = MagicMock()
    o._on_codex_exit = MagicMock()
    o._on_session_exit = MagicMock()
    o._drain_spawn_queue = MagicMock()
    monkeypatch.setattr(o, "_final_gate_clear", lambda: True)
    return o


def _launch(orch, pane, *, label, codex_exit=False, auto_trust=False):
    """Run _launch_session with PtySession + transcript mocked. Returns
    (ok, msg, mock_session, connected_handler)."""
    connected: list = []
    mock_session = MagicMock()
    mock_session.processExited.connect.side_effect = lambda h: connected.append(h)

    with (
        patch("agent_takkub.orchestrator.PtySession", return_value=mock_session),
        patch("agent_takkub.orchestrator._build_transcript_path", return_value="/tmp/t.log"),
    ):
        ok, msg = orch._launch_session(
            pane=pane,
            role_name=label,
            project_ns=TEST_PROJECT,
            spawn_cwd="/work/dir",
            argv=[label, "--flag"],
            env={"X": "1"},
            pane_tok="tok-" + label,
            label=label,
            cwd=None,
            project=TEST_PROJECT,
            _from_auto_respawn=False,
            _shard_total=0,
            codex_exit=codex_exit,
            auto_trust=auto_trust,
        )
    return ok, msg, mock_session, (connected[0] if connected else None)


def _pane():
    p = MagicMock()
    p.attach_session = MagicMock()
    return p


class TestLaunchSessionCommonTail:
    def test_shell_spawns_and_returns_label_message(self, orch):
        pane = _pane()
        ok, msg, sess, _ = _launch(orch, pane, label="shell")
        assert ok is True
        assert msg == "shell spawned in /work/dir"
        # native spawn invoked with our argv/env/cwd
        sess.spawn.assert_called_once()
        kw = sess.spawn.call_args.kwargs
        assert kw["argv"] == ["shell", "--flag"]
        assert kw["cwd"] == "/work/dir"
        pane.attach_session.assert_called_once()

    def test_shell_does_not_auto_trust(self, orch):
        _launch(orch, _pane(), label="shell")
        orch._auto_trust.assert_not_called()

    def test_gemini_auto_trusts(self, orch):
        _launch(orch, _pane(), label="gemini", auto_trust=True)
        orch._auto_trust.assert_called_once()

    def test_non_codex_wires_session_exit_not_codex(self, orch):
        orch._panes_by_project[TEST_PROJECT] = {}
        pane = _pane()
        _ok, _msg, sess, handler = _launch(orch, pane, label="gemini", auto_trust=True)
        # make the stale-guard see this pane+session as current, then fire
        orch._panes_by_project[TEST_PROJECT] = {"gemini": pane}
        pane.session = sess
        handler(0)
        orch._on_session_exit.assert_called_once()
        orch._on_codex_exit.assert_not_called()

    def test_codex_stamps_spawn_ts_and_wires_codex_exit(self, orch):
        pane = _pane()
        _ok, _msg, _sess, handler = _launch(
            orch, pane, label="codex", codex_exit=True, auto_trust=True
        )
        # codex early-crash bookkeeping recorded
        ps = orch._ps(f"{TEST_PROJECT}::codex")
        assert ps.codex_spawn_ts is not None and ps.codex_spawn_ts > 0
        # the wired handler routes to _on_codex_exit, not _on_session_exit
        handler(0)
        orch._on_codex_exit.assert_called_once()
        orch._on_session_exit.assert_not_called()
        orch._auto_trust.assert_called_once()

    def test_spawn_failure_revokes_token_and_reports(self, orch):
        orch._pane_tokens["tok-codex"] = (TEST_PROJECT, "codex")
        pane = _pane()
        connected: list = []
        mock_session = MagicMock()
        mock_session.spawn.side_effect = RuntimeError("conpty boom")
        with (
            patch("agent_takkub.orchestrator.PtySession", return_value=mock_session),
            patch("agent_takkub.orchestrator._build_transcript_path", return_value="/tmp/t.log"),
        ):
            ok, msg = orch._launch_session(
                pane=pane,
                role_name="codex",
                project_ns=TEST_PROJECT,
                spawn_cwd="/work/dir",
                argv=["codex"],
                env={},
                pane_tok="tok-codex",
                label="codex",
                cwd=None,
                project=TEST_PROJECT,
                _from_auto_respawn=False,
                _shard_total=0,
                codex_exit=True,
            )
        assert ok is False
        assert "failed to spawn codex" in msg
        assert "tok-codex" not in orch._pane_tokens  # revoked on failure
        assert connected == []

    def test_spawn_in_progress_reset_in_finally(self, orch):
        _launch(orch, _pane(), label="shell")
        assert orch._spawn_in_progress is False
        orch._drain_spawn_queue.assert_called()
