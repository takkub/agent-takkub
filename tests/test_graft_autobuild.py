"""graft_autobuild.py — auto-run `graft build` so the graft MCP has a graph
without the user running the CLI by hand.

Covers: kill switch, missing-CLI skip, per-project path dedup (identical dirs
collapse, nested dirs do NOT — each is a separate pane cwd), single-flight
per directory, and debounce coalescing for the post-done trigger.
"""

from __future__ import annotations

import json
import subprocess
import threading

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import config
from agent_takkub import graft_autobuild as gab
from agent_takkub.graft_autobuild import (
    build_all_projects_async as _real_build_all_projects_async,
)

# Imported at module level (before any QCoreApplication is constructed) so
# terminal_widget's QtWebEngineWidgets import happens in the right order —
# same reason test_mcp_warm_guard.py imports Orchestrator at module scope.
from agent_takkub.orchestrator import Orchestrator


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


# conftest.py's autouse fixture monkeypatches gab.build_all_projects_async to a
# no-op (belt-and-suspenders for the boot-trigger path, mirroring
# shared_dev_tools.warm_browser_mcps). Tests that exercise the real function
# call this name — imported here at module load time, before any per-test
# monkeypatch rebinds the module attribute — same pattern as
# test_mcp_warm_guard.py's _real_warm_browser_mcps.


@pytest.fixture(autouse=True)
def _isolated_projects(tmp_path, monkeypatch):
    pj = tmp_path / "projects.json"
    monkeypatch.setattr(config, "PROJECTS_JSON", pj)
    return pj


def _write_projects(pj, projects: dict) -> None:
    pj.write_text(json.dumps({"active": None, "projects": projects}), encoding="utf-8")


class _SyncThread:
    """Runs target() inline instead of on a real thread, so tests are
    deterministic and never race a background build."""

    def __init__(self, target=None, args=(), name=None, daemon=None) -> None:
        self._target = target
        self._args = args

    def start(self) -> None:
        self._target(*self._args)


# ── kill switch / missing CLI ────────────────────────────────────────────


def test_build_all_projects_noop_when_env_set(tmp_path, monkeypatch):
    monkeypatch.setenv("TAKKUB_SKIP_GRAFT_BUILD", "1")
    monkeypatch.setattr(gab, "_graft_cli", lambda: r"C:\fake\graft.cmd")
    calls = []
    monkeypatch.setattr(gab, "_spawn_build", lambda d: calls.append(d))

    _real_build_all_projects_async()

    assert calls == []


def test_build_all_projects_noop_when_graft_cli_missing(tmp_path, monkeypatch, _isolated_projects):
    monkeypatch.delenv("TAKKUB_SKIP_GRAFT_BUILD", raising=False)
    monkeypatch.setattr(gab, "_graft_cli", lambda: None)
    _write_projects(_isolated_projects, {"p": {"paths": {"main": str(tmp_path)}}})
    calls = []
    monkeypatch.setattr(gab, "_spawn_build", lambda d: calls.append(d))

    _real_build_all_projects_async()

    assert calls == []


def test_schedule_rebuild_noop_without_cwd(monkeypatch):
    monkeypatch.delenv("TAKKUB_SKIP_GRAFT_BUILD", raising=False)
    monkeypatch.setattr(gab, "_graft_cli", lambda: r"C:\fake\graft.cmd")
    fired = []
    monkeypatch.setattr(gab.threading, "Timer", lambda *a, **k: fired.append(a) or None)

    gab.schedule_rebuild_after_done(None)

    assert fired == []


# ── path resolution ──────────────────────────────────────────────────────


def test_dirs_for_project_dedupes_identical_paths(tmp_path):
    d = tmp_path / "root"
    d.mkdir()
    project = {"paths": {"a": str(d), "b": str(d)}}

    dirs = gab._dirs_for_project(project)

    assert dirs == [d.resolve()]


def test_dirs_for_project_keeps_nested_paths_distinct(tmp_path):
    """A pane's graft MCP resolves the graph from ITS OWN cwd (no dir arg),
    so a path nested inside another must still get its own build — building
    only the parent would leave the nested pane's cwd graph-less."""
    parent = tmp_path / "root"
    child = parent / "api"
    child.mkdir(parents=True)
    project = {"paths": {"root": str(parent), "api": str(child)}}

    dirs = {str(p) for p in gab._dirs_for_project(project)}

    assert dirs == {str(parent.resolve()), str(child.resolve())}


