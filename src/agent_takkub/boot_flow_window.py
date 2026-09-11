"""Boot-flow wizard (#574) — 5-page GUI replacing the old provider-update-only
splash (`boot_update_window.py`) with a full pre-flight wizard: provider
updates (page A), a storage-migration confirm/backup step (page B), live
migration progress (page C), and a success/failure landing (page D/E).

Visual spec is the mockup set under the boot-flow-design canvas (5 `.dc.html`
artboards, 640x540) — per user directive (2026-09-11) the mockup IS the spec,
not inspiration; every color/spacing value here traces to either a literal
mockup pixel value or a `cockpit_theme` token that already equals it exactly
(new tokens were added to `cockpit_theme.py` for the handful of values with no
existing exact match: ``GROUND_INSET``, ``BORDER_CARD``, ``BORDER_CARD_ROW``,
``BORDER_CONTROL``, ``STATE_ERROR_ICON_BG``).

Backend interface: this module talks to a duck-typed ``flow`` object shaped
like the (still-being-written-in-parallel, #574 backend half) `boot_flow`
module — ``check_provider_updates``, ``remembered_provider_choice``,
``remember_provider_choice``, ``run_provider_updates``, ``plan_migration``,
``run_migration``. ``BootFlowWindow`` never imports ``boot_flow`` at module
scope (it may not exist yet on a machine mid-merge) — ``flow=None`` lazily
resolves it in ``start()`` only, so the class stays fully unit-testable with
a fake passed to the constructor today and wires up for real the moment
``boot_flow.py`` lands, no code changes needed. Every field read off a
backend object uses ``getattr(..., default)`` rather than assuming the exact
dataclass shape, since that interface hadn't shipped yet when this was
written — see the `takkub send --to backend` note this task ends with for
the exact assumptions made (phase-key vocabulary, item tuple shapes, the
version-number fields the mockup shows but the documented interface doesn't
carry).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QObject, QPointF, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import cockpit_theme as theme

PAGE_MAIN = 0
PAGE_PREMIGRATE = 1
PAGE_MIGRATING = 2
PAGE_DONE = 3
PAGE_FAILED = 4

# Canonical 5-phase order + Thai labels the migration progress page shows up
# front (mockup: all 5 rows render immediately, most as "todo", before any
# progress event arrives) — `plan_migration()`'s per-phase item counts seed
# each row's right-hand count; live `ProgressEvent`s then flip rows through
# todo -> active -> done as they land. `ProgressEvent.phase` is matched
# against these keys case-insensitively; an unrecognized key still renders
# (via `phase_label`) by advancing the next not-yet-seen row rather than
# failing closed — see `_MigrationPage._on_progress`.
_PHASE_ORDER: tuple[tuple[str, str], ...] = (
    ("backup", "สำรองข้อมูล"),
    ("promote", "คัดลอกขึ้นโครงใหม่"),
    ("verify", "ตรวจสอบ"),
    ("archive", "เก็บของเก่าเข้า archive"),
    ("done", "เสร็จ"),
)


def boot_update_enabled() -> bool:
    """False only when the user explicitly opted out (`TAKKUB_BOOT_UPDATE=0`)."""
    return os.environ.get("TAKKUB_BOOT_UPDATE", "").strip() != "0"


def _app_version() -> str:
    from . import __version__

    return __version__


class _WorkerError:
    """Wraps an exception raised inside a worker thread's target callable so
    it can travel across the `resultReady` signal instead of raising there
    (a QThread's `run()` swallowing everything but this sentinel would hide
    the failure entirely)."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


