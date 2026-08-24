"""manager.py — OpenVikingManager: process-lifecycle state machine for the
managed local OpenViking sidecar (`05_PROCESS_LIFECYCLE.md`).

Startup:
    disabled                         -> zero-cost, touches nothing
    user TAKKUB_OPENVIKING_URL set   -> respected as-is, never spawn/manage
    an existing process is healthy   -> use it (owned iff THIS manager spawned it)
    unavailable + managed install    -> spawn locally, poll /health (bounded)
    anything fails                   -> Takkub continues without OpenViking (fail-open)

Shutdown: only a process this manager itself spawned (`owned=True`) is ever
killed — an externally-owned OpenViking is never touched.

Crash: `restart()` applies bounded backoff with a capped retry count, then
disables OpenViking for the rest of this session rather than retrying forever.

Synchronous, blocking I/O throughout (subprocess spawn, HTTP polling) — same
contract `core.context_sources.openviking_adapter.health()` already
documents for its own callers: MUST NOT run on the Qt main thread. A caller
wraps this in a worker thread (see `settings_knowledge_design.py`'s
`_CallableThread` for the pattern already used with `adapter.health()`).

No auto-install/auto-start at cockpit boot — every method here only runs on
an explicit caller action (Wave 1 constraint, `16_PHASES.md` item 5).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

from .. import openviking_settings
from ..core.context_sources import openviking_adapter
from . import installer, port
from .process import OpenVikingProcess, ProcessError, is_process_alive, reap_orphan_process

_log = logging.getLogger(__name__)

_HEALTH_TIMEOUT_S = 2.0
_START_POLL_TIMEOUT_S = 10.0
_START_POLL_INTERVAL_S = 0.5
_MAX_RESTART_ATTEMPTS = 3
_RESTART_BACKOFF_S = (1.0, 3.0, 8.0)


@dataclass(frozen=True, slots=True)
class ManagerStatus:
    enabled: bool
    healthy: bool
    owned: bool  # True: this manager instance spawned the process it's reporting on
    url: str | None
    installed: bool
    error: str | None = None


class OpenVikingManager:
    """One instance per cockpit process. `start()` is idempotent — safe to
    call repeatedly (Settings UI "Start" button, doctor, CLI `ov managed
    start` alike all converge on the same state machine)."""

    def __init__(self) -> None:
        self._process: OpenVikingProcess | None = None
        self._owned = False
        self._restart_attempts = 0
        self._disabled_for_session = False

    @staticmethod
    def _user_override() -> str | None:
        return os.environ.get(openviking_adapter._ENV_URL, "").strip() or None

    def status(self) -> ManagerStatus:
        """Read-only snapshot — never spawns or kills anything."""
        if not openviking_adapter.enabled():
            return ManagerStatus(False, False, False, None, installer.is_installed())
        url = openviking_adapter.base_url()
        healthy = port.is_healthy(url, timeout=_HEALTH_TIMEOUT_S)
        return ManagerStatus(
            True, healthy, self._owned, url if healthy else None, installer.is_installed()
        )

    def ensure_installed(self, *, force: bool = False) -> bool:
        try:
            return installer.ensure_installed(force=force)
        except installer.InstallerError as exc:
            _log.warning("openviking installer failed: %s", exc)
            return False

    def start(self) -> ManagerStatus:
        if not openviking_adapter.enabled():
            return ManagerStatus(False, False, False, None, installer.is_installed())

        if self._disabled_for_session:
            return ManagerStatus(
                True,
                False,
                False,
                None,
                installer.is_installed(),
                error="disabled for this session after repeated restart failures",
            )

        override = self._user_override()
        if override:
            # A user pointing this cockpit at their own external OpenViking
            # instance — respected verbatim, never spawn/own anything.
            healthy = port.is_healthy(override, timeout=_HEALTH_TIMEOUT_S)
            return ManagerStatus(
                True, healthy, False, override if healthy else None, installer.is_installed()
            )

        if self._process is not None and self._process.is_alive:
            url = openviking_adapter.base_url()
            healthy = port.is_healthy(url, timeout=_HEALTH_TIMEOUT_S)
            return ManagerStatus(True, healthy, True, url if healthy else None, True)

        decision = port.pick_port()
        url = f"http://127.0.0.1:{decision.port}"

        if decision.already_healthy:
            openviking_adapter.set_runtime_url(url)
            self._owned = False
            return ManagerStatus(True, True, False, url, installer.is_installed())

        if not installer.is_installed():
            return ManagerStatus(
                True, False, False, None, False, error="OpenViking is not installed"
            )

        return self._spawn_and_poll(decision.port)

    def _spawn_and_poll(self, chosen_port: int) -> ManagerStatus:
        proc = OpenVikingProcess(installer.CONFIG_FILE, chosen_port)
        try:
            proc.start()
        except ProcessError as exc:
            _log.warning("openviking process failed to start: %s", exc)
            return ManagerStatus(True, False, False, None, True, error=str(exc))

        url = f"http://127.0.0.1:{chosen_port}"
        deadline = time.monotonic() + _START_POLL_TIMEOUT_S
        healthy = False
        while time.monotonic() < deadline:
            if not proc.is_alive:
                break
            if port.is_healthy(url, timeout=_HEALTH_TIMEOUT_S):
                healthy = True
                break
            time.sleep(_START_POLL_INTERVAL_S)

        if not healthy:
            proc.stop()
            return ManagerStatus(
                True,
                False,
                False,
                None,
                True,
                error="openviking-server did not become healthy in time",
            )

        self._process = proc
        self._owned = True
        openviking_adapter.set_runtime_url(url)
        return ManagerStatus(True, True, True, url, True)

    def stop(self) -> None:
        """Kills only a process THIS manager spawned (`owned=True`) — an
        externally-owned OpenViking (a different session's managed instance,
        or the user's own `TAKKUB_OPENVIKING_URL` override) is never
        touched (`05_PROCESS_LIFECYCLE.md`: "external process is never
        killed")."""
        if self._process is not None and self._owned:
            self._process.stop()
        self._process = None
        self._owned = False
        openviking_adapter.set_runtime_url(None)

    def restart(self) -> ManagerStatus:
        """Bounded backoff, capped retries, then disables OpenViking for the
        rest of this session (`05_PROCESS_LIFECYCLE.md` "Crash") — never
        retries forever."""
        if self._disabled_for_session:
            return self.status()
        self.stop()
        if self._restart_attempts >= _MAX_RESTART_ATTEMPTS:
            self._disabled_for_session = True
            return ManagerStatus(
                True,
                False,
                False,
                None,
                installer.is_installed(),
                error="restart attempts exhausted; OpenViking disabled for this session",
            )
        delay = _RESTART_BACKOFF_S[min(self._restart_attempts, len(_RESTART_BACKOFF_S) - 1)]
        self._restart_attempts += 1
        time.sleep(delay)
        result = self.start()
        if result.healthy:
            self._restart_attempts = 0
        return result


@dataclass(frozen=True, slots=True)
class ManagedRuntimeReport:
    """One shared read-only summary of the managed runtime for both
    `takkub ov managed status` and doctor's `[openviking]` managed-runtime
    section (Wave 3, `10_CLI.md` / `19_DIAGNOSTICS_OBSERVABILITY.md`) — the
    single place both callers build this so their output can never drift."""

    installed: bool
    version: str | None
    running: bool
    owned: bool
    address: str | None
    healthy: bool
    error: str | None = None


def managed_runtime_report(mgr: OpenVikingManager | None = None) -> ManagedRuntimeReport:
    """`running` comes from `process.is_process_alive()` (disk-based PID
    file check, survives across manager instances/cockpit sessions) rather
    than from `status().healthy` alone — a managed process that's up but
    failing its own `/health` check should report running=True,
    healthy=False, not vanish entirely. Read-only, same "never spawns or
    kills anything" contract as `OpenVikingManager.status()`."""
    mgr = mgr or get_manager()
    status = mgr.status()
    version = installer.read_state().get("version")
    return ManagedRuntimeReport(
        installed=status.installed,
        version=version,
        running=is_process_alive(),
        owned=status.owned,
        address=status.url,
        healthy=status.healthy,
        error=status.error,
    )


_instance: OpenVikingManager | None = None


def get_manager() -> OpenVikingManager:
    """One shared instance per cockpit process (module docstring) — Settings
    UI, the Setup Wizard, and boot wiring all need to observe the SAME
    `owned`/`_process` state, not three independent managers each thinking
    nothing is running."""
    global _instance
    if _instance is None:
        _instance = OpenVikingManager()
    return _instance


def boot_wiring() -> None:
    """Called once from `MainWindow._boot()` (`12_PERFORMANCE.md`: render UI
    first, never block boot, install never happens automatically on boot).
    Mirrors `remote.tunnel.reap_orphan_tunnel`'s boot-time call site (#197
    pattern): unconditionally reap a process orphaned by a previous session
    that crashed before calling `stop()`, then — only when the user has
    both enabled OpenViking AND opted into "Start automatically with
    Cockpit" (Settings UI, `08_SETTINGS_UI.md`) — start it on a background
    thread. `start()` itself already refuses to spawn anything when nothing
    is installed, so this can never trigger an install on its own."""
    try:
        reap_orphan_process()
    except Exception:
        _log.exception("openviking: boot-time orphan reap failed")

    try:
        cfg = openviking_settings.load()
    except Exception:
        _log.exception("openviking: boot-time settings load failed")
        return

    if cfg.enabled:
        # `setdefault`, never an unconditional set: an explicit
        # TAKKUB_OPENVIKING_ENABLED the user set in their own shell before
        # launching Cockpit always wins over this persisted preference
        # (same contract `openviking_adapter.base_url()` already applies to
        # TAKKUB_OPENVIKING_URL vs. `set_runtime_url`).
        os.environ.setdefault(openviking_adapter._ENV_ENABLED, "1")

    if not (cfg.enabled and cfg.start_automatically):
        return

    def _run() -> None:
        try:
            get_manager().start()
        except Exception:
            _log.exception("openviking: background boot-time start failed")

    threading.Thread(target=_run, daemon=True, name="openviking-boot-start").start()