def test_dirs_for_project_skips_missing_dirs(tmp_path):
    project = {"paths": {"gone": str(tmp_path / "does-not-exist")}}

    assert gab._dirs_for_project(project) == []


def test_build_all_projects_covers_every_project_path(tmp_path, monkeypatch, _isolated_projects):
    monkeypatch.delenv("TAKKUB_SKIP_GRAFT_BUILD", raising=False)
    monkeypatch.setattr(gab, "_graft_cli", lambda: r"C:\fake\graft.cmd")
    web = tmp_path / "web"
    api = tmp_path / "api"
    web.mkdir()
    api.mkdir()
    _write_projects(
        _isolated_projects,
        {"proj": {"paths": {"web": str(web), "api": str(api)}}},
    )
    calls = []
    monkeypatch.setattr(gab, "_spawn_build", lambda d: calls.append(d))

    _real_build_all_projects_async()

    assert {str(c) for c in calls} == {str(web.resolve()), str(api.resolve())}


# ── tab-switch trigger: only missing graphs ─────────────────────────────


def test_ensure_project_graph_skips_dir_with_existing_graph(
    tmp_path, monkeypatch, _isolated_projects
):
    """The graph now lives in the EXTERNAL store (`graph_store_dir`), not
    `<target>/graft` — the existing-graph check must look there, never at
    a `graft/` folder inside the target itself (#146 follow-up)."""
    monkeypatch.delenv("TAKKUB_SKIP_GRAFT_BUILD", raising=False)
    monkeypatch.setattr(gab, "_graft_cli", lambda: r"C:\fake\graft.cmd")
    has_graph = tmp_path / "has-graph"
    has_graph.mkdir()
    (gab.graph_store_dir(has_graph) / ".graph").mkdir(parents=True)
    no_graph = tmp_path / "no-graph"
    no_graph.mkdir()
    _write_projects(
        _isolated_projects,
        {"proj": {"paths": {"a": str(has_graph), "b": str(no_graph)}}},
    )
    calls = []
    monkeypatch.setattr(gab, "_spawn_build", lambda d: calls.append(d))

    gab.ensure_project_graph_async("proj")

    assert {str(c) for c in calls} == {str(no_graph.resolve())}
    # No `graft/` (or anything else) written inside the target dirs themselves.
    assert list(has_graph.iterdir()) == []
    assert list(no_graph.iterdir()) == []


def test_ensure_project_graph_unknown_project_is_noop(monkeypatch, _isolated_projects):
    monkeypatch.delenv("TAKKUB_SKIP_GRAFT_BUILD", raising=False)
    monkeypatch.setattr(gab, "_graft_cli", lambda: r"C:\fake\graft.cmd")
    _write_projects(_isolated_projects, {})
    calls = []
    monkeypatch.setattr(gab, "_spawn_build", lambda d: calls.append(d))

    gab.ensure_project_graph_async("does-not-exist")

    assert calls == []


# ── single-flight ─────────────────────────────────────────────────────────


def test_build_one_is_single_flight_for_same_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(gab, "_graft_cli", lambda: r"C:\fake\graft.cmd")
    entered = threading.Event()
    release = threading.Event()
    run_count = []

    def _fake_run_build(graft_bin, target):
        run_count.append(target)
        entered.set()
        release.wait(timeout=5)
        return True, 0.01, ""

    monkeypatch.setattr(gab, "_run_build", _fake_run_build)

    t1 = threading.Thread(target=gab._build_one, args=(tmp_path,))
    t1.start()
    assert entered.wait(timeout=5), "first build never started"

    # Second call for the SAME dir while the first is still running must be
    # a no-op (not a second subprocess) — proves the single-flight guard.
    gab._build_one(tmp_path)

    release.set()
    t1.join(timeout=5)

    assert len(run_count) == 1
    assert str(tmp_path) not in gab._building  # cleaned up after completion


def test_build_one_noop_when_graft_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(gab, "_graft_cli", lambda: None)
    calls = []
    monkeypatch.setattr(gab, "_run_build", lambda *a: calls.append(a))

    gab._build_one(tmp_path)

    assert calls == []


