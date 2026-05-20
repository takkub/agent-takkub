"""Server-side auth gate for Lead-only CLI commands.

Tests call `CliServer._dispatch()` directly with a fake socket so they
exercise the authorization logic without needing a running Qt event loop
or a real TCP connection. This proves raw JSON payloads cannot perform
Lead-only actions even when cli.py's role gate is bypassed.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.cli_server import _LEAD_ONLY_CMDS, CliServer

# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


class _FakeSock:
    """Minimal stand-in for QTcpSocket that captures written bytes."""

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
    """Return a (CliServer, FakeSock, real_token) triple.

    The CliServer is wired to a MagicMock orchestrator whose `_lead_token`
    attribute is set to a known value so tests can check both the reject
    path (wrong token) and the accept path (correct token).
    """
    real_token = "test-lead-token-abc123"
    mock_orch = MagicMock()
    mock_orch._lead_token = real_token

    # Stub out orchestrator methods that Lead-only cmds call
    mock_orch.spawn.return_value = (True, "spawned")
    mock_orch.assign.return_value = (True, "assigned")
    mock_orch.close.return_value = (True, "closed")
    mock_orch.close_all_teammates.return_value = (True, "all closed")
    mock_orch.done.return_value = (True, "done")
    mock_orch.send.return_value = (True, "sent")
    mock_orch.list_status.return_value = {}

    srv = CliServer(mock_orch)
    sock = _FakeSock()
    return srv, sock, real_token


# ─────────────────────────────────────────────────────────────
# Lead-only commands without any auth field → unauthorized
# ─────────────────────────────────────────────────────────────


class TestLeadOnlyCommandsRejectedWithNoAuth:
    @pytest.mark.parametrize("cmd", sorted(_LEAD_ONLY_CMDS))
    def test_no_auth_field_rejected(self, server_and_sock, cmd: str) -> None:
        srv, sock, _ = server_and_sock
        sock.reset()
        payload: dict = {"cmd": cmd, "role": "backend", "task": "do x"}
        srv._dispatch(sock, payload)
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "unauthorized" in resp["msg"].lower()


# ─────────────────────────────────────────────────────────────
# Lead-only commands with wrong auth → unauthorized
# ─────────────────────────────────────────────────────────────


class TestLeadOnlyCommandsRejectedWithWrongToken:
    @pytest.mark.parametrize("cmd", sorted(_LEAD_ONLY_CMDS))
    def test_wrong_token_rejected(self, server_and_sock, cmd: str) -> None:
        srv, sock, _ = server_and_sock
        sock.reset()
        payload = {"cmd": cmd, "role": "backend", "task": "do x", "auth": "not-the-real-token"}
        srv._dispatch(sock, payload)
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "unauthorized" in resp["msg"].lower()

    def test_empty_string_auth_rejected(self, server_and_sock) -> None:
        srv, sock, _ = server_and_sock
        sock.reset()
        srv._dispatch(sock, {"cmd": "spawn", "role": "frontend", "auth": ""})
        resp = sock.last_response()
        assert resp["ok"] is False

    def test_none_auth_rejected(self, server_and_sock) -> None:
        srv, sock, _ = server_and_sock
        sock.reset()
        srv._dispatch(sock, {"cmd": "assign", "role": "frontend", "task": "x", "auth": None})
        resp = sock.last_response()
        assert resp["ok"] is False


# ─────────────────────────────────────────────────────────────
# Lead-only commands with correct token → accepted
# ─────────────────────────────────────────────────────────────


class TestLeadOnlyCommandsAcceptedWithCorrectToken:
    def test_spawn_accepted(self, server_and_sock) -> None:
        srv, sock, token = server_and_sock
        sock.reset()
        srv._dispatch(sock, {"cmd": "spawn", "role": "frontend", "auth": token})
        resp = sock.last_response()
        assert resp["ok"] is True

    def test_assign_accepted(self, server_and_sock) -> None:
        srv, sock, token = server_and_sock
        sock.reset()
        srv._dispatch(sock, {"cmd": "assign", "role": "backend", "task": "work", "auth": token})
        resp = sock.last_response()
        assert resp["ok"] is True

    def test_close_accepted(self, server_and_sock) -> None:
        srv, sock, token = server_and_sock
        sock.reset()
        srv._dispatch(sock, {"cmd": "close", "role": "qa", "auth": token})
        resp = sock.last_response()
        assert resp["ok"] is True

    def test_close_all_accepted(self, server_and_sock) -> None:
        srv, sock, token = server_and_sock
        sock.reset()
        srv._dispatch(sock, {"cmd": "close-all", "auth": token})
        resp = sock.last_response()
        assert resp["ok"] is True


# ─────────────────────────────────────────────────────────────
# done: rejected for lead, allowed for teammates
# ─────────────────────────────────────────────────────────────


class TestDoneCommand:
    def test_done_from_lead_rejected(self, server_and_sock) -> None:
        srv, sock, _ = server_and_sock
        sock.reset()
        srv._dispatch(sock, {"cmd": "done", "from": "lead", "note": ""})
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "lead cannot" in resp["msg"].lower()

    def test_done_from_teammate_allowed(self, server_and_sock) -> None:
        srv, sock, _ = server_and_sock
        sock.reset()
        srv._dispatch(sock, {"cmd": "done", "from": "backend", "note": "finished"})
        resp = sock.last_response()
        assert resp["ok"] is True

    def test_done_with_no_from_field_allowed(self, server_and_sock) -> None:
        """Raw TCP client that omits 'from' should not be blocked by the done gate."""
        srv, sock, _ = server_and_sock
        sock.reset()
        srv._dispatch(sock, {"cmd": "done", "note": "x"})
        resp = sock.last_response()
        assert resp["ok"] is True


# ─────────────────────────────────────────────────────────────
# Non-Lead commands don't require auth
# ─────────────────────────────────────────────────────────────


class TestNonLeadCommandsPassThrough:
    def test_list_requires_no_auth(self, server_and_sock) -> None:
        srv, sock, _ = server_and_sock
        sock.reset()
        srv._dispatch(sock, {"cmd": "list"})
        resp = sock.last_response()
        assert resp["ok"] is True

    def test_send_requires_no_auth(self, server_and_sock) -> None:
        srv, sock, _ = server_and_sock
        sock.reset()
        srv._dispatch(sock, {"cmd": "send", "to": "frontend", "msg": "hi", "from": "backend"})
        resp = sock.last_response()
        assert resp["ok"] is True
