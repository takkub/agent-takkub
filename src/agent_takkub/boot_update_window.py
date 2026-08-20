"""Boot-time provider-update splash — gates MainWindow behind a one-time,
bounded update pass over every installed+enabled provider CLI (#313).

Root incident this whole feature exists to prevent: a provider's own npm
self-update raced a live pane's spawn of its binary, leaving it mid-write —
`_pty_backend`'s pre-flight header check now catches that defensively, but
the better fix is to never let a provider update itself once a pane could
spawn it. `pane_env.inject_provider_no_autoupdate_env` suppresses every
provider's self-update knob for the life of a pane; this module is the other
half — the ONE place updates are allowed to run, once, before any pane
exists.

``TAKKUB_BOOT_UPDATE=0`` skips this whole module (checked by the caller,
`app.py`, before ever constructing `BootUpdateWindow`) — the cockpit then
opens exactly as it did before this feature existed.

Cross-thread safety: `_ProviderUpdateWorker` runs `provider_update.update_provider`
on a `QThreadPool` thread and reports back ONLY via its `finished` Qt signal —
`BootUpdateWindow` never touches a widget from any thread but the one Qt
delivered the signal on (the main thread), the standard PyQt6-safe pattern.
"""

from __future__ import annotations

import os

from PyQt6.QtCore import (
    QObject,
    QPropertyAnimation,
    QRect,
    QRectF,
    QRunnable,
    Qt,
    QThreadPool,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QFontMetrics, QGuiApplication, QPainter, QPen
from PyQt6.QtWidgets import (
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from . import cockpit_theme as theme
from . import provider_model_refresh, provider_update
from .provider_spec import PROVIDER_REGISTRY

# Overall bounded ceiling for the whole boot-update phase. Providers run in
# parallel via QThreadPool, EXCEPT that every `npm install -g` is serialised
# behind one mutex (`provider_update._NPM_LOCK`) because npm has no lock of
# its own for the global prefix — so with several npm providers installed
# this ceiling can cut off a run still queued for that mutex, which lands as
# a "timeout" row and lets the cockpit open (the corrupt binary a truncated
# install could leave behind is still caught at spawn by
# `_pty_backend._validate_spawn_target`). Default sized for a slow-network
# cold install of claude's ~330 MB
# optional-dep binary (the exact payload that made the #313 incident's
# window so wide) with real margin, while still bounding boot — a stuck
# updater must never block the cockpit from opening at all.
_DEFAULT_TIMEOUT_S = 240.0

_STATUS_LABELS = {
    "pending": "รอคิว",
    "updating": "กำลังอัพเดต…",
    provider_update.STATUS_UP_TO_DATE: "เป็นเวอร์ชันล่าสุดอยู่แล้ว",
    provider_update.STATUS_UPDATED: "อัพเดตแล้ว",
    provider_update.STATUS_FAILED: "ล้มเหลว",
    provider_update.STATUS_SKIPPED_NOT_INSTALLED: "ไม่ได้ติดตั้ง",
    provider_update.STATUS_SKIPPED_DISABLED: "ปิดใช้งานอยู่",
    provider_update.STATUS_SKIPPED_NO_MECHANISM: "ไม่มีกลไกอัพเดต",
}

_DONE_STATUSES = frozenset(
    {
        provider_update.STATUS_UP_TO_DATE,
        provider_update.STATUS_UPDATED,
    }
)
_SKIPPED_STATUSES = frozenset(
    {
        provider_update.STATUS_SKIPPED_NOT_INSTALLED,
        provider_update.STATUS_SKIPPED_DISABLED,
        provider_update.STATUS_SKIPPED_NO_MECHANISM,
    }
)


def _boot_update_timeout_s() -> float:
    raw = os.environ.get("TAKKUB_BOOT_UPDATE_TIMEOUT_S", "").strip()
    if not raw:
        return _DEFAULT_TIMEOUT_S
    try:
        val = float(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT_S
    return val if val > 0 else _DEFAULT_TIMEOUT_S


def boot_update_enabled() -> bool:
    """False only when the user explicitly opted out (`TAKKUB_BOOT_UPDATE=0`)."""
    return os.environ.get("TAKKUB_BOOT_UPDATE", "").strip() != "0"


class _WorkerSignals(QObject):
    finished = pyqtSignal(str, str, str, str)  # provider, status, detail, model_note


def _model_note(name: str, binary: str | None) -> str:
    """One-line model-catalog freshness note, or "" when there's nothing to
    show (no pin configured, already fresh, or no discovery mechanism for
    this provider — see provider_model_refresh.NO_MODEL_DISCOVERY_GAPS).
    Only ever called for an already-eligible provider (see `start()`)."""
    if binary is None:
        return ""
    result = provider_model_refresh.refresh_provider_model(name, binary)
    if result.status == provider_model_refresh.STATUS_BUMPED:
        # ↑ (U+2191), not ⬆ (U+2B06) — the heavy arrow isn't in the bundled
        # IBM Plex fonts (critic review 2026-08-20, finding #1); the thin
        # arrow is confirmed present by the same glyph-coverage check.
        return f"model: {result.detail.split(' -> ')[-1]} ↑ updated"
    return ""


class _ProviderUpdateWorker(QRunnable):
    """Run `provider_update.update_provider(name)` (+ its model-catalog
    refresh) off the Qt main thread.

    Mirrors `update_worker.UpdateCheckWorker`'s sibling-QObject-for-signals
    pattern (QRunnable itself cannot carry pyqtSignal)."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name
        self.signals = _WorkerSignals()

    def run(self) -> None:  # QThreadPool thread
        outcome = provider_update.update_provider(self._name)
        binary = provider_update._discover(PROVIDER_REGISTRY[self._name])
        note = _model_note(self._name, binary)
        try:
            self.signals.finished.emit(outcome.provider, outcome.status, outcome.detail, note)
        except RuntimeError:
            pass  # receiver torn down mid-flight (window closed) — nothing to deliver to


class _StatusIcon(QWidget):
    """Painted status mark — filled circle + check/x, or a ring for the
    not-yet-terminal states. Replaces the old `✓ `/`✗ ` text prefix (critic
    review 2026-08-20, finding #1): those glyphs (and the model-note's old
    `⬆`) sit outside the bundled IBM Plex fonts' coverage and rendered as
    empty boxes. Painting the mark with `QPainter` sidesteps font-glyph
    coverage entirely — same idea as the on-disk-SVG spinbox arrows in
    `cockpit_theme.py`, but drawn directly since these are simple shapes and
    need per-instance color (role/state color varies per row)."""

    _KIND_PENDING = "pending"
    _KIND_UPDATING = "updating"
    _KIND_OK = "ok"
    _KIND_ERROR = "error"
    _KIND_SKIPPED = "skipped"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(14, 14)
        self._kind = self._KIND_PENDING

    def set_kind(self, kind: str) -> None:
        if kind == self._kind:
            return
        self._kind = kind
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(1, 1, -1, -1)
        cx, cy = rect.center().x(), rect.center().y()
        kind = self._kind

        if kind == self._KIND_OK:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.STATE_OK))
            painter.drawEllipse(rect)
            mark = QPen(QColor(theme.GROUND_WINDOW))
            mark.setWidth(2)
            mark.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(mark)
            painter.drawLine(cx - 3, cy, cx - 1, cy + 3)
            painter.drawLine(cx - 1, cy + 3, cx + 4, cy - 3)
        elif kind == self._KIND_ERROR:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.STATE_ERROR))
            painter.drawEllipse(rect)
            mark = QPen(QColor(theme.GROUND_WINDOW))
            mark.setWidth(2)
            mark.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(mark)
            painter.drawLine(cx - 3, cy - 3, cx + 3, cy + 3)
            painter.drawLine(cx - 3, cy + 3, cx + 3, cy - 3)
        elif kind == self._KIND_UPDATING:
            ring = QPen(QColor(theme.ACCENT_GOLD))
            ring.setWidth(2)
            painter.setPen(ring)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(rect)
        elif kind == self._KIND_SKIPPED:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.TEXT_FAINT))
            painter.drawEllipse(rect.adjusted(4, 4, -4, -4))
        else:  # pending
            ring = QPen(QColor(theme.TEXT_MUTED))
            ring.setWidth(1)
            painter.setPen(ring)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(rect)


class _RoleDot(QWidget):
    """Small painted circle — the provider's role-color identity marker.

    Was a `QLabel("●", ...)` (U+25CF BLACK CIRCLE). Applying the bundled
    IBM Plex font to it (finding #4) turned it into an empty tofu box too —
    caught live while rendering this fix, not in the critic's original
    glyph table, but the exact same class of bug as finding #1. Painting it
    closes off font-glyph coverage as a risk for every mark in this row, not
    just the ones already caught."""

    def __init__(self, color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = QColor(color)
        self.setFixedSize(14, 14)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._color)
        painter.drawEllipse(self.rect().center(), 4, 4)


class _ProviderRow(QWidget):
    """One provider's badge + name + status text + indeterminate progress bar."""

    def __init__(
        self,
        name: str,
        display_name: str,
        color: str,
        sans: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.name = name
        self.setObjectName("bootUpdateRow")

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(10)

        dot = _RoleDot(color, self)
        row.addWidget(dot)

        # Elided (not just clipped) — the multi-provider directive (#103)
        # means this list only grows, and a future display name longer than
        # the fixed column would otherwise silently overflow/clip.
        name_font = _font(sans, 13, 600)
        elided_name = QFontMetrics(name_font).elidedText(
            display_name, Qt.TextElideMode.ElideRight, 90
        )
        name_label = QLabel(elided_name, self)
        name_label.setStyleSheet(f"color: {theme.TEXT_PRIMARY};")
        name_label.setFont(name_font)
        name_label.setFixedWidth(90)
        if elided_name != display_name:
            name_label.setToolTip(display_name)
        row.addWidget(name_label)

        self._status_label = QLabel(_STATUS_LABELS["pending"], self)
        self._status_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        self._status_label.setFont(_font(sans, 13))
        self._status_label.setWordWrap(True)
        row.addWidget(self._status_label, 1)

        self._icon = _StatusIcon(self)
        row.addWidget(self._icon)

        self._bar = QProgressBar(self)
        self._bar.setRange(0, 0)  # indeterminate — never a fabricated percent
        # Widened from 70->110 and 6->8px tall — the "something is
        # happening" signal, not an afterthought competing with the status
        # text for a 6px sliver (critic review 2026-08-20, finding #10).
        self._bar.setFixedWidth(110)
        self._bar.setFixedHeight(8)
        self._bar.setTextVisible(False)
        self._bar.setStyleSheet(
            f"QProgressBar {{ background: {theme.GROUND_INPUT}; border: none; "
            f"border-radius: 4px; }} "
            f"QProgressBar::chunk {{ background: {theme.ACCENT_GOLD}; border-radius: 4px; }}"
        )
        self._bar.setVisible(False)
        row.addWidget(self._bar)

        self._opacity = QGraphicsOpacityEffect(self._status_label)
        self._status_label.setGraphicsEffect(self._opacity)
        self._opacity.setOpacity(1.0)

    def set_status(self, status: str, detail: str, model_note: str = "") -> None:
        label = _STATUS_LABELS.get(status, status)
        self._bar.setVisible(status == "updating")
        if status in _DONE_STATUSES:
            color, kind = theme.STATE_OK, _StatusIcon._KIND_OK
        elif status == provider_update.STATUS_FAILED:
            color, kind = theme.STATE_ERROR, _StatusIcon._KIND_ERROR
        elif status in _SKIPPED_STATUSES:
            color, kind = theme.TEXT_FAINT, _StatusIcon._KIND_SKIPPED
        elif status == "updating":
            color, kind = theme.ACCENT_GOLD, _StatusIcon._KIND_UPDATING
        else:
            color, kind = theme.TEXT_MUTED, _StatusIcon._KIND_PENDING
        self._icon.set_kind(kind)
        text = label
        if detail and status not in ("pending", "updating"):
            text += f" — {detail}"
        if model_note:
            text += f"\n{model_note}"
        self._status_label.setText(text)
        self._status_label.setStyleSheet(f"color: {color};")
        self._status_label.setToolTip(detail or "")

        # Soft fade on every status transition — the "smooth transition"
        # premium touch (user directive 2026-08-20), kept to one cheap
        # QPropertyAnimation per row rather than a whole animation framework.
        anim = QPropertyAnimation(self._opacity, b"opacity", self)
        anim.setDuration(220)
        anim.setStartValue(0.35)
        anim.setEndValue(1.0)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def sync_status_height(self) -> None:
        """Pin an exact minimum height on the status label, computed
        directly from `QFontMetrics` at the label's actual current width.

        Qt's automatic height-for-width propagation through two nested
        layouts (`BootUpdateWindow`'s outer `QVBoxLayout` -> this row's own
        `QHBoxLayout` -> the wrapping `QLabel`) undercounts once the text
        wraps to 3+ lines — proven by stress-testing all 6 providers with a
        status detail *and* a model-catalog note at once: `totalHeightForWidth`
        claimed enough room, rows didn't overlap by that measure, but the
        label's own text still visually overflowed past its row into the
        next one. Measuring directly from the font/text/actual-width, the
        same inputs Qt's own word-wrap painter uses, sidesteps that instead
        of trying to out-guess the layout engine's approximation.

        Must be called only after this row itself already has a real
        (non-zero) width assigned by one prior *outer* layout pass — see
        `BootUpdateWindow._resize_to_content`. That outer pass settles this
        row's own geometry but not its *children's* — activating this row's
        own `QHBoxLayout` explicitly is what actually splits the columns
        (dot/name/status/icon/bar) using this row's real current width,
        without waiting for an event-loop resize cycle."""
        inner = self.layout()
        if inner is not None:
            inner.activate()
        width = self._status_label.width()
        if width <= 0:
            return
        metrics = self._status_label.fontMetrics()
        needed = metrics.boundingRect(
            QRect(0, 0, width, 0), Qt.TextFlag.TextWordWrap, self._status_label.text()
        ).height()
        self._status_label.setMinimumHeight(needed)


class BootUpdateWindow(QWidget):
    """Frameless splash shown while boot-time provider updates run.

    Usage: construct, connect `finished`, call `start()`. Emits `finished`
    exactly once, either when every eligible provider reaches a terminal
    state or when the bounded timeout fires (whichever comes first) — the
    caller (`app.py`) then proceeds to construct/show MainWindow regardless
    of outcome; a stuck/slow updater must never block the cockpit forever.
    """

    finished = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        # True, not False: a `border-radius`-styled background only looks
        # rounded if the native window surface itself is translucent —
        # otherwise the opaque rectangular surface shows through as square
        # artifacts outside the rounded fill at each corner (textbook Qt
        # frameless-window pitfall; critic review 2026-08-20, finding #3).
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # Width is the only fixed dimension — height is layout-driven
        # (`_resize_to_content`, called after every `set_status()`), not a
        # row-count formula. The old `setFixedSize` formula assumed at most
        # one wrapped model-note line across the whole splash; every
        # provider getting a bump note in the same boot grew rows past that
        # budget and text overlapped the row below (critic review
        # 2026-08-20, finding #2, reproduced).
        self.setFixedWidth(480)
        self.setObjectName("bootUpdateWindow")
        # NOT a QSS `background`/`border-radius` rule: proven live (probed
        # all 4 combinations of WA_TranslucentBackground x WA_StyledBackground
        # offscreen) that once WA_TranslucentBackground is True, Qt stops
        # auto-filling a top-level widget's background from its stylesheet
        # entirely — WA_StyledBackground doesn't bring it back either. A
        # translucent top-level widget must paint its own background; see
        # `paintEvent` below.

        fonts = theme.ensure_fonts_loaded()
        sans = fonts["sans"]

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QVBoxLayout()
        header.setContentsMargins(20, 20, 20, 12)
        header.setSpacing(4)
        title = QLabel("Takkub Cockpit", self)
        title.setStyleSheet(f"color: {theme.TEXT_PRIMARY_ALT}; font-size: 16px; font-weight: 700;")
        title.setFont(_font(sans, 16, 700))
        # No brand mark: no app-icon/wordmark asset exists elsewhere in the
        # repo to echo here (critic review 2026-08-20, finding #8) — adding
        # one would mean inventing a new asset, out of scope for this pass.
        subtitle = QLabel("กำลังตรวจสอบอัพเดต provider ก่อนเปิดใช้งาน…", self)
        subtitle.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 12px;")
        subtitle.setFont(_font(sans, 12))
        header.addWidget(title)
        header.addWidget(subtitle)
        outer.addLayout(header)

        # Aggregate progress across every eligible provider — a full-width
        # gold bar reads at a glance without parsing the Thai footer text,
        # matching how native installers signal overall progress vs.
        # per-item status (critic review 2026-08-20, finding #7). Range is
        # set for real once `start()` knows the eligible count; hidden until
        # then so it never flashes an empty/indeterminate bar.
        self._agg_bar = QProgressBar(self)
        self._agg_bar.setFixedHeight(4)
        self._agg_bar.setTextVisible(False)
        self._agg_bar.setStyleSheet(
            f"QProgressBar {{ background: {theme.GROUND_INPUT}; border: none; }} "
            f"QProgressBar::chunk {{ background: {theme.ACCENT_GOLD}; }}"
        )
        self._agg_bar.setVisible(False)
        outer.addWidget(self._agg_bar)

        sep = QWidget(self)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {theme.BORDER_HAIRLINE};")
        outer.addWidget(sep)

        self._rows: dict[str, _ProviderRow] = {}
        for name, spec in PROVIDER_REGISTRY.items():
            color = (
                theme.ACCENT_GOLD
                if name == "claude"
                else theme.ROLE_COLORS.get(name, theme.ROLE_COLOR_FALLBACK)
            )
            row = _ProviderRow(name, spec.display_name or spec.name.capitalize(), color, sans, self)
            self._rows[name] = row
            outer.addWidget(row)

        sep2 = QWidget(self)
        sep2.setFixedHeight(1)
        sep2.setStyleSheet(f"background: {theme.BORDER_HAIRLINE};")
        outer.addWidget(sep2)

        footer = QHBoxLayout()
        footer.setContentsMargins(20, 12, 20, 16)
        self._footer_label = QLabel("", self)
        self._footer_label.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 11px;")
        self._footer_label.setFont(_font(sans, 11))
        footer.addWidget(self._footer_label)
        outer.addLayout(footer)

        self._pending: set[str] = set()
        self._emitted_finished = False
        self._timeout_timer = QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.timeout.connect(self._on_timeout)

        # Window-level entrance/exit fade (critic review 2026-08-20, finding
        # #6) — bookends the per-row status-transition fade (`_ProviderRow.
        # set_status`) so the splash itself doesn't just snap into/out of
        # existence. Starts at 0 opacity; `showEvent` fades it in.
        self._window_opacity = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._window_opacity)
        self._window_opacity.setOpacity(0.0)

        self._resize_to_content()

    def _center_on_screen(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.move(
            geo.x() + (geo.width() - self.width()) // 2,
            geo.y() + (geo.height() - self.height()) // 2,
        )

    def _resize_to_content(self) -> None:
        """Height re-derives from actual content, not a row-count formula
        (critic review 2026-08-20, finding #2). Called after every
        `set_status()` so a row that grows to a wrapped 2nd/3rd line never
        overlaps the row below it; width stays pinned via `setFixedWidth`.

        Plain `adjustSize()`/`sizeHint()` under-measures here: per the Qt
        docs, a layout's `sizeHint()` derives its height-for-width using the
        layout's own *unconstrained* preferred width, not the widget's
        actual fixed width — since this window's content wants more than
        480px wide when nothing wraps, that overestimates available width
        and undercounts wrap height. `layout().totalHeightForWidth(480)`
        fixes that first-order error, but the *nested* heightForWidth chain
        (this window's QVBoxLayout -> each row's own QHBoxLayout -> its
        wrapping QLabel) still undercounts once a row's text wraps to 3+
        lines (stress-tested: all 6 providers with both a detail and a
        model-note at once). So this runs two passes: the first settles
        each row's real column width; the second re-measures every row's
        label height directly from its font/text/now-real width
        (`_ProviderRow.sync_status_height`) and lets THAT drive the final
        resize, rather than trusting the layout engine's own estimate.
        """
        layout = self.layout()
        if layout is None:
            self.resize(self.width(), self.sizeHint().height())
            self._center_on_screen()
            return
        self.resize(self.width(), layout.totalHeightForWidth(self.width()))
        layout.activate()
        for row in self._rows.values():
            row.sync_status_height()
        layout.activate()
        # Force child row geometries to settle synchronously, independent of
        # the event loop actually running a paint/resize cycle — without
        # this, a caller measuring row geometry right after this call (e.g.
        # a status change arriving before the splash is shown/processed)
        # would still read stale positions from before the resize.
        self.resize(self.width(), layout.totalHeightForWidth(self.width()))
        layout.activate()
        self._center_on_screen()

    def paintEvent(self, _event) -> None:
        """Paint the rounded dark panel directly — see the comment on
        `WA_TranslucentBackground` in `__init__`: a QSS `background`/
        `border-radius` rule on this widget's objectName never actually
        paints once translucency is on, so this window owns the fill
        itself rather than depending on it."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(QColor(255, 255, 255, 15), 1))
        painter.setBrush(QColor(theme.GROUND_WINDOW))
        painter.drawRoundedRect(rect, theme.RADIUS_LG, theme.RADIUS_LG)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._window_opacity.opacity() == 0.0:
            anim = QPropertyAnimation(self._window_opacity, b"opacity", self)
            anim.setDuration(220)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def close_animated(self) -> None:
        """Fade out, then close — the exit half of the window-level fade
        (finding #6). Uses a short bounded nested `QEventLoop` to let the
        animation actually paint frames before `close()`, the same
        local-event-loop idiom `run_boot_update_gate` already uses for the
        splash's whole run — never a nested `QApplication.exec()`."""
        from PyQt6.QtCore import QEventLoop

        anim = QPropertyAnimation(self._window_opacity, b"opacity", self)
        anim.setDuration(160)
        anim.setStartValue(self._window_opacity.opacity())
        anim.setEndValue(0.0)
        loop = QEventLoop()
        anim.finished.connect(loop.quit)
        anim.start()
        loop.exec()
        self.close()

    def start(self) -> None:
        """Kick off one QThreadPool worker per eligible provider. Providers
        that are not installed or are disabled resolve instantly (no
        subprocess/network cost — `eligibility_gap` is a PATH probe only)."""
        eligible = set(provider_update.eligible_providers())
        for name, row in self._rows.items():
            if name in eligible:
                row.set_status("updating", "")
            else:
                gap = provider_update.eligibility_gap(name)
                status = (
                    gap.status if gap is not None else provider_update.STATUS_SKIPPED_NOT_INSTALLED
                )
                row.set_status(status, "")

        self._pending = set(eligible)
        self._refresh_footer()
        if not self._pending:
            self._finish()
            return
        for name in eligible:
            worker = _ProviderUpdateWorker(name)
            worker.signals.finished.connect(self._on_provider_finished)
            QThreadPool.globalInstance().start(worker)
        self._timeout_timer.start(int(_boot_update_timeout_s() * 1000))

    def _on_provider_finished(
        self, name: str, status: str, detail: str, model_note: str = ""
    ) -> None:
        # NOT `from .orchestrator import _log_event` (the re-export facade) —
        # orchestrator.py transitively imports agent_pane -> terminal_widget
        # -> QtWebEngineWidgets, which raises if a QCoreApplication already
        # exists when it's FIRST imported (it must be imported before any Qt
        # application instance). In the real app.main() boot sequence
        # orchestrator is already imported earlier (pre-QApplication) so this
        # is currently harmless there — but this splash's own QApplication
        # already exists by the time these callbacks fire, so relying on that
        # ordering accident is fragile. orchestrator_text.py owns the actual
        # `_log_event` definition and has zero Qt imports of its own.
        from .orchestrator_text import _log_event

        row = self._rows.get(name)
        if row is not None:
            row.set_status(status, detail, model_note)
        self._pending.discard(name)
        try:
            _log_event(
                "boot_update_provider",
                provider=name,
                status=status,
                detail=detail[:200],
                model_note=model_note,
            )
        except Exception:
            pass
        self._refresh_footer()
        if not self._pending:
            self._finish()

    def _on_timeout(self) -> None:
        # NOT `from .orchestrator import _log_event` (the re-export facade) —
        # orchestrator.py transitively imports agent_pane -> terminal_widget
        # -> QtWebEngineWidgets, which raises if a QCoreApplication already
        # exists when it's FIRST imported (it must be imported before any Qt
        # application instance). In the real app.main() boot sequence
        # orchestrator is already imported earlier (pre-QApplication) so this
        # is currently harmless there — but this splash's own QApplication
        # already exists by the time these callbacks fire, so relying on that
        # ordering accident is fragile. orchestrator_text.py owns the actual
        # `_log_event` definition and has zero Qt imports of its own.
        from .orchestrator_text import _log_event

        for name in list(self._pending):
            row = self._rows.get(name)
            if row is not None:
                row.set_status(provider_update.STATUS_FAILED, "timeout")
            try:
                _log_event("boot_update_provider", provider=name, status="timeout")
            except Exception:
                pass
        self._pending.clear()
        self._refresh_footer()
        self._finish()

    def _refresh_footer(self) -> None:
        total = len(provider_update.eligible_providers())
        done = total - len(self._pending)
        self._footer_label.setText(f"เสร็จ {done}/{total} ตัว" if total else "ไม่มี provider ที่ต้องอัพเดต")
        self._agg_bar.setVisible(total > 0)
        if total:
            self._agg_bar.setRange(0, total)
            self._agg_bar.setValue(done)
        # Row text can wrap/unwrap on every status change (a model-catalog
        # note appearing, or a detail string arriving) — re-derive the
        # window height every time, not just once at construction.
        self._resize_to_content()

    def _finish(self) -> None:
        if self._emitted_finished:
            return
        self._emitted_finished = True
        self._timeout_timer.stop()
        self.finished.emit()


def _font(family: str, size: int, weight: int = 400):
    from PyQt6.QtGui import QFont

    f = QFont(family, size)
    f.setWeight(QFont.Weight(weight))
    return f


def run_boot_update_gate(main_window_factory):
    """Show the splash, run it to completion (or timeout) via a local
    `QEventLoop` — NOT a nested `QApplication.exec()` — then construct and
    return the main window. Caller must check `boot_update_enabled()` before
    calling in; this function always runs the splash unconditionally.

    ``splash.start()`` is deferred one event-loop tick via
    ``QTimer.singleShot(0, ...)`` rather than called inline before
    ``loop.exec()``: when there are zero eligible providers (nothing
    installed/enabled — a real state, e.g. a fresh machine with no provider
    CLI yet), ``start()`` finishes and emits ``finished`` SYNCHRONOUSLY.
    ``QEventLoop.quit()`` called before the loop is actually running is a
    documented no-op (nothing to quit yet), so ``loop.exec()`` right after
    would then block forever waiting for a signal that already fired. Firing
    ``start()`` from inside the loop instead guarantees ``quit()`` always
    lands on a loop that is genuinely running.
    """
    from PyQt6.QtCore import QEventLoop, QTimer

    splash = BootUpdateWindow()
    loop = QEventLoop()
    splash.finished.connect(loop.quit)
    splash.show()
    QTimer.singleShot(0, splash.start)
    loop.exec()
    window = main_window_factory()
    splash.close_animated()
    return window
