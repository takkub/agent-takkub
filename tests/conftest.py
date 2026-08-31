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

import contextlib
import importlib
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
import venv
import weakref
from pathlib import Path

# Captured now, before any test module runs, so `_snapshot_settings_home_files`
# below never depends on `os.scandir`/`os.stat` names (or `Path.stat`) that a
# test may have monkeypatched by the time its teardown runs — e.g.
# test_graft_chip.py's `test_main_thread_refresh_reads_cache_without_filesystem_calls`
# patches `Path.stat`/`Path.resolve` to raise AssertionError on any call, and
# that patch is still live during this fixture's post-`yield` teardown check.

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

# Captured at import time, before any test can monkeypatch os.* — the
# settings-home guard's snapshot uses only these (see _snapshot_settings_home_files).
_ORIG_OS_SCANDIR = os.scandir
_ORIG_OS_STAT = os.stat

_REPO_ROOT_FOR_WHEEL_BUILD = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def _cross_process_wheel_lock(lock_path: Path, *, timeout: float = 240.0, poll: float = 0.2):
    """A cross-process mutual-exclusion lock via exclusive file creation.

    ``threading.Lock`` is useless here — under pytest-xdist each worker is a
    *separate process*, not a thread of this one. ``O_CREAT | O_EXCL`` is
    portable (Windows + macOS/Linux) and atomic at the OS level, unlike a
    plain existence check + create.
    """
    deadline = time.monotonic() + timeout
    fd = None
    while fd is None:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, PermissionError):
            # PermissionError: Windows reports EACCES (not EEXIST) when the
            # previous holder's `unlink` is still pending-delete at the
            # moment we try to create — seen as a flaky
            # test_wheel_build_lock failure on windows-latest CI
            # (2026-08-26, 1.6.6 run). Same meaning as "still held": wait.
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"timed out after {timeout}s waiting for lock {lock_path} "
                    "(held by another pytest-xdist worker?)"
                ) from None
            time.sleep(poll)
    try:
        yield
    finally:
        os.close(fd)
        lock_path.unlink(missing_ok=True)


def _latest_wheel_source_mtime() -> float:
    """Newest mtime across everything `python -m build` reads from, so a
    cached wheel can be trusted (or correctly invalidated) without rebuilding
    just to find out."""
    latest = 0.0
    for tracked in (
        _REPO_ROOT_FOR_WHEEL_BUILD / "pyproject.toml",
        _REPO_ROOT_FOR_WHEEL_BUILD / "MANIFEST.in",
    ):
        if tracked.exists():
            latest = max(latest, tracked.stat().st_mtime)
    for path in (_REPO_ROOT_FOR_WHEEL_BUILD / "src").rglob("*"):
        if path.is_file():
            latest = max(latest, path.stat().st_mtime)
    return latest


def _build_or_reuse_wheel(wheel_cache_dir: Path) -> Path:
    """Build the current source into *wheel_cache_dir*, or reuse whatever's
    already cached there if it's not older than the source tree.

    Caller must hold ``_cross_process_wheel_lock`` around this — it mutates a
    directory (and `<repo_root>/build/`, `python -m build`'s own scratch dir)
    shared by every pytest-xdist worker in the run.
    """
    cached = sorted(wheel_cache_dir.glob("*.whl"))
    if cached and cached[0].stat().st_mtime >= _latest_wheel_source_mtime():
        return cached[0]
    for stale in cached:
        stale.unlink()
    # `python -m build` reuses <repo_root>/build/ as a staging dir across
    # runs (regardless of --outdir, which only controls the final wheel's
    # destination) — a tree left over from a prior source layout (e.g. a
    # since-renamed _assets file) makes setuptools reference a path that no
    # longer exists and fail with a spurious WinError 2 / errno 2, even
    # though the current source is fine (see build_wheel() in release.py for
    # the same fix). Safe to rmtree unconditionally here: the caller's lock
    # guarantees we're the only worker touching repo_root/build/ right now —
    # this fixture is the ONLY place in the suite that runs a real
    # `python -m build` against this checkout (#388: two independent,
    # unlocked copies of this fixture — one here, one in
    # test_installed_cli_bin_integration.py — used to race on the same
    # repo_root/build/ dir under pytest-xdist, producing a spurious
    # "[Errno 2] No such file or directory: build/bdist.*/wheel" on CI).
    shutil.rmtree(_REPO_ROOT_FOR_WHEEL_BUILD / "build", ignore_errors=True)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--outdir",
            str(wheel_cache_dir),
            str(_REPO_ROOT_FOR_WHEEL_BUILD),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"wheel build failed:\n{result.stdout}\n{result.stderr}"
    wheels = list(wheel_cache_dir.glob("*.whl"))
    assert wheels, "no wheel produced"
    return wheels[0]


