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
carry, plus this round's additions: ``plan.backup_items`` entries may carry
an optional 3rd ``unit`` string; ``plan.verify_steps`` is the true
validation-step count, distinct from ``len(plan.promote_items)``;
``ProgressEvent.current_path`` names the item actively being processed, for
the migrating page's active-row counter).
"""

from __future__ import annotations

import html
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
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

_LOG = logging.getLogger(__name__)

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


def _previous_app_version() -> str | None:
    """Best-effort fallback for the "previous version" text on the done/
    failed pages: `outcome.previous_version` is the documented source, but
    #574's backend interface may not carry it yet (see module docstring),
    so this reads the last app version recorded in `version.json`
    (`core.versioning.store`, under DATA_HOME) directly rather than waiting
    on that field. Never raises — missing/corrupt file, or no "app"
    component recorded yet, both read as `None` (caller already treats a
    falsy previous-version as "don't show one")."""
    try:
        from .core.versioning.store import read_version_doc

        for component_version in read_version_doc():
            if component_version.component == "app":
                return component_version.version
    except Exception:
        pass
    return None


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
    """`size` is a literal CSS pixel value (the mockup's spec unit) — every
    call site below already passes the mockup's px number, so this must use
    `setPixelSize`, never the constructor's point-size overload (round 2's
    bug: `QFont(family, size)` treats `size` as *points*, which at a typical
    96 DPI renders ~33% larger than the spec, throwing off wrapping and
    spacing on every page)."""
    f = QFont(family)
    f.setPixelSize(size)
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


def _shorten_current_path(path: str) -> str:
    """`providers/codex/default` -> `providers/…` (mockup: the active-row
    counter only ever shows the first path segment, not the full item
    path — that full detail belongs to the log line below it)."""
    head, sep, rest = path.partition("/")
    return f"{head}/…" if sep and rest else path


def _parse_log_line(text: str, current_path: str | None = None) -> tuple[str, str, str, str]:
    """Splits a `ProgressEvent.log_line` into the mockup's 4 differently-
    colored runs: timestamp, operation, current path, explanation.

    Two shapes reach here: the mockup's own double-space-separated 4-part
    text (`"08:31:12  promote  providers/codex/default  คัดลอก 1,204
    ไฟล์"`), and the *real* `boot_flow.py` backend's shape — `on_entry`
    emits a bare `"<step_id>: <name>"` pair, no timestamp, no detail at
    all (round 4 bug: the old parser only recognized the double-space
    shape, so a real log line landed entirely, uncolored, in the FAINT
    timestamp slot). `current_path` (from `ProgressEvent.current_path`,
    once that field lands — see module docstring) always wins over
    whatever path the text itself carries. Pads with empty strings on a
    short/malformed line rather than raising."""
    text = text.strip()
    if not text:
        return "", "", "", ""
    double_space_parts = [p for p in text.split("  ") if p]
    if len(double_space_parts) >= 3:
        timestamp = double_space_parts[0]
        operation = double_space_parts[1]
        path = double_space_parts[2]
        detail = "  ".join(double_space_parts[3:])
    elif ":" in text:
        operation, _sep, rest = text.partition(":")
        timestamp, operation, path, detail = "", operation.strip(), rest.strip(), ""
    else:
        # Unstructured — degrade to the whole line in the timestamp slot
        # rather than guessing at fields that aren't there.
        timestamp, operation, path, detail = text, "", "", ""
    if current_path:
        path = str(current_path)
    return timestamp, operation, path, detail


def _path_str(value: Any) -> str:
    """`pathlib.Path` (or path-like) -> display string, relative to
    `config.DATA_HOME` when the path lives under it (mockup shows short
    paths, never a full absolute one) — `boot_flow.py`'s real dataclasses
    carry `Path` for `backup_dir`/`archive_dir`/`log_paths`, but `QLabel`/
    `str.join` both need a plain `str` (round 4 crash: passing a `Path`
    straight through raised `TypeError` and took the whole boot process
    down with it)."""
    if value is None or value == "":
        return ""
    if isinstance(value, Path):
        from . import config

        try:
            return str(value.relative_to(config.DATA_HOME))
        except ValueError:
            return str(value)
    return str(value)


def _clear_layout(layout: Any) -> None:
    while layout.count():
        child = layout.takeAt(0)
        w = child.widget()
        if w is not None:
            w.deleteLater()


def _unpack_backup_row(entry: Any) -> tuple[str, int, int | None, str]:
    """`(label, count, size_bytes_or_None, unit)` from one `plan.
    backup_items` entry. The real interface is a 4-tuple `(label, count,
    bytes, unit)`; a 3-tuple `(label, count, unit)` (no byte total) and a
    bare 2-tuple `(label, count)` (unit defaults to "รายการ") both still
    work. Round 4 bug: reading a 4-tuple's index 2 (bytes) as the unit
    string rendered the byte count itself as the unit word."""
    if isinstance(entry, (list, tuple)):
        if len(entry) >= 4:
            return str(entry[0]), int(entry[1]), entry[2], str(entry[3])
        if len(entry) == 3:
            return str(entry[0]), int(entry[1]), None, str(entry[2])
        if len(entry) == 2:
            return str(entry[0]), int(entry[1]), None, "รายการ"
    return str(entry), 0, None, "รายการ"


def _phase_label_and_number(failed_phase: Any) -> tuple[str, int | None]:
    """`(label, 1-based phase number)` for a `MigrationOutcome.
    failed_phase` value — the real interface sends an `int` (`_phase_of_
    step`'s 1..4 output); a string phase key (older/test fixtures, e.g.
    "verify") still works too. Round 4 bug: `str(3).lower()` is `"3"`,
    never a key in `_PHASE_ORDER`, so an int `failed_phase` always fell
    through to "ไม่ทราบขั้นตอน" and lost the "(7/11)" step-position suffix
    entirely."""
    order = [k for k, _ in _PHASE_ORDER]
    labels = dict(_PHASE_ORDER)
    if isinstance(failed_phase, int):
        if 1 <= failed_phase <= len(order):
            key = order[failed_phase - 1]
            return labels[key], failed_phase
        return "ไม่ทราบขั้นตอน", None
    key = str(failed_phase or "").lower()
    if key in labels:
        return labels[key], order.index(key) + 1
    return (str(failed_phase) if failed_phase else "ไม่ทราบขั้นตอน"), None


def _apply_line_height(label: QLabel, px_size: float, line_height: float = 1.45) -> None:
    """Approximates the mockup's CSS `line-height: 1.45` for a native
    QLabel — Qt sizes a plain label's height off font-metrics ascent +
    descent alone, with no equivalent of a CSS line box's extra leading,
    so a stack of these labels renders visibly tighter/shifted-up versus
    the mockup (round 4 audit V1: 16px title should occupy a ~23px line
    box; a bare QLabel measured ~20px, shifting every label below it up
    and compounding down the whole header). `setMinimumHeight` (not
    `setFixedHeight`) so a label that actually wraps to multiple lines
    (long provider-update subtitles) still grows past this floor instead
    of being clipped — only the common single-line case is pinned to the
    exact target height, with Qt's default vertical-center alignment
    distributing the extra space as leading above/below the text."""
    label.setMinimumHeight(round(px_size * line_height))


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
        font.setPixelSize(11)
        font.setWeight(QFont.Weight(700 if self._kind == "active" else 600))
        painter.setFont(font)
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), self._number)


def _draw_warn_triangle(painter: QPainter, size: float, color: str) -> None:
    """Outline warning-triangle + exclamation mark (mockup's SVG icon,
    viewBox 24, stroke-width 2.5) scaled to `size` — not the "⚠" text glyph,
    which tofus on the bundled IBM Plex fonts exactly like the other glyphs
    `boot_update_window.py` already documents replacing for the same
    reason. Shared by `_WarnTriangle` (the 16px footer/legend icon) and the
    failed-page 36px result disk's 20px icon so both trace to one drawing."""
    scale = size / 24.0
    pen = QPen(QColor(color))
    pen.setWidthF(2.5 * scale)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    top, left, right = 3.9 * scale, 1.8 * scale, 22.2 * scale
    bottom = 21 * scale
    triangle = [QPointF(12 * scale, top), QPointF(right, bottom), QPointF(left, bottom)]
    painter.drawPolygon(triangle)
    painter.drawLine(QPointF(12 * scale, 9 * scale), QPointF(12 * scale, 13 * scale))
    painter.drawPoint(QPointF(12 * scale, 17 * scale))


