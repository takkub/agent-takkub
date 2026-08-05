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
from pathlib import Path

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
    a `graft/` folder inside the target itself (#146 follow-up). And it must
    check the H2 completion MARKER, not bare `.graph` existence — a `.graph`
    dir alone doesn't prove the build that made it finished (partial builds
    from a timeout/MAX_PATH failure leave one behind too)."""
    monkeypatch.delenv("TAKKUB_SKIP_GRAFT_BUILD", raising=False)
    monkeypatch.setattr(gab, "_graft_cli", lambda: r"C:\fake\graft.cmd")
    has_graph = tmp_path / "has-graph"
    has_graph.mkdir()
    store = gab.graph_store_dir(has_graph)
    (store / ".graph").mkdir(parents=True)
    gab.mark_build_complete(store)
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


def test_ensure_project_graph_rebuilds_partial_graph_without_marker(
    tmp_path, monkeypatch, _isolated_projects
):
    """A `.graph` dir with no completion marker is what a killed/interrupted
    build leaves behind — must be treated as needing a rebuild, not as done
    (H2). Proves the fix actually changes behavior vs. the old bare-`.graph`
    check: without this fix this test would see `calls == []` instead."""
    monkeypatch.delenv("TAKKUB_SKIP_GRAFT_BUILD", raising=False)
    monkeypatch.setattr(gab, "_graft_cli", lambda: r"C:\fake\graft.cmd")
    partial = tmp_path / "partial-graph"
    partial.mkdir()
    (gab.graph_store_dir(partial) / ".graph").mkdir(parents=True)  # no marker written
    _write_projects(_isolated_projects, {"proj": {"paths": {"a": str(partial)}}})
    calls = []
    monkeypatch.setattr(gab, "_spawn_build", lambda d: calls.append(d))

    gab.ensure_project_graph_async("proj")

    assert {str(c) for c in calls} == {str(partial.resolve())}


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


class _FakePopen:
    """Stand-in for `subprocess.Popen` that mimics the real object's
    `.pid`/`.returncode`/`.communicate()`/`.kill()` surface, so `_run_build`
    can be exercised without a real graft subprocess while still letting
    tests assert the exact argv it was launched with."""

    def __init__(self, argv, returncode=0, err="", timeout=False, **kwargs):
        self.argv = argv
        self.pid = 4242
        self.returncode = returncode
        self._err = err
        self._timeout = timeout
        self.killed = False

    def communicate(self, timeout=None):
        if self._timeout and timeout is not None:
            self._timeout = False  # only raise once — the retry after kill() must succeed
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return "", self._err

    def kill(self):
        self.killed = True


def test_run_build_invokes_graft_build_no_deep_no_init(tmp_path, monkeypatch):
    """Structural layer only: the actual subprocess argv must be exactly
    `<graft> --dir <store> build <staging dir>` — no --deep, no init. The
    graph store is external to *target* (#146 follow-up) so this also
    asserts the store dir sits OUTSIDE the target and nothing lands inside
    target. The build argv points at a STAGING mirror, not *target* itself
    (H1) — see `test_run_build_stages_only_git_nonignored_files` for what
    goes into that copy, and `test_run_build_leaves_staging_dir_in_place`
    for why it must NOT be torn down afterwards (H1 follow-up, 2026-08-06).

    *target* is a dedicated subdir, deliberately NOT `tmp_path` itself —
    conftest's isolated runtime (where `graph_store_dir` resolves under in
    tests) also lives under `tmp_path`, so using `tmp_path` as the target
    would make the store a descendant of the target by test-harness
    coincidence even though production DATA_HOME and a project path are
    always unrelated directories.
    """
    target = tmp_path / "target-repo"
    target.mkdir()
    (target / "main.py").write_text("pass", encoding="utf-8")
    monkeypatch.setattr(gab, "_git_nonignored_files", lambda t: ["main.py"])
    captured = {}

    def _fake_popen(argv, **kwargs):
        captured["argv"] = argv
        return _FakePopen(argv, returncode=0)

    monkeypatch.setattr(gab.subprocess, "Popen", _fake_popen)

    ok, _elapsed, _err = gab._run_build("graft.cmd", target)

    assert ok is True
    expected_store = gab.graph_store_dir(target)
    staging_dir = Path(captured["argv"][-1])
    assert captured["argv"][:4] == ["graft.cmd", "--dir", str(expected_store), "build"]
    assert str(expected_store) != str(target)
    assert not str(expected_store).startswith(str(target))
    assert str(staging_dir) != str(target)
    assert not str(staging_dir).startswith(str(expected_store))  # never nested in the store either
    assert list(target.iterdir()) == [target / "main.py"]  # nothing else written into target


def test_run_build_leaves_staging_dir_in_place(tmp_path, monkeypatch):
    """H1 follow-up (2026-08-06): the staging mirror must survive the build,
    at the deterministic `staging_dir_for(target)` path — NOT a one-shot
    tempdir deleted afterwards. A deleted mirror is exactly what made every
    subsequent `graft ask`/`graft mcp` query (both default their `dir`
    argument to the pane's own unfiltered cwd) rebuild the graph unfiltered
    on first use — see the module docstring and `graft_store.staging_dir_for`
    for the full mechanism this proved out."""
    target = tmp_path / "target-repo"
    target.mkdir()
    (target / "main.py").write_text("pass", encoding="utf-8")
    monkeypatch.setattr(gab, "_git_nonignored_files", lambda t: ["main.py"])
    monkeypatch.setattr(gab.subprocess, "Popen", lambda argv, **k: _FakePopen(argv, returncode=0))

    gab._run_build("graft.cmd", target)

    staging = gab.staging_dir_for(target)
    assert staging.is_dir()
    assert (staging / "main.py").read_text(encoding="utf-8") == "pass"


def test_run_build_stages_only_git_nonignored_files(tmp_path, monkeypatch):
    """The staged copy handed to `graft build` contains exactly what
    `_git_nonignored_files` reported — not the raw target tree — which is
    the actual H1 fix (graft has no `.gitignore` support of its own, see
    module docstring)."""
    target = tmp_path / "target-repo"
    (target / "src").mkdir(parents=True)
    (target / "src" / "main.py").write_text("real source", encoding="utf-8")
    (target / "runtime").mkdir()
    (target / "runtime" / "bloat.py").write_text("gitignored venv noise", encoding="utf-8")
    monkeypatch.setattr(gab, "_git_nonignored_files", lambda t: ["src/main.py"])
    seen_staging = {}

    def _fake_popen(argv, **kwargs):
        staging_dir = Path(argv[-1])
        seen_staging["files"] = sorted(
            str(p.relative_to(staging_dir)).replace("\\", "/")
            for p in staging_dir.rglob("*")
            if p.is_file()
        )
        return _FakePopen(argv, returncode=0)

    monkeypatch.setattr(gab.subprocess, "Popen", _fake_popen)

    gab._run_build("graft.cmd", target)

    assert seen_staging["files"] == ["src/main.py"]


def test_run_build_removes_stale_staged_files_on_resync(tmp_path, monkeypatch):
    """The staging mirror is now persistent (H1 follow-up, 2026-08-06) — a
    file deleted/renamed/newly-gitignored since the last build must not
    linger in the mirror forever, or the mirror silently diverges from
    `_git_nonignored_files`'s current answer and grows unbounded across
    renames."""
    target = tmp_path / "target-repo"
    (target / "src").mkdir(parents=True)
    (target / "src" / "keep.py").write_text("keep", encoding="utf-8")
    monkeypatch.setattr(gab.subprocess, "Popen", lambda argv, **k: _FakePopen(argv, returncode=0))

    monkeypatch.setattr(gab, "_git_nonignored_files", lambda t: ["src/keep.py", "src/gone.py"])
    (target / "src" / "gone.py").write_text("temporary", encoding="utf-8")
    gab._run_build("graft.cmd", target)
    staging = gab.staging_dir_for(target)
    assert {p.name for p in staging.rglob("*") if p.is_file()} == {"keep.py", "gone.py"}

    # gone.py is deleted from target and no longer reported non-ignored —
    # the NEXT build must remove it from the persistent mirror too.
    (target / "src" / "gone.py").unlink()
    monkeypatch.setattr(gab, "_git_nonignored_files", lambda t: ["src/keep.py"])
    gab._run_build("graft.cmd", target)

    assert {p.name for p in staging.rglob("*") if p.is_file()} == {"keep.py"}


def test_run_build_refreshes_changed_content_on_resync(tmp_path, monkeypatch):
    """A file whose content changed between builds must be picked up in the
    persistent mirror, not left pointing at whatever was staged the first
    time (`_stage_files`'s unlink-before-relink)."""
    target = tmp_path / "target-repo"
    target.mkdir()
    (target / "main.py").write_text("version one", encoding="utf-8")
    monkeypatch.setattr(gab, "_git_nonignored_files", lambda t: ["main.py"])
    monkeypatch.setattr(gab.subprocess, "Popen", lambda argv, **k: _FakePopen(argv, returncode=0))

    gab._run_build("graft.cmd", target)
    staging = gab.staging_dir_for(target)
    assert (staging / "main.py").read_text(encoding="utf-8") == "version one"

    (target / "main.py").write_text("version two", encoding="utf-8")
    gab._run_build("graft.cmd", target)

    assert (staging / "main.py").read_text(encoding="utf-8") == "version two"


def test_run_build_skips_target_with_no_git_files(tmp_path, monkeypatch):
    """H1(a) / L5: a target that isn't a git work-tree (or has nothing
    non-ignored) is skipped outright — never handed to `graft build` raw.
    This is the actual behavior change from the pre-fix code, which built
    `target` directly regardless of git status."""
    target = tmp_path / "not-a-repo"
    target.mkdir()
    monkeypatch.setattr(gab, "_git_nonignored_files", lambda t: None)
    calls = []
    monkeypatch.setattr(
        gab.subprocess, "Popen", lambda *a, **k: calls.append(a) or _FakePopen(a[0])
    )

    ok, _elapsed, err = gab._run_build("graft.cmd", target)

    assert ok is False
    assert "skipped" in err
    assert calls == []  # graft was never invoked


def test_run_build_writes_store_manifest_on_success(tmp_path, monkeypatch):
    target = tmp_path / "target-repo"
    target.mkdir()
    (target / "main.py").write_text("pass", encoding="utf-8")
    monkeypatch.setattr(gab, "_git_nonignored_files", lambda t: ["main.py"])
    monkeypatch.setattr(gab.subprocess, "Popen", lambda argv, **k: _FakePopen(argv, returncode=0))

    gab._run_build("graft.cmd", target)

    store = gab.graph_store_dir(target)
    manifest = store / "source.json"
    assert manifest.is_file()
    assert json.loads(manifest.read_text(encoding="utf-8"))["source"] == str(target.resolve())
    assert gab.has_completed_build(store)  # H2 marker written on success


def test_run_build_no_manifest_or_marker_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "target-repo"
    target.mkdir()
    (target / "main.py").write_text("pass", encoding="utf-8")
    monkeypatch.setattr(gab, "_git_nonignored_files", lambda t: ["main.py"])
    monkeypatch.setattr(gab.subprocess, "Popen", lambda argv, **k: _FakePopen(argv, returncode=1))

    ok, _elapsed, _err = gab._run_build("graft.cmd", target)

    assert ok is False
    store = gab.graph_store_dir(target)
    assert not (store / "source.json").is_file()
    assert not gab.has_completed_build(store)


def test_run_build_kills_process_tree_on_timeout(tmp_path, monkeypatch):
    """M4: on timeout, `_run_build` must kill the process TREE (not just the
    direct child) via `_kill_orphan_tree`, and still return a clean failure
    instead of propagating the TimeoutExpired."""
    target = tmp_path / "target-repo"
    target.mkdir()
    (target / "main.py").write_text("pass", encoding="utf-8")
    monkeypatch.setattr(gab, "_git_nonignored_files", lambda t: ["main.py"])
    fake_proc = _FakePopen(["graft.cmd"], timeout=True)
    monkeypatch.setattr(gab.subprocess, "Popen", lambda argv, **k: fake_proc)
    killed_pids = []
    monkeypatch.setattr(gab, "_kill_orphan_tree", lambda pid: killed_pids.append(pid))

    ok, _elapsed, err = gab._run_build("graft.cmd", target)

    assert ok is False
    assert "timed out" in err
    assert killed_pids == [fake_proc.pid]
    assert fake_proc.killed is True


def test_kill_orphan_tree_uses_taskkill_on_windows(monkeypatch):
    monkeypatch.setattr(gab.sys, "platform", "win32")
    calls = []
    monkeypatch.setattr(
        gab.subprocess,
        "run",
        lambda argv, **k: calls.append(argv) or subprocess.CompletedProcess(argv, 0),
    )

    gab._kill_orphan_tree(1234)

    assert calls == [["taskkill", "/PID", "1234", "/T", "/F"]]


def test_kill_orphan_tree_noop_off_windows(monkeypatch):
    monkeypatch.setattr(gab.sys, "platform", "linux")
    calls = []
    monkeypatch.setattr(gab.subprocess, "run", lambda *a, **k: calls.append(a))

    gab._kill_orphan_tree(1234)

    assert calls == []


# ── _git_nonignored_files: real git, real .gitignore ─────────────────────


def test_git_nonignored_files_excludes_gitignored_dir(tmp_path):
    """Integration test against the REAL `git` binary (no mocking) — proves
    the actual mechanism `_run_build` relies on to keep gitignored bulk
    (this repo's own `runtime/`, H1) out of the staged copy."""
    if gab._git_bin() is None:
        pytest.skip("git not on PATH")
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "ignored").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (repo / "src" / "main.py").write_text("pass", encoding="utf-8")
    (repo / "ignored" / "bloat.py").write_text("noise", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", "src/main.py"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-q", "-m", "init"],
        cwd=repo,
        check=True,
    )

    files = gab._git_nonignored_files(repo)

    assert files is not None
    normalized = {f.replace("\\", "/") for f in files}
    assert normalized == {".gitignore", "src/main.py"}


def test_git_nonignored_files_none_for_non_git_dir(tmp_path):
    if gab._git_bin() is None:
        pytest.skip("git not on PATH")
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    (plain / "data.txt").write_text("x", encoding="utf-8")

    assert gab._git_nonignored_files(plain) is None


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
