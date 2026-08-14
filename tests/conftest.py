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
from pathlib import Path

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
# Same rationale as TAKKUB_SKIP_MCP_WARM above, for graft_autobuild.py:
# Orchestrator() construction and done() both trigger real `graft build`
# subprocesses otherwise — a full pytest run would spawn dozens of node
# processes across every projects.json path.
os.environ.setdefault("TAKKUB_SKIP_GRAFT_BUILD", "1")
# Same rationale again, for disk_usage.py's prune_orphan_worktrees_boot():
# Orchestrator() construction runs it at boot, and it shells out to real
# `git rev-parse` / `git worktree list` (via WorktreeManager) for every dir
# under <data_home>/worktrees/** — including any leftover real checkout on
# the dev machine running the suite, not just test fixtures.
os.environ.setdefault("TAKKUB_SKIP_ORPHAN_WORKTREE_PRUNE", "1")
# Browser-role spawn tests mock the PTY and must never launch a real Chrome.
# NativeChromeManager itself is tested directly with subprocess/CDP mocks.
os.environ.setdefault("TAKKUB_SKIP_NATIVE_CHROME", "1")
# app.py installs sys.excepthook at import time, so any pytest process that
# imports it (directly or transitively) routes its own unhandled exceptions
# into auto_issue_capture.capture_cockpit_crash — which files a real GitHub
# issue against the public repo (#188). auto_issue_capture._auto_issue_suppressed
# already checks PYTEST_CURRENT_TEST/`pytest in sys.modules` as a fallback, but
# this explicit flag documents the guard alongside its siblings and gives a
# single toggle a test can clear on purpose.
os.environ.setdefault("TAKKUB_SKIP_AUTO_ISSUE_CAPTURE", "1")

import pytest


def _assert_agent_takkub_matches_this_checkout() -> None:
    """#202: a shared dev-venv editable install (`__editable__.agent_takkub-
    *.pth`) can be repointed at a DIFFERENT checkout — most dangerously a
    `--isolation worktree` that a `pip install -e .` run from inside it left
    behind, which then goes stale (or silently redirects every OTHER pane's
    imports) without anyone noticing. Catch that BEFORE a single test runs
    against the wrong code, instead of after a confusing failure.

    No-op when `agent_takkub` isn't importable at all in this process — the
    `installed-gate` CI job deliberately runs `test_installed_mode_gate.py`
    without it (assertions there run via subprocess against a throwaway
    venv; see .github/workflows/ci.yml), and that absence is not this
    guard's concern.
    """
    try:
        import agent_takkub
    except Exception:
        return
    expected = (Path(__file__).resolve().parent.parent / "src" / "agent_takkub").resolve()
    actual = Path(agent_takkub.__file__).resolve().parent
    if actual != expected:
        raise RuntimeError(
            f"agent_takkub imported from {actual}, expected {expected} (this repo's own "
            "src/) — the shared venv's editable-install .pth points at a different "
            "checkout (#202). Fix: `pip install -e . --no-deps` from THIS repo's root "
            "(never from inside a worktree — see pane_guard.py's pip_editable rule), or "
            "run this suite with PYTHONPATH=<this-repo>/src prepended to override the "
            "shared install without touching it."
        )


