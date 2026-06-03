"""CLI server: listens on a local TCP port for JSON requests from the `takkub` CLI.

Protocol (newline-delimited JSON):

  request:  {"cmd": "send|assign|spawn|close|done|list", ...args}
  response: {"ok": bool, "msg": str, ...extras}

Runs on the Qt main thread via QTcpServer so all calls into Orchestrator are
serialised naturally.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket

from .config import write_port
from .orchestrator import Orchestrator

# Commands that mutate cockpit structure — only the Lead pane is allowed to
# run these. The gate is enforced server-side so raw TCP clients that bypass
# the cli.py role check (including confused teammate shells) are rejected.
_LEAD_ONLY_CMDS = frozenset({"spawn", "assign", "close", "close-all", "harvest", "harvest-done"})

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
            sock.readyRead.connect(lambda s=sock: self._on_ready_read(s))
            sock.disconnected.connect(sock.deleteLater)

    def _on_ready_read(self, sock: QTcpSocket) -> None:
        # read everything currently available, split on newline, dispatch each
        while sock.canReadLine():
            line = bytes(sock.readLine()).decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError as e:
                self._reply(sock, ok=False, msg=f"bad json: {e}")
                continue
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

        # end-session: Lead-only (reverse of done — only Lead may call this).
        if cmd == "end-session":
            from_role = (req.get("from") or "").lower().strip()
            if from_role not in ("lead", ""):
                self._reply(sock, ok=False, msg="only lead can call end-session")
                return

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
                if cmd == "spawn":
                    QTimer.singleShot(
                        0,
                        lambda: self._orch.spawn(role, cwd=req.get("cwd"), project=from_project),
                    )
                    self._reply(sock, ok=True, msg=f"spawning {role} (async)")
                else:
                    QTimer.singleShot(
                        0,
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
                    self._reply(sock, ok=True, msg=f"task queued for {role} (spawning async)")
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
