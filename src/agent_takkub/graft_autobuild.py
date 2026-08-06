"""Auto-run `graft build` so the graft MCP (shared_dev_tools.GRAFT_MCP) has a
graph to answer from without the user having to run the CLI by hand.

Without this, a project that never ran `graft build` gets graceful-but-empty
answers forever (shared_dev_tools.py's comment on GRAFT_MCP) — the MCP is
wired into every code-reading role's pane, but silently useless until someone
knows to run the command themselves.

Four triggers call into this module (each fire-and-forget, background only):
  * cockpit boot — `build_all_projects_async()` (orchestrator.py __init__)
  * project tab switch — `ensure_project_graph_async()`, only projects with
    no graph yet (main_window.py `_on_tab_switched`)
  * a teammate's `done()` — `schedule_rebuild_after_done()`, debounced so a
    burst of shards finishing together doesn't rebuild the same dir N times
    (orchestrator.py `done()`)
  * a LIVE pane's own idle-watchdog tick, mid-task — `resync_staging_only()`,
    throttled per directory (orchestrator.py `_check_idle_teammates`). The
    first three only ever touch a directory's staging mirror at build
    boundaries the pane itself never crosses mid-task, so without this a
    pane asking graft about a file it just edited got an answer as of its
    LAST `done()`. For a file the graph already knows about, this trigger
    does not run `graft build` at all — see its own docstring for why
    keeping the staging mirror in sync is enough. A file the graph has
    NEVER seen before (created since the last real build) is a known
    exception to that — see `_has_new_files`'s docstring — and escalates to
    a real `_spawn_build` instead.

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
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

from ._win_console import SUBPROCESS_NO_WINDOW
from .config import load_projects
from .graft_store import (
    graft_cli_path,
    graph_key,
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
# abs dir path actually running INSIDE `_build_semaphore` right now (M2,
# 2026-08-06): `_building` is added pre-semaphore so single-flight can reject
# a duplicate call before it even queues, which means at boot (46 project
# dirs, `_MAX_CONCURRENT_BUILDS = 3`) `_building` holds all 46 the instant
# every thread starts, while only 3 are actually doing anything — the chip
# reported "Building 46 now" when 43 of them were merely queued on the
# semaphore. `get_build_status()` reports THIS set, not `_building`.
_in_flight: set[str] = set()
_debounce_timers: dict[str, threading.Timer] = {}  # abs dir path -> pending Timer
# abs dir path -> (last build's failure reason, time.monotonic() it failed at)
# (M6 follow-up, 2026-08-06): the UI status-bar chip needs to tell "building"
# / "failed, gave up" / "never started" apart, which `has_completed_build`
# alone can't (it only answers completed-or-not). Populated/cleared by
# `_build_one`'s own success/failure branch — the single choke point every
# trigger (boot/tab-switch/done/live resync's full-build sibling) already
# funnels through. Entry is removed on that SAME dir's next SUCCESSFUL build,
# OR lazily by `get_build_status()` once `_FAILED_ENTRY_TTL_S` has elapsed
# (2026-08-06 follow-up: a dir deleted from projects.json after a failed
# build has no trigger left to ever re-attempt/clear it, so without a TTL it
# stuck in the chip forever — #146-style stale-state leak, same shape as the
# orphan-worktree bug, just for this dict instead of disk). A still-live,
# still-failing project keeps refreshing its own timestamp via tab-switch/
# done() re-attempts, so the TTL only ever prunes entries nothing has retried
# in a day.
_last_build_failed: dict[str, tuple[str, float]] = {}
# abs dir path -> (skip reason, time.monotonic() it was skipped at). A real
# user was shown "17/20 built. Failed: genimage, oracle, sales" for 3 dirs
# that were never git repos in the first place — `_run_build` used to return
# `ok=False` for that case too, so a perfectly normal "nothing to index here"
# state read identically to an actual build failure, and permanently drowned
# out any REAL failure in the same list (bug report, 2026-08-06). Kept as its
# own dict, not folded into `_last_build_failed`, precisely so the two can
# never collide again — see `_run_build`'s tri-state return for the split.
# Same TTL/lazy-prune shape as `_last_build_failed` for the same reason (a
# dir removed from projects.json has no trigger left to ever clear its own
# entry).
_last_build_skipped: dict[str, tuple[str, float]] = {}
_FAILED_ENTRY_TTL_S = 24 * 3600.0

# H1 residual (2026-08-06 re-review): `_stageable` only gates the SOURCE side
# (a submodule/broken-symlink/dir-symlink entry) — it cannot see a failure on
# the DESTINATION side, e.g. a staged rel path pushing `staging/<hash>/<hash>/
# <rel>` over Windows' MAX_PATH even though the shorter *target*/<rel> source
# stats fine. That class read as "new" on every `_has_new_files` check
# forever, re-escalating to a full `_spawn_build` roughly every 15s for the
# rest of the pane's life — same infinite-loop shape H1 was filed against,
# just reached through the mirror side instead of the source side.
#
# `_pending_escalation` records, per target, exactly which rel paths a
# resync-triggered `_spawn_build` was asked to resolve; `_build_one` checks
# that same set once ITS OWN `_run_build` (which re-syncs the mirror
# synchronously before returning, success or failure) has finished. A rel
# still absent from the mirror at that point is a structural failure, not a
# timing gap — recorded in `_unresolvable_rels` so `_new_stageable_rels`
# (and therefore `_has_new_files`) never escalates on it again, logged once
# instead of every cycle. A rel later renamed/moved is a DIFFERENT rel path
# (git reports the new name), so it gets a fresh chance on its own — nothing
# here is permanently stuck for a rel whose situation actually changes.
_pending_escalation: dict[str, frozenset[str]] = {}
_unresolvable_rels: dict[str, frozenset[str]] = {}


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

    Dedup key is `graph_key()` — the SAME hash `graph_store_dir` uses to
    pick a store directory — not a hand-rolled `os.name == "nt"` casing
    (M5, 2026-08-06). `graph_key`'s own `_normalize_for_key` case-folds on
    BOTH Windows and macOS (M1, 2026-08-05 audit); the old expression here
    only folded on Windows, so two case-variant paths on macOS (or two
    `paths` entries differing only in case, on either OS) survived dedup as
    two distinct dirs, each spawning its own `_build_one` thread with an
    UNFOLDED single-flight key — while `graph_store_dir` folded both to the
    SAME store, letting two `graft build` calls race into it concurrently.
    Sharing the exact key `graph_store_dir` derives from means this can
    never drift from the store key again.
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
        seen[graph_key(resolved)] = resolved
    return list(seen.values())


def _git_bin() -> str | None:
    return shutil.which("git")


def _git_nonignored_files(target: Path) -> list[str] | None:
    """`git ls-files` (tracked + untracked-but-not-`.gitignore`d) under
    *target*, as paths relative to *target*. `None` means "nothing safe to
    index" — *target* not a git work-tree, the listing call itself failed,
    or a git work-tree with zero non-ignored files (a real project always
    has at least one) — which is the caller's signal to skip the build
    entirely rather than hand graft the raw directory (H1a, L5).

    Git-binary-missing is a DIFFERENT case, checked separately by the caller
    (`_run_build`, before this is ever called): a repo with nothing to index
    is ordinary and expected (an image/docs folder in projects.json — see
    `get_build_status()`'s `skipped` list), but no `git` on PATH at all is a
    real, actionable problem — bundling the two under one `None` used to
    report both identically as "skipped", hiding the git-missing case from
    the user entirely (2026-08-06 bug report). This function still returns
    plain `None` for the "no git" case too if called directly (defensive —
    every caller besides `_run_build` is a test), it just never reaches that
    branch through `_run_build`'s normal path.

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


def _stat_or_none(path: Path) -> os.stat_result | None:
    try:
        return path.stat()
    except OSError:
        return None


def _stageable(path: Path) -> bool:
    """True when *path* is a plain file `_stage_files` could plausibly
    hardlink/copy. A git submodule (a single `ls-files` entry that is a
    DIRECTORY on disk), a broken symlink, a directory symlink, and a path
    Windows refuses to `stat` (MAX_PATH) all read `False` here — every one
    of them made `os.link`/`shutil.copy2` raise and silently `continue` in
    `_stage_files` while still counting as "missing from the mirror" to
    `_has_new_files`, which is what fed the H1 (2026-08-06) infinite
    rebuild loop: an entry that can never be staged looked identical,
    forever, to one merely not staged YET. Re-checked fresh on every call
    (no cached skip-set) so a submodule later replaced with a real file is
    picked up on its own next cycle instead of needing a state reset."""
    st = _stat_or_none(path)
    return st is not None and stat.S_ISREG(st.st_mode)


def _stage_files(target: Path, rel_paths: list[str], staging: Path) -> None:
    """Best-effort hardlink-or-copy of *rel_paths* (relative to *target*)
    into *staging*, preserving relative structure so graft's own per-file
    card paths still read as ordinary project-relative paths. Hardlink
    first (same-volume, near-zero cost — the common case since *staging*
    and *target* are usually on the same drive); falls back to a real copy
    on any failure (cross-device, no link permission, Windows without the
    privilege).

    Skips anything `_stageable` rejects (submodules, broken/dir symlinks,
    MAX_PATH) outright rather than paying for a doomed `os.link` + `copy2`
    attempt on every cycle.

    A destination already hardlinked to the CURRENT source (same file) is
    left alone: `_sync_staging` runs on a 15s timer for every live pane
    (M1, 2026-08-06), and re-`unlink`+`link`ing hundreds of untouched files
    every cycle was measured at ~264ms of pure disk churn for zero effect —
    an already-identical dst would pick up an in-place edit for free
    regardless (the whole reason relinking is needed AT ALL is a
    rename-over-write save giving src a NEW inode). Deliberately NOT a
    `(mtime, size)` fallback for the `copy2` case too: measured on this
    machine, two files written back-to-back can land on the exact same
    `st_mtime_ns` (Windows batches nearby writes onto one timestamp tick),
    so that heuristic can silently skip syncing a real content change —
    unlike `os.path.samestat`, which is always exact, never a probabilistic
    match. The `copy2` fallback path (cross-device/no-link-permission) pays
    the full re-copy every cycle same as before; only the common
    same-volume hardlink case gets the speedup.

    `os.path.samestat` (st_dev AND st_ino), not a bare `st_ino` comparison
    (R-1, 2026-08-06 re-review): inode/file-index numbers are allocated
    PER VOLUME, so a bare `st_ino` match is only proof of identity when src
    and dst are known to share a device. Cross-device staging is not
    hypothetical here — it is the documented reason `AGENT_TAKKUB_HOME`
    exists (`graft_store.py`'s module docstring: moving cockpit data off a
    small/boot drive), and on that path `_stage_files` always takes the
    `copy2` fallback below, landing dst in an INDEPENDENT inode space from
    src. A bare `st_ino` coincidence there would skip re-copying a real
    content change forever, silently — proven on this machine (two real
    volumes) by forcing the collision: `os.path.samestat` correctly said
    "different file" while `st_ino` alone agreed by chance.

    Always unlinks *dst* first before actually relinking (a no-op OSError
    when it doesn't exist yet): *staging* is now PERSISTENT across builds
    (H1 follow-up, 2026-08-06 — see module docstring), so a previously
    -staged file's content must be refreshed, not left pointing at a stale
    inode. An editor that saves via rename-over-write gives the file a NEW
    inode each time — re-linking is what keeps that case in sync. Never
    raises: a raced/unreadable file is skipped, not fatal — matches the
    fail-soft posture of the rest of this module."""
    for rel in rel_paths:
        src = target / rel
        src_stat = _stat_or_none(src)
        if src_stat is None or not stat.S_ISREG(src_stat.st_mode):
            continue
        dst = staging / rel
        dst_stat = _stat_or_none(dst)
        if dst_stat is not None and os.path.samestat(src_stat, dst_stat):
            continue
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


def _run_build(graft_bin: str, target: Path) -> tuple[bool | None, float, str]:
    """Returns `(status, elapsed_s, message)`. *status* is tri-state, NOT a
    plain success/fail bool (2026-08-06 fix — see `_last_build_skipped`'s
    module-level comment for the bug this closes):

    * `True`  — build actually ran and succeeded.
    * `False` — a REAL failure: unwritable store, no `git` on PATH, the
      `graft build` subprocess itself errored or timed out. Surfaced via
      `get_build_status()`'s `failed` list — this is the list a user should
      act on.
    * `None`  — not applicable: *target* is not a git work-tree, or is one
      with nothing non-ignored to index. Ordinary and expected for a
      non-code folder in projects.json (L5) — surfaced separately via
      `get_build_status()`'s `skipped` list so it can never again drown out
      a real failure in the same bucket.
    """
    t0 = time.monotonic()
    store = graph_store_dir(target)
    try:
        store.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, time.monotonic() - t0, f"could not create graph store {store}: {e}"

    if _git_bin() is None:
        return (
            False,
            time.monotonic() - t0,
            "git not found on PATH — install git to enable code-intelligence indexing",
        )

    rel_paths = _git_nonignored_files(target)
    if rel_paths is None:
        return (
            None,
            time.monotonic() - t0,
            "not a git work-tree, or nothing non-ignored to index — not applicable, skipped",
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
            with _lock:
                _in_flight.add(key)
            try:
                ok, elapsed, err = _run_build(graft_bin, target)
            finally:
                with _lock:
                    _in_flight.discard(key)
        with _lock:
            if ok is True:
                _last_build_failed.pop(key, None)
                _last_build_skipped.pop(key, None)
            elif ok is None:
                _last_build_skipped[key] = (err or "not applicable", time.monotonic())
                _last_build_failed.pop(key, None)
            else:
                _last_build_failed[key] = (err or "build failed", time.monotonic())
                _last_build_skipped.pop(key, None)
            pending = _pending_escalation.pop(key, None)
        if pending:
            # `_run_build` re-syncs the mirror synchronously before it
            # returns (success OR failure), so this is a definitive
            # convergence check, not a guess (H1 residual, 2026-08-06
            # re-review). Only walks `staging` when there was something
            # pending to check — zero extra cost for the boot/tab-switch/
            # done triggers, which never populate `_pending_escalation`.
            still_missing = pending - _staging_relpaths(staging_dir_for(target))
            if still_missing:
                with _lock:
                    _unresolvable_rels[key] = (
                        _unresolvable_rels.get(key, frozenset()) | still_missing
                    )
                _log.warning(
                    "graft_autobuild: %s: giving up on staging %d file(s) that never "
                    "land in the mirror (destination-side failure, e.g. MAX_PATH) — "
                    "will not re-escalate for them: %s",
                    target,
                    len(still_missing),
                    sorted(still_missing)[:5],
                )
        if ok is True:
            _log.info("graft_autobuild: built %s in %.1fs", target, elapsed)
        elif ok is None:
            _log.debug(
                "graft_autobuild: %s not applicable, skipped (%.1fs): %s", target, elapsed, err
            )
        else:
            _log.warning("graft_autobuild: build failed for %s (%.1fs): %s", target, elapsed, err)
    finally:
        with _lock:
            _building.discard(key)


def get_build_status() -> dict:
    """Thread-safe snapshot for the UI status-bar chip: how many builds are
    ACTUALLY RUNNING inside `_build_semaphore` right now (`_in_flight`, not
    the broader single-flight `_building` set — see `_in_flight`'s
    module-level comment for why the two diverge at boot, M2 2026-08-06),
    which directories' LAST build attempt actually FAILED, and which were
    SKIPPED as not applicable (not a git work-tree — an ordinary, expected
    state, not an error the user needs to act on).

    `has_completed_build` alone can't tell a chip "building" / "failed, gave
    up" / "never started" apart — it only ever answers completed-or-not, so a
    dir that failed every attempt looks identical, forever, to one that
    simply hasn't been asked to build yet (M6/H3 follow-up: a build failure
    with zero UI surface). Deliberately cheap — dict/set snapshots under the
    same lock every other build-state mutation already uses, never a
    filesystem walk — so polling this on a short interval (e.g. every 2s from
    the UI thread) costs nothing.

    `failed` and `skipped` are two SEPARATE lists, not one bucket split by a
    flag (2026-08-06 bug report: a user's non-git projects.json dirs — 3 on
    prod, 15 on dev — were reported to them as "failed" every time the
    cockpit opened, drowning out any dir that actually failed to build in
    the same list, forever). See `_run_build`'s tri-state return and
    `_last_build_skipped`'s module-level comment for the full mechanism.

    Both lists persist across a dir's later NEW attempts and are removed
    once that same dir's build actually succeeds, or lazily pruned here
    after `_FAILED_ENTRY_TTL_S` — see `_last_build_failed`'s module-level
    comment for why a TTL and not a projects.json cross-check (the latter
    would add a JSON read + a resolve()/is_dir() stat per path on every 2s
    poll; this adds one time.monotonic() call and reuses the same dict
    iteration `sorted()` already paid for below).
    """
    with _lock:
        cutoff = time.monotonic() - _FAILED_ENTRY_TTL_S
        expired_failed = [k for k, (_reason, at) in _last_build_failed.items() if at < cutoff]
        for k in expired_failed:
            _last_build_failed.pop(k, None)
        expired_skipped = [k for k, (_reason, at) in _last_build_skipped.items() if at < cutoff]
        for k in expired_skipped:
            _last_build_skipped.pop(k, None)
        return {
            "building": len(_in_flight),
            "failed": sorted(_last_build_failed),
            "skipped": sorted(_last_build_skipped),
        }


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


def _staging_relpaths(staging: Path) -> set[str]:
    """Every file currently present in *staging*, as POSIX-style paths
    relative to *staging*. A missing/unreadable *staging* reads as empty
    (`os.walk` on a nonexistent dir silently yields nothing) — same
    fail-soft posture as the rest of this module."""
    out: set[str] = set()
    for dirpath, _dirnames, filenames in os.walk(staging):
        for name in filenames:
            full = Path(dirpath) / name
            try:
                out.add(full.relative_to(staging).as_posix())
            except ValueError:
                continue
    return out


def _new_stageable_rels(target: Path, rel_paths: list[str], staging: Path) -> set[str]:
    """The subset of *rel_paths* that are missing from *staging*, pass the
    SOURCE-side `_stageable` gate (H1, 2026-08-06 — rules out a submodule,
    broken symlink, dir symlink, or a source Windows refuses to `stat`), and
    are NOT already known to fail on the DESTINATION side no matter how many
    times staging is retried (`_unresolvable_rels` — H1 residual, 2026-08-06
    re-review; see that dict's module-level comment for the MAX_PATH class
    `_stageable` structurally cannot see, since it only ever looks at the
    source path). Shared by `_has_new_files` (bool view) and
    `resync_staging_only` (needs the actual set to hand to `_build_one` for
    post-build convergence checking)."""
    staged = _staging_relpaths(staging)
    with _lock:
        given_up = _unresolvable_rels.get(str(target), frozenset())
    return {
        rel
        for rel in rel_paths
        if rel not in staged and rel not in given_up and _stageable(target / rel)
    }


def _has_new_files(target: Path, rel_paths: list[str], staging: Path) -> bool:
    """True when *rel_paths* (git's current non-ignored set) contains a
    STAGEABLE file *staging* doesn't have yet — i.e. created since the
    mirror was last synced.

    Why this needs its own escalation instead of just letting
    `_sync_staging` copy the new file in: verified against the real CLI
    (2026-08-06) that graft's own freshness gate (`ensureFreshGraph` /
    `probeDrift` in its shipped `graph/refresh.js` + `graph/fingerprint.js`)
    reliably catches a MODIFIED file — one it already has a fingerprint
    entry for — through a plain mirror sync, exactly as this module's
    docstring on trigger #4 describes. A file with NO prior fingerprint
    entry at all did not: syncing it into the staging mirror produced no
    `[graft] refreshed the graph ...` note and an empty `ask` answer, and
    only a real `graft build` (this module's own, not graft's query-time
    incremental one) made it visible. Root cause not chased into graft's
    own source — treating "new to the mirror" as its own case here is a
    contained, low-risk fix that doesn't depend on understanding why
    upstream's added-file path behaves differently from its changed-file
    path.

    Gated on `_stageable(target / rel)` (H1, 2026-08-06): a git submodule,
    broken symlink, dir symlink, or MAX_PATH-too-long entry is reported by
    `git ls-files` but can never actually land in *staging* — without the
    gate, it read as "new" on EVERY call forever, escalating to a full
    `_spawn_build` roughly every 15s for the rest of the pane's life
    (verified with a real `git submodule add` repo). That gate only sees
    SOURCE-side failures, though — see `_new_stageable_rels`'s doc for the
    DESTINATION-side class (`_unresolvable_rels`) it delegates to as well.
    """
    return bool(_new_stageable_rels(target, rel_paths, staging))


# How often a LIVE pane's staging mirror is allowed to resync while the pane
# is mid-task (no `done()` yet) — closes the mid-edit staleness gap the 3
# triggers above (boot / tab-switch / done) leave open: without this, a pane
# asking graft about a file it just edited gets an answer as of its LAST
# `done()` (or nothing, on a pane's very first task), because those are the
# only events that ever touch a directory's staging mirror or graph. This
# does NOT run `graft build` — it only keeps the staging mirror in sync;
# graft's OWN freshness gate (`ensureFreshGraph`, see
# `graft_store.staging_dir_for`'s docstring) already re-probes its `dir`
# argument on every tool call inside the pane's own long-lived `graft mcp`
# process and incrementally refreshes the graph the instant it sees the
# mirror changed — so a live pane self-heals within one polling interval
# instead of only at its own `done()`. Deliberately much lighter than a full
# build: `_sync_staging` is a `git ls-files` + hardlink-relink of the
# (usually small) delta, never a `graft build` subprocess.
_LIVE_RESYNC_MIN_INTERVAL_S = 15.0
_live_resync_lock = threading.Lock()
_last_live_resync: dict[str, float] = {}


def resync_staging_only(cwd: str | None) -> None:
    """Best-effort, throttled staging-mirror refresh for a LIVE pane's own
    *cwd*. See `_LIVE_RESYNC_MIN_INTERVAL_S` for why this exists and why it
    is deliberately not a `graft build` call for the common case (a
    modified file). Safe to call from any thread on any cadence — a no-op
    inside the per-directory throttle window, while a full build for the
    same directory is already in-flight (that build's own `_sync_staging`
    call already covers this pass), or when the kill switch is set / *cwd*
    isn't a real directory.

    Exception: a file *brand new* to the staging mirror escalates to a real
    `_spawn_build` instead of a plain sync — see `_has_new_files`'s
    docstring for why a lightweight sync alone isn't enough for that case.
    """
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
        if key in _building:
            return  # in-flight full build already re-syncs this same mirror
    now = time.monotonic()
    with _live_resync_lock:
        last = _last_live_resync.get(key, 0.0)
        if now - last < _LIVE_RESYNC_MIN_INTERVAL_S:
            return
        _last_live_resync[key] = now

    def _do() -> None:
        rel_paths = _git_nonignored_files(target)
        if rel_paths is None:
            return
        staging = staging_dir_for(target)
        new_rels = _new_stageable_rels(target, rel_paths, staging)
        if new_rels:
            # A file the graph has never seen needs a real build, not just a
            # mirror sync (`_has_new_files`). `_spawn_build` -> `_build_one`
            # is single-flight and semaphore-bounded like every other build
            # trigger, and `_run_build` re-syncs the mirror itself, so this
            # subsumes the plain `_sync_staging` call below. Recording *what*
            # we escalated for BEFORE spawning lets `_build_one` check, once
            # that same build has actually finished, whether it converged —
            # see `_pending_escalation`'s module-level comment (H1 residual,
            # 2026-08-06 re-review).
            with _lock:
                _pending_escalation[key] = frozenset(new_rels)
            _spawn_build(target)
            return
        _sync_staging(target, rel_paths, staging)

    threading.Thread(target=_do, name=f"graft-live-resync-{target.name}", daemon=True).start()


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
