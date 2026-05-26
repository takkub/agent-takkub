"""Tests for cli_server harvest + harvest-done dispatch.

The existing test_cli_server_auth.py parametrizes over _LEAD_ONLY_CMDS, so
harvest / harvest-done auth rejection is covered automatically once they're
added to that set. These tests cover the happy-path dispatch and the
synthesize-done IPC path.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.cli_server import CliServer


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


_REAL_TOKEN = "test-harvest-token-xyz"

_SAMPLE_ARTIFACTS = [
    {"path": "/proj/src/foo.py", "mtime_ts": 1_700_000_000.0, "mtime_rel": "5m ago"},
]


@pytest.fixture
def srv_and_sock(qapp: QCoreApplication):
    mock_orch = MagicMock()
    mock_orch._lead_token = _REAL_TOKEN
    mock_orch.harvest_info.return_value = (
        True,
        "ok",
        {
            "state": "working",
            "spawn_ts": 1_700_000_000.0,
            "since_ts": 1_699_996_400.0,
            "artifacts": _SAMPLE_ARTIFACTS,
        },
    )
    mock_orch.done.return_value = (True, "backend reported done")
    srv = CliServer(mock_orch)
    sock = _FakeSock()
    return srv, sock, mock_orch


def _auth_payload(cmd: str, **extra) -> dict:
    return {"cmd": cmd, "from": "lead", "auth": _REAL_TOKEN, **extra}


class TestHarvestDispatch:
    def test_harvest_calls_harvest_info_and_returns_artifacts(self, srv_and_sock) -> None:
        srv, sock, mock_orch = srv_and_sock
        srv._dispatch(sock, _auth_payload("harvest", role="backend"))
        resp = sock.last_response()
        assert resp["ok"] is True
        assert resp["artifacts"] == _SAMPLE_ARTIFACTS
        assert resp["state"] == "working"
        mock_orch.harvest_info.assert_called_once()

    def test_harvest_passes_since_as_timestamp(self, srv_and_sock) -> None:
        srv, sock, mock_orch = srv_and_sock
        sock.reset()
        mock_orch.harvest_info.reset_mock()
        # Construct an HH:MM that is definitely in the past today
        past_hhmm = "00:01"
        srv._dispatch(sock, _auth_payload("harvest", role="backend", since=past_hhmm))
        resp = sock.last_response()
        assert resp["ok"] is True
        call_kwargs = mock_orch.harvest_info.call_args
        since_arg = call_kwargs[1].get("since_ts") or call_kwargs[0][2]
        assert since_arg is not None
        assert since_arg > 0

    def test_harvest_role_not_running_returns_error(self, srv_and_sock) -> None:
        srv, sock, mock_orch = srv_and_sock
        sock.reset()
        mock_orch.harvest_info.return_value = (False, "role not running: qa", {})
        srv._dispatch(sock, _auth_payload("harvest", role="qa"))
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "role not running" in resp["msg"]

    def test_harvest_bad_since_format_returns_error(self, srv_and_sock) -> None:
        srv, sock, _mock_orch = srv_and_sock
        sock.reset()
        srv._dispatch(sock, _auth_payload("harvest", role="backend", since="bad"))
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "--since" in resp["msg"] or "format" in resp["msg"]

    def test_harvest_passes_limit(self, srv_and_sock) -> None:
        srv, sock, mock_orch = srv_and_sock
        sock.reset()
        mock_orch.harvest_info.reset_mock()
        mock_orch.harvest_info.return_value = (
            True,
            "ok",
            {
                "state": "working",
                "spawn_ts": 0.0,
                "since_ts": 0.0,
                "artifacts": [],
            },
        )
        srv._dispatch(sock, _auth_payload("harvest", role="backend", limit=42))
        call_kwargs = mock_orch.harvest_info.call_args
        limit_arg = call_kwargs[1].get("limit") or call_kwargs[0][3]
        assert limit_arg == 42


class TestHarvestDoneDispatch:
    def test_harvest_done_calls_orchestrator_done(self, srv_and_sock) -> None:
        srv, sock, mock_orch = srv_and_sock
        sock.reset()
        mock_orch.done.return_value = (True, "backend reported done")
        srv._dispatch(
            sock,
            _auth_payload("harvest-done", role="backend", note="harvest: 3 artifact(s)"),
        )
        resp = sock.last_response()
        assert resp["ok"] is True
        mock_orch.done.assert_called_once()
        call_args = mock_orch.done.call_args[0]
        assert call_args[0] == "backend"

    def test_harvest_done_propagates_done_failure(self, srv_and_sock) -> None:
        srv, sock, mock_orch = srv_and_sock
        sock.reset()
        mock_orch.done.return_value = (False, "working tree dirty")
        srv._dispatch(sock, _auth_payload("harvest-done", role="backend"))
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "dirty" in resp["msg"]

    def test_harvest_done_uses_default_note(self, srv_and_sock) -> None:
        srv, sock, mock_orch = srv_and_sock
        sock.reset()
        mock_orch.done.return_value = (True, "ok")
        mock_orch.done.reset_mock()
        srv._dispatch(sock, _auth_payload("harvest-done", role="backend"))
        mock_orch.done.assert_called_once()
        _, kwargs = mock_orch.done.call_args
        note = kwargs.get("note", "")
        assert isinstance(note, str) and len(note) > 0
