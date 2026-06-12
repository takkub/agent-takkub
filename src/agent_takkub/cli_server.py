"""CLI server: listens on a local TCP port for JSON requests from the `takkub` CLI.

Protocol (newline-delimited JSON):

  request:  {"cmd": "send|assign|spawn|close|done|list", ...args}
  response: {"ok": bool, "msg": str, ...extras}

Runs on the Qt main thread via QTcpServer so all calls into Orchestrator are
serialised naturally.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket

from .config import write_port
from .orchestrator import Orchestrator

# Maximum allowed frame size (bytes). Frames larger than this are rejected so
# a malicious or buggy client cannot force the Qt main thread to parse/process
# an arbitrarily large JSON blob.
_MAX_FRAME_BYTES = 64 * 1024  # 64 KiB

# Maximum number of concurrent loopback connections. Keeps the connection table
# bounded; local-only threat model means no legitimate use case needs more.
_MAX_CONNECTIONS = 32

# Seconds an open connection may exist with no complete newline-terminated
# frame before it is closed. Prevents unbounded read-buffer accumulation when
# a client opens a socket but never writes a newline.
_IDLE_CONNECTION_TIMEOUT_S = 30.0

# Commands that mutate cockpit structure — only the Lead pane is allowed to
# run these. The gate is enforced server-side so raw TCP clients that bypass
# the cli.py role check (including confused teammate shells) are rejected.
_LEAD_ONLY_CMDS = frozenset(
    {
        "spawn",
        "assign",
        "close",
        "close-all",
        "harvest",
        "harvest-done",
        "pipeline-run",
        "goal",
        "end-session",  # Lead-only: only Lead summarises + closes the session
    }
)

# Commands that ANY pane may call, but where claiming `from: lead` in the
# payload would let a teammate (or any local process) forge a message that
# appears in another pane as if Lead authored it. Whenever the caller
# stamps `from: lead` on one of these, require the Lead token — same gate
# as _LEAD_ONLY_CMDS, just scoped to the spoofing surface. Listed here
# rather than added to _LEAD_ONLY_CMDS so legitimate peer-to-peer use
# (e.g. backend → qa) keeps working without the token.
_LEAD_SPOOF_GUARDED_CMDS = frozenset({"send"})


class CliServer(QObject):
    started = pyqtSignal(int)  # port

    def __init__(self, orchestrator: Orchestrator, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._orch = orchestrator
        self._server = QTcpServer(self)
        self._server.newConnection.connect(self._on_new_connection)
        # {socket: connect_time} — track open connections for the idle-timeout
        # reaper and the connection cap.
        self._open_connections: dict[object, float] = {}
        # Reap idle (no newline received) connections once per second.
        self._reaper = QTimer(self)
        self._reaper.setInterval(1_000)
        self._reaper.timeout.connect(self._reap_idle_connections)
        self._reaper.start()
        # Spawn staggering (#44/#38). Concurrent `takkub assign` (parallel
        # fan-out / shard fan-out) would otherwise schedule N QTimer.singleShot(0)
        # spawns that fire back-to-back on one tick; the 2nd+ ConPTY COM call
        # lands during the 1st spawn's input-synchronous WebEngine dispatch and
        # Windows rejects it (RPC_E_CANTCALLOUT) → spawn_failed_warned. We reserve
        # a time slot per spawn so the actual spawns are spaced apart (non-blocking
        # — QTimer, never a main-thread sleep, which would re-introduce the freeze).
        self._spawn_gap_ms = int(os.environ.get("TAKKUB_SPAWN_STAGGER_MS", "400"))
        # codex needs a bigger gap: each codex child runs `npm i -g @openai/codex`
        # on boot (codex v0.137 has no off-switch), and two overlapping global-npm
        # installs collide on EBUSY on Windows (#38). Space codex spawns further so
        # their update windows don't overlap.
        self._codex_gap_ms = int(os.environ.get("TAKKUB_CODEX_SPAWN_STAGGER_MS", "10000"))
        self._spawn_slot_until = 0.0  # monotonic ms; next non-codex spawn may start
        self._codex_slot_until = 0.0  # monotonic ms; next codex spawn may start

    def _is_codex_spawn(self, role: str | None, project: str | None) -> bool:
        """True iff this spawn will actually be backed by the codex CLI.

        Resolves the EFFECTIVE provider (per-project role→CLI mapping) rather than
        sniffing the role name, so it (a) catches a role REMAPPED to codex via
        role-providers.json (e.g. backend→codex) and (b) does NOT apply the codex
        gap to a `codex` role that has degraded to claude (codex toggled off / not
        installed) — that pane runs no npm self-update, so it needs no gap.
        Best-effort: falls back to the name heuristic if resolution fails."""
        base = (role or "").split("#", 1)[0].strip().lower()
        if not base:
            return False
        try:
            from .provider_config import CODEX, effective_provider_for

            return effective_provider_for(base, project) == CODEX
        except Exception:
            return base == "codex"

    def _next_spawn_delay_ms(self, role: str | None, project: str | None = None) -> int:
        """Reserve the next spawn time slot and return the delay (ms) until it.

        Two slots: a general one spaces ALL spawns ≥ _spawn_gap_ms apart (the
        ConPTY collision fix, #44); a codex one additionally spaces codex spawns
        ≥ _codex_gap_ms apart (the npm-EBUSY mitigation, #38). A non-codex spawn
        following a SINGLE codex spawn is not penalised by the codex gap (it uses
        the general slot); after multiple codex spawns the general slot is dragged
        forward by the in-flight codex window, which is benign (the system is
        mid-codex-install anyway). The first spawn in an idle period yields delay
        0, so a lone `takkub assign` is unchanged. Runs on the Qt main thread
        (QTcpServer), so no locking is needed. codex detection resolves the
        effective provider (see _is_codex_spawn) so remapped→codex roles are
        covered and a degraded-to-claude codex role is not over-staggered."""
        now = time.monotonic() * 1000.0
        is_codex = self._is_codex_spawn(role, project)
        start = max(now, self._spawn_slot_until)
        if is_codex:
            start = max(start, self._codex_slot_until)
        # General slot advances for every spawn; codex slot only for codex spawns.
        self._spawn_slot_until = start + self._spawn_gap_ms
        if is_codex:
            self._codex_slot_until = start + self._codex_gap_ms
        return max(0, int(start - now))

    def listen(self, port: int = 0) -> int:
        # bind to loopback only — other machines on the LAN must not reach us
        if not self._server.listen(QHostAddress.SpecialAddress.LocalHost, port):
            raise RuntimeError(f"failed to bind cli server: {self._server.errorString()}")
        actual = int(self._server.serverPort())
        write_port(actual)
        self.started.emit(actual)
        return actual

    def close(self) -> None:
        self._server.close()

    # ──────────────────────────────────────────────────────────────
    def _on_new_connection(self) -> None:
        while self._server.hasPendingConnections():
            sock: QTcpSocket = self._server.nextPendingConnection()
            if len(self._open_connections) >= _MAX_CONNECTIONS:
                sock.disconnectFromHost()
                sock.deleteLater()
                continue
            connect_ts = time.time()
            self._open_connections[sock] = connect_ts
            sock.readyRead.connect(lambda s=sock: self._on_ready_read(s))
            # Remove from tracking only when fully disconnected — keeps the
            # connection counted against _MAX_CONNECTIONS for its whole lifetime
            # and allows the reaper to evict it on inactivity.
            sock.disconnected.connect(lambda s=sock: self._open_connections.pop(s, None))
            sock.disconnected.connect(sock.deleteLater)

    def _reap_idle_connections(self) -> None:
        """Close connections idle longer than _IDLE_CONNECTION_TIMEOUT_S.

        Uses last-activity timestamp: updated to now() each time a valid frame
        arrives, so a client that sends one frame then holds the connection does
        NOT escape the reaper — it just gets a fresh 30-second window.  Prevents
        both unbounded read-buffer accumulation (no newline) and connection-cap
        bypass (valid frame then idle)."""
        cutoff = time.time() - _IDLE_CONNECTION_TIMEOUT_S
        stale = [s for s, ts in list(self._open_connections.items()) if ts < cutoff]
        for sock in stale:
            self._open_connections.pop(sock, None)
            try:
                sock.disconnectFromHost()
            except Exception:
                pass

    def _on_ready_read(self, sock: QTcpSocket) -> None:
        # Reject connections whose buffered data exceeds the frame cap without a
        # terminating newline — canReadLine() will be False while bytesAvailable()
        # grows, indicating a partial / unterminated oversized frame.
        available = sock.bytesAvailable() if hasattr(sock, "bytesAvailable") else 0
        if available > _MAX_FRAME_BYTES and not sock.canReadLine():
            self._reply(sock, ok=False, msg="frame too large (unterminated)")
            sock.disconnectFromHost()
            self._open_connections.pop(sock, None)
            return

        # read everything currently available, split on newline, dispatch each
        while sock.canReadLine():
            # Cap each frame to _MAX_FRAME_BYTES.  Pass maxSize to readLine() so
            # Qt truncates at the boundary rather than buffering a giant line.
            raw_bytes = bytes(sock.readLine(_MAX_FRAME_BYTES + 2))
            if len(raw_bytes) > _MAX_FRAME_BYTES:
                self._reply(sock, ok=False, msg="frame too large")
                sock.disconnectFromHost()
                self._open_connections.pop(sock, None)
                return
            line = raw_bytes.decode("utf-8", "replace").strip()
            if not line:
                continue
            # Update last-activity timestamp so the reaper gives this connection
            # another full idle window.  Keep it in _open_connections (don't pop)
            # so the connection still counts toward _MAX_CONNECTIONS.
            self._open_connections[sock] = time.time()
            try:
                req = json.loads(line)
            except json.JSONDecodeError as e:
                self._reply(sock, ok=False, msg=f"bad json: {e}")
                continue
            if not isinstance(req, dict):
                self._reply(sock, ok=False, msg="request must be a JSON object")
                continue
            # Validate required field types early — malformed values for cmd/from/auth
            # would otherwise raise AttributeError inside _dispatch.
            for _field in ("cmd", "from", "auth"):
                _val = req.get(_field)
                if _val is not None and not isinstance(_val, str):
                    self._reply(sock, ok=False, msg=f"field {_field!r} must be a string")
                    break
            else:
                self._dispatch(sock, req)

    def _dispatch(self, sock: QTcpSocket, req: dict) -> None:
        cmd = (req.get("cmd") or "").lower()
        # `from_project` is stamped by the cli when the calling pane was
        # spawned with TAKKUB_PROJECT set. Manual terminal invocations
        # don't carry it; the orchestrator falls back to the active
        # project in that case. Reserved for the multi-tab refactor —
        # currently informational and only used to scope `list`.
        from_project = req.get("from_project")

        # Layer 1 — role gate: check the stamped `from` field before the
        # token check.  cli.py stamps `from: _from_role()` on every request
        # so the server can see who is calling.  If the field is absent or is
        # not "lead", reject lifecycle commands immediately.  This blocks
        # confused teammate panes that open the TCP socket directly and try to
        # call assign/spawn/close without the lead token (Gap B hardening).
        if cmd in _LEAD_ONLY_CMDS:
            from_role = (req.get("from") or "").lower().strip()
            if from_role != "lead":
                self._reply(sock, ok=False, msg=f"role gate: only lead can {cmd}")
                return

        # Layer 2 — capability token: verify TAKKUB_LEAD_TOKEN so that even a
        # process that spoofs `from: "lead"` cannot proceed without the token
        # injected into the Lead pane's env by the orchestrator.
        # secrets.compare_digest prevents timing-side-channel attacks.
        if cmd in _LEAD_ONLY_CMDS:
            lead_token = getattr(self._orch, "_lead_token", None)
            caller_auth = req.get("auth") or ""
            if not lead_token or not secrets.compare_digest(
                caller_auth.encode(), lead_token.encode()
            ):
                self._reply(sock, ok=False, msg="unauthorized: lead-only command")
                return

        # Layer 3 — send-as-lead guard. `send` isn't lead-only (teammates
        # message each other peer-to-peer), but a payload claiming
        # `from: lead` would otherwise inject a `[lead → x]` message into
        # another pane that any local process can forge. Demand the Lead
        # token whenever the caller claims to *be* Lead, regardless of which
        # non-lifecycle command they ran. Skipped when `from` is empty
        # (manual terminal invocations) or any other role.
        from_role_norm = (req.get("from") or "").lower().strip()
        if cmd in _LEAD_SPOOF_GUARDED_CMDS and from_role_norm == "lead":
            lead_token = getattr(self._orch, "_lead_token", None)
            caller_auth = req.get("auth") or ""
            if not lead_token or not secrets.compare_digest(
                caller_auth.encode(), lead_token.encode()
            ):
                self._reply(sock, ok=False, msg=f"unauthorized: {cmd} as lead requires token")
                return

        # done: reject from_role == "lead" — Lead never closes itself via done.
        # This guard lives at the orchestrator level too; both layers protect
        # against the done→close chain accidentally targeting the Lead pane.
        if cmd == "done" and from_role_norm == "lead":
            self._reply(sock, ok=False, msg="lead cannot call done")
            return

        # Layer 4 — per-pane capability token for `done` and `send`.
        #
        # Each non-Lead pane receives TAKKUB_PANE_TOKEN in its env at spawn time.
        # The token is bound to (project, role) server-side. For `done` and `send`,
        # callers MUST present their token in the `auth` field. The server derives
        # caller identity (from_role, from_project) from the token instead of
        # trusting the caller-supplied `from`/`from_project` fields.
        #
        # Raw clients that haven't been spawned by the orchestrator have no token
        # and are rejected for these two commands.
        if cmd in ("done", "send"):
            caller_auth = req.get("auth") or ""
            pane_tokens: dict[str, tuple[str, str]] = getattr(self._orch, "_pane_tokens", {})
            # Lead token is valid for `send` (Lead sends task specs to teammates)
            # but not for `done` (Lead cannot call done on itself).
            lead_token = getattr(self._orch, "_lead_token", None)
            if (
                lead_token
                and caller_auth
                and secrets.compare_digest(caller_auth.encode(), lead_token.encode())
            ):
                # Lead is sending — identity already verified by the lead-spoof
                # guard above; allow through with the caller-supplied from/project.
                pass
            elif caller_auth in pane_tokens:
                # Valid pane token — derive identity from the server's registry,
                # overriding whatever the caller put in `from`/`from_project`.
                _tok_project, _tok_role = pane_tokens[caller_auth]
                req = {**req, "from": _tok_role, "from_project": _tok_project}
                from_project = _tok_project
                from_role_norm = _tok_role
            else:
                self._reply(
                    sock,
                    ok=False,
                    msg=f"unauthorized: {cmd} requires a valid pane token (TAKKUB_PANE_TOKEN)",
                )
                return

        # list/status: intentionally open — trust-local model; any local process
        # may query pane state without a token.  If the threat model is tightened
        # to require tokens for every command, add them to _LEAD_ONLY_CMDS or the
        # pane-token gate above.

        try:
            if cmd in ("spawn", "assign"):
                # Spawning a pane is heavy (QWebEngine init) and runs on THIS
                # thread — the same one serving IPC + UI. Doing it inline blocked
                # the reply until the pane was up, routinely blowing the client's
                # 15 s timeout and making `takkub` look hung. Ack immediately and
                # run the spawn on the next event-loop tick (the reply is already
                # flushed to the socket by then). The real outcome shows up via
                # `takkub list` / done events; failures are logged in spawn().
                role = req.get("role")
                if not role:
                    self._reply(sock, ok=False, msg="missing arg: 'role'")
                    return
                delay = self._next_spawn_delay_ms(role, from_project)
                if cmd == "spawn":
                    QTimer.singleShot(
                        delay,
                        lambda: self._orch.spawn(role, cwd=req.get("cwd"), project=from_project),
                    )
                    self._reply(sock, ok=True, msg=f"spawning {role} (async, +{delay}ms)")
                else:
                    QTimer.singleShot(
                        delay,
                        lambda: self._orch.assign(
                            role,
                            cwd=req.get("cwd"),
                            task=req.get("task", ""),
                            requires_commit=bool(req.get("requires_commit", False)),
                            auto_chain=bool(req.get("auto_chain", False)),
                            shard_total=int(req.get("shard_total", 0)),
                            project=from_project,
                        ),
                    )
                    self._reply(
                        sock, ok=True, msg=f"task queued for {role} (spawning async, +{delay}ms)"
                    )
                return
            elif cmd == "send":
                ok, msg = self._orch.send(
                    req["to"],
                    msg=req.get("msg", ""),
                    from_role=req.get("from"),
                    project=from_project,
                )
            elif cmd == "close":
                ok, msg = self._orch.close(req["role"], project=from_project)
            elif cmd == "close-all":
                ok, msg = self._orch.close_all_teammates(project=from_project)
            elif cmd == "done":
                ok, msg = self._orch.done(
                    req.get("from") or "", note=req.get("note", ""), project=from_project
                )
            elif cmd == "end-session":
                ok, msg = self._orch.end_session(project=from_project, note=req.get("note", ""))
            elif cmd == "goal":
                # #50: set / clear / show the session objective. Lead-only
                # (gated above). `clear` wins over `text`; absent both = show.
                if req.get("clear"):
                    ok, msg = self._orch.clear_session_goal(project=from_project)
                elif (req.get("text") or "").strip():
                    ok, msg = self._orch.set_session_goal(req["text"], project=from_project)
                else:
                    current = self._orch.get_session_goal(project=from_project)
                    ok, msg = True, (f"current goal: {current}" if current else "no goal set")
            elif cmd == "list":
                detailed = self._orch.list_status_detailed(project=from_project)
                status: dict[str, str] = {}
                for role, info in detailed.items():
                    state = info["state"]
                    stall_min = info.get("stall_minutes")
                    if stall_min is not None:
                        state = f"{state} (stalled {stall_min}m)"
                    status[role] = state
                self._reply(sock, ok=True, msg="status", status=status)
                return
            elif cmd == "status":
                since_ts: float | None = None
                since_hhmm = req.get("since")
                if since_hhmm:
                    try:
                        h, m = str(since_hhmm).split(":")
                        now_dt = datetime.now()
                        since_dt = now_dt.replace(
                            hour=int(h), minute=int(m), second=0, microsecond=0
                        )
                        if since_dt > now_dt:
                            from datetime import timedelta

                            since_dt -= timedelta(days=1)
                        since_ts = since_dt.timestamp()
                    except (ValueError, AttributeError):
                        self._reply(
                            sock,
                            ok=False,
                            msg=f"bad --since format: {since_hhmm!r} (use HH:MM)",
                        )
                        return
                report = self._orch.pane_status_report(project=from_project, since_ts=since_ts)
                self._reply(sock, ok=True, msg="status report", report=report)
                return
            elif cmd == "harvest":
                harvest_since_ts: float | None = None
                harvest_since_hhmm = req.get("since")
                if harvest_since_hhmm:
                    try:
                        h, m = str(harvest_since_hhmm).split(":")
                        now_dt = datetime.now()
                        since_dt = now_dt.replace(
                            hour=int(h), minute=int(m), second=0, microsecond=0
                        )
                        if since_dt > now_dt:
                            from datetime import timedelta

                            since_dt -= timedelta(days=1)
                        harvest_since_ts = since_dt.timestamp()
                    except (ValueError, AttributeError):
                        self._reply(
                            sock,
                            ok=False,
                            msg=f"bad --since format: {harvest_since_hhmm!r} (use HH:MM)",
                        )
                        return
                harvest_limit = int(req.get("limit", 100))
                harvest_role = req.get("role", "")
                ok_h, msg_h, payload_h = self._orch.harvest_info(
                    harvest_role,
                    project=from_project,
                    since_ts=harvest_since_ts,
                    limit=harvest_limit,
                )
                if ok_h:
                    self._reply(sock, ok=True, msg=msg_h, **payload_h)
                else:
                    self._reply(sock, ok=False, msg=msg_h)
                return
            elif cmd == "harvest-done":
                harvest_role = req.get("role", "")
                harvest_note = req.get("note", "harvested by lead")
                ok, msg = self._orch.done(harvest_role, note=harvest_note, project=from_project)
            elif cmd == "pipeline-run":
                template_id = (req.get("template_id") or "").strip()
                if not template_id:
                    self._reply(sock, ok=False, msg="missing arg: 'template_id'")
                    return
                pl_delay = self._next_spawn_delay_ms(None, from_project)
                QTimer.singleShot(
                    pl_delay,
                    lambda tid=template_id: self._orch.run_pipeline(
                        template_id=tid,
                        project=from_project,
                    ),
                )
                self._reply(
                    sock, ok=True, msg=f"pipeline {template_id!r} starting (async, +{pl_delay}ms)"
                )
                return
            else:
                ok, msg = False, f"unknown cmd: {cmd}"
        except KeyError as e:
            ok, msg = False, f"missing arg: {e}"
        except Exception as e:  # pragma: no cover - defensive
            ok, msg = False, f"error: {e}"

        self._reply(sock, ok=ok, msg=msg)

    def _reply(self, sock: QTcpSocket, *, ok: bool, msg: str, **extra) -> None:
        payload = {"ok": ok, "msg": msg, **extra}
        sock.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        sock.flush()
