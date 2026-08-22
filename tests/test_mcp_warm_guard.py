"""Regression tests for #91 — pytest full suite spawning real browser-MCP
warm processes on every Orchestrator() construction (CPU idle 0%).

Root cause: Orchestrator.__init__ calls shared_dev_tools.warm_browser_mcps(),
which spawns real `npx -y @playwright/mcp@<v>` + `npx -y chrome-devtools-
mcp@<v>` in daemon threads to pre-warm the npx cache. A full pytest run
constructs dozens of Orchestrators, so without a guard the suite floods the
machine with concurrent npx/node children that outlive individual tests.

The fix is env-guarded in warm_browser_mcps() itself (not just at the
caller), plus an autouse conftest fixture that sets the env var and
monkeypatches the function directly as a second layer. These tests exercise
the *real* (unpatched) warm_browser_mcps to prove the guard itself works —
not just that conftest's monkeypatch happens to shadow it.
"""

from __future__ import annotations

import subprocess

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import shared_dev_tools as sdt
from agent_takkub.orchestrator import Orchestrator
from agent_takkub.shared_dev_tools import warm_browser_mcps as _real_warm_browser_mcps


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


class _SyncThread:
    """Stand-in for threading.Thread that runs target() inline, so the
    "guard off" test doesn't race a real background thread."""

    def __init__(self, target=None, args=(), name=None, daemon=None) -> None:
        self._target = target
        self._args = args

    def start(self) -> None:
        self._target(*self._args)


def test_warm_browser_mcps_noop_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAKKUB_SKIP_MCP_WARM", "1")
    calls: list = []
    monkeypatch.setattr(sdt.subprocess, "run", lambda *a, **k: calls.append(a))

    _real_warm_browser_mcps()

    assert calls == [], "warm_browser_mcps must no-op when TAKKUB_SKIP_MCP_WARM is set"


def test_warm_browser_mcps_spawns_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # Proves the guard (not something else) is what suppresses the spawn above.
    monkeypatch.delenv("TAKKUB_SKIP_MCP_WARM", raising=False)
    monkeypatch.setattr(sdt, "threading", type("FakeThreadingModule", (), {"Thread": _SyncThread}))
    calls: list = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(sdt.subprocess, "run", _fake_run)

    _real_warm_browser_mcps()

    assert len(calls) == 2
    # argv[0] is now the shutil.which-resolved npx launcher (e.g. .../npx.cmd on
    # Windows) — the finding fix for the bare-'npx' FileNotFoundError — so check
    # the resolved launcher, not literal-'npx' list membership.
    assert all("npx" in argv[0] for argv in calls)


def test_orchestrator_construction_spawns_no_subprocess(
    qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actual #91 regression: constructing an Orchestrator — as every
    test that imports orchestrator.py transitively does — must never spawn a
    real subprocess via shared_dev_tools. Restores the real warm_browser_mcps
    (undoing conftest's belt-and-suspenders monkeypatch) so this exercises
    the production env-guard alone, relying only on the env var conftest sets."""
    monkeypatch.setattr(sdt, "warm_browser_mcps", _real_warm_browser_mcps)
    assert sdt.os.environ.get("TAKKUB_SKIP_MCP_WARM", "").strip() not in ("", "0"), (
        "conftest.py should already have TAKKUB_SKIP_MCP_WARM set for every test"
    )
    calls: list = []

    def _fake_run(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(sdt.subprocess, "run", _fake_run)

    o = Orchestrator()
    o.shutdown_timers()

    assert calls == [], f"Orchestrator() construction spawned subprocess.run calls: {calls}"


class TestWarmFailuresAreVisible:
    """2026-08-21: a teammate's mac sat on codex's "Starting MCP servers:
    graft" run after run. The warm that exists to prevent exactly that ran
    with a 30 s cap and a bare `except Exception: pass`, so a warm killed
    mid-download looked identical to one that succeeded — from the cockpit
    there was no way to tell whether the npx cache was ever hot."""

    def test_warm_cap_outlasts_a_cold_tarball_download(self) -> None:
        # The cap has to survive a first-run download+extract, not just the
        # server's own startup — 30 s did not.
        assert sdt._MCP_WARM_TIMEOUT_S >= 120

    def test_timeout_is_logged_not_swallowed(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def _boom(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))

        monkeypatch.setattr(sdt.subprocess, "run", _boom)
        with caplog.at_level("WARNING", logger=sdt._log.name):
            sdt._warm_mcp_process("graft", ["npx", "-y", "pkg", "mcp"])
        assert any("graft" in r.getMessage() for r in caplog.records)

    def test_unexpected_error_is_logged_not_swallowed(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(
            sdt.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
        )
        with caplog.at_level("WARNING", logger=sdt._log.name):
            sdt._warm_mcp_process("graft", ["npx", "-y", "pkg", "mcp"])
        assert caplog.records, "a failed warm must leave a trace"

    def test_a_failed_warm_never_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Boot calls this; a network blip must not take the cockpit with it.
        monkeypatch.setattr(
            sdt.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        sdt._warm_mcp_process("graft", ["npx"])  # must not raise

    def test_graft_warm_uses_the_shared_helper_with_the_real_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TAKKUB_SKIP_MCP_WARM", raising=False)
        monkeypatch.setattr(
            sdt, "threading", type("FakeThreadingModule", (), {"Thread": _SyncThread})
        )
        seen: list[dict] = []

        def _fake_run(argv, **kwargs):
            seen.append({"argv": argv, **kwargs})
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(sdt.subprocess, "run", _fake_run)
        monkeypatch.setattr(sdt.shutil, "which", lambda name: "/usr/bin/npx")

        sdt.warm_graft_mcp()

        assert len(seen) == 1
        assert seen[0]["timeout"] == sdt._MCP_WARM_TIMEOUT_S
        assert "mcp" in seen[0]["argv"]
