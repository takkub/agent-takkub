"""PreviewHost / preview_widget.py (#365 phase 5 widget + phase 6 Design
Director UI).

Lifecycle (lazy-create/single-instance/destroy) and navigation-policy tests
use a stub `view_factory` instead of a real `QWebEngineView` — same
constraint/convention `tests/test_editor_widget.py` documents for
`EditorHost` (a real `QWebEngineView` construction hard-aborted pytest
before the conftest.py argv fix; the stub keeps this file fast and free of
a renderer process per test either way).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget

from agent_takkub import preview_widget as pw
from agent_takkub.core.storage.jsonl_store import JsonlStore
from agent_takkub.design_actions import (
    DesignArtifact,
    DesignArtifactRegistry,
    approve,
    request_revision,
)
from agent_takkub.preview_controller import PreviewController, PreviewState

from ._qt_timer_leak_guard import stop_timers_after


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def container(qapp) -> QWidget:
    return QWidget()


@pytest.fixture(autouse=True)
def _stop_discard_timer(monkeypatch):
    # Every PreviewHost starts a #364-lever-1 discard QTimer; a test that
    # arms it (set_keepalive(False)) without letting it fire naturally must
    # not leave it active past teardown — same #344/#345 guard convention
    # test_editor_widget.py uses for FileWatchService's timer.
    finalize = stop_timers_after(monkeypatch, pw.PreviewHost, "_discard_timer")
    yield
    finalize()


# ── stub view: mirrors _PreviewWebView's public surface, no real Chromium ──


class _StubPreviewView(QWidget):
    navigationBlocked = pyqtSignal(str)

    def __init__(self, nav_check) -> None:
        super().__init__()
        self.nav_check = nav_check
        self.loaded_urls: list[str] = []
        self.loaded_files: list[str] = []
        self.reload_calls = 0
        self.applied_devices: list[str] = []
        self.destroyed_called = False
        self.discard_called = False
        self.reattach_called = False

    def load_url(self, url: str) -> None:
        self.loaded_urls.append(url)

    def load_file(self, path: str) -> None:
        self.loaded_files.append(path)

    def reload(self) -> None:
        self.reload_calls += 1

    def apply_device(self, device: str) -> None:
        self.applied_devices.append(device)

    def destroy(self) -> None:
        self.destroyed_called = True

    def discard(self) -> None:
        self.discard_called = True

    def reattach(self) -> None:
        self.reattach_called = True


@pytest.fixture
def stub_factory():
    created: list[_StubPreviewView] = []

    def factory(nav_check):
        view = _StubPreviewView(nav_check)
        created.append(view)
        return view

    factory.created = created
    return factory


def _no_artifact(_project: str, _target: str) -> dict | None:
    return None


# ── pure config ──────────────────────────────────────────────────────────


def test_device_frame_sizes_cover_every_controller_preset() -> None:
    from agent_takkub.preview_controller import DEVICE_PRESETS

    assert set(pw.DEVICE_FRAME_SIZES) == set(DEVICE_PRESETS)
    assert pw.DEVICE_FRAME_SIZES["desktop"] is None
    assert pw.DEVICE_FRAME_SIZES["tablet"] == (768, 1024)
    assert pw.DEVICE_FRAME_SIZES["mobile"] == (375, 812)


# ── no QWebChannel bridge anywhere in this module (12_SECURITY_THREAT_MODEL) ──


def test_no_qwebchannel_bridge_in_preview_widget_source() -> None:
    src = Path(pw.__file__).read_text(encoding="utf-8")
    msg = (
        "preview_widget.py must never import/construct a QWebChannel — Preview "
        "shows arbitrary remote/local content and must carry no privileged "
        "bridge (12_SECURITY_THREAT_MODEL.md)"
    )
    assert "QtWebChannel" not in src, msg
    assert "QWebChannel(" not in src, msg
    assert "registerObject" not in src, msg


# ── lazy create / single instance / destroy ─────────────────────────────


class TestLazyCreateAndSingleInstance:
    def test_no_view_until_first_show(self, container, stub_factory) -> None:
        host = pw.PreviewHost(container, view_factory=stub_factory, artifact_lookup=_no_artifact)
        assert host.has_view() is False
        assert stub_factory.created == []

    def test_show_state_url_lazily_creates_the_view(self, container, stub_factory) -> None:
        host = pw.PreviewHost(container, view_factory=stub_factory, artifact_lookup=_no_artifact)
        state = PreviewState(project="demo", mode="url", target="http://127.0.0.1:3000")

        host.show_state("demo", state)

        assert host.has_view() is True
        assert len(stub_factory.created) == 1
        view = stub_factory.created[0]
        assert view.loaded_urls == ["http://127.0.0.1:3000"]
        assert host.current_project() == "demo"

    def test_show_state_file_loads_via_load_file(self, container, stub_factory, tmp_path) -> None:
        f = tmp_path / "mock.html"
        f.write_text("<html></html>", encoding="utf-8")
        host = pw.PreviewHost(container, view_factory=stub_factory, artifact_lookup=_no_artifact)
        state = PreviewState(project="demo", mode="file", target=str(f))

        host.show_state("demo", state)

        view = stub_factory.created[0]
        assert view.loaded_files == [str(f)]
        assert view.loaded_urls == []

    def test_switching_project_reuses_the_same_view(self, container, stub_factory) -> None:
        host = pw.PreviewHost(container, view_factory=stub_factory, artifact_lookup=_no_artifact)
        host.show_state("a", PreviewState(project="a", mode="url", target="http://127.0.0.1:1"))
        host.show_state("b", PreviewState(project="b", mode="url", target="http://127.0.0.1:2"))

        assert len(stub_factory.created) == 1  # never a second WebView
        assert host.current_project() == "b"
        view = stub_factory.created[0]
        assert view.loaded_urls == ["http://127.0.0.1:1", "http://127.0.0.1:2"]


class TestDestroyOnClose:
    def test_close_current_project_destroys_the_view(self, container, stub_factory) -> None:
        host = pw.PreviewHost(container, view_factory=stub_factory, artifact_lookup=_no_artifact)
        closed: list[bool] = []
        host.closed.connect(lambda: closed.append(True))
        host.show_state(
            "demo", PreviewState(project="demo", mode="url", target="http://127.0.0.1:1")
        )
        view = stub_factory.created[0]

        host.close_project("demo")

        assert host.has_view() is False
        assert view.destroyed_called is True
        assert closed == [True]
        assert host.current_project() is None

    def test_close_a_different_project_is_a_noop(self, container, stub_factory) -> None:
        host = pw.PreviewHost(container, view_factory=stub_factory, artifact_lookup=_no_artifact)
        host.show_state(
            "demo", PreviewState(project="demo", mode="url", target="http://127.0.0.1:1")
        )
        view = stub_factory.created[0]

        host.close_project("other-project")

        assert host.has_view() is True
        assert view.destroyed_called is False
        assert host.current_project() == "demo"

    def test_reopening_after_close_creates_a_fresh_view(self, container, stub_factory) -> None:
        host = pw.PreviewHost(container, view_factory=stub_factory, artifact_lookup=_no_artifact)
        host.show_state(
            "demo", PreviewState(project="demo", mode="url", target="http://127.0.0.1:1")
        )
        host.close_project("demo")

        host.show_state(
            "demo", PreviewState(project="demo", mode="url", target="http://127.0.0.1:2")
        )

        assert host.has_view() is True
        assert len(stub_factory.created) == 2


# ── navigation policy (12_SECURITY_THREAT_MODEL.md) ──────────────────────


class TestNavigationPolicy:
    def test_no_state_yet_blocks_everything(self, container, stub_factory) -> None:
        host = pw.PreviewHost(container, view_factory=stub_factory, artifact_lookup=_no_artifact)
        assert host.navigation_allowed("http://127.0.0.1:3000") is False

    def test_url_mode_allows_same_origin_blocks_cross_origin(self, container, stub_factory) -> None:
        host = pw.PreviewHost(container, view_factory=stub_factory, artifact_lookup=_no_artifact)
        host.show_state(
            "demo", PreviewState(project="demo", mode="url", target="http://127.0.0.1:3000/")
        )

        assert host.navigation_allowed("http://127.0.0.1:3000/about") is True
        assert host.navigation_allowed("https://evil.example.com") is False
        assert host.navigation_allowed("http://127.0.0.1:9999/") is False  # different port

    def test_file_mode_allows_only_the_exact_open_file(
        self, container, stub_factory, tmp_path
    ) -> None:
        f = tmp_path / "mock.html"
        f.write_text("<html></html>", encoding="utf-8")
        other = tmp_path / "other.html"
        other.write_text("<html></html>", encoding="utf-8")
        host = pw.PreviewHost(container, view_factory=stub_factory, artifact_lookup=_no_artifact)
        host.show_state("demo", PreviewState(project="demo", mode="file", target=str(f)))

        assert host.navigation_allowed(str(f)) is True
        assert host.navigation_allowed(str(other)) is False

    def test_blocked_navigation_from_the_view_is_relayed(self, container, stub_factory) -> None:
        host = pw.PreviewHost(container, view_factory=stub_factory, artifact_lookup=_no_artifact)
        host.show_state(
            "demo", PreviewState(project="demo", mode="url", target="http://127.0.0.1:3000/")
        )
        view = stub_factory.created[0]
        blocked: list[tuple[str, str]] = []
        host.navigationBlocked.connect(lambda proj, url: blocked.append((proj, url)))

        view.navigationBlocked.emit("https://evil.example.com")

        assert blocked == [("demo", "https://evil.example.com")]


# ── _PreviewPage.acceptNavigationRequest contract (reviewer finding #4,
# 2026-08-23 phase 3-5 review; contract pinned in
# preview_controller.navigation_allowed's docstring) ─────────────────────
#
# `_PreviewPage` subclasses the real `QWebEnginePage`, so constructing one
# needs a real `QWebEngineProfile` — same "hard-aborts pytest" constraint
# `test_editor_widget.py`/this file's own module docstring document. But
# `acceptNavigationRequest`'s body only touches `self._nav_check`/
# `self._on_blocked` (plain attributes, never a Qt-bound call), so it can be
# exercised as an unbound function against a bare duck-typed stand-in —
# no QtWebEngine construction, no opt-in flag needed.


class _FakePreviewPageSelf:
    def __init__(self, nav_check) -> None:
        self._nav_check = nav_check
        self.blocked: list[str] = []
        self._on_blocked = self.blocked.append


class TestPreviewPageNavigationContract:
    def test_main_frame_navigation_consults_nav_check_and_allows(self) -> None:
        checked: list[str] = []
        fake = _FakePreviewPageSelf(lambda url: checked.append(url) or True)

        allowed = pw._PreviewPage.acceptNavigationRequest(
            fake, pw.QUrl("http://127.0.0.1:3000/about"), object(), True
        )

        assert allowed is True
        assert checked == ["http://127.0.0.1:3000/about"]
        assert fake.blocked == []

    def test_main_frame_navigation_refused_by_nav_check_is_blocked_and_reported(self) -> None:
        fake = _FakePreviewPageSelf(lambda url: False)

        allowed = pw._PreviewPage.acceptNavigationRequest(
            fake, pw.QUrl("https://evil.example.com"), object(), True
        )

        assert allowed is False
        assert fake.blocked == ["https://evil.example.com"]

    def test_nav_check_is_consulted_regardless_of_nav_type_covers_redirects(self) -> None:
        # Chromium calls acceptNavigationRequest once per hop of a redirect
        # chain (each redirect is its own NavigationRequest) — the widget
        # covers that automatically by never filtering on `nav_type`. This
        # sweeps a handful of stand-in nav_type values to pin that
        # independence, since the real QWebEnginePage.NavigationType enum
        # isn't meaningfully constructible outside a real page.
        for fake_nav_type in ("link_clicked", "typed", "redirect", "form_submitted", object()):
            checked: list[str] = []
            fake = _FakePreviewPageSelf(lambda url, checked=checked: checked.append(url) or True)

            allowed = pw._PreviewPage.acceptNavigationRequest(
                fake, pw.QUrl("http://127.0.0.1:3000/"), fake_nav_type, True
            )

            assert allowed is True
            assert checked == ["http://127.0.0.1:3000/"]

    def test_sub_frame_iframe_navigation_bypasses_nav_check_entirely(self) -> None:
        checked: list[str] = []
        fake = _FakePreviewPageSelf(lambda url: checked.append(url) or False)  # would refuse

        allowed = pw._PreviewPage.acceptNavigationRequest(
            fake, pw.QUrl("https://embedded.example.com/widget"), object(), False
        )

        assert allowed is True  # iframe nav is let through unconditionally
        assert checked == []  # nav_check never even called for a sub-frame
        assert fake.blocked == []

    def test_nav_check_raising_fails_closed(self) -> None:
        def _boom(_url: str) -> bool:
            raise RuntimeError("policy bug")

        fake = _FakePreviewPageSelf(_boom)

        allowed = pw._PreviewPage.acceptNavigationRequest(
            fake, pw.QUrl("http://127.0.0.1:3000/"), object(), True
        )

        assert allowed is False
        assert fake.blocked == ["http://127.0.0.1:3000/"]

    def test_no_createwindow_override_relies_on_qt_default_fail_closed(self) -> None:
        # New-window/popup requests (target="_blank", window.open()) must
        # never reach nav_check at all — _PreviewPage deliberately has no
        # createWindow override, so QWebEnginePage's own default (returns
        # None, silently refusing the popup) applies. Confirmed identical
        # behavior already shipped in terminal_widget.py's _on_open_url
        # docstring. Pinning the absence here so a future edit that adds an
        # override notices it must route through nav_check too (see the
        # navigation_allowed docstring contract).
        assert "createWindow" not in vars(pw._PreviewPage)


# ── device presets ────────────────────────────────────────────────────────


class TestDevicePresets:
    def test_show_state_applies_the_state_device(self, container, stub_factory) -> None:
        host = pw.PreviewHost(container, view_factory=stub_factory, artifact_lookup=_no_artifact)
        host.show_state(
            "demo",
            PreviewState(project="demo", mode="url", target="http://127.0.0.1:1", device="mobile"),
        )
        view = stub_factory.created[0]
        assert view.applied_devices[-1] == "mobile"

    def test_set_device_applies_to_the_live_view(self, container, stub_factory) -> None:
        host = pw.PreviewHost(container, view_factory=stub_factory, artifact_lookup=_no_artifact)
        host.show_state(
            "demo", PreviewState(project="demo", mode="url", target="http://127.0.0.1:1")
        )
        view = stub_factory.created[0]

        host.set_device("tablet")

        assert view.applied_devices[-1] == "tablet"
        assert host._device_buttons["tablet"].isChecked() is True
        assert host._device_buttons["desktop"].isChecked() is False

    def test_unknown_device_is_ignored(self, container, stub_factory) -> None:
        host = pw.PreviewHost(container, view_factory=stub_factory, artifact_lookup=_no_artifact)
        host.show_state(
            "demo", PreviewState(project="demo", mode="url", target="http://127.0.0.1:1")
        )
        view = stub_factory.created[0]
        before = list(view.applied_devices)

        host.set_device("smartwatch")

        assert view.applied_devices == before


# ── toolbar actions: refresh / open externally ────────────────────────────


class TestToolbarActions:
    def test_refresh_reloads_the_live_view(self, container, stub_factory) -> None:
        host = pw.PreviewHost(container, view_factory=stub_factory, artifact_lookup=_no_artifact)
        host.show_state(
            "demo", PreviewState(project="demo", mode="url", target="http://127.0.0.1:1")
        )

        host.refresh()

        assert stub_factory.created[0].reload_calls == 1

    def test_open_externally_emits_the_target(self, container, stub_factory, monkeypatch) -> None:
        opened: list[str] = []
        monkeypatch.setattr(
            pw.QDesktopServices, "openUrl", lambda qurl: opened.append(qurl.toString())
        )
        host = pw.PreviewHost(container, view_factory=stub_factory, artifact_lookup=_no_artifact)
        host.show_state(
            "demo", PreviewState(project="demo", mode="url", target="http://127.0.0.1:1")
        )
        externally: list[str] = []
        host.externallyOpened.connect(externally.append)

        host.open_externally()

        assert externally == ["http://127.0.0.1:1"]
        assert opened  # QDesktopServices.openUrl was called


# ── #364 lever 1: discard when hidden ─────────────────────────────────────


class TestDiscardOnHidden:
    def test_hiding_arms_discard_and_timeout_discards(self, container, stub_factory) -> None:
        host = pw.PreviewHost(container, view_factory=stub_factory, artifact_lookup=_no_artifact)
        host.set_discard_enabled(True)
        host.show_state(
            "demo", PreviewState(project="demo", mode="url", target="http://127.0.0.1:1")
        )
        view = stub_factory.created[0]

        host.set_keepalive(False)
        assert host._discard_timer.isActive() is True
        host._on_discard_timeout()  # simulate the debounce firing

        assert view.discard_called is True

    def test_showing_again_cancels_pending_discard(self, container, stub_factory) -> None:
        host = pw.PreviewHost(container, view_factory=stub_factory, artifact_lookup=_no_artifact)
        host.set_discard_enabled(True)
        host.show_state(
            "demo", PreviewState(project="demo", mode="url", target="http://127.0.0.1:1")
        )
        view = stub_factory.created[0]

        host.set_keepalive(False)
        host.set_keepalive(True)
        host._on_discard_timeout()  # timer was cancelled — this must be a no-op

        assert view.discard_called is False
        assert host._discard_timer.isActive() is False

    def test_showing_reattaches_a_discarded_view(self, container, stub_factory) -> None:
        host = pw.PreviewHost(container, view_factory=stub_factory, artifact_lookup=_no_artifact)
        host.show_state(
            "demo", PreviewState(project="demo", mode="url", target="http://127.0.0.1:1")
        )
        view = stub_factory.created[0]

        host.set_keepalive(True)

        assert view.reattach_called is True

    def test_discard_disabled_never_arms_the_timer(self, container, stub_factory) -> None:
        host = pw.PreviewHost(container, view_factory=stub_factory, artifact_lookup=_no_artifact)
        host.set_discard_enabled(False)
        host.show_state(
            "demo", PreviewState(project="demo", mode="url", target="http://127.0.0.1:1")
        )

        host.set_keepalive(False)

        assert host._discard_timer.isActive() is False


# ── phase 6: artifact header + approve/revise wiring ─────────────────────


def test_default_artifact_lookup_picks_latest_matching_target(monkeypatch) -> None:
    class _FakeRegistry:
        def __init__(self, project_id: str) -> None:
            self.project_id = project_id

        def all(self):
            return [
                DesignArtifact(
                    artifact_id="old",
                    project_id="demo",
                    title="v1",
                    kind="html",
                    target="/x.html",
                    created_at="2026-01-01T00:00:00",
                ),
                DesignArtifact(
                    artifact_id="new",
                    project_id="demo",
                    title="v2",
                    kind="html",
                    target="/x.html",
                    created_at="2026-01-02T00:00:00",
                ),
                DesignArtifact(
                    artifact_id="other",
                    project_id="demo",
                    title="unrelated",
                    kind="html",
                    target="/y.html",
                    created_at="2026-01-03T00:00:00",
                ),
            ]

    monkeypatch.setattr(pw, "DesignArtifactRegistry", _FakeRegistry)

    found = pw._default_artifact_lookup("demo", "/x.html")

    assert found is not None
    assert found["artifact_id"] == "new"


class TestArtifactHeaderAndActions:
    def _lookup_for(self, artifact: DesignArtifact):
        return lambda project, target: artifact.as_dict() if target == artifact.target else None

    def test_header_shows_title_and_status_and_enables_actions(
        self, container, stub_factory
    ) -> None:
        artifact = DesignArtifact(
            artifact_id="a1",
            project_id="demo",
            title="Dashboard",
            kind="html",
            target="http://127.0.0.1:1",
            status="review",
        )
        host = pw.PreviewHost(
            container, view_factory=stub_factory, artifact_lookup=self._lookup_for(artifact)
        )

        host.show_state(
            "demo", PreviewState(project="demo", mode="url", target="http://127.0.0.1:1")
        )

        assert "Dashboard" in host._lbl_artifact.text()
        assert "review" in host._lbl_artifact.text()
        assert host._btn_approve.isEnabled() is True
        assert host._btn_revise.isEnabled() is True

    def test_no_matching_artifact_disables_actions(self, container, stub_factory) -> None:
        host = pw.PreviewHost(container, view_factory=stub_factory, artifact_lookup=_no_artifact)

        host.show_state(
            "demo", PreviewState(project="demo", mode="url", target="http://127.0.0.1:1")
        )

        assert host._lbl_artifact.text() == ""
        assert host._btn_approve.isEnabled() is False
        assert host._btn_revise.isEnabled() is False

    def test_approved_artifact_disables_actions(self, container, stub_factory) -> None:
        artifact = DesignArtifact(
            artifact_id="a1",
            project_id="demo",
            title="Dashboard",
            kind="html",
            target="http://127.0.0.1:1",
            status="approved",
        )
        host = pw.PreviewHost(
            container, view_factory=stub_factory, artifact_lookup=self._lookup_for(artifact)
        )

        host.show_state(
            "demo", PreviewState(project="demo", mode="url", target="http://127.0.0.1:1")
        )

        assert host._btn_approve.isEnabled() is False
        assert host._btn_revise.isEnabled() is False

    def test_approve_button_click_emits_approveRequested(self, container, stub_factory) -> None:
        artifact = DesignArtifact(
            artifact_id="a1",
            project_id="demo",
            title="Dashboard",
            kind="html",
            target="http://127.0.0.1:1",
            status="draft",
        )
        host = pw.PreviewHost(
            container, view_factory=stub_factory, artifact_lookup=self._lookup_for(artifact)
        )
        host.show_state(
            "demo", PreviewState(project="demo", mode="url", target="http://127.0.0.1:1")
        )
        approved: list[tuple[str, str]] = []
        host.approveRequested.connect(lambda proj, aid: approved.append((proj, aid)))

        host._btn_approve.click()

        assert approved == [("demo", "a1")]

    def test_revise_button_click_emits_reviseRequested_with_feedback(
        self, container, stub_factory, monkeypatch
    ) -> None:
        artifact = DesignArtifact(
            artifact_id="a1",
            project_id="demo",
            title="Dashboard",
            kind="html",
            target="http://127.0.0.1:1",
            status="draft",
        )
        host = pw.PreviewHost(
            container, view_factory=stub_factory, artifact_lookup=self._lookup_for(artifact)
        )
        host.show_state(
            "demo", PreviewState(project="demo", mode="url", target="http://127.0.0.1:1")
        )
        monkeypatch.setattr(
            pw.QInputDialog,
            "getMultiLineText",
            staticmethod(lambda *a, **k: ("too much purple", True)),
        )
        revised: list[tuple[str, str, str]] = []
        host.reviseRequested.connect(lambda proj, aid, fb: revised.append((proj, aid, fb)))

        host._btn_revise.click()

        assert revised == [("demo", "a1", "too much purple")]

    def test_revise_dialog_cancelled_emits_nothing(
        self, container, stub_factory, monkeypatch
    ) -> None:
        artifact = DesignArtifact(
            artifact_id="a1",
            project_id="demo",
            title="Dashboard",
            kind="html",
            target="http://127.0.0.1:1",
            status="draft",
        )
        host = pw.PreviewHost(
            container, view_factory=stub_factory, artifact_lookup=self._lookup_for(artifact)
        )
        host.show_state(
            "demo", PreviewState(project="demo", mode="url", target="http://127.0.0.1:1")
        )
        monkeypatch.setattr(
            pw.QInputDialog, "getMultiLineText", staticmethod(lambda *a, **k: ("", False))
        )
        revised: list[tuple[str, str, str]] = []
        host.reviseRequested.connect(lambda proj, aid, fb: revised.append((proj, aid, fb)))

        host._btn_revise.click()

        assert revised == []

    def test_set_artifact_status_refreshes_header_and_buttons(
        self, container, stub_factory
    ) -> None:
        artifact = DesignArtifact(
            artifact_id="a1",
            project_id="demo",
            title="Dashboard",
            kind="html",
            target="http://127.0.0.1:1",
            status="draft",
        )
        host = pw.PreviewHost(
            container, view_factory=stub_factory, artifact_lookup=self._lookup_for(artifact)
        )
        host.show_state(
            "demo", PreviewState(project="demo", mode="url", target="http://127.0.0.1:1")
        )

        host.set_artifact_status("approved")

        assert "approved" in host._lbl_artifact.text()
        assert host._btn_approve.isEnabled() is False
        assert host._btn_revise.isEnabled() is False


# ── end-to-end approve/revise against the real design_actions functions ──


class TestApproveReviseFlowThroughRealDesignActions:
    """Exercises the exact call the production wiring makes: PreviewHost's
    approveRequested/reviseRequested signal -> a caller (MainWindow, in
    production) invoking design_actions.approve/request_revision -> the
    caller pushing the resulting status back via set_artifact_status. Uses a
    real DesignArtifactRegistry backed by a tmp JSONL store (same fixture
    shape test_design_actions.py uses) rather than design_actions itself
    being faked, so the transition rules (terminal-status guard etc.) are
    genuinely exercised."""

    @pytest.fixture
    def registry(self, tmp_path: Path) -> DesignArtifactRegistry:
        reg = DesignArtifactRegistry("demo", store=JsonlStore(tmp_path / "design_artifacts.jsonl"))
        reg.upsert(
            DesignArtifact(
                artifact_id="a1",
                project_id="demo",
                title="Dashboard",
                kind="html",
                target="http://127.0.0.1:1",
                status="draft",
            )
        )
        return reg

    def _host_for(
        self, container, stub_factory, registry: DesignArtifactRegistry
    ) -> pw.PreviewHost:
        lookup = lambda project, target: (  # noqa: E731
            next((a.as_dict() for a in registry.all() if a.target == target), None)
        )
        host = pw.PreviewHost(container, view_factory=stub_factory, artifact_lookup=lookup)
        host.show_state(
            "demo", PreviewState(project="demo", mode="url", target="http://127.0.0.1:1")
        )
        return host

    def test_approve_flow_transitions_status_and_updates_header(
        self, container, stub_factory, registry
    ) -> None:
        host = self._host_for(container, stub_factory, registry)

        def on_approve(project: str, artifact_id: str) -> None:
            updated = approve(project, artifact_id, registry=registry)
            host.set_artifact_status(updated.status)

        host.approveRequested.connect(on_approve)

        host._btn_approve.click()

        assert registry.get("a1").status == "approved"
        assert "approved" in host._lbl_artifact.text()
        assert host._btn_approve.isEnabled() is False  # terminal — re-fetch would refuse again

    def test_revise_flow_transitions_status_with_feedback(
        self, container, stub_factory, registry, monkeypatch
    ) -> None:
        host = self._host_for(container, stub_factory, registry)
        monkeypatch.setattr(
            pw.QInputDialog, "getMultiLineText", staticmethod(lambda *a, **k: ("too generic", True))
        )
        received_feedback: list[str] = []

        def on_revise(project: str, artifact_id: str, feedback: str) -> None:
            received_feedback.append(feedback)
            updated = request_revision(project, artifact_id, feedback=feedback, registry=registry)
            host.set_artifact_status(updated.status)

        host.reviseRequested.connect(on_revise)

        host._btn_revise.click()

        assert received_feedback == ["too generic"]
        assert registry.get("a1").status == "revision_requested"
        assert "revision_requested" in host._lbl_artifact.text()
        assert host._btn_approve.isEnabled() is True  # non-terminal — still actionable


# ── CLI -> widget: PreviewController signals drive PreviewHost ───────────


class TestControllerSignalsDriveTheWidget:
    """Orchestrator.previewOpened/Updated/Closed are a pure 1:1 re-emit of
    PreviewController's own opened/updated/closed signals (see
    orchestrator.py's __init__ wiring) — connecting straight to the
    controller here is a faithful stand-in for the full CLI -> IPC ->
    Orchestrator -> PreviewHost round trip without spinning up a CliServer."""

    def test_open_url_via_controller_shows_it_in_the_widget(self, container, stub_factory) -> None:
        controller = PreviewController()
        host = pw.PreviewHost(container, view_factory=stub_factory, artifact_lookup=_no_artifact)
        controller.opened.connect(host.show_state)
        controller.updated.connect(host.show_state)
        controller.closed.connect(host.close_project)

        controller.open_url("demo", "http://127.0.0.1:4000", device="mobile")

        assert host.has_view() is True
        view = stub_factory.created[0]
        assert view.loaded_urls == ["http://127.0.0.1:4000"]
        assert view.applied_devices[-1] == "mobile"

    def test_device_change_via_controller_updates_the_widget(self, container, stub_factory) -> None:
        controller = PreviewController()
        host = pw.PreviewHost(container, view_factory=stub_factory, artifact_lookup=_no_artifact)
        controller.opened.connect(host.show_state)
        controller.updated.connect(host.show_state)
        controller.closed.connect(host.close_project)
        controller.open_url("demo", "http://127.0.0.1:4000")

        controller.set_device("demo", "tablet")

        view = stub_factory.created[0]
        assert view.applied_devices[-1] == "tablet"

    def test_close_via_controller_destroys_the_widget(self, container, stub_factory) -> None:
        controller = PreviewController()
        host = pw.PreviewHost(container, view_factory=stub_factory, artifact_lookup=_no_artifact)
        controller.opened.connect(host.show_state)
        controller.updated.connect(host.show_state)
        controller.closed.connect(host.close_project)
        controller.open_url("demo", "http://127.0.0.1:4000")

        controller.close("demo")

        assert host.has_view() is False


# ── real QWebEngineView smoke (mirrors test_editor_widget.py's) ──────────
#
# opt-in only (set AGENT_TAKKUB_QT_WEBENGINE_SMOKE=1) — see
# test_editor_widget.py's identical block for why this stays out of the
# default gate (unverified on CI's macos/ubuntu runners). The rest of this
# file only ever exercises _StubPreviewView, which never touches real Qt
# WebEngine code at all.
@pytest.mark.skipif(
    os.environ.get("AGENT_TAKKUB_QT_WEBENGINE_SMOKE") != "1",
    reason="opt-in — see comment above; unverified on CI's macos/ubuntu runners",
)
def test_real_qwebengineview_construction_does_not_abort(qapp) -> None:
    """Exercises the exact construction path (`_PreviewWebView.__init__` ->
    `QWebEngineProfile` + `QWebEngineView` + `_PreviewPage`) that a real
    PreviewHost._ensure_view() drives."""
    view = pw._PreviewWebView(lambda url: True)
    try:
        view.load_url("about:blank")
        QTest.qWait(500)  # let the renderer process actually spawn
        assert view._view is not None
    finally:
        view.destroy()
        QTest.qWait(50)
