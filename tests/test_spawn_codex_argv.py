"""Tests for codex spawn argv (issue #26 Mode B).

`-s workspace-write` blocks outbound network by default (per codex docs),
including loopback — and `takkub done` reports back over a loopback TCP
socket to the cockpit's cli_server. Without
`-c sandbox_workspace_write.network_access=true` that connect gets
sandboxed away, so codex finishes its task but can never call
`takkub done` and the pane hangs "working" forever.

These tests pin down:
  - macOS/Linux codex argv includes `-c sandbox_workspace_write.network_access=true`
  - Windows codex argv is unaffected (still the bypass-sandbox escape hatch)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import Orchestrator

TEST_PROJECT = "codexargvtest"


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
    o._idle_watchdog.stop()
    return o


def _make_codex_pane(role: str = "codex"):
    pane = MagicMock()
    pane.role = MagicMock()
    pane.role.name = role
    pane.session = None
    pane.state = "empty"
    pane._transcript_path = None
    return pane


def _spawn_codex_and_capture_argv(qapp, monkeypatch, platform: str):
    from agent_takkub.provider_config import CODEX

    orch = _make_orchestrator(qapp, monkeypatch)
    pane = _make_codex_pane("codex")
    orch._panes_by_project[TEST_PROJECT] = {"codex": pane}

    pty_spawn_calls = []

    with (
        patch.object(orch, "_is_spawn_blocked", return_value=False),
        patch.object(orch, "_final_gate_clear", return_value=True),
        patch("agent_takkub.orchestrator.PtySession") as mock_pty_cls,
        patch("agent_takkub.orchestrator.QTimer.singleShot"),
        patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
        patch("agent_takkub.spawn_engine.sys.platform", platform),
        patch(
            "agent_takkub.provider_config.effective_provider_for",
            return_value=CODEX,
        ),
        patch(
            "agent_takkub.codex_helper.find_codex_executable",
            return_value="codex",
        ),
        patch("agent_takkub.codex_agents_md.ensure_agents_md"),
        patch("agent_takkub.orchestrator.inject_user_profile_env"),
    ):
        mock_pty = MagicMock()
        mock_pty.spawn.side_effect = lambda **kw: pty_spawn_calls.append(kw)
        mock_pty_cls.return_value = mock_pty
        pane.attach_session = MagicMock()

        ok, msg = orch.spawn("codex", project=TEST_PROJECT)

    assert ok is True, msg
    assert pty_spawn_calls, "PtySession.spawn was not called"
    return pty_spawn_calls[0]["argv"]


class TestCodexArgvNetworkAccess:
    def test_macos_codex_argv_opens_workspace_write_network(self, qapp, monkeypatch):
        argv = _spawn_codex_and_capture_argv(qapp, monkeypatch, "darwin")

        assert "-s" in argv and "workspace-write" in argv
        assert "-c" in argv
        idx = argv.index("-c")
        assert argv[idx + 1] == "sandbox_workspace_write.network_access=true"

    def test_linux_codex_argv_opens_workspace_write_network(self, qapp, monkeypatch):
        argv = _spawn_codex_and_capture_argv(qapp, monkeypatch, "linux")

        assert "-c" in argv
        idx = argv.index("-c")
        assert argv[idx + 1] == "sandbox_workspace_write.network_access=true"

    def test_windows_codex_argv_unaffected(self, qapp, monkeypatch):
        argv = _spawn_codex_and_capture_argv(qapp, monkeypatch, "win32")

        assert argv == ["codex", "--dangerously-bypass-approvals-and-sandbox"]
        assert "-c" not in argv
