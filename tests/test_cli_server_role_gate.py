"""Server-side role gate using the stamped `from` field (Gap B hardening).

Tests call `CliServer._dispatch()` directly with a fake socket — no running
Qt event loop or real TCP connection needed.  The gate uses the `from` field
that cli.py stamps in every outbound payload; raw TCP clients that omit `from`
or stamp a non-lead role are rejected before the token check runs.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.cli_server import _LEAD_ONLY_CMDS, CliServer

# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────

_GATE_TEST_TOKEN = "role-gate-test-token-xyz"


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
def srv_sock(qapp: QCoreApplication):
    """Return (CliServer, FakeSock). Orchestrator has a known lead token."""
    mock_orch = MagicMock()
    mock_orch._lead_token = _GATE_TEST_TOKEN
    mock_orch.spawn.return_value = (True, "spawned")
    mock_orch.assign.return_value = (True, "assigned")
    mock_orch.close.return_value = (True, "closed")
    mock_orch.close_all_teammates.return_value = (True, "all closed")
    mock_orch.done.return_value = (True, "done")
    mock_orch.send.return_value = (True, "sent")
    mock_orch.list_status.return_value = {}

    srv = CliServer(mock_orch)
    sock = _FakeSock()
    return srv, sock


# ──────────────────────────────────────────────────────────────
# Non-lead roles → lifecycle commands rejected
# ──────────────────────────────────────────────────────────────


class TestNonLeadRoleRejected:
    def test_backend_cannot_assign(self, srv_sock) -> None:
        srv, sock = srv_sock
        sock.reset()
        srv._dispatch(sock, {"cmd": "assign", "from": "backend", "role": "qa", "task": "x"})
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "role gate" in resp["msg"].lower()
        assert "only lead" in resp["msg"].lower()
        assert "assign" in resp["msg"].lower()

    def test_frontend_cannot_spawn(self, srv_sock) -> None:
        srv, sock = srv_sock
        sock.reset()
        srv._dispatch(sock, {"cmd": "spawn", "from": "frontend", "role": "backend"})
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "role gate" in resp["msg"].lower()

    def test_qa_cannot_close(self, srv_sock) -> None:
        srv, sock = srv_sock
        sock.reset()
        srv._dispatch(sock, {"cmd": "close", "from": "qa", "role": "frontend"})
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "role gate" in resp["msg"].lower()

    def test_devops_cannot_close_all(self, srv_sock) -> None:
        srv, sock = srv_sock
        sock.reset()
        srv._dispatch(sock, {"cmd": "close-all", "from": "devops"})
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "role gate" in resp["msg"].lower()


# ──────────────────────────────────────────────────────────────
# Non-lead roles → non-lifecycle commands allowed
# ──────────────────────────────────────────────────────────────


class TestNonLeadRoleAllowedForOtherCmds:
    def test_backend_can_send(self, srv_sock) -> None:
        srv, sock = srv_sock
        sock.reset()
        srv._dispatch(sock, {"cmd": "send", "from": "backend", "to": "frontend", "msg": "hi"})
        resp = sock.last_response()
        assert resp["ok"] is True

    def test_backend_can_done(self, srv_sock) -> None:
        srv, sock = srv_sock
        sock.reset()
        srv._dispatch(sock, {"cmd": "done", "from": "backend", "note": "finished"})
        resp = sock.last_response()
        assert resp["ok"] is True


# ──────────────────────────────────────────────────────────────
# Lead role → all commands allowed (needs valid auth token too)
# ──────────────────────────────────────────────────────────────


class TestLeadRoleAllowed:
    @pytest.mark.parametrize(
        "payload",
        [
            {"cmd": "assign", "from": "lead", "role": "backend", "task": "work"},
            {"cmd": "spawn", "from": "lead", "role": "frontend"},
            {"cmd": "close", "from": "lead", "role": "qa"},
            {"cmd": "close-all", "from": "lead"},
            {"cmd": "send", "from": "lead", "to": "backend", "msg": "hi"},
            {"cmd": "list", "from": "lead"},
        ],
    )
    def test_lead_command_allowed(self, srv_sock, payload: dict) -> None:
        srv, sock = srv_sock
        sock.reset()
        # Include valid auth token so the token gate also passes for lifecycle cmds.
        full_payload = {**payload, "auth": _GATE_TEST_TOKEN}
        srv._dispatch(sock, full_payload)
        resp = sock.last_response()
        assert resp["ok"] is True, f"expected ok for {payload['cmd']} but got: {resp}"


# ──────────────────────────────────────────────────────────────
# No `from` field → lifecycle commands rejected
# ──────────────────────────────────────────────────────────────


class TestMissingFromFieldRejected:
    @pytest.mark.parametrize("cmd", sorted(_LEAD_ONLY_CMDS))
    def test_no_from_field_rejects_lifecycle(self, srv_sock, cmd: str) -> None:
        """Raw TCP client that omits 'from' cannot run lifecycle commands."""
        srv, sock = srv_sock
        sock.reset()
        srv._dispatch(sock, {"cmd": cmd, "role": "backend", "task": "x"})
        resp = sock.last_response()
        assert resp["ok"] is False