class _CallWorker(QThread):
    """Runs one no-arg callable off the Qt main thread, reporting the return
    value (or a `_WorkerError`) via `resultReady`. Covers every `flow` call
    that doesn't need a progress callback (`check_provider_updates`,
    `run_provider_updates`, `plan_migration`)."""

    resultReady = pyqtSignal(object)

    def __init__(self, fn: Callable[[], Any], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:  # worker thread
        try:
            result = self._fn()
        except Exception as exc:  # pragma: no cover - defensive, must never wedge boot
            result = _WorkerError(exc)
        self.resultReady.emit(result)


class _MigrationSignals(QObject):
    progress = pyqtSignal(object)  # ProgressEvent


class _MigrationWorker(QThread):
    """Runs `flow.run_migration(progress_cb)`. `progress_cb` is
    `_MigrationSignals.progress.emit`, a bound signal-emit created on the
    main thread before this thread starts — emitting it from here is the
    standard cross-thread-safe Qt pattern (the receiver, living on the main
    thread, gets it queued automatically)."""

    resultReady = pyqtSignal(object)

    def __init__(
        self, flow: Any, signals: _MigrationSignals, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._flow = flow
        self._signals = signals

    def run(self) -> None:  # worker thread
        try:
            outcome = self._flow.run_migration(self._signals.progress.emit)
        except Exception as exc:  # pragma: no cover - defensive, must never wedge boot
            outcome = _WorkerError(exc)
        self.resultReady.emit(outcome)


def _font(family: str, size: int, weight: int = 400) -> QFont:
    f = QFont(family, size)
    f.setWeight(QFont.Weight(weight))
    return f


def _has_update(item: Any) -> bool:
    """True when a `ProviderUpdateItem` (duck-typed — the interface hadn't
    shipped when this was written, see module docstring) names a newer
    version than what's installed."""
    latest = getattr(item, "latest", None)
    return bool(latest) and latest != getattr(item, "current", None)


def _version_diff_widget(current: str, latest: str | None, mono: str) -> QWidget:
    """`current` alone, or `current → latest` with the arrow/latest each
    their own color — 3 separate QLabels (see `_kv_row`'s docstring for why
    every label here gets an explicit transparent/no-border override)."""
    wrap = QWidget()
    lay = QHBoxLayout(wrap)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    cur_lbl = QLabel(current)
    cur_lbl.setFont(_font(mono, 12))
    cur_lbl.setStyleSheet(f"color: {theme.TEXT_MUTED}; background: transparent; border: none;")
    lay.addWidget(cur_lbl)
    if latest and latest != current:
        arrow_lbl = QLabel("→")
        arrow_lbl.setFont(_font(mono, 12))
        arrow_lbl.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; background: transparent; border: none;"
        )
        lay.addWidget(arrow_lbl)
        latest_lbl = QLabel(latest)
        latest_lbl.setFont(_font(mono, 12))
        latest_lbl.setStyleSheet(
            f"color: {theme.TEXT_PRIMARY}; background: transparent; border: none;"
        )
        lay.addWidget(latest_lbl)
    lay.addStretch(1)
    return wrap


def _fmt_gb(num_bytes: float | None) -> str | None:
    if num_bytes is None:
        return None
    return f"{num_bytes / 1e9:.1f} GB"


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    seconds = round(seconds)
    minutes, secs = divmod(seconds, 60)
    if minutes:
        return f"{minutes} นาที {secs} วินาที"
    return f"{secs} วินาที"


def _fmt_eta(seconds: float | None) -> str:
    if seconds is None:
        return ""
    minutes = max(1, round(seconds / 60))
    return f"เหลืออีกประมาณ {minutes} นาที"


class _CheckSquare(QWidget):
    """A 16x16, radius-4 checkbox drawn directly (matches the mockup's
    painted-square look, not the OS native indicator). `interactive=False`
    renders the disabled/inert look (an already-up-to-date provider row,
    the not-yet-decided remember-choice default) and never toggles."""

    toggled = pyqtSignal(bool)

    def __init__(
        self, checked: bool = False, interactive: bool = True, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setFixedSize(16, 16)
        self._checked = checked
        self._interactive = interactive
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if interactive else Qt.CursorShape.ArrowCursor
        )

    def is_checked(self) -> bool:
        return self._checked

    def set_checked(self, value: bool) -> None:
        if value == self._checked:
            return
        self._checked = value
        self.update()
        self.toggled.emit(value)

    def mousePressEvent(self, event) -> None:
        if self._interactive:
            self.set_checked(not self._checked)
        super().mousePressEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(0, 0, -1, -1)
        if self._checked:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.ACCENT_GOLD))
            painter.drawRoundedRect(rect, 4, 4)
            pen = QPen(QColor(theme.GOLD_TEXT_ON))
            pen.setWidth(2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawLine(4, 8, 7, 11)
            painter.drawLine(7, 11, 12, 5)
        else:
            painter.setPen(QPen(QColor(theme.BORDER_CONTROL), 1))
            painter.setBrush(QColor(theme.GROUND_INPUT))
            painter.drawRoundedRect(rect, 4, 4)


class _PhaseDot(QWidget):
    """22px circular phase marker on the migrating page — filled green+check
    when done, filled accent+number when active, outlined+muted number when
    not yet reached."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(22, 22)
        self._kind = "todo"
        self._number = ""

    def set_state(self, kind: str, number: str = "") -> None:
        self._kind = kind
        self._number = number
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(0, 0, -1, -1)
        if self._kind == "done":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.STATE_OK))
            painter.drawEllipse(rect)
            pen = QPen(QColor(theme.GOLD_TEXT_ON))
            pen.setWidth(2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            cx, cy = rect.center().x(), rect.center().y()
            painter.drawLine(cx - 4, cy, cx - 1, cy + 3)
            painter.drawLine(cx - 1, cy + 3, cx + 5, cy - 4)
            return
        if self._kind == "active":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.ACCENT_GOLD))
            painter.drawEllipse(rect)
            painter.setPen(QColor(theme.GOLD_TEXT_ON))
        else:
            painter.setPen(QPen(QColor(theme.BORDER_CONTROL), 1))
            painter.setBrush(QColor(theme.GROUND_INPUT))
            painter.drawEllipse(rect)
            painter.setPen(QColor(theme.TEXT_MUTED))
        font = QFont(self.font())
        font.setPointSize(11)
        font.setWeight(QFont.Weight(700 if self._kind == "active" else 600))
        painter.setFont(font)
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), self._number)


class _WarnTriangle(QWidget):
    """Painted outline warning-triangle + exclamation mark (mockup's SVG
    icon) — not the "⚠" text glyph, which tofus on the bundled IBM Plex
    fonts exactly like the other glyphs `boot_update_window.py` already
    documents replacing for the same reason."""

    def __init__(self, color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = color
        self.setFixedSize(16, 16)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(self._color))
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        triangle = [QPointF(8, 1.5), QPointF(14.5, 13.5), QPointF(1.5, 13.5)]
        painter.drawPolygon(triangle)
        painter.drawLine(QPointF(8, 6), QPointF(8, 9.5))
        painter.drawPoint(QPointF(8, 11.8))


def _styled(widget: QWidget, css: str) -> QWidget:
    widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    widget.setStyleSheet(css)
    return widget


def _primary_button(text: str, sans: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFixedHeight(36)
    btn.setFont(_font(sans, 13, 600))
    btn.setStyleSheet(
        f"QPushButton {{ background: {theme.ACCENT_GOLD}; color: {theme.GOLD_TEXT_ON}; "
        f"border: none; border-radius: 6px; padding: 0 18px; }}"
        f"QPushButton:disabled {{ background: {theme.BORDER_CONTROL}; color: {theme.TEXT_FAINT}; }}"
    )
    return btn


def _secondary_button(text: str, sans: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFixedHeight(36)
    btn.setFont(_font(sans, 13, 500))
    btn.setStyleSheet(
        f"QPushButton {{ background: {theme.GROUND_INPUT}; color: {theme.TEXT_PRIMARY}; "
        f"border: 1px solid {theme.BORDER_CARD}; border-radius: 6px; padding: 0 18px; }}"
        f"QPushButton:disabled {{ color: {theme.TEXT_FAINT}; }}"
    )
    return btn


def _card(parent: QWidget | None = None) -> QWidget:
    card = QWidget(parent)
    _styled(
        card,
        f"background: {theme.GROUND_INSET}; border: 1px solid {theme.BORDER_CARD}; "
        f"border-radius: 8px;",
    )
    return card


def _kv_key_label(key: str, sans: str) -> QLabel:
    key_lbl = QLabel(key)
    key_lbl.setFont(_font(sans, 13))
    key_lbl.setStyleSheet(f"color: {theme.TEXT_MUTED};")
    key_lbl.setFixedWidth(150)
    key_lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
    return key_lbl


def _kv_row(
    key: str, value: str, sans: str, mono: bool = False, color: str | None = None
) -> QWidget:
    """One key/value line for an info card. `value` is plain text (`color`
    sets the *entire* value one uniform color — every real call site here
    only ever needed one highlight color for the whole value, never a
    genuinely mixed-color run; use `_kv_row_mixed` for that). Every label
    below gets an explicit `background: transparent; border: none;` — a
    QLabel nested inside one of this dialog's rounded `WA_StyledBackground`
    cards paints a stray rounded box behind its own text once the *whole
    dialog* is rendered as a single `grab()`/`render()` pass (reproduces
    only through an ancestor's grab, never the label's own; confirmed by
    isolating each: dropping this explicit override — even with plain,
    tag-free text — brings the box straight back)."""
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(12)
    lay.addWidget(_kv_key_label(key, sans))
    val_lbl = QLabel(value)
    val_lbl.setTextFormat(Qt.TextFormat.PlainText)
    val_lbl.setWordWrap(True)
    val_lbl.setFont(
        _font((mono and theme.ensure_fonts_loaded()["mono"]) or sans, 12 if mono else 13)
    )
    val_lbl.setStyleSheet(
        f"color: {color or theme.TEXT_PRIMARY}; background: transparent; border: none;"
    )
    lay.addWidget(val_lbl, 1)
    return row


def _kv_row_mixed(key: str, sans: str, *segments: tuple[str, bool]) -> QWidget:
    """Like `_kv_row`, but the value is built from several `(text, mono)`
    segments laid out left-to-right on one line, each its own QLabel.
    Every real caller's mixed line is short enough to fit unwrapped."""
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(12)
    lay.addWidget(_kv_key_label(key, sans))
    for text, mono in segments:
        lbl = QLabel(text)
        lbl.setTextFormat(Qt.TextFormat.PlainText)
        lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        lbl.setFont(
            _font((mono and theme.ensure_fonts_loaded()["mono"]) or sans, 12 if mono else 13)
        )
        lbl.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; background: transparent; border: none;")
        lay.addWidget(lbl)
    lay.addStretch(1)
    return row


def _footer(sans: str, left: QWidget | str | None, buttons: list[QPushButton]) -> QWidget:
    footer = QWidget()
    _styled(footer, f"border-top: 1px solid {theme.BORDER_CARD};")
    lay = QHBoxLayout(footer)
    lay.setContentsMargins(20, 14, 20, 18)
    lay.setSpacing(12)
    if isinstance(left, str):
        lbl = QLabel(left)
        lbl.setFont(_font(sans, 12))
        lbl.setStyleSheet(f"color: {theme.TEXT_FAINT}; border: none;")
        lbl.setWordWrap(True)
        lay.addWidget(lbl, 1)
    elif left is not None:
        lay.addWidget(left, 1)
    else:
        lay.addStretch(1)
    btn_row = QHBoxLayout()
    btn_row.setSpacing(10)
    for b in buttons:
        btn_row.addWidget(b)
    lay.addLayout(btn_row)
    return footer


class BootFlowWindow(QDialog):
    """5-page boot wizard: provider updates -> pre-migrate confirm -> live
    migration progress -> done/failed landing. See module docstring for the
    backend contract and the `flow=None` lazy-import/dependency-injection
    design that keeps this fully unit-testable ahead of `boot_flow.py`
    landing."""

    #: Emitted exactly once, when the wizard is done deciding whether the
    #: cockpit should open. `proceed=False` only from the pre-migrate page's
    #: explicit "close program" button — every other exit path proceeds
    #: (mirrors the old splash's "never block the cockpit forever" rule).
    flowFinished = pyqtSignal(bool)

    def __init__(self, flow: Any = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._flow = flow
        self._proceed = True
        self._plan: Any = None
        self._outcome: Any = None
        self._provider_items: list[Any] = []
        self._workers: list[QThread] = []  # keep refs alive until finished
        self._last_percent = 0
        self._phase_rows: dict[str, _PhaseDot] = {}
        self._phase_labels: dict[str, QLabel] = {}
        self._phase_count_labels: dict[str, QLabel] = {}
        self._next_phase_slot = 0

        self.setWindowTitle("Takkub Cockpit")
        self.setFixedSize(640, 540)
        fonts = theme.ensure_fonts_loaded()
        self._sans = str(fonts["sans"])
        self._mono = str(fonts["mono"])
        # QLabel { background: transparent; border: none; } as a real
        # TYPE selector (not a bare per-instance declaration) is the
        # dialog-wide belt to every label's own matching belt-and-braces
        # override: a QLabel nested inside one of this dialog's rounded
        # `WA_StyledBackground` cards paints a stray rounded panel behind
        # its own text once the *whole dialog* renders as a single
        # `grab()`/`render()` pass (confirmed empirically — a label with
        # no override at all shows it; the same label with an explicit
        # `background: transparent; border: none;` does not — root Qt
        # mechanism not fully pinned down, but 100% reproducible either
        # way). Sets a floor here so a label added later can't reintroduce
        # this by forgetting the override.
        self.setStyleSheet(
            f"BootFlowWindow {{ background: {theme.GROUND_PANEL}; color: {theme.TEXT_PRIMARY}; }} "
            f"QLabel {{ background: transparent; border: none; }}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QVBoxLayout()
        header.setContentsMargins(20, 20, 20, 12)
        header.setSpacing(4)
        self._title_label = QLabel("Takkub Cockpit")
        self._title_label.setFont(_font(self._sans, 16, 700))
        self._title_label.setStyleSheet(f"color: {theme.TEXT_PRIMARY_ALT};")
        self._subtitle_label = QLabel("")
        self._subtitle_label.setFont(_font(self._sans, 12))
        self._subtitle_label.setWordWrap(True)
        header.addWidget(self._title_label)
        header.addWidget(self._subtitle_label)
        outer.addLayout(header)

        self._agg_bar = QProgressBar()
        self._agg_bar.setFixedHeight(4)
        self._agg_bar.setTextVisible(False)
        self._agg_bar.setRange(0, 100)
        self._agg_bar.setStyleSheet(
            f"QProgressBar {{ background: {theme.GROUND_INPUT}; border: none; }} "
            f"QProgressBar::chunk {{ background: {theme.ACCENT_GOLD}; }}"
        )
        outer.addWidget(self._agg_bar)

        hairline = QWidget()
        hairline.setFixedHeight(1)
        _styled(hairline, f"background: {theme.BORDER_CARD};")
        outer.addWidget(hairline)

        self._stack = QStackedWidget()
        outer.addWidget(self._stack, 1)

        self._page_main = self._build_page_main()
        self._page_premigrate = self._build_page_premigrate()
        self._page_migrating = self._build_page_migrating()
        self._page_done = self._build_page_done()
        self._page_failed = self._build_page_failed()
        for page in (
            self._page_main,
            self._page_premigrate,
            self._page_migrating,
            self._page_done,
            self._page_failed,
        ):
            self._stack.addWidget(page)

    # ── header ──────────────────────────────────────────────────
    def _set_header(self, subtitle: str, color: str, percent: int | None) -> None:
        self._subtitle_label.setText(subtitle)
        self._subtitle_label.setStyleSheet(f"color: {color};")
        self._agg_bar.setVisible(percent is not None)
        if percent is not None:
            self._agg_bar.setValue(max(0, min(100, percent)))

    # ── page A: provider updates ───────────────────────────────
    def _build_page_main(self) -> QWidget:
        page = QWidget()
        _styled(page, f"background: {theme.GROUND_PANEL};")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(20, 8, 20, 0)
        body_lay.setSpacing(0)

        self._provider_card = _card()
        self._provider_card_lay = QVBoxLayout(self._provider_card)
        self._provider_card_lay.setContentsMargins(0, 0, 0, 0)
        self._provider_card_lay.setSpacing(0)
        body_lay.addWidget(self._provider_card)

        remember_row = QWidget()
        remember_lay = QHBoxLayout(remember_row)
        remember_lay.setContentsMargins(2, 12, 2, 0)
        remember_lay.setSpacing(8)
        self._remember_check = _CheckSquare(checked=False, interactive=True)
        remember_lay.addWidget(self._remember_check)
        remember_lbl = QLabel("จำตัวเลือกนี้ไว้ — ครั้งหน้าไม่ต้องถาม (เปลี่ยนได้ที่ Settings → Providers)")
        remember_lbl.setFont(_font(self._sans, 12))
        remember_lbl.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        remember_lbl.setWordWrap(True)
        remember_lay.addWidget(remember_lbl, 1)
        body_lay.addWidget(remember_row)
        body_lay.addStretch(1)

        outer.addWidget(body, 1)

        self._main_skip_btn = _secondary_button("ข้าม — ใช้เวอร์ชันเดิม", self._sans)
        self._main_update_btn = _primary_button("อัพเดตที่เลือก (0)", self._sans)
        self._main_skip_btn.clicked.connect(self._on_main_skip_clicked)
        self._main_update_btn.clicked.connect(self._on_main_update_clicked)
        outer.addWidget(
            _footer(
                self._sans,
                "ข้ามได้เสมอ — provider เวอร์ชันเดิมยังใช้งานได้ตามปกติ",
                [self._main_skip_btn, self._main_update_btn],
            )
        )
        return page

    def _populate_provider_rows(self, items: list[Any]) -> None:
        self._provider_items = items
        while self._provider_card_lay.count():
            child = self._provider_card_lay.takeAt(0)
            w = child.widget()
            if w is not None:
                w.deleteLater()
        self._provider_row_checks: dict[str, _CheckSquare] = {}
        for i, item in enumerate(items):
            row = QWidget()
            row_lay = QHBoxLayout(row)
            row_lay.setContentsMargins(14, 10, 14, 10)
            row_lay.setSpacing(10)
            if i < len(items) - 1:
                _styled(row, f"border-bottom: 1px solid {theme.BORDER_CARD_ROW};")

            current = getattr(item, "current", "")
            latest = getattr(item, "latest", None)
            has_update = bool(latest) and latest != current
            selected = bool(getattr(item, "selected", has_update))

            check = _CheckSquare(checked=selected and has_update, interactive=has_update)
            if has_update:
                check.toggled.connect(self._refresh_update_button)
                self._provider_row_checks[getattr(item, "name", str(i))] = check
            row_lay.addWidget(check)

            name_lbl = QLabel(str(getattr(item, "label", getattr(item, "name", ""))))
            name_lbl.setFont(_font(self._sans, 13, 500))
            name_lbl.setStyleSheet(f"color: {theme.TEXT_PRIMARY};")
            name_lbl.setFixedWidth(110)
            row_lay.addWidget(name_lbl)

            ver_widget = _version_diff_widget(current, latest if has_update else None, self._mono)
            row_lay.addWidget(ver_widget, 1)

            chip = QLabel("มีอัพเดต" if has_update else "ล่าสุดแล้ว")
            chip.setFont(_font(self._sans, 11, 500))
            chip.setFixedHeight(20)
            chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            color = theme.STATE_WARN if has_update else theme.STATE_OK
            _styled(
                chip,
                f"background: {theme.GROUND_INPUT}; color: {color}; border-radius: 10px; padding: 0 8px;",
            )
            row_lay.addWidget(chip)

            self._provider_card_lay.addWidget(row)
        self._refresh_update_button()

    def _refresh_update_button(self) -> None:
        n = sum(1 for c in getattr(self, "_provider_row_checks", {}).values() if c.is_checked())
        self._main_update_btn.setText(f"อัพเดตที่เลือก ({n})")
        self._main_update_btn.setEnabled(n > 0)

    def _on_main_skip_clicked(self) -> None:
        self._save_remembered_choice(mode="skip", selected=[])
        self._proceed_to_migration_check()

    def _on_main_update_clicked(self) -> None:
        checks = getattr(self, "_provider_row_checks", {})
        selected_names = [name for name, c in checks.items() if c.is_checked()]
        all_selected = len(selected_names) == len(checks) and len(checks) > 0
        self._save_remembered_choice(
            mode="update_all" if all_selected else "selected", selected=selected_names
        )
        selected_items = [
            item for item in self._provider_items if getattr(item, "name", None) in selected_names
        ]
        self._run_provider_updates(selected_items)

    def _save_remembered_choice(self, mode: str, selected: list[str]) -> None:
        if not self._remember_check.is_checked() or self._flow is None:
            return
        try:
            self._flow.remember_provider_choice({"mode": mode, "selected": selected})
        except Exception:
            pass

    # ── page B: pre-migrate confirm ────────────────────────────
    def _build_page_premigrate(self) -> QWidget:
        page = QWidget()
        _styled(page, f"background: {theme.GROUND_PANEL};")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(20, 12, 20, 0)
        body_lay.setSpacing(12)

        section_lbl = QLabel("จะสำรองข้อมูลก่อนย้าย")
        section_lbl.setFont(_font(self._sans, 12, 600))
        section_lbl.setStyleSheet(f"color: {theme.TEXT_MUTED}; letter-spacing: 1px;")
        body_lay.addWidget(section_lbl)

        self._backup_card = _card()
        self._backup_card_lay = QVBoxLayout(self._backup_card)
        self._backup_card_lay.setContentsMargins(0, 0, 0, 0)
        self._backup_card_lay.setSpacing(0)
        body_lay.addWidget(self._backup_card)

        self._backup_info_box = QWidget()
        _styled(self._backup_info_box, f"background: {theme.GROUND_INPUT}; border-radius: 8px;")
        self._backup_info_lay = QVBoxLayout(self._backup_info_box)
        self._backup_info_lay.setContentsMargins(14, 10, 14, 10)
        self._backup_info_lay.setSpacing(6)
        body_lay.addWidget(self._backup_info_box)

        # ponytail: mockup bolds "คัดลอกก่อนเสมอ" mid-sentence — dropped to a
        # uniform tone here since this line wraps across 2 lines, and a
        # QLabel mixing RichText formatting runs paints a stray box behind
        # the whole label once rendered through a `grab()` from an ancestor
        # (see `_kv_row`'s docstring); a wrapping multi-line sentence can't
        # be split into separate per-run QLabels the way `_kv_row_mixed`
        # does for a short single-line value. Upgrade if a RichText fix
        # surfaces upstream.
        note = QLabel(
            "ระหว่างย้าย จะคัดลอกก่อนเสมอ "
            "และลบของเก่าเฉพาะหลังตรวจสอบครบทุกรายการ — ของเก่าถูกเก็บไว้ใน archive ไม่ถูกลบทิ้ง"
        )
        note.setTextFormat(Qt.TextFormat.PlainText)
        note.setWordWrap(True)
        note.setFont(_font(self._sans, 12))
        note.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        body_lay.addWidget(note)
        body_lay.addStretch(1)

        outer.addWidget(body, 1)

        self._premigrate_cancel_btn = _secondary_button("ยกเลิก — ปิดโปรแกรม", self._sans)
        self._premigrate_start_btn = _primary_button("สำรองข้อมูลแล้วเริ่มย้าย", self._sans)
        self._premigrate_cancel_btn.clicked.connect(self._on_premigrate_cancel_clicked)
        self._premigrate_start_btn.clicked.connect(self._on_premigrate_start_clicked)
        outer.addWidget(
            _footer(
                self._sans,
                "ใช้เวลาประมาณ 2–5 นาที",
                [self._premigrate_cancel_btn, self._premigrate_start_btn],
            )
        )
        return page

    def _populate_premigrate(self, plan: Any) -> None:
        while self._backup_card_lay.count():
            child = self._backup_card_lay.takeAt(0)
            w = child.widget()
            if w is not None:
                w.deleteLater()
        backup_items = list(getattr(plan, "backup_items", []) or [])
        for i, entry in enumerate(backup_items):
            label, count = (entry[0], entry[1]) if len(entry) >= 2 else (str(entry), 0)
            row = QWidget()
            row_lay = QHBoxLayout(row)
            row_lay.setContentsMargins(14, 8, 14, 8)
            if i < len(backup_items) - 1:
                _styled(row, f"border-bottom: 1px solid {theme.BORDER_CARD_ROW};")
            key_lbl = QLabel(str(label))
            key_lbl.setFont(_font(self._sans, 13))
            key_lbl.setStyleSheet(f"color: {theme.TEXT_PRIMARY};")
            row_lay.addWidget(key_lbl)
            row_lay.addStretch(1)
            val_lbl = QLabel(f"{count:,} รายการ")
            val_lbl.setFont(_font(self._mono, 12))
            val_lbl.setStyleSheet(f"color: {theme.TEXT_MUTED};")
            row_lay.addWidget(val_lbl)
            self._backup_card_lay.addWidget(row)

        while self._backup_info_lay.count():
            child = self._backup_info_lay.takeAt(0)
            w = child.widget()
            if w is not None:
                w.deleteLater()
        backup_dir = getattr(plan, "backup_dir", "") or ""
        self._backup_info_lay.addWidget(_kv_row("ที่เก็บสำรอง", backup_dir, self._sans, mono=True))
        est = _fmt_gb(getattr(plan, "estimated_bytes", None))
        free = _fmt_gb(getattr(plan, "free_bytes", None))
        if est or free:
            size_text = " · ".join(
                s for s in (f"~{est}" if est else None, f"ว่างในดิสก์ {free}" if free else None) if s
            )
            self._backup_info_lay.addWidget(
                _kv_row("ขนาดโดยประมาณ", size_text, self._sans, color=theme.STATE_OK)
            )
        self._backup_info_lay.addWidget(
            _kv_row_mixed(
                "ย้อนกลับ", self._sans, ("ได้ทุกเมื่อ — ", False), ("takkub migrate restore-v1", True)
            )
        )

    def _on_premigrate_cancel_clicked(self) -> None:
        self._proceed = False
        self.accept()

    def _on_premigrate_start_clicked(self) -> None:
        self._show_page(PAGE_MIGRATING, subtitle_only=False)
        self._start_migration()

    # ── page C: migrating ──────────────────────────────────────
    def _build_page_migrating(self) -> QWidget:
        page = QWidget()
        _styled(page, f"background: {theme.GROUND_PANEL};")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(20, 14, 20, 0)
        body_lay.setSpacing(10)

        pct_row = QHBoxLayout()
        self._pct_label = QLabel("0%")
        self._pct_label.setFont(_font(self._sans, 22, 700))
        self._pct_label.setStyleSheet(f"color: {theme.TEXT_PRIMARY_ALT};")
        pct_row.addWidget(self._pct_label)
        pct_row.addStretch(1)
        self._eta_label = QLabel("")
        self._eta_label.setFont(_font(self._sans, 12))
        self._eta_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        pct_row.addWidget(self._eta_label, 0, Qt.AlignmentFlag.AlignBottom)
        body_lay.addLayout(pct_row)

        self._migrate_bar = QProgressBar()
        self._migrate_bar.setFixedHeight(8)
        self._migrate_bar.setTextVisible(False)
        self._migrate_bar.setRange(0, 100)
        self._migrate_bar.setStyleSheet(
            f"QProgressBar {{ background: {theme.GROUND_INPUT}; border: none; border-radius: 4px; }} "
            f"QProgressBar::chunk {{ background: {theme.ACCENT_GOLD}; border-radius: 4px; }}"
        )
        body_lay.addWidget(self._migrate_bar)

        self._phase_list = QVBoxLayout()
        self._phase_list.setContentsMargins(0, 4, 0, 0)
        self._phase_list.setSpacing(0)
        for key, label in _PHASE_ORDER:
            row = QWidget()
            row_lay = QHBoxLayout(row)
            row_lay.setContentsMargins(0, 7, 0, 7)
            row_lay.setSpacing(12)
            dot = _PhaseDot()
            self._phase_rows[key] = dot
            row_lay.addWidget(dot)
            lbl = QLabel(label)
            lbl.setFont(_font(self._sans, 13))
            lbl.setStyleSheet(f"color: {theme.TEXT_MUTED}; background: transparent; border: none;")
            lbl.setFixedWidth(190)
            self._phase_labels[key] = lbl
            row_lay.addWidget(lbl)
            count_lbl = QLabel("")
            count_lbl.setFont(_font(self._mono, 12))
            count_lbl.setStyleSheet(f"color: {theme.TEXT_FAINT};")
            self._phase_count_labels[key] = count_lbl
            row_lay.addWidget(count_lbl, 1)
            self._phase_list.addWidget(row)
        body_lay.addLayout(self._phase_list)

        self._log_box = QLabel("")
        self._log_box.setFont(_font(self._mono, 11))
        self._log_box.setStyleSheet(f"color: {theme.TEXT_MUTED}; background: transparent;")
        self._log_box.setWordWrap(False)
        log_wrap = QWidget()
        _styled(log_wrap, f"background: {theme.GROUND_INPUT}; border-radius: 6px;")
        log_lay = QHBoxLayout(log_wrap)
        log_lay.setContentsMargins(12, 8, 12, 8)
        log_lay.addWidget(self._log_box)
        body_lay.addWidget(log_wrap)
        body_lay.addStretch(1)

        outer.addWidget(body, 1)

        warn_wrap = QWidget()
        warn_lay = QHBoxLayout(warn_wrap)
        warn_lay.setContentsMargins(0, 0, 0, 0)
        warn_lay.setSpacing(8)
        warn_icon = _WarnTriangle(theme.STATE_WARN)
        warn_lay.addWidget(warn_icon)
        warn_lbl = QLabel("อย่าปิดโปรแกรมระหว่างนี้ — ถ้าปิด ระบบจะกู้คืนให้เองตอนเปิดครั้งถัดไป")
        warn_lbl.setFont(_font(self._sans, 12))
        warn_lbl.setStyleSheet(f"color: {theme.STATE_WARN}; border: none;")
        warn_lay.addWidget(warn_lbl)
        self._migrate_footer_right = QLabel("")
        self._migrate_footer_right.setFont(_font(self._sans, 12))
        self._migrate_footer_right.setStyleSheet(f"color: {theme.TEXT_FAINT}; border: none;")
        outer.addWidget(_footer(self._sans, warn_wrap, []))
        # Right-hand backup-path note shares the footer row — appended after
        # `_footer()` builds it since that helper's `left` slot takes the
        # warning instead here (mirrors the mockup's two-sided footer).
        footer_widget = outer.itemAt(outer.count() - 1).widget()
        footer_widget.layout().addWidget(self._migrate_footer_right)
        return page

    def _set_phase_row_kind(self, key: str, kind: str) -> None:
        """Matches the mockup: the active row's label goes bold+bright,
        done/todo stay muted — only the dot (`_PhaseDot`) painted the
        active/done distinction before this, leaving every label the same
        dim gray regardless of state."""
        lbl = self._phase_labels.get(key)
        if lbl is None:
            return
        if kind == "active":
            lbl.setFont(_font(self._sans, 13, 600))
            lbl.setStyleSheet(
                f"color: {theme.TEXT_PRIMARY_ALT}; font-weight: 600; background: transparent; border: none;"
            )
        else:
            lbl.setFont(_font(self._sans, 13))
            lbl.setStyleSheet(f"color: {theme.TEXT_MUTED}; background: transparent; border: none;")

    def _start_migration(self) -> None:
        self._next_phase_slot = 0
        for key, _ in _PHASE_ORDER:
            self._phase_rows[key].set_state("todo", "")
            self._set_phase_row_kind(key, "todo")
        plan = self._plan
        if plan is not None:
            self._phase_count_labels["verify"].setText(
                f"{len(getattr(plan, 'promote_items', []) or [])} ขั้น"
            )
            self._phase_count_labels["archive"].setText(
                f"{len(getattr(plan, 'archive_items', []) or [])} รายการ"
            )
        signals = _MigrationSignals(self)
        signals.progress.connect(self._on_progress)
        self._migrate_pending_event = None
        self._migrate_throttle = QTimer(self)
        self._migrate_throttle.setInterval(120)
        self._migrate_throttle.timeout.connect(self._apply_pending_event)
        self._migrate_throttle.start()
        worker = _MigrationWorker(self._flow, signals, self)
        worker.resultReady.connect(self._on_migration_done)
        self._workers.append(worker)
        worker.start()

    def _on_progress(self, event: Any) -> None:
        self._migrate_pending_event = event

    def _apply_pending_event(self) -> None:
        event = self._migrate_pending_event
        if event is None:
            return
        self._migrate_pending_event = None
        percent = getattr(event, "percent_overall", None)
        if percent is not None:
            self._last_percent = int(percent)
            self._pct_label.setText(f"{int(percent)}%")
            self._migrate_bar.setValue(int(percent))
            self._agg_bar.setValue(int(percent))
        eta = _fmt_eta(getattr(event, "eta_s", None))
        if eta:
            self._eta_label.setText(eta)
        phase_key = str(getattr(event, "phase", "") or "").lower()
        if phase_key not in self._phase_rows:
            order = [k for k, _ in _PHASE_ORDER]
            phase_key = order[min(self._next_phase_slot, len(order) - 1)]
        idx = [k for k, _ in _PHASE_ORDER].index(phase_key)
        for i, (key, _) in enumerate(_PHASE_ORDER):
            if i < idx:
                self._phase_rows[key].set_state("done")
                self._set_phase_row_kind(key, "done")
            elif i == idx:
                self._phase_rows[key].set_state("active", str(idx + 1))
                self._set_phase_row_kind(key, "active")
            else:
                self._phase_rows[key].set_state("todo")
                self._set_phase_row_kind(key, "todo")
        self._next_phase_slot = max(self._next_phase_slot, idx + 1)
        done = getattr(event, "done", None)
        total = getattr(event, "total", None)
        unit = getattr(event, "unit", "") or ""
        if done is not None and total is not None:
            self._phase_count_labels[phase_key].setText(f"{done:,} / {total:,} {unit}".strip())
        log_line = getattr(event, "log_line", None)
        if log_line:
            self._log_box.setText(str(log_line))
        backup_dir = getattr(event, "backup_dir", None)
        if backup_dir:
            self._migrate_footer_right.setText(f"สำรองไว้ที่ {backup_dir}")

    def _on_migration_done(self, outcome: Any) -> None:
        self._migrate_throttle.stop()
        if isinstance(outcome, _WorkerError):
            self._outcome = None
            self._show_failed(error=str(outcome.exc), rolled_back=False, data_intact=False)
            return
        self._outcome = outcome
        if getattr(outcome, "ok", False):
            self._show_done(outcome)
        else:
            self._show_failed_outcome(outcome)

    # ── page D: done ────────────────────────────────────────────
    def _build_page_done(self) -> QWidget:
        page = QWidget()
        _styled(page, f"background: {theme.GROUND_PANEL};")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(20, 16, 20, 0)
        body_lay.setSpacing(14)

        head_row = QHBoxLayout()
        head_row.setSpacing(12)
        self._done_icon = _circle_icon(theme.STATE_OK, check=True)
        head_row.addWidget(self._done_icon)
        head_text = QVBoxLayout()
        head_text.setSpacing(0)
        self._done_heading = QLabel("")
        self._done_heading.setFont(_font(self._sans, 15, 600))
        self._done_heading.setStyleSheet(f"color: {theme.TEXT_PRIMARY_ALT};")
        self._done_sub = QLabel("")
        self._done_sub.setFont(_font(self._sans, 12))
        self._done_sub.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        head_text.addWidget(self._done_heading)
        head_text.addWidget(self._done_sub)
        head_row.addLayout(head_text, 1)
        body_lay.addLayout(head_row)

        self._done_summary_box = QWidget()
        _styled(self._done_summary_box, f"background: {theme.GROUND_INPUT}; border-radius: 8px;")
        self._done_summary_lay = QVBoxLayout(self._done_summary_box)
        self._done_summary_lay.setContentsMargins(14, 10, 14, 10)
        self._done_summary_lay.setSpacing(6)
        body_lay.addWidget(self._done_summary_box)

        self._done_paths_box = QWidget()
        _styled(self._done_paths_box, f"border: 1px solid {theme.BORDER_CARD}; border-radius: 8px;")
        self._done_paths_lay = QVBoxLayout(self._done_paths_box)
        self._done_paths_lay.setContentsMargins(14, 10, 14, 10)
        self._done_paths_lay.setSpacing(6)
        body_lay.addWidget(self._done_paths_box)
        body_lay.addStretch(1)

        outer.addWidget(body, 1)

        self._done_log_btn = _secondary_button("เปิด log", self._sans)
        self._done_open_btn = _primary_button("เปิดโปรแกรม", self._sans)
        self._done_log_btn.clicked.connect(lambda: self._open_first_log(self._outcome))
        self._done_open_btn.clicked.connect(self._on_done_open_clicked)
        outer.addWidget(
            _footer(
                self._sans,
                "ดูรายละเอียดได้ที่ Settings → Storage",
                [self._done_log_btn, self._done_open_btn],
            )
        )
        return page

    def _show_done(self, outcome: Any) -> None:
        self._set_header(f"ย้ายข้อมูลเสร็จแล้ว — พร้อมเปิดใช้งาน {_app_version()}", theme.STATE_OK, 100)
        self._done_heading.setText("ตรวจสอบครบทุกขั้น — ไม่มีข้อมูลหาย")
        duration = _fmt_duration(getattr(outcome, "duration_s", None))
        self._done_sub.setText(f"ใช้เวลา {duration}" if duration else "")

        while self._done_summary_lay.count():
            child = self._done_summary_lay.takeAt(0)
            w = child.widget()
            if w is not None:
                w.deleteLater()
        promoted = getattr(outcome, "promoted", None)
        promoted_n = len(promoted) if isinstance(promoted, (list, tuple)) else promoted
        if promoted_n is not None:
            extra = f" ({', '.join(promoted)})" if isinstance(promoted, (list, tuple)) else ""
            self._done_summary_lay.addWidget(
                _kv_row("ย้ายขึ้นโครงใหม่", f"{promoted_n} รายการ{extra}", self._sans)
            )
        archived = getattr(outcome, "archived", None)
        junk = getattr(outcome, "junk_deleted", None)
        if archived is not None:
            archived_n = len(archived) if isinstance(archived, (list, tuple)) else archived
            junk_text = f" · ลบไฟล์ขยะ {junk} รายการ" if junk else ""
            self._done_summary_lay.addWidget(
                _kv_row("เก็บของเก่าเข้า archive", f"{archived_n} รายการ{junk_text}", self._sans)
            )
        projects_n = getattr(outcome, "projects_count", None)
        if projects_n is not None:
            self._done_summary_lay.addWidget(
                _kv_row(
                    "โปรเจค",
                    f"{projects_n} โปรเจค — ครบเหมือนเดิม",
                    self._sans,
                    color=theme.STATE_OK,
                )
            )

        while self._done_paths_lay.count():
            child = self._done_paths_lay.takeAt(0)
            w = child.widget()
            if w is not None:
                w.deleteLater()
        backup_dir = getattr(outcome, "backup_dir", None)
        if backup_dir:
            self._done_paths_lay.addWidget(
                _kv_row("สำรองก่อนย้าย", backup_dir, self._sans, mono=True)
            )
        archive_dir = getattr(outcome, "archive_dir", None)
        if archive_dir:
            self._done_paths_lay.addWidget(
                _kv_row("archive ของเก่า", archive_dir, self._sans, mono=True)
            )
        prev = getattr(outcome, "previous_version", None)
        restore_segments = [("รัน ", False), ("takkub migrate restore-v1", True)]
        if prev:
            restore_segments.append((f" ก่อนติดตั้ง {prev}", False))
        self._done_paths_lay.addWidget(
            _kv_row_mixed("ถ้าต้องกลับเวอร์ชันเดิม", self._sans, *restore_segments)
        )

        self._show_page(PAGE_DONE, subtitle_only=True)

    def _on_done_open_clicked(self) -> None:
        self._proceed = True
        self.accept()

    # ── page E: failed ──────────────────────────────────────────
    def _build_page_failed(self) -> QWidget:
        page = QWidget()
        _styled(page, f"background: {theme.GROUND_PANEL};")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(20, 16, 20, 0)
        body_lay.setSpacing(14)

        head_row = QHBoxLayout()
        head_row.setSpacing(12)
        self._failed_icon = _circle_icon(theme.STATE_ERROR_ICON_BG, warn=True)
        head_row.addWidget(self._failed_icon, 0, Qt.AlignmentFlag.AlignTop)
        head_text = QVBoxLayout()
        head_text.setSpacing(2)
        self._failed_heading = QLabel("")
        self._failed_heading.setFont(_font(self._sans, 15, 600))
        self._failed_heading.setStyleSheet(f"color: {theme.TEXT_PRIMARY_ALT};")
        self._failed_sub = QLabel("")
        self._failed_sub.setFont(_font(self._sans, 12))
        self._failed_sub.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        self._failed_sub.setWordWrap(True)
        head_text.addWidget(self._failed_heading)
        head_text.addWidget(self._failed_sub)
        head_row.addLayout(head_text, 1)
        body_lay.addLayout(head_row)

        self._failed_info_box = QWidget()
        _styled(self._failed_info_box, f"background: {theme.GROUND_INPUT}; border-radius: 8px;")
        self._failed_info_lay = QVBoxLayout(self._failed_info_box)
        self._failed_info_lay.setContentsMargins(14, 10, 14, 10)
        self._failed_info_lay.setSpacing(6)
        body_lay.addWidget(self._failed_info_box)

        self._failed_note = QLabel("")
        self._failed_note.setFont(_font(self._sans, 12))
        self._failed_note.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        self._failed_note.setWordWrap(True)
        body_lay.addWidget(self._failed_note)
        body_lay.addStretch(1)

        outer.addWidget(body, 1)

        self._failed_log_btn = _secondary_button("เปิด log", self._sans)
        self._failed_retry_btn = _secondary_button("ลองใหม่", self._sans)
        self._failed_continue_btn = _primary_button("ใช้เวอร์ชันเดิมต่อ", self._sans)
        self._failed_log_btn.clicked.connect(lambda: self._open_first_log(self._outcome))
        self._failed_retry_btn.clicked.connect(self._on_failed_retry_clicked)
        self._failed_continue_btn.clicked.connect(self._on_failed_continue_clicked)

        footer = QWidget()
        _styled(footer, f"border-top: 1px solid {theme.BORDER_CARD};")
        f_lay = QHBoxLayout(footer)
        f_lay.setContentsMargins(20, 14, 20, 18)
        f_lay.setSpacing(10)
        left_row = QHBoxLayout()
        left_row.setSpacing(10)
        left_row.addWidget(self._failed_log_btn)
        left_row.addWidget(self._failed_retry_btn)
        f_lay.addLayout(left_row)
        f_lay.addStretch(1)
        f_lay.addWidget(self._failed_continue_btn)
        outer.addWidget(footer)
        return page

    def _show_failed_outcome(self, outcome: Any) -> None:
        failed_phase = str(getattr(outcome, "failed_phase", "") or "")
        phase_label = dict(_PHASE_ORDER).get(failed_phase.lower(), failed_phase or "ไม่ทราบขั้นตอน")
        failed_step = getattr(outcome, "failed_step", "") or ""
        error = getattr(outcome, "error", "") or ""
        rolled_back = bool(getattr(outcome, "rolled_back", True))
        data_intact = bool(getattr(outcome, "data_intact", rolled_back))
        self._show_failed(
            heading=f"ขั้นตอน {phase_label} ไม่ผ่าน",
            detail=str(failed_step or error),
            rolled_back=rolled_back,
            data_intact=data_intact,
            outcome=outcome,
        )

    def _show_failed(
        self,
        error: str = "",
        heading: str = "",
        detail: str = "",
        rolled_back: bool = False,
        data_intact: bool = False,
        outcome: Any = None,
    ) -> None:
        subtitle = (
            "ย้ายข้อมูลไม่สำเร็จ — ย้อนกลับอัตโนมัติแล้ว ไม่มีข้อมูลหาย"
            if rolled_back
            else "ย้ายข้อมูลไม่สำเร็จ — ระบบยังไม่ได้ย้อนกลับ กรุณาตรวจสอบ"
        )
        self._set_header(subtitle, theme.STATE_ERROR_BRIGHT, self._last_percent)
        self._failed_heading.setText(heading or "ย้ายข้อมูลไม่สำเร็จ")
        sub_text = detail or error
        if rolled_back and detail:
            sub_text += " — ระบบคัดลอกทุกอย่างกลับที่เดิมจากสำเนาแล้ว"
        self._failed_sub.setText(sub_text)

        while self._failed_info_lay.count():
            child = self._failed_info_lay.takeAt(0)
            w = child.widget()
            if w is not None:
                w.deleteLater()
        status_color = theme.STATE_OK if data_intact else theme.STATE_ERROR_BRIGHT
        status_text = (
            "เหมือนก่อนเริ่มทุกไบต์ (ตรวจ sha256 แล้ว)"
            if data_intact
            else "ตรวจสอบไม่ผ่าน — ดู log ก่อนดำเนินการต่อ"
        )
        self._failed_info_lay.addWidget(
            _kv_row("สถานะข้อมูล", status_text, self._sans, color=status_color)
        )
        backup_dir = getattr(outcome, "backup_dir", None) if outcome is not None else None
        if backup_dir:
            self._failed_info_lay.addWidget(
                _kv_row("สำรองก่อนย้าย", backup_dir, self._sans, mono=True)
            )
        log_paths = list(getattr(outcome, "log_paths", []) or []) if outcome is not None else []
        if log_paths:
            self._failed_info_lay.addWidget(
                _kv_row("log", " · ".join(log_paths), self._sans, mono=True)
            )

        prev = getattr(outcome, "previous_version", None) if outcome is not None else None
        prev_text = f" ({prev})" if prev else ""
        self._failed_continue_btn.setText(f"ใช้เวอร์ชันเดิมต่อ{prev_text}")
        self._failed_note.setText(
            f"ใช้เวอร์ชันเดิม{prev_text} ต่อได้ทันที หรือส่ง log ให้ทีมดูแล้วค่อยลองใหม่ภายหลัง — ข้อมูลไม่ได้ถูกเปลี่ยน"
        )

        self._show_page(PAGE_FAILED, subtitle_only=True)

    def _on_failed_retry_clicked(self) -> None:
        self._show_page(PAGE_MIGRATING, subtitle_only=False)
        self._set_header(f"กำลังย้ายข้อมูลเป็นโครงใหม่ ({_app_version()}) — ลองใหม่", theme.TEXT_MUTED, 0)
        self._start_migration()

    def _on_failed_continue_clicked(self) -> None:
        self._proceed = True
        self.accept()

    def _open_first_log(self, outcome: Any) -> None:
        paths = list(getattr(outcome, "log_paths", []) or []) if outcome is not None else []
        if not paths:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(paths[0]))

    # ── shared page-switch helper ──────────────────────────────
    def _show_page(self, index: int, subtitle_only: bool) -> None:
        self._stack.setCurrentIndex(index)

    # ── orchestration ────────────────────────────────────────────
    def start(self) -> None:
        """Entry point — call once after `show()`. Decides whether page A is
        needed at all (env opt-out / a remembered non-"ask" choice skip it
        straight to the migration check), else fetches provider updates on a
        worker thread and shows page A once they arrive."""
        flow = self._resolve_flow()
        self._flow = flow
        if not boot_update_enabled():
            self._proceed_to_migration_check()
            return
        remembered = None
        try:
            remembered = flow.remembered_provider_choice()
        except Exception:
            remembered = None
        mode = (remembered or {}).get("mode") if remembered else "ask"
        if mode in ("update_all", "skip", "selected"):
            self._apply_remembered_choice(mode, (remembered or {}).get("selected") or [])
            return
        self._set_header("กำลังตรวจสอบอัพเดต provider…", theme.TEXT_MUTED, None)
        worker = _CallWorker(lambda: flow.check_provider_updates(30.0), self)
        worker.resultReady.connect(self._on_provider_check_done)
        self._workers.append(worker)
        worker.start()

    def _resolve_flow(self) -> Any:
        if self._flow is not None:
            return self._flow
        from . import boot_flow  # lazy — see module docstring

        return boot_flow

    def _apply_remembered_choice(self, mode: str, selected_names: list[str]) -> None:
        flow = self._flow
        worker = _CallWorker(lambda: flow.check_provider_updates(30.0), self)

        def _on_items(items: Any) -> None:
            if isinstance(items, _WorkerError) or not items:
                self._proceed_to_migration_check()
                return
            if mode == "skip":
                self._proceed_to_migration_check()
                return
            to_run = (
                [i for i in items if _has_update(i)]
                if mode == "update_all"
                else [i for i in items if getattr(i, "name", None) in selected_names]
            )
            self._run_provider_updates(to_run)

        worker.resultReady.connect(_on_items)
        self._workers.append(worker)
        worker.start()

    def _on_provider_check_done(self, items: Any) -> None:
        if isinstance(items, _WorkerError) or not items:
            self._proceed_to_migration_check()
            return
        has_any_update = any(_has_update(i) for i in items)
        if not has_any_update:
            self._proceed_to_migration_check()
            return
        self._set_header(
            f"พบอัพเดตของ provider {sum(1 for i in items if _has_update(i))} รายการ — เลือกได้ว่าจะอัพเดตตอนนี้หรือใช้เวอร์ชันเดิมต่อ",
            theme.TEXT_MUTED,
            None,
        )
        self._populate_provider_rows(items)
        self._show_page(PAGE_MAIN, subtitle_only=False)

    def _run_provider_updates(self, items: list[Any]) -> None:
        if not items:
            self._proceed_to_migration_check()
            return
        flow = self._flow

        def _progress_cb(*_args: Any, **_kwargs: Any) -> None:
            pass  # ponytail: no live per-row spinner yet — page A is a quick pre-boot step; add if it proves too quiet in practice

        worker = _CallWorker(lambda: flow.run_provider_updates(items, _progress_cb), self)
        worker.resultReady.connect(lambda _result: self._proceed_to_migration_check())
        self._workers.append(worker)
        worker.start()

    def _proceed_to_migration_check(self) -> None:
        self._set_header("กำลังตรวจสอบโครงสร้างข้อมูล…", theme.TEXT_MUTED, None)
        flow = self._flow
        worker = _CallWorker(lambda: flow.plan_migration(), self)
        worker.resultReady.connect(self._on_plan_ready)
        self._workers.append(worker)
        worker.start()

    def _on_plan_ready(self, plan: Any) -> None:
        if isinstance(plan, _WorkerError) or plan is None:
            self._proceed = True
            self.accept()
            return
        self._plan = plan
        self._set_header(
            f"เวอร์ชัน {_app_version()} ใช้โครงสร้างข้อมูลใหม่ — ต้องย้ายข้อมูลครั้งเดียวก่อนเปิดใช้งาน",
            theme.TEXT_MUTED,
            None,
        )
        self._populate_premigrate(plan)
        self._show_page(PAGE_PREMIGRATE, subtitle_only=False)

    # ── close handling ──────────────────────────────────────────
    def closeEvent(self, event) -> None:
        if self._stack.currentIndex() == PAGE_MIGRATING:
            event.ignore()
            return
        super().closeEvent(event)

    def done(self, result: int) -> None:
        super().done(result)
        self.flowFinished.emit(self._proceed)


def _circle_icon(bg: str, check: bool = False, warn: bool = False) -> QWidget:
    class _Icon(QWidget):
        def paintEvent(self, _event) -> None:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = self.rect().adjusted(0, 0, -1, -1)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(bg))
            painter.drawEllipse(rect)
            cx, cy = rect.center().x(), rect.center().y()
            if check:
                pen = QPen(QColor(theme.GOLD_TEXT_ON))
                pen.setWidth(3)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(pen)
                painter.drawLine(cx - 6, cy, cx - 2, cy + 5)
                painter.drawLine(cx - 2, cy + 5, cx + 7, cy - 6)
            elif warn:
                pen = QPen(QColor(theme.STATE_ERROR_BRIGHT))
                pen.setWidth(2)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(pen)
                painter.drawLine(cx, cy - 6, cx, cy + 1)
                painter.drawPoint(cx, cy + 5)

    icon = _Icon()
    icon.setFixedSize(36, 36)
    return icon


def run_boot_flow_gate(main_window_factory: Callable[[], Any]) -> Any:
    """Show the wizard, run it to completion via a local `QEventLoop` (never
    a nested `QApplication.exec()`), then construct and return the main
    window — or exit the process if the user explicitly chose to close the
    program rather than migrate (page B's "close program" button)."""
    from PyQt6.QtCore import QEventLoop

    wizard = BootFlowWindow()
    loop = QEventLoop()
    result = {"proceed": True}

    def _on_finished(proceed: bool) -> None:
        result["proceed"] = proceed
        loop.quit()

    wizard.flowFinished.connect(_on_finished)
    wizard.show()
    QTimer.singleShot(0, wizard.start)
    loop.exec()
    if not result["proceed"]:
        sys.exit(0)
    return main_window_factory()