class _WarnTriangle(QWidget):
    """16px painted warning-triangle + "!" — see `_draw_warn_triangle`."""

    def __init__(self, color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = color
        self.setFixedSize(16, 16)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        _draw_warn_triangle(painter, 16, self._color)


class _InfoCircle(QWidget):
    """16px painted outline info-circle + dot/line (mockup's SVG lucide
    "info" icon, viewBox 24) — same tofu-avoidance reasoning as
    `_WarnTriangle`."""

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
        painter.drawEllipse(QPointF(8, 8), 6.5, 6.5)
        painter.drawLine(QPointF(8, 8.5), QPointF(8, 11))
        painter.drawPoint(QPointF(8, 5.3))


def _styled(widget: QWidget, css: str) -> QWidget:
    """Applies `css` scoped to exactly this widget instance via a `QWidget#id`
    selector — a selector-less `setStyleSheet()` call is NOT scoped to the
    widget alone: Qt treats a bare declaration block as an implicit
    universal rule that cascades to every descendant regardless of type
    (`QWidget#id` still matches, since every widget — QLabel included — is
    a `QWidget` subclass). Before this, every `_card()`/box/row built with
    the old bare form painted its own background+border+radius a *second*
    time around each child row, wrapper, and label that didn't carry its
    own explicit override — the root cause behind the many per-label
    "background: transparent; border: none;" overrides elsewhere in this
    module (harmless now, but were load-bearing against this leak)."""
    name = f"styled_{id(widget)}"
    widget.setObjectName(name)
    widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    widget.setStyleSheet(f"QWidget#{name} {{ {css} }}")
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
    key_lbl.setStyleSheet(f"color: {theme.TEXT_MUTED}; background: transparent; border: none;")
    key_lbl.setFixedWidth(150)
    key_lbl.setWordWrap(True)
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


def _kv_row_rich(key: str, sans: str, value_html: str, color: str) -> QWidget:
    """Like `_kv_row`, but the value is HTML so one inline run (the mono
    command in the restore-version line) can carry its own font while the
    rest of the sentence stays `sans`/`color` — needed because `_kv_row`'s
    `color` only ever paints one uniform tone across the whole value."""
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(12)
    lay.addWidget(_kv_key_label(key, sans))
    val_lbl = QLabel(value_html)
    val_lbl.setTextFormat(Qt.TextFormat.RichText)
    val_lbl.setWordWrap(True)
    val_lbl.setFont(_font(sans, 13))
    val_lbl.setStyleSheet(f"color: {color}; background: transparent; border: none;")
    lay.addWidget(val_lbl, 1)
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
        lay.addWidget(left, 1, Qt.AlignmentFlag.AlignVCenter)
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
        _apply_line_height(self._title_label, 16)
        self._subtitle_label = QLabel("")
        self._subtitle_label.setFont(_font(self._sans, 12))
        self._subtitle_label.setWordWrap(True)
        _apply_line_height(self._subtitle_label, 12)
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
        """The 4px aggregate track is always visible (mockup: A/B show it
        empty at 0%, C/D/E show it filled) — `percent=None` only means "no
        new value to apply this call", never "hide the bar"; leaving it out
        keeps whatever value was last set instead of resetting to 0."""
        self._subtitle_label.setText(subtitle)
        self._subtitle_label.setStyleSheet(f"color: {color};")
        self._agg_bar.setVisible(True)
        if percent is not None:
            self._agg_bar.setValue(max(0, min(100, percent)))

    def _render_safely(
        self, label: str, render: Callable[[], None], fallback: Callable[[], None]
    ) -> None:
        """Runs *render* (one page's widget-population step, built off
        fields read straight from the backend's `flow` object); on any
        exception, logs it and falls back to *fallback* instead of letting
        it propagate — every field this module reads off a backend object
        is `getattr(..., default)`-defensive against a *missing* field
        already, but a field that's present with an unexpected *type*
        (round 4: `pathlib.Path` where a `str` was assumed) must still
        never crash the whole boot wizard process."""
        try:
            render()
        except Exception:
            _LOG.exception("boot_flow_window: %s render failed", label)
            try:
                fallback()
            except Exception:
                _LOG.exception("boot_flow_window: %s fallback render also failed", label)

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
            name_lbl.setStyleSheet(
                f"color: {theme.TEXT_PRIMARY}; background: transparent; border: none;"
            )
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

        # Heading-to-card gap (6px) is its own group, nested inside the
        # 12px-spaced outer column alongside the info box and note below —
        # the mockup's two gap values (6 inside this group, 12 between
        # groups) aren't the same number, so they can't share one flat
        # QVBoxLayout.
        section_group = QVBoxLayout()
        section_group.setSpacing(6)
        section_lbl = QLabel("จะสำรองข้อมูลก่อนย้าย")
        section_lbl.setFont(_font(self._sans, 12, 600))
        section_lbl.setStyleSheet(f"color: {theme.TEXT_MUTED}; letter-spacing: 0.48px;")
        section_group.addWidget(section_lbl)

        self._backup_card = _card()
        self._backup_card_lay = QVBoxLayout(self._backup_card)
        self._backup_card_lay.setContentsMargins(0, 0, 0, 0)
        self._backup_card_lay.setSpacing(0)
        section_group.addWidget(self._backup_card)
        body_lay.addLayout(section_group)

        self._backup_info_box = QWidget()
        _styled(self._backup_info_box, f"background: {theme.GROUND_INPUT}; border-radius: 8px;")
        self._backup_info_lay = QVBoxLayout(self._backup_info_box)
        self._backup_info_lay.setContentsMargins(14, 10, 14, 10)
        self._backup_info_lay.setSpacing(6)
        body_lay.addWidget(self._backup_info_box)

        note_row = QWidget()
        note_lay = QHBoxLayout(note_row)
        note_lay.setContentsMargins(0, 0, 0, 0)
        note_lay.setSpacing(8)
        note_icon = _InfoCircle(theme.TEXT_MUTED)
        # Mockup: icon has its own `margin-top: 1px`, offset 1px below the
        # note text's own top — a bare `AlignTop` on the icon lines it up
        # flush with the label instead (round 4 audit V7).
        note_icon_wrap = QWidget()
        note_icon_wrap_lay = QVBoxLayout(note_icon_wrap)
        note_icon_wrap_lay.setContentsMargins(0, 1, 0, 0)
        note_icon_wrap_lay.setSpacing(0)
        note_icon_wrap_lay.addWidget(note_icon)
        note_lay.addWidget(note_icon_wrap, 0, Qt.AlignmentFlag.AlignTop)
        # RichText for the bold "คัดลอกก่อนเสมอ" mid-sentence run. Round-2
        # dropped this to plain text over a (never actually verified against
        # RichText) worry that the ancestor-`grab()` stray-box artifact
        # `_kv_row`'s docstring documents would reappear here; the same
        # explicit background/border override that fixes it there fixes it
        # for this wrapped 2-line RichText label too (confirmed by real
        # render, not just reasoning from that docstring).
        note = QLabel(
            "ระหว่างย้าย จะ"
            f'<b style="color: {theme.TEXT_PRIMARY};">คัดลอกก่อนเสมอ</b> '
            "และลบของเก่าเฉพาะหลังตรวจสอบครบทุกรายการ — ของเก่าถูกเก็บไว้ใน archive ไม่ถูกลบทิ้ง"
        )
        note.setTextFormat(Qt.TextFormat.RichText)
        note.setWordWrap(True)
        note.setFont(_font(self._sans, 12))
        note.setStyleSheet(f"color: {theme.TEXT_MUTED}; background: transparent; border: none;")
        note_lay.addWidget(note, 1)
        body_lay.addWidget(note_row)
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
        self._render_safely(
            "pre-migrate page",
            lambda: self._populate_premigrate_unsafe(plan),
            self._populate_premigrate_fallback,
        )

    def _populate_premigrate_unsafe(self, plan: Any) -> None:
        _clear_layout(self._backup_card_lay)
        backup_items = list(getattr(plan, "backup_items", []) or [])
        for i, entry in enumerate(backup_items):
            label, count, size_bytes, unit = _unpack_backup_row(entry)
            row = QWidget()
            row_lay = QHBoxLayout(row)
            row_lay.setContentsMargins(14, 8, 14, 8)
            if i < len(backup_items) - 1:
                _styled(row, f"border-bottom: 1px solid {theme.BORDER_CARD_ROW};")
            key_lbl = QLabel(str(label))
            key_lbl.setFont(_font(self._sans, 13))
            key_lbl.setStyleSheet(
                f"color: {theme.TEXT_PRIMARY}; background: transparent; border: none;"
            )
            row_lay.addWidget(key_lbl)
            row_lay.addStretch(1)
            value_text = f"{count:,} {unit}"
            size_text = _fmt_gb(size_bytes)
            if size_text:
                value_text += f" · {size_text}"
            val_lbl = QLabel(value_text)
            val_lbl.setFont(_font(self._mono, 12))
            val_lbl.setStyleSheet(
                f"color: {theme.TEXT_MUTED}; background: transparent; border: none;"
            )
            row_lay.addWidget(val_lbl)
            self._backup_card_lay.addWidget(row)

        _clear_layout(self._backup_info_lay)
        backup_dir = _path_str(getattr(plan, "backup_dir", None))
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

    def _populate_premigrate_fallback(self) -> None:
        _clear_layout(self._backup_card_lay)
        _clear_layout(self._backup_info_lay)
        self._backup_info_lay.addWidget(
            _kv_row(
                "สถานะ",
                "ไม่สามารถแสดงรายละเอียดแผนย้ายข้อมูลได้ — ดู log",
                self._sans,
                color=theme.STATE_WARN,
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
            count_lbl.setStyleSheet(
                f"color: {theme.TEXT_FAINT}; background: transparent; border: none;"
            )
            self._phase_count_labels[key] = count_lbl
            row_lay.addWidget(count_lbl, 1)
            self._phase_list.addWidget(row)
        body_lay.addLayout(self._phase_list)

        self._log_box = QLabel("")
        self._log_box.setTextFormat(Qt.TextFormat.RichText)
        self._log_box.setFont(_font(self._mono, 11))
        self._log_box.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; background: transparent; border: none;"
        )
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
        warn_lay.addWidget(warn_icon, 0, Qt.AlignmentFlag.AlignVCenter)
        warn_lbl = QLabel("อย่าปิดโปรแกรมระหว่างนี้ — ถ้าปิด ระบบจะกู้คืนให้เองตอนเปิดครั้งถัดไป")
        warn_lbl.setFont(_font(self._sans, 12))
        warn_lbl.setStyleSheet(f"color: {theme.STATE_WARN}; background: transparent; border: none;")
        # Fixed to ~60% of the footer's usable width (mockup: wraps to 2
        # lines) — without wordWrap, this sentence's single-line sizeHint
        # is wider than the 640px dialog, so the layout can't shrink it and
        # both this label and `_migrate_footer_right` clip instead.
        warn_lbl.setWordWrap(True)
        warn_lbl.setFixedWidth(340)
        warn_lay.addWidget(warn_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        # Both children are fixed-size and neither has a stretch factor, so
        # without this, `warn_wrap`'s own stretch (below, against
        # `_migrate_footer_right`) leaves leftover width *inside* warn_lay
        # too — Qt then spreads that leftover on both sides of each item
        # instead of packing them at the left (confirmed empirically: the
        # icon rendered ~25px right of x=0 without this stretch anchor).
        warn_lay.addStretch(1)
        self._migrate_warn_label = warn_lbl
        self._migrate_footer_right = QLabel("")
        self._migrate_footer_right.setFont(_font(self._sans, 12))
        self._migrate_footer_right.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; background: transparent; border: none;"
        )
        self._migrate_footer_right.setWordWrap(True)
        self._migrate_footer_right.setMaximumWidth(220)
        self._migrate_footer_right.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        outer.addWidget(_footer(self._sans, warn_wrap, []))
        # Right-hand backup-path note shares the footer row — appended after
        # `_footer()` builds it since that helper's `left` slot takes the
        # warning instead here (mirrors the mockup's two-sided footer).
        footer_widget = outer.itemAt(outer.count() - 1).widget()
        footer_widget.layout().addWidget(
            self._migrate_footer_right, 0, Qt.AlignmentFlag.AlignVCenter
        )
        return page

    def _set_footer_right_elided(self, text: str) -> None:
        """Wraps rather than elides (the mockup has no ellipsis rule here) —
        the full backup path stays readable across up to a couple of lines
        instead of losing its `สำรองไว้ที่` prefix or its timestamped
        directory name to a fixed-width single-line clip. Name kept for
        callers/tests; behavior is "wrap", not "elide"."""
        self._migrate_footer_right.setText(text)

    def _set_phase_row_kind(self, key: str, kind: str) -> None:
        """Matches the mockup: the active row's label AND its right-hand
        count both go bright (label bold+`TEXT_PRIMARY_ALT`, count
        `TEXT_PRIMARY`); done/todo stay muted/faint — only the dot
        (`_PhaseDot`) painted the active/done distinction before this,
        leaving every label/count the same dim gray regardless of state."""
        lbl = self._phase_labels.get(key)
        count_lbl = self._phase_count_labels.get(key)
        if lbl is None:
            return
        if kind == "active":
            lbl.setFont(_font(self._sans, 13, 600))
            lbl.setStyleSheet(
                f"color: {theme.TEXT_PRIMARY_ALT}; font-weight: 600; background: transparent; border: none;"
            )
            if count_lbl is not None:
                count_lbl.setStyleSheet(
                    f"color: {theme.TEXT_PRIMARY}; background: transparent; border: none;"
                )
        else:
            lbl.setFont(_font(self._sans, 13))
            lbl.setStyleSheet(f"color: {theme.TEXT_MUTED}; background: transparent; border: none;")
            if count_lbl is not None:
                count_lbl.setStyleSheet(
                    f"color: {theme.TEXT_FAINT}; background: transparent; border: none;"
                )

    def _start_migration(self) -> None:
        self._next_phase_slot = 0
        for i, (key, _) in enumerate(_PHASE_ORDER):
            self._phase_rows[key].set_state("todo", str(i + 1))
            self._set_phase_row_kind(key, "todo")
        plan = self._plan
        if plan is not None:
            # `plan.verify_steps` — optional, not in the documented
            # interface yet (see module docstring): the true validation
            # step count, distinct from `len(promote_items)` (the promoted
            # *category* count, a different number in the mockup — 9
            # categories, 11 validation steps). Falls back to the category
            # count only so this never crashes ahead of the field landing.
            verify_steps = getattr(plan, "verify_steps", None)
            if verify_steps is None:
                verify_steps = len(getattr(plan, "promote_items", []) or [])
            self._phase_count_labels["verify"].setText(f"{verify_steps} ขั้น")
            self._phase_count_labels["archive"].setText(
                f"{len(getattr(plan, 'archive_items', []) or [])} รายการ"
            )
        self._set_header(
            f"กำลังย้ายข้อมูลเป็นโครงใหม่ ({_app_version()}) — ขั้นตอน 1 จาก {len(_PHASE_ORDER)}",
            theme.TEXT_MUTED,
            0,
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
        try:
            self._render_progress_event(event)
        except Exception:
            _LOG.exception("boot_flow_window: progress event render failed")

    def _render_progress_event(self, event: Any) -> None:
        percent = getattr(event, "percent_overall", None)
        if percent is not None:
            self._last_percent = int(percent)
            self._pct_label.setText(f"{int(percent)}%")
            self._migrate_bar.setValue(int(percent))
        eta = _fmt_eta(getattr(event, "eta_s", None))
        if eta:
            self._eta_label.setText(eta)
        order = [k for k, _ in _PHASE_ORDER]
        raw_phase = getattr(event, "phase", None)
        # The real backend sends `phase` as an `int` (1..5, matching
        # `boot_flow._PHASE_LABELS`) — index straight off it so a REPEATED
        # phase number stays on the same row. Round 4 bug: this used to
        # stringify+lowercase first (`str(2)` -> `"2"`, never a key in
        # `_phase_rows`), so every event — including a second event for the
        # SAME phase — fell through to "next never-seen slot", silently
        # advancing to the next phase on every repeat. A string phase key
        # (older/test fixtures, e.g. "promote") still resolves directly too.
        if isinstance(raw_phase, int) and 1 <= raw_phase <= len(order):
            idx = raw_phase - 1
            phase_key = order[idx]
        else:
            phase_key = str(raw_phase or "").lower()
            if phase_key in order:
                idx = order.index(phase_key)
            else:
                idx = min(self._next_phase_slot, len(order) - 1)
                phase_key = order[idx]
        for i, (key, _) in enumerate(_PHASE_ORDER):
            if i < idx:
                self._phase_rows[key].set_state("done")
                self._set_phase_row_kind(key, "done")
            elif i == idx:
                self._phase_rows[key].set_state("active", str(idx + 1))
                self._set_phase_row_kind(key, "active")
            else:
                self._phase_rows[key].set_state("todo", str(i + 1))
                self._set_phase_row_kind(key, "todo")
        self._next_phase_slot = max(self._next_phase_slot, idx + 1)
        self._set_header(
            f"กำลังย้ายข้อมูลเป็นโครงใหม่ ({_app_version()}) — ขั้นตอน {idx + 1} จาก {len(_PHASE_ORDER)}",
            theme.TEXT_MUTED,
            int(percent) if percent is not None else None,
        )
        done = getattr(event, "done", None)
        total = getattr(event, "total", None)
        unit = getattr(event, "unit", "") or ""
        if done is not None and total is not None:
            count_text = f"{done:,} / {total:,} {unit}".strip()
            # `current_path` — optional, not in the documented interface yet
            # (see module docstring): the mockup only appends the "· providers/…"
            # segment to the row that's actively running.
            current_path = getattr(event, "current_path", None)
            if current_path:
                count_text += f" · {_shorten_current_path(str(current_path))}"
            self._phase_count_labels[phase_key].setText(count_text)
        log_line = getattr(event, "log_line", None)
        if log_line:
            current_path_for_log = getattr(event, "current_path", None)
            timestamp, operation, path, detail = _parse_log_line(
                str(log_line), current_path_for_log
            )
            spans = [
                (timestamp, theme.TEXT_FAINT),
                (operation, theme.TEXT_MUTED),
                (path, theme.TEXT_PRIMARY),
                (detail, theme.TEXT_MUTED),
            ]
            rendered = [(text, color) for text, color in spans if text]
            # A single-row, borderless `<table>` — NOT `&nbsp;`-joined
            # spans — is the only way Qt's rich-text engine gives an exact
            # px gap here: `margin`/`padding` on an inline `<span>` is
            # silently ignored (confirmed empirically), but the same
            # `padding-left` on a `<td>` renders 1:1. Round 4 audit V10:
            # 2 literal `&nbsp;` measured ~14px at this font, not the
            # mockup's 8px, and couldn't be tuned to an exact value at all.
            cells = "".join(
                f'<td{td_style}><span style="color: {color};">{html.escape(text)}</span></td>'
                for i, (text, color) in enumerate(rendered)
                for td_style in (' style="padding-left: 8px;"' if i > 0 else "",)
            )
            self._log_box.setText(
                f'<table cellspacing="0" cellpadding="0" border="0"><tr>{cells}</tr></table>'
            )
        backup_dir = getattr(event, "backup_dir", None)
        if backup_dir:
            self._set_footer_right_elided(f"สำรองไว้ที่ {backup_dir}")

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
        done_hint = QLabel(
            f'ดูรายละเอียดได้ที่ <span style="color: {theme.TEXT_MUTED};">Settings → Storage</span>'
        )
        done_hint.setTextFormat(Qt.TextFormat.RichText)
        done_hint.setFont(_font(self._sans, 12))
        done_hint.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; background: transparent; border: none;"
        )
        done_hint.setWordWrap(True)
        outer.addWidget(_footer(self._sans, done_hint, [self._done_log_btn, self._done_open_btn]))
        return page

    def _show_done(self, outcome: Any) -> None:
        self._set_header(f"ย้ายข้อมูลเสร็จแล้ว — พร้อมเปิดใช้งาน {_app_version()}", theme.STATE_OK, 100)
        self._render_safely(
            "done page",
            lambda: self._populate_done_unsafe(outcome),
            self._populate_done_fallback,
        )
        self._show_page(PAGE_DONE, subtitle_only=True)

    def _populate_done_fallback(self) -> None:
        self._done_heading.setText("ย้ายข้อมูลเสร็จแล้ว")
        self._done_sub.setText("")
        _clear_layout(self._done_summary_lay)
        _clear_layout(self._done_paths_lay)
        self._done_summary_lay.addWidget(
            _kv_row(
                "สถานะ",
                "ไม่สามารถแสดงรายละเอียดผลลัพธ์ได้ — ดู log",
                self._sans,
                color=theme.STATE_WARN,
            )
        )

    def _populate_done_unsafe(self, outcome: Any) -> None:
        # `validated_steps` — optional, not in the documented interface yet
        # (see module docstring's backend-assumptions note): the mockup
        # folds the count straight into this heading, not a separate
        # summary row, so an outcome that predates the field just falls
        # back to the old unqualified wording.
        validated_steps = getattr(outcome, "validated_steps", None)
        self._done_heading.setText(
            f"ตรวจสอบครบ {validated_steps} ขั้น — ไม่มีข้อมูลหาย"
            if validated_steps is not None
            else "ตรวจสอบครบทุกขั้น — ไม่มีข้อมูลหาย"
        )
        duration = _fmt_duration(getattr(outcome, "duration_s", None))
        self._done_sub.setText(f"ใช้เวลา {duration}" if duration else "")

        _clear_layout(self._done_summary_lay)
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
        _clear_layout(self._done_paths_lay)
        backup_dir = _path_str(getattr(outcome, "backup_dir", None))
        if backup_dir:
            self._done_paths_lay.addWidget(
                _kv_row("สำรองก่อนย้าย", backup_dir, self._sans, mono=True)
            )
        archive_dir = _path_str(getattr(outcome, "archive_dir", None))
        if archive_dir:
            self._done_paths_lay.addWidget(
                _kv_row("archive ของเก่า", archive_dir, self._sans, mono=True)
            )
        prev = getattr(outcome, "previous_version", None) or _previous_app_version()
        # A single wrapping `_kv_row` rather than `_kv_row_mixed`'s fixed-width
        # segments: with `prev` appended this line is long enough to overflow
        # `_done_paths_box`'s width, and `_kv_row_mixed`'s segments (each
        # `QSizePolicy.Fixed`) can't reflow — they'd rather silently clip
        # mid-word (as `note`'s docstring already found for RichText) than
        # wrap: RichText (see `_kv_row_rich`) rather than plain text, so the
        # command keeps its mono styling inline without `_kv_row_mixed`'s
        # fixed-width segments, which can't reflow and would clip mid-word
        # for a long `prev` instead.
        restore_html = f"รัน <span style=\"font-family: '{self._mono}'; font-size: 12px;\">takkub migrate restore-v1</span>"
        if prev:
            restore_html += f" ก่อนติดตั้ง {html.escape(str(prev))}"
        self._done_paths_lay.addWidget(
            _kv_row_rich("ถ้าต้องกลับเวอร์ชันเดิม", self._sans, restore_html, theme.TEXT_MUTED)
        )

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
        phase_label, phase_number = _phase_label_and_number(getattr(outcome, "failed_phase", None))
        failed_step = getattr(outcome, "failed_step", "") or ""
        error = getattr(outcome, "error", "") or ""
        rolled_back = bool(getattr(outcome, "rolled_back", True))
        data_intact = bool(getattr(outcome, "data_intact", rolled_back))
        # `failed_step_index`/`failed_step_total` — optional, not in the
        # documented interface yet (see module docstring): when present,
        # names which phase-ordinal and which sub-step within it failed
        # (e.g. "ขั้นที่ 3 ตรวจสอบ (7/11) ไม่ผ่าน"); falls back to the plain
        # phase-only heading otherwise.
        step_index = getattr(outcome, "failed_step_index", None)
        step_total = getattr(outcome, "failed_step_total", None)
        if phase_number is not None and step_index is not None and step_total is not None:
            heading = f"ขั้นที่ {phase_number} {phase_label} ({step_index}/{step_total}) ไม่ผ่าน"
        else:
            heading = f"ขั้นตอน {phase_label} ไม่ผ่าน"
        self._show_failed(
            heading=heading,
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

        self._render_safely(
            "failed page info box",
            lambda: self._populate_failed_info(data_intact, outcome),
            self._populate_failed_info_fallback,
        )

        prev = getattr(outcome, "previous_version", None) if outcome is not None else None
        prev = prev or _previous_app_version()
        prev_text = f" ({prev})" if prev else ""
        self._failed_continue_btn.setText(f"ใช้เวอร์ชันเดิมต่อ{prev_text}")
        self._failed_note.setText(
            f"ใช้เวอร์ชันเดิม{prev_text} ต่อได้ทันที หรือส่ง log ให้ทีมดูแล้วค่อยลองใหม่ภายหลัง — ข้อมูลไม่ได้ถูกเปลี่ยน"
        )

        self._show_page(PAGE_FAILED, subtitle_only=True)

    def _populate_failed_info(self, data_intact: bool, outcome: Any) -> None:
        _clear_layout(self._failed_info_lay)
        status_color = theme.STATE_OK if data_intact else theme.STATE_ERROR_BRIGHT
        status_text = (
            "เหมือนก่อนเริ่มทุกไบต์ (ตรวจ sha256 แล้ว)"
            if data_intact
            else "ตรวจสอบไม่ผ่าน — ดู log ก่อนดำเนินการต่อ"
        )
        self._failed_info_lay.addWidget(
            _kv_row("สถานะข้อมูล", status_text, self._sans, color=status_color)
        )
        backup_dir = _path_str(getattr(outcome, "backup_dir", None)) if outcome is not None else ""
        if backup_dir:
            self._failed_info_lay.addWidget(
                _kv_row("สำรองก่อนย้าย", backup_dir, self._sans, mono=True)
            )
        log_paths = (
            [_path_str(p) for p in (getattr(outcome, "log_paths", []) or [])]
            if outcome is not None
            else []
        )
        log_paths = [p for p in log_paths if p]
        if log_paths:
            self._failed_info_lay.addWidget(
                _kv_row("log", " · ".join(log_paths), self._sans, mono=True)
            )

    def _populate_failed_info_fallback(self) -> None:
        _clear_layout(self._failed_info_lay)
        self._failed_info_lay.addWidget(
            _kv_row("สถานะ", "ไม่สามารถแสดงรายละเอียดได้ — ดู log", self._sans, color=theme.STATE_WARN)
        )

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
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths[0])))

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

    def reject(self) -> None:
        """QDialog's Escape-key handling calls `reject()` directly — it
        does NOT go through `closeEvent` (`reject()` -> `done()` skips it
        entirely), so `closeEvent`'s guard above never sees an Escape press.
        Round-2 missed this: Escape during a real migration worker closed
        the dialog and `done()` (below) emitted `flowFinished(True)`,
        letting the gate build the cockpit while the worker was still
        running (#574 fix-loop round 3, B1)."""
        if self._stack.currentIndex() == PAGE_MIGRATING:
            return
        super().reject()

    def accept(self) -> None:
        """Same guard as `reject()`, for symmetry — nothing in this module
        currently calls `accept()` while on the migrating page, but a
        future completion path shouldn't have to remember this rule too."""
        if self._stack.currentIndex() == PAGE_MIGRATING:
            return
        super().accept()

    def done(self, result: int) -> None:
        """Same migrating-page guard as `reject()`/`accept()`/`closeEvent`
        — `accept()`/`reject()` already check this before ever reaching
        here, but `done()` is itself a public `QDialog` API a caller can
        invoke directly, bypassing both (round 4 audit B1 residual: a
        direct `done(0)` call while a real worker was still migrating
        still dismissed the dialog and emitted `flowFinished(True)`)."""
        if self._stack.currentIndex() == PAGE_MIGRATING:
            return
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
                painter.save()
                painter.translate(cx - 10, cy - 10)
                _draw_warn_triangle(painter, 20, theme.STATE_ERROR_BRIGHT)
                painter.restore()

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
