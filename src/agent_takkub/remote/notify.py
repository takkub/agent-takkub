"""notify.py — bridges Lead-level orchestrator events into the SSE
broadcaster (§6.5, X-check 2.1 — hooks confirmed against the running
orchestrator, not guessed):

* done events: `orch.agentDone` (orchestrator.py, emitted on every
  `takkub done`).
* live Lead output: `orch.statusChanged` re-discovers which pane is
  currently "lead" for the active project, then hooks that pane's
  `PtySession.bytesIn` — never `register_pane` monkeypatching (ruled out
  as too invasive during design cross-check).

Runs entirely on the Qt main thread (constructed inside
`RemoteControl._start`) — this is a normal Qt object wired to normal Qt
signals, not something a handler thread ever touches.

Scoping (ponytail, matches `api.py`): tracks the orchestrator's
currently-active project's Lead pane only. Upgrade path: accept a project
name (e.g. stamped on the SSE ticket) once the PWA offers a project picker.

Cross-project isolation (H-A): `orch.agentDone` fires for *every* project,
not just the active one, so every push is stamped with the event's own
`project_ns` and `SSEBroadcaster.push` drops it for any client whose ticket
was issued for a different project.
"""

from __future__ import annotations

import re

from PyQt6.QtCore import QObject, QTimer

_MAX_EVENT_CHARS = 4000
_COALESCE_MS = 150
_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]")


class LeadNotifier(QObject):
    def __init__(self, orch, broadcaster) -> None:
        super().__init__()
        self._orch = orch
        self._broadcaster = broadcaster
        self._session = None
        self._project_ns: str | None = None
        self._buf = bytearray()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._flush)

        orch.agentDone.connect(self._on_done)
        orch.statusChanged.connect(self._resync_lead_session)
        self._resync_lead_session()

    # ── discover / rediscover the live Lead pane's session ──────────────
    def _pane_for_role(self, role: str):
        panes_by_project = getattr(self._orch, "_panes_by_project", None)
        if not isinstance(panes_by_project, dict):
            return None
        project_ns = self._orch._resolve_project(None)
        return panes_by_project.get(project_ns, {}).get(role)

    def _resync_lead_session(self) -> None:
        self._project_ns = self._orch._resolve_project(None)
        pane = self._pane_for_role("lead")
        session = getattr(pane, "session", None) if pane is not None else None
        if session is self._session:
            return
        if self._session is not None:
            try:
                self._session.bytesIn.disconnect(self._on_lead_bytes)
            except (TypeError, RuntimeError):
                pass
        self._session = session
        if session is not None:
            session.bytesIn.connect(self._on_lead_bytes)

    # ── live output: coalesce + cap before it ever reaches SSE (B3) ─────
    def _on_lead_bytes(self, data: bytes) -> None:
        self._buf.extend(_ANSI_RE.sub(b"", data))
        cap = _MAX_EVENT_CHARS * 4
        if len(self._buf) > cap:
            del self._buf[: len(self._buf) - cap]
        if not self._timer.isActive():
            self._timer.start(_COALESCE_MS)

    def _flush(self) -> None:
        if not self._buf:
            return
        text = bytes(self._buf).decode("utf-8", errors="replace")[-_MAX_EVENT_CHARS:]
        self._buf.clear()
        self._broadcaster.push("lead", text, self._project_ns)

    # ── done events ───────────────────────────────────────────────────
    def _on_done(self, project_ns: str, role: str, note: str) -> None:
        # H-A: stamp the event's own project, not whatever project happens
        # to be active right now — `agentDone` fires for every project.
        self._broadcaster.push("done", f"{role}: {note}"[:_MAX_EVENT_CHARS], project_ns)

    def stop(self) -> None:
        for signal, slot in (
            (self._orch.agentDone, self._on_done),
            (self._orch.statusChanged, self._resync_lead_session),
        ):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        if self._session is not None:
            try:
                self._session.bytesIn.disconnect(self._on_lead_bytes)
            except (TypeError, RuntimeError):
                pass
            self._session = None
        self._timer.stop()
