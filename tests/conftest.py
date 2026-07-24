"""Global test isolation.

The orchestrator writes its audit log (`_log_event` → events.log) and various
session/brief/task files under `RUNTIME_DIR`. Those paths are module-level
constants imported *by value* from `agent_takkub.config`, so a test that only
monkeypatches `orchestrator.RUNTIME_DIR` still lets `_log_event` (and
`ensure_runtime`) write to the REAL `runtime/events.log`. That pollution is
how the live log once bloated to 10 MB and wedged the cockpit (see
docs/cockpit-freeze-rca-2026-05-29.md).

This autouse fixture redirects EVENTS_LOG and RUNTIME_DIR — in every module
that bound them — to a per-test tmp dir, so no test can touch the real runtime.
"""

from __future__ import annotations

import importlib
import os
import sys

# Must be set before any QApplication/QCoreApplication is constructed.
# Individual test modules import PyQt6 at module level, but Qt reads this
# env var at application-creation time — so setting it here (conftest loads
# first) is sufficient.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Declare the test process as a multi-instance run so it can NEVER take the
# single-instance lock path in app.main(), whose auto-kill os.kill()s the PID
# holding the cockpit lock (app.py `[single-instance] killing old process`).
# Without this, running the suite while a real cockpit is open risks the test
# process — or any code under test that reaches that path — terminating the
# user's live dev instance. setdefault so an explicit outer value still wins.
# Per-test env assertions can monkeypatch/delenv to override.
os.environ.setdefault("TAKKUB_ALLOW_MULTI", "1")

# Every Orchestrator() construction calls shared_dev_tools.warm_browser_mcps(),
# which spawns real `npx @playwright/mcp` + `npx chrome-devtools-mcp` processes
# in daemon threads to pre-warm the npx cache. A full pytest run constructs
# dozens of Orchestrators, so without this guard the suite floods the machine
# with concurrent npx/node children that outlive individual tests (#91 — CPU
# idle 0%, 50-74 concurrent procs observed). Set before any test module (or
# agent_takkub.shared_dev_tools) imports, so the very first Orchestrator() in
# the suite is already covered. The autouse fixture below adds a second,
# belt-and-suspenders layer (monkeypatch) in case a test explicitly clears
# this env var.
os.environ.setdefault("TAKKUB_SKIP_MCP_WARM", "1")

import pytest

# Modules that bind RUNTIME_DIR / EVENTS_LOG as a module-level name. config is
# the source of truth (and what ensure_runtime uses); the rest copy the value
# at import time, so each needs its own patch. main_window is patched only if
# already imported — we never force-import the GUI window in unit tests.
_RUNTIME_DIR_MODULES = (
    "agent_takkub.config",
    "agent_takkub.orchestrator",
    "agent_takkub.orchestrator_text",  # pure-helper leaf, copies RUNTIME_DIR at import time
    "agent_takkub.agent_pane",
    "agent_takkub.lead_bash_audit",
    "agent_takkub.shared_dev_tools",
)
_EVENTS_LOG_MODULES = (
    "agent_takkub.config",
    "agent_takkub.orchestrator",
    "agent_takkub.orchestrator_text",  # _log_event reads EVENTS_LOG from this module
)
# Patched when present but never force-imported (heavy GUI deps).
_OPTIONAL_MODULES = ("agent_takkub.main_window",)


def _maybe_module(name: str, *, force: bool):
    mod = sys.modules.get(name)
    if mod is None and force:
        try:
            mod = importlib.import_module(name)
        except Exception:
            return None
    return mod


