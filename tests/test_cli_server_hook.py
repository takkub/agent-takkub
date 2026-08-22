"""Server-side dispatch for the `hook` command (takkub _hook -> cli_server).

Mirrors the token-gate pattern in test_cli_server_auth.py: `hook` shares the
same pane-token / lead-token gate as `done`/`send` so identity is derived
server-side from the token rather than trusted from the caller-supplied
`from` field.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.cli_server import CliServer

from ._qt_timer_leak_guard import stop_timers_after

_PANE_TOKEN_BACKEND = "test-pane-token-backend-abc"
_LEAD_TOKEN = "test-lead-token-abc123"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture(autouse=True)
def _stop_cli_server_reapers(monkeypatch):
    # CliServer.__init__ starts self._reaper (1s repeating) unconditionally
    # (#344), and _dispatch's spawn/assign/pipeline-run path can leave a
    # pending spawn-stagger QTimer behind too (#345) — every `CliServer(...)`
    # in this file otherwise leaves one or more running for the rest of the
    # pytest session. shutdown_timers() stops both kinds.
    finalize = stop_timers_after(monkeypatch, CliServer, "shutdown_timers")
    yield
    finalize()


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
    mock_orch.consume_pane_hook.return_value = (True, False, "")

    srv = CliServer(mock_orch)
    sock = _FakeSock()
    return srv, sock, mock_orch


class TestHookCommandAuth:
    def test_no_token_rejected(self, server_and_sock) -> None:
        srv, sock, _ = server_and_sock
        sock.reset()
        srv._dispatch(sock, {"cmd": "hook", "event": "Stop", "from": "backend"})
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "unauthorized" in resp["msg"].lower()

    def test_valid_pane_token_derives_identity(self, server_and_sock) -> None:
        srv, sock, mock_orch = server_and_sock
        sock.reset()
        # Caller lies about `from` — server must override it from the token.
        srv._dispatch(
            sock,
            {
                "cmd": "hook",
                "event": "Stop",
                "from": "someone-else",
                "auth": _PANE_TOKEN_BACKEND,
            },
        )
        resp = sock.last_response()
        assert resp["ok"] is True
        mock_orch.consume_pane_hook.assert_called_once()
        _, kwargs = mock_orch.consume_pane_hook.call_args
        assert mock_orch.consume_pane_hook.call_args[0][0] == "backend"
        assert kwargs["project"] == "test-project"

    def test_lead_token_allowed(self, server_and_sock) -> None:
        srv, sock, _ = server_and_sock
        sock.reset()
        srv._dispatch(
            sock,
            {"cmd": "hook", "event": "Stop", "from": "lead", "auth": _LEAD_TOKEN},
        )
        resp = sock.last_response()
        assert resp["ok"] is True

    def test_block_flag_propagated_in_reply(self, server_and_sock) -> None:
        srv, sock, mock_orch = server_and_sock
        mock_orch.consume_pane_hook.return_value = (True, True, "รายงานผลด้วย takkub done ก่อนจบ")
        sock.reset()
        srv._dispatch(
            sock,
            {"cmd": "hook", "event": "Stop", "from": "backend", "auth": _PANE_TOKEN_BACKEND},
        )
        resp = sock.last_response()
        assert resp["ok"] is True
        assert resp["block"] is True
        assert "takkub done" in resp["msg"]
