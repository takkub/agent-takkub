"""PreviewHost: the single, app-wide Live Preview `QWebEngineView` (#365
phase 5 widget + phase 6 Design Director UI on top of it).

RAM rule (`docs/plans/2026-08-23-master-dev-plan.md` §4, the same rule
`editor_widget.py`'s `EditorHost` implements for Monaco — read that module's
docstring first, this one mirrors its shape almost exactly): there is
exactly ONE Preview `QWebEngineView` for the whole app, parked in a fixed
dock outside every `ProjectTab`. Switching which project's preview is on
screen switches *content* (a fresh `load()` on the one existing view), never
spawns a second WebView. Lazily created on first `show_state()` call, fully
torn down (`deleteLater` on page + view + profile) the moment the currently
shown project's preview closes — a session that never opens Preview pays
+0 RAM. Unlike `EditorHost` there is no multi-tab bookkeeping: only one
project can be on screen at a time, so `show_state`/`close_project` are the
whole lifecycle surface `main_window.py` needs to drive from the
Orchestrator's `previewOpened`/`previewUpdated`/`previewClosed` signals
(a 1:1 proxy of `PreviewController`'s own signals — see that module's
docstring for why the controller itself stays widget-free).

Security (`12_SECURITY_THREAT_MODEL.md`): no `QWebChannel` is ever
constructed anywhere in this module — not for a URL-mode dev-server page,
not for a file-mode local artifact either, matching the threat model's "no
privileged bridge on arbitrary remote pages" (a future dev server could
serve attacker-controlled content; a bridge would hand it Python access).
Every in-page navigation (link click, JS redirect, a dev server's own
hot-reload) is gated through `PreviewHost.navigation_allowed`, which is
exactly `preview_controller.navigation_allowed` applied to the state
currently on screen — same-origin only in URL mode, the exact open file
only in file mode. `_PreviewWebView` gets a fresh off-the-record
`QWebEngineProfile` (no persistent cookies/storage, isolated from every
other WebView in the app) rather than the shared default profile.

No comments about the RAM hard rule are duplicated per line below the way
`editor_widget.py` doesn't either — see that module + the master plan for
the numbers.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from PyQt6.QtCore import QObject, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import performance_settings
from .design_actions import DesignArtifactRegistry
from .preview_controller import DEVICE_PRESETS, PreviewState
from .preview_controller import navigation_allowed as _controller_navigation_allowed

logger = logging.getLogger(__name__)

# width, height per preset; None (desktop) means "fill whatever space the
# dock gives it" rather than a fixed viewport.
DEVICE_FRAME_SIZES: dict[str, tuple[int, int] | None] = {
    "desktop": None,
    "tablet": (768, 1024),
    "mobile": (375, 812),
}
_QWIDGETSIZE_MAX = 16_777_215  # Qt's own QWIDGETSIZE_MAX, not exposed to Python bindings

_NON_TERMINAL_ARTIFACT_STATUSES = frozenset({"draft", "review", "revision_requested"})

# #364 lever 1, applied to Preview the same way terminal_widget.py applies it
# to a hidden pane — see that module's set_keepalive()/_begin_discard() for
# the fuller story. Preview has no scrollback to snapshot (a reattach is
# just "reload the same target"), so this copy is deliberately simpler.
_DISCARD_DEBOUNCE_MS_DEFAULT = 25_000


def _discard_debounce_ms() -> int:
    try:
        return int(os.environ.get("TAKKUB_PANE_DISCARD_DEBOUNCE_MS", _DISCARD_DEBOUNCE_MS_DEFAULT))
    except ValueError:
        return _DISCARD_DEBOUNCE_MS_DEFAULT


def _env_pane_discard_override(persisted: bool) -> bool:
    """Same disable-token vocabulary/precedence as `agent_pane.py`'s
    `_env_pane_discard_override` — kept as a separate small copy rather than
    a cross-module import of a private helper, same convention
    `editor_widget.py`'s `_reveal_in_file_manager` docstring documents."""
    raw = os.environ.get("TAKKUB_PANE_DISCARD", "").strip().lower()
    if raw in {"0", "off", "false", "no"}:
        return False
    if raw in {"1", "on", "true", "yes"}:
        return True
    return bool(persisted)


