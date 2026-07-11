"""TutorialOverlay — first-run guided tour.

A dark translucent scrim over the whole window with a rounded "spotlight" hole
cut over one widget at a time, plus a callout card (title + body + Skip/Next).
Walks a new user through "how to start": add a project → talk to the Lead →
the key status-bar chips → wrap up with End Session.

Trigger: auto on first launch (persisted via a flag under RUNTIME_DIR so it
fires once per install) and replayable any time from the status-bar ❓ Tour
button. Everything is painted with QPainter — no image assets, no web view.

Pure-leaf UI: imports only config (for the flag path). MUST NOT import app/cli.
"""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import QEvent, QPoint, QRect, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import cockpit_theme
from .config import RUNTIME_DIR

_CLAUDE_CORAL = cockpit_theme.METER_CLAY


def _seen_flag_path():
    return RUNTIME_DIR / "tutorial-seen.flag"


def has_seen_tutorial() -> bool:
    """True once the tour has been completed or skipped on this install."""
    try:
        return _seen_flag_path().exists()
    except OSError:
        return False


def mark_tutorial_seen() -> None:
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        _seen_flag_path().write_text("1", encoding="utf-8")
    except OSError:
        pass


class TutorialStep:
    """One tour stop. `target` is resolved lazily (returns the widget to spotlight
    or None to just center the callout) so panes that don't exist yet are fine."""

    def __init__(self, target: Callable[[], QWidget | None], title: str, body: str) -> None:
        self.target = target
        self.title = title
        self.body = body


