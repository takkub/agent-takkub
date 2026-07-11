"""HeadlessPane: display-free stand-in for AgentPane (#105 Phase B).

Wraps an `AgentPaneModel` (Phase A) and gives the orchestrator/spawn_engine
the exact attribute/method/signal surface they call on a real `AgentPane` —
`role`/`session`/`state`/`last_note`, `set_state()`, `attach_session()`,
`detach_session()`, `mark_expected_exit()`, `current_usage()`,
`set_worktree_branch()`, and the `spawnRequested`/`closeRequested`/
`inputBytes` signals `spawn_engine.register_pane()` connects to — with zero
QWidget/QWebEngineView/TerminalWidget construction, so the engine can spawn
and drive a pane with no display at all.

`spawnRequested`/`closeRequested`/`inputBytes` exist only so
`register_pane()`'s `.connect()` calls succeed — headless mode has no pane
header buttons or local terminal-widget keyboard capture to emit them from.
All headless input (CLI `takkub`, the PWA remote-control) already writes
straight to `pane.session.write(...)` via `Orchestrator`/`lead_inbox`, the
same path used for engine-originated writes on the desktop build, never
through these signals — see docs/design/2026-07-11-105-phaseB-headless.md.
"""

from __future__ import annotations

import time

from PyQt6.QtCore import QObject, pyqtSignal

from .agent_pane_model import AgentPaneModel
from .pty_session import PtySession
from .roles import Role


class HeadlessPane(QObject):
    """One agent slot's session/state, driven with no display."""

    spawnRequested = pyqtSignal(str)  # role name — unused headless, kept for register_pane()
    closeRequested = pyqtSignal(str)
    inputBytes = pyqtSignal(str, bytes)

    def __init__(self, role: Role) -> None:
        super().__init__()
        self.model = AgentPaneModel(role)
        self._exit_conn = None

    # ── proxies (mirrors AgentPane's property layer, Phase A) ──────
    @property
    def role(self) -> Role:
        return self.model.role

    @property
    def session(self) -> PtySession | None:
        return self.model.session

    @session.setter
    def session(self, value: PtySession | None) -> None:
        self.model.session = value

    @property
    def state(self) -> str:
        return self.model.state

    @state.setter
    def state(self, value: str) -> None:
        self.model.state = value

    @property
    def last_note(self) -> str | None:
        return self.model.last_note

    @last_note.setter
    def last_note(self, value: str | None) -> None:
        self.model.last_note = value

    @property
    def _last_output_ts(self) -> float:
        return self.model.last_output_ts

    @_last_output_ts.setter
    def _last_output_ts(self, value: float) -> None:
        self.model.last_output_ts = value

    @property
    def _tp_total_bytes(self) -> int:
        return self.model.tp_total_bytes

    @_tp_total_bytes.setter
    def _tp_total_bytes(self, value: int) -> None:
        self.model.tp_total_bytes = value

    @property
    def _worktree_branch(self) -> str | None:
        return self.model.worktree_branch

    @property
    def _session_generation(self) -> int:
        return self.model.session_generation

    @property
    def _session_cwd(self) -> str | None:
        return self.model.session_cwd

    @property
    def _transcript_path(self) -> object | None:
        return self.model.transcript_path

    @_transcript_path.setter
    def _transcript_path(self, value: object | None) -> None:
        self.model.transcript_path = value

    # ── data-only mirrors of AgentPane's view-mixed methods ────────
    def set_state(self, state: str, note: str | None = None) -> None:
        self.model.state = state
        if note is not None:
            self.model.last_note = note

    def mark_expected_exit(self) -> None:
        self.model.mark_expected_exit()

    def current_usage(self) -> dict | None:
        return self.model.current_usage()

    def set_worktree_branch(self, branch: str | None) -> None:
        self.model.set_worktree_branch(branch)

    def attach_session(self, session: PtySession, cwd: str | None = None) -> None:
        """Bind `session` — the data-only half of `AgentPane.attach_session`
        (no terminal resize/focus/idle-flag/token-label widget work)."""
        self.model.session = session
        self.model.last_output_ts = time.time()
        self.model.tp_total_bytes = 0
        self.model.session_generation += 1
        gen = self.model.session_generation
        session.bytesIn.connect(self._mark_output_ts)
        self._exit_conn = session.processExited.connect(lambda code, g=gen: self._on_exit(code, g))
        self.model.spawn_ts = time.time()
        self.model.session_cwd = cwd
        self.model.session_jsonl = None
        self.model.last_usage = None
        self.set_state("active")

    def detach_session(self) -> None:
        session = self.model.session
        if session is not None:
            try:
                session.bytesIn.disconnect(self._mark_output_ts)
            except Exception:
                pass
            if self._exit_conn is not None:
                try:
                    session.processExited.disconnect(self._exit_conn)
                except Exception:
                    pass
                self._exit_conn = None
            self.model.session = None
        self.model.session_jsonl = None
        self.model.last_usage = None

    def _mark_output_ts(self, _data: bytes) -> None:
        self.model.last_output_ts = time.time()

    def _on_exit(self, code: int, gen: int | None = None) -> None:
        if gen is not None and gen != self.model.session_generation:
            return
        new_state, note = self.model.decide_exit_state(code)
        self.set_state(new_state, note=note)
        self.model.expected_exit = False
        self.detach_session()
