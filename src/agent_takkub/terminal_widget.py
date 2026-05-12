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

        # debounced refresh
        self._pending_rich: list | None = None
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(16)
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

        # CRITICAL: copy the widget's QFont as the base so its setFamilies()
        # fallback chain (Cascadia → Consolas → Tahoma → Leelawadee) is
        # preserved. Calling setFontWeight/Italic/Underline directly on the
        # format would collapse the chain to the first family only, and Thai
        # diacritics (◌ิ ◌ี ◌่ ◌้ ◌์ ฯลฯ) silently disappear because
        # Cascadia has no Thai glyphs.
        font = QFont(self.font())
        if bold:
            font.setBold(True)
        if italic:
            font.setItalic(True)
        if underline:
            font.setUnderline(True)
        fmt.setFont(font)

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

        sb = self.verticalScrollBar()
        if sb is not None:
            sb.setValue(sb.maximum())

    # ──────────────────────────────────────────────────────────────
    # input handling
    # ──────────────────────────────────────────────────────────────
    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        mods = event.modifiers()
        text = event.text()

        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)

        # Ctrl + (+|=|-) → adjust font size. Reserved before forwarding to PTY.
        if ctrl and not alt and key in (
            Qt.Key.Key_Plus,
            Qt.Key.Key_Equal,
            Qt.Key.Key_Minus,
            Qt.Key.Key_0,
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
        # invalidate format cache — old QTextCharFormats hold the old font
        self._fmt_cache.clear()
        # recompute the pty grid for the new font metrics and tell the session
        fm = QFontMetrics(self.font())
        char_w = max(1, fm.horizontalAdvance("M"))
        char_h = max(1, fm.lineSpacing())
        cols = max(40, (self.viewport().width() - 12) // char_w)
        rows = max(10, (self.viewport().height() - 12) // char_h)
        self.resized.emit(cols, rows)
        self.fontSizeChanged.emit(size)

    def inputMethodEvent(self, event: QInputMethodEvent) -> None:  # noqa: N802
        """Forward IME commit text (Thai, CJK, etc.) to the PTY."""
        commit = event.commitString()
        if commit:
            self.inputBytes.emit(commit.encode("utf-8"))
        event.accept()

    def wheelEvent(self, event) -> None:  # noqa: N802
        """Forward mouse wheel as PgUp/PgDn so the user can scroll claude's
        internal alt-screen history (pyte's scrollback won't help — claude
        runs in alt-screen and owns its own buffer)."""
        delta = event.angleDelta().y()
        if delta == 0:
            return
        ticks = max(1, abs(delta) // 120)
        seq = (
            _KEY_MAP[Qt.Key.Key_PageUp]
            if delta > 0
            else _KEY_MAP[Qt.Key.Key_PageDown]
        )
        self.inputBytes.emit(seq * ticks)
        event.accept()

    # ──────────────────────────────────────────────────────────────
    # size reporting (so the orchestrator can resize the PTY)
    # ──────────────────────────────────────────────────────────────
    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        fm = QFontMetrics(self.font())
        char_w = max(1, fm.horizontalAdvance("M"))
        char_h = max(1, fm.lineSpacing())
        cols = max(40, (self.viewport().width() - 12) // char_w)
        rows = max(10, (self.viewport().height() - 12) // char_h)
        self.resized.emit(cols, rows)
