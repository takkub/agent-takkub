"""HeadlessWindow: display-free stand-in for MainWindow (#105 Phase B).

Gives `Orchestrator` the exact QObject-parent surface `main_window.py`
provides in the desktop build:

  - creates/tears down panes on `paneRequested`/`paneClosed` (every teammate
    `AgentPane` becomes a `HeadlessPane` — no QWebEngineView, no tab widget)
  - `_open_project_tab`/`_close_project_tab`, the methods `remote/api.py`
    reaches dynamically via `orch.parent()` for the PWA's project open/close
    control-mode actions

so the whole engine — spawn, watchdogs, task ledger, `lead_inbox`,
`cli_server`, remote control — runs with zero QtWidgets/QWebEngineView
construction. The PWA (remote-control) is the only UI surface in this mode;
see `python -m agent_takkub.headless` and
docs/design/2026-07-11-105-phaseB-headless.md.
"""

from __future__ import annotations

from PyQt6.QtCore import QCoreApplication, QObject, QTimer

from . import cockpit_theme
from .cli_server import CliServer
from .config import (
    active_project,
    get_open_tabs,
    list_project_names,
    project_folder_exists,
    set_active_project,
    set_open_tabs,
)
from .headless_pane import HeadlessPane
from .orchestrator import Orchestrator, _log_event, _split_shard
from .roles import LEAD, Role, by_name


class _HeadlessTab:
    """Bookkeeping for one open project — the headless analogue of
    `ProjectTab`'s `lead_pane`/`teammate_panes`, with no widget underneath."""

    __slots__ = ("lead_pane", "project_name", "teammate_panes")

    def __init__(self, project_name: str) -> None:
        self.project_name = project_name
        self.lead_pane: HeadlessPane | None = None
        self.teammate_panes: dict[str, HeadlessPane] = {}


