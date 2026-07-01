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

from .config import RUNTIME_DIR
from .pty_session import PtySession
from .roles import LEAD, USER_DRIVEN_ROLES, Role
from .terminal_widget import TerminalWidget
from .token_meter import (
    effective_context_limit,
    find_latest_session,
    format_tokens,
    read_last_usage,
    usage_color,
)

STATUS_COLORS = {
    "empty": "#3f3f46",
    "active": "#22c55e",
    "working": "#facc15",
    "done": "#0ea5e9",
    "exited": "#f97316",  # orange — unexpected exit, can respawn
    "error": "#ef4444",
}

SPINNER_FRAMES = "◐◓◑◒"


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
        self.state: str = "empty"
        self.last_note: str | None = None
        self.session: PtySession | None = None

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

        # Token-meter bookkeeping. The pane locks on to the first JSONL
        # session file that appears under the encoded project dir after
        # spawn — if two panes share a cwd, the second pane locks on to
        # whichever new file claude opens for it. Once locked, the path
        # is held until the pane detaches.
        self._spawn_ts: float = 0.0
        self._session_cwd: str | None = None
        self._session_jsonl = None  # type: object | None
        self._last_usage: dict | None = None
        # Known context cap for the token badge. Teammates use per-model limits
        # (200k). The Lead inherits the user's default model — on a Max plan the
        # 1M-context variant — but the JSONL stamps the bare model name with no
        # `[1m]` suffix so the limit can't be read back from it. Pin 1M for a
        # Max Lead; the runtime guard in _refresh_token_meter self-heals a wrong
        # tier guess once usage exceeds the base. (None = derive per-model.)
        self._context_limit: int | None = None
        if role.name == LEAD.name:
            from .plan_tier import is_pro

            self._context_limit = None if is_pro() else 1_000_000
        self._token_timer = QTimer(self)
        self._token_timer.setInterval(5_000)
        self._token_timer.timeout.connect(self._refresh_token_meter)
        # Apply off-thread token-meter reads back on the main (GUI) thread.
        self._tokenMeterReady.connect(self._apply_token_meter)
        self._token_refreshing = False
        # Wall-clock timestamp of the most recent byte received from the
        # PTY. The orchestrator's stuck-pane watchdog reads this to
        # decide whether a "working" pane is silently hung (no output
        # for STUCK_THRESHOLD_S → auto-recover via close + respawn with
        # --continue).
        self._last_output_ts: float = 0.0

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

        # Monotonically increasing byte counter used by the orchestrator's
        # throughput watchdog to detect runaway-output panes.
        self._tp_total_bytes: int = 0

        # Qt CSS ID selectors use "#id" syntax; a "#" inside the name itself
        # breaks the parser (e.g. "pane_qa#1" → "#pane_qa#1" is invalid CSS).
        # Sanitise by replacing "#" with "-" for the objectName/stylesheet while
        # keeping role.name intact for signal identity and registry routing.
        _css_safe = role.name.replace("#", "-")
        self.setObjectName(f"pane_{_css_safe}")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(self._stylesheet())

        # Tracks whether the next process exit is "expected" (user-triggered
        # close / done / orchestrator.close). If False when exit fires, we
        # treat it as a crash and surface the "exited" state.
        self._expected_exit = False

        # Monotonically incremented each time a new PtySession is attached.
        # Captured inside the processExited lambda so stale exit signals from
        # an old session (emitted after a replacement is already attached) are
        # dropped rather than mutating the new session's state.
        self._session_generation: int = 0

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
        self._title.setStyleSheet(f"color: {role.color};")

        self._note = QLabel("", header)
        self._note.setStyleSheet("color: #9ca3af; font-size: 11px;")

        # Token-usage badge: shows "52k/200k" of the current claude session
        # for this pane, refreshed on a slow timer. Hidden until the pane has
        # an active session with at least one assistant turn on disk.
        self._token_label = QLabel("", header)
        self._token_label.setStyleSheet("color: #6b7280; font-size: 11px;")
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
        msg.setStyleSheet("color: #525252; font-size: 13px;")
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
        self._dot.setStyleSheet(f"color: {STATUS_COLORS.get(state, '#3f3f46')}; font-size: 14px;")
        if state in ("active", "working"):
            self._stack.setCurrentIndex(1)
            self._btn_spawn.hide()
            self._btn_export.show()
        else:
            self._stack.setCurrentIndex(0)
            self._btn_spawn.show()
            self._btn_export.hide()
        # × is now always visible (see _btn_close init comment) — user
        # always has an escape hatch regardless of pane state.
        self._btn_close.show()

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

    def attach_session(self, session: PtySession, cwd: str | None = None) -> None:
        """Bind a PtySession to this pane's terminal widget. `cwd` (optional)
        is shown in the header next to the role label."""
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
            idle = self.session.is_at_ready_prompt()
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

    def _update_title_with_cwd(self, cwd: str | None) -> None:
        if not cwd:
            self._title.setText(self.role.label)
            return
        # show role + tail of the path (basename) so user knows where the
        # agent is working. eg.  Frontend · pms-web
        import os

        tail = os.path.basename(cwd.rstrip("/\\")) or cwd
        self._title.setText(f"{self.role.label} · {tail}")

    def detach_session(self) -> None:
        # Flush any pending render bytes before tearing down so no output is
        # silently dropped when the session ends (e.g. final "done" message).
        self._flush_render_buf()
        self._render_timer.stop()
        if self.session is not None:
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
        if self.state == "done" or self._expected_exit:
            self.set_state("empty", note=None)
        else:
            # show 'exited' state so the user can click Spawn to retry
            note = f"claude exited unexpectedly (code {code})"
            self.set_state("exited", note=note)
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
        prompt = usage["prompt"]
        limit = effective_context_limit(usage["model"], prompt, base=self._context_limit)
        pct = (prompt / limit) if limit else 0.0
        color = usage_color(pct)
        text = f"{format_tokens(prompt)}/{format_tokens(limit)} · {int(pct * 100)}%"
        self._token_label.setText(text)
        self._token_label.setStyleSheet(f"color: {color}; font-size: 11px;")
        self._token_label.setToolTip(
            f"model: {usage['model']}\n"
            f"prompt: {usage['prompt']:,} tokens  (input {usage['input']:,} + "
            f"cache write {usage['cache_creation']:,} + cache read {usage['cache_read']:,})\n"
            f"output: {usage['output']:,} tokens\n"
            f"context limit: {limit:,}"
        )
        self._token_label.show()

    def current_usage(self) -> dict | None:
        """Return the last-known usage dict for status-bar aggregation, or
        None if this pane has no active session / hasn't logged a turn yet."""
        if self.session is None:
            return None
        return self._last_usage

    def mark_expected_exit(self) -> None:
        """Called by orchestrator.close()/done() before terminate so the next
        exit notification isn't treated as a crash."""
        self._expected_exit = True

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
        pane in a hidden project tab can release its Chromium compositor RAM."""
        self._terminal.set_keepalive(active)

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
    def _settings_key(self) -> str:
        return f"pane/{self.role.name}/font_pt"

    def _restore_font_size(self) -> None:
        s = QSettings("agent-takkub", "cockpit")
        v = s.value(self._settings_key())
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
            "  background-color: #18181b;"
            "  border: 1px solid #27272a;"
            "  border-radius: 6px;"
            "}"
            "#paneHeader {"
            "  background-color: #1c1c20;"
            "  border-bottom: 1px solid #27272a;"
            "}"
            "QPushButton {"
            "  background-color: #27272a;"
            "  color: #e5e7eb;"
            "  border: 1px solid #3f3f46;"
            "  border-radius: 3px;"
            "  padding: 2px 8px;"
            "  font-size: 11px;"
            "}"
            "QPushButton:hover { background-color: #3f3f46; }"
        )