def _default_artifact_lookup(project: str, target: str) -> dict | None:
    """Best-effort: the latest design artifact (any status) whose recorded
    `target` matches the Preview state currently on screen, so the header
    can show its title/status (phase 6: "สถานะ artifact แสดงใน Preview
    header"). `PreviewState` carries no `artifact_id` of its own —
    `preview_controller.py` predates `design_actions.py` in the dependency
    order and stays free of it (see that module's docstring) — so this is a
    lookup by value rather than a stored foreign key. Returns None for a
    preview that was never published as a design artifact (a plain dev
    server or an ad-hoc `takkub preview open-file`)."""
    try:
        artifacts = DesignArtifactRegistry(project).all()
    except Exception as exc:  # defensive — a malformed/missing store must never block Preview
        logger.debug("preview_widget: artifact lookup failed for %s: %s", project, exc)
        return None
    matches = [a for a in artifacts if a.target == target]
    if not matches:
        return None
    matches.sort(key=lambda a: a.created_at or "")
    return matches[-1].as_dict()


# ---------------------------------------------------------------------------
# QWebEnginePage — navigation policy gate, never a QWebChannel host
# ---------------------------------------------------------------------------


class _PreviewPage(QWebEnginePage):
    """Every top-level navigation Chromium is about to follow on this page —
    including each hop of a server-side HTTP redirect, which Chromium
    re-queries `acceptNavigationRequest` for individually — is checked
    against `nav_check` (`PreviewHost.navigation_allowed`, injected at
    construction) before it's allowed through; `nav_type` is never filtered
    on, so redirect coverage is automatic rather than a separate hook.
    Sub-frame navigations (iframes) pass through unchecked — the threat
    model's concern is the top-level page a user could be redirected off of,
    not embedded content already sandboxed by the browser's own frame
    isolation. New-window/popup requests (`target="_blank"`, `window.open()`)
    never reach `nav_check` at all: this class deliberately has no
    `createWindow` override, so `QWebEnginePage`'s own default (returns
    `None`, silently refusing the popup — the same fail-closed behavior
    `terminal_widget.py`'s `_on_open_url` docstring already documents for
    the terminal's WebEngine view) applies, matching the app-wide "exactly
    ONE Preview widget, never a second window" rule. Full contract pinned in
    `preview_controller.navigation_allowed`'s docstring."""

    def __init__(
        self,
        profile: QWebEngineProfile,
        parent: QObject,
        nav_check: Callable[[str], bool],
        on_blocked: Callable[[str], None],
    ) -> None:
        super().__init__(profile, parent)
        self._nav_check = nav_check
        self._on_blocked = on_blocked

    def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame: bool) -> bool:
        if not is_main_frame:
            return True
        try:
            allowed = bool(self._nav_check(url.toString()))
        except (
            Exception
        ) as exc:  # defensive — a policy-check bug must fail closed, not crash Chromium's call-in
            logger.debug("preview_widget: nav_check raised for %s: %s", url.toString(), exc)
            allowed = False
        if not allowed:
            self._on_blocked(url.toString())
        return allowed


