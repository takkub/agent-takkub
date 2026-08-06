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


def test_dirs_for_project_dedup_key_matches_graph_store_key(tmp_path):
    """M5 (2026-08-06): the dedup key here must be the SAME `graph_key()`
    `graph_store_dir` uses, not a hand-rolled `os.name == 'nt'` casing — the
    old expression only case-folded on Windows, so on macOS two
    case-variant `paths` entries for the same directory survived dedup as
    two dirs, each spawning its own `graft build` thread into what
    `graph_store_dir` folds to the SAME store (concurrent-write
    corruption). Directly asserting the key equality here (rather than
    faking a case-insensitive filesystem) is what actually pins the
    invariant regardless of which OS the suite runs on."""
    d = tmp_path / "root"
    d.mkdir()

    dirs = gab._dirs_for_project({"paths": {"a": str(d)}})

    assert len(dirs) == 1
    assert gab.graph_key(dirs[0]) == gab.graph_key(d.resolve())


def test_dirs_for_project_dedup_actually_calls_graph_key(tmp_path, monkeypatch):
    """Wiring proof (platform-independent, unlike a real case-variant-path
    repro which only manifests on macOS): two DIFFERENT directories that
    `graph_key` maps to the same value must collapse to one entry — proves
    dedup goes through `graph_key`, not a re-derived casing rule that could
    silently drift from it again."""
    d1 = tmp_path / "one"
    d2 = tmp_path / "two"
    d1.mkdir()
    d2.mkdir()
    monkeypatch.setattr(gab, "graph_key", lambda p: "same-key-regardless-of-path")

    dirs = gab._dirs_for_project({"paths": {"a": str(d1), "b": str(d2)}})

    assert len(dirs) == 1


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


def test_stage_files_skips_relink_for_already_hardlinked_unchanged_file(tmp_path, monkeypatch):
    """M1 (2026-08-06): re-`unlink`+`link`ing every file every cycle even
    when nothing changed was measured at ~264ms of pure disk churn for a
    739-file repo. A dst already hardlinked to the CURRENT src (same inode)
    must be left alone — proven here by spying on `os.link`/`os.unlink` and
    asserting neither is called on the second, no-op `_stage_files` pass."""
    target = tmp_path / "target-repo"
    target.mkdir()
    (target / "main.py").write_text("unchanged", encoding="utf-8")
    staging = tmp_path / "staging"

    gab._stage_files(target, ["main.py"], staging)
    assert (staging / "main.py").stat().st_ino == (target / "main.py").stat().st_ino

    link_calls = []
    unlink_calls = []
    real_link = gab.os.link
    real_unlink = Path.unlink

    def _spy_link(src, dst):
        link_calls.append((src, dst))
        return real_link(src, dst)

    def _spy_unlink(self, *a, **k):
        if self == staging / "main.py":
            unlink_calls.append(self)
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(gab.os, "link", _spy_link)
    monkeypatch.setattr(Path, "unlink", _spy_unlink)

    gab._stage_files(target, ["main.py"], staging)

    assert link_calls == []
    assert unlink_calls == []


def test_stage_files_relinks_on_st_ino_collision_across_devices(tmp_path, monkeypatch):
    """R-1 (2026-08-06 re-review): the M1 fix above compared `st_ino` alone,
    but inode/file-index numbers are allocated PER VOLUME — a bare `st_ino`
    match is only proof of identity when src and dst are known to share a
    device. Cross-device staging is the documented `AGENT_TAKKUB_HOME` use
    case (moving cockpit data off a small/boot drive), and on that path
    `_stage_files` always takes the `copy2` fallback, landing dst in an
    INDEPENDENT inode space from src — a coincidental `st_ino` match there
    must not be read as 'already synced', or a real content change is
    silently skipped forever. `os.path.samestat` (`st_dev` AND `st_ino`)
    fixes this; proven here by faking a dst stat that collides on `st_ino`
    with a DIFFERENT `st_dev` and asserting the changed content still gets
    relinked instead of being skipped."""
    target = tmp_path / "target-repo"
    target.mkdir()
    (target / "main.py").write_text("version one", encoding="utf-8")
    staging = tmp_path / "staging"
    gab._stage_files(target, ["main.py"], staging)
    assert (staging / "main.py").read_text(encoding="utf-8") == "version one"

    (target / "main.py").write_text("version two", encoding="utf-8")
    src_ino = (target / "main.py").stat().st_ino
    dst_path = staging / "main.py"
    real_stat_or_none = gab._stat_or_none

    class _FakedStat:
        def __init__(self, real):
            self.st_mode = real.st_mode
            self.st_ino = src_ino  # collides with src...
            self.st_dev = real.st_dev + 1  # ...but on a DIFFERENT device

    def _fake_stat_or_none(path):
        result = real_stat_or_none(path)
        if result is not None and path == dst_path:
            return _FakedStat(result)
        return result

    monkeypatch.setattr(gab, "_stat_or_none", _fake_stat_or_none)

    gab._stage_files(target, ["main.py"], staging)

    assert (staging / "main.py").read_text(encoding="utf-8") == "version two", (
        "a bare st_ino match must not be treated as 'already synced' across devices"
    )


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


