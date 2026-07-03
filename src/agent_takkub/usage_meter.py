"""UsageMeter — the little Claude-usage readout that lives in the pane-tabs corner.

Replaces the old bare QLabel ("3:45 52% / 2:12 18%") with a friendlier widget:
a small Claude "spark" that gently breathes (fade + scale pulse) next to a
compact 5h / 7d usage line. The spark + text share one colour that tracks the
peak utilisation window (Claude-coral when calm → amber → red), so a glance at
the corner tells you how close you are to a rate-limit reset.

Pure-leaf UI: no orchestrator/app/cli imports. The parent (LimitPanelMixin)
computes the text + colour from the usage payload and calls `apply()` /
`set_offline()`; this widget only owns the drawing + animation.

**Import constraint:** this module MUST NOT import ``app`` or ``cli``.
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, Qt, QTimer
from PyQt6.QtGui import QBrush, QColor, QPainter, QPolygonF
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QWidget

# Claude's brand coral — the spark's calm/default colour so the corner always
# reads as "a little Claude", not a grey system chip.
_CLAUDE_CORAL = "#d97757"


class _Spark(QWidget):
    """A 4-point Claude sparkle that breathes (opacity + scale) on a timer.

    Drawn with QPainter (no image asset → scales crisply at any DPI and never
    ships a binary). The pulse is a slow sine so it reads as "alive / tracking"
    without being distracting in the corner of the eye.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(18, 18)
        self._color = QColor(_CLAUDE_CORAL)
        self._phase = 0.0  # 0..2π, advanced by the timer
        # ~25 fps is plenty for a gentle breathe and stays cheap for an 18px
        # widget. Advance a phase counter rather than reading the clock so the
        # animation is deterministic and needs no wall-time source.
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_color(self, color: str) -> None:
        c = QColor(color)
        if c != self._color:
            self._color = c
            self.update()

    def _tick(self) -> None:
        # 2π over ~1.6 s (40 steps) → one calm breath per ~1.6 s.
        self._phase = (self._phase + (2 * math.pi / 40)) % (2 * math.pi)
        self.update()

    def paintEvent(self, _ev) -> None:
        # Breathe: opacity 0.5↔1.0 and scale 0.82↔1.0, in sync off the sine.
        wave = (math.sin(self._phase) + 1) / 2  # 0..1
        opacity = 0.5 + 0.5 * wave
        scale = 0.82 + 0.18 * wave

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(opacity)
        cx, cy = self.width() / 2, self.height() / 2
        r = (self.width() / 2 - 1) * scale
        inner = r * 0.34  # waist of the sparkle → concave 4-point star

        # 8 vertices: outer tip, inner waist, outer tip, … around the circle.
        pts = []
        for i in range(8):
            ang = math.pi / 2 * (i / 2)  # 0, 45, 90, … degrees in radians
            rad = r if i % 2 == 0 else inner
            pts.append(
                (cx + rad * math.cos(ang - math.pi / 2), cy + rad * math.sin(ang - math.pi / 2))
            )
        poly = QPolygonF([QPointF(x, y) for x, y in pts])
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(self._color))
        p.drawPolygon(poly)
        p.end()


class UsageMeter(QWidget):
    """Spark + compact usage line, mounted in the active tab's corner."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.setSpacing(5)
        self._spark = _Spark(self)
        self._label = QLabel("—", self)
        self._label.setStyleSheet("QLabel { color:#71717a; font-size:11px; }")
        lay.addWidget(self._spark)
        lay.addWidget(self._label)

    def apply(self, text: str, color: str) -> None:
        """Show `text` (e.g. '5h 52% · 7d 18%') tinted `color`; spark matches."""
        self._spark.set_color(color)
        self._label.setText(text)
        self._label.setStyleSheet(f"QLabel {{ color:{color}; font-size:11px; }}")

    def set_offline(self) -> None:
        """Dim state when usage is unavailable (offline / not logged in)."""
        self._spark.set_color("#52525b")
        self._label.setText("—")
        self._label.setStyleSheet("QLabel { color:#52525b; font-size:11px; }")