class _PreviewWebView(QWidget):
    """Owns exactly one `QWebEngineView` + a private off-the-record
    `QWebEngineProfile`, wrapped in a `QScrollArea` so a fixed device-preset
    size (tablet/mobile) can exceed the dock's own size without clipping.
    Never reparented after first paint — same Windows Chromium constraint
    `editor_widget.py`/`terminal_widget.py` each document for their own
    `QWebEngineView`."""

    navigationBlocked = pyqtSignal(str)  # attempted url

    def __init__(self, nav_check: Callable[[str], bool], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._scroll = QScrollArea(self)
        self._scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._scroll.setWidgetResizable(True)
        layout.addWidget(self._scroll)

        self._profile = QWebEngineProfile(self)  # no storage name => off-the-record
        self._view = QWebEngineView()
        self._page = _PreviewPage(self._profile, self._view, nav_check, self.navigationBlocked.emit)
        self._view.setPage(self._page)
        self._scroll.setWidget(self._view)
        self._discarded = False

    def load_url(self, url: str) -> None:
        self._view.setUrl(QUrl(url))

    def load_file(self, path: str) -> None:
        self._view.setUrl(QUrl.fromLocalFile(path))

    def reload(self) -> None:
        self._view.reload()

    def apply_device(self, device: str) -> None:
        size = DEVICE_FRAME_SIZES.get(device)
        if size is None:
            self._view.setMinimumSize(0, 0)
            self._view.setMaximumSize(_QWIDGETSIZE_MAX, _QWIDGETSIZE_MAX)
            self._scroll.setWidgetResizable(True)
        else:
            self._scroll.setWidgetResizable(False)
            self._view.setFixedSize(*size)

    def destroy(self) -> None:
        """Full teardown so Chromium can release the renderer + JS heap —
        distinct from `discard()`, used when the currently-shown project's
        preview actually closes."""
        for obj in (self._page, self._view, self._profile):
            try:
                obj.deleteLater()
            except RuntimeError:
                pass
        self.deleteLater()

    def discard(self) -> None:
        if self._discarded:
            return
        try:
            self._page.setLifecycleState(QWebEnginePage.LifecycleState.Discarded)
        except Exception as exc:  # defensive — never let a lever-1 discard crash the app
            logger.debug("preview_widget: discard failed: %s", exc)
            return
        self._discarded = True

    def reattach(self) -> None:
        if not self._discarded:
            return
        self._discarded = False
        try:
            self._view.page().setLifecycleState(QWebEnginePage.LifecycleState.Active)
        except Exception as exc:
            logger.debug("preview_widget: reattach failed: %s", exc)


# ---------------------------------------------------------------------------
# PreviewHost — the app-wide singleton MainWindow owns
# ---------------------------------------------------------------------------


class PreviewHost(QObject):
    """Lazily creates/destroys the one app-wide `_PreviewWebView` inside a
    caller-owned container widget (MainWindow's preview dock).

    `view_factory` defaults to `_PreviewWebView` (a real QWebEngineView);
    tests pass a stub with the same `load_url`/`load_file`/`reload`/
    `apply_device`/`discard`/`reattach`/`destroy`/`navigationBlocked`
    surface so lifecycle + navigation-policy + device-preset logic is
    exercised without constructing a real WebEngineView (see
    `editor_widget.py`'s module docstring for why that hard-aborts pytest).
    """

    opened = pyqtSignal(str)  # project now shown — dock should show/raise
    closed = pyqtSignal()  # nothing left to show — dock may hide
    navigationBlocked = pyqtSignal(str, str)  # project, attempted url
    approveRequested = pyqtSignal(str, str)  # project, artifact_id
    reviseRequested = pyqtSignal(str, str, str)  # project, artifact_id, feedback
    externallyOpened = pyqtSignal(str)  # url/path

    def __init__(
        self,
        container: QWidget,
        *,
        view_factory: Callable[[Callable[[str], bool]], _PreviewWebView] | None = None,
        artifact_lookup: Callable[[str, str], dict | None] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._container = container
        layout = container.layout()
        if layout is None:
            layout = QVBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)
        self._layout = layout
        self._view_factory = view_factory or _PreviewWebView
        self._artifact_lookup = artifact_lookup or _default_artifact_lookup

        self._view: _PreviewWebView | None = None
        self._toolbar: QWidget | None = None
        self._project: str | None = None
        self._state: PreviewState | None = None
        self._artifact: dict | None = None
        self._device = "desktop"
        self._device_buttons: dict[str, QPushButton] = {}
        self._lbl_status: QLabel | None = None
        self._lbl_artifact: QLabel | None = None
        self._btn_approve: QPushButton | None = None
        self._btn_revise: QPushButton | None = None

        try:
            self._discard_enabled = _env_pane_discard_override(
                performance_settings.load().pane_discard_enabled
            )
        except Exception:
            self._discard_enabled = True
        self._keepalive = True
        self._discard_timer = QTimer(self)
        self._discard_timer.setSingleShot(True)
        self._discard_timer.setInterval(_discard_debounce_ms())
        self._discard_timer.timeout.connect(self._on_discard_timeout)

    # -- introspection (also used by tests) -------------------------------
    def has_view(self) -> bool:
        return self._view is not None

    def current_project(self) -> str | None:
        return self._project

    # -- show / close, driven by Orchestrator previewOpened/Updated/Closed --
    def show_state(self, project: str, state: PreviewState) -> None:
        self._ensure_view()
        self._project = project
        self._state = state
        if state.mode == "url":
            self._view.load_url(state.target)
        else:
            self._view.load_file(state.target)
        self.set_device(state.device)
        self._artifact = self._artifact_lookup(project, state.target)
        self._refresh_header()
        self.opened.emit(project)

    def close_project(self, project: str) -> None:
        if self._project != project:
            return  # a different project's preview closed — nothing on screen changes
        self._project = None
        self._state = None
        self._artifact = None
        self._destroy_view()
        self.closed.emit()

    def _ensure_view(self) -> None:
        if self._view is not None:
            return
        if self._toolbar is None:
            self._toolbar = self._build_toolbar()
            self._layout.addWidget(self._toolbar)
        self._view = self._view_factory(self.navigation_allowed)
        self._view.navigationBlocked.connect(self._on_navigation_blocked)
        self._layout.addWidget(self._view)

    def _destroy_view(self) -> None:
        if self._view is None:
            return
        view = self._view
        self._view = None
        self._layout.removeWidget(view)
        view.destroy()

    # -- navigation policy (12_SECURITY_THREAT_MODEL.md) ---------------------
    def navigation_allowed(self, target_url: str) -> bool:
        """The one gate every in-page navigation on the shared WebView goes
        through — `_PreviewPage.acceptNavigationRequest` calls this
        (injected as `nav_check` at construction). Kept as a plain method
        (not buried inside the Qt override) so a test can drive the policy
        without constructing a real `QWebEngineView`."""
        if self._state is None:
            return False
        return _controller_navigation_allowed(self._state, target_url)

    def _on_navigation_blocked(self, url: str) -> None:
        if self._project is not None:
            self.navigationBlocked.emit(self._project, url)

    # -- device presets --------------------------------------------------------
    def set_device(self, device: str) -> None:
        if device not in DEVICE_PRESETS:
            return
        self._device = device
        if self._view is not None:
            self._view.apply_device(device)
        for name, btn in self._device_buttons.items():
            btn.setChecked(name == device)
        self._refresh_header()  # #369 item 4: header's device text stays live

    # -- toolbar actions -----------------------------------------------------
    def refresh(self) -> None:
        if self._view is not None:
            self._view.reload()

    def open_externally(self) -> None:
        if self._state is None:
            return
        if self._state.mode == "url":
            QDesktopServices.openUrl(QUrl(self._state.target))
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._state.target))
        self.externallyOpened.emit(self._state.target)

    def _on_approve_clicked(self) -> None:
        if self._project is not None and self._artifact is not None:
            self.approveRequested.emit(self._project, self._artifact["artifact_id"])

    def _on_revise_clicked(self) -> None:
        if self._project is None or self._artifact is None:
            return
        feedback, ok = QInputDialog.getMultiLineText(
            self._toolbar, "Request revision", "Feedback for the Designer:"
        )
        if ok:
            self.reviseRequested.emit(self._project, self._artifact["artifact_id"], feedback)

    def set_artifact_status(self, status: str) -> None:
        """Called back by MainWindow once `Orchestrator.design_approve`/
        `design_revise` actually lands (`design_actions.py` owns the status
        transition + Lead notice, per that module's own docstring; this
        just keeps the header in sync without a second registry read)."""
        if self._artifact is not None:
            self._artifact = {**self._artifact, "status": status}
        self._refresh_header()

    def _refresh_header(self) -> None:
        # #369 item 4: project · target · device — the artifact half lives
        # in the separate _lbl_artifact label refreshed just below.
        if self._lbl_status is not None and self._state is not None:
            self._lbl_status.setText(
                f"{self._project} · {self._state.mode}: {self._state.target} · {self._device}"
            )
        has_artifact = self._artifact is not None
        if self._lbl_artifact is not None:
            self._lbl_artifact.setText(
                f"{self._artifact['title']} · {self._artifact['status']}" if has_artifact else ""
            )
        actionable = has_artifact and self._artifact["status"] in _NON_TERMINAL_ARTIFACT_STATUSES
        if self._btn_approve is not None:
            self._btn_approve.setEnabled(actionable)
        if self._btn_revise is not None:
            self._btn_revise.setEnabled(actionable)

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(4, 4, 4, 4)
        row.setSpacing(6)

        group = QButtonGroup(bar)
        group.setExclusive(True)
        for name, label in (("desktop", "Desktop"), ("tablet", "Tablet"), ("mobile", "Mobile")):
            btn = QPushButton(label, bar)
            btn.setCheckable(True)
            btn.setChecked(name == self._device)
            btn.clicked.connect(lambda _checked, n=name: self.set_device(n))
            group.addButton(btn)
            self._device_buttons[name] = btn
            row.addWidget(btn)

        btn_refresh = QPushButton("Refresh", bar)
        btn_refresh.clicked.connect(self.refresh)
        row.addWidget(btn_refresh)

        btn_external = QPushButton("Open externally", bar)
        btn_external.clicked.connect(self.open_externally)
        row.addWidget(btn_external)

        self._lbl_status = QLabel("", bar)
        self._lbl_status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row.addWidget(self._lbl_status, 1)

        self._lbl_artifact = QLabel("", bar)
        row.addWidget(self._lbl_artifact)

        self._btn_approve = QPushButton("Approve", bar)
        self._btn_approve.setEnabled(False)
        self._btn_approve.clicked.connect(self._on_approve_clicked)
        row.addWidget(self._btn_approve)

        self._btn_revise = QPushButton("Revise", bar)
        self._btn_revise.setEnabled(False)
        self._btn_revise.clicked.connect(self._on_revise_clicked)
        row.addWidget(self._btn_revise)

        return bar

    # -- #364 lever 1: discard when hidden ------------------------------------
    def set_keepalive(self, active: bool) -> None:
        """Called by MainWindow off the preview dock's own `visibilityChanged`
        signal — same "hidden = discard candidate" wiring
        `terminal_widget.py`'s pane-tab keep-alive uses, applied to the dock
        instead of a pane tab."""
        active = bool(active)
        self._keepalive = active
        if self._view is None:
            return
        if active:
            if self._discard_timer.isActive():
                self._discard_timer.stop()
            self._view.reattach()
        elif self._discard_enabled:
            self._discard_timer.start()

    def set_discard_enabled(self, enabled: bool) -> None:
        self._discard_enabled = bool(enabled)
        if not self._discard_enabled and self._discard_timer.isActive():
            self._discard_timer.stop()

    def _on_discard_timeout(self) -> None:
        if self._keepalive or not self._discard_enabled or self._view is None:
            return
        self._view.discard()
