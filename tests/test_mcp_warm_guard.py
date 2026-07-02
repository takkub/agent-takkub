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
    assert all("npx" in argv for argv in calls)


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
    o._idle_watchdog.stop()
    o._hot_md_timer.stop()

    assert calls == [], f"Orchestrator() construction spawned subprocess.run calls: {calls}"