def test_run_build_unwritable_store_reported_via_get_build_status(tmp_path, monkeypatch):
    """M5 (2026-08-05 cross-OS audit): an unwritable `~` (locked-down
    corporate/roaming profile) used to degrade completely silently — `store.
    mkdir` fails, the caller gets a descriptive error string back, but
    nothing surfaced it anywhere a user could see. This is now visible via
    `get_build_status()`'s `failed` list (the same UI status-bar chip
    surfaces any build failure, not just this one) — end-to-end through the
    REAL `_run_build` + `_build_one`, not a stubbed failure."""
    target = tmp_path / "target-repo"
    target.mkdir()
    (target / "main.py").write_text("pass", encoding="utf-8")
    monkeypatch.setattr(gab, "_graft_cli", lambda: r"C:\fake\graft.cmd")
    monkeypatch.setattr(gab, "_git_nonignored_files", lambda t: ["main.py"])

    real_mkdir = Path.mkdir

    def _unwritable_mkdir(self, *a, **k):
        if self == gab.graph_store_dir(target):
            raise OSError(13, "Permission denied")
        return real_mkdir(self, *a, **k)

    monkeypatch.setattr(Path, "mkdir", _unwritable_mkdir)

    gab._build_one(target)

    status = gab.get_build_status()
    assert status["building"] == 0
    assert str(target) in status["failed"]


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


# ── get_build_status(): UI status-bar chip snapshot ───────────────────────


def test_get_build_status_empty_by_default(monkeypatch):
    # `_building`/`_last_build_failed` are module-level state shared with
    # every other test in this file (mirrors `_building`/`_debounce_timers`'
    # existing lack of per-test isolation) — reset explicitly rather than
    # relying on test execution order leaving them empty.
    monkeypatch.setattr(gab, "_last_build_failed", {})
    monkeypatch.setattr(gab, "_building", set())
    monkeypatch.setattr(gab, "_in_flight", set())

    assert gab.get_build_status() == {"building": 0, "failed": []}


def test_get_build_status_reports_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(gab, "_graft_cli", lambda: r"C:\fake\graft.cmd")
    monkeypatch.setattr(gab, "_run_build", lambda graft_bin, target: (False, 0.01, "boom"))
    target = tmp_path / "fails-repo"

    gab._build_one(target)

    status = gab.get_build_status()
    assert status["building"] == 0
    assert str(target) in status["failed"]


def test_get_build_status_clears_failure_on_next_success(tmp_path, monkeypatch):
    monkeypatch.setattr(gab, "_graft_cli", lambda: r"C:\fake\graft.cmd")
    target = tmp_path / "flaky-repo"

    monkeypatch.setattr(gab, "_run_build", lambda graft_bin, target: (False, 0.01, "boom"))
    gab._build_one(target)
    assert str(target) in gab.get_build_status()["failed"]

    monkeypatch.setattr(gab, "_run_build", lambda graft_bin, target: (True, 0.01, ""))
    gab._build_one(target)

    assert str(target) not in gab.get_build_status()["failed"]