class HeadlessWindow(QObject):
    """Owns the orchestrator + every open project's panes with no display.

    Constructed by `python -m agent_takkub.headless` in place of
    `MainWindow`. Requires a running Qt event loop (`QCoreApplication` is
    enough — no GUI platform plugin needed) so the orchestrator's `QTimer`
    watchdogs (stuck-pane, idle, hot.md snapshot, ...) keep firing.
    """

    def __init__(self) -> None:
        super().__init__()
        self.orch = Orchestrator(self)
        # No spawn-gate predicate is set: `_spawn_gate_pred=None` is the
        # documented no-guard path ("tests, headless paths" — see
        # orchestrator.py's Spawn arbiter comment). Headless mode has no Qt
        # modal/popup surface to guard ConPTY spawn against.
        self.cli = CliServer(self.orch, self)
        self.orch.paneRequested.connect(self._ensure_teammate_pane)
        self.orch.paneClosed.connect(self._remove_teammate_pane)
        self.orch.restartRequested.connect(self._on_restart_requested)

        self._tabs: dict[str, _HeadlessTab] = {}
        self._remote = None

    # ──────────────────────────────────────────────────────────────
    # boot
    # ──────────────────────────────────────────────────────────────
    def boot(self) -> int:
        """Bind the CLI server, start remote control if configured, open
        every saved project tab, and spawn each project's Lead.

        Returns the bound CLI port. Raises whatever `CliServer.listen()`
        raises on a bind failure — the caller decides how to surface that
        (no `QMessageBox` here; there is no display)."""
        port = self.cli.listen()
        _log_event("headless_cli_listening", port=port)

        try:
            import importlib

            _remote_mod = importlib.import_module("agent_takkub.remote")
            self._remote = _remote_mod.RemoteControl.maybe_start(self.orch)
        except ModuleNotFoundError:  # folder deleted = uninstall no-op (B4)
            self._remote = None
        except Exception:
            self._remote = None
            _log_event("headless_remote_boot_failed")

        active = active_project()[0]
        saved = get_open_tabs()
        names = list(dict.fromkeys(([active] if active else []) + saved))
        if not names:
            first = list_project_names()
            names = first[:1]
        for name in names:
            self._open_project_tab(name)
        return port

    def shutdown(self) -> None:
        """Best-effort teardown for every open project — used by the
        entrypoint's signal handler (SIGTERM/SIGINT) so a `docker stop`
        terminates every live claude/codex/agy child instead of orphaning
        them."""
        if self._remote is not None:
            try:
                self._remote.stop()
            except Exception:
                pass
        for project_name in list(self._tabs.keys()):
            self._close_project_tab(project_name, confirm=False)
        try:
            self.orch.close_native_chrome()
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────
    # project lifecycle — mirrors main_window._open_project_tab /
    # _close_project_tab (reached dynamically via `orch.parent()` by
    # remote/api.py's open_project/close_project control-mode actions)
    # ──────────────────────────────────────────────────────────────
    def _open_project_tab(self, project_name: str) -> None:
        if not project_folder_exists(project_name):
            _log_event("headless_open_tab_missing_folder", project=project_name)
            return
        if project_name in self._tabs:
            return
        set_active_project(project_name)
        tab = _HeadlessTab(project_name)
        self._tabs[project_name] = tab
        lead = HeadlessPane(LEAD)
        self.orch.register_pane(lead, project=project_name)
        tab.lead_pane = lead

        ok, msg = self.orch.spawn(LEAD.name, project=project_name)
        if not ok:
            _log_event("headless_lead_spawn_failed", project=project_name, msg=msg)
        self._persist_open_tabs()

    def _close_project_tab(self, project: str, confirm: bool = False) -> tuple[bool, str]:
        tab = self._tabs.get(project)
        if tab is None:
            return False, f"no open tab for project '{project}'"
        self.orch.close_all_teammates(project=project)
        self.orch.close(LEAD.name, project=project, force=True, reason="tab_close")
        self.orch.unregister_pane(LEAD.name, project=project, force=True)
        del self._tabs[project]
        self._persist_open_tabs()
        return True, f"closed tab · {project}"

    def _persist_open_tabs(self) -> None:
        try:
            set_open_tabs(list(self._tabs.keys()))
        except Exception:
            _log_event("headless_persist_open_tabs_failed")

    # ──────────────────────────────────────────────────────────────
    # teammate pane lifecycle — mirrors
    # main_window._ensure_teammate_pane / _remove_teammate_pane
    # ──────────────────────────────────────────────────────────────
    def _ensure_teammate_pane(self, role_name: str, project: str) -> None:
        if role_name == LEAD.name:
            return
        tab = self._tabs.get(project)
        if tab is None:
            return
        existing = tab.teammate_panes.get(role_name)
        if existing is not None:
            if self.orch._project_panes(tab.project_name).get(role_name) is not existing:
                self.orch.register_pane(existing, project=tab.project_name)
            return

        base_role, shard_idx = _split_shard(role_name)
        role = by_name(base_role) if shard_idx is not None else by_name(role_name)
        if role is None:
            role = Role(
                name=role_name,
                label=role_name.capitalize(),
                color=cockpit_theme.ROLE_COLOR_FALLBACK,
                column=2,
                row=99,
            )
        elif shard_idx is not None:
            role = Role(
                name=role_name,
                label=f"{role.label}#{shard_idx}",
                color=role.color,
                column=role.column,
                row=role.row,
            )

        pane = HeadlessPane(role)
        self.orch.register_pane(pane, project=tab.project_name)
        tab.teammate_panes[role_name] = pane
        _log_event("headless_pane_opened", role=role_name, project=project)

    def _remove_teammate_pane(self, role_name: str, project: str) -> None:
        tab = self._tabs.get(project)
        if tab is None:
            return

        # Deferred to the next event-loop tick for the same reason
        # main_window's _remove_teammate_pane defers its teardown: this slot
        # runs synchronously inside Orchestrator.close()'s paneClosed.emit(),
        # and unregister_pane() mutating the registry reentrantly inside that
        # emit is the pattern that crashed the desktop build (0xc0000409).
        # HeadlessPane holds no WebEngine view, but the same emit-stack
        # discipline is kept here to stay behaviorally aligned.
        def _teardown() -> None:
            pane = tab.teammate_panes.pop(role_name, None)
            if pane is None:
                return
            self.orch.unregister_pane(role_name, project=project)

        QTimer.singleShot(0, _teardown)

    # ──────────────────────────────────────────────────────────────
    def _on_restart_requested(self) -> None:
        # No process-relaunch story in headless mode (that's a desktop-only
        # `os.execv`/relaunch dance in app.py) — state is already persisted
        # by the caller before this fires, same as the desktop path. Exit
        # cleanly so a `restart: unless-stopped` container policy relaunches
        # the process; see docs/guides/2026-07-11-headless-docker.md.
        _log_event("headless_restart_requested")
        app = QCoreApplication.instance()
        if app is not None:
            QTimer.singleShot(200, app.quit)
