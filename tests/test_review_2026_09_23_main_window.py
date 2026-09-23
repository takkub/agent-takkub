"""System review 2026-09-23 — main_window.py regressions.

Three findings, each pinned by the scenario the verifiers reproduced:

* :975 preset roles spawned into whichever project was `active` 15 s after
  boot, and the boot tab-restore flipped `active`/focus to the LAST restored
  tab on every multi-tab boot.
* :1062 Lead never spawned while the cockpit was not the foreground app, and
  the 50 ms gate poll wrote a `boot_lead_gate_blocked` line per tick (845 in
  one morning; events.log rotated in minutes).
* :1524 a Lead-spawn failure on the only tab deleted the shared usage corner
  with the tab — every status tick raised, and every later tab switch raised
  on the dead host before it could set `active`.

(:1588 — teammates never unregistered on tab close — lives with the other
close-tab guards in test_close_tab_active_project.py.)

Hermetic: no real MainWindow, no PTY, no network. The tab-lifecycle tests
drive a real `ProjectNav` with fake tabs, like test_close_tab_active_project.
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent, QSettings, Qt
from PyQt6.QtWidgets import QApplication, QTabWidget, QWidget

from agent_takkub import main_window as mw
from agent_takkub import project_nav as project_nav_module
from agent_takkub.project_nav import ProjectNav
from agent_takkub.roles import LEAD

from ._qt_timer_leak_guard import stop_timers_after


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path: pathlib.Path):
    monkeypatch.setattr(project_nav_module, "list_project_names", lambda: [])
    ini = str(tmp_path / "nav.ini")
    monkeypatch.setattr(
        project_nav_module,
        "QSettings",
        lambda *_a, **_k: QSettings(ini, QSettings.Format.IniFormat),
    )
    # _on_tab_switched kicks a background graph build for the project —
    # never in a test.
    import agent_takkub.graft_autobuild as graft_autobuild

    monkeypatch.setattr(graft_autobuild, "ensure_project_graph_async", lambda _name: None)
    finalize = stop_timers_after(monkeypatch, ProjectNav, "_pending_timer")
    yield
    finalize()


# ──────────────────────────────────────────────────────────────────────────────
# shared harness: real ProjectNav + fake tabs, real MainWindow methods under test
# ──────────────────────────────────────────────────────────────────────────────


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


class _FakeTab(QWidget):
    """ProjectTab stand-in (patched in as `mw.ProjectTab`): a real
    `pane_tabs` QTabWidget so the usage corner really is reparented into
    it, no explorer/terminal."""

    def __init__(self, name: str, lead_pane=None) -> None:
        super().__init__()
        self.project_name = name
        self.teammate_panes: dict = {}
        self.lead_pane = lead_pane
        self.explorer = None
        self.pane_tabs = QTabWidget(self)
        self.keepalive: list[bool] = []

    def attach_lead(self, pane) -> None:
        self.lead_pane = pane

    def mount_usage_widget(self, widget: QWidget) -> None:
        self.pane_tabs.setCornerWidget(widget, Qt.Corner.TopRightCorner)
        widget.show()

    def set_keepalive(self, active: bool) -> None:
        self.keepalive.append(bool(active))


def _fake_agent_pane(role, parent=None):
    return SimpleNamespace(
        role=role,
        parent=parent,
        _terminal=SimpleNamespace(destroy_terminal=lambda: None),
        _title=SimpleNamespace(setText=lambda *_a: None),
    )


class _Orch:
    def __init__(self, spawn_results: list[tuple[bool, str]]) -> None:
        self.spawn_results = list(spawn_results)
        self.registry: dict[str, dict] = {}
        self.diag_unregistered: list[str] = []

    def _project_panes(self, project=None):
        return self.registry.setdefault(project, {})

    def register_pane(self, pane, project=None) -> None:
        self._project_panes(project)[pane.role.name] = pane

    def unregister_pane(self, role, project=None, force=False) -> None:
        self._project_panes(project).pop(role, None)

    def spawn(self, role, project=None, **_kw):
        return self.spawn_results.pop(0)

    def unregister_workspace_diag_sources(self, project) -> None:
        self.diag_unregistered.append(project)

    def close_all_teammates(self, project=None) -> None:
        pass

    def close(self, *_a, **_k):
        return True, ""

    def preview_command(self, *_a, **_k) -> None:
        pass


class _Window:
    """Only the MainWindow methods under test are real; every collaborator
    they touch is a recording stub."""

    _tab_for_project = mw.MainWindow._tab_for_project
    _open_projects = mw.MainWindow._open_projects
    _open_project_tab = mw.MainWindow._open_project_tab
    _close_project_tab = mw.MainWindow._close_project_tab
    _on_tab_switched = mw.MainWindow._on_tab_switched
    # getattr so a checkout without the helper fails per test, not at import.
    _release_usage_corner = getattr(mw.MainWindow, "_release_usage_corner", None)

    def __init__(self, nav: ProjectNav, orch: _Orch) -> None:
        self.tabs = nav
        self.orch = orch
        self._limit_store = None
        self._usage_corner = QWidget()
        self._limit_label_host: QWidget | None = None
        self.messages: list[str] = []
        self._status = SimpleNamespace(showMessage=lambda msg, *_a: self.messages.append(msg))
        self._tasks_dock_widget = SimpleNamespace(set_project=lambda *_a: None)
        # Exceptions inside a Qt slot never propagate to the test — record
        # them instead of letting PyQt swallow (or abort on) them.
        self.slot_errors: list[BaseException] = []
        nav.currentChanged.connect(self._switched)

    def _switched(self, index: int) -> None:
        try:
            self._on_tab_switched(index)
        except Exception as exc:  # the point is to surface it
            self.slot_errors.append(exc)

    def _wire_project_tab(self, tab) -> None:
        pass

    def _refresh_project_list(self) -> None:
        pass

    def _ensure_project_skill_links(self, name: str) -> None:
        pass

    def _refresh_rtk_button(self) -> None:
        pass

    def _persist_open_tabs(self) -> None:
        pass

    def _sync_preview_to_active_tab(self, project) -> None:
        pass

    def _refresh_limit_label(self, *_a) -> None:
        pass


def _harness(monkeypatch, *, spawn_results, initial: str | None = None):
    store = _ActiveStore(initial)
    monkeypatch.setattr(mw, "ProjectTab", _FakeTab)
    monkeypatch.setattr(mw, "AgentPane", _fake_agent_pane)
    monkeypatch.setattr(mw, "project_folder_exists", lambda _name: True)
    monkeypatch.setattr(mw, "active_project", store.get)
    monkeypatch.setattr(mw, "set_active_project", store.set)
    monkeypatch.setattr(mw, "clear_active_project", store.clear)
    nav = ProjectNav()
    win = _Window(nav, _Orch(spawn_results))
    if initial is not None:
        tab = _FakeTab(initial)
        nav.addTab(tab, initial)
        nav.setCurrentIndex(0)
        assert win._limit_label_host is tab  # mounted by _on_tab_switched(0)
    return win, store


def _flush_deferred_deletes(qapp) -> None:
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


# ──────────────────────────────────────────────────────────────────────────────
# :1524 — Lead-spawn failure on the sole tab must not take the usage corner
# ──────────────────────────────────────────────────────────────────────────────


def test_lead_spawn_failure_on_the_sole_tab_keeps_the_usage_corner_alive(qapp, monkeypatch):
    """Empty state → `+` → Lead spawn fails. The failed tab hosted the shared
    corner (mounted by _on_tab_switched when it became current); deleting the
    tab used to destroy the corner's C++ side while `_limit_label_host` kept
    pointing at the dead tab."""
    win, store = _harness(monkeypatch, spawn_results=[(False, "provider binary missing")])
    corner = win._usage_corner

    win._open_project_tab("alpha")
    _flush_deferred_deletes(qapp)

    assert win.slot_errors == []
    assert win.tabs.count() == 0
    assert not sip.isdeleted(corner), "usage corner must survive the failed tab"
    assert corner.parent() is None
    assert win._limit_label_host is None
    assert store.active is None
    # The failed tab's explorer index must not linger for `takkub doctor`.
    assert win.orch.diag_unregistered == ["alpha"]
    assert any("Lead spawn failed for alpha" in m for m in win.messages)


def test_next_open_after_a_sole_tab_failure_remounts_corner_and_sets_active(qapp, monkeypatch):
    """The follow-on damage: with a dead host, every later _on_tab_switched
    raised at `self._limit_label_host.pane_tabs...` BEFORE set_active_project,
    so `active` desynced from the visible tab (Restart Lead / provider switch
    then acted on the wrong project)."""
    win, store = _harness(monkeypatch, spawn_results=[(False, "boom"), (True, ""), (True, "")])
    corner = win._usage_corner

    win._open_project_tab("alpha")
    _flush_deferred_deletes(qapp)
    win._open_project_tab("beta")
    win._open_project_tab("gamma")

    assert win.slot_errors == []
    gamma = win.tabs.widget(1)
    assert gamma.project_name == "gamma"
    assert win._limit_label_host is gamma
    assert gamma.pane_tabs.cornerWidget(Qt.Corner.TopRightCorner) is corner
    assert store.active == "gamma"

    # A plain sidebar switch (no open) must still move `active` + the corner.
    win.tabs.setCurrentIndex(0)
    assert win.slot_errors == []
    beta = win.tabs.widget(0)
    assert win._limit_label_host is beta
    assert beta.pane_tabs.cornerWidget(Qt.Corner.TopRightCorner) is corner
    assert store.active == "beta"


def test_lead_spawn_failure_on_a_second_tab_hands_corner_back(qapp, monkeypatch):
    """Multi-tab failure was already safe (removeTab re-emits currentChanged
    onto the surviving tab) — pin it so the helper doesn't regress it."""
    win, store = _harness(monkeypatch, spawn_results=[(False, "boom")], initial="alpha")
    alpha = win.tabs.widget(0)

    win._open_project_tab("beta")
    _flush_deferred_deletes(qapp)

    assert win.slot_errors == []
    assert not sip.isdeleted(win._usage_corner)
    assert win._limit_label_host is alpha
    assert alpha.pane_tabs.cornerWidget(Qt.Corner.TopRightCorner) is win._usage_corner
    assert store.active == "alpha"