def test_get_build_status_prunes_failure_after_ttl(tmp_path, monkeypatch):
    """2026-08-06 follow-up: a dir removed from projects.json after a failed
    build has no trigger left to ever retry/clear it, so `failed` used to
    stick in the chip forever. `get_build_status()` now lazily prunes any
    entry whose failure is older than `_FAILED_ENTRY_TTL_S`."""
    monkeypatch.setattr(gab, "_graft_cli", lambda: r"C:\fake\graft.cmd")
    monkeypatch.setattr(gab, "_run_build", lambda graft_bin, target: (False, 0.01, "boom"))
    target = tmp_path / "abandoned-repo"

    fake_now = [1000.0]
    monkeypatch.setattr(gab.time, "monotonic", lambda: fake_now[0])

    gab._build_one(target)
    assert str(target) in gab.get_build_status()["failed"]

    fake_now[0] += gab._FAILED_ENTRY_TTL_S + 1.0

    status = gab.get_build_status()
    assert str(target) not in status["failed"]
    assert str(target) not in gab._last_build_failed


def test_get_build_status_counts_in_flight_builds(tmp_path, monkeypatch):
    monkeypatch.setattr(gab, "_graft_cli", lambda: r"C:\fake\graft.cmd")
    entered = threading.Event()
    release = threading.Event()

    def _fake_run_build(graft_bin, target):
        entered.set()
        release.wait(timeout=5)
        return True, 0.01, ""

    monkeypatch.setattr(gab, "_run_build", _fake_run_build)
    target = tmp_path / "slow-repo"

    t1 = threading.Thread(target=gab._build_one, args=(target,))
    t1.start()
    assert entered.wait(timeout=5), "build never started"

    assert gab.get_build_status()["building"] == 1

    release.set()
    t1.join(timeout=5)
    assert gab.get_build_status()["building"] == 0


def test_get_build_status_distinguishes_queued_from_in_flight(tmp_path, monkeypatch):
    """M2 (2026-08-06): at boot, `build_all_projects_async` spawns one thread
    per distinct project dir — 46 on the pilot machine — but only
    `_MAX_CONCURRENT_BUILDS` (3) ever run inside the semaphore at once. The
    chip must report the semaphore-bounded number, not every thread that has
    merely started and is blocked waiting for a slot."""
    monkeypatch.setattr(gab, "_graft_cli", lambda: r"C:\fake\graft.cmd")
    monkeypatch.setattr(gab, "_MAX_CONCURRENT_BUILDS", 2)
    monkeypatch.setattr(gab, "_build_semaphore", threading.Semaphore(2))
    entered = threading.Semaphore(0)
    release = threading.Event()

    def _fake_run_build(graft_bin, target):
        entered.release()
        release.wait(timeout=5)
        return True, 0.01, ""

    monkeypatch.setattr(gab, "_run_build", _fake_run_build)

    targets = [tmp_path / f"repo-{i}" for i in range(5)]
    threads = [threading.Thread(target=gab._build_one, args=(t,)) for t in targets]
    for th in threads:
        th.start()

    # Wait for exactly the semaphore cap (2) to actually enter `_run_build`.
    assert entered.acquire(timeout=5)
    assert entered.acquire(timeout=5)

    status = gab.get_build_status()
    assert status["building"] == 2, "chip must report in-flight, not every queued thread"
    assert len(gab._building) == 5, "single-flight guard still tracks all 5 as claimed"

    release.set()
    for th in threads:
        th.join(timeout=5)
    assert gab.get_build_status()["building"] == 0


# ── live mid-task resync (staleness gap, 2026-08-06) ─────────────────────


def test_resync_staging_only_noop_when_env_set(tmp_path, monkeypatch):
    monkeypatch.setenv("TAKKUB_SKIP_GRAFT_BUILD", "1")
    calls = []
    monkeypatch.setattr(gab, "_git_nonignored_files", lambda t: calls.append(t) or None)
    monkeypatch.setattr(gab.threading, "Thread", _SyncThread)

    gab.resync_staging_only(str(tmp_path))

    assert calls == []


def test_resync_staging_only_noop_without_cwd(monkeypatch):
    monkeypatch.delenv("TAKKUB_SKIP_GRAFT_BUILD", raising=False)
    calls = []
    monkeypatch.setattr(gab, "_git_nonignored_files", lambda t: calls.append(t) or None)
    monkeypatch.setattr(gab.threading, "Thread", _SyncThread)

    gab.resync_staging_only(None)
    gab.resync_staging_only("")

    assert calls == []


