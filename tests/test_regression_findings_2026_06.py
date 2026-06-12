"""Regression tests for 2026-06-12 reviewer + codex findings (rounds 1 & 2).

Covers:
  IPC auth    — forged `done`/`send` without pane token are rejected
  IPC auth    — pane token overrides caller-supplied from/from_project
  IPC auth    — identity args verified (role/project passed to orchestrator)
  TCP         — oversized frames, JSON non-dict, non-string cmd/from/auth fields
  TCP         — unterminated oversized frame (partial bytes, no newline)
  TCP         — post-first-frame idle socket still reaped + cap enforced
  Session gen — stale processExited from old session is silently dropped
  Codex exit  — _on_codex_exit stale-session guard via production code path
  Done-close  — delayed close is a no-op when session has changed
  `end-session` — missing/wrong token rejected even for `from: lead`
  Token lifecycle — token revoked on session exit (crash/close path)
  Token lifecycle — only matching token revoked; other roles unaffected
  Sanitizer   — ESC bracket-paste markers stripped in short and long payloads
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.cli_server import _IDLE_CONNECTION_TIMEOUT_S, _MAX_FRAME_BYTES, CliServer

# ─────────────────────────────────────────────────────────────────────────────
# Shared fixtures
# ─────────────────────────────────────────────────────────────────────────────

_LEAD_TOKEN = "regression-lead-token"
_PANE_TOKEN_BACKEND = "regression-pane-token-backend"
_PANE_TOKEN_FRONTEND = "regression-pane-token-frontend"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


class _FakeSock:
    """Write-only socket stub for _dispatch() tests."""

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


class _FakeReadSock(_FakeSock):
    """Socket stub that also supports canReadLine/readLine/bytesAvailable for _on_ready_read()."""

    def __init__(self, line: bytes, *, has_newline: bool = True) -> None:
        super().__init__()
        self._line = line
        self._has_newline = has_newline
        self._read = False
        self._disconnected = False

    def canReadLine(self) -> bool:
        return (not self._read) and self._has_newline

    def bytesAvailable(self) -> int:
        return 0 if self._read else len(self._line)

    def readLine(self, max_size: int = -1) -> bytes:
        self._read = True
        if max_size > 0:
            return self._line[:max_size]
        return self._line

    def disconnectFromHost(self) -> None:
        self._disconnected = True


@pytest.fixture
def srv(qapp: QCoreApplication) -> CliServer:
    mock_orch = MagicMock()
    mock_orch._lead_token = _LEAD_TOKEN
    mock_orch._pane_tokens = {
        _PANE_TOKEN_BACKEND: ("proj-a", "backend"),
        _PANE_TOKEN_FRONTEND: ("proj-a", "frontend"),
    }
    mock_orch.done.return_value = (True, "done")
    mock_orch.send.return_value = (True, "sent")
    mock_orch.end_session.return_value = (True, "ok")
    mock_orch.list_status.return_value = {}
    return CliServer(mock_orch)


# ─────────────────────────────────────────────────────────────────────────────
# IPC auth — forged done/send without pane token
# ─────────────────────────────────────────────────────────────────────────────


class TestForgedDoneRejected:
    def test_done_no_token_rejected(self, srv: CliServer) -> None:
        sock = _FakeSock()
        srv._dispatch(sock, {"cmd": "done", "from": "backend", "note": "x"})
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "unauthorized" in resp["msg"].lower()

    def test_done_wrong_token_rejected(self, srv: CliServer) -> None:
        sock = _FakeSock()
        srv._dispatch(sock, {"cmd": "done", "from": "backend", "note": "x", "auth": "bogus-tok"})
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "unauthorized" in resp["msg"].lower()

    def test_done_with_valid_pane_token_allowed(self, srv: CliServer) -> None:
        sock = _FakeSock()
        srv._dispatch(
            sock,
            {"cmd": "done", "from": "backend", "note": "x", "auth": _PANE_TOKEN_BACKEND},
        )
        resp = sock.last_response()
        assert resp["ok"] is True


class TestForgedSendRejected:
    def test_send_no_token_rejected(self, srv: CliServer) -> None:
        sock = _FakeSock()
        srv._dispatch(sock, {"cmd": "send", "from": "backend", "to": "qa", "msg": "hi"})
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "unauthorized" in resp["msg"].lower()

    def test_send_wrong_token_rejected(self, srv: CliServer) -> None:
        sock = _FakeSock()
        srv._dispatch(
            sock, {"cmd": "send", "from": "backend", "to": "qa", "msg": "hi", "auth": "fake"}
        )
        resp = sock.last_response()
        assert resp["ok"] is False

    def test_send_with_valid_pane_token_allowed(self, srv: CliServer) -> None:
        sock = _FakeSock()
        srv._dispatch(
            sock,
            {
                "cmd": "send",
                "from": "backend",
                "to": "qa",
                "msg": "hi",
                "auth": _PANE_TOKEN_BACKEND,
            },
        )
        resp = sock.last_response()
        assert resp["ok"] is True


# ─────────────────────────────────────────────────────────────────────────────
# IPC auth — pane token overrides caller-supplied identity (cannot spoof role)
# ─────────────────────────────────────────────────────────────────────────────


class TestPaneTokenOverridesCallerIdentity:
    """A pane cannot claim a different role/project by forging the `from` field."""

    def test_send_identity_derived_from_token_not_from_field(self, srv: CliServer) -> None:
        """Backend pane using its token but claiming `from: frontend` → identity = backend."""
        sock = _FakeSock()
        srv._dispatch(
            sock,
            {
                "cmd": "send",
                "from": "frontend",  # forged role
                "to": "qa",
                "msg": "hi",
                "auth": _PANE_TOKEN_BACKEND,
            },
        )
        resp = sock.last_response()
        assert resp["ok"] is True
        # Orchestrator.send() must have been called with from_role=backend (from token),
        # NOT "frontend" (caller-supplied).
        call_kwargs = srv._orch.send.call_args
        assert call_kwargs is not None
        from_role_passed = (
            call_kwargs[1].get("from_role") or call_kwargs[0][2]
            if call_kwargs[0] and len(call_kwargs[0]) > 2
            else call_kwargs[1].get("from_role")
        )
        # The server rewrites req["from"] to the token-derived role before dispatching.
        # Verify send was called with the token-derived role (backend), not the forged one.
        assert from_role_passed == "backend"

    def test_send_cross_project_claim_ignored(self, srv: CliServer) -> None:
        """Backend token (proj-a) but payload claims a different project → token wins."""
        sock = _FakeSock()
        srv._dispatch(
            sock,
            {
                "cmd": "send",
                "from": "backend",
                "from_project": "proj-b",  # forged project
                "to": "qa",
                "msg": "x",
                "auth": _PANE_TOKEN_BACKEND,
            },
        )
        resp = sock.last_response()
        assert resp["ok"] is True
        # project must be derived from the token (proj-a), not the forged claim (proj-b)
        call_kwargs = srv._orch.send.call_args
        project_passed = call_kwargs[1].get("project")
        assert project_passed == "proj-a", f"expected proj-a from token, got {project_passed!r}"

    def test_done_identity_derived_from_token(self, srv: CliServer) -> None:
        """Done with backend token claiming from=frontend → orchestrator.done called as backend."""
        sock = _FakeSock()
        srv._dispatch(
            sock,
            {
                "cmd": "done",
                "from": "frontend",  # forged
                "note": "finished",
                "auth": _PANE_TOKEN_BACKEND,
            },
        )
        resp = sock.last_response()
        assert resp["ok"] is True
        call_kwargs = srv._orch.done.call_args
        assert call_kwargs is not None
        # first positional arg to done() is from_role
        from_role_arg = call_kwargs[0][0] if call_kwargs[0] else call_kwargs[1].get("from_role")
        assert from_role_arg == "backend"


# ─────────────────────────────────────────────────────────────────────────────
# end-session — missing/wrong token rejected even for `from: lead`
# ─────────────────────────────────────────────────────────────────────────────


class TestEndSessionTokenGate:
    def test_end_session_no_token_rejected(self, srv: CliServer) -> None:
        sock = _FakeSock()
        srv._dispatch(sock, {"cmd": "end-session", "from": "lead", "note": "done"})
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "unauthorized" in resp["msg"].lower()

    def test_end_session_wrong_token_rejected(self, srv: CliServer) -> None:
        sock = _FakeSock()
        srv._dispatch(sock, {"cmd": "end-session", "from": "lead", "note": "done", "auth": "wrong"})
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "unauthorized" in resp["msg"].lower()

    def test_end_session_valid_lead_token_allowed(self, srv: CliServer) -> None:
        sock = _FakeSock()
        srv._dispatch(
            sock,
            {"cmd": "end-session", "from": "lead", "note": "done", "auth": _LEAD_TOKEN},
        )
        resp = sock.last_response()
        assert resp["ok"] is True


# ─────────────────────────────────────────────────────────────────────────────
# TCP hardening — frame validation via _on_ready_read
# ─────────────────────────────────────────────────────────────────────────────


class TestFrameValidation:
    def test_oversized_frame_rejected_and_disconnects(self, srv: CliServer) -> None:
        big_line = b"x" * (_MAX_FRAME_BYTES + 1) + b"\n"
        sock = _FakeReadSock(big_line)
        srv._on_ready_read(sock)
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "too large" in resp["msg"].lower()
        assert sock._disconnected, "oversized-frame connection must be closed"

    def test_json_array_rejected(self, srv: CliServer) -> None:
        line = b'["cmd", "list"]\n'
        sock = _FakeReadSock(line)
        srv._on_ready_read(sock)
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "json object" in resp["msg"].lower()

    def test_json_scalar_rejected(self, srv: CliServer) -> None:
        line = b'"just-a-string"\n'
        sock = _FakeReadSock(line)
        srv._on_ready_read(sock)
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "json object" in resp["msg"].lower()

    def test_non_string_cmd_rejected(self, srv: CliServer) -> None:
        line = json.dumps({"cmd": 42, "from": "lead"}).encode() + b"\n"
        sock = _FakeReadSock(line)
        srv._on_ready_read(sock)
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "'cmd'" in resp["msg"]

    def test_non_string_auth_rejected(self, srv: CliServer) -> None:
        line = json.dumps({"cmd": "list", "auth": {"evil": True}}).encode() + b"\n"
        sock = _FakeReadSock(line)
        srv._on_ready_read(sock)
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "'auth'" in resp["msg"]

    def test_invalid_json_rejected(self, srv: CliServer) -> None:
        line = b"{bad json\n"
        sock = _FakeReadSock(line)
        srv._on_ready_read(sock)
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "json" in resp["msg"].lower()


# ─────────────────────────────────────────────────────────────────────────────
# Session generation — stale processExited from old session is dropped
# ─────────────────────────────────────────────────────────────────────────────


class TestStaleSessionExitDropped:
    """Verify the `pp.session is s` guard in the orchestrator spawn lambdas.

    When session B replaces session A on a pane, a late exit signal from A
    must not call _on_session_exit (which would mark the pane as crashed or
    trigger auto-respawn logic for the live session B).
    """

    def _make_pane(self, session_a: object, session_b: object) -> MagicMock:
        pane = MagicMock()
        pane.session = session_a
        return pane

    def _make_orch_dispatch(self, project: str, role: str, session_a: object):
        """Build the lambda used in the orchestrator spawn code.

        lambda _code, r=role, c=cwd, p=project, s=session_a:
            _on_session_exit(r, c, p)
            if pp.session is s else None
        """
        panes: dict[str, object] = {}
        on_exit_calls: list[tuple] = []

        def _on_session_exit(r: str, c: str, p: str) -> None:
            on_exit_calls.append((r, c, p))

        pane_mock = MagicMock()
        pane_mock.session = session_a
        panes[role] = pane_mock

        cwd = "/proj"

        def _get_pane(p: str, r: str) -> object | None:
            return panes.get(r)

        def callback(_code, r=role, c=cwd, p=project, s=session_a) -> None:
            _pp = _get_pane(p, r)
            if _pp is not None and _pp.session is s:
                _on_session_exit(r, c, p)

        return callback, pane_mock, on_exit_calls

    def test_exit_from_current_session_fires(self) -> None:
        sess_a = object()
        callback, pane, calls = self._make_orch_dispatch("proj", "backend", sess_a)
        # pane.session is still sess_a → exit should fire
        pane.session = sess_a
        callback(0)
        assert len(calls) == 1

    def test_exit_from_old_session_dropped(self) -> None:
        sess_a = object()
        sess_b = object()
        callback, pane, calls = self._make_orch_dispatch("proj", "backend", sess_a)
        # Simulate: new session B has been attached before exit fires
        pane.session = sess_b
        callback(0)
        assert len(calls) == 0, "stale exit from old session must not trigger _on_session_exit"

    def test_exit_after_close_dropped(self) -> None:
        sess_a = object()
        callback, pane, calls = self._make_orch_dispatch("proj", "backend", sess_a)
        # After close(), pane.session is None
        pane.session = None
        callback(0)
        assert len(calls) == 0, "exit after close (session=None) must be dropped"


# ─────────────────────────────────────────────────────────────────────────────
# Done-close delayed guard — no-op when session has changed
# ─────────────────────────────────────────────────────────────────────────────


class TestDoneCloseSessionGuard:
    """Verify _close_if_same_session pattern from orchestrator.done().

    The closure captures `_done_sess = pane.session` at done-call time. When
    the QTimer fires 2.5 s later, it should call close() only if the pane
    still has the same session — not if it was respawned in the meantime.
    """

    def _make_close_guard(self, project: str, role: str, pane: MagicMock):
        close_calls: list[tuple] = []

        def _close(r: str, project: str) -> tuple[bool, str]:
            close_calls.append((r, project))
            return True, "closed"

        project_panes: dict[str, MagicMock] = {role: pane}
        done_sess = pane.session

        def _close_if_same_session() -> None:
            _pp = project_panes.get(role)
            if _pp is not None and _pp.session is done_sess:
                _close(role, project=project)

        return _close_if_same_session, close_calls

    def test_close_fires_when_session_unchanged(self) -> None:
        sess = object()
        pane = MagicMock()
        pane.session = sess
        guard, calls = self._make_close_guard("proj", "backend", pane)
        guard()
        assert len(calls) == 1

    def test_close_skipped_when_session_replaced(self) -> None:
        sess_a = object()
        sess_b = object()
        pane = MagicMock()
        pane.session = sess_a
        guard, calls = self._make_close_guard("proj", "backend", pane)
        # Simulate: pane was respawned with a new session before timer fired
        pane.session = sess_b
        guard()
        assert len(calls) == 0, "close must be skipped when session has been replaced"

    def test_close_skipped_when_pane_gone(self) -> None:
        sess = object()
        pane = MagicMock()
        pane.session = sess
        project_panes: dict[str, object] = {}  # pane was removed entirely
        close_calls: list[tuple] = []

        def _close_if_same_session() -> None:
            _pp = project_panes.get("backend")
            if _pp is not None and _pp.session is sess:
                close_calls.append(("backend", "proj"))

        _close_if_same_session()
        assert len(close_calls) == 0, "close must be skipped when pane has been removed"


# ─────────────────────────────────────────────────────────────────────────────
# Codex exit — stale-session guard via production _on_codex_exit code path
# ─────────────────────────────────────────────────────────────────────────────


class TestCodexExitStaleSessionGuard:
    """_on_codex_exit must apply the same current-session guard as the
    shell/gemini/claude processExited lambdas.

    Tests call the production Orchestrator._on_codex_exit unbound so they
    exercise the actual guard rather than a locally-reconstructed closure.
    """

    def _make_orch(self, pane_session):
        orch = MagicMock()
        pane = MagicMock()
        pane.session = pane_session
        orch._panes_by_project = {"proj": {"backend": pane}}
        orch._pane_state = {}
        return orch

    def _call(self, orch, session):
        from agent_takkub.orchestrator import Orchestrator

        Orchestrator._on_codex_exit(
            orch,
            exit_code=0,
            role_name="backend",
            cwd="/cwd",
            project="proj",
            session=session,
        )

    def test_current_session_fires_on_session_exit(self) -> None:
        sess = object()
        orch = self._make_orch(pane_session=sess)
        self._call(orch, sess)
        orch._on_session_exit.assert_called_once_with("backend", "/cwd", "proj")

    def test_stale_session_does_not_fire_on_session_exit(self) -> None:
        sess_a = object()
        sess_b = object()
        orch = self._make_orch(pane_session=sess_b)  # pane already on B
        self._call(orch, sess_a)  # exit from A
        orch._on_session_exit.assert_not_called()

    def test_exit_after_close_does_not_fire(self) -> None:
        sess_a = object()
        orch = self._make_orch(pane_session=None)  # closed
        self._call(orch, sess_a)
        orch._on_session_exit.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Token lifecycle — revocation on session exit and at respawn
# ─────────────────────────────────────────────────────────────────────────────


class TestTokenRevocationOnSessionExit:
    """Pane token must be revoked when the session dies (crash / normal exit)
    so a stale token cannot be replayed after the pane is gone."""

    def _make_orch(self, tokens: dict, project: str = "proj", role: str = "backend"):
        orch = MagicMock()
        orch._pane_tokens = dict(tokens)
        orch._recent_exits = {}
        pane = MagicMock()
        pane.state = "idle"  # not "exited" — exits the early return without respawn
        orch._panes_by_project = {project: {role: pane}}
        return orch

    def test_token_revoked_after_session_exit(self) -> None:
        from agent_takkub.orchestrator import Orchestrator

        orch = self._make_orch({"tok-backend": ("proj", "backend")})
        Orchestrator._on_session_exit(orch, "backend", "/cwd", "proj")
        assert "tok-backend" not in orch._pane_tokens

    def test_only_matching_token_revoked(self) -> None:
        from agent_takkub.orchestrator import Orchestrator

        orch = self._make_orch(
            {
                "tok-backend": ("proj", "backend"),
                "tok-frontend": ("proj", "frontend"),
            }
        )
        Orchestrator._on_session_exit(orch, "backend", "/cwd", "proj")
        assert "tok-backend" not in orch._pane_tokens
        assert "tok-frontend" in orch._pane_tokens, "frontend token must not be revoked"

    def test_revoked_token_rejected_by_server(self, srv: CliServer) -> None:
        """After crash simulation, the old token must be rejected for done/send."""
        from agent_takkub.orchestrator import Orchestrator

        # Simulate crash: _on_session_exit revokes the token in _orch._pane_tokens
        Orchestrator._on_session_exit(srv._orch, "backend", "/cwd", "proj-a")

        sock = _FakeSock()
        srv._dispatch(
            sock,
            {"cmd": "done", "from": "backend", "note": "x", "auth": _PANE_TOKEN_BACKEND},
        )
        resp = sock.last_response()
        assert resp["ok"] is False, "revoked token must be rejected after crash"
        assert "unauthorized" in resp["msg"].lower()

    def test_respawn_old_token_rejected_new_token_accepted(self, srv: CliServer) -> None:
        """Respawn registers a new token; the previous session's token must be rejected."""
        old_tok = _PANE_TOKEN_BACKEND
        new_tok = "new-tok-after-respawn"

        # Simulate respawn: revoke old + register new (mirrors spawn branch logic)
        orch = srv._orch
        _ptoks: dict = orch._pane_tokens
        _ptoks.pop(old_tok, None)
        _ptoks[new_tok] = ("proj-a", "backend")

        # Old token → rejected
        sock = _FakeSock()
        srv._dispatch(sock, {"cmd": "done", "from": "backend", "note": "", "auth": old_tok})
        assert sock.last_response()["ok"] is False

        # New token → accepted
        sock2 = _FakeSock()
        srv._dispatch(sock2, {"cmd": "done", "from": "backend", "note": "", "auth": new_tok})
        assert sock2.last_response()["ok"] is True


