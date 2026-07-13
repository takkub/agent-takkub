"""Logs panel: tails runtime/events.log and renders recent entries.

Displayed as a collapsible dock at the bottom of the main window. Reads the
log file every second; only re-renders when the file's size has grown.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import cockpit_theme

# Only read the tail of events.log — never the whole file. The panel shows
# the last ~120 lines, so a fixed-size tail is always enough. Reading the
# entire file on the Qt main thread every second wedged the cockpit once the
# log grew to multi-MB (IPC + UI share this thread). 256 KiB >> 120 lines.
_TAIL_BYTES = 256 * 1024


def read_log_tail(path: Path, tail_bytes: int = _TAIL_BYTES) -> str:
    """Return the decoded tail of *path* — at most *tail_bytes* bytes, never the
    whole file. When the file is larger than the window we seek to the end and
    drop the (likely partial) first line. Empty string on any read error.

    Extracted from LogsPanel._poll so the cheap-read behaviour can be tested
    without standing up a QWidget."""
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    try:
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
            raw = f.read()
    except OSError:
        return ""
    if size > tail_bytes:
        nl = raw.find(b"\n")
        if nl != -1:
            raw = raw[nl + 1 :]
    return raw.decode("utf-8", "replace")


_EVENT_COLOR = {
    "spawn": cockpit_theme.STATE_OK_BRIGHT,
    "assign": cockpit_theme.STATE_WARN_BRIGHT,
    "send": cockpit_theme.STATE_INFO_BRIGHT,
    "done": cockpit_theme.CHIP_REMOTE_ON,
    "close": cockpit_theme.STATE_EXITED,
}


class LogsPanel(QWidget):
    """A read-only viewport that tails an events.log file."""

    def __init__(self, log_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._log_path = log_path
        self._last_size = 0
        # filter state
        self._event_filter = "all"
        self._role_filter = ""
        self._search_text = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 4, 8, 8)
        root.setSpacing(4)

        header = QHBoxLayout()
        title = QLabel("events log")
        title.setStyleSheet(
            f"color: {cockpit_theme.TEXT_MUTED}; font-size: 11px; font-weight: bold;"
        )

        self._event_combo = QComboBox()
        self._event_combo.addItems(["all", "spawn", "assign", "send", "done", "close"])
        self._event_combo.setFixedHeight(22)
        self._event_combo.setToolTip("Filter by event type")
        self._event_combo.currentTextChanged.connect(self._on_event_filter)

        self._role_input = QLineEdit()
        self._role_input.setFixedHeight(22)
        self._role_input.setMaximumWidth(120)
        self._role_input.setPlaceholderText("role filter…")
        self._role_input.textChanged.connect(self._on_role_filter)

        self._search_input = QLineEdit()
        self._search_input.setFixedHeight(22)
        self._search_input.setMaximumWidth(180)
        self._search_input.setPlaceholderText("search text…")
        self._search_input.textChanged.connect(self._on_search)

        self._btn_clear = QPushButton("clear")
        self._btn_clear.setFixedHeight(22)
        self._btn_clear.setToolTip("Erase events.log on disk")
        self._btn_clear.clicked.connect(self._on_clear)

        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self._event_combo)
        header.addWidget(self._role_input)
        header.addWidget(self._search_input)
        header.addWidget(self._btn_clear)
        root.addLayout(header)

        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        # Cascadia Mono ships on Windows; on macOS it is absent (costly
        # font-alias scan), so fall back to the native monospace per platform.
        if sys.platform == "win32":
            _mono_family = "Cascadia Mono"
        elif sys.platform == "darwin":
            _mono_family = "Menlo"
        else:
            _mono_family = "monospace"
        f = QFont(_mono_family, 9)
        f.setStyleHint(QFont.StyleHint.Monospace)
        self._view.setFont(f)
        self._view.setStyleSheet(
            "QPlainTextEdit {"
            f"  background-color: {cockpit_theme.GROUND_SIDEBAR};"
            f"  color: {cockpit_theme.TEXT_SECONDARY};"
            f"  border: 1px solid {cockpit_theme.BORDER_STRONG};"
            "  border-radius: 4px;"
            "  padding: 4px;"
            "}"
        )
        root.addWidget(self._view, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(1_000)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

        self._poll()  # initial fill

    # ──────────────────────────────────────────────────────────────
    def _on_event_filter(self, value: str) -> None:
        self._event_filter = value or "all"
        self._last_size = 0  # force re-render
        self._poll()

    def _on_role_filter(self, value: str) -> None:
        self._role_filter = (value or "").strip().lower()
        self._last_size = 0
        self._poll()

    def _on_search(self, value: str) -> None:
        self._search_text = (value or "").strip().lower()
        self._last_size = 0
        self._poll()

    def _poll(self) -> None:
        if not self._log_path.exists():
            return
        try:
            size = self._log_path.stat().st_size
        except OSError:
            return
        if size == self._last_size:
            return
        self._last_size = size
        text = read_log_tail(self._log_path)

        # show the last ~120 lines so we don't blow up memory
        lines = text.splitlines()[-120:]
        rendered: list[str] = []
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                rendered.append(line)
                continue
            ts = str(row.get("ts") or "")
            ev = str(row.get("event") or "?")
            role = str(row.get("role") or row.get("to") or "")
            note = row.get("note") or row.get("task_preview") or row.get("msg_preview") or ""
            note = str(note)
            cwd = str(row.get("cwd") or "")

            # apply filters
            if self._event_filter != "all" and ev != self._event_filter:
                continue
            if self._role_filter and self._role_filter not in str(role).lower():
                continue

            line_str = f"{ts}  {ev:7s}  {role:10s}  {note}"
            if cwd and ev == "spawn":
                line_str += f"   ({cwd})"

            # text search across the whole rendered line (case-insensitive)
            if self._search_text and self._search_text not in line_str.lower():
                continue

            rendered.append(line_str)

        self._view.setPlainText("\n".join(rendered))
        sb = self._view.verticalScrollBar()
        if sb is not None:
            sb.setValue(sb.maximum())

    def _on_clear(self) -> None:
        try:
            self._log_path.write_text("", encoding="utf-8")
            self._last_size = 0
            self._view.clear()
        except OSError:
            pass