def test_resync_staging_only_noop_when_graft_cli_missing(tmp_path, monkeypatch):
    """M3 (2026-08-06): trigger 4 was the only one of the four triggers that
    didn't check `_graft_cli() is None` — on a machine without graft
    installed (the default; `doctor.check_graft` only installs under
    `--fix`), every LIVE pane paid a `git ls-files` subprocess + two thread
    spawns every 15s forever, for a feature never enabled. It must now be a
    no-op, exactly like `build_all_projects_async`/`ensure_project_graph_async`/
    `schedule_rebuild_after_done` already are."""
    monkeypatch.delenv("TAKKUB_SKIP_GRAFT_BUILD", raising=False)
    monkeypatch.setattr(gab, "_graft_cli", lambda: None)
    calls = []
    monkeypatch.setattr(gab, "_git_nonignored_files", lambda t: calls.append(t) or None)
    monkeypatch.setattr(gab.threading, "Thread", _SyncThread)

    gab.resync_staging_only(str(tmp_path))

    assert calls == []


def test_resync_staging_only_noop_for_missing_dir(tmp_path, monkeypatch):
    """MED-3 (2026-08-06 re-review): CI never installs graft (`ci.yml`
    grepped — no install step), so `_graft_cli()` is `None` on every CI
    runner and the missing-dir guard below it was never actually reached —
    the test passed identically with that guard deleted (verified with
    `probe_vacuous.py`). Mocking `_graft_cli` here makes this exercise the
    real guard on every machine, not just ones with graft on PATH."""
    monkeypatch.delenv("TAKKUB_SKIP_GRAFT_BUILD", raising=False)
    monkeypatch.setattr(gab, "_graft_cli", lambda: r"C:\fake\graft.cmd")
    calls = []
    monkeypatch.setattr(gab, "_git_nonignored_files", lambda t: calls.append(t) or None)
    monkeypatch.setattr(gab.threading, "Thread", _SyncThread)

    gab.resync_staging_only(str(tmp_path / "does-not-exist"))

    assert calls == []


def test_resync_staging_only_syncs_staging_mirror(tmp_path, monkeypatch):
    """The actual gap this closes: a pane mid-task edits a file ALREADY
    known to the graph (staged by an earlier build) — no `done()` yet, so
    `schedule_rebuild_after_done` never fires — and asks graft about it.
    This proves the staging mirror (the directory graft's OWN freshness
    gate re-probes on every query, see `graft_store.staging_dir_for`'s
    docstring) is brought up to date without running a `graft build`
    subprocess at all. A file with NO prior staged entry is a different
    case — see `test_resync_staging_only_escalates_to_full_build_for_new_file`."""
    monkeypatch.delenv("TAKKUB_SKIP_GRAFT_BUILD", raising=False)
    monkeypatch.setattr(gab, "_graft_cli", lambda: r"C:\fake\graft.cmd")
    monkeypatch.setattr(gab.threading, "Thread", _SyncThread)
    target = tmp_path / "target-repo"
    target.mkdir()
    (target / "main.py").write_text("version two", encoding="utf-8")
    monkeypatch.setattr(gab, "_git_nonignored_files", lambda t: ["main.py"])
    staging = gab.staging_dir_for(target)
    staging.mkdir(parents=True)
    (staging / "main.py").write_text("version one", encoding="utf-8")  # staged by a prior build
    build_calls = []
    monkeypatch.setattr(gab.subprocess, "Popen", lambda *a, **k: build_calls.append(a) or None)

    gab.resync_staging_only(str(target))

    assert (staging / "main.py").read_text(encoding="utf-8") == "version two"
    assert build_calls == []  # never shells out to `graft build`


