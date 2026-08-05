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
import threading
import time
from pathlib import Path

from ._win_console import SUBPROCESS_NO_WINDOW
from .config import load_projects
from .graft_store import graph_store_dir, write_store_manifest

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
    return shutil.which("graft.cmd") or shutil.which("graft")


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


def _run_build(graft_bin: str, target: Path) -> tuple[bool, float, str]:
    t0 = time.monotonic()
    store = graph_store_dir(target)
    try:
        store.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, time.monotonic() - t0, f"could not create graph store {store}: {e}"
    try:
        r = subprocess.run(
            [graft_bin, "--dir", str(store), "build", str(target)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=_BUILD_TIMEOUT_S,
            text=True,
            check=False,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
        if r.returncode == 0:
            write_store_manifest(target)
        return r.returncode == 0, time.monotonic() - t0, (r.stderr or "").strip()[:500]
    except subprocess.TimeoutExpired:
        return False, time.monotonic() - t0, f"timed out after {_BUILD_TIMEOUT_S}s"
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
        # `.graph` (not `graft/`) is the real on-disk marker a `graft --dir
        # <store> build` writes into the store root — confirmed empirically
        # 2026-08-05 against the real CLI (`@nanonets/graft@0.8.2`): a build
        # produces `.graph/`, `.cache/`, `INDEX.md`, and one `<file>.md`
        # per source file directly under *store*, no nested `graft/` folder.
        if not (graph_store_dir(d) / ".graph").is_dir():
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