class TutorialOverlay(QWidget):
    """Full-window scrim + spotlight + callout. One instance per run of the tour."""

    def __init__(self, host: QWidget, steps: list[TutorialStep]) -> None:
        super().__init__(host)
        self._host = host
        self._steps = steps
        self._idx = 0
        self._target_rect = QRect()

        # ── callout card (real child → real clickable buttons) ──────────
        self._callout = QFrame(self)
        self._callout.setObjectName("tutorialCallout")
        self._callout.setFixedWidth(360)
        self._callout.setStyleSheet(
            "#tutorialCallout {"
            f" background:{cockpit_theme.GROUND_PANEL}; "
            f"border:1px solid {cockpit_theme.BORDER_STRONG2};"
            f" border-left:3px solid {_CLAUDE_CORAL}; "
            f"border-radius:{cockpit_theme.RADIUS_MD}px; }}"
        )
        cl = QVBoxLayout(self._callout)
        cl.setContentsMargins(16, 14, 16, 12)
        cl.setSpacing(8)

        self._title_lbl = QLabel(self._callout)
        self._title_lbl.setStyleSheet(f"color:{_CLAUDE_CORAL}; font-size:14px; font-weight:700;")
        self._body_lbl = QLabel(self._callout)
        self._body_lbl.setWordWrap(True)
        self._body_lbl.setStyleSheet(f"color:{cockpit_theme.TEXT_SECONDARY}; font-size:12px;")
        cl.addWidget(self._title_lbl)
        cl.addWidget(self._body_lbl)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._step_lbl = QLabel(self._callout)
        self._step_lbl.setStyleSheet(f"color:{cockpit_theme.TEXT_MUTED}; font-size:11px;")
        self._skip_btn = QPushButton("ข้าม", self._callout)
        self._skip_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._skip_btn.setStyleSheet(
            f"QPushButton {{ color:{cockpit_theme.TEXT_MUTED}; background:transparent;"
            f" border:1px solid {cockpit_theme.BORDER_STRONG}; border-radius:6px;"
            " padding:4px 12px; }"
            f"QPushButton:hover {{ background:{cockpit_theme.GROUND_SELECT}; }}"
        )
        # gold_button() sets objectName #goldButton, whose QSS lives in
        # cockpit_theme.build_stylesheet() — the overlay isn't a descendant
        # of a widget that applies that stylesheet, so the same gold-CTA
        # rule is also set directly here (matching the design system's
        # tokens: GOLD_GRAD_TOP/BOTTOM, GOLD_TEXT_ON, RADIUS_SM) instead of
        # the previous METER_CLAY + literal 6px radius that had drifted off
        # the design system's primary-CTA styling.
        self._next_btn = cockpit_theme.gold_button("ถัดไป →", self._callout)
        self._next_btn.setStyleSheet(
            "QPushButton#goldButton {"
            " background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
            f" stop:0 {cockpit_theme.GOLD_GRAD_TOP}, stop:1 {cockpit_theme.GOLD_GRAD_BOTTOM});"
            f" color:{cockpit_theme.GOLD_TEXT_ON}; border:none;"
            f" border-radius:{cockpit_theme.RADIUS_SM}px; padding:4px 14px; font-weight:700; }}"
            "QPushButton#goldButton:hover { background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
            f" stop:0 #f2cd75, stop:1 {cockpit_theme.GOLD_GRAD_TOP}); }}"
        )
        self._skip_btn.clicked.connect(lambda: self.finish(mark=True))
        self._next_btn.clicked.connect(self._advance)
        row.addWidget(self._step_lbl)
        row.addStretch(1)
        row.addWidget(self._skip_btn)
        row.addWidget(self._next_btn)
        cl.addLayout(row)

        # Reposition when the window resizes/moves so the spotlight stays glued.
        self._host.installEventFilter(self)

    # ── lifecycle ──────────────────────────────────────────────────────
    def start(self) -> None:
        if not self._steps:
            self.finish(mark=True)
            return
        self.setGeometry(self._host.rect())
        self._idx = 0
        self._show_step()
        self.show()
        self.raise_()
        self.setFocus()

    def finish(self, mark: bool) -> None:
        if mark:
            mark_tutorial_seen()
        try:
            self._host.removeEventFilter(self)
        except (RuntimeError, TypeError):
            pass
        self.hide()
        self.deleteLater()

    # ── step rendering ─────────────────────────────────────────────────
    def _show_step(self) -> None:
        step = self._steps[self._idx]
        widget = None
        try:
            widget = step.target()
        except (RuntimeError, AttributeError):
            widget = None
        if widget is not None and widget.isVisible():
            top_left = widget.mapTo(self._host, QPoint(0, 0))
            self._target_rect = QRect(top_left, widget.size())
        else:
            self._target_rect = QRect()  # no spotlight → callout is centered

        self._title_lbl.setText(step.title)
        self._body_lbl.setText(step.body)
        self._step_lbl.setText(f"{self._idx + 1} / {len(self._steps)}")
        last = self._idx == len(self._steps) - 1
        self._next_btn.setText("เสร็จ ✓" if last else "ถัดไป →")
        self._reposition_callout()
        self.update()

    def _advance(self) -> None:
        if self._idx >= len(self._steps) - 1:
            self.finish(mark=True)
            return
        self._idx += 1
        self._show_step()

    def _reposition_callout(self) -> None:
        self._callout.adjustSize()
        cw = self._callout.width()
        ch = self._callout.height()
        margin = 16
        bounds = self.rect()

        if self._target_rect.isNull():
            # No spotlight → center the card.
            x = (bounds.width() - cw) // 2
            y = (bounds.height() - ch) // 2
        else:
            spot = self._target_rect
            # Prefer below the target, else above, else centered vertically.
            if spot.bottom() + margin + ch <= bounds.height():
                y = spot.bottom() + margin
            elif spot.top() - margin - ch >= 0:
                y = spot.top() - margin - ch
            else:
                y = max(margin, (bounds.height() - ch) // 2)
            # Horizontally: align to the target's left, clamped into view.
            x = spot.left()
            x = max(margin, min(x, bounds.width() - cw - margin))
        self._callout.move(x, y)

    # ── painting ───────────────────────────────────────────────────────
    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        scrim = QPainterPath()
        scrim.addRect(QRectF(self.rect()))
        if not self._target_rect.isNull():
            pad = self._target_rect.adjusted(-6, -6, 6, 6)
            hole = QPainterPath()
            hole.addRoundedRect(QRectF(pad), 8, 8)
            scrim = scrim.subtracted(hole)
        p.fillPath(scrim, QColor(0, 0, 0, 185))

        if not self._target_rect.isNull():
            pad = self._target_rect.adjusted(-6, -6, 6, 6)
            pen = QPen(QColor(_CLAUDE_CORAL))
            pen.setWidth(2)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(pad), 8, 8)
        p.end()

    # ── input ──────────────────────────────────────────────────────────
    def mousePressEvent(self, _ev) -> None:
        # Swallow scrim clicks so the user can't poke the UI behind the tour;
        # the callout's own buttons still receive their events normally.
        pass

    def keyPressEvent(self, ev) -> None:
        if ev.key() == Qt.Key.Key_Escape:
            self.finish(mark=True)
        elif ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self._advance()
        else:
            super().keyPressEvent(ev)

    def resizeEvent(self, _ev) -> None:
        self._reposition_callout()

    def eventFilter(self, obj, ev) -> bool:
        if obj is self._host and ev.type() in (QEvent.Type.Resize, QEvent.Type.Move):
            self.setGeometry(self._host.rect())
            self._show_step()
        return False