# ─────────────────────────────────────────────────────────────────────────────
# TCP hardening — unterminated frame + post-first-frame tracking + cap
# ─────────────────────────────────────────────────────────────────────────────


class TestTcpConnectionTracking:
    """TCP connection cap and reaper must remain effective after the first
    valid frame — a client cannot escape _MAX_CONNECTIONS by sending one
    frame and then holding the socket or streaming partial data."""

    def test_unterminated_oversized_frame_rejected_and_disconnected(
        self, srv: CliServer, qapp: QCoreApplication
    ) -> None:
        """Partial bytes exceeding _MAX_FRAME_BYTES without a newline → rejected."""
        partial = b"x" * (_MAX_FRAME_BYTES + 1)  # no newline
        sock = _FakeReadSock(partial, has_newline=False)
        # Register the socket so _on_ready_read can remove it on rejection.
        srv._open_connections[sock] = time.time()
        srv._on_ready_read(sock)
        assert sock._disconnected, "unterminated oversized frame must disconnect"
        assert sock._buf, "rejection response must have been written"
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "too large" in resp["msg"].lower()

    def test_socket_remains_tracked_after_valid_frame(
        self, srv: CliServer, qapp: QCoreApplication
    ) -> None:
        """After delivering a valid frame, the socket stays in _open_connections
        so (a) the cap is still enforced and (b) the reaper can evict idle sockets."""
        line = json.dumps({"cmd": "list"}).encode() + b"\n"
        sock = _FakeReadSock(line)
        srv._open_connections[sock] = time.time()
        srv._on_ready_read(sock)
        assert sock in srv._open_connections, (
            "socket must stay in _open_connections after first frame — "
            "otherwise the cap and reaper lose track of it"
        )

    def test_last_activity_updated_after_valid_frame(
        self, srv: CliServer, qapp: QCoreApplication
    ) -> None:
        """Activity timestamp is refreshed on frame delivery so the reaper
        gives the socket a fresh idle window rather than cutting it off."""
        line = json.dumps({"cmd": "list"}).encode() + b"\n"
        sock = _FakeReadSock(line)
        old_ts = time.time() - (_IDLE_CONNECTION_TIMEOUT_S + 1)  # would be reaped
        srv._open_connections[sock] = old_ts
        srv._on_ready_read(sock)
        new_ts = srv._open_connections.get(sock)
        assert new_ts is not None, "socket must remain tracked"
        assert new_ts > old_ts, "activity timestamp must have been refreshed after frame"

    def test_idle_socket_reaped_after_timeout(self, srv: CliServer, qapp: QCoreApplication) -> None:
        """Sockets that deliver no frame within _IDLE_CONNECTION_TIMEOUT_S are closed."""
        sock = _FakeReadSock(b"", has_newline=False)
        sock.disconnectFromHost = MagicMock()
        old_ts = time.time() - (_IDLE_CONNECTION_TIMEOUT_S + 1)
        srv._open_connections[sock] = old_ts
        srv._reap_idle_connections()
        assert sock not in srv._open_connections
        sock.disconnectFromHost.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# Sanitizer — ESC bracket-paste markers and CR in short/long payloads