# ──────────────────────────────────────────────────────────────────────────────
# :975 — boot tab restore must not steal focus/active; presets pin their project
# ──────────────────────────────────────────────────────────────────────────────


def test_open_project_tab_in_background_keeps_focus_and_active(qapp, monkeypatch):
    """`make_current=False` (boot restore): the tab is built and its Lead
    spawned, but the boot tab stays current, `active` stays put, and the
    new tab starts suspended like every other hidden project."""
    win, store = _harness(monkeypatch, spawn_results=[(True, "")], initial="alpha")
    alpha = win.tabs.widget(0)

    win._open_project_tab("beta", make_current=False)

    assert win.slot_errors == []
    assert win.tabs.count() == 2
    assert win.tabs.currentIndex() == 0
    assert store.active == "alpha"
    assert win._limit_label_host is alpha
    beta = win.tabs.widget(1)
    assert beta.keepalive == [False]
    assert LEAD.name in win.orch.registry["beta"]


def test_open_project_tab_default_still_focuses_and_activates(qapp, monkeypatch):
    """The `+` picker path is unchanged: the new tab becomes current + active."""
    win, store = _harness(monkeypatch, spawn_results=[(True, "")], initial="alpha")

    win._open_project_tab("beta")

    assert win.slot_errors == []
    assert win.tabs.currentIndex() == 1
    assert store.active == "beta"
    assert win._limit_label_host is win.tabs.widget(1)


