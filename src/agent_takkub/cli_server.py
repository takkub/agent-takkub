"""CLI server: listens on a local TCP port for JSON requests from the `takkub` CLI.

Protocol (newline-delimited JSON):

  request:  {"cmd": "send|assign|spawn|close|done|subagent-done|list|hook|session-report", ...args}
  response: {"ok": bool, "msg": str, ...extras}

Runs on the Qt main thread via QTcpServer so all calls into Orchestrator are
serialised naturally.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from datetime import datetime

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket

from . import browser_chrome
from .config import write_port
from .orchestrator import Orchestrator, _human_duration
from .spawn_queue_health import SpawnQueueHealthMonitor

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

# #233: window in which an identical (project, role, task) `assign` is treated
# as a retry of a request the client already sent, not a fresh dispatch. The
# CLI's own response wait has a hard deadline (`cli._RESPONSE_TIMEOUT_S`), so
# an operator who sees "no response" and reruns the exact same `takkub assign`
# must not risk a duplicate spawn/task-paste (worst case: a side-effecting
# task like a migration or docker compose up running twice concurrently) if
# the first request actually did land, just slower than the client waited.
_ASSIGN_DEDUP_WINDOW_S = 8.0

# Commands that mutate cockpit structure — only the Lead pane is allowed to
# run these. The gate is enforced server-side so raw TCP clients that bypass
# the cli.py role check (including confused teammate shells) are rejected.
_LEAD_ONLY_CMDS = frozenset(
    {
        "spawn",
        "assign",
        "subagent-done",
        "close",
        "close-all",
        "harvest",
        "harvest-done",
        "pipeline-run",
        "goal",
        "end-session",  # Lead-only: only Lead summarises + closes the session
        "restart",  # Lead-only: kills every pane and relaunches the app
        # inbox is read-only, not a mutation, but reuses this same gate
        # (#231): its payload is other panes' report bodies — the same
        # M3#16 sensitivity as status's transcript_tail, not something a
        # confused teammate shell should be able to read cold.
        "inbox",
        # wait-* (#242): blocks on other panes' delivery pipeline, same
        # read-sensitivity rationale as inbox — a teammate pane has no
        # business polling another role's completion state either.
        "wait-begin",
        "wait-poll",
        "wait-end",
        "wait-cancel",
        # messages (#277): the send-audit log carries other roles' full
        # instruction text — same read-sensitivity call as `inbox` above.
        "messages",
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
        # #233: {(project, role, task_hash): last_seen_ts} — recent `assign`
        # fingerprints so a client retry of the identical request within
        # _ASSIGN_DEDUP_WINDOW_S is acked without a second dispatch. Pruned
        # alongside the idle-connection reaper (same 1s tick).
        self._recent_assign_fingerprints: dict[tuple[str, str, str, str], float] = {}
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
        # #177 mitigation (not a proven fix — see docs/audit/2026-08-04-issue-146-
        # playwright-shards.md's H1/H2): a browser-role shard (qa#N/critic#N/
        # designer#N) spawns TWO npx MCP server processes (playwright +
        # chrome-devtools) on top of its own claude.exe, and claude code's MCP
        # connect/startup window has no configurable timeout. N shards' MCP-init
        # firing near-simultaneously is the leading (unproven, no live repro yet)
        # hypothesis for "shard doesn't connect, single pane does" reports. Widen
        # the gap for these spawns specifically, same mechanism as the codex gap
        # above, so MCP-init windows overlap less without slowing down non-browser
        # fan-out (backend/frontend shards keep the tight 400ms gap).
        self._browser_shard_gap_ms = int(
            os.environ.get("TAKKUB_BROWSER_SHARD_SPAWN_STAGGER_MS", "3000")
        )
        self._spawn_slot_until = 0.0  # monotonic ms; next non-codex spawn may start
        self._codex_slot_until = 0.0  # monotonic ms; next codex spawn may start
        self._browser_shard_slot_until = 0.0  # monotonic ms; next browser-shard spawn may start
        # #141: read-only spawn-arbiter wedge diagnostics for `takkub doctor --live`.
        self._spawn_health = SpawnQueueHealthMonitor(orchestrator, parent=self)

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

    @staticmethod
    def _is_browser_shard_spawn(role: str | None) -> bool:
        """True iff *role* is a fan-out shard (`#N` suffix) of a browser role
        (qa/critic/designer) — see `_browser_shard_gap_ms`'s comment for why
        these specifically get a wider spawn gap (#177 mitigation)."""
        raw = (role or "").strip().lower()
        if "#" not in raw:
            return False
        base = raw.split("#", 1)[0]
        return base in browser_chrome.BROWSER_ROLES

    def _next_spawn_delay_ms(self, role: str | None, project: str | None = None) -> int:
        """Reserve the next spawn time slot and return the delay (ms) until it.

        Three slots, each additive on top of the general one: a general slot
        spaces ALL spawns ≥ _spawn_gap_ms apart (the ConPTY collision fix,
        #44); a codex slot additionally spaces codex spawns ≥ _codex_gap_ms
        apart (the npm-EBUSY mitigation, #38); a browser-shard slot
        additionally spaces qa#N/critic#N/designer#N spawns ≥
        _browser_shard_gap_ms apart (the MCP cold-start mitigation, #177). A
        spawn outside a given slot's category is not penalised by that slot
        (it only waits on the general one); after several spawns IN that
        category the general slot itself gets dragged forward by the
        in-flight window, which is benign (the system is mid-install/
        mid-MCP-init anyway). The first spawn in an idle period yields delay
        0, so a lone `takkub assign` is unchanged. Runs on the Qt main thread
        (QTcpServer), so no locking is needed. codex detection resolves the
        effective provider (see _is_codex_spawn) so remapped→codex roles are
        covered and a degraded-to-claude codex role is not over-staggered."""
        now = time.monotonic() * 1000.0
        is_codex = self._is_codex_spawn(role, project)
        is_browser_shard = self._is_browser_shard_spawn(role)
        start = max(now, self._spawn_slot_until)
        if is_codex:
            start = max(start, self._codex_slot_until)
        if is_browser_shard:
            start = max(start, self._browser_shard_slot_until)
        # General slot advances for every spawn; the other two only for
        # spawns in their own category.
        self._spawn_slot_until = start + self._spawn_gap_ms
        if is_codex:
            self._codex_slot_until = start + self._codex_gap_ms
        if is_browser_shard:
            self._browser_shard_slot_until = start + self._browser_shard_gap_ms
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
        fp_cutoff = time.time() - _ASSIGN_DEDUP_WINDOW_S
        stale_fps = [k for k, ts in self._recent_assign_fingerprints.items() if ts < fp_cutoff]
        for k in stale_fps:
            self._recent_assign_fingerprints.pop(k, None)

    @staticmethod
    def _assign_fingerprint(
        project_ns: str, role: str, task: str, mode: str = "pane"
    ) -> tuple[str, str, str, str]:
        task_hash = hashlib.blake2b(
            (task or "").encode("utf-8", "replace"), digest_size=8
        ).hexdigest()
        return (project_ns or "default", role, task_hash, mode)

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

    def _caller_is_lead(self, req: dict) -> bool:
        """True if the request carries the Lead capability token. Used to gate
        sensitive read-only payloads (M3#16) without timing side-channels."""
        lead_token = getattr(self._orch, "_lead_token", None)
        caller_auth = req.get("auth") or ""
        return bool(
            lead_token
            and caller_auth
            and secrets.compare_digest(caller_auth.encode(), lead_token.encode())
        )

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

        # done/progress: reject from_role == "lead" — Lead never closes (or
        # progress-reports on) itself. This guard lives at the orchestrator
        # level too; both layers protect against the done→close chain
        # accidentally targeting the Lead pane.
        if cmd in ("done", "progress") and from_role_norm == "lead":
            self._reply(sock, ok=False, msg=f"lead cannot call {cmd}")
            return

        # Layer 4 — per-pane capability token for `done`, `progress`, `send`,
        # and `answer-picker`.
        #
        # Each non-Lead pane receives TAKKUB_PANE_TOKEN in its env at spawn time.
        # The token is bound to (project, role) server-side. For these commands,
        # callers MUST present their token in the `auth` field. The server derives
        # caller identity (from_role, from_project) from the token instead of
        # trusting the caller-supplied `from`/`from_project` fields.
        #
        # Raw clients that haven't been spawned by the orchestrator have no token
        # and are rejected for these commands.
        if cmd in ("done", "progress", "send", "answer-picker", "hook", "session-report"):
            caller_auth = req.get("auth") or ""
            pane_tokens: dict[str, tuple[str, str]] = getattr(self._orch, "_pane_tokens", {})
            # Lead token is valid for `send` (Lead sends task specs to teammates),
            # `hook` and `session-report` (Lead's own claude session also fires
            # Stop/Notification/SessionStart hooks — the done-gate itself is a
            # no-op for Lead) but not `done`/`progress` (Lead cannot report on
            # itself). `answer-picker` is remote-mobile-only (remote AskUserQuestion fix: Remote's
            # `api.lead_say` never sends `from: lead`, always `from: remote`,
            # same as `send` already does for chat messages) — it reuses this
            # same lead-token branch rather than `_LEAD_ONLY_CMDS` because it
            # isn't Lead calling itself, it's the mobile client relaying key
            # presses into the Lead pane's own PTY.
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
                # #143: cwd escaping the project's configured paths used to be
                # caught only inside spawn() — which runs AFTER the "task
                # queued" ack below (async, next event-loop tick). The Lead
                # saw a false "ok" and only found out it failed once the
                # deferred [spawn-failed] notice arrived. Validate here,
                # synchronously, before any ack goes out.
                cwd_req = req.get("cwd")
                if cwd_req:
                    _resolve_project = getattr(self._orch, "_resolve_project", None)
                    project_ns = (
                        _resolve_project(from_project)
                        if _resolve_project is not None
                        else (from_project or "default")
                    )
                    if project_ns != "default":
                        from .orchestrator_text import cwd_validation_error

                        cwd_err = cwd_validation_error(str(cwd_req), project_ns, role)
                        if cwd_err:
                            self._reply(sock, ok=False, msg=cwd_err)
                            return
                if cmd == "assign":
                    mode = str(req.get("mode", "pane") or "pane")
                    if mode not in {"pane", "subagent"}:
                        self._reply(sock, ok=False, msg="--mode must be pane or subagent")
                        return
                    from .provider_config import (
                        assign_model_override_error,
                        assign_provider_override_error,
                    )

                    provider_req = str(req.get("provider", "") or "").strip().lower() or None
                    if provider_req:
                        provider_error = assign_provider_override_error(provider_req)
                        if provider_error:
                            self._reply(sock, ok=False, msg=provider_error)
                            return
                    model_error = assign_model_override_error(
                        role,
                        req.get("model"),
                        from_project,
                        provider_override=provider_req,
                    )
                    if model_error:
                        self._reply(sock, ok=False, msg=model_error)
                        return
                    # #162: assign() itself already rejects this, but it runs
                    # deferred (QTimer below) and its return value is never
                    # relayed back to the socket — the ack a few lines down
                    # is unconditionally ok=True regardless of what the
                    # deferred call returns. Without this synchronous
                    # pre-check a rejected worktree collision would look like
                    # a success to the caller. Mirrors the cwd check above.
                    isolation_req = str(req.get("isolation", "shared") or "shared")
                    if isolation_req == "worktree":
                        _collision_check = getattr(
                            self._orch, "_worktree_bare_role_collision", None
                        )
                        if _collision_check is not None:
                            collision_err = _collision_check(role, from_project)
                            if collision_err:
                                self._reply(sock, ok=False, msg=collision_err)
                                return
                    # #233: dedup an identical (project, role, task) assign seen
                    # again within _ASSIGN_DEDUP_WINDOW_S — makes a client retry
                    # after a client-side timeout safe (never a double-dispatch)
                    # without needing the caller to pass an explicit request id.
                    _resolve_project_fp = getattr(self._orch, "_resolve_project", None)
                    project_ns_fp = (
                        _resolve_project_fp(from_project)
                        if _resolve_project_fp is not None
                        else (from_project or "default")
                    )
                    fp = self._assign_fingerprint(project_ns_fp, role, req.get("task", ""), mode)
                    now_fp = time.time()
                    last_seen = self._recent_assign_fingerprints.get(fp)
                    self._recent_assign_fingerprints[fp] = now_fp
                    if last_seen is not None and (now_fp - last_seen) < _ASSIGN_DEDUP_WINDOW_S:
                        self._reply(
                            sock,
                            ok=True,
                            msg=(
                                f"task already queued for {role} moments ago "
                                "(deduped — safe retry, not re-dispatched)"
                            ),
                        )
                        return
                if cmd == "assign" and mode == "subagent":
                    ok, msg = self._orch.assign(
                        role,
                        cwd=req.get("cwd"),
                        task=req.get("task", ""),
                        requires_commit=bool(req.get("requires_commit", False)),
                        auto_chain=bool(req.get("auto_chain", False)),
                        shard_total=int(req.get("shard_total", 0)),
                        isolation=str(req.get("isolation", "shared") or "shared"),
                        project=from_project,
                        feature=str(req.get("feature", "") or ""),
                        mode=mode,
                    )
                    self._reply(sock, ok=ok, msg=msg)
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
                            plan=bool(req.get("plan", False)),
                            isolation=str(req.get("isolation", "shared") or "shared"),
                            project=from_project,
                            feature=str(req.get("feature", "") or ""),
                            model=(str(req.get("model", "") or "").strip() or None),
                            provider=(str(req.get("provider", "") or "").strip().lower() or None),
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
            elif cmd == "answer-picker":
                ok, msg = self._orch.answer_picker(
                    req.get("key_sequence", ""), project=from_project
                )
            elif cmd == "close":
                ok, msg = self._orch.close(req["role"], project=from_project)
            elif cmd == "close-all":
                ok, msg = self._orch.close_all_teammates(project=from_project)
            elif cmd == "restart":
                # Full cockpit restart (persist state → relaunch). The
                # orchestrator emits deferred so this reply flushes first.
                ok, msg = self._orch.request_restart()
            elif cmd == "done":
                ok, msg = self._orch.done(
                    req.get("from") or "",
                    note=req.get("note", ""),
                    project=from_project,
                    failed=bool(req.get("failed", False)),
                    blocked=bool(req.get("blocked", False)),
                    force=bool(req.get("force", False)),
                )
            elif cmd == "subagent-done":
                ok, msg = self._orch.subagent_done(
                    req.get("role") or "",
                    note=req.get("note", ""),
                    project=from_project,
                    failed=bool(req.get("failed", False)),
                )
            elif cmd == "progress":
                # #234: status update that does NOT schedule the pane's
                # teardown — see Orchestrator.progress() docstring.
                ok, msg = self._orch.progress(
                    req.get("from") or "",
                    note=req.get("note", ""),
                    project=from_project,
                )
            elif cmd == "hook":
                ok, blocked, msg = self._orch.consume_pane_hook(
                    req.get("from") or "",
                    project=from_project,
                    event=req.get("event", ""),
                    notification_type=req.get("notification_type", ""),
                )
                self._reply(sock, ok=ok, msg=msg, block=blocked)
                return
            elif cmd == "session-report":
                ok, msg = self._orch.consume_session_report(
                    req.get("from") or "",
                    project=from_project,
                    session_id=req.get("session_id", ""),
                    source=req.get("source", ""),
                    cwd=req.get("cwd", ""),
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
                    # #263: show the unified display_state (login-required /
                    # booting / waiting-delivery / busy / unknown) so `takkub
                    # list` can't silently disagree with what the screen
                    # actually shows; falls back to the raw state if a caller
                    # somehow gets a dict predating this key.
                    state = info.get("display_state", info["state"])
                    stall_min = info.get("stall_minutes")
                    if stall_min is not None:
                        state = f"{state} (stalled {stall_min}m)"
                    # #301: quote the quota reset window right on the compact
                    # `takkub list` line too, not just the detailed `status`
                    # report — this is the line a Lead skims most often.
                    quota_resets_at = info.get("quota_resets_at") or 0.0
                    if quota_resets_at:
                        state = f"{state} (resets {_human_duration(quota_resets_at - time.time())})"
                    model = info.get("model")
                    if model:
                        state = f"{state} [{model}]"
                    status[role] = state
                self._reply(sock, ok=True, msg="status", status=status)
                return
            elif cmd == "worktree-live-paths":
                # #187: read-only, same trust level as `list` — lets `takkub
                # worktree clean` (which is otherwise pure-local/no-socket by
                # design, see cli.py cmd_worktree) refuse to touch a
                # checkout a currently-alive pane still holds.
                paths = sorted(self._orch.live_worktree_paths(project=from_project))
                self._reply(sock, ok=True, msg=f"{len(paths)} live worktree(s)", paths=paths)
                return
            elif cmd == "spawn-queue-status":
                # #141: read-only spawn-arbiter wedge diagnostics. Open like
                # list/status (trust-local model) — no cwd/task content, only
                # a depth/bool/age summary.
                snap = self._spawn_health.snapshot()
                self._reply(
                    sock,
                    ok=True,
                    msg="spawn queue status",
                    queue_depth=snap.queue_depth,
                    spawn_in_progress=snap.spawn_in_progress,
                    spawn_in_progress_age_s=snap.spawn_in_progress_age_s,
                    oldest_queued_age_s=snap.oldest_queued_age_s,
                )
                return
            elif cmd == "performance-status":
                status = self._orch.performance_status(project=from_project)
                self._reply(sock, ok=True, msg="performance status", **status)
                return
            elif cmd == "remote-mirror-status":
                # 2026-08-13 remote-mirror-blank fix: `takkub doctor --live`
                # diagnostic for "phone shows nothing back from Lead".
                # Interpretation lives in doctor.check_remote_mirror_live —
                # this handler only gathers the raw live-only facts doctor's
                # pure-logic checks structurally cannot see (in-memory pane
                # state) AND cannot import (the `remote-bolt-on-isolation`
                # import-linter contract forbids both cli_server.py and
                # doctor.py from depending on `agent_takkub.remote`, so this
                # deliberately duplicates remote/notify.py's small uuid/path
                # resolution instead of importing it).
                self._reply(sock, **self._remote_mirror_status(from_project))
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
                # M3#16: transcript tails can contain secrets and screenshot paths
                # leak the filesystem layout. Surface them only to a caller holding
                # the Lead token; any other local caller (a teammate pane, a manual
                # shell) still gets per-pane state/stall but not the raw content.
                # (The cockpit's own status bar reads pane_status_report directly,
                # not over IPC, so the UI is unaffected.)
                if not self._caller_is_lead(req):
                    for _info in (report.get("panes") or {}).values():
                        if isinstance(_info, dict):
                            _info.pop("transcript_tail", None)
                            _info.pop("last_screenshot", None)
                self._reply(sock, ok=True, msg="status report", report=report)
                return
            elif cmd == "inbox":
                # #231: `takkub status` could only ever say a role's report was
                # "queued — not yet delivered"; nothing let Lead read the
                # content back out short of hand-Glob'ing runtime/sessions.
                # Lead-only (see _LEAD_ONLY_CMDS above) — same M3#16
                # rationale as status's transcript_tail gate.
                items = self._orch.inbox_report(project=from_project, role=req.get("role"))
                self._reply(sock, ok=True, msg=f"{len(items)} pending item(s)", items=items)
                return
            elif cmd == "wait-begin":
                # #242: register (or attach to) a wait for one or more roles.
                # See lead_wait.py — this is the single per-project poll
                # registration `takkub wait` blocks on client-side, replacing
                # the hand-rolled `takkub status` loops Lead used to write.
                _resolve_project_wb = getattr(self._orch, "_resolve_project", None)
                project_ns_wb = (
                    _resolve_project_wb(from_project)
                    if _resolve_project_wb is not None
                    else (from_project or "default")
                )
                result = self._orch.begin_wait(
                    project_ns_wb,
                    req.get("roles") or [],
                    float(req.get("timeout") or 0.0),
                )
                self._reply(sock, **result)
                return
            elif cmd == "wait-poll":
                _resolve_project_wp = getattr(self._orch, "_resolve_project", None)
                project_ns_wp = (
                    _resolve_project_wp(from_project)
                    if _resolve_project_wp is not None
                    else (from_project or "default")
                )
                result = self._orch.poll_wait(project_ns_wp, str(req.get("wait_id") or ""))
                self._reply(sock, **result)
                return
            elif cmd == "wait-end":
                _resolve_project_we = getattr(self._orch, "_resolve_project", None)
                project_ns_we = (
                    _resolve_project_we(from_project)
                    if _resolve_project_we is not None
                    else (from_project or "default")
                )
                self._orch.end_wait(project_ns_we, str(req.get("wait_id") or ""))
                self._reply(sock, ok=True, msg="wait ended")
                return
            elif cmd == "wait-cancel":
                # #249 item 5: `takkub wait --cancel` — releases whatever
                # registration is active for this project without needing a
                # wait_id (a fresh CLI invocation never has one).
                _resolve_project_wc = getattr(self._orch, "_resolve_project", None)
                project_ns_wc = (
                    _resolve_project_wc(from_project)
                    if _resolve_project_wc is not None
                    else (from_project or "default")
                )
                cancelled, cancel_msg = self._orch.cancel_wait(project_ns_wc)
                self._reply(sock, ok=cancelled, msg=cancel_msg)
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
            elif cmd == "task-show":
                task_role = req.get("role", "")
                ok_t, msg_t, payload_t = self._orch.task_show_info(task_role, project=from_project)
                if ok_t:
                    self._reply(sock, ok=True, msg=msg_t, **payload_t)
                else:
                    self._reply(sock, ok=False, msg=msg_t)
                return
            elif cmd == "messages":
                # #277: read-only audit of `takkub send` traffic for one role.
                ok_m, msg_m, lines_m = self._orch.role_message_log(
                    req.get("role", ""),
                    project=from_project,
                    limit=int(req.get("limit", 20) or 20),
                )
                self._reply(sock, ok=ok_m, msg=msg_m, lines=lines_m)
                return
            elif cmd == "task-reconcile":
                ok, msg = self._orch.task_reconcile(
                    project=from_project, dry_run=bool(req.get("dry_run", False))
                )
            elif cmd == "task-close":
                ok, msg = self._orch.task_close_role(
                    req.get("role", ""),
                    project=from_project,
                    force=bool(req.get("force", False)),
                    dry_run=bool(req.get("dry_run", False)),
                )
            elif cmd == "task-cancel":
                ok, msg = self._orch.cancel_task_delivery(
                    req.get("role", ""),
                    project=from_project,
                )
            elif cmd == "harvest-done":
                harvest_role = req.get("role", "")
                harvest_note = req.get("note", "harvested by lead")
                ok, msg = self._orch.done(harvest_role, note=harvest_note, project=from_project)
            elif cmd == "pipeline-run":
                template_id = (req.get("template_id") or "").strip()
                if not template_id:
                    self._reply(sock, ok=False, msg="missing arg: 'template_id'")
                    return
                # Pre-check before the async schedule so we don't ack ok=true for
                # a missing/empty template that run_pipeline would reject silently.
                pre_ok, pre_msg = self._orch.pipeline_precheck(template_id, project=from_project)
                if not pre_ok:
                    self._reply(sock, ok=False, msg=pre_msg)
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

    def _remote_mirror_status(self, from_project: str | None) -> dict:
        """Live facts behind `takkub doctor --live`'s remote-mirror check
        (2026-08-13): which provider actually backs this project's Lead
        pane, whether that provider has a registered remote-history scanner
        at all, and — for claude, the only provider with a fixed uuid-exact
        transcript path — whether that exact file exists on disk. Mirrors
        remote/notify.py's `pane_provider_name`/`_lead_session_uuid`/
        `_resolve_claude_jsonl_path` logic verbatim rather than importing
        it (forbidden by the `remote-bolt-on-isolation` contract)."""
        resolve_project = getattr(self._orch, "_resolve_project", None)
        project_ns = (
            resolve_project(from_project)
            if resolve_project is not None
            else (from_project or "default")
        )

        panes_by_project = getattr(self._orch, "_panes_by_project", None)
        pane_state = getattr(self._orch, "_pane_state", None)
        panes = panes_by_project.get(project_ns) if isinstance(panes_by_project, dict) else None
        lead_pane = panes.get("lead") if isinstance(panes, dict) else None

        provider = None
        model = getattr(lead_pane, "model", None)
        model_provider = getattr(model, "provider_name", None)
        if isinstance(model_provider, str) and model_provider.strip():
            provider = model_provider.strip().lower()
        else:
            from .provider_config import effective_provider_for

            provider = str(effective_provider_for("lead", project_ns) or "").strip().lower()

        from .provider_spec import PROVIDER_REGISTRY

        spec = PROVIDER_REGISTRY.get(provider)
        supports_remote_history = bool(spec is not None and spec.supports_remote_history)

        session_uuid = None
        if isinstance(pane_state, dict):
            from .orchestrator_text import _exit_key

            ps = pane_state.get(_exit_key(project_ns, "lead"))
            uuid_val = getattr(ps, "session_uuid", None) if ps is not None else None
            if isinstance(uuid_val, str) and uuid_val.strip():
                session_uuid = uuid_val.strip()

        # Only claude resolves a Lead session by an exact uuid->jsonl glob
        # (codex/gemini pick their own file by cwd + mtime, so "does the
        # uuid's file exist" isn't a meaningful question for them — None
        # means "not applicable", never a false negative).
        transcript_exists: bool | None = None
        if provider == "claude" and session_uuid:
            try:
                from .user_profile import config_dir_for

                base = config_dir_for(project_ns) / "projects"
                transcript_exists = any(base.glob(f"*/{session_uuid}.jsonl"))
            except OSError:
                transcript_exists = False

        return {
            "ok": True,
            "msg": "remote mirror status",
            "project": project_ns,
            "provider": provider,
            "supports_remote_history": supports_remote_history,
            "lead_pane_open": lead_pane is not None,
            "session_uuid": session_uuid,
            "transcript_exists": transcript_exists,
        }

    def _reply(self, sock: QTcpSocket, *, ok: bool, msg: str, **extra) -> None:
        payload = {"ok": ok, "msg": msg, **extra}
        sock.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        sock.flush()
