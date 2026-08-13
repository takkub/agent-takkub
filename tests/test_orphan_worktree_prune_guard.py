"""Regression tests for the boot-sweep subprocess guard on
disk_usage.prune_orphan_worktrees_boot().

Root cause: Orchestrator.__init__ calls prune_orphan_worktrees_boot() at
boot (via disk_usage.py) to sweep orphan worktree checkouts. That sweep's
classify_worktree() -> WorktreeManager.git_root() shells out to a real `git
-C <dir> rev-parse --show-toplevel` subprocess for every dir it finds under
DATA_HOME/worktrees/*/*. #91's TAKKUB_SKIP_GRAFT_BUILD / TAKKUB_SKIP_MCP_WARM
guards did not cover this third boot-time subprocess source, so a pytest run
on a dev machine with any leftover worktree checkout on disk spawned real
git processes on every Orchestrator() construction.

The fix is env-guarded in prune_orphan_worktrees_boot() itself (not just at
the orchestrator.py call site), plus conftest.py setting the env var for
every test. These tests exercise the *real* (unpatched) function to prove
the guard itself works — not that it merely happens to find nothing to
scan.
"""

from __future__ import annotations

import subprocess

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import disk_usage
from agent_takkub import worktree_manager as wtm
from agent_takkub.orchestrator import Orchestrator


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _make_orphan_worktree(tmp_path) -> None:
    """A dir under <tmp_path>/worktrees/*/* with a `.git` pointer file —
    exactly what find_worktree_dirs()/classify_worktree() scan for. Left
    otherwise empty so, absent the guard, classify_worktree() reaches
    WorktreeManager.git_root() (the real subprocess call) instead of
    returning early on a missing `.git` file."""
    wt = tmp_path / "worktrees" / "proj" / "backend-guard-repro"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /repo/.git/worktrees/backend-guard-repro\n")


def test_prune_orphan_worktrees_boot_noop_when_env_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("TAKKUB_SKIP_ORPHAN_WORKTREE_PRUNE", "1")
    _make_orphan_worktree(tmp_path)
    calls: list = []
    monkeypatch.setattr(wtm.subprocess, "run", lambda *a, **k: calls.append(a))

    removed = disk_usage.prune_orphan_worktrees_boot(data_home=tmp_path)

    assert removed == 0
    assert calls == [], (
        "prune_orphan_worktrees_boot must no-op when TAKKUB_SKIP_ORPHAN_WORKTREE_PRUNE is set"
    )


def test_prune_orphan_worktrees_boot_scans_when_env_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # Proves the guard (not "nothing to scan") is what suppresses the spawn
    # in the test above — same fixture dir actually reaches subprocess.run
    # once the guard is off.
    monkeypatch.delenv("TAKKUB_SKIP_ORPHAN_WORKTREE_PRUNE", raising=False)
    _make_orphan_worktree(tmp_path)
    calls: list = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(wtm.subprocess, "run", _fake_run)

    disk_usage.prune_orphan_worktrees_boot(data_home=tmp_path)

    assert calls, "expected a real git subprocess call once the guard is off"
    assert any("rev-parse" in argv for argv in calls)


def test_orchestrator_construction_spawns_no_worktree_subprocess(
    qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The actual regression: constructing an Orchestrator — as every test
    that imports orchestrator.py transitively does — must never spawn a real
    git subprocess via the boot-time orphan-worktree sweep, relying only on
    the production env-guard (conftest.py's env var, not a function-level
    monkeypatch stub — disk_usage.prune_orphan_worktrees_boot has none)."""
    assert disk_usage.os.environ.get("TAKKUB_SKIP_ORPHAN_WORKTREE_PRUNE", "").strip() not in (
        "",
        "0",
    ), "conftest.py should already have TAKKUB_SKIP_ORPHAN_WORKTREE_PRUNE set for every test"
    # DATA_HOME normally resolves to this repo's own checkout (config.py's
    # _resolve_data_home, M5) — point it at a tmp dir holding a fabricated
    # orphan worktree so, absent the guard, the boot sweep would actually
    # have something to shell out to git about.
    monkeypatch.setattr(disk_usage, "DATA_HOME", tmp_path)
    _make_orphan_worktree(tmp_path)
    calls: list = []

    def _fake_run(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(wtm.subprocess, "run", _fake_run)

    o = Orchestrator()
    o._idle_watchdog.stop()
    o._hot_md_timer.stop()

    assert calls == [], (
        f"Orchestrator() construction spawned worktree subprocess.run calls: {calls}"
    )