_assert_agent_takkub_matches_this_checkout()

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
    "agent_takkub.limit_autoresume",  # #158 progress-marker dump, copies RUNTIME_DIR at import time
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
    # Most orchestration unit tests intentionally assert the legacy
    # synchronous Lead-notice path without running a Qt event loop. Keep those
    # tests focused by disabling the production 60 s inbox window per test;
    # digest-specific tests explicitly remove/override this value.
    monkeypatch.setenv("TAKKUB_INBOX_DIGEST_MS", "0")

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

    # role_memory computes ROLE_MEMORY_DIR = RUNTIME_DIR / "role-memory" once at
    # import time (not a lazy join), so patching its copied RUNTIME_DIR name
    # above doesn't touch it. Without this, any test that exercises the
    # spawn-time role-memory injection (e.g. test_spawn_task_delivery.py)
    # writes real files into this checkout's actual runtime/role-memory/
    # (confirmed: proj_a/, proj/, spawn-task-test/ leaked there from prior runs).
    rmem = _maybe_module("agent_takkub.role_memory", force=False)
    if rmem is not None:
        monkeypatch.setattr(rmem, "ROLE_MEMORY_DIR", runtime / "role-memory", raising=False)

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

    # #196: AuthGate.__init__ now reads/writes `session_store.py`'s on-disk
    # password-session store unconditionally (not opt-in like the P0 remote
    # scaffold — any test that builds a real RemoteHttpServer/AuthGate, not
    # just test_remote_auth.py, goes through it). Unpatched, that's a read
    # and (on `issue_password_session`) a write to the real
    # `~/.takkub/takkub-remote-sessions.json` on the machine running pytest.
    ss = _maybe_module("agent_takkub.remote.session_store", force=True)
    if ss is not None:
        monkeypatch.setattr(ss, "_PATH", runtime / "takkub-remote-sessions.json", raising=False)

    # Second layer for #91 (see the module-level os.environ.setdefault above):
    # force the env guard back on per-test (in case a prior test cleared it)
    # AND monkeypatch warm_browser_mcps to a no-op directly, so a stray import
    # path that dodges the env check still can't spawn real npx/node children
    # during the suite.
    monkeypatch.setenv("TAKKUB_SKIP_MCP_WARM", "1")
    monkeypatch.setenv("TAKKUB_SKIP_NATIVE_CHROME", "1")
    monkeypatch.setenv("TAKKUB_SKIP_GRAFT_BUILD", "1")
    monkeypatch.setenv("TAKKUB_SKIP_ORPHAN_WORKTREE_PRUNE", "1")
    monkeypatch.setenv("TAKKUB_SKIP_AUTO_ISSUE_CAPTURE", "1")
    sdt = _maybe_module("agent_takkub.shared_dev_tools", force=True)
    if sdt is not None:
        monkeypatch.setattr(sdt, "warm_browser_mcps", lambda: None, raising=False)
    gab = _maybe_module("agent_takkub.graft_autobuild", force=True)
    if gab is not None:
        monkeypatch.setattr(gab, "build_all_projects_async", lambda: None, raising=False)
        # graft_autobuild's single-flight/failure-tracking state (_building,
        # _last_build_failed, _last_live_resync, _pending_escalation,
        # _unresolvable_rels) lives in module-level mutable containers, not
        # per-call locals — a test that records a failure (test_graft_
        # autobuild.py) leaves it there for every test that runs after it in
        # the same process, including unrelated ones in test_graft_chip.py
        # that assert `get_build_status()["failed"] == []` (proven: passes
        # in isolation, fails in the full suite). Reset by mutating in place
        # (not reassigning) since other modules may hold a reference to the
        # same dict/set object.
        for timer in list(gab._debounce_timers.values()):
            timer.cancel()
        gab._building.clear()
        gab._in_flight.clear()
        gab._debounce_timers.clear()
        gab._last_build_failed.clear()
        gab._last_live_resync.clear()
        gab._pending_escalation.clear()
        gab._unresolvable_rels.clear()

    # graft_store.GRAFT_STORE_ROOT falls back to Path.home()/".agent-takkub"
    # exactly when DATA_HOME == REPO_ROOT (M5, graft_store.py's module
    # docstring) — which is always true for THIS repo's own dev checkout
    # running the suite (config.py's `_resolve_data_home`). An unpatched
    # test calling `_run_build`/`graph_store_dir` for real would therefore
    # write into the real `~/.agent-takkub` on the machine running pytest,
    # not the actual agent-takkub working tree (that self-nesting case is
    # exactly what #146/M5 avoid) — still not what a test run should touch.
    # Redirect to the isolated runtime dir either way.
    gst = _maybe_module("agent_takkub.graft_store", force=True)
    if gst is not None:
        monkeypatch.setattr(gst, "GRAFT_STORE_ROOT", runtime / "graft-graphs", raising=False)
        # Same reasoning as GRAFT_STORE_ROOT above, for its persistent staging
        # sibling (H1 follow-up, 2026-08-06) — an unpatched test calling
        # `_run_build`/`staging_dir_for` for real would otherwise write into
        # the real `~/.agent-takkub/graft-staging`.
        monkeypatch.setattr(gst, "GRAFT_STAGING_ROOT", runtime / "graft-staging", raising=False)

    # `mcp_bridge._codex_cli_version_cached`'s cache (2026-08-06, cheap-spawn
    # follow-up) is keyed by `(provider_bin, mtime-of-resolved-binary)` — on
    # a dev machine with a real `codex` on PATH, that key is IDENTICAL across
    # every test in the suite, so without a per-test reset the first test to
    # populate it would silently poison every later test that monkeypatches
    # `_codex_cli_version` expecting its own return value to take effect.
    mcpb = _maybe_module("agent_takkub.mcp_bridge", force=True)
    if mcpb is not None:
        monkeypatch.setattr(mcpb, "_version_cache", {}, raising=False)

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