class _BootStub:
    """`_boot()` on a MainWindow whose __init__ never ran — the pattern
    test_cli_bind_error.py uses. Only the attributes `_boot` touches."""

    @staticmethod
    def make(monkeypatch, *, active: str | None, presets: list[str], open_tabs: list[str]):
        with patch.object(mw.MainWindow, "__init__", lambda self: None):
            win = mw.MainWindow.__new__(mw.MainWindow)
        win._status = MagicMock()
        win.cli = MagicMock()
        win.cli.listen.return_value = 54321
        win.orch = MagicMock()
        win.orch.spawn.return_value = (True, "ok")
        win._lead_first_input_fired = set()
        fake_lead = MagicMock()
        monkeypatch.setattr(mw.MainWindow, "lead_pane", property(lambda self: fake_lead))
        store = _ActiveStore(active)
        monkeypatch.setattr(mw, "active_project", store.get)
        monkeypatch.setattr(mw, "project_folder_exists", lambda _name: True)
        monkeypatch.setattr(mw, "preset_roles_for_active", lambda: list(presets))
        monkeypatch.setattr(mw, "get_open_tabs", lambda: list(open_tabs))
        monkeypatch.setattr(mw.MainWindow, "_refresh_rtk_button", lambda self: None)
        monkeypatch.setattr(mw.MainWindow, "_restore_teammates_from_snapshot", lambda self: None)
        monkeypatch.setattr(mw.MainWindow, "_open_projects", lambda self: [active or "default"])
        monkeypatch.setattr(mw.MainWindow, "_persist_open_tabs", lambda self: None)
        opened: list[tuple[str, bool]] = []
        monkeypatch.setattr(
            mw.MainWindow,
            "_open_project_tab",
            lambda self, name, make_current=True: opened.append((name, make_current)),
        )
        timers: list[tuple[int, object]] = []
        monkeypatch.setattr(
            mw, "QTimer", SimpleNamespace(singleShot=lambda ms, fn: timers.append((ms, fn)))
        )
        return win, store, timers, opened


