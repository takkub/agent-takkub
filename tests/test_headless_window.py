"""HeadlessWindow (#105 Phase B) — display-free stand-in for MainWindow.

Exercises the pane-registry glue (`_ensure_teammate_pane`/
`_remove_teammate_pane`, wired to `paneRequested`/`paneClosed`) and the
project-tab lifecycle (`_open_project_tab`/`_close_project_tab`, the methods
`remote/api.py` reaches via `orch.parent()`) with no QWidget anywhere —
`orch.spawn()` itself (real ConPTY/claude-process spawning) is mocked out;
that machinery is covered by the existing spawn_engine test suite.
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import config
from agent_takkub import orchestrator as orch_mod
from agent_takkub.headless_pane import HeadlessPane
from agent_takkub.headless_window import HeadlessWindow, _HeadlessTab
from agent_takkub.roles import LEAD


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def project_env(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """A real projects.json with one project ("proj") whose Lead cwd exists
    on disk, so project_folder_exists()/set_active_project() succeed."""
    proj_dir = tmp_path / "proj" / "api"
    proj_dir.mkdir(parents=True)
    pj = tmp_path / "projects.json"
    pj.write_text(
        json.dumps({"active": None, "projects": {"proj": {"paths": {"api": str(proj_dir)}}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "PROJECTS_JSON", pj)
    cockpit = tmp_path / "cockpit"
    monkeypatch.setattr(config, "REPO_ROOT", cockpit)
    monkeypatch.setattr(orch_mod, "REPO_ROOT", cockpit)
    return pj


@pytest.fixture
def window(qapp: QCoreApplication, project_env: pathlib.Path) -> HeadlessWindow:
    w = HeadlessWindow()
    w.orch._idle_watchdog.stop()
    w.orch._hot_md_timer.stop()
    yield w


def _pump(app: QCoreApplication, n: int = 5) -> None:
    """Drain queued QTimer.singleShot(0, ...) callbacks."""
    for _ in range(n):
        app.processEvents()


class TestOpenCloseProjectTab:
    def test_open_project_tab_registers_lead_and_spawns(
        self, window: HeadlessWindow, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spawn = MagicMock(return_value=(True, "ok"))
        monkeypatch.setattr(window.orch, "spawn", spawn)

        window._open_project_tab("proj")

        assert "proj" in window._tabs
        tab = window._tabs["proj"]
        assert isinstance(tab.lead_pane, HeadlessPane)
        assert window.orch._project_panes("proj")[LEAD.name] is tab.lead_pane
        spawn.assert_called_once_with(LEAD.name, project="proj")
        assert config.get_open_tabs() == ["proj"]

    def test_open_project_tab_missing_folder_is_noop(
        self, window: HeadlessWindow, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spawn = MagicMock(return_value=(True, "ok"))
        monkeypatch.setattr(window.orch, "spawn", spawn)

        window._open_project_tab("no-such-project")

        assert "no-such-project" not in window._tabs
        spawn.assert_not_called()

    def test_open_project_tab_idempotent(
        self, window: HeadlessWindow, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spawn = MagicMock(return_value=(True, "ok"))
        monkeypatch.setattr(window.orch, "spawn", spawn)

        window._open_project_tab("proj")
        window._open_project_tab("proj")

        assert spawn.call_count == 1

    def test_close_project_tab_tears_down_lead_and_teammates(
        self, window: HeadlessWindow, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(window.orch, "spawn", MagicMock(return_value=(True, "ok")))
        window._open_project_tab("proj")
        window._ensure_teammate_pane("backend", "proj")

        close_all = MagicMock(return_value=(True, "ok"))
        monkeypatch.setattr(window.orch, "close_all_teammates", close_all)

        ok, _msg = window._close_project_tab("proj")

        assert ok is True
        assert "proj" not in window._tabs
        close_all.assert_called_once_with(project="proj")
        assert window.orch._project_panes("proj").get(LEAD.name) is None
        assert config.get_open_tabs() == []

    def test_close_project_tab_unknown_project(self, window: HeadlessWindow) -> None:
        ok, msg = window._close_project_tab("never-opened")
        assert ok is False
        assert "never-opened" in msg


class TestTeammatePaneLifecycle:
    def test_ensure_teammate_pane_creates_and_registers(
        self, window: HeadlessWindow, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(window.orch, "spawn", MagicMock(return_value=(True, "ok")))
        window._open_project_tab("proj")

        window._ensure_teammate_pane("backend", "proj")

        tab = window._tabs["proj"]
        pane = tab.teammate_panes.get("backend")
        assert isinstance(pane, HeadlessPane)
        assert pane.role.name == "backend"
        assert window.orch._project_panes("proj")["backend"] is pane

    def test_ensure_teammate_pane_ignores_lead(self, window: HeadlessWindow) -> None:
        window._tabs["proj"] = _HeadlessTab("proj")
        window._ensure_teammate_pane(LEAD.name, "proj")
        assert window._tabs["proj"].teammate_panes == {}

    def test_ensure_teammate_pane_unknown_project_is_noop(self, window: HeadlessWindow) -> None:
        window._ensure_teammate_pane("backend", "no-tab-open")
        assert window.orch._project_panes("no-tab-open").get("backend") is None

    def test_ensure_teammate_pane_shard_gets_own_key(self, window: HeadlessWindow) -> None:
        window._tabs["proj"] = _HeadlessTab("proj")
        window._ensure_teammate_pane("qa#1", "proj")
        pane = window._tabs["proj"].teammate_panes["qa#1"]
        assert pane.role.name == "qa#1"
        assert pane.role.label == "QA#1"

    def test_ensure_teammate_pane_custom_role_gets_fallback_color(
        self, window: HeadlessWindow
    ) -> None:
        window._tabs["proj"] = _HeadlessTab("proj")
        window._ensure_teammate_pane("designer2", "proj")
        pane = window._tabs["proj"].teammate_panes["designer2"]
        assert pane.role.name == "designer2"
        assert pane.role.color == "#94a3b8"

    def test_remove_teammate_pane_unregisters_on_next_tick(
        self, window: HeadlessWindow, qapp: QCoreApplication
    ) -> None:
        window._tabs["proj"] = _HeadlessTab("proj")
        window._ensure_teammate_pane("backend", "proj")
        assert window.orch._project_panes("proj").get("backend") is not None

        window._remove_teammate_pane("backend", "proj")
        _pump(qapp)

        assert "backend" not in window._tabs["proj"].teammate_panes
        assert window.orch._project_panes("proj").get("backend") is None

    def test_remove_teammate_pane_unknown_project_is_noop(
        self, window: HeadlessWindow, qapp: QCoreApplication
    ) -> None:
        # Must not raise even though no tab is open for this project.
        window._remove_teammate_pane("backend", "never-opened")
        _pump(qapp)


class TestPaneRequestedSignalWiring:
    def test_pane_requested_signal_drives_ensure_teammate_pane(
        self, window: HeadlessWindow
    ) -> None:
        window._tabs["proj"] = _HeadlessTab("proj")
        window.orch.paneRequested.emit("backend", "proj")
        assert "backend" in window._tabs["proj"].teammate_panes
