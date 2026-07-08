"""Server-side dispatch for the `session-report` command
(takkub session-report -> cli_server).

Mirrors the token-gate pattern in test_cli_server_hook.py: `session-report`
shares the same pane-token / lead-token gate as `done`/`send`/`hook` so
identity is derived server-side from the token rather than trusted from the
caller-supplied `from` field.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.cli_server import CliServer

_PANE_TOKEN_BACKEND = "test-pane-token-backend-abc"
_LEAD_TOKEN = "test-lead-token-abc123"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


class _FakeSock:
    def __init__(self) -> None:
        self._buf = b""

    def write(self, data: bytes) -> None:
        self._buf += data

    def flush(self) -> None:
        pass

    def last_response(self) -> dict:
        line = self._buf.split(b"\n", 1)[0]
        return json.loads(line.decode("utf-8"))

    def reset(self) -> None:
        self._buf = b""


@pytest.fixture
def server_and_sock(qapp: QCoreApplication):
    mock_orch = MagicMock()
    mock_orch._lead_token = _LEAD_TOKEN
    mock_orch._pane_tokens = {_PANE_TOKEN_BACKEND: ("test-project", "backend")}
    mock_orch.consume_session_report.return_value = (True, "")

    srv = CliServer(mock_orch)
    sock = _FakeSock()
    return srv, sock, mock_orch


class TestSessionReportCommandAuth:
    def test_no_token_rejected(self, server_and_sock) -> None:
        srv, sock, _ = server_and_sock
        sock.reset()
        srv._dispatch(
            sock,
            {"cmd": "session-report", "session_id": "abc-123", "from": "backend"},
        )
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "unauthorized" in resp["msg"].lower()

    def test_valid_pane_token_derives_identity(self, server_and_sock) -> None:
        srv, sock, mock_orch = server_and_sock
        sock.reset()
        # Caller lies about `from`/`from_project` — server must override
        # both from the token, same as done()/send()/hook().
        srv._dispatch(
            sock,
            {
                "cmd": "session-report",
                "session_id": "abc-123",
                "source": "resume",
                "cwd": "/proj",
                "from": "someone-else",
                "from_project": "someone-elses-project",
                "auth": _PANE_TOKEN_BACKEND,
            },
        )
        resp = sock.last_response()
        assert resp["ok"] is True
        mock_orch.consume_session_report.assert_called_once()
        args, kwargs = mock_orch.consume_session_report.call_args
        assert args[0] == "backend"
        assert kwargs["project"] == "test-project"
        assert kwargs["session_id"] == "abc-123"
        assert kwargs["source"] == "resume"
        assert kwargs["cwd"] == "/proj"

    def test_lead_token_allowed(self, server_and_sock) -> None:
        srv, sock, mock_orch = server_and_sock
        sock.reset()
        srv._dispatch(
            sock,
            {
                "cmd": "session-report",
                "session_id": "lead-uuid",
                "from": "lead",
                "auth": _LEAD_TOKEN,
            },
        )
        resp = sock.last_response()
        assert resp["ok"] is True
        mock_orch.consume_session_report.assert_called_once()
        assert mock_orch.consume_session_report.call_args[0][0] == "lead"

    def test_failure_msg_propagated(self, server_and_sock) -> None:
        srv, sock, mock_orch = server_and_sock
        mock_orch.consume_session_report.return_value = (False, "missing session_id")
        sock.reset()
        srv._dispatch(
            sock,
            {
                "cmd": "session-report",
                "session_id": "",
                "from": "backend",
                "auth": _PANE_TOKEN_BACKEND,
            },
        )
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "missing session_id" in resp["msg"]