def test_has_new_files_never_escalates_for_a_permanently_unstageable_entry(tmp_path):
    """HIGH-1 (2026-08-06): `git ls-files` reports a git SUBMODULE as one
    entry, but it's a DIRECTORY on disk — `_stage_files` can never actually
    stage it (`os.link`/`copy2` both raise on a directory), so before this
    fix `_has_new_files` saw it as permanently "missing from the mirror" and
    escalated to a full `_spawn_build` every single cycle, forever (verified
    against a real `git submodule add` repo). A broken symlink, a directory
    symlink, and a MAX_PATH-too-long entry all collapse to this exact same
    shape — `_stageable`'s `path.stat()` fails or reports a non-regular
    file for every one of them, same as a directory does.

    This uses a real directory (not a symlink — Windows dev boxes without
    Developer Mode/admin can't create one, as this repo's own submodule
    verification hit) since a directory alone is enough to reproduce the
    exact `_stage_files` failure mode H1 was filed against."""
    target = tmp_path / "target-repo"
    (target / "vendorsub").mkdir(parents=True)  # a "submodule" — a dir, not a file
    (target / "vendorsub" / "inner.txt").write_text("nested", encoding="utf-8")
    (target / "r.txt").write_text("real file", encoding="utf-8")
    rel_paths = ["r.txt", "vendorsub"]  # git reports the submodule as ONE entry
    staging = tmp_path / "staging"

    for _cycle in range(4):
        gab._sync_staging(target, rel_paths, staging)
        assert gab._has_new_files(target, rel_paths, staging) is False, (
            "an unstageable directory entry must never read as 'new' — "
            "that is exactly what fed the infinite rebuild loop"
        )

    staged = {p.name for p in staging.rglob("*") if p.is_file()}
    assert staged == {"r.txt"}  # the submodule dir itself was never staged


def test_build_one_gives_up_escalating_for_a_permanent_destination_side_failure(
    tmp_path, monkeypatch
):
    """HIGH-1 residual (2026-08-06 re-review): `_stageable`'s SOURCE-side
    gate (see the test above) closes every class visible by `stat()`-ing
    the SOURCE — but a MAX_PATH-too-long staged path (or any other reason
    `os.link`/`shutil.copy2` can never land the file at the DESTINATION)
    stats fine as a source and read as 'new' on every `_has_new_files` call
    forever before this fix, re-escalating to a full `_spawn_build` roughly
    every 15s for the rest of the pane's life — proven with a real
    MAX_PATH-shaped failure via the reviewer's `probe_h1.py` §3.

    Proves the escalation now converges: once a real, completed build's own
    `_sync_staging` still can't land the rel, `_build_one` must remember it
    (`_unresolvable_rels`) so `_new_stageable_rels`/`_has_new_files` never
    escalate on it again."""
    target = tmp_path / "target-repo"
    (target / "pkg").mkdir(parents=True)
    (target / "pkg" / "deep.py").write_text("x", encoding="utf-8")
    (target / "ok.py").write_text("y", encoding="utf-8")
    rel_paths = ["ok.py", "pkg/deep.py"]
    monkeypatch.setattr(gab, "_git_nonignored_files", lambda t: rel_paths)
    monkeypatch.setattr(gab, "_graft_cli", lambda: r"C:\fake\graft.cmd")
    monkeypatch.setattr(gab.subprocess, "Popen", lambda argv, **k: _FakePopen(argv, returncode=0))

    # Simulate a destination that can NEVER receive deep.py no matter how
    # many times staging is retried — the observable shape of a MAX_PATH
    # failure (`os.link` then `shutil.copy2` both raise), without needing a
    # real >259-char path to reproduce it.
    real_link = gab.os.link
    real_copy2 = gab.shutil.copy2

    def _fail_link(src, dst):
        if Path(dst).name == "deep.py":
            raise OSError(206, "path too long (simulated)")
        return real_link(src, dst)

    def _fail_copy2(src, dst, *a, **k):
        if Path(dst).name == "deep.py":
            raise OSError(206, "path too long (simulated)")
        return real_copy2(src, dst, *a, **k)

    monkeypatch.setattr(gab.os, "link", _fail_link)
    monkeypatch.setattr(gab.shutil, "copy2", _fail_copy2)

    staging = gab.staging_dir_for(target)
    key = str(target)

    # Cycle 1: looks new (indistinguishable from "not synced yet"), so it
    # escalates — `_build_one` runs a real build whose own `_sync_staging`
    # still can't land it.
    new_rels = gab._new_stageable_rels(target, rel_paths, staging)
    assert "pkg/deep.py" in new_rels
    gab._pending_escalation[key] = frozenset(new_rels)
    gab._build_one(target)

    assert "pkg/deep.py" not in gab._staging_relpaths(staging)
    assert "pkg/deep.py" in gab._unresolvable_rels[key]

    # Cycle 2: without the memory this would escalate again, forever.
    assert gab._new_stageable_rels(target, rel_paths, staging) == set()
    assert gab._has_new_files(target, rel_paths, staging) is False


