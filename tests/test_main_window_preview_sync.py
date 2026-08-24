"""main_window.py preview-sync helpers (#369 BUG-002): the shared
`PreviewHost` must only ever paint the *active* tab's project — a
background project's `previewOpened`/`previewUpdated` must not hijack it,
and switching tabs must show the new project's own state (or hide the dock
if it has none).

Exercised against a lightweight duck-typed stand-in for `MainWindow` rather
than a real one — same "unbound method against a fake self" convention
`test_preview_widget.py`'s `TestPreviewPageNavigationContract` uses for
`_PreviewPage.acceptNavigationRequest`, since a real `MainWindow()` boots the
full cockpit (orchestrator, CLI server, Lead pane).
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_takkub.main_window import MainWindow
from agent_takkub.preview_controller import PreviewState


class _FakeTab:
    def __init__(self, project_name: str) -> None:
        self.project_name = project_name


class _FakeTabs:
    def __init__(self, current=None) -> None:
        self._current = current

    def currentWidget(self):
        return self._current


class _FakeOrch:
    def __init__(self, states: dict[str, PreviewState] | None = None) -> None:
        self._states = states or {}

    def preview_status(self, project: str) -> PreviewState | None:
        return self._states.get(project)


class _FakeStatusBar:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def showMessage(self, text: str, timeout: int = 0) -> None:
        self.messages.append(text)


class _FakePreviewHost:
    def __init__(self, current_project: str | None = None) -> None:
        self.shown: list[tuple[str, PreviewState]] = []
        self._current_project = current_project

    def show_state(self, project: str, state: PreviewState) -> None:
        self.shown.append((project, state))
        self._current_project = project

    def current_project(self) -> str | None:
        return self._current_project


class _FakeDock:
    def __init__(self) -> None:
        self.hide_calls = 0

    def hide(self) -> None:
        self.hide_calls += 1


def _fake_self(tab=None, orch=None, host=None, dock=None, status=None) -> SimpleNamespace:
    ns = SimpleNamespace(
        tabs=_FakeTabs(tab),
        orch=orch or _FakeOrch(),
        _preview_host=host or _FakePreviewHost(),
        _preview_dock=dock or _FakeDock(),
        _status=status or _FakeStatusBar(),
    )
    # _on_preview_state_changed calls self._active_project_name() — bind the
    # real implementation against this same fake `self` rather than
    # reimplementing its logic here.
    ns._active_project_name = lambda: MainWindow._active_project_name(ns)
    return ns


class TestActiveProjectName:
    def test_returns_project_name_of_current_tab(self) -> None:
        fake = _fake_self(tab=_FakeTab("demo"))
        assert MainWindow._active_project_name(fake) == "demo"

    def test_returns_none_when_no_tab_is_current(self) -> None:
        fake = _fake_self(tab=None)
        assert MainWindow._active_project_name(fake) is None


class TestOnPreviewStateChanged:
    def test_active_project_update_is_shown_in_the_host(self) -> None:
        host = _FakePreviewHost()
        fake = _fake_self(tab=_FakeTab("demo"), host=host)
        state = PreviewState(project="demo", mode="url", target="http://127.0.0.1:3000")

        MainWindow._on_preview_state_changed(fake, "demo", state)

        assert host.shown == [("demo", state)]

    def test_background_project_update_does_not_touch_the_host(self) -> None:
        host = _FakePreviewHost()
        status = _FakeStatusBar()
        fake = _fake_self(tab=_FakeTab("demo"), host=host, status=status)
        state = PreviewState(project="other", mode="url", target="http://127.0.0.1:4000")

        MainWindow._on_preview_state_changed(fake, "other", state)

        assert host.shown == []
        assert len(status.messages) == 1
        assert "other" in status.messages[0]

    def test_no_active_tab_treats_every_update_as_background(self) -> None:
        host = _FakePreviewHost()
        fake = _fake_self(tab=None, host=host)
        state = PreviewState(project="demo", mode="url", target="http://127.0.0.1:3000")

        MainWindow._on_preview_state_changed(fake, "demo", state)

        assert host.shown == []


class TestSyncPreviewToActiveTab:
    def test_shows_state_when_the_new_active_project_has_one_open(self) -> None:
        state = PreviewState(project="demo", mode="url", target="http://127.0.0.1:3000")
        host = _FakePreviewHost()
        fake = _fake_self(orch=_FakeOrch({"demo": state}), host=host)

        MainWindow._sync_preview_to_active_tab(fake, "demo")

        assert host.shown == [("demo", state)]

    def test_hides_the_dock_when_the_new_project_has_no_state_but_something_else_was_shown(
        self,
    ) -> None:
        host = _FakePreviewHost(current_project="other")
        dock = _FakeDock()
        fake = _fake_self(orch=_FakeOrch({}), host=host, dock=dock)

        MainWindow._sync_preview_to_active_tab(fake, "demo")

        assert dock.hide_calls == 1
        assert host.shown == []  # the widget's own content is left untouched

    def test_no_hide_when_nothing_was_being_shown(self) -> None:
        host = _FakePreviewHost(current_project=None)
        dock = _FakeDock()
        fake = _fake_self(orch=_FakeOrch({}), host=host, dock=dock)

        MainWindow._sync_preview_to_active_tab(fake, "demo")

        assert dock.hide_calls == 0

    def test_no_hide_when_the_shown_project_already_matches(self) -> None:
        # Same project the host already shows but with no controller state
        # (edge case — shouldn't normally happen since a close already
        # clears both together): nothing to hide since it already matches.
        host = _FakePreviewHost(current_project="demo")
        dock = _FakeDock()
        fake = _fake_self(orch=_FakeOrch({}), host=host, dock=dock)

        MainWindow._sync_preview_to_active_tab(fake, "demo")

        assert dock.hide_calls == 0

    def test_switching_to_no_tabs_left_hides_whatever_was_shown(self) -> None:
        host = _FakePreviewHost(current_project="other")
        dock = _FakeDock()
        fake = _fake_self(orch=_FakeOrch({}), host=host, dock=dock)

        MainWindow._sync_preview_to_active_tab(fake, None)

        assert dock.hide_calls == 1
