"""TerminalWidget: QWebEngineView hosting xterm.js for true terminal fidelity.

This replaces the Iter 1–9 QPlainTextEdit + pyte rendering pipeline. xterm.js
is the same terminal emulator VS Code, Hyper, GitHub Codespaces and Cursor
use; it handles ANSI, alt-screen, mouse modes, IME composition, BiDi text,
and Thai/CJK combining marks natively via the browser layout engine.

Wiring:
  PTY bytes (from PtySession reader thread)
    → terminal_widget.write_bytes(data)
    → page.runJavaScript("termWrite(...)")
    → xterm.js renders

  User keystroke / IME commit (inside xterm.js)
    → bridge.sendInput(data)        [QWebChannel slot]
    → emit inputBytes(bytes)        [Qt signal]
    → AgentPane → Orchestrator → PtySession.write()

  Resize:
    xterm.js FitAddon → bridge.resize(cols, rows)
    → emit resized(cols, rows)      [Qt signal]
    → PtySession.resize() → winpty.setwinsize()
"""

from __future__ import annotations

import json
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QVBoxLayout, QWidget

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_INDEX_URL = QUrl.fromLocalFile(str(_STATIC_DIR / "terminal.html"))


class _Bridge(QObject):
    """Object exposed to JS via QWebChannel."""

    inputData = pyqtSignal(str)  # text the user typed in xterm.js
    sizeChanged = pyqtSignal(int, int)  # cols, rows reported by FitAddon
    pageReady = pyqtSignal()

    @pyqtSlot(str)
    def sendInput(self, data: str) -> None:
        self.inputData.emit(data)

    @pyqtSlot(int, int)
    def resize(self, cols: int, rows: int) -> None:
        self.sizeChanged.emit(cols, rows)

    @pyqtSlot()
    def ready(self) -> None:
        self.pageReady.emit()


class TerminalWidget(QWidget):
    """xterm.js-backed terminal that drops into an AgentPane.

    Public signals match the v0.2.x QPlainTextEdit-based widget so AgentPane
    can stay nearly identical:

      inputBytes(bytes)        — user typed something; forward to PTY
      resized(cols, rows)      — terminal grid size changed; resize PTY
      fontSizeChanged(int)     — for QSettings per-role persistence
    """

    inputBytes = pyqtSignal(bytes)
    resized = pyqtSignal(int, int)
    fontSizeChanged = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._view = QWebEngineView(self)
        layout.addWidget(self._view, 1)

        self._channel = QWebChannel(self)
        self._bridge = _Bridge(self)
        self._channel.registerObject("bridge", self._bridge)
        self._view.page().setWebChannel(self._channel)

        # buffer bytes until xterm.js says it's ready, then flush in order
        self._pending_writes: list[str] = []
        self._page_ready = False

        # Coalesce multiple write_bytes() calls within the same event-loop
        # tick into a single runJavaScript IPC roundtrip. Each PTY chunk
        # used to fire its own runJavaScript — for chatty TUIs that meant
        # dozens of IPC hops per frame and visible cursor jitter.
        self._write_buf: list[str] = []
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(0)
        self._flush_timer.timeout.connect(self._flush_writes)

        self._bridge.inputData.connect(self._on_input_data)
        self._bridge.sizeChanged.connect(self.resized.emit)
        self._bridge.pageReady.connect(self._on_page_ready)

        self._view.load(_INDEX_URL)

    # ------------------------------------------------------------------
    # Python → JS
    # ------------------------------------------------------------------
    def write_bytes(self, data: bytes | str) -> None:
        """Forward PTY output to xterm.js (batched per event-loop tick)."""
        if isinstance(data, bytes):
            text = data.decode("utf-8", "replace")
        else:
            text = data
        if not text:
            return
        if not self._page_ready:
            self._pending_writes.append(text)
            return
        self._write_buf.append(text)
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def _flush_writes(self) -> None:
        if not self._write_buf:
            return
        joined = "".join(self._write_buf)
        self._write_buf.clear()
        self._view.page().runJavaScript(f"termWrite({json.dumps(joined)});")

    def clear(self) -> None:
        if not self._page_ready:
            self._pending_writes.clear()
            return
        self._view.page().runJavaScript("termClear();")

    def set_font_point_size(self, size: int) -> None:
        size = max(7, min(28, int(size)))
        # xterm.js wants pixel size; rough conversion: pt * 1.333 ≈ px
        px = int(size * 1.333)
        self._view.page().runJavaScript(f"termSetFontSize({px});")
        self.fontSizeChanged.emit(size)

    def request_buffer_text(self, callback) -> None:
        """Async fetch the current visible+scrollback buffer as text.
        `callback(str)` is invoked once the JS resolves."""
        self._view.page().runJavaScript("termGetBufferText();", callback)

    def set_idle(self, idle: bool) -> None:
        """Tell xterm.js whether claude is sitting at the ready prompt
        (idle=True) or busy (idle=False). The terminal uses this flag to
        decide whether to local-echo keystrokes for snappier feedback.

        Best-effort: if the page hasn't booted, we drop the call (the
        default JS-side flag is `false` so we err on the safe side).
        """
        if not self._page_ready:
            return
        flag = "true" if idle else "false"
        try:
            self._view.page().runJavaScript(f"termSetIdle({flag});")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------
    def _on_input_data(self, data: str) -> None:
        # xterm.js gives us already-encoded escape sequences for keys; just
        # ship the bytes to the PTY.
        self.inputBytes.emit(data.encode("utf-8"))

    def _on_page_ready(self) -> None:
        self._page_ready = True
        if self._pending_writes:
            joined = "".join(self._pending_writes)
            self._pending_writes.clear()
            self._view.page().runJavaScript(f"termWrite({json.dumps(joined)});")

    def setFocus(self) -> None:
        self._view.setFocus()
