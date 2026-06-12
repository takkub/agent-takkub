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

from agent_takkub.cli_server import _LEAD_ONLY_CMDS, _LEAD_SPOOF_GUARDED_CMDS, CliServer

# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────

_GATE_TEST_TOKEN = "role-gate-test-token-xyz"
_PANE_TOKEN_BACKEND = "pane-tok-backend-role-gate"


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
    """Return (CliServer, FakeSock). Orchestrator has a known lead token and a backend pane token."""
    mock_orch = MagicMock()
    mock_orch._lead_token = _GATE_TEST_TOKEN
    # Pre-register a backend pane token so tests for send/done can present credentials.
    mock_orch._pane_tokens = {_PANE_TOKEN_BACKEND: ("test-project", "backend")}
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
        # `send` requires a valid pane token; identity is derived from the token.
        srv, sock = srv_sock
        sock.reset()
        srv._dispatch(
            sock,
            {
                "cmd": "send",
                "from": "backend",
                "to": "frontend",
                "msg": "hi",
                "auth": _PANE_TOKEN_BACKEND,
            },
        )
        resp = sock.last_response()
        assert resp["ok"] is True

    def test_backend_can_done(self, srv_sock) -> None:
        # `done` requires a valid pane token; identity is derived from the token.
        srv, sock = srv_sock
        sock.reset()
        srv._dispatch(
            sock,
            {"cmd": "done", "from": "backend", "note": "finished", "auth": _PANE_TOKEN_BACKEND},
        )
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


# ──────────────────────────────────────────────────────────────
# Send-as-lead spoofing guard — non-lifecycle command still needs
# the Lead token whenever the caller claims `from: lead`.
# ──────────────────────────────────────────────────────────────


class TestSendAsLeadSpoofGuard:
    """`send` is the only non-lifecycle command in _LEAD_SPOOF_GUARDED_CMDS.

    Without this guard, any local process (or a confused teammate pane)
    could connect to the cli server and send {"cmd":"send","from":"lead",
    "to":"frontend","msg":"<malicious>"} — the receiving pane would see
    `[lead → frontend] <msg>` and follow it as if Lead authored the
    instruction. The token gate matches the one used for _LEAD_ONLY_CMDS
    so the cli pulls TAKKUB_LEAD_TOKEN automatically when sending from
    the Lead pane.
    """

    def test_send_as_lead_without_token_rejected(self, srv_sock) -> None:
        srv, sock = srv_sock
        sock.reset()
        # No auth field — simulates a raw TCP client or a teammate trying
        # to forge a Lead-authored message.
        srv._dispatch(sock, {"cmd": "send", "from": "lead", "to": "backend", "msg": "x"})
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "unauthorized" in resp["msg"].lower()
        assert "send" in resp["msg"].lower()

    def test_send_as_lead_with_wrong_token_rejected(self, srv_sock) -> None:
        srv, sock = srv_sock
        sock.reset()
        srv._dispatch(
            sock,
            {
                "cmd": "send",
                "from": "lead",
                "to": "backend",
                "msg": "x",
                "auth": "nope-wrong-token",
            },
        )
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "unauthorized" in resp["msg"].lower()

    def test_send_as_lead_with_valid_token_allowed(self, srv_sock) -> None:
        srv, sock = srv_sock
        sock.reset()
        srv._dispatch(
            sock,
            {
                "cmd": "send",
                "from": "lead",
                "to": "backend",
                "msg": "x",
                "auth": _GATE_TEST_TOKEN,
            },
        )
        resp = sock.last_response()
        assert resp["ok"] is True

    def test_send_from_teammate_no_token_rejected(self, srv_sock) -> None:
        # Layer 4: `send` requires a pane token (or lead token) for ALL callers,
        # not just ones claiming `from: lead`. Raw clients without a token are
        # rejected to prevent unregistered processes from forging peer messages.
        srv, sock = srv_sock
        sock.reset()
        srv._dispatch(sock, {"cmd": "send", "from": "backend", "to": "qa", "msg": "x"})
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "unauthorized" in resp["msg"].lower()

    def test_send_from_empty_no_token_rejected(self, srv_sock) -> None:
        # Same as above: omitting `from` does not bypass the pane-token gate.
        srv, sock = srv_sock
        sock.reset()
        srv._dispatch(sock, {"cmd": "send", "to": "backend", "msg": "x"})
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "unauthorized" in resp["msg"].lower()

    def test_only_send_is_currently_guarded(self) -> None:
        # Pin the membership so a future contributor doesn't quietly add
        # a new spoof-guarded command without updating this test bank.
        # If you're adding a new command here, write its three positive/
        # negative tests above and then update this assertion.
        assert _LEAD_SPOOF_GUARDED_CMDS == frozenset({"send"})
