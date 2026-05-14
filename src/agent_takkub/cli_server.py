"""CLI server: listens on a local TCP port for JSON requests from the `takkub` CLI.

Protocol (newline-delimited JSON):

  request:  {"cmd": "send|assign|spawn|close|done|list", ...args}
  response: {"ok": bool, "msg": str, ...extras}

Runs on the Qt main thread via QTcpServer so all calls into Orchestrator are
serialised naturally.
"""

from __future__ import annotations

import json

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket

from .config import write_port
from .orchestrator import Orchestrator


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
        try:
            if cmd == "spawn":
                ok, msg = self._orch.spawn(req["role"], cwd=req.get("cwd"))
            elif cmd == "assign":
                ok, msg = self._orch.assign(
                    req["role"], cwd=req.get("cwd"), task=req.get("task", "")
                )
            elif cmd == "send":
                ok, msg = self._orch.send(
                    req["to"], msg=req.get("msg", ""), from_role=req.get("from")
                )
            elif cmd == "close":
                ok, msg = self._orch.close(req["role"])
            elif cmd == "close-all":
                ok, msg = self._orch.close_all_teammates()
            elif cmd == "done":
                ok, msg = self._orch.done(req.get("from") or "", note=req.get("note", ""))
            elif cmd == "list":
                self._reply(
                    sock,
                    ok=True,
                    msg="status",
                    status=self._orch.list_status(project=from_project),
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