def test_run_build_invokes_graft_build_no_deep_no_init(tmp_path, monkeypatch):
    """Structural layer only: the actual subprocess argv must be exactly
    `<graft> --dir <store> build <dir>` — no --deep, no init. The graph
    store is external to *target* (#146 follow-up) so this also asserts the
    store dir sits OUTSIDE the target and nothing lands inside target.

    *target* is a dedicated subdir, deliberately NOT `tmp_path` itself —
    conftest's isolated runtime (where `graph_store_dir` resolves under in
    tests) also lives under `tmp_path`, so using `tmp_path` as the target
    would make the store a descendant of the target by test-harness
    coincidence even though production DATA_HOME and a project path are
    always unrelated directories.
    """
    target = tmp_path / "target-repo"
    target.mkdir()
    captured = {}

    def _fake_subprocess_run(argv, **kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(gab.subprocess, "run", _fake_subprocess_run)

    ok, _elapsed, _err = gab._run_build("graft.cmd", target)

    assert ok is True
    expected_store = gab.graph_store_dir(target)
    assert captured["argv"] == ["graft.cmd", "--dir", str(expected_store), "build", str(target)]
    assert str(expected_store) != str(target)
    assert not str(expected_store).startswith(str(target))
    assert list(target.iterdir()) == []  # nothing written into the target dir


def test_run_build_writes_store_manifest_on_success(tmp_path, monkeypatch):
    monkeypatch.setattr(
        gab.subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(argv, 0)
    )

    gab._run_build("graft.cmd", tmp_path)

    store = gab.graph_store_dir(tmp_path)
    manifest = store / "source.json"
    assert manifest.is_file()
    assert json.loads(manifest.read_text(encoding="utf-8"))["source"] == str(tmp_path.resolve())


def test_run_build_no_manifest_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        gab.subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(argv, 1)
    )

    ok, _elapsed, _err = gab._run_build("graft.cmd", tmp_path)

    assert ok is False
    store = gab.graph_store_dir(tmp_path)
    assert not (store / "source.json").is_file()


# ── debounce ────────────────────────────────────────────────────────────


def test_schedule_rebuild_debounces_bursts_into_one_timer(tmp_path, monkeypatch):
    monkeypatch.delenv("TAKKUB_SKIP_GRAFT_BUILD", raising=False)
    monkeypatch.setattr(gab, "_graft_cli", lambda: r"C:\fake\graft.cmd")
    cancelled = []
    started = []

    class _FakeTimer:
        def __init__(self, interval, fn, args=()):
            self.cancel_called = False

        def cancel(self):
            cancelled.append(True)
            self.cancel_called = True

        def start(self):
            started.append(True)

    monkeypatch.setattr(gab.threading, "Timer", _FakeTimer)

    gab.schedule_rebuild_after_done(str(tmp_path))
    gab.schedule_rebuild_after_done(str(tmp_path))
    gab.schedule_rebuild_after_done(str(tmp_path))

    # 3 calls for the SAME dir → each new call cancels the previous pending
    # timer (only 2 cancels: the 1st timer has nothing to cancel).
    assert len(cancelled) == 2
    assert len(started) == 3
    assert str(tmp_path.resolve()) in gab._debounce_timers


def test_debounced_fire_pops_timer_and_builds(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(gab, "_build_one", lambda d: calls.append(d))
    key = str(tmp_path)
    gab._debounce_timers[key] = object()

    gab._debounced_fire(key, tmp_path)

    assert calls == [tmp_path]
    assert key not in gab._debounce_timers


# ── Orchestrator construction never spawns a real graft subprocess ───────


def test_orchestrator_construction_spawns_no_graft_subprocess(qapp, monkeypatch):
    """Mirrors test_mcp_warm_guard.py's #91 regression test: constructing an
    Orchestrator — as every test importing orchestrator.py transitively does
    — must never spawn a real `graft build` subprocess. Restores the real
    build_all_projects_async (undoing conftest's belt-and-suspenders
    monkeypatch) so this exercises the production env-guard alone."""
    monkeypatch.setattr(gab, "build_all_projects_async", _real_build_all_projects_async)
    assert gab.os.environ.get("TAKKUB_SKIP_GRAFT_BUILD", "").strip() not in ("", "0"), (
        "conftest.py should already have TAKKUB_SKIP_GRAFT_BUILD set for every test"
    )
    calls: list = []
    monkeypatch.setattr(gab.subprocess, "run", lambda *a, **k: calls.append(a))

    o = Orchestrator()
    o._idle_watchdog.stop()
    o._hot_md_timer.stop()

    assert calls == [], f"Orchestrator() construction spawned graft subprocess.run calls: {calls}"
