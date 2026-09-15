"""Tests for agent_takkub.__main__ dispatch and CLI server ping (#632).

Prevents regression where `python -m agent_takkub <cli-args>` accidentally booted
the GUI application rather than executing the CLI command.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

import agent_takkub.__main__ as main_entrypoint
from agent_takkub.cli_server import CliServer


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


class TestMainEntrypointDispatch:
    def test_cli_args_forward_to_cli_main(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_cli = MagicMock(return_value=0)
        mock_app = MagicMock(return_value=0)

        import agent_takkub.app
        import agent_takkub.cli

        monkeypatch.setattr(agent_takkub.cli, "main", mock_cli)
        monkeypatch.setattr(agent_takkub.app, "main", mock_app)

        ret = main_entrypoint.main(["report", "build", "--type", "customer"])
        assert ret == 0
        mock_cli.assert_called_once_with(["report", "build", "--type", "customer"])
        mock_app.assert_not_called()

    def test_empty_args_calls_app_main(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_cli = MagicMock(return_value=0)
        mock_app = MagicMock(return_value=0)

        import agent_takkub.app
        import agent_takkub.cli

        monkeypatch.setattr(agent_takkub.cli, "main", mock_cli)
        monkeypatch.setattr(agent_takkub.app, "main", mock_app)

        ret = main_entrypoint.main([])
        assert ret == 0
        mock_app.assert_called_once_with([])
        mock_cli.assert_not_called()

    @pytest.mark.parametrize("flag", ["-platform", "-style", "-stylesheet", "-geometry"])
    def test_qt_flags_call_app_main(self, flag: str, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_cli = MagicMock(return_value=0)
        mock_app = MagicMock(return_value=0)

        import agent_takkub.app
        import agent_takkub.cli

        monkeypatch.setattr(agent_takkub.cli, "main", mock_cli)
        monkeypatch.setattr(agent_takkub.app, "main", mock_app)

        ret = main_entrypoint.main([flag, "offscreen"])
        assert ret == 0
        mock_app.assert_called_once_with([flag, "offscreen"])
        mock_cli.assert_not_called()


class _FakeSock:
    def __init__(self) -> None:
        self.written = b""

    def write(self, b) -> None:
        self.written += bytes(b)

    def flush(self) -> None:
        pass


class _FakeServer:
    def serverPort(self) -> int:
        return 65432


class TestCliServerPing:
    def test_ping_command_returns_pong_with_pid(self, qapp: QCoreApplication) -> None:
        srv = CliServer(MagicMock())
        try:
            srv._server = _FakeServer()
            sock = _FakeSock()

            srv._dispatch(sock, {"cmd": "ping"})
            replies = [
                json.loads(line) for line in sock.written.decode().splitlines() if line.strip()
            ]
            assert len(replies) == 1
            resp = replies[0]
            assert resp.get("ok") is True
            assert resp.get("msg") == "pong"
            assert resp.get("port") == 65432
            assert isinstance(resp.get("pid"), int)
        finally:
            srv.shutdown_timers()

    def test_instance_identity_returns_pid(self, qapp: QCoreApplication) -> None:
        srv = CliServer(MagicMock())
        try:
            srv._server = _FakeServer()
            sock = _FakeSock()

            srv._dispatch(sock, {"cmd": "instance-identity"})
            replies = [
                json.loads(line) for line in sock.written.decode().splitlines() if line.strip()
            ]
            assert len(replies) == 1
            resp = replies[0]
            assert resp.get("ok") is True
            assert resp.get("msg") == "instance identity"
            assert isinstance(resp.get("pid"), int)
        finally:
            srv.shutdown_timers()
