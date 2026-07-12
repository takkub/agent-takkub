"""AgentPane: a single slot in the cockpit grid.

States:
  - empty:    placeholder with role name + Spawn button + (optional) note
  - active:   terminal widget streaming claude.exe output
  - working:  active + small status indicator (e.g. "running task")
  - done:     terminal cleared, placeholder back, optional badge of last result

The header bar is always visible (role label + status dot + Spawn/Close
buttons). The body switches between QStackedWidget pages.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

from PyQt6 import sip
from PyQt6.QtCore import QSettings, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import cockpit_theme
from .agent_pane_model import AgentPaneModel
from .config import RUNTIME_DIR
from .pty_session import PtySession
from .roles import LEAD, USER_DRIVEN_ROLES, Role
from .terminal_widget import TerminalWidget
from .token_meter import find_latest_session, read_last_usage

# Pane state → dot color, using cockpit_theme state tokens (semantic — never
# gold). "empty" is a neutral faint grey; the rest are the ok/warn/info/exit/
# error ramp.
STATUS_COLORS = {
    "empty": cockpit_theme.TEXT_FAINT,
    "active": cockpit_theme.STATE_OK_BRIGHT,
    "working": cockpit_theme.STATE_WARN_BRIGHT,
    "done": cockpit_theme.STATE_INFO_BRIGHT,
    "exited": cockpit_theme.STATE_EXITED,  # orange — unexpected exit, can respawn
    "error": cockpit_theme.STATE_ERROR,
}

SPINNER_FRAMES = "◐◓◑◒"

# Auto clear-view tuning (teammate panes only — see AgentPane.__init__).
_DONE_AUTO_CLEAR_DELAY_MS = 5_000
_IDLE_AUTO_CLEAR_THRESHOLD_S = 600  # 10 minutes


class AgentPane(QFrame):
    """One agent slot. Owns its PtySession when active."""

    spawnRequested = pyqtSignal(str)  # role name
    closeRequested = pyqtSignal(str)
    inputBytes = pyqtSignal(str, bytes)  # role name, bytes for pty
    # Off-thread token-meter result → applied on the main thread by
    # _apply_token_meter. Carries (session_path | None, usage dict | None).
    _tokenMeterReady = pyqtSignal(object, object)

    def __init__(self, role: Role, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.role = role
        # Session + state bookkeeping lives in AgentPaneModel (issue #105
        # Phase A) so it stays importable/testable without a display. The
        # properties below proxy the legacy self.<field> reads/writes onto
        # it, so every other module's `pane.session`/`pane.state`/... call
        # sites are unchanged.
        self.model = AgentPaneModel(role)

        # spinner + elapsed time bookkeeping for the working state
        self._spinner_idx = 0
        self._working_start: float | None = None
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(250)
        self._tick_timer.timeout.connect(self._tick)

        # smart-local-echo idle flag — declared here so detach_session can
        # always reset it safely. Filled by attach_session / _sync_idle_flag.
        self._last_idle: bool | None = None
        # throttle pyte-state polling so chatty TUIs don't fire it 50+/sec
        self._idle_check_at: float = 0.0

        self._token_timer = QTimer(self)
        self._token_timer.setInterval(5_000)
        self._token_timer.timeout.connect(self._refresh_token_meter)
        # Apply off-thread token-meter reads back on the main (GUI) thread.
        self._tokenMeterReady.connect(self._apply_token_meter)
        self._token_refreshing = False

        # Issue #35 — render-path coalescing.
        # Instead of forwarding every PTY chunk to xterm.js immediately (which
        # floods the Qt main thread when an agent loops and emits thousands of
        # chunks/s), we buffer incoming bytes and flush on a ~16 ms timer
        # (≈60 fps).  Force-flush when the buffer exceeds 256 KB so a single
        # bursty write doesn't accumulate unbounded backlog.
        self._render_buf: bytearray = bytearray()
        self._render_timer = QTimer(self)
        self._render_timer.setInterval(16)  # ≈60 fps
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._flush_render_buf)

        # Auto clear-view (teammate panes only, never Lead):
        #  - "done": clear scrollback ~5s after a done report, unless the
        #    user is looking at this pane right now — then defer until it
        #    stops being the active tab.
        #  - idle: clear if a teammate pane sits with no PTY output for
        #    _IDLE_AUTO_CLEAR_THRESHOLD_S while not the active tab.
        # `_keepalive_active` mirrors set_keepalive() — True while this pane
        # is the one currently on screen (project tab visible AND pane tab
        # current), which is the existing "active tab" signal the tab-visibility
        # keep-alive feature already computes.
        self._keepalive_active: bool = True
        self._pending_auto_clear: bool = False
        self._idle_auto_cleared: bool = False
        self._done_clear_timer = QTimer(self)
        self._done_clear_timer.setSingleShot(True)
        self._done_clear_timer.timeout.connect(self._on_done_clear_timeout)
        self._idle_clear_timer = QTimer(self)
        self._idle_clear_timer.setInterval(60_000)  # poll once/min — cheap
        self._idle_clear_timer.timeout.connect(self._check_idle_auto_clear)
        self._idle_clear_timer.start()

        # Qt CSS ID selectors use "#id" syntax; a "#" inside the name itself
        # breaks the parser (e.g. "pane_qa#1" → "#pane_qa#1" is invalid CSS).
        # Sanitise by replacing "#" with "-" for the objectName/stylesheet while
        # keeping role.name intact for signal identity and registry routing.
        _css_safe = role.name.replace("#", "-")
        self.setObjectName(f"pane_{_css_safe}")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(self._stylesheet())

        # _expected_exit / _session_generation live on self.model (see the
        # property proxies below) — AgentPaneModel.__init__ already seeded
        # their defaults.

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # header
        header = QWidget(self)
        header.setObjectName("paneHeader")
        header.setFixedHeight(28)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(8, 0, 4, 0)
        hl.setSpacing(6)

        self._dot = QLabel("●", header)
        self._dot.setStyleSheet(f"color: {STATUS_COLORS['empty']}; font-size: 14px;")

        self._title = QLabel(role.label, header)
        f = QFont()
        f.setBold(True)
        self._title.setFont(f)
        title_color = cockpit_theme.ROLE_COLORS.get(role.name, role.color)
        self._title.setStyleSheet(f"color: {title_color};")

        self._note = QLabel("", header)
        self._note.setStyleSheet(f"color: {cockpit_theme.TEXT_MUTED}; font-size: 11px;")

        # Token-usage badge: shows "52k/200k" of the current claude session
        # for this pane, refreshed on a slow timer. Hidden until the pane has
        # an active session with at least one assistant turn on disk.
        self._token_label = QLabel("", header)
        self._token_label.setStyleSheet(f"color: {cockpit_theme.TEXT_FAINT_ALT}; font-size: 11px;")
        self._token_label.setToolTip("Last-turn context occupancy (prompt tokens / limit)")
        self._token_label.hide()

        self._btn_spawn = QPushButton("Spawn", header)
        self._btn_spawn.setFixedHeight(22)
        self._btn_spawn.clicked.connect(lambda: self.spawnRequested.emit(self.role.name))

        self._btn_export = QPushButton("⤓", header)
        self._btn_export.setFixedSize(22, 22)
        self._btn_export.setToolTip("Export pane buffer to .txt")
        self._btn_export.clicked.connect(self._export_buffer)
        self._btn_export.hide()

        self._btn_clear_view = QPushButton("🧹", header)
        self._btn_clear_view.setFixedSize(22, 22)
        self._btn_clear_view.setToolTip("ล้างหน้าจอ pane (โปรแกรมยังรันต่อ ไม่กระทบงาน)")
        self._btn_clear_view.clicked.connect(self._clear_pane_view)
        self._btn_clear_view.hide()

        # Input lock toggle (orchestrator-driven panes only). Teammates are
        # driven by takkub assign/send, so the user almost never types into
        # them — locking by default stops an accidental keypress from derailing a
        # working agent. User-driven panes (Lead = command surface, Shell =
        # ad-hoc terminal the user opened to type in) are never auto-locked and
        # get no lock button. Default: teammates locked, user-driven unlocked.
        self._lockable = role.name not in USER_DRIVEN_ROLES
        self._input_locked = self._lockable
        self._btn_lock: QPushButton | None = None
        if self._lockable:
            self._btn_lock = QPushButton("🔒", header)
            self._btn_lock.setFixedSize(22, 22)
            self._btn_lock.clicked.connect(self._toggle_input_lock)
            self._refresh_lock_button()

        self._btn_min = QPushButton("▾", header)
        self._btn_min.setFixedSize(22, 22)
        self._btn_min.setToolTip("Minimise pane (collapse body)")
        self._btn_min.clicked.connect(self._toggle_minimised)

        self._btn_close = QPushButton("×", header)
        self._btn_close.setFixedSize(22, 22)
        self._btn_close.setToolTip("Close pane")
        self._btn_close.clicked.connect(lambda: self.closeRequested.emit(self.role.name))
        # Always visible — even in empty/exited states the user needs a way
        # to dismiss the pane (e.g. a Shell pane whose spawn just failed,
        # an exited claude session, or an empty preset slot the user never
        # used). Orchestrator.close() still gates Lead so clicking × on
        # Lead is a safe no-op.

        hl.addWidget(self._dot)
        hl.addWidget(self._title)
        hl.addWidget(self._note, 1)
        hl.addWidget(self._token_label)
        hl.addWidget(self._btn_spawn)
        hl.addWidget(self._btn_export)
        hl.addWidget(self._btn_clear_view)
        if self._btn_lock is not None:
            hl.addWidget(self._btn_lock)
        hl.addWidget(self._btn_min)
        hl.addWidget(self._btn_close)

        # whether the body (terminal/placeholder) is hidden, leaving only
        # the header strip visible to save vertical space.
        self._minimised = False

        # body: stacked placeholder vs terminal
        self._stack = QStackedWidget(self)

        # placeholder page
        ph = QWidget()
        phl = QVBoxLayout(ph)
        phl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg = QLabel(f"{role.label}\nempty slot")
        msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg.setStyleSheet(f"color: {cockpit_theme.TEXT_FAINT}; font-size: 13px;")
        phl.addWidget(msg)
        self._stack.addWidget(ph)

        # terminal page
        self._terminal = TerminalWidget()
        self._terminal.inputBytes.connect(lambda data: self.inputBytes.emit(self.role.name, data))
        self._terminal.fontSizeChanged.connect(self._save_font_size)
        # Seed the terminal with this pane's default lock state (teammate=locked).
        self._terminal.set_input_locked(self._input_locked)
        self._stack.addWidget(self._terminal)

        # restore last font size for this role
        self._restore_font_size()

        root.addWidget(header)
        root.addWidget(self._stack, 1)

    # ──────────────────────────────────────────────────────────────
    # model proxies — session/state bookkeeping lives on self.model
    # (AgentPaneModel, issue #105 Phase A); these keep every other module's
    # `pane.session`/`pane.state`/... call sites unchanged.
    # ──────────────────────────────────────────────────────────────
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
    def _worktree_branch(self) -> str | None:
        return self.model.worktree_branch

    @_worktree_branch.setter
    def _worktree_branch(self, value: str | None) -> None:
        self.model.worktree_branch = value

    @property
    def _expected_exit(self) -> bool:
        return self.model.expected_exit

    @_expected_exit.setter
    def _expected_exit(self, value: bool) -> None:
        self.model.expected_exit = value

    @property
    def _session_generation(self) -> int:
        return self.model.session_generation

    @_session_generation.setter
    def _session_generation(self, value: int) -> None:
        self.model.session_generation = value

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
    def _spawn_ts(self) -> float:
        return self.model.spawn_ts

    @_spawn_ts.setter
    def _spawn_ts(self, value: float) -> None:
        self.model.spawn_ts = value

    @property
    def _session_cwd(self) -> str | None:
        return self.model.session_cwd

    @_session_cwd.setter
    def _session_cwd(self, value: str | None) -> None:
        self.model.session_cwd = value

    @property
    def _session_jsonl(self):
        return self.model.session_jsonl

    @_session_jsonl.setter
    def _session_jsonl(self, value) -> None:
        self.model.session_jsonl = value

    @property
    def _last_usage(self) -> dict | None:
        return self.model.last_usage

    @_last_usage.setter
    def _last_usage(self, value: dict | None) -> None:
        self.model.last_usage = value

    @property
    def _context_limit(self) -> int | None:
        return self.model.context_limit

    @_context_limit.setter
    def _context_limit(self, value: int | None) -> None:
        self.model.context_limit = value

    @property
    def _transcript_path(self):
        return self.model.transcript_path

    @_transcript_path.setter
    def _transcript_path(self, value) -> None:
        self.model.transcript_path = value

    # ──────────────────────────────────────────────────────────────
    # state transitions
    # ──────────────────────────────────────────────────────────────
    def set_state(self, state: str, note: str | None = None) -> None:
        self.state = state
        if note is not None:
            self.last_note = note

        # spinner + elapsed only while actively working
        if state == "working":
            if self._working_start is None:
                self._working_start = time.time()
            if not self._tick_timer.isActive():
                self._tick_timer.start()
        else:
            self._working_start = None
            self._tick_timer.stop()

        self._refresh_note()
        self._dot.setStyleSheet(
            f"color: {STATUS_COLORS.get(state, cockpit_theme.TEXT_FAINT)}; font-size: 14px;"
        )
        if state in ("active", "working"):
            self._stack.setCurrentIndex(1)
            self._btn_spawn.hide()
            self._btn_export.show()
            self._btn_clear_view.show()
        else:
            self._stack.setCurrentIndex(0)
            self._btn_spawn.show()
            self._btn_export.hide()
            self._btn_clear_view.hide()
        # × is now always visible (see _btn_close init comment) — user
        # always has an escape hatch regardless of pane state.
        self._btn_close.show()

        # Auto clear-view: arm a delayed clear on entering "done", disarm on
        # leaving it (e.g. respawned before the delay elapsed). Lead is never
        # auto-cleared — it's the user's main screen and manages its own
        # context via /compact.
        if state == "done" and self.role.name != LEAD.name:
            self._pending_auto_clear = False
            self._done_clear_timer.start(_DONE_AUTO_CLEAR_DELAY_MS)
        else:
            self._done_clear_timer.stop()
            self._pending_auto_clear = False

    def _tick(self) -> None:
        self._spinner_idx = (self._spinner_idx + 1) % len(SPINNER_FRAMES)
        self._refresh_note()

    def _refresh_note(self) -> None:
        if self.state == "working" and self._working_start is not None:
            elapsed = int(time.time() - self._working_start)
            if elapsed < 60:
                t = f"{elapsed}s"
            else:
                t = f"{elapsed // 60}:{elapsed % 60:02d}"
            spinner = SPINNER_FRAMES[self._spinner_idx]
            text = f"{spinner} {t}"
            if self.last_note:
                text += f"  ·  {self.last_note[:60]}"
            self._note.setText(text)
        elif self.last_note and self.state != "empty":
            self._note.setText(self.last_note[:80])
        else:
            self._note.setText("")

    # Maximum render-buffer size before a forced flush regardless of timer.
    _RENDER_FLUSH_CAP = 256 * 1024  # 256 KB

    def _coalesce_bytes(self, data: bytes) -> None:
        """Accumulate PTY bytes and schedule a batched flush to xterm.js.

        Replaces the direct bytesIn→write_bytes connection so the Qt main
        thread isn't flooded when a pane emits thousands of chunks/s.
        Force-flushes immediately when the buffer exceeds _RENDER_FLUSH_CAP
        to bound worst-case latency on very large writes.
        """
        self._render_buf.extend(data)
        self._tp_total_bytes += len(data)
        if len(self._render_buf) >= self._RENDER_FLUSH_CAP:
            self._flush_render_buf()
        elif not self._render_timer.isActive():
            self._render_timer.start()

    def _flush_render_buf(self) -> None:
        """Write the coalesced buffer to xterm.js and reset the timer."""
        if not self._render_buf:
            return
        chunk = bytes(self._render_buf)
        self._render_buf.clear()
        self._render_timer.stop()
        try:
            self._terminal.write_bytes(chunk)
        except Exception:
            pass

    def _mark_output_ts(self, _data: bytes) -> None:
        """Slot bound to PtySession.bytesIn — bump the last-output
        wall-clock so the orchestrator's stuck-pane watchdog can tell a
        live working pane from a silently-hung one."""
        self._last_output_ts = time.time()
        self._idle_auto_cleared = False

    def attach_session(self, session: PtySession, cwd: str | None = None) -> None:
        """Bind a PtySession to this pane's terminal widget. `cwd` (optional)
        is shown in the header next to the role label."""
        if self.session is not None and self.session is not session:
            self.detach_session()
        self.session = session
        # Route PTY bytes through the coalescing buffer (issue #35).
        # _coalesce_bytes accumulates chunks and flushes to xterm.js every
        # ~16 ms, preventing main-thread flooding on high-throughput panes.
        session.bytesIn.connect(self._coalesce_bytes)
        # Also tap bytesIn into the stuck-pane watchdog so it knows when
        # claude was last *actually* producing output. Reset to "now" at
        # attach so the watchdog starts the clock from spawn time, not
        # from whatever the previous session left behind.
        self._last_output_ts = time.time()
        self._tp_total_bytes = 0
        self._idle_auto_cleared = False
        session.bytesIn.connect(self._mark_output_ts)
        # pyte still parses the bytes in parallel — use that to push the
        # "claude is idle at the ready prompt" signal into the terminal
        # widget so it can decide whether to local-echo keystrokes.
        session.outputUpdated.connect(self._sync_idle_flag)
        # Increment generation so stale exit signals from a previous session
        # (which may still be emitting after we attached a replacement) are
        # silently dropped by _on_exit's generation check.
        self._session_generation += 1
        _gen = self._session_generation
        # Keep the connection handle so detach_session can sever it. The
        # PtySession is parented to the engine (not this pane), so without an
        # explicit disconnect a late processExited would fire into a torn-down
        # widget. _on_exit also guards against that, but disconnecting is the
        # clean primary defense.
        self._exit_conn = session.processExited.connect(lambda code, g=_gen: self._on_exit(code, g))
        self._terminal.resized.connect(session.resize)
        self._update_title_with_cwd(cwd)
        self.set_state("active")
        # Every (re)spawn of a teammate pane starts input-locked, even if the
        # user had unlocked the previous session in this slot — an accidental
        # keypress into a freshly-spawned agent is exactly what we guard against.
        # User-driven panes (Lead / Shell) are exempt.
        if self._lockable:
            self.set_input_locked(True)
        self._terminal.setFocus()
        # explicit reset — start in busy (no local echo) until pyte
        # confirms we're at the ready prompt
        self._last_idle = None
        self._idle_check_at = 0.0
        self._terminal.set_idle(False)

        # Token-meter: remember when we spawned + which cwd to scan, then
        # start polling. The very first claude turn won't land for a few
        # seconds, so the label stays hidden until read_last_usage returns.
        self._spawn_ts = time.time()
        self._session_cwd = cwd
        # let the terminal resolve clicked relative paths against this cwd
        self._terminal.set_cwd(cwd)
        self._session_jsonl = None
        self._last_usage = None
        self._token_label.hide()
        if cwd:
            self._token_timer.start()
            # one quick refresh after a short delay so the badge appears as
            # soon as the first turn lands without waiting a full interval
            QTimer.singleShot(1_500, self._refresh_token_meter)

    # Minimum interval between pyte-state polls so a chatty TUI doesn't
    # fire this 50+ times a second.
    _IDLE_POLL_MIN_INTERVAL = 0.15  # 150 ms

    def _sync_idle_flag(self) -> None:
        if self.session is None:
            return
        now = time.time()
        if now - self._idle_check_at < self._IDLE_POLL_MIN_INTERVAL:
            return
        self._idle_check_at = now
        try:
            # Lock-free cached read (#106) — avoids taking PtySession's
            # _screen_lock on the main thread on every outputUpdated, which
            # was contending with the reader thread's stream.feed() under the
            # same lock. See PtySession.is_at_ready_prompt_cached().
            idle = self.session.is_at_ready_prompt_cached()
        except Exception:
            idle = False
        if idle == self._last_idle:
            return
        self._last_idle = idle
        try:
            self._terminal.set_idle(idle)
        except Exception:
            # never let a JS bridge hiccup tear the signal chain down
            pass

    def set_worktree_branch(self, branch: str | None) -> None:
        """Tag (or clear) the isolated-worktree branch chip (issue #81).

        Re-renders the header so the 🌿 <branch> marker appears immediately.
        """
        self.model.set_worktree_branch(branch)
        self._update_title_with_cwd(self._session_cwd)

    def _update_title_with_cwd(self, cwd: str | None) -> None:
        # Isolated worktree (issue #81): a 🌿 chip makes the isolation obvious.
        wt = f"  🌿 {self._worktree_branch}" if self._worktree_branch else ""
        if not cwd:
            self._title.setText(f"{self.role.label}{wt}")
            return
        # show role + tail of the path (basename) so user knows where the
        # agent is working. eg.  Frontend · app-web
        import os

        tail = os.path.basename(cwd.rstrip("/\\")) or cwd
        self._title.setText(f"{self.role.label} · {tail}{wt}")

    def detach_session(self) -> None:
        # Flush any pending render bytes before tearing down so no output is
        # silently dropped when the session ends (e.g. final "done" message).
        self._flush_render_buf()
        self._render_timer.stop()
        if self.session is not None:
            outgoing = self.session
            try:
                self.session.bytesIn.disconnect(self._coalesce_bytes)
            except Exception:
                pass
            try:
                self.session.bytesIn.disconnect(self._mark_output_ts)
            except Exception:
                pass
            try:
                self.session.outputUpdated.disconnect(self._sync_idle_flag)
            except Exception:
                pass
            try:
                self._terminal.resized.disconnect(self.session.resize)
            except Exception:
                pass
            # Sever processExited so a late exit from this (engine-owned)
            # session can't call back into a pane that's being torn down.
            exit_conn = getattr(self, "_exit_conn", None)
            if exit_conn is not None:
                try:
                    self.session.processExited.disconnect(exit_conn)
                except Exception:
                    pass
                self._exit_conn = None
            self.session = None
            outgoing.terminate()
        # __new__-built pane doubles (see test_render_coalesce.py) never ran
        # __init__, so this attr may not exist — and reading a missing attr on
        # such a double raises RuntimeError, not AttributeError, so getattr's
        # default alone can't swallow it.
        try:
            self._done_clear_timer.stop()
        except (RuntimeError, AttributeError):
            pass
        self._pending_auto_clear = False
        self._last_idle = None
        # Full reset (scrollback + heartbeat stop) so Lead's reused pane
        # doesn't carry the prior project's transcript and timers across
        # restarts. attach_session will restart the heartbeat when the
        # fresh page reports ready.
        self._terminal.reset()
        # tear down token-meter state
        self._token_timer.stop()
        self._session_jsonl = None
        self._last_usage = None
        self._token_label.hide()

    def _on_exit(self, code: int, gen: int | None = None) -> None:
        # Teardown guard: the PtySession is parented to the engine, so it can
        # outlive this pane and deliver a queued processExited after Qt has
        # begun destroying the widget. Children (e.g. _tick_timer) are deleted
        # before the parent, so set_state()'s _tick_timer.stop() would raise
        # RuntimeError inside this slot → PyQt aborts the process (segfault).
        # Drop the exit once the tick timer's C++ object is gone. The try guards
        # the __new__-built pane doubles used in unit tests, where any missing
        # attribute read raises RuntimeError instead of AttributeError.
        try:
            timer_deleted = sip.isdeleted(self._tick_timer)
        except (RuntimeError, AttributeError):
            timer_deleted = False
        if timer_deleted:
            return
        # Drop stale signals: if the generation captured at connection time no
        # longer matches the current generation, an old session emitted after a
        # replacement was attached — ignore it to protect the new session's state.
        if gen is not None and gen != self._session_generation:
            return
        # Distinguish:
        #   - expected: orchestrator.close() / done() called terminate first
        #   - unexpected: claude.exe died on its own (crash, OOM, user `/exit`)
        new_state, note = self.model.decide_exit_state(code)
        self.set_state(new_state, note=note)
        self._expected_exit = False
        self.detach_session()

    # ──────────────────────────────────────────────────────────────
    # token meter
    # ──────────────────────────────────────────────────────────────
    def _refresh_token_meter(self) -> None:
        """Poll the active claude session's JSONL for the latest usage block
        and update the header badge. Runs every 5 s.

        The file glob (`find_latest_session`) + JSONL read (`read_last_usage`)
        run on a background thread — on a large/active session file this was a
        proven main_thread_stall source at the 5 s tick. The badge itself is
        updated back on the GUI thread via _tokenMeterReady → _apply_token_meter.
        """
        if self.session is None or not self._session_cwd:
            return
        # Coalesce: skip if the previous off-thread read is still in flight.
        if getattr(self, "_token_refreshing", False):
            return
        # Always re-poll for the newest JSONL under this pane's cwd, not the one
        # we first saw — Claude's `/clear` rolls over to a fresh session file.
        # Scope to this pane's Claude config home (per-profile panes write under
        # <CLAUDE_CONFIG_DIR>/projects/, not ~/.claude/projects/).
        cwd = self._session_cwd
        since_ts = self._spawn_ts - 5
        cfg_dir = getattr(self.session, "_claude_config_dir", None)
        self._token_refreshing = True

        def _worker() -> None:
            cand = None
            usage = None
            try:
                cand = find_latest_session(cwd, since_ts=since_ts, config_dir=cfg_dir)
                if cand is not None:
                    try:
                        usage = read_last_usage(cand)
                    except Exception:
                        usage = None
            finally:
                # Hand back to the GUI thread (queued — emitter != receiver thread).
                self._tokenMeterReady.emit(cand, usage)

        threading.Thread(target=_worker, daemon=True, name="token-meter").start()

    def _apply_token_meter(self, cand, usage) -> None:
        """GUI-thread slot: apply an off-thread token-meter read to the badge."""
        self._token_refreshing = False
        # Pane may have been torn down or respawned while the worker ran.
        if self.session is None or cand is None:
            return
        if cand != self._session_jsonl:
            # Roll-over (typically `/clear`): point at the fresh file, clear
            # cached usage, and hide the badge until the new session emits its
            # first assistant turn (otherwise the header keeps the old "128%").
            self._session_jsonl = cand
            self._last_usage = None
            self._token_label.setText("")
            self._token_label.hide()
        if usage is None:
            return
        self._last_usage = usage
        badge = self.model.format_token_badge(usage)
        self._token_label.setText(badge["text"])
        self._token_label.setStyleSheet(f"color: {badge['color']}; font-size: 11px;")
        self._token_label.setToolTip(badge["tooltip"])
        self._token_label.show()

    def current_usage(self) -> dict | None:
        """Return the last-known usage dict for status-bar aggregation, or
        None if this pane has no active session / hasn't logged a turn yet."""
        return self.model.current_usage()

    def mark_expected_exit(self) -> None:
        """Called by orchestrator.close()/done() before terminate so the next
        exit notification isn't treated as a crash."""
        self.model.mark_expected_exit()

    # ──────────────────────────────────────────────────────────────
    # minimise / restore — hides the terminal body so the parent splitter
    # shrinks this pane to header-only height.
    # ──────────────────────────────────────────────────────────────
    def _toggle_minimised(self) -> None:
        self._minimised = not self._minimised
        self._stack.setVisible(not self._minimised)
        self._btn_min.setText("▸" if self._minimised else "▾")
        self._btn_min.setToolTip(
            "Restore pane (expand body)" if self._minimised else "Minimise pane (collapse body)"
        )
        # Tell the parent splitter to give us minimal height when minimised.
        if self._minimised:
            self.setMaximumHeight(self._header_height())
        else:
            self.setMaximumHeight(16777215)  # Qt's QWIDGETSIZE_MAX

    # ──────────────────────────────────────────────────────────────
    # input lock — teammate panes only (Lead is never locked)
    # ──────────────────────────────────────────────────────────────
    def _toggle_input_lock(self) -> None:
        self.set_input_locked(not self._input_locked)

    def set_input_locked(self, locked: bool) -> None:
        """Lock/unlock manual typing into this pane. No-op on user-driven panes
        (Lead / Shell) — their input must stay open."""
        if not self._lockable:
            return
        self._input_locked = bool(locked)
        self._terminal.set_input_locked(self._input_locked)
        self._refresh_lock_button()

    def set_keepalive(self, active: bool) -> None:
        """Forward tab-visibility keep-alive state to the terminal widget so a
        pane in a hidden project tab can release its Chromium compositor RAM.

        Also doubles as the "active tab" signal for auto clear-view: a done
        clear that fired while this pane was on screen is deferred (see
        _on_done_clear_timeout) and flushed here the moment the pane stops
        being the active tab.
        """
        active = bool(active)
        became_inactive = self._keepalive_active and not active
        self._keepalive_active = active
        self._terminal.set_keepalive(active)
        if became_inactive and self._pending_auto_clear:
            self._pending_auto_clear = False
            self._clear_pane_view()

    def _refresh_lock_button(self) -> None:
        if self._btn_lock is None:
            return
        if self._input_locked:
            self._btn_lock.setText("🔒")
            self._btn_lock.setToolTip("Input locked — click to unlock and type into this pane")
        else:
            self._btn_lock.setText("🔓")
            self._btn_lock.setToolTip("Input unlocked — click to lock (block accidental typing)")

    def _header_height(self) -> int:
        # the header is the first child of the root layout
        layout = self.layout()
        if layout is None:
            return 36
        item = layout.itemAt(0)
        w = item.widget() if item else None
        return (w.height() if w else 28) + 6  # +pad for frame border

    # ──────────────────────────────────────────────────────────────
    # font size persistence (per role)
    # ──────────────────────────────────────────────────────────────
    _FONT_SIZE_DEFAULT_KEY = "pane/_default/font_pt"

    def _settings_key(self) -> str:
        return f"pane/{self.role.name}/font_pt"

    def _restore_font_size(self) -> None:
        s = QSettings("agent-takkub", "cockpit")
        v = s.value(self._settings_key())
        if v is None:
            # No size recorded for this exact role yet — fall back to the
            # most recent zoom from any pane, so a newly spawned pane picks
            # up where the user last left the font (Ctrl/Cmd+wheel zoom).
            v = s.value(self._FONT_SIZE_DEFAULT_KEY)
        if v is None:
            return
        try:
            size = int(v)
        except (TypeError, ValueError):
            return
        self._terminal.set_font_point_size(size)

    def _save_font_size(self, size: int) -> None:
        s = QSettings("agent-takkub", "cockpit")
        s.setValue(self._settings_key(), int(size))
        s.setValue(self._FONT_SIZE_DEFAULT_KEY, int(size))

    # ──────────────────────────────────────────────────────────────
    # clear pane view — wipes xterm.js scrollback without touching the
    # live PTY session, then nudges the TUI to redraw its current screen
    # (Ctrl+L) so the pane doesn't sit blank until new output arrives.
    # ──────────────────────────────────────────────────────────────
    def _clear_pane_view(self) -> None:
        self._terminal.clear_view()
        if self.session is not None:
            try:
                self.session.write("\x0c")
            except Exception:
                pass

    def _on_done_clear_timeout(self) -> None:
        """Fired _DONE_AUTO_CLEAR_DELAY_MS after entering "done". Clears the
        view unless the user is currently looking at this pane, in which case
        the clear is deferred until set_keepalive(False) flushes it."""
        if self.state != "done" or self.role.name == LEAD.name:
            return
        if self._keepalive_active:
            self._pending_auto_clear = True
            return
        self._clear_pane_view()

    def _check_idle_auto_clear(self) -> None:
        """Polled every 60s: clear a teammate pane that has been idle (no PTY
        output) for _IDLE_AUTO_CLEAR_THRESHOLD_S while it isn't the active tab."""
        if self.role.name == LEAD.name:
            return
        if self._keepalive_active:
            return
        if self.state == "empty" or self._idle_auto_cleared:
            return
        if self._last_output_ts <= 0:
            return
        if time.time() - self._last_output_ts < _IDLE_AUTO_CLEAR_THRESHOLD_S:
            return
        self._idle_auto_cleared = True
        self._clear_pane_view()

    # ──────────────────────────────────────────────────────────────
    # export current pane buffer to a text file under runtime/exports/
    # ──────────────────────────────────────────────────────────────
    def _export_buffer(self) -> None:
        if self.session is None:
            return
        out_dir = RUNTIME_DIR / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = out_dir / f"{self.role.name}-{stamp}.txt"
        try:
            text = "\n".join(self.session.display_lines()).rstrip()
            path.write_text(text, encoding="utf-8")
            self._note.setText(f"exported → runtime/exports/{path.name}")
            self.last_note = path.name
        except OSError as e:
            self._note.setText(f"export failed: {e}")

    # ──────────────────────────────────────────────────────────────
    # styling
    # ──────────────────────────────────────────────────────────────
    def _stylesheet(self) -> str:
        _css_safe = self.role.name.replace("#", "-")
        return (
            f"#pane_{_css_safe} {{"
            f"  background-color: {cockpit_theme.GROUND_PANEL};"
            f"  border: 1px solid {cockpit_theme.BORDER_STRONG};"
            "  border-radius: 6px;"
            "}"
            "#paneHeader {"
            f"  background-color: {cockpit_theme.GROUND_INPUT};"
            f"  border-bottom: 1px solid {cockpit_theme.BORDER_STRONG};"
            "}"
            "QPushButton {"
            f"  background-color: {cockpit_theme.GROUND_SELECT};"
            f"  color: {cockpit_theme.TEXT_PRIMARY_ALT};"
            f"  border: 1px solid {cockpit_theme.BORDER_STRONG2};"
            "  border-radius: 3px;"
            "  padding: 2px 8px;"
            "  font-size: 11px;"
            "}"
            f"QPushButton:hover {{ background-color: {cockpit_theme.BORDER_STRONG2}; }}"
        )