# ─────────────────────────────────────────────────────────────────────────────


class TestSanitizePaneText:
    """_sanitize_pane_text must strip ESC-based bracket-paste sequences and CR
    regardless of payload length, and must preserve LF (intentional newlines)."""

    def _sanitize(self, text: str) -> str:
        from agent_takkub.orchestrator import _sanitize_pane_text

        return _sanitize_pane_text(text)

    def test_strip_paste_start_marker(self) -> None:
        assert "\x1b[200~" not in self._sanitize("before\x1b[200~after")

    def test_strip_paste_end_marker(self) -> None:
        assert "\x1b[201~" not in self._sanitize("before\x1b[201~after")

    def test_strip_bare_esc(self) -> None:
        assert "\x1b" not in self._sanitize("a\x1bb")

    def test_strip_cr(self) -> None:
        assert "\r" not in self._sanitize("line1\rline2")

    def test_preserve_lf(self) -> None:
        result = self._sanitize("line1\nline2")
        assert "\n" in result, "LF must be preserved (intentional in multi-line task bodies)"

    def test_short_payload_with_esc_markers(self) -> None:
        payload = "\x1b[200~evil\x1b[201~"
        result = self._sanitize(payload)
        assert "\x1b" not in result
        assert "evil" in result  # content preserved, only control sequences removed

    def test_long_payload_with_esc_markers(self) -> None:
        long_safe = "a" * 70_000
        payload = f"\x1b[200~{long_safe}\x1b[201~"
        result = self._sanitize(payload)
        assert "\x1b" not in result
        assert len(result) > 0

    def test_combined_cr_and_esc(self) -> None:
        payload = "\x1b[200~line\rinjected\x1b[201~"
        result = self._sanitize(payload)
        assert "\x1b" not in result
        assert "\r" not in result