def test_resync_staging_only_escalates_to_full_build_for_new_file(tmp_path, monkeypatch):
    """New-file gap (2026-08-06 follow-up to the staleness fix above):
    `_sync_staging` alone happily copies a brand-new file into the mirror,
    but graft's own freshness gate only reliably rebuilds for files it
    already has a fingerprint entry for (verified against the real CLI — a
    file with no prior entry stayed invisible to `ask`/`mcp` after a plain
    mirror sync; only a real `graft build` picked it up). So a file not yet
    present in the staging mirror must escalate to a full build instead of
    a no-op-for-graft-purposes sync."""
    monkeypatch.delenv("TAKKUB_SKIP_GRAFT_BUILD", raising=False)
    monkeypatch.setattr(gab.threading, "Thread", _SyncThread)
    monkeypatch.setattr(gab, "_graft_cli", lambda: r"C:\fake\graft.cmd")
    target = tmp_path / "target-repo"
    target.mkdir()
    (target / "main.py").write_text("existing", encoding="utf-8")
    (target / "brand_new.py").write_text("new file", encoding="utf-8")
    staging = gab.staging_dir_for(target)
    staging.mkdir(parents=True)
    (staging / "main.py").write_text("existing", encoding="utf-8")  # staged by a prior build
    monkeypatch.setattr(gab, "_git_nonignored_files", lambda t: ["main.py", "brand_new.py"])
    build_calls = []

    def _fake_popen(argv, **kwargs):
        build_calls.append(argv)
        return _FakePopen(argv, returncode=0)

    monkeypatch.setattr(gab.subprocess, "Popen", _fake_popen)

    gab.resync_staging_only(str(target))

    assert len(build_calls) == 1, (
        "a new file must trigger a real `graft build`, not just a mirror sync"
    )
    assert build_calls[0][:4] == [
        r"C:\fake\graft.cmd",
        "--dir",
        str(gab.graph_store_dir(target)),
        "build",
    ]
    # the full build's own `_sync_staging` call still stages the new file
    assert (staging / "brand_new.py").read_text(encoding="utf-8") == "new file"
    # a file that WAS successfully staged must never be misfiled as
    # unresolvable (HIGH-1 residual, 2026-08-06 re-review) — `_build_one`
    # checked convergence and it actually converged.
    assert gab._unresolvable_rels.get(str(target)) in (None, frozenset())


def test_resync_staging_only_throttled_per_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("TAKKUB_SKIP_GRAFT_BUILD", raising=False)
    monkeypatch.setattr(gab, "_graft_cli", lambda: r"C:\fake\graft.cmd")
    monkeypatch.setattr(gab.threading, "Thread", _SyncThread)
    target = tmp_path / "target-repo"
    target.mkdir()
    calls = []
    monkeypatch.setattr(gab, "_git_nonignored_files", lambda t: calls.append(t) or None)

    gab.resync_staging_only(str(target))
    gab.resync_staging_only(str(target))  # immediate 2nd call, same dir

    assert len(calls) == 1, "second call inside the throttle window must be a no-op"


def test_resync_staging_only_skips_dir_with_full_build_in_flight(tmp_path, monkeypatch):
    """A full `graft build` for this dir is already running — its own
    `_sync_staging` call already covers this pass, so a concurrent live
    resync would just race it for nothing.

    MED-3 (2026-08-06 re-review): this is the only test of the `_building`
    in-flight guard, and without mocking `_graft_cli` it passed identically
    on CI (no graft installed) whether or not the guard under test existed
    at all — verified with `probe_vacuous.py`. Mocking `_graft_cli` here
    makes the assertion actually depend on the guard."""
    monkeypatch.delenv("TAKKUB_SKIP_GRAFT_BUILD", raising=False)
    monkeypatch.setattr(gab, "_graft_cli", lambda: r"C:\fake\graft.cmd")
    monkeypatch.setattr(gab.threading, "Thread", _SyncThread)
    target = tmp_path / "target-repo"
    target.mkdir()
    calls = []
    monkeypatch.setattr(gab, "_git_nonignored_files", lambda t: calls.append(t) or None)
    gab._building.add(str(target.resolve()))
    try:
        gab.resync_staging_only(str(target))
    finally:
        gab._building.discard(str(target.resolve()))

    assert calls == []


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
