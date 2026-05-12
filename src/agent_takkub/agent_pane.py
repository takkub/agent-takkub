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

import time
from datetime import datetime

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
from .roles import Role
from .terminal_widget import TerminalWidget

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

        self.setObjectName(f"pane_{role.name}")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(self._stylesheet())

        # Tracks whether the next process exit is "expected" (user-triggered
        # close / done / orchestrator.close). If False when exit fires, we
        # treat it as a crash and surface the "exited" state.
        self._expected_exit = False

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

        self._btn_spawn = QPushButton("Spawn", header)
        self._btn_spawn.setFixedHeight(22)
        self._btn_spawn.clicked.connect(lambda: self.spawnRequested.emit(self.role.name))

        self._btn_export = QPushButton("⤓", header)
        self._btn_export.setFixedSize(22, 22)
        self._btn_export.setToolTip("Export pane buffer to .txt")
        self._btn_export.clicked.connect(self._export_buffer)
        self._btn_export.hide()

        self._btn_min = QPushButton("▾", header)
        self._btn_min.setFixedSize(22, 22)
        self._btn_min.setToolTip("Minimise pane (collapse body)")
        self._btn_min.clicked.connect(self._toggle_minimised)

        self._btn_close = QPushButton("×", header)
        self._btn_close.setFixedSize(22, 22)
        self._btn_close.setToolTip("Close pane")
        self._btn_close.clicked.connect(lambda: self.closeRequested.emit(self.role.name))
        self._btn_close.hide()

        hl.addWidget(self._dot)
        hl.addWidget(self._title)
        hl.addWidget(self._note, 1)
        hl.addWidget(self._btn_spawn)
        hl.addWidget(self._btn_export)
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
            self._btn_close.show()
            self._btn_export.show()
        else:
            self._stack.setCurrentIndex(0)
            self._btn_spawn.show()
            self._btn_close.hide()
            self._btn_export.hide()

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

    def attach_session(self, session: PtySession, cwd: str | None = None) -> None:
        """Bind a PtySession to this pane's terminal widget. `cwd` (optional)
        is shown in the header next to the role label."""
        self.session = session
        # xterm.js consumes raw PTY bytes directly (no pyte → rich rebuild)
        session.bytesIn.connect(self._terminal.write_bytes)
        # pyte still parses the bytes in parallel — use that to push the
        # "claude is idle at the ready prompt" signal into the terminal
        # widget so it can decide whether to local-echo keystrokes.
        session.outputUpdated.connect(self._sync_idle_flag)
        session.processExited.connect(self._on_exit)
        self._terminal.resized.connect(session.resize)
        self._update_title_with_cwd(cwd)
        self.set_state("active")
        self._terminal.setFocus()
        # explicit reset — start in busy (no local echo) until pyte
        # confirms we're at the ready prompt
        self._last_idle = None
        self._idle_check_at = 0.0
        self._terminal.set_idle(False)

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
        if self.session is not None:
            try:
                self.session.bytesIn.disconnect(self._terminal.write_bytes)
            except Exception:
                pass
            try:
                self.session.outputUpdated.disconnect(self._sync_idle_flag)
            except Exception:
                pass
            self.session = None
        self._last_idle = None
        self._terminal.clear()

    def _on_exit(self, code: int) -> None:
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
        return (
            f"#pane_{self.role.name} {{"
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