def test_boot_presets_spawn_into_the_boot_project_even_after_active_flips(qapp, monkeypatch):
    """projects.json: A active with presets=[frontend], open_tabs=[A, B].
    The restore of B runs at 2.5 s, the preset spawn at 15 s — the preset
    must land in A regardless of what `active` says by then."""
    win, store, timers, _opened = _BootStub.make(
        monkeypatch, active="alpha", presets=["frontend"], open_tabs=["alpha", "beta"]
    )

    win._boot()

    preset_fns = [fn for ms, fn in timers if ms == 15_000]
    assert len(preset_fns) == 1
    store.active = "beta"  # what the old tab restore did before the preset fired
    preset_fns[0]()
    win.orch.spawn.assert_called_once_with("frontend", project="alpha")


def test_boot_restores_extra_tabs_in_the_background(qapp, monkeypatch):
    win, _store, timers, opened = _BootStub.make(
        monkeypatch, active="alpha", presets=[], open_tabs=["alpha", "beta", "gamma"]
    )

    win._boot()

    restore_fns = [fn for ms, fn in timers if ms in (2_500, 6_500)]
    assert len(restore_fns) == 2
    for fn in restore_fns:
        fn()
    assert opened == [("beta", False), ("gamma", False)]


def test_boot_with_no_active_project_lets_the_restored_tab_take_focus(qapp, monkeypatch):
    """Bare "default" placeholder tab (no active project): the old behaviour
    — the restored tab becomes current — is the right one there."""
    win, _store, timers, opened = _BootStub.make(
        monkeypatch, active=None, presets=[], open_tabs=["beta"]
    )

    win._boot()

    next(fn for ms, fn in timers if ms == 2_500)()
    assert opened == [("beta", True)]


# ──────────────────────────────────────────────────────────────────────────────
# :1062 — Lead must spawn without foreground focus; gate log once per reason
# ──────────────────────────────────────────────────────────────────────────────


