"""Auto-run `graft build` so the graft MCP (shared_dev_tools.GRAFT_MCP) has a
graph to answer from without the user having to run the CLI by hand.

Without this, a project that never ran `graft build` gets graceful-but-empty
answers forever (shared_dev_tools.py's comment on GRAFT_MCP) — the MCP is
wired into every code-reading role's pane, but silently useless until someone
knows to run the command themselves.

Three triggers call into this module (each fire-and-forget, background only):
  * cockpit boot — `build_all_projects_async()` (orchestrator.py __init__)
  * project tab switch — `ensure_project_graph_async()`, only projects with
    no graph yet (main_window.py `_on_tab_switched`)
  * a teammate's `done()` — `schedule_rebuild_after_done()`, debounced so a
    burst of shards finishing together doesn't rebuild the same dir N times
    (orchestrator.py `done()`)

Structural layer ONLY — every build call is `graft --dir <store> build
<dir>`, never `--deep` (needs an API key the user hasn't approved paying
for) and never `graft init` (would overwrite the cockpit's own
`.claude/settings.json` + statusline, same rule `doctor.check_graft` already
documents).

Every build routes through graft's global `--dir <store>` flag (before the
subcommand) so the graph itself never lands inside the target directory —
see `graft_store.py`'s module docstring for why (#146 follow-up: writing
`.gitignore`/`.ignore`/`graft/` straight into 46 other repos on boot was a
real regression). `graft_store.graph_store_dir(target)` derives *store* from
a hash of *target*'s own resolved path, so each distinct target still gets
its own isolated graph.

`build` NEVER points at *target* directly (H1, 2026-08-05 cross-OS audit) —
it points at a staging mirror of only the files `git` itself considers
non-ignored under *target* (`_git_nonignored_files` + `_stage_files`).
Read directly from the shipped CLI (`dist/ingest/fs.js` in
`@nanonets/graft`): graft's own walker has no `--exclude` and does NOT
consult `.gitignore` at all — it skips dot-directories plus one fixed
9-name list (`node_modules`, `dist`, `build`, `out`, `target`, `vendor`,
`coverage`, `__pycache__`, `venv`) and nothing else. Handed a real project
root, it happily walks and parses every OTHER gitignored directory too —
verified empirically: this repo's own `runtime/` (venvs/worktrees/scratch,
`.gitignore` line 16, not on graft's fixed list) was 72% of a 507 MB store,
3591 of 4954 cards, for 143 cards of actual source. Staging only what `git
ls-files --cached --others --exclude-standard` reports makes "what gets
indexed" match this repo's own `.gitignore` exactly — deliberately NOT
`git archive HEAD`, which would index the last COMMIT and silently miss
live uncommitted edits, defeating the entire point of an agent-facing index.
A target with no git repository at all (a non-code documents/images folder
in projects.json, L5) has nothing `git ls-files` can report, so it is
skipped rather than indexed wholesale — same fix covers H1(a).

The staging mirror lives at `graft_store.staging_dir_for(target)` and is
PERSISTENT — it is never torn down after a build (H1 follow-up, 2026-08-06).
The original version of this fix used a `tempfile.mkdtemp()` copy deleted in
`_run_build`'s `finally` block, which passed every test and every manual
build-then-check-store-size probe, but broke the very first real QUERY:
graft's own freshness gate (`ensureFreshGraph` in its shipped
`graph/refresh.js`) re-probes the working tree at whatever `dir` argument
the CURRENT call was given — `graft ask`/`graft mcp [dir]` both default
`dir` to "." (the calling process's own cwd) when not given explicitly —
and, if that doesn't match what the graph was built from, silently rebuilds
UNFILTERED from it before answering. A pane's `graft mcp` (no positional
`dir`, see `shared_dev_tools.browser_profile_mcp_config_path`) defaults
that root to the pane's own cwd — the real, gitignored-bulk-containing
target — so with the staging copy already deleted, this fired on literally
every query, re-inflating the store by hundreds of MB the first time any
agent asked it a question. Verified against the real CLI: building into a
deleted tempdir then querying with the default `dir="."` reproduces
`[graft] refreshed the graph (N files changed) before answering` with N =
every file under the target; building into and querying against a
directory that stays in place with the same content produces no refresh at
all. Fix: keep the mirror around, and thread its path through as the
explicit positional `dir` on every build AND every query
(`shared_dev_tools.browser_profile_mcp_config_path` appends it after `mcp`)
so freshness always compares against the SAME stable, filtered root — see
`graft_store.staging_dir_for`'s docstring for the full mechanism and the
staleness trade-off this accepts. Each build re-syncs the mirror
(`_sync_staging`): existing files are refreshed in place (unlink-then-relink
so a rename-over-write editor save is picked up, not silently stuck on the
old inode) and files no longer in the current non-ignored set are removed,
so the mirror never accumulates stale content across renames/deletes.

Why a project's `paths` entries are built individually rather than once at
the project's common root: the graft MCP is spawned with a per-pane `--dir
<store>` templated from the SAME pane's own cwd (`shared_dev_tools.
browser_profile_mcp_config_path`, mirroring the browser-profile-isolation
precedent). A backend pane's cwd is `paths["api"]`, a frontend pane's is
`paths["web"]` — those are frequently *siblings*, sometimes one nested
inside the other (see docs/audit/2026-08-05-graft-pilot.md's disk-cost
note) — but a graph built only at the parent would leave `paths["api"]`'s
own store empty, so the backend pane's MCP call still returns empty. Each
distinct configured path gets its own build (and its own store, keyed
independently by `graph_key`).

Worktree isolation (#81) is skipped implicitly, not by special-casing it: the
only inputs this module ever reads are `projects.json`'s `paths` (the main
tree) and a `done()` pane's own cwd when NOT running under `--isolation
worktree` (orchestrator.py gates that call on `had_worktree`). A worktree
checkout lives under `worktree_manager.worktree_root()`
(`<DATA_HOME>/worktrees/<project>/...`), which this module never touches.

Kill switch: TAKKUB_SKIP_GRAFT_BUILD (any value other than "" or "0"),
mirroring shared_dev_tools.TAKKUB_SKIP_MCP_WARM — set by conftest.py for
every test run so the suite never spawns real `graft build` subprocesses.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from ._win_console import SUBPROCESS_NO_WINDOW
from .config import load_projects
from .graft_store import (
    graft_cli_path,
    graph_store_dir,
    has_completed_build,
    mark_build_complete,
    staging_dir_for,
    write_store_manifest,
)

_log = logging.getLogger(__name__)

# Generous ceiling for a huge repo's first-ever (no-cache) build. Runs in a
# background thread with a timeout, never on the Qt main thread, so a slow
# build only ties up its own daemon thread — never blocks spawn/UI.
_BUILD_TIMEOUT_S = 600

# How long to wait after the LAST done() touching a directory before
# rebuilding — coalesces a burst (e.g. 5 shards finishing within seconds)
# into one rebuild per directory instead of one per done().
_DEBOUNCE_S = 20.0

# Real repos vary hugely in size; cap concurrent `graft build` child
# processes so a boot-time fan-out across every projects.json path (measured
# 46 distinct dirs across 27 projects on the pilot machine) doesn't spike
# CPU/disk with dozens of node processes at once. Threads beyond this just
# wait on the semaphore — spawning them is cheap.
_MAX_CONCURRENT_BUILDS = 3
_build_semaphore = threading.Semaphore(_MAX_CONCURRENT_BUILDS)

_lock = threading.Lock()
_building: set[str] = set()  # abs dir path currently building (single-flight)
_debounce_timers: dict[str, threading.Timer] = {}  # abs dir path -> pending Timer


def _skip_env() -> bool:
    return os.environ.get("TAKKUB_SKIP_GRAFT_BUILD", "").strip() not in ("", "0")


def _graft_cli() -> str | None:
    return graft_cli_path()


def _dir_stats(root: Path) -> tuple[int, int]:
    """Best-effort `(total_bytes, file_count)` for *root* — mirrors
    `disk_usage._dir_stats`'s fail-soft posture but kept local so this
    module never has to import the UI-adjacent `disk_usage` (its own
    import-linter contract forbids that direction anyway)."""
    total = 0
    count = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                continue
            count += 1
    return total, count


def _fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _dirs_for_project(project: dict) -> list[Path]:
    """Every distinct, existing absolute directory in a project's `paths`.

    Dedupes identical entries (two path keys pointing at the same folder)
    but deliberately does NOT collapse a path nested inside another — see
    the module docstring for why (each is a separate pane cwd, hence a
    separate graft MCP root).
    """
    paths = project.get("paths") if isinstance(project, dict) else None
    if not isinstance(paths, dict):
        return []
    seen: dict[str, Path] = {}
    for raw in paths.values():
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            resolved = Path(raw).expanduser().resolve()
        except OSError:
            continue
        if not resolved.is_dir():
            continue
        key = str(resolved).lower() if os.name == "nt" else str(resolved)
        seen[key] = resolved
    return list(seen.values())


def _git_bin() -> str | None:
    return shutil.which("git")


def _git_nonignored_files(target: Path) -> list[str] | None:
    """`git ls-files` (tracked + untracked-but-not-`.gitignore`d) under
    *target*, as paths relative to *target*. `None` means "nothing safe to
    index" — git missing, *target* not a git work-tree, or the listing call
    itself failed — which is the caller's signal to skip the build entirely
    rather than hand graft the raw directory (H1a, L5). A git work-tree with
    zero non-ignored files also returns `None` (nothing to stage either
    way); a real project always has at least one.

    Separate seam from the actual `graft build` subprocess call so tests can
    fake this without touching the argv assertions on the graft invocation.
    """
    git_bin = _git_bin()
    if git_bin is None:
        return None
    try:
        r = subprocess.run(
            [
                git_bin,
                "-C",
                str(target),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    rel_paths = [p for p in r.stdout.decode("utf-8", "replace").split("\0") if p]
    return rel_paths or None


def _stage_files(target: Path, rel_paths: list[str], staging: Path) -> None:
    """Best-effort hardlink-or-copy of *rel_paths* (relative to *target*)
    into *staging*, preserving relative structure so graft's own per-file
    card paths still read as ordinary project-relative paths. Hardlink
    first (same-volume, near-zero cost — the common case since *staging*
    and *target* are usually on the same drive); falls back to a real copy
    on any failure (cross-device, no link permission, Windows without the
    privilege).

    Always unlinks *dst* first (a no-op OSError when it doesn't exist yet):
    *staging* is now PERSISTENT across builds (H1 follow-up, 2026-08-06 —
    see module docstring), so a previously-staged file's content must be
    refreshed, not left pointing at a stale inode. A hardlinked dst already
    shares the source's inode and would pick up an in-place edit for free,
    but an editor that saves via rename-over-write gives the file a NEW
    inode each time — re-linking on every build is what keeps that case in
    sync too. Never raises: a raced/unreadable file is skipped, not fatal —
    matches the fail-soft posture of the rest of this module."""
    for rel in rel_paths:
        src = target / rel
        dst = staging / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        try:
            dst.unlink()
        except OSError:
            pass
        try:
            os.link(src, dst)
        except OSError:
            try:
                shutil.copy2(src, dst)
            except OSError:
                continue


def _sync_staging(target: Path, rel_paths: list[str], staging: Path) -> None:
    """Bring *staging* in line with *rel_paths* exactly: remove any staged
    file no longer in the current non-ignored set (deleted, renamed, or
    newly `.gitignore`d since the last build — otherwise the persistent
    mirror only ever grows) then re-stage every file in *rel_paths*
    (`_stage_files` handles refreshing content for files already present).
    Best-effort: never raises."""
    try:
        staging.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    desired = set(rel_paths)
    try:
        for dirpath, _dirnames, filenames in os.walk(staging):
            for name in filenames:
                full = Path(dirpath) / name
                try:
                    rel = full.relative_to(staging).as_posix()
                except ValueError:
                    continue
                if rel not in desired:
                    try:
                        full.unlink()
                    except OSError:
                        pass
    except OSError:
        pass
    # Bottom-up so a now-empty child dir is gone before its parent is checked.
    try:
        for dirpath, _dirnames, _filenames in os.walk(staging, topdown=False):
            d = Path(dirpath)
            if d == staging:
                continue
            try:
                next(d.iterdir())
            except StopIteration:
                try:
                    d.rmdir()
                except OSError:
                    pass
            except OSError:
                pass
    except OSError:
        pass
    _stage_files(target, rel_paths, staging)


def _kill_orphan_tree(pid: int) -> None:
    """Best-effort recursive kill of a timed-out build's process tree.

    `graft` resolves to `graft.cmd` on Windows, a batch shim — plain
    `Popen.kill()` only kills the direct `cmd.exe` child; the real
    `node.exe` grandchild survives (M4 — this project has a documented prior
    incident of ~3170 orphaned node processes / 18 GB from exactly this
    class of leak). `taskkill /T` walks the live parent→child tree by PID
    before anything in it can exit — same pattern already used for Chrome
    (`browser_chrome.py`) and pane teardown (`pty_session.py`). POSIX needs
    no equivalent: `Popen.kill()` there already reaches the real child with
    no batch-shim layer in between.
    """
    if sys.platform != "win32":
        return
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
    except OSError:
        pass


def _run_build(graft_bin: str, target: Path) -> tuple[bool, float, str]:
    t0 = time.monotonic()
    store = graph_store_dir(target)
    try:
        store.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, time.monotonic() - t0, f"could not create graph store {store}: {e}"

    rel_paths = _git_nonignored_files(target)
    if rel_paths is None:
        return (
            False,
            time.monotonic() - t0,
            "not a git work-tree, git unavailable, or nothing non-ignored to index — skipped",
        )

    staging = staging_dir_for(target)
    _sync_staging(target, rel_paths, staging)
    try:
        proc = subprocess.Popen(
            [graft_bin, "--dir", str(store), "build", str(staging)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
        try:
            _out, err = proc.communicate(timeout=_BUILD_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            _kill_orphan_tree(proc.pid)
            proc.kill()
            proc.communicate()
            return False, time.monotonic() - t0, f"timed out after {_BUILD_TIMEOUT_S}s"
        if proc.returncode == 0:
            write_store_manifest(target)
            mark_build_complete(store)
            size_bytes, file_count = _dir_stats(store)
            stage_bytes, stage_files = _dir_stats(staging)
            _log.info(
                "graft_autobuild: store for %s is now %s (%d files) · staging mirror %s (%d files)",
                target,
                _fmt_bytes(size_bytes),
                file_count,
                _fmt_bytes(stage_bytes),
                stage_files,
            )
        return proc.returncode == 0, time.monotonic() - t0, (err or "").strip()[:500]
    except OSError as e:
        return False, time.monotonic() - t0, str(e)


def _build_one(target: Path) -> None:
    """Single-flight `graft build` for one directory. Safe to call from any
    thread; a second call for the same directory while one is already
    running is a no-op (the earlier build will pick up any newer changes on
    its own next trigger)."""
    graft_bin = _graft_cli()
    if graft_bin is None:
        return
    key = str(target)
    with _lock:
        if key in _building:
            return
        _building.add(key)
    try:
        with _build_semaphore:
            ok, elapsed, err = _run_build(graft_bin, target)
        if ok:
            _log.info("graft_autobuild: built %s in %.1fs", target, elapsed)
        else:
            _log.warning("graft_autobuild: build failed for %s (%.1fs): %s", target, elapsed, err)
    finally:
        with _lock:
            _building.discard(key)


def _spawn_build(target: Path) -> None:
    threading.Thread(
        target=_build_one,
        args=(target,),
        name=f"graft-build-{target.name}",
        daemon=True,
    ).start()


def build_all_projects_async() -> None:
    """Boot trigger: kick a background build for every distinct path across
    every project in projects.json. Best-effort — never raises, never blocks
    the caller. No-op if the kill switch is set or the graft CLI isn't
    installed (doctor handles nudging the user to install it)."""
    if _skip_env():
        _log.debug("graft_autobuild: skipped (TAKKUB_SKIP_GRAFT_BUILD set)")
        return
    if _graft_cli() is None:
        _log.debug("graft_autobuild: graft CLI not found on PATH; skipping")
        return
    try:
        projects = load_projects().get("projects") or {}
    except Exception:
        return
    dirs: dict[str, Path] = {}
    for proj in projects.values():
        if not isinstance(proj, dict):
            continue
        for d in _dirs_for_project(proj):
            dirs[str(d)] = d
    for d in dirs.values():
        _spawn_build(d)


def ensure_project_graph_async(project_name: str) -> None:
    """Tab-switch trigger: build only the paths of *project_name* that have
    no graph yet (an existing store is left alone here — boot/done triggers
    own refreshing it). Best-effort, never raises."""
    if _skip_env() or _graft_cli() is None:
        return
    try:
        proj = (load_projects().get("projects") or {}).get(project_name)
    except Exception:
        return
    if not isinstance(proj, dict):
        return
    for d in _dirs_for_project(proj):
        # `.graph` existing is NOT proof of a COMPLETE build (H2) — a
        # timeout or a Windows MAX_PATH failure partway through leaves a
        # partial `.graph` behind that looks identical to a finished one to
        # a bare `.is_dir()` check, and the pane then reads from a silently
        # truncated graph forever (this check never fires again). Use the
        # completion marker `_run_build` writes LAST, only on exit 0.
        if not has_completed_build(graph_store_dir(d)):
            _spawn_build(d)


def _debounced_fire(key: str, target: Path) -> None:
    with _lock:
        _debounce_timers.pop(key, None)
    _build_one(target)


def schedule_rebuild_after_done(cwd: str | None) -> None:
    """Post-done trigger: rebuild the graph for *cwd* (a teammate pane's own
    working directory) `_DEBOUNCE_S` seconds after the LAST call for that
    same directory — coalesces a burst of near-simultaneous `done()` calls
    (e.g. shards) into a single rebuild. Best-effort, never raises. Caller
    is responsible for not invoking this for a worktree-isolated pane (its
    cwd is a throwaway checkout, not a directory this module should ever
    touch) — see the module docstring."""
    if _skip_env() or _graft_cli() is None or not cwd:
        return
    try:
        target = Path(cwd).expanduser().resolve()
    except OSError:
        return
    if not target.is_dir():
        return
    key = str(target)
    with _lock:
        existing = _debounce_timers.get(key)
        if existing is not None:
            existing.cancel()
        timer = threading.Timer(_DEBOUNCE_S, _debounced_fire, args=(key, target))
        timer.daemon = True
        _debounce_timers[key] = timer
        timer.start()
