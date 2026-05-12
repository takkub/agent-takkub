"""TerminalWidget: renders a pyte screen with ANSI colours, forwards keys.

Iter 3: full ANSI colour rendering via QTextCharFormat. Background/foreground,
bold, italic, underline, and reverse are honoured. Builds runs of identically
styled text from pyte's per-cell attributes (see PtySession.display_rich) so
we don't pay one format-object per character.

Input handling:
  - keyPressEvent: standard keys to bytes, function keys to escape sequences
  - inputMethodEvent: IME composition commits (Thai, CJK) to bytes
  - wheelEvent: forwarded as PgUp/PgDn so the user can scroll claude's
    internal alt-screen history.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QInputMethodEvent,
    QKeyEvent,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
)
from PyQt6.QtWidgets import QPlainTextEdit, QWidget

_KEY_MAP = {
    Qt.Key.Key_Enter: b"\r",
    Qt.Key.Key_Return: b"\r",
    Qt.Key.Key_Backspace: b"\x7f",
    Qt.Key.Key_Tab: b"\t",
    Qt.Key.Key_Escape: b"\x1b",
    Qt.Key.Key_Up: b"\x1b[A",
    Qt.Key.Key_Down: b"\x1b[B",
    Qt.Key.Key_Right: b"\x1b[C",
    Qt.Key.Key_Left: b"\x1b[D",
    Qt.Key.Key_Home: b"\x1b[H",
    Qt.Key.Key_End: b"\x1b[F",
    Qt.Key.Key_PageUp: b"\x1b[5~",
    Qt.Key.Key_PageDown: b"\x1b[6~",
    Qt.Key.Key_Delete: b"\x1b[3~",
    Qt.Key.Key_F1: b"\x1bOP",
    Qt.Key.Key_F2: b"\x1bOQ",
    Qt.Key.Key_F3: b"\x1bOR",
    Qt.Key.Key_F4: b"\x1bOS",
}

# ──────────────────────────────────────────────────────────────────────
# pyte → Qt colour translation
# ──────────────────────────────────────────────────────────────────────

DEFAULT_FG = QColor("#e6e6e6")
DEFAULT_BG = QColor("#0e0e10")

# Standard 16-colour ANSI palette (named + bright variants). pyte uses these
# strings literally in Char.fg / Char.bg.
_PALETTE: dict[str, QColor] = {
    "black": QColor("#1c1c20"),
    "red": QColor("#ef4444"),
    "green": QColor("#22c55e"),
    "yellow": QColor("#facc15"),
    "blue": QColor("#3b82f6"),
    "magenta": QColor("#a855f7"),
    "cyan": QColor("#22d3ee"),
    "white": QColor("#e6e6e6"),
    "brightblack": QColor("#52525b"),
    "brightred": QColor("#f87171"),
    "brightgreen": QColor("#4ade80"),
    "brightyellow": QColor("#fde047"),
    "brightblue": QColor("#60a5fa"),
    "brightmagenta": QColor("#c084fc"),
    "brightcyan": QColor("#67e8f9"),
    "brightwhite": QColor("#fafafa"),
}


def _resolve_color(name: str, default: QColor) -> QColor:
    if not name or name == "default":
        return default
    if name in _PALETTE:
        return _PALETTE[name]
    # 6-char hex (24-bit truecolor) — pyte stores it without the leading '#'
    if len(name) == 6:
        c = QColor("#" + name)
        if c.isValid():
            return c
    c = QColor(name)
    return c if c.isValid() else default


class TerminalWidget(QPlainTextEdit):
    """Read-only viewport over a pyte screen with key/IME input passthrough."""

    inputBytes = pyqtSignal(bytes)
    resized = pyqtSignal(int, int)  # cols, rows  (when user resizes window)
    fontSizeChanged = pyqtSignal(int)  # for per-pane size persistence

    # Updated by AgentPane whenever the screen is refreshed; lets us forward
    # mouse-wheel events as proper SGR mouse events when claude has mouse
    # tracking on (`\x1b[?1006h`) and fall back to PgUp/PgDn otherwise.
    mouse_tracking_on: bool = False

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(False)  # accept IME composition + focus
        self.setUndoRedoEnabled(False)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabChangesFocus(False)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)

        font = self._pick_font()
        self.setFont(font)
        self.setStyleSheet(
            "QPlainTextEdit {"
            "  background-color: #0e0e10;"
            "  color: #e6e6e6;"
            "  selection-background-color: #3b82f6;"
            "  border: none;"
            "  padding: 6px;"
            "}"
        )

        # debounced refresh — 33ms = 30fps, easier on typing-induced storms
        self._pending_rich: list | None = None
        self._last_rendered_rich: list | None = None
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(33)
        self._refresh_timer.timeout.connect(self._flush_rich)

        # cache of QTextCharFormat keyed by (fg, bg, bold, italic, ul, rev)
        self._fmt_cache: dict[tuple, QTextCharFormat] = {}

        # block format (line height) reused for every paragraph
        self._block_fmt = QTextBlockFormat()
        self._block_fmt.setLineHeight(
            135, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value
        )

    # ──────────────────────────────────────────────────────────────
    def _pick_font(self) -> QFont:
        f = QFont()
        f.setFamilies(
            [
                "Cascadia Mono",
                "Consolas",
                "Courier New",
                "Leelawadee UI",
                "Tahoma",
                "Microsoft Sans Serif",
            ]
        )
        f.setPointSize(11)
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setFixedPitch(True)
        return f

    # ──────────────────────────────────────────────────────────────
    # output rendering
    # ──────────────────────────────────────────────────────────────
    def set_screen_rich(
        self,
        rows: list[list[tuple[str, str, str, bool, bool, bool, bool]]],
    ) -> None:
        """Schedule a redraw with the given rich screen rows."""
        self._pending_rich = rows
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _format_for(
        self,
        fg: str,
        bg: str,
        bold: bool,
        italic: bool,
        underline: bool,
        reverse: bool,
    ) -> QTextCharFormat:
        key = (fg, bg, bold, italic, underline, reverse)
        fmt = self._fmt_cache.get(key)
        if fmt is not None:
            return fmt

        fg_color = _resolve_color(fg, DEFAULT_FG)
        bg_color = _resolve_color(bg, DEFAULT_BG)
        if reverse:
            fg_color, bg_color = bg_color, fg_color

        fmt = QTextCharFormat()

        # Set the families list directly on the format instead of building a
        # whole QFont. Using setFont(QFont(widget.font())) collapses the
        # families fallback chain in some Qt builds, so Thai/CJK chars stop
        # falling back to Tahoma/Leelawadee and combining marks disappear.
        # setFontFamilies preserves the per-glyph fallback.
        fmt.setFontFamilies(self.font().families())
        if bold:
            fmt.setFontWeight(QFont.Weight.Bold.value)
        if italic:
            fmt.setFontItalic(True)
        if underline:
            fmt.setFontUnderline(True)

        fmt.setForeground(fg_color)
        # Only paint background if it differs from the widget bg — keeps the
        # rendering cheaper and avoids cell-grid artefacts on bigger spans.
        if bg_color != DEFAULT_BG or reverse:
            fmt.setBackground(bg_color)
        self._fmt_cache[key] = fmt
        return fmt

    def _flush_rich(self) -> None:
        if self._pending_rich is None:
            return
        rows = self._pending_rich
        self._pending_rich = None

        # Skip rebuild if nothing actually changed. pyte's outputUpdated
        # fires for every byte chunk including mouse-mode toggles and
        # cursor-save sequences that don't visibly mutate the screen.
        # Without this check, every keystroke pays a ~360-insertText doc
        # rebuild and the typing feels stuttery.
        if rows == self._last_rendered_rich:
            return
        self._last_rendered_rich = rows

        # Only auto-scroll to the bottom if the user wasn't already
        # scrolled up (so manual scroll-back during a long output isn't
        # yanked away by the next refresh).
        sb = self.verticalScrollBar()
        at_bottom = sb is None or sb.value() >= sb.maximum() - 4

        # Rebuild the document. setUpdatesEnabled(False) avoids partial
        # repaints while we churn through the cursor.
        self.setUpdatesEnabled(False)
        try:
            doc = self.document()
            doc.clear()
            cursor = QTextCursor(doc)
            cursor.setBlockFormat(self._block_fmt)

            for y, runs in enumerate(rows):
                if y > 0:
                    cursor.insertBlock(self._block_fmt)
                for text, fg, bg, bold, italic, underline, reverse in runs:
                    if not text:
                        continue
                    fmt = self._format_for(fg, bg, bold, italic, underline, reverse)
                    cursor.insertText(text, fmt)
        finally:
            self.setUpdatesEnabled(True)

        if at_bottom and sb is not None:
            sb.setValue(sb.maximum())

    # ──────────────────────────────────────────────────────────────
    # input handling
    # ──────────────────────────────────────────────────────────────
    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        mods = event.modifiers()
        text = event.text()

        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)

        # Ctrl + (+|=|-) → adjust font size. Reserved before forwarding to PTY.
        if (
            ctrl
            and not alt
            and key
            in (
                Qt.Key.Key_Plus,
                Qt.Key.Key_Equal,
                Qt.Key.Key_Minus,
                Qt.Key.Key_0,
            )
        ):
            self._adjust_font(key)
            return

        if ctrl and not alt:
            if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
                ctrl_byte = bytes([key - Qt.Key.Key_A + 1])
                self.inputBytes.emit(ctrl_byte)
                return

        if key in _KEY_MAP:
            self.inputBytes.emit(_KEY_MAP[key])
            return

        if text:
            self.inputBytes.emit(text.encode("utf-8"))
            return

    def _adjust_font(self, key: int) -> None:
        f = self.font()
        size = f.pointSize() if f.pointSize() > 0 else 11
        if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            size = min(28, size + 1)
        elif key == Qt.Key.Key_Minus:
            size = max(7, size - 1)
        elif key == Qt.Key.Key_0:
            size = 11  # reset
        self.set_font_point_size(size)

    def set_font_point_size(self, size: int) -> None:
        """Set terminal font size + emit fontSizeChanged so AgentPane can
        persist the choice per role."""
        f = self.font()
        size = max(7, min(28, int(size)))
        if f.pointSize() == size:
            return
        f.setPointSize(size)
        self.setFont(f)
        # invalidate format cache + last-render cache so the next flush
        # rebuilds with the new font metrics
        self._fmt_cache.clear()
        self._last_rendered_rich = None
        # recompute the pty grid for the new font metrics and tell the session
        fm = QFontMetrics(self.font())
        char_w = max(1, fm.horizontalAdvance("M"))
        char_h = max(1, fm.lineSpacing())
        cols = max(40, (self.viewport().width() - 12) // char_w)
        rows = max(10, (self.viewport().height() - 12) // char_h)
        self.resized.emit(cols, rows)
        self.fontSizeChanged.emit(size)

    def inputMethodEvent(self, event: QInputMethodEvent) -> None:
        """Forward IME commit text (Thai, CJK, etc.) to the PTY."""
        commit = event.commitString()
        if commit:
            self.inputBytes.emit(commit.encode("utf-8"))
        event.accept()

    def wheelEvent(self, event) -> None:
        """Forward mouse wheel to claude.

        Two strategies:
          1. If claude has SGR mouse tracking enabled (most modern TUIs do),
             send a proper wheel-button-press SGR event so claude scrolls its
             own internal buffer with smooth granularity.
          2. Otherwise, fall back to PgUp/PgDn so the user can still page
             through claude's history.
        """
        delta = event.angleDelta().y()
        if delta == 0:
            return
        ticks = max(1, abs(delta) // 120)

        if self.mouse_tracking_on:
            # SGR mouse format: ESC [ < button ; col ; row M
            # button 64 = wheel up, 65 = wheel down (X10 wheel-as-buttons)
            button = 64 if delta > 0 else 65
            # use a sensible coordinate near the cursor — claude doesn't
            # really care for wheel events, but the field is mandatory
            seq = f"\x1b[<{button};1;1M".encode()
            self.inputBytes.emit(seq * ticks)
        else:
            seq = _KEY_MAP[Qt.Key.Key_PageUp] if delta > 0 else _KEY_MAP[Qt.Key.Key_PageDown]
            self.inputBytes.emit(seq * ticks)
        event.accept()

    # ──────────────────────────────────────────────────────────────
    # size reporting (so the orchestrator can resize the PTY)
    # ──────────────────────────────────────────────────────────────
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        fm = QFontMetrics(self.font())
        char_w = max(1, fm.horizontalAdvance("M"))
        char_h = max(1, fm.lineSpacing())
        cols = max(40, (self.viewport().width() - 12) // char_w)
        rows = max(10, (self.viewport().height() - 12) // char_h)
        self.resized.emit(cols, rows)