def _gate_window(*, wait_active: bool):
    win = MagicMock()
    win._boot_quiet_count = 0
    win.isVisible.return_value = True
    win.orch._spawn_gate_pred = None
    win.orch.spawn.return_value = (True, "spawned")
    win._boot_lead_wait_active = wait_active
    win._boot_lead_last_block = None
    return win


class _InactiveApp:
    """Patch bundle: application NOT active, InSendMessageEx clear, timers
    recorded, `_log_event` recorded."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.timers: list[int] = []

    def __enter__(self):
        active_state = object()
        self._patches = [
            patch("agent_takkub.main_window.QApplication"),
            patch("agent_takkub.main_window.QTimer"),
            patch("agent_takkub.main_window.Qt"),
            patch("agent_takkub.spawn_gate.is_in_send_blocked", return_value=False),
            patch(
                "agent_takkub.main_window._log_event",
                side_effect=lambda ev, **kw: self.events.append((ev, kw)),
            ),
        ]
        mock_qa, mock_qt, mock_qt_class, _isb, _log = [p.__enter__() for p in self._patches]
        mock_qt_class.ApplicationState.ApplicationActive = active_state
        mock_qa.applicationState.return_value = object()  # never the active sentinel
        mock_qt.singleShot.side_effect = lambda ms, _fn: self.timers.append(ms)
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.__exit__(*exc)
        return False


def test_lead_spawns_without_foreground_focus_once_the_grace_is_over(qapp):
    """Cockpit launched behind another window / from the phone: after the
    grace timer flipped `_boot_lead_wait_active`, ApplicationInactive must
    not reset the quiet streak — N clear turns spawn Lead."""
    win = _gate_window(wait_active=False)

    with _InactiveApp():
        for _ in range(mw._BOOT_LEAD_QUIET_N):
            mw.MainWindow._spawn_lead_when_quiet(win)

    win.orch.spawn.assert_called_once_with(LEAD.name)


def test_lead_still_waits_for_focus_during_the_grace_window(qapp):
    win = _gate_window(wait_active=True)

    with _InactiveApp():
        for _ in range(mw._BOOT_LEAD_QUIET_N + 2):
            mw.MainWindow._spawn_lead_when_quiet(win)

    assert win.orch.spawn.call_count == 0
    assert win._boot_quiet_count == 0


def test_gate_blocked_is_logged_once_per_reason_not_per_poll(qapp):
    """845 `boot_lead_gate_blocked` lines in one morning boot (20/s) rotated
    events.log; the same reason must produce one line, a new reason one more."""
    win = _gate_window(wait_active=True)

    with _InactiveApp() as env:
        for _ in range(20):
            mw.MainWindow._spawn_lead_when_quiet(win)
        blocked = [kw for ev, kw in env.events if ev == "boot_lead_gate_blocked"]
        assert len(blocked) == 1
        assert blocked[0]["app_active"] is False
        # Reason changes (window also not visible) → exactly one more line.
        win.isVisible.return_value = False
        for _ in range(20):
            mw.MainWindow._spawn_lead_when_quiet(win)
        blocked = [kw for ev, kw in env.events if ev == "boot_lead_gate_blocked"]
        assert len(blocked) == 2
        assert blocked[1]["window_ready"] is False
        # Every blocked poll still reschedules itself.
        assert env.timers.count(mw._BOOT_LEAD_POLL_MS) == 40


def test_boot_schedules_the_inactive_grace_timer(qapp, monkeypatch):
    win, _store, timers, _opened = _BootStub.make(
        monkeypatch, active="alpha", presets=[], open_tabs=["alpha"]
    )

    win._boot()

    assert win._boot_lead_wait_active is True
    grace = [fn for ms, fn in timers if ms == mw._BOOT_LEAD_INACTIVE_GRACE_MS]
    assert len(grace) == 1
    grace[0]()
    assert win._boot_lead_wait_active is False
