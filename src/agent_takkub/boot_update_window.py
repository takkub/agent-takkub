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

from PyQt6.QtCore import QObject, QPropertyAnimation, QRunnable, Qt, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtGui import QGuiApplication
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

# Overall bounded ceiling for the whole boot-update phase (all providers run
# in parallel via QThreadPool, so this is ~one provider's worst case, not a
# sum). Default sized for a slow-network cold install of claude's ~330 MB
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
        return f"model: {result.detail.split(' -> ')[-1]} ⬆ updated"
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


class _ProviderRow(QWidget):
    """One provider's badge + name + status text + indeterminate progress bar."""

    def __init__(
        self, name: str, display_name: str, color: str, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.name = name
        self.setObjectName("bootUpdateRow")

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(10)

        dot = QLabel("●", self)
        dot.setStyleSheet(f"color: {color}; font-size: 11px;")
        dot.setFixedWidth(14)
        row.addWidget(dot)

        name_label = QLabel(display_name, self)
        name_label.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; font-weight: 600;")
        name_label.setFixedWidth(90)
        row.addWidget(name_label)

        self._status_label = QLabel(_STATUS_LABELS["pending"], self)
        self._status_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        self._status_label.setWordWrap(True)
        row.addWidget(self._status_label, 1)

        self._bar = QProgressBar(self)
        self._bar.setRange(0, 0)  # indeterminate — never a fabricated percent
        self._bar.setFixedWidth(70)
        self._bar.setFixedHeight(6)
        self._bar.setTextVisible(False)
        self._bar.setStyleSheet(
            f"QProgressBar {{ background: {theme.GROUND_INPUT}; border: none; "
            f"border-radius: 3px; }} "
            f"QProgressBar::chunk {{ background: {theme.ACCENT_GOLD}; border-radius: 3px; }}"
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
            color, prefix = theme.STATE_OK, "✓ "
        elif status == provider_update.STATUS_FAILED:
            color, prefix = theme.STATE_ERROR, "✗ "
        elif status in _SKIPPED_STATUSES:
            color, prefix = theme.TEXT_FAINT, ""
        elif status == "updating":
            color, prefix = theme.ACCENT_GOLD, ""
        else:
            color, prefix = theme.TEXT_MUTED, ""
        text = f"{prefix}{label}"
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
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        # 54px/row (not 46) leaves headroom for the occasional second line —
        # a model-catalog bump note ("model: gemini-3.7-flash-medium ⬆
        # updated") renders under the status text on the rare row that has
        # one, without a full dynamic-height layout system for this splash.
        self.setFixedSize(480, 60 + 56 * len(PROVIDER_REGISTRY) + 70)
        self.setObjectName("bootUpdateWindow")
        self.setStyleSheet(
            f"#bootUpdateWindow {{ background: {theme.GROUND_WINDOW}; "
            f"border: 1px solid {theme.BORDER_HAIRLINE}; border-radius: {theme.RADIUS_LG}px; }}"
        )

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
        subtitle = QLabel("กำลังตรวจสอบอัพเดต provider ก่อนเปิดใช้งาน…", self)
        subtitle.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 12px;")
        subtitle.setFont(_font(sans, 12))
        header.addWidget(title)
        header.addWidget(subtitle)
        outer.addLayout(header)

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
            row = _ProviderRow(name, spec.display_name or spec.name.capitalize(), color, self)
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
        footer.addWidget(self._footer_label)
        outer.addLayout(footer)

        self._pending: set[str] = set()
        self._emitted_finished = False
        self._timeout_timer = QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.timeout.connect(self._on_timeout)

        self._center_on_screen()

    def _center_on_screen(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.move(
            geo.x() + (geo.width() - self.width()) // 2,
            geo.y() + (geo.height() - self.height()) // 2,
        )

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
    splash.close()
    return window
