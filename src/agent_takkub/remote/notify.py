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

# ── junk-line filter (best-effort heuristic, see UI-junkfilter-backend.md):
# strips TUI chrome (composer box, spinner/status footer, tool-call bullets)
# from decoded Lead output before it reaches the SSE broadcaster, keeping
# only the actual conversation text + done/summary content.
_BOX_LINE_RE = re.compile(r"^\s*[│┃╭╮╯╰┌┐└┘├┤┬┴┼═║╔╗╚╝╠╣╦╩╬]")
_GLYPH_ONLY_RE = re.compile(
    r"^[\s│─┃┄┅┆┇┈┉┊┋╭╮╯╰╱╲╳┌┐└┘├┤┬┴┼═║╔╗╚╝╠╣╦╩╬"
    r"✻✶✳✢✽⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏·•]*$"
)
_TOOL_BULLET_RE = re.compile(r"^\s*⏺")
_SPINNER_PREFIX_RE = re.compile(r"^\s*[✻✶✳✢✽⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]")
_XML_TAG_LINE_RE = re.compile(r"^\s*</?[a-zA-Z][\w:.-]*(?:\s[^>]*)?/?>\s*$")
_CHROME_RE = re.compile(
    r"esc to interrupt"
    r"|for shortcuts"
    r"|bypass permissions"
    r"|booting mcp"
    r"|context left"
    r"|auto-compact"
    r"|usage limit"
    r"|\d[\d,.]*\s*[kmb]?\s*tokens?\b",
    re.IGNORECASE,
)


def _is_junk_line(line: str) -> bool:
    if _GLYPH_ONLY_RE.match(line) and line.strip():
        return True
    if _BOX_LINE_RE.match(line):
        return True
    if _TOOL_BULLET_RE.match(line):
        return True
    if _SPINNER_PREFIX_RE.match(line):
        return True
    if _XML_TAG_LINE_RE.match(line):
        return True
    if _CHROME_RE.search(line):
        return True
    return False


def _filter_junk(text: str) -> str:
    kept: list[str] = []
    prev_blank = True  # drop leading blank lines
    for line in text.split("\n"):
        if _is_junk_line(line):
            continue
        if not line.strip():
            if prev_blank:
                continue
            kept.append("")
            prev_blank = True
            continue
        kept.append(line)
        prev_blank = False
    while kept and kept[-1] == "":
        kept.pop()
    return "\n".join(kept)


class LeadNotifier(QObject):
    def __init__(self, orch, broadcaster) -> None:
        super().__init__()
        self._orch = orch
        self._broadcaster = broadcaster
        self._session = None
        self._project_ns: str | None = None
        # list of (project_ns, bytearray) — project_ns is captured per-chunk at
        # receipt time (B1), not read fresh at flush time. A mid-coalesce
        # project switch (_resync_lead_session changes self._project_ns before
        # the 150ms timer fires) would otherwise stamp buffered bytes from the
        # old project with the new project's namespace.
        self._buf: list[tuple[str | None, bytearray]] = []
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
        cleaned = _ANSI_RE.sub(b"", data)
        if not cleaned:
            return
        ns = self._project_ns
        if self._buf and self._buf[-1][0] == ns:
            self._buf[-1][1].extend(cleaned)
        else:
            self._buf.append((ns, bytearray(cleaned)))
        cap = _MAX_EVENT_CHARS * 4
        total = sum(len(chunk) for _, chunk in self._buf)
        while total > cap and self._buf:
            overflow = total - cap
            _, oldest = self._buf[0]
            if len(oldest) <= overflow:
                total -= len(oldest)
                self._buf.pop(0)
            else:
                del oldest[:overflow]
                total -= overflow
        if not self._timer.isActive():
            self._timer.start(_COALESCE_MS)

    def _flush(self) -> None:
        if not self._buf:
            return
        chunks = self._buf
        self._buf = []
        for ns, chunk in chunks:
            raw = bytes(chunk).decode("utf-8", errors="replace")
            text = _filter_junk(raw)[-_MAX_EVENT_CHARS:]
            if text:
                self._broadcaster.push("lead", text, ns)

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