@pytest.fixture(autouse=True)
def _isolate_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path):
    # Distinct name (not "runtime") so we don't collide with test-local fixtures
    # that do `(tmp_path / "runtime").mkdir()` without exist_ok. Tests that set
    # their own RUNTIME_DIR re-patch over ours (autouse runs first); this is
    # just the safety net for everything else.
    runtime = tmp_path / "_isolated_runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    events = runtime / "events.log"

    targets = [(n, "RUNTIME_DIR", runtime) for n in _RUNTIME_DIR_MODULES]
    targets += [(n, "EVENTS_LOG", events) for n in _EVENTS_LOG_MODULES]
    for name in _OPTIONAL_MODULES:
        targets.append((name, "RUNTIME_DIR", runtime))
        targets.append((name, "EVENTS_LOG", events))

    for name, attr, value in targets:
        force = name in ("agent_takkub.config", "agent_takkub.orchestrator")
        mod = _maybe_module(name, force=force)
        if mod is not None and hasattr(mod, attr):
            monkeypatch.setattr(mod, attr, value, raising=False)

    # Neutralise TAKKUB_PORT_FILE + isolate PORT_FILE. Importing agent_takkub.app
    # runs a module-level `os.environ.setdefault("TAKKUB_PORT_FILE", <tmp>/agent-
    # takkub-port.<pid>)` whenever TAKKUB_ALLOW_MULTI is set (which it always is
    # here — see above). That write is process-wide, not via monkeypatch, so once
    # ANY test imports app the override leaks into every later test: read_port()
    # then resolves to that stale per-PID file instead of the per-test PORT_FILE,
    # and test_config's corrupt-file test reads a value another test wrote there.
    # delenv per test (autouse runs first; tests that need the override re-set it
    # themselves) + redirect PORT_FILE to the isolated runtime so no test can
    # read or write the real runtime/port.
    monkeypatch.delenv("TAKKUB_PORT_FILE", raising=False)
    monkeypatch.delenv("_TAKKUB_AUTO_PORT_FILE", raising=False)
    cfg = _maybe_module("agent_takkub.config", force=True)
    if cfg is not None and hasattr(cfg, "PORT_FILE"):
        monkeypatch.setattr(cfg, "PORT_FILE", runtime / "port", raising=False)

    # Redirect per-role provider config off the real ~/.takkub. effective_provider_for
    # is now on the spawn/assign stagger path (codex-gap detection, #38), so any
    # assign/spawn/run_pipeline test would otherwise read — and auto-create — the
    # user's real role-providers.json. provider_config is stdlib-only, so we can
    # force-import it to patch the paths before the test runs. Tests with their own
    # provider-config fixture re-patch over this (autouse runs first).
    pc = _maybe_module("agent_takkub.provider_config", force=True)
    if pc is not None:
        takkub_dir = tmp_path / "_isolated_takkub"
        monkeypatch.setattr(pc, "_BASE_DIR", takkub_dir, raising=False)
        monkeypatch.setattr(pc, "_CONFIG_PATH", takkub_dir / "role-providers.json", raising=False)

    # Second layer for #91 (see the module-level os.environ.setdefault above):
    # force the env guard back on per-test (in case a prior test cleared it)
    # AND monkeypatch warm_browser_mcps to a no-op directly, so a stray import
    # path that dodges the env check still can't spawn real npx/node children
    # during the suite.
    monkeypatch.setenv("TAKKUB_SKIP_MCP_WARM", "1")
    sdt = _maybe_module("agent_takkub.shared_dev_tools", force=True)
    if sdt is not None:
        monkeypatch.setattr(sdt, "warm_browser_mcps", lambda: None, raising=False)

    yield


@pytest.fixture(scope="session", autouse=True)
def _qt_session_app():
    """Keep a single QApplication alive for the entire test session.

    Qt forbids creating a second application object in the same process after
    the first has been destroyed.  Without this fixture, module-scoped ``qapp``
    fixtures in individual test files (test_auto_chain, test_cli_server, …)
    each create a QCoreApplication and drop it at module teardown, leaving the
    C++ singleton dead.  When test_config_wizard.py then tries to construct a
    QApplication, Qt aborts the process (exit 127 in the full suite, but passes
    when run in isolation because no prior module has polluted the singleton).

    This fixture creates one QApplication before any test module runs and holds
    the Python reference for the entire session, so the C++ singleton is never
    destroyed between modules.  Module-scoped ``qapp`` fixtures in test files
    call ``QCoreApplication.instance()`` / ``QApplication.instance()`` first
    and reuse this instance — no second construction ever happens.

    PyQt6 is OPTIONAL here: the CI ``installed-gate`` job runs only
    tests/test_installed_mode_gate.py in a minimal env (pytest + build, no
    PyQt6 — every assertion executes inside a throwaway venv via subprocess),
    but pytest still imports this conftest at collection. An unconditional
    import made the whole gate error out before running a single test
    (2026-07-05, run 28729942340 — both OSes). Qt-dependent tests still fail
    loudly if PyQt6 is genuinely missing in the full-suite env: they import
    Qt themselves at module level.
    """
    try:
        from PyQt6.QtWidgets import QApplication
    except ModuleNotFoundError:
        yield None
        return

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
    # Do NOT call app.quit() here — session-scoped fixture teardown may race
    # with other fixtures still running.  Let the process exit handle cleanup.