@pytest.fixture(scope="session")
def installed_venv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A throwaway venv with the current source installed as a wheel
    (--no-deps: only console-script placement + config/lead_context/pane_env/
    cli's stdlib-only import chain matter to the tests that use this).

    Shared by every test module that needs an installed-mode venv
    (test_installed_mode_gate.py, test_installed_cli_bin_integration.py) —
    a SINGLE fixture defined once here, so there is exactly one
    `python -m build` per pytest run, guarded by one lock (#388). Do not
    redefine a local `installed_venv` fixture in a test module; depend on
    this one instead.

    Session-scoped, but under pytest-xdist ``scope="session"`` only means
    "once per worker *process*" — and `--dist loadscope`'s scheduling unit
    is the enclosing test *class*, not the module, so
    test_installed_mode_gate.py's several classes can land on several
    different workers. Without sharing the finished venv itself (not just
    the wheel), every one of those workers would separately pay a real
    `venv.create(with_pip=True)` + `pip install` (~30-45s observed) even
    though only one worker's wheel build actually needs to be exclusive —
    the venv is read-only from every test's point of view (console-script
    path checks, subprocess invocations of the installed CLI; nothing here
    installs into or mutates it further), so it is safe to build once and
    have every worker reuse the same directory. Both the wheel-cache dir
    and the venv dir are anchored at ``getbasetemp().parent`` — the root
    all workers in this run share, one level up from each worker's own
    ``.../popen-gwN`` — and both the build and the venv-creation happen
    inside the SAME lock, so exactly one worker per pytest run does the
    ~30-45s of real work; every other worker's fixture call is a lock
    acquire + an existence check.
    """
    shared_root = tmp_path_factory.getbasetemp().parent
    wheel_cache_dir = shared_root / "installed-mode-wheel-cache"
    wheel_cache_dir.mkdir(exist_ok=True)
    venv_dir = shared_root / "installed-mode-shared-venv"
    lock_path = shared_root / "installed-mode-wheel-build.lock"
    vpy = venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")

    with _cross_process_wheel_lock(lock_path):
        wheel = _build_or_reuse_wheel(wheel_cache_dir)

        # A stamp file records which wheel (by mtime) the shared venv was
        # last built from — if a source change produced a fresher wheel
        # since, the venv holds stale console-script/package state and must
        # be rebuilt, not silently reused.
        stamp_path = venv_dir / ".built-from-wheel-mtime"
        wheel_mtime = str(wheel.stat().st_mtime)
        stale = not vpy.exists() or (
            not stamp_path.exists() or stamp_path.read_text(encoding="utf-8") != wheel_mtime
        )
        if stale:
            shutil.rmtree(venv_dir, ignore_errors=True)
            venv.create(venv_dir, with_pip=True)
            assert vpy.exists(), f"venv python missing at {vpy}"

            # A caller's own PYTHONPATH (e.g. a worktree pane's documented
            # workaround for this file's own
            # `_assert_agent_takkub_matches_this_checkout` guard below) makes
            # `agent_takkub` importable straight off source, with no
            # dist-info — pip then treats the target as already-satisfied
            # and skips writing its console-script entry points, producing a
            # venv with NO `takkub`/`agent-takkub` scripts even though `pip
            # install` itself reports success (reproduced directly:
            # identical wheel, only the installing process's PYTHONPATH
            # differed). Strip it so this fixture always yields a real,
            # fully-installed venv regardless of the parent process's own
            # PYTHONPATH.
            install_env = dict(os.environ)
            install_env.pop("PYTHONPATH", None)
            result = subprocess.run(
                [str(vpy), "-m", "pip", "install", "--no-deps", "--quiet", str(wheel)],
                capture_output=True,
                text=True,
                timeout=180,
                env=install_env,
            )
            assert result.returncode == 0, f"pip install failed:\n{result.stdout}\n{result.stderr}"
            stamp_path.write_text(wheel_mtime, encoding="utf-8")

    return venv_dir


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


def _snapshot_settings_home_files(base: Path) -> dict | None:
    """Top-level files directly under a `SETTINGS_HOME`-shaped dir, keyed by
    name -> (mtime_ns, size). Non-recursive by design: cheap enough to run on
    every test, and every module-level `_PATH`/`_FILE` constant this fixture
    isolates below writes directly under SETTINGS_HOME, not into a
    subdirectory (the one exception, CUSTOM_AGENTS_DIR/*.md, isn't covered —
    acceptable, this exists to catch the NEXT unisolated module-level
    constant, not to replace the isolation below).

    Uses only the `_ORIG_OS_SCANDIR`/`_ORIG_OS_STAT` references captured at
    import time — never `Path.is_file()`/`Path.stat()` or the live
    `os.scandir`/`os.stat` names, both of which a test under measurement may
    have monkeypatched (e.g. to trap and fail on any filesystem call). Any
    unexpected exception (not just OSError) reaching here means the originals
    themselves got tangled up in that trap somehow; return None ("unmeasurable
    this round") rather than raise, so the caller skips the guard instead of
    failing an unrelated test."""
    try:
        try:
            base_stat = _ORIG_OS_STAT(base)
        except OSError:
            return {}
        if not stat.S_ISDIR(base_stat.st_mode):
            return {}
        snap: dict = {}
        with _ORIG_OS_SCANDIR(base) as it:
            for entry in it:
                try:
                    st = _ORIG_OS_STAT(entry.path)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    snap[entry.name] = (st.st_mtime_ns, st.st_size)
        return snap
    except OSError:
        return {}
    except BaseException:
        return None


class _AbundantVM:
    """Fake `psutil.virtual_memory()` result with generous headroom, used by
    `_isolate_runtime` below to keep #364 lever 3's RAM-derived
    `max_panes_global` default (`core.scheduling.facade.
    _ram_derived_max_panes_global`) from varying by the RAM of the machine
    running the suite."""

    total = 64 * 1024**3
    available = 56 * 1024**3


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
    # config._effective_port_file_for_app()'s #354 guard trusts a foreign-
    # looking TAKKUB_PORT_FILE unless TAKKUB_ROLE (the pane marker
    # pane_env.py stamps into every spawned pane) is also present. This test
    # suite is routinely itself RUN INSIDE a spawned pane (a backend/qa/etc.
    # teammate), so TAKKUB_ROLE is ambient in the real process env here —
    # without clearing it, every test that doesn't explicitly delenv it would
    # spuriously look like a leaked cross-instance override. Tests that need
    # to model the actual leak scenario re-set it themselves.
    monkeypatch.delenv("TAKKUB_ROLE", raising=False)
    cfg = _maybe_module("agent_takkub.config", force=True)
    # #362 guard setup: capture the REAL SETTINGS_HOME (before anything below
    # patches `cfg.SETTINGS_HOME` to the isolated tmp dir) and snapshot its
    # top-level files now, so teardown can detect whether this test wrote to
    # it despite every isolation patch below. See the check after `yield`.
    _real_settings_home = getattr(cfg, "SETTINGS_HOME", None) if cfg is not None else None
    _settings_home_before = (
        _snapshot_settings_home_files(_real_settings_home)
        if _real_settings_home is not None
        else {}
    )
    # `DATA_HOME` defaults to `REPO_ROOT` verbatim in a dev checkout
    # (config.py's `_resolve_data_home`) — exactly this repo's own checkout
    # when the suite runs from source — so any CALL-TIME reader of
    # `config.DATA_HOME` (unlike a module-level constant bound once at
    # import, e.g. PROJECTS_JSON/RUNTIME_DIR/ASSETS_ROOT/CLI_BIN_DIR — those
    # don't move no matter what we patch here) that a test exercises without
    # its own isolation fixture writes into the REAL repo checkout. Confirmed
    # live: `trace_store._trace_path()` (`config.DATA_HOME / "context" /
    # "last_context_trace.json"`) leaked into this checkout's real `context/`
    # dir from test_orchestrator_assign_resource_gate.py's and
    # test_orchestrator_shard.py's real (unmocked) `orch.assign()` calls,
    # caught by `_guard_repo_root_no_leaked_context_dir` below.
    #
    # Redirect BOTH `DATA_HOME` and `REPO_ROOT` to the SAME isolated tmp dir
    # — not just `DATA_HOME` — so `DATA_HOME == REPO_ROOT` stays True by
    # default: every "dev checkout" branch in config.py (`provider_home_env`,
    # `instance_identity_label`, `instance_window_title`, ...) keeps taking
    # the same branch it always has, just pointed at an isolated location
    # instead of the real repo. Patching `DATA_HOME` alone was tried before
    # and reverted (see the `storage_layout_v2` comment below — proven: broke
    # 3 test_spawn_v2_account_env.py tests that rely on the default
    # `DATA_HOME == REPO_ROOT` identity with no fixture of their own, e.g.
    # `provider_home_env` must stay `{}`); pairing `REPO_ROOT` alongside it
    # preserves that identity instead of breaking it. A test with its own
    # `monkeypatch.setattr(config, "REPO_ROOT"/"DATA_HOME", ...)` — the
    # codebase's existing convention for simulating a specific instance, see
    # test_config.py — still wins: autouse fixtures run first, so the test's
    # own call simply overwrites this default afterward.
    if cfg is not None and hasattr(cfg, "REPO_ROOT") and hasattr(cfg, "DATA_HOME"):
        _isolated_repo_root = tmp_path / "_isolated_takkub" / "repo-root"
        monkeypatch.setattr(cfg, "REPO_ROOT", _isolated_repo_root, raising=False)
        monkeypatch.setattr(cfg, "DATA_HOME", _isolated_repo_root, raising=False)
    if cfg is not None and hasattr(cfg, "PORT_FILE"):
        monkeypatch.setattr(cfg, "PORT_FILE", runtime / "port", raising=False)
    # epic #309 Wave C: core.routing.Router.effective_model_for's own
    # migration-detection check (`storage_layout_v2().models` existence,
    # see router.py) reads `storage_layout_v2()`'s DEFAULT target, which
    # resolves through `config.DATA_HOME` — on a dev checkout that's
    # REPO_ROOT, and this checkout has a REAL migrated `v2/models/
    # registry.json`/`aliases.json` on disk (gitignored runtime state from
    # running this very cockpit locally). Unpatched, every test that spawns
    # a pane through core.routing (flag ON by default) would silently see
    # "already migrated" and read THAT file's real pins instead of
    # whatever role_models/provider_models state the test itself set up —
    # same "real machine file leaks into a test" lesson as core-v2-
    # settings/role-models below, confirmed live (test_spawn_default_model_
    # env.py resolved "sonnet"/"opus" from this checkout's real v2/models/
    # aliases.json instead of the test's own pins).
    #
    # Deliberately narrower than redirecting `config.DATA_HOME` itself:
    # several `config.py` functions (`provider_home_env`, `instance_display_
    # version`, ...) branch on `DATA_HOME == REPO_ROOT` to detect "dev
    # checkout" — globally repointing DATA_HOME breaks that comparison for
    # every test relying on it (proven: 3 test_spawn_v2_account_env.py
    # failures the first time this was tried). Patching only
    # `storage_layout_v2`'s own default keeps `config.DATA_HOME` completely
    # untouched; the module-attribute swap only reaches `router.py`'s late
    # (per-call) `from ... import storage_layout_v2` — every OTHER caller
    # (`core.storage.paths`, `core.model_catalog.legacy`, the migration
    # engine/steps) imports it at module top level and is unaffected,
    # exactly the same "late import is patchable, top-level import isn't"
    # split this file already relies on elsewhere.
    sl = _maybe_module("agent_takkub.core.storage.layout", force=True)
    if sl is not None and hasattr(sl, "storage_layout_v2"):
        isolated_data_home = tmp_path / "_isolated_takkub" / "data-home"
        _orig_storage_layout_v2 = sl.storage_layout_v2
        monkeypatch.setattr(
            sl,
            "storage_layout_v2",
            lambda data_home=None: _orig_storage_layout_v2(
                data_home if data_home is not None else isolated_data_home
            ),
            raising=False,
        )

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
        # #343: _provider_available()'s CLI-installed TTL cache is module-global
        # and outlives any single test's monkeypatched discovery result — reset
        # it here (not just in provider_config's own test fixture) so every
        # test file that touches _provider_available (e.g. settings_window's
        # badge sync) starts from a clean cache, same lesson as core-v2-settings
        # below.
        if hasattr(pc, "reset_provider_available_cache"):
            pc.reset_provider_available_cache()

    # Core V2 (#309): the five TAKKUB_V2_* flags fall back to the Settings UI's
    # persisted toggle in ~/.takkub/core-v2-settings.json when the env var is
    # unset. On a dev box where the user flipped them on, every "flag off by
    # default" test would otherwise read the real file and fail (proven
    # 2026-08-19). Redirect the settings path to the isolated tmp dir.
    cvs = _maybe_module("agent_takkub.core_v2_settings", force=True)
    if cvs is not None:
        _v2_path = tmp_path / "_isolated_takkub" / "core-v2-settings.json"
        monkeypatch.setattr(cvs, "path", lambda: _v2_path, raising=False)
        if hasattr(cvs, "_reset_cache"):
            cvs._reset_cache()  # (mtime,size) cache must not leak across tests

    # #338: `provider_config.provider_for` now falls back to the provider
    # recorded in role-models.json when role-providers.json says nothing about
    # a role — which makes the user's REAL ~/.takkub/role-models.json able to
    # decide what a test's "default provider" resolves to. Same isolation
    # reasoning (and same 2026-08-19 lesson) as core-v2-settings above.
    rm = _maybe_module("agent_takkub.role_models", force=True)
    if rm is not None:
        monkeypatch.setattr(
            rm, "_PATH", tmp_path / "_isolated_takkub" / "role-models.json", raising=False
        )

    # #362: provider_models._PATH = SETTINGS_HOME / "provider-models.json" is
    # bound by value at import time (same shape as role_models._PATH above),
    # so it was NOT covered by anything in this fixture — settings_window's
    # provider-roles view touches it, and two xdist workers racing
    # `tmp.replace(_PATH)` (provider_models._save) against the SAME real
    # ~/.takkub/provider-models.json produced `PermissionError: [WinError 5]
    # Access is denied` on windows-latest CI (Windows refuses to replace a
    # file another process still has a handle open on). Audited every
    # `<NAME> = SETTINGS_HOME / "..."` module-level binding in src/agent_takkub
    # (grep, 2026-08-23) and isolate ALL of them here, not just the one that
    # happened to fail first — each of these is the same unpatched-real-home
    # bug, just not yet caught by a concurrent write.
    _settings_home_path_modules = (
        ("agent_takkub.auto_resume", "_PATH", "autoresume.json"),
        ("agent_takkub.exec_mode", "_PATH", "exec-mode.json"),
        ("agent_takkub.plan_tier", "_PATH", "plan.json"),
        ("agent_takkub.provider_models", "_PATH", "provider-models.json"),
        ("agent_takkub.provider_state", "_PATH", "disabled-providers.json"),
        ("agent_takkub.remote.config", "_PATH", "remote.json"),
        ("agent_takkub.pane_tools_policy", "PANE_TOOLS_POLICY_FILE", "pane-tools.json"),
        ("agent_takkub.skill_policy", "SKILL_POLICY_FILE", "skill-policy.json"),
        ("agent_takkub.custom_roles", "CUSTOM_ROLES_FILE", "custom-roles.json"),
    )
    for _mod_name, _attr, _fname in _settings_home_path_modules:
        _m = _maybe_module(_mod_name, force=True)
        if _m is not None and hasattr(_m, _attr):
            monkeypatch.setattr(_m, _attr, tmp_path / "_isolated_takkub" / _fname, raising=False)

    # CUSTOM_AGENTS_DIR (a directory of <role>.md files, not a single json
    # file) is bound the same way in BOTH config.py and custom_roles.py
    # (`from .config import CUSTOM_AGENTS_DIR`) — custom_roles.create_role()
    # writes into it via NamedTemporaryFile(dir=CUSTOM_AGENTS_DIR), same
    # real-home-write risk as the constants above. Patch both copies.
    _custom_agents_dir = tmp_path / "_isolated_takkub" / "agents"
    if cfg is not None and hasattr(cfg, "CUSTOM_AGENTS_DIR"):
        monkeypatch.setattr(cfg, "CUSTOM_AGENTS_DIR", _custom_agents_dir, raising=False)
    cr = _maybe_module("agent_takkub.custom_roles", force=True)
    if cr is not None and hasattr(cr, "CUSTOM_AGENTS_DIR"):
        monkeypatch.setattr(cr, "CUSTOM_AGENTS_DIR", _custom_agents_dir, raising=False)

    # user_profile._DEFAULT_CONFIG_DIR = config.default_claude_config_dir()
    # is ANOTHER import-time-bound constant (same shape as the SETTINGS_HOME
    # ones above, just not itself SETTINGS_HOME-rooted) — on a dev checkout
    # it's the real `~/.claude`. worktree_manager.pre_trust_worktrees_root
    # (#444) derives the default-profile `.claude.json` location from it, so
    # an unpatched test exercising that path (any `--isolation worktree`
    # assign through a real, un-mocked Orchestrator._assign_with_worktree —
    # confirmed live via test_worktree_assign.py before this fix) wrote a
    # stray trust entry into the REAL `~/.claude.json` on the machine
    # running the suite. Same fix shape as CUSTOM_AGENTS_DIR above; a test
    # with its own `monkeypatch.setattr(up, "_DEFAULT_CONFIG_DIR", ...)`
    # (the test_user_profile.py `isolate` fixture's convention) still wins.
    up_mod = _maybe_module("agent_takkub.user_profile", force=True)
    if up_mod is not None and hasattr(up_mod, "_DEFAULT_CONFIG_DIR"):
        monkeypatch.setattr(
            up_mod,
            "_DEFAULT_CONFIG_DIR",
            tmp_path / "_isolated_takkub" / "dot-claude",
            raising=False,
        )

    # worktree_manager.DATA_HOME = config.DATA_HOME is likewise bound by
    # value at import time (`from .config import DATA_HOME`), so patching
    # `cfg.DATA_HOME` above (kept equal to `cfg.REPO_ROOT` for the "dev
    # checkout" identity other code branches on) does not reach it. Every
    # worktree-managed-root computation (`worktree_root`,
    # `worktrees_managed_root`, `pre_trust_worktrees_root`) otherwise
    # resolves against the REAL DATA_HOME of whatever checkout runs the
    # suite instead of an isolated tmp dir. A test with its own
    # `monkeypatch.setattr(wm, "DATA_HOME", ...)` (the existing
    # test_worktree_manager.py convention) still wins.
    wm_mod = _maybe_module("agent_takkub.worktree_manager", force=True)
    if wm_mod is not None and hasattr(wm_mod, "DATA_HOME") and "_isolated_repo_root" in locals():
        monkeypatch.setattr(wm_mod, "DATA_HOME", _isolated_repo_root, raising=False)

    # Several modules deliberately resolve their state path lazily from
    # `config.SETTINGS_HOME` at CALL time instead of binding a module-level
    # constant, specifically so a test that monkeypatches `config.SETTINGS_
    # HOME` lands in its own tmp dir (see e.g. auto_issue_signals._flag_path's
    # docstring: "Resolved at call time so a test monkeypatching SETTINGS_HOME
    # lands in its own tmp dir"). That convention only works if something
    # actually patches `config.SETTINGS_HOME` for tests that don't set up
    # their own isolation fixture (core_v2_settings needed exactly this fix
    # on 2026-08-19, above) — patch the module attribute itself so every
    # current AND future lazy `config.SETTINGS_HOME`-reader is covered in one
    # place, on top of the import-time-bound constants patched individually
    # above. No known code branches on `SETTINGS_HOME == <anything>` (unlike
    # `DATA_HOME == REPO_ROOT`, which is why DATA_HOME itself stays untouched
    # here — see storage_layout_v2 comment above), so this is safe globally.
    # A test file with its OWN `monkeypatch.setattr(config, "SETTINGS_HOME",
    # ...)` fixture (e.g. test_auto_migrate_boot.py) still wins — autouse
    # fixtures from conftest run first, so the test-local one simply
    # overwrites this default afterward.
    if cfg is not None and hasattr(cfg, "SETTINGS_HOME"):
        monkeypatch.setattr(cfg, "SETTINGS_HOME", tmp_path / "_isolated_takkub", raising=False)

    # #196: AuthGate.__init__ now reads/writes `session_store.py`'s on-disk
    # password-session store unconditionally (not opt-in like the P0 remote
    # scaffold — any test that builds a real RemoteHttpServer/AuthGate, not
    # just test_remote_auth.py, goes through it). Unpatched, that's a read
    # and (on `issue_password_session`) a write to the real
    # `~/.takkub/takkub-remote-sessions.json` on the machine running pytest.
    ss = _maybe_module("agent_takkub.remote.session_store", force=True)
    if ss is not None:
        monkeypatch.setattr(ss, "_PATH", runtime / "takkub-remote-sessions.json", raising=False)

    # `orchestrator._main_thread_heartbeat_age` is a module-global probe
    # (set via `set_main_thread_heartbeat_probe`) that lead_inbox's
    # submit-verify loop reads on every decision (#133). Production wiring
    # is app.py's `_start_deadman_watchdog`, which registers a REAL probe
    # closed over a `window` object — `tests/test_single_instance_watchdog.py`
    # calls that function directly with a local `MagicMock()` window and
    # never restores the probe afterward, so it stays permanently "stale"
    # (growing `time.monotonic() - <frozen ts>`) for every test that runs
    # later in the same process. Combined with a `QTimer.singleShot`
    # synchronous-mock (several test files patch it to call its callback
    # immediately instead of via the Qt event loop), `_delayed_enter_verified`'s
    # stall-deferral branch — normally bounded to a few nested calls — instead
    # re-triggers on every iteration of its ~150-attempt busy-resend budget,
    # multiplying the synchronous call depth enough to blow Python's
    # recursion limit (reproduced: `test_single_instance_watchdog.py` then
    # `test_no_content_watchdog_cap.py::TestAttemptTwoCapsOut` in one pytest
    # run → RecursionError). Force it back to the neutral "never stale"
    # probe before every test so no test — this one or a future one — can
    # leak it into another via process/worker reuse under xdist.
    orch_hb = _maybe_module("agent_takkub.orchestrator", force=True)
    if orch_hb is not None and hasattr(orch_hb, "_main_thread_heartbeat_age"):
        monkeypatch.setattr(orch_hb, "_main_thread_heartbeat_age", lambda: 0.0, raising=False)

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

    # #344: re-arm every test, not just once at session start. Some test
    # (test_app_exception_guard.py) legitimately installs app.py's OWN
    # sys.excepthook mid-session to test it directly, which would otherwise
    # leave every later test's leaked-QTimer exceptions logged-and-swallowed
    # by app.py's hook instead of queued for the session-end check below.
    _install_qt_exception_guard()

    # #364 lever 3 CI fix: `_ram_derived_max_panes_global()` derives its
    # default max_panes_global cap from the machine's REAL available RAM
    # (`psutil.virtual_memory()`) — on a RAM-constrained CI runner (macOS
    # runners in particular) that can floor the cap to as low as 2, silently
    # dropping a 3rd concurrent spawn in any test that doesn't pin its own
    # slot_policy (proven: test_spawn_task_delivery.py's FIFO queue test lost
    # its 3rd backend assign on macOS CI while windows/ubuntu stayed green —
    # not a timing bug, an environment-dependent one). Patch the RAM sample
    # itself (not `_ram_derived_max_panes_global` directly) so tests keep
    # exercising the real headroom-over-reserve arithmetic —
    # test_core_scheduling.py's own RAM-cap tests set their own fake
    # `psutil.virtual_memory` per test, which simply overrides this default
    # via the same monkeypatch stack (last `setattr` wins).
    psu = _maybe_module("psutil", force=True)
    if psu is not None and hasattr(psu, "virtual_memory"):
        monkeypatch.setattr(psu, "virtual_memory", lambda: _AbundantVM(), raising=False)

    yield

    # #344: after every other per-test fixture has torn down (autouse
    # fixtures set up first among same-scope fixtures, so they tear down
    # last), stop any QTimer this test leaked before it can fire against the
    # now-gone state in a later test. See _install_qtimer_leak_tracker's
    # docstring above for why this is non-fatal.
    _stop_leaked_qtimers()

    # #362 guard: fail loudly if this test wrote to the REAL SETTINGS_HOME
    # (top-level files only — see _snapshot_settings_home_files) instead of
    # the isolated tmp dir every patch above redirects to. A hit here means
    # some module-level `_PATH`/`_FILE = SETTINGS_HOME / ...` (or a lazy
    # `config.SETTINGS_HOME`-reader) escaped isolation — the exact failure
    # class that broke CI as provider_models.py `WinError 5` before this
    # fixture patched it. Under pytest-xdist, several workers snapshot the
    # SAME real directory concurrently, so a positive here isn't proof THIS
    # specific test is the leaker if others ran at the same moment — but it
    # is proof some test in this run wrote to real user state and needs
    # fixing, which is the point.
    if _real_settings_home is not None:
        _settings_home_after = _snapshot_settings_home_files(_real_settings_home)
        if _settings_home_before is None or _settings_home_after is None:
            # A test in progress had trapped filesystem calls (e.g. patched
            # `Path.stat`) when a snapshot was attempted — unmeasurable this
            # round, so skip the guard rather than fail an unrelated test.
            pass
        elif _settings_home_after != _settings_home_before:
            _changed = sorted(
                (set(_settings_home_after) ^ set(_settings_home_before))
                | {
                    name
                    for name in _settings_home_after.keys() & _settings_home_before.keys()
                    if _settings_home_after[name] != _settings_home_before[name]
                }
            )
            pytest.fail(
                f"test wrote to the REAL {_real_settings_home} instead of an "
                f"isolated tmp dir: {_changed}. Add the offending module's "
                "`_PATH`/`_FILE = SETTINGS_HOME / ...` constant to "
                "`_settings_home_path_modules` (or patch it directly) in "
                "tests/conftest.py's `_isolate_runtime`, same as "
                "provider_models._PATH (#362)."
            )


@pytest.fixture(scope="session", autouse=True)
def _guard_repo_root_no_leaked_context_dir():
    """Regression backstop for the `trace_store._trace_path()` leak: it
    resolves via `config.DATA_HOME`, which equals REPO_ROOT on this dev
    checkout's default resolution (`config.py`'s `_resolve_data_home`)
    whenever some test's real context-build pipeline call reaches it with an
    unpatched `config.DATA_HOME` — confirmed live as
    `test_spawn_task_delivery.py`'s real (unmocked) `orch.assign()` calls
    writing `<repo_root>/context/last_context_trace.json` before that file
    gained its own `_isolate_data_home` fixture (see
    `test_orchestrator_v2_context_hook.py`'s fixture of the same name for
    the established fix). Session-scoped so it catches the leak regardless
    of which test causes it, not just this one file.

    Do NOT "fix" a failure here by gitignoring `context/` — find the test
    with the real (unpatched) pipeline call and give it its own
    `monkeypatch.setattr(config, "DATA_HOME", tmp_path / "data")`, same as
    the two files referenced above."""
    context_dir = _REPO_ROOT_FOR_WHEEL_BUILD / "context"
    pre_existing = context_dir.exists()
    yield
    if not pre_existing and context_dir.exists():
        leaked = sorted(p.name for p in context_dir.iterdir())
        pytest.fail(
            f"test session leaked real files into {context_dir} via an "
            f"unisolated config.DATA_HOME resolving to REPO_ROOT: {leaked}. "
            "Isolate config.DATA_HOME in the leaking test (see "
            "test_orchestrator_v2_context_hook.py's _isolate_data_home) — "
            "do not gitignore context/ instead."
        )


@pytest.fixture
def isolated_v2_data_home(tmp_path):
    """The exact path `_isolate_runtime` (above) redirects `storage_layout_v2()`'s
    no-arg default to for this test. A test proving epic #309 Wave C parity
    (core.routing.Router.effective_model_for reading a REAL migrated V2
    catalog) needs to write into this SAME tree — not just any tmp dir — so
    the façade's own default resolution finds what the test migrated."""
    return tmp_path / "_isolated_takkub" / "data-home"


# #344: pytest install no sys.excepthook of its own, and PyQt6's default
# behaviour when an exception escapes a slot invoked from C++ (a QTimer
# firing, a signal emitted through the event loop, …) is qFatal() — a hard
# process abort with no traceback and no pytest summary. This bit the suite
# for real: a QTimer leaked by an earlier test (an Orchestrator whose
# `_idle_watchdog` was never stopped) kept firing after that test's fixtures
# tore its state down, its slot raised, and the whole run died silently
# somewhere around the 35-77% mark depending on timing (#344).
#
# Same technique `agent_takkub.app._install_exception_guard()` uses for the
# real GUI entrypoint (read its docstring for the qFatal mechanism) — a
# NON-default sys.excepthook/threading.excepthook/unraisablehook is enough to
# flip PyQt6 from aborting to routing the exception here and letting the
# event loop continue. Tests don't get app.py's boot.log / auto_issue_capture
# routing (that would risk filing a real GitHub issue, #188, and is a
# GUI-process concern anyway) — instead the traceback goes straight to the
# real stderr (bypasses pytest's capsys, so it's visible even without -s) and
# is queued so a leak fails the SESSION at teardown instead of being silently
# swallowed. A test that deliberately triggers one on purpose (proving this
# guard works) must drain the queue with `pop_qt_slot_exceptions()` afterwards
# — see test_provider_toggle_orchestrator.py.
_qt_slot_exceptions: list[str] = []


def _record_qt_slot_exception(exc_type, exc_value, exc_tb, *, source: str) -> None:
    import traceback

    tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    current_test = os.environ.get("PYTEST_CURRENT_TEST", "?")
    entry = f"\n--- UNHANDLED QT EXCEPTION ({source}) during {current_test} ---\n{tb}"
    _qt_slot_exceptions.append(entry)
    try:
        if sys.__stderr__ is not None:
            sys.__stderr__.write(entry)
            sys.__stderr__.flush()
    except Exception:
        pass


def _install_qt_exception_guard() -> None:
    def _hook(exc_type, exc_value, exc_tb):
        _record_qt_slot_exception(exc_type, exc_value, exc_tb, source="sys.excepthook")

    sys.excepthook = _hook

    def _thread_hook(args):
        name = args.thread.name if args.thread is not None else "?"
        _record_qt_slot_exception(
            args.exc_type, args.exc_value, args.exc_traceback, source=f"thread:{name}"
        )

    threading.excepthook = _thread_hook

    if hasattr(sys, "unraisablehook"):

        def _unraisable(unr):
            _record_qt_slot_exception(
                type(unr.exc_value), unr.exc_value, unr.exc_traceback, source="unraisable"
            )

        sys.unraisablehook = _unraisable


class UnexpectedModalDialogError(AssertionError):
    """#349: raised when test code reaches a real Qt modal-dialog static call
    (QMessageBox.*, QInputDialog.*, QFileDialog.*) that no test explicitly
    patched. See `_block_qt_modals` below."""


_qt_modal_calls: list[tuple[str, str, str]] = []


def pop_qt_modal_calls() -> list[tuple[str, str, str]]:
    """Test-only escape hatch: fetch-and-clear (method, title, text) tuples
    recorded by `_block_qt_modals`'s default patch before it raised. Mirrors
    `pop_qt_slot_exceptions` above."""
    drained = list(_qt_modal_calls)
    _qt_modal_calls.clear()
    return drained


def _blocked_modal(method_name: str):
    def _call(*args, **kwargs):
        # Best-effort extraction: every patched static takes (parent, title,
        # text, ...) or (parent, caption, dir, ...) — same shape, args[1]/[2].
        title = args[1] if len(args) > 1 else kwargs.get("title", kwargs.get("caption", ""))
        text = args[2] if len(args) > 2 else kwargs.get("text", kwargs.get("directory", ""))
        _qt_modal_calls.append((method_name, str(title), str(text)))
        raise UnexpectedModalDialogError(
            f"{method_name}(title={title!r}, text={text!r}) tried to open a real Qt "
            "modal dialog during a test that never patched it (#349) — under the "
            "offscreen platform this blocks the event loop forever waiting for a click "
            "nobody can give it, which is what made worker gw7 look 'crashed' at "
            "settings_window.py:1238 (QMessageBox.critical in the save-rollback OSError "
            "path) instead of failing loudly. If this test intentionally exercises this "
            "dialog, monkeypatch it yourself before triggering the code path — e.g. "
            f"monkeypatch.setattr({method_name.split('.', 1)[0]}, "
            f"{method_name.split('.', 1)[1]!r}, lambda *a, **k: ...)."
        )

    return _call


@pytest.fixture(autouse=True)
def _block_qt_modals(monkeypatch: pytest.MonkeyPatch):
    """#349 root cause: a real QMessageBox/QInputDialog/QFileDialog modal
    blocks the Qt event loop forever under the offscreen test platform —
    nobody can click it — until faulthandler kills the pytest-xdist worker at
    the 280s timeout, which then reports as an opaque 'worker crashed'
    indistinguishable from a native abort. That specific path (settings_
    window.py's Save & Apply OSError-rollback handler) is only reached when a
    real disk write races, so it slipped through in ~2 of 3 CI runs.

    Patch every static modal-dialog entry point so any call a test did NOT
    explicitly patch itself fails immediately with a clear message naming the
    dialog, instead of hanging. This fixture's patch is applied before the
    test body runs, so a test's own `monkeypatch.setattr(QMessageBox, ...)`
    (the existing, already-widespread pattern in this suite — see
    test_settings_window.py, test_project_wizard.py, etc.) simply overrides
    it for that test; nothing here changes production code, which should
    keep showing these dialogs to a real user.
    """
    try:
        from PyQt6.QtWidgets import QFileDialog, QInputDialog, QMessageBox
    except ModuleNotFoundError:
        yield
        return

    for name in ("critical", "warning", "information", "question", "about"):
        monkeypatch.setattr(QMessageBox, name, _blocked_modal(f"QMessageBox.{name}"))
    for name in ("getItem", "getMultiLineText", "getText", "getInt", "getDouble"):
        if hasattr(QInputDialog, name):
            monkeypatch.setattr(QInputDialog, name, _blocked_modal(f"QInputDialog.{name}"))
    for name in ("getExistingDirectory", "getOpenFileName", "getSaveFileName"):
        monkeypatch.setattr(QFileDialog, name, _blocked_modal(f"QFileDialog.{name}"))

    yield


def pop_qt_slot_exceptions() -> list[str]:
    """Test-only escape hatch: fetch-and-clear queued Qt slot exceptions.

    A test proving the guard above works (#344) raises inside a Qt slot on
    purpose and must drain the queue afterwards — otherwise the deliberate
    exception fails the whole session at `_qt_session_app` teardown below.
    """
    drained = list(_qt_slot_exceptions)
    _qt_slot_exceptions.clear()
    return drained


# #344 follow-up: the excepthook above turns a leaked-timer exception into a
# diagnosable failure instead of a silent abort, but it only fires when the
# leaked timer's slot actually raises. backend#2's audit found the leak
# itself is much broader than the one confirmed case this file already
# closes: `Orchestrator.__init__` arms THREE QTimers unconditionally
# (`_idle_watchdog`, `_resource_timer`, `_hot_md_timer`), most Orchestrator-
# constructing tests across the suite stop only `_idle_watchdog`, and one
# file alone (test_idle_watchdog.py, 48 tests, each with a function-scoped
# Orchestrator) leaks 2 timers x 48 — which lines up with the 35% repro
# death zone (it collects right before test_issues.py).
#
# This tracker is deliberately generic: it does not know which object owns a
# given QTimer, only that one exists and is still ticking after the test
# that created it tore down — so it catches every leak, including ones no
# one has audited yet. Every QTimer() construction is recorded in a
# WeakSet (QTimer.singleShot() is a separate static C++ path that self-
# cleans after firing once — nothing to track there); `_isolate_runtime`'s
# per-test teardown below stops anything still active so it can't fire
# against torn-down state later in the session (the actual abort risk),
# and queues a one-line note for a session-end report.
#
# #345 follow-up: was deliberately non-fatal while backend#2's per-file audit
# was still in progress (240 leaks at the low point) — failing on it then
# would have turned nearly the whole run red and buried the (rare, always-
# a-real-bug) exception-escape failures the guard above exists to surface.
# That audit is done (240 -> 0, #345) — this now FAILS the session (see
# `_qt_session_app`'s teardown below, same `pytest.fail` pattern already
# used for the exception queue above) instead of only printing the
# non-fatal `pytest_terminal_summary` section, so a QTimer newly leaked by
# a future test/production change goes red immediately instead of quietly
# regrowing this list unnoticed until it hits #344-style abort territory
# again.
_live_qtimers: weakref.WeakSet | None = None
_leaked_timer_reports: list[str] = []


def _install_qtimer_leak_tracker() -> None:
    global _live_qtimers
    if _live_qtimers is not None:
        return  # already installed this session
    _live_qtimers = weakref.WeakSet()

    from PyQt6.QtCore import QTimer

    _original_init = QTimer.__init__

    def _tracking_init(self, *args, **kwargs):
        _original_init(self, *args, **kwargs)
        _live_qtimers.add(self)

    QTimer.__init__ = _tracking_init


def _stop_leaked_qtimers() -> None:
    """Per-test teardown: stop (never fail on) any QTimer still active."""
    if _live_qtimers is None:
        return
    current_test = os.environ.get("PYTEST_CURRENT_TEST", "?")
    for timer in list(_live_qtimers):
        try:
            active = timer.isActive()
        except RuntimeError:
            continue  # underlying C++ QTimer already destroyed
        if not active:
            continue
        interval = timer.interval()
        name = timer.objectName() or "<unnamed>"
        timer.stop()
        _leaked_timer_reports.append(
            f"{current_test}: leaked active QTimer(interval={interval}ms, objectName={name!r})"
        )


def _format_leaked_timer_reports(reports: list[str], *, limit: int = 50) -> str:
    """Render *reports* (one `_stop_leaked_qtimers` line per leaked timer,
    already naming the leaking test + the timer's interval/objectName) as an
    indented block, capped at *limit* lines. Shared by the non-fatal
    terminal-summary section and the session-failure message below so the
    two never drift out of sync (#345)."""
    shown = reports[:limit]
    more = len(reports) - len(shown)
    detail = "\n".join(f"  {line}" for line in shown)
    if more:
        detail += f"\n  ... and {more} more"
    return detail


def _qtimer_leak_failure_message(reports: list[str]) -> str | None:
    """Build the `pytest.fail` message for `_qt_session_app`'s teardown, or
    None when *reports* is empty (nothing to fail on). Pulled out as a pure
    function so it's unit-testable without actually failing a pytest
    session — see test_conftest_qtimer_leak_tracker.py."""
    if not reports:
        return None
    return (
        f"#344/#345: {len(reports)} QTimer(s) were still active after their owning "
        "test finished (each already stopped here so it couldn't fire against "
        "torn-down state later in the session). A QTimer constructed with a parent "
        "(e.g. `QTimer(self)`) must be stopped by the test that armed it, or by that "
        "owner's `shutdown_timers()` in a fixture teardown — see #345's "
        "CliServer._fire_staggered/shutdown_timers and Orchestrator.shutdown_timers "
        "for the established pattern. Do not silence this check by widening the "
        "non-fatal report again.\n" + _format_leaked_timer_reports(reports)
    )


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if not _leaked_timer_reports:
        return
    terminalreporter.section("#344/#345 leaked QTimer summary (fails the session)")
    terminalreporter.write_line(
        f"{len(_leaked_timer_reports)} QTimer(s) were still active after their owning "
        "test finished; stopped here so they can't fire against torn-down state later "
        "in the session. Each line names the leaking test, the timer's interval, and "
        "its objectName (if any) — find the QTimer(...) construction in that test's "
        "code path and stop it (or park it in a per-test/fixture teardown) instead of "
        "leaving it armed past teardown. See _install_qtimer_leak_tracker's docstring "
        "in conftest.py for #344/#345 context. `_qt_session_app`'s teardown below turns "
        "this into a session failure."
    )
    terminalreporter.write_line(_format_leaked_timer_reports(_leaked_timer_reports))


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

    # Install before constructing QApplication: exceptions raised from slots
    # fired during construction (or by any test module before its own local
    # fixtures run) must be caught too, not just ones after this point.
    _install_qt_exception_guard()
    _install_qtimer_leak_tracker()

    app = QApplication.instance()
    if app is None:
        # #364 lever-1 spike (docs/audit/2026-08-23-364-lever1-pane-discard-
        # spike.md): QApplication([]) leaves argc=0, no argv[0] — Chromium's
        # base::CommandLine::Init chokes on that and the process hard-aborts
        # natively (Windows exit -1073740791) the instant a real
        # QWebEngineView spins up its renderer, even under
        # QT_QPA_PLATFORM=offscreen. [sys.argv[0]] (not the full sys.argv —
        # pytest/xdist's own flags aren't guaranteed safe to hand to
        # Chromium's arg parser) supplies just enough for a valid
        # command line; confirmed by direct repro both ways.
        app = QApplication([sys.argv[0]])
    yield app
    # Do NOT call app.quit() here — session-scoped fixture teardown may race
    # with other fixtures still running.  Let the process exit handle cleanup.

    leaked = pop_qt_slot_exceptions()
    if leaked:
        pytest.fail(
            f"#344: {len(leaked)} unhandled exception(s) escaped a Qt slot during "
            "the session (full traceback(s) already on stderr above). This is "
            "almost always a QTimer leaked by an earlier test — e.g. an "
            "Orchestrator built without stopping `_idle_watchdog` — that kept "
            "firing after that test's fixtures tore its state down. Find and "
            "stop the leaking timer; do not silence this check.\n" + "\n".join(leaked),
            pytrace=False,
        )

    # #345: promoted from the non-fatal `pytest_terminal_summary` report to a
    # real failure now that the per-file audit (#345) closed the count to 0 —
    # see _install_qtimer_leak_tracker's docstring above for why this waited.
    # Each entry already names the leaking test + the timer's interval/
    # objectName (`_stop_leaked_qtimers`), so the failure message is
    # self-contained: no need to re-run with extra flags to find the culprit.
    timer_leak_msg = _qtimer_leak_failure_message(_leaked_timer_reports)
    if timer_leak_msg is not None:
        pytest.fail(timer_leak_msg, pytrace=False)
