"""Closing a project tab must never leave projects.json's `active` naming it.

Prod 2026-09-21: the user closed `saas_admin_amb` (selected, first row) while
`unirecon` stayed open. `active` stayed `saas_admin_amb`, and since "Restart
Lead" and the provider switch both act on `active` rather than on the visible
tab, every press logged `close_noop_no_pane` + `spawn_failed` against a
project with no pane — the visible Lead was never touched.

Root cause is fixed in `ProjectNav.removeTab` (see test_project_nav.py). These
tests pin the second, independent guard in `MainWindow._close_project_tab`:
the nav's `currentChanged` is deliberately NOT connected here, so the only
thing that can repair `active` is the guard itself.
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QWidget

from agent_takkub import main_window as mw
from agent_takkub import project_nav as project_nav_module
from agent_takkub.project_nav import ProjectNav

from ._qt_timer_leak_guard import stop_timers_after


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate_nav(monkeypatch, tmp_path: pathlib.Path):
    monkeypatch.setattr(project_nav_module, "list_project_names", lambda: [])
    ini = str(tmp_path / "nav.ini")
    monkeypatch.setattr(
        project_nav_module,
        "QSettings",
        lambda *_a, **_k: QSettings(ini, QSettings.Format.IniFormat),
    )
    finalize = stop_timers_after(monkeypatch, ProjectNav, "_pending_timer")
    yield
    finalize()


class _FakeTab(QWidget):
    """Stands in for ProjectTab (patched in as `mw.ProjectTab` so the
    isinstance checks in `_close_project_tab` accept it) without building a
    real explorer/terminal."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.project_name = name
        self.teammate_panes: dict = {}
        self.lead_pane = SimpleNamespace(_terminal=SimpleNamespace(destroy_terminal=lambda: None))


class _ActiveStore:
    def __init__(self, active: str | None) -> None:
        self.active = active

    def get(self):
        return (self.active, {}) if self.active else (None, {})

    def set(self, name: str) -> bool:
        self.active = name
        return True

    def clear(self) -> None:
        self.active = None


def _window(nav: ProjectNav):
    orch = SimpleNamespace(
        close_all_teammates=lambda **_k: None,
        close=lambda *_a, **_k: (True, ""),
        unregister_pane=lambda *_a, **_k: None,
        unregister_workspace_diag_sources=lambda *_a, **_k: None,
        preview_command=lambda *_a, **_k: None,
        _project_panes=lambda *_a, **_k: {},
    )
    return SimpleNamespace(
        tabs=nav,
        orch=orch,
        _limit_store=None,
        _limit_label_host=None,
        _persist_open_tabs=lambda: None,
        _release_usage_corner=lambda _tab: None,
        _status=SimpleNamespace(showMessage=lambda *_a, **_k: None),
    )


def _setup(monkeypatch, names: list[str], selected: int, active: str):
    monkeypatch.setattr(mw, "ProjectTab", _FakeTab)
    store = _ActiveStore(active)
    monkeypatch.setattr(mw, "active_project", store.get)
    monkeypatch.setattr(mw, "set_active_project", store.set)
    monkeypatch.setattr(mw, "clear_active_project", store.clear)
    nav = ProjectNav()
    for n in names:
        nav.addTab(_FakeTab(n), n)
    nav.setCurrentIndex(selected)
    return nav, store


def test_closing_the_active_tab_hands_active_to_the_visible_tab(qapp, monkeypatch):
    """The exact prod shape: selected first row closed, one tab left."""
    nav, store = _setup(monkeypatch, ["saas_admin_amb", "unirecon"], 0, "saas_admin_amb")

    ok, _msg = mw.MainWindow._close_project_tab(_window(nav), "saas_admin_amb")

    assert ok
    assert store.active == "unirecon"


def test_closing_the_last_tab_clears_active(qapp, monkeypatch):
    nav, store = _setup(monkeypatch, ["saas_admin_amb"], 0, "saas_admin_amb")

    mw.MainWindow._close_project_tab(_window(nav), "saas_admin_amb")

    assert store.active is None


def test_closing_a_background_tab_leaves_active_alone(qapp, monkeypatch):
    nav, store = _setup(monkeypatch, ["saas_admin_amb", "unirecon"], 1, "unirecon")

    mw.MainWindow._close_project_tab(_window(nav), "saas_admin_amb")

    assert store.active == "unirecon"


def test_closing_a_tab_unregisters_every_teammate_from_the_orchestrator(qapp, monkeypatch):
    """Review 2026-09-23 (main_window.py:1588): `_close_project_tab` clears
    `tab.teammate_panes` BEFORE `close_all_teammates`, which turns the
    deferred `_teardown` (the only GUI-side `unregister_pane` for teammates)
    into a no-op — so the dead panes stayed in `_panes_by_project` after the
    tab was deleted. Reopening the project and assigning the same role then
    found the stale pane, skipped `paneRequested`, and failed on the deleted
    terminal on every attempt until the cockpit restarted."""
    nav, _store = _setup(monkeypatch, ["saas_admin_amb"], 0, "saas_admin_amb")
    win = _window(nav)
    tab = nav.widget(0)
    tab.teammate_panes = {"qa": object(), "backend#1": object()}
    registry = {"saas_admin_amb": {"lead": object(), **tab.teammate_panes}}
    unregistered: list[tuple[str, str | None, bool]] = []

    def _unregister(role, project=None, force=False):
        unregistered.append((role, project, force))
        registry.get(project, {}).pop(role, None)

    win.orch._project_panes = lambda project=None: registry.setdefault(project, {})
    win.orch.unregister_pane = _unregister

    ok, _msg = mw.MainWindow._close_project_tab(win, "saas_admin_amb")

    assert ok
    assert registry["saas_admin_amb"] == {}, "every pane must leave the registry on tab close"
    # Teammates are popped without force; Lead keeps its force=True contract.
    assert ("qa", "saas_admin_amb", False) in unregistered
    assert ("backend#1", "saas_admin_amb", False) in unregistered
    assert ("lead", "saas_admin_amb", True) in unregistered
