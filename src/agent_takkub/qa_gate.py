"""Canonical QA gate (#325) — the ONE entrypoint for pytest + ruff +
import-linter, run identically from a qa pane, CI, and a user's terminal.

Fixes the two footguns that used to bite ad-hoc invocations:
  * system python + PYTHONPATH=src ("fake packaging bug") — every step here
    resolves and invokes the shared `.venv`'s own pytest/ruff/lint-imports
    binaries directly whenever that venv exists, so it never matters which
    interpreter launched `takkub`. `venv-check` refuses outright if the venv
    exists but is missing a tool (broken install) rather than silently
    falling back to a bare command name that might resolve to something else
    on PATH.
  * exit code swallowed by a shell pipe — every step runs via
    `subprocess.run()` with no `shell=True`/pipe; `.returncode` is read back
    directly, never inferred from piped output.

No local `.venv` (CI, or a fresh machine before the shared venv exists) is a
supported, not a refused, state — the running interpreter (`sys.executable`)
is trusted then, exactly as CI's own `pip install -e .[dev]` step already
made it the correct one.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ._win_console import SUBPROCESS_NO_WINDOW

# Mirrors core/*/flag.py's os.environ.get(...) == "1" contract exactly —
# #309 Phase 9's 5 named flags (context has no module yet, see
# core_v2_settings.py's own comment, but the env var is still forced for the
# day it exists).
V2_FLAG_ENV_VARS: tuple[str, ...] = (
    "TAKKUB_V2_ROUTER",
    "TAKKUB_V2_CONVERSATION",
    "TAKKUB_V2_CONTEXT",
    "TAKKUB_V2_BRAIN",
    "TAKKUB_V2_SCHEDULER",
)

_WIN = os.name == "nt"

# #349: full pytest died silently (exit 127, ~1 in 3-4 full runs) with no
# traceback, no pytest summary, and no evidence of *why* — the leading
# suspect (OOM) was unprovable because nothing sampled memory while it ran.
# Sampling every 30s is effectively free next to a 600s+ full run and turns
# "no evidence" into "here's what memory looked like right before it died".
_MEMORY_SAMPLE_INTERVAL_S = 30.0

# pytest's own documented exit codes (https://docs.pytest.org/en/stable/
# reference/exit-codes.html) — anything outside this set (127, a negative
# signal-kill code, a Windows fault code like 3221225477) is the #349
# signature: the process died a way pytest itself never defined, which is
# exactly when a normal "N passed/failed" summary line goes missing too.
_PYTEST_DEFINED_EXIT_CODES = frozenset({0, 1, 2, 3, 4, 5})
_PYTEST_SUMMARY_RE = re.compile(
    r"=+\s+.*\b(?:passed|failed|error|no tests ran|deselected)\b.*\s+=+"
)

# #401: distinct from pytest's own 0-5 range and from a passthrough tool
# returncode — a caller (CI, cmd_qa_gate) needs to tell "this venv is
# genuinely missing a dev tool" apart from "a real test/lint failed" without
# scraping stdout. 78 is sysexits.h's EX_CONFIG ("something was found in an
# unconfigured or misconfigured state") — not load-bearing, just a
# self-documenting, unlikely-to-collide choice.
ENV_GAP_EXIT_CODE = 78

# Install hint shown on an ENV_GAP step — the pip package name, which is not
# always the same as the console-script name (`lint-imports` ships in the
# `import-linter` package).
_PIP_INSTALL_HINT = {
    "pytest": "pip install pytest",
    "ruff": "pip install ruff",
    "lint-imports": "pip install import-linter",
}


def _sample_memory_line() -> str:
    """One diagnostic line: wall-clock, system available memory, and the
    RSS of every child process this gate has spawned so far (pytest/ruff/
    lint-imports run as a direct child, so this is that child's own memory
    footprint at the moment of the sample).

    psutil is already a hard dependency of this project (resource_governor.py,
    performance_settings.py) and abstracts the Windows/macOS difference
    itself — `virtual_memory()` and `Process.children()`/`memory_info()` are
    the same call on both, so no `sys.platform` branch is needed here; that
    parity is exactly why the rest of the codebase already relies on psutil
    the same way cross-platform.

    Must never raise into the sampler thread — a resource-exhaustion moment
    is exactly when psutil itself is more likely to fail too (permission
    denied, process already gone).
    """
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        import psutil

        vm = psutil.virtual_memory()
        rss_bytes = 0
        child_count = 0
        for child in psutil.Process(os.getpid()).children(recursive=True):
            try:
                rss_bytes += child.memory_info().rss
                child_count += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return (
            f"{ts}  system_available={vm.available / 1024 / 1024:.0f}MB"
            f"  system_total={vm.total / 1024 / 1024:.0f}MB"
            f"  subprocess_rss={rss_bytes / 1024 / 1024:.0f}MB"
            f"  ({child_count} child process{'es' if child_count != 1 else ''})"
        )
    except Exception as e:
        return f"{ts}  memory sample failed: {type(e).__name__}: {e}"


def _is_silent_pytest_abort(name: str, returncode: int, output: str) -> bool:
    """True for pytest's #349 signature: an exit code pytest itself never
    documents, with no terminal summary line printed — proof the process
    died before reaching its own reporting, not a normal test failure."""
    if name != "pytest" or returncode in _PYTEST_DEFINED_EXIT_CODES:
        return False
    return not _PYTEST_SUMMARY_RE.search(output)


@dataclass
class StepResult:
    name: str
    ok: bool
    skipped: bool
    seconds: float
    detail: str
    returncode: int | None = None
    log_path: Path | None = None
    memory_log_path: Path | None = None
    # #401: this tool is legitimately absent from the project's environment
    # (e.g. a unittest-only project designed to run its tests inside Docker)
    # rather than a genuine code/test failure or a broken venv. `ok` stays
    # True (an env gap must not fail the gate the same way a red test does)
    # — `env_gap` is what a caller checks to still surface it distinctly.
    env_gap: bool = False


@dataclass
class GateReport:
    steps: list[StepResult] = field(default_factory=list)
    v2_flags: bool = False
    targeted: list[str] | None = None
    report_path: Path | None = None

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps if not s.skipped)

    @property
    def env_gap(self) -> bool:
        return any(s.env_gap for s in self.steps)

    @property
    def exit_code(self) -> int:
        for s in self.steps:
            if not s.skipped and not s.ok:
                return s.returncode if s.returncode else 1
        if self.env_gap:
            return ENV_GAP_EXIT_CODE
        return 0


def worktree_root(cwd: Path | None = None) -> Path:
    """This checkout's own root (git-dir, not git-common-dir) — a linked
    worktree has its own root even though it shares one `.venv` with the main
    tree. Same split the import-linter pre-commit hook already relies on."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            cwd=cwd,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
        return Path(out.stdout.strip())
    except Exception:
        return (cwd or Path.cwd()).resolve()


def shared_venv_bin(cwd: Path | None = None) -> Path | None:
    """The shared `.venv`'s Scripts/(bin) dir, resolved from git-common-dir
    so every linked worktree (#81) finds the ONE venv all panes share —
    never a per-worktree `.venv` that doesn't exist. `None` when no shared
    venv exists at all (CI, fresh machine)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            cwd=cwd,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
        root = Path(out.stdout.strip()).parent
    except Exception:
        return None
    for sub in ("Scripts", "bin"):
        candidate = root / ".venv" / sub
        if candidate.is_dir():
            return candidate
    return None


def _resolve_tool(bin_dir: Path | None, name: str) -> str | None:
    if bin_dir is None:
        return None
    exts = (".exe",) if _WIN else ("",)
    for ext in exts:
        candidate = bin_dir / f"{name}{ext}"
        if candidate.exists():
            return str(candidate)
    return None


def _venv_check(bin_dir: Path | None) -> StepResult:
    """Only refuses when the venv can't run ANYTHING at all (no `python`
    itself) — that's a broken/incomplete install, still a hard FAIL. A venv
    that has `python` but is missing `pytest`/`ruff`/`lint-imports` is not
    broken — plenty of real projects (e.g. unittest-only, tests run inside
    Docker) never install some or all of these on purpose (#401). `run_gate`
    checks per-tool availability itself and reports each gap as ENV_GAP
    rather than failing the whole gate on a venv-check refusal.
    """
    t0 = time.monotonic()
    if bin_dir is None:
        return StepResult(
            "venv-check",
            True,
            False,
            time.monotonic() - t0,
            "no shared .venv found — trusting the running interpreter (CI/fresh install)",
        )
    if _resolve_tool(bin_dir, "python") is None:
        return StepResult(
            "venv-check",
            False,
            False,
            time.monotonic() - t0,
            f"refuse: {bin_dir} missing python — broken/incomplete venv. "
            "Do NOT fall back to system python + PYTHONPATH=src (known footgun, #202).",
        )
    missing = [n for n in ("pytest", "ruff", "lint-imports") if _resolve_tool(bin_dir, n) is None]
    detail = f"using {bin_dir}"
    if missing:
        detail += (
            f" — env gap: {', '.join(missing)} not installed in this venv (#401, not a "
            "broken install; each missing tool is reported as ENV_GAP below, not FAIL)"
        )
    return StepResult("venv-check", True, False, time.monotonic() - t0, detail)


def _env_gap_step(tool: str, *, extra: str = "") -> StepResult:
    """#401: a project's venv legitimately lacking this tool is a different
    situation from a broken venv or a red test/lint run — surface it as
    ENV_GAP (ok=True so it doesn't fail the gate the way a real failure
    would, env_gap=True so a caller can still tell the difference from a
    clean pass) with the install command that closes the gap."""
    hint = _PIP_INSTALL_HINT.get(tool, f"pip install {tool}")
    return StepResult(
        tool,
        True,
        False,
        0.0,
        f"ENV_GAP: {tool} not found in this project's environment ({hint}){extra} "
        "— not a code/test failure, see #401",
        env_gap=True,
    )


def _unittest_discover_cmd(py: str, wroot: Path) -> list[str] | None:
    """`python -m unittest discover` fallback for a venv with no pytest
    (#401). Only offered when there's something under `tests/` to discover —
    otherwise the caller reports ENV_GAP instead of a misleading "0 tests
    ran" pass. Always discovers the whole `tests/` tree, even in targeted
    mode: unittest's own discovery has no path-list narrowing to map targeted
    paths onto (same reason the Node gate's `--targeted` note exists — the
    caller adds that note to the step detail when `targeted` was requested)."""
    tests_dir = wroot / "tests"
    if not tests_dir.is_dir() or not any(tests_dir.rglob("test_*.py")):
        return None
    return [py, "-m", "unittest", "discover", "-s", str(tests_dir), "-p", "test_*.py"]


def _xdist_worker_count() -> str:
    """Worker count for the full tier's `-n` flag — a number, never `auto`.

    #349 risk: `-n auto` on this 16-core dev box spins one worker per core;
    a serial full-suite pytest process alone measures ~2.9GB RSS, and
    committed memory is what actually faults a process on Windows when it
    runs out (not physical RAM — see #349's docstring above), not something
    16 idle-looking cores would tell you about. `8` here is a conservative
    pick from that commit-charge headroom math, NOT a benchmarked number —
    a same-machine `-n auto` vs `-n 8` comparison was attempted and aborted
    (this dev box had too many other panes contending for cores at the time
    to produce a clean reading); redo that comparison under a quiet machine
    before tuning this further. `TAKKUB_QA_XDIST_N` overrides this for boxes
    with different core/RAM ratios (CI runners, other dev machines) without
    editing this file.
    """
    try:
        n = int(os.environ.get("TAKKUB_QA_XDIST_N", "8"))
        if n < 1:
            raise ValueError
    except (TypeError, ValueError):
        n = 8
    return str(n)


def _pytest_cmd(
    bin_dir: Path | None,
    py: str,
    targeted: list[str] | None,
    exec_prefix: list[str] | None = None,
) -> list[str]:
    """The full tier (`targeted` is None) runs under pytest-xdist — 16 idle
    cores running 8564 tests serially at ~646s was pure waste. A fixed
    worker count (see `_xdist_worker_count`) picks fewer workers than cores
    on purpose — memory scales with workers, not with idle cores.

    `--dist loadscope` (NOT `loadgroup` — that only groups items an explicit
    `@pytest.mark.xdist_group` names, everything else is freely distributed
    with no grouping at all): loadscope groups every test by its module (bare
    functions) or class (methods) and always runs one group inside a single
    worker, in original collection order. This suite was never audited for
    cross-worker safety before this change, so the safer default that needs
    no per-file opt-in wins — confirmed necessary, not just theoretical: a
    bare `patch(...).start()` leak in test_spawn_gate.py (fixed alongside
    this) used to survive for the rest of *whichever* process happened to run
    it, silently relied on by a later test class expecting the same leaked
    state — invisible in serial (fixed collection order always ran them in
    the same process in the right sequence) and even under plain `load`/
    un-grouped `loadgroup` (that class could land on a different worker
    process entirely, which never sees the leak). `loadscope` keeps each
    class in one worker the way serial always implicitly did; it would NOT
    have caught this bug on its own before the source leak was fixed, since
    even one worker still hits the real problem if two *different* scopes
    (classes) depend on each other's leaked state — audit any future
    xdist failure the same way: reproduce with `-n 1` (passes) vs a plain
    single-test run in isolation (fails) to tell "genuine parallel hazard"
    apart from "was already a hidden test-order dependency parallelism
    merely exposed".

    The targeted tier stays serial on purpose: it runs a handful of paths
    mid-flight (team policy — full suite once at the batch gate, see this
    module's docstring), where spinning up N worker processes costs more
    than the run itself saves.

    `--timeout=300` (pytest-timeout) dumps stack traces from all threads when a
    test hangs for >5 minutes (targeted runs). `--timeout=600` for full suite
    allows slower tests to complete while catching ubuntu CI hangs. Both were
    300s/120s before this was widened — the real full-suite worst case (8
    workers + CPU contention from a concurrently-running cockpit) pushed the
    single slowest test past 300s on a run with no actual hang, killing its
    xdist worker (`node down: Not properly terminated`) and failing the gate
    on nothing but a too-tight budget. 600s still catches a genuine hang fast
    next to CI's own `timeout-minutes: 20` per job (.github/workflows/ci.yml)
    — a real full-suite run recently took ~934s (~15.6min) total, so a single
    test eating up to 600s before this fires would already be starving that
    budget on its own, hang or not.

    `--timeout-method=thread` is already this platform's default (no
    SIGALRM on Windows), made explicit here because it matters on the
    signal-default platforms too: pytest-xdist workers talk to the
    controller only over an execnet channel built from `Popen(stdout=PIPE)`
    — stderr is left inherited, not piped, so anything written straight to
    the real stderr fd survives a worker that dies mid-test, while anything
    that goes through pytest's own TerminalWriter (i.e. stdout) does not.
    `--timeout-method=thread`'s handler calls `os._exit()` right after
    writing its dump — that write races the process exit and is lost before
    it can be forwarded, which is exactly why a timed-out worker crash in
    this gate showed only "node down: Not properly terminated" with no
    stack dump at all. `faulthandler_timeout` in pyproject.toml's pytest
    config (fires a bit before `--timeout` does) is the actual fix for that:
    it writes straight to a dup'd stderr fd via the stdlib `faulthandler`
    module, bypassing pytest's TerminalWriter and therefore surviving both
    the os._exit() race and xdist's stdout-only channel.

    `--max-worker-restart=0` prevents xdist from silently restarting a dead
    worker — a worker crash should fail immediately (no hidden stderr) rather
    than restart invisibly and potentially corrupt state or mask a bug.

    ``exec_prefix`` (#401): when set, every local venv resolution is skipped
    — the command runs as ``[*exec_prefix, "pytest", ...]`` (e.g.
    ``["docker", "compose", "exec", "-T", "gateway"]``), trusting that target
    to own its own pytest on its own PATH.
    """
    if exec_prefix is not None:
        base = [*exec_prefix, "pytest"]
    else:
        exe = _resolve_tool(bin_dir, "pytest")
        base = [exe] if exe else [py, "-m", "pytest"]
    if targeted:
        return [
            *base,
            "--timeout=300",
            "--timeout-method=thread",
            "--max-worker-restart=0",
            *targeted,
        ]
    return [
        *base,
        "-n",
        _xdist_worker_count(),
        "--dist",
        "loadscope",
        "--timeout=600",
        "--timeout-method=thread",
        "--max-worker-restart=0",
    ]


def _ruff_cmd(bin_dir: Path | None, py: str, exec_prefix: list[str] | None = None) -> list[str]:
    if exec_prefix is not None:
        base = [*exec_prefix, "ruff", "check"]
    else:
        exe = _resolve_tool(bin_dir, "ruff")
        base = [exe, "check"] if exe else [py, "-m", "ruff", "check"]
    return [*base, "src/", "tests/"]


def _lint_imports_cmd(bin_dir: Path | None, exec_prefix: list[str] | None = None) -> list[str]:
    if exec_prefix is not None:
        return [*exec_prefix, "lint-imports"]
    exe = _resolve_tool(bin_dir, "lint-imports")
    return [exe] if exe else ["lint-imports"]


def _log_stem(step_name: str) -> str:
    """Step name → safe log-file stem (#378).

    Node monorepo steps are named after their workspace (`typecheck:apps/admin`)
    and were used verbatim as the log filename: `:` is illegal on NTFS (drive
    letter / ADS separator → `[Errno 22] Invalid argument`, killing the gate
    before any step ran) and `/` became an unintended sub-directory. Every
    character outside `[A-Za-z0-9._-]` collapses to `-` on all platforms so
    the exports layout is identical on Windows and macOS.
    """
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", step_name).strip("-.")
    return stem or "step"


def _run_step(name: str, cmd: list[str], env: dict, cwd: Path, log_dir: Path | None) -> StepResult:
    t0 = time.monotonic()
    # #349: sample memory in the background for the whole life of this step
    # (30s cadence — cheap next to a 600s+ full pytest run) instead of only
    # ever finding out after the fact that nothing was recorded. The sampler
    # reads THIS process's child tree (see _sample_memory_line), so it needs
    # no reference to the subprocess object below — works the same whether
    # this step ends up completing normally, refusing, or dying silently.
    memory_samples: list[str] = [_sample_memory_line()]
    stop_sampling = threading.Event()

    def _sample_loop() -> None:
        while not stop_sampling.wait(_MEMORY_SAMPLE_INTERVAL_S):
            memory_samples.append(_sample_memory_line())

    sampler = threading.Thread(target=_sample_loop, name=f"qa-gate-mem-{name}", daemon=True)
    sampler.start()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=cwd,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
    except OSError as e:
        # e.g. FileNotFoundError from a resolved-but-deleted-mid-run exe —
        # never let this masquerade as a generic Python traceback.
        stop_sampling.set()
        sampler.join(timeout=2)
        return StepResult(
            name, False, False, time.monotonic() - t0, f"refuse: {type(e).__name__}: {e}"
        )
    stop_sampling.set()
    sampler.join(timeout=2)
    memory_samples.append(_sample_memory_line())
    elapsed = time.monotonic() - t0
    output = (proc.stdout or "") + (proc.stderr or "")
    log_path = None
    memory_log_path = None
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        safe = _log_stem(name)
        log_path = log_dir / f"{safe}.log"
        log_path.write_text(output, encoding="utf-8")
        memory_log_path = log_dir / f"{safe}-memory.log"
        memory_log_path.write_text("\n".join(memory_samples) + "\n", encoding="utf-8")
    tail_lines = [ln for ln in output.strip().splitlines() if ln.strip()][-6:]
    detail = " / ".join(tail_lines) if tail_lines else "(no output)"
    # #349: an exit code pytest never documents, with no summary line, is an
    # abort — not a test failure. Say so plainly instead of leaving the
    # reader to guess from a bare non-zero returncode.
    native_abort = _is_silent_pytest_abort(name, proc.returncode, output)
    if native_abort:
        detail = (
            f"NATIVE ABORT, not a test failure — see #349 (exit {proc.returncode}, "
            f"pytest never printed its own summary line): {detail}"
        )
    if proc.returncode != 0:
        # A failed step must be diagnosable from THIS process's stdout alone —
        # on CI the on-runner log file is unreachable after the job dies
        # (proven on run 32335988102: three OSes said only "FAILED tests/..."
        # with the traceback stranded in the runner's runtime/exports/). Print
        # the pytest FAILURES section when present (assertions live there),
        # else the last chunk of output, capped so a pathological log can't
        # flood the console.
        lines = output.splitlines()
        start = next((i for i, ln in enumerate(lines) if "= FAILURES =" in ln), None)
        excerpt = lines[start:] if start is not None else lines
        header = f"\n----- {name} failure output (excerpt) -----"
        if native_abort:
            header += (
                "\n*** NATIVE ABORT — no pytest summary printed, see #349."
                " This is not a code/test regression to chase. ***"
            )
        print(header)
        for ln in excerpt[-200:]:
            print(ln)
        print(f"----- end {name} failure output -----\n")
    # proc.returncode is read straight off the completed subprocess — never
    # inferred from a shell pipe's own $? (the #234-adjacent footgun this
    # gate exists to structurally rule out).
    return StepResult(
        name,
        proc.returncode == 0,
        False,
        elapsed,
        detail,
        proc.returncode,
        log_path,
        memory_log_path,
    )


def _skip(name: str, reason: str) -> StepResult:
    return StepResult(name, True, True, 0.0, f"skipped ({reason})")


def _head_sha(cwd: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            cwd=cwd,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


# Markers that decide which gate a tree can actually run (#329). Python wins
# when both are present: a Python project that also carries a package.json for
# tooling is still a Python project, while a Node repo with a stray
# pyproject.toml is far rarer.
_PYTHON_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini")
_NODE_MARKERS = ("package.json", "pnpm-workspace.yaml")


def detect_project_kind(root: Path) -> str:
    """``"python"`` | ``"node"`` | ``"unknown"`` for the tree at *root*.

    A bare `tests/` directory counts as Python too: the marker list exists to
    avoid misrouting a real Python project, and refusing one that merely
    packages itself unusually would be a worse failure than the one this
    replaces.
    """
    if any((root / m).exists() for m in _PYTHON_MARKERS) or (root / "tests").is_dir():
        return "python"
    if any((root / m).exists() for m in _NODE_MARKERS):
        return "node"
    return "unknown"


def _non_python_gate(
    kind: str,
    root: Path,
    targeted: list[str] | None,
    report: GateReport,
    env: dict,
    log_dir: Path | None,
) -> GateReport:
    """Gate a tree that has no Python in it (#329).

    The team rule is "`takkub qa-gate` is the ONE entrypoint, never run pytest/
    ruff/lint-imports by hand" — but the gate only knew how to run those three,
    so calling it inside a Node repo died on `No module named pytest` and left
    the specialist to run the project's real commands by hand, with nothing in
    their report to prove they had. Either the rule or the tool had to give;
    the tool gives. `verify.detect_stack` already knows how to read a Node
    project's own scripts, so this delegates there rather than inventing a
    second detector that would drift from it.
    """
    if kind != "node":
        report.steps.append(
            StepResult(
                "detect",
                False,
                False,
                0.0,
                f"refuse: no Python ({'/'.join(_PYTHON_MARKERS)}) and no Node "
                f"({'/'.join(_NODE_MARKERS)}) markers in {root} — qa-gate has no gate "
                "to run for this project. Run this project's own test command.",
            )
        )
        return report

    from .verify import detect_stack

    checks = detect_stack(root)
    if not checks:
        report.steps.append(
            StepResult(
                "detect",
                False,
                False,
                0.0,
                f"refuse: Node project at {root} but nothing to run — no `verify`/"
                "`typecheck`/`test` script, no tsconfig.json (root or workspace), no "
                "eslint config. Add one, or run the project's own command directly.",
            )
        )
        return report

    report.steps.append(
        StepResult(
            "detect",
            True,
            False,
            0.0,
            f"node project — delegating to: {', '.join(c.name for c in checks)}",
        )
    )
    if targeted:
        # Never silently swallow them: a Node gate has no generic way to map
        # source paths onto a test selection, and pretending otherwise is how
        # a "targeted" run quietly became a full one with no one told. The
        # typecheck in particular always runs whole-project (#368) — tsc
        # can't be narrowed to a path list without losing the cross-file
        # signature drift it exists to catch.
        report.steps.append(
            StepResult(
                "targeted",
                True,
                True,
                0.0,
                "--targeted is Python-only — these paths did NOT narrow anything "
                "(typecheck + test run whole-project): " + " ".join(targeted),
            )
        )

    for index, check in enumerate(checks):
        step = _run_step(check.name, check.cmd, env, check.cwd or root, log_dir)
        report.steps.append(step)
        if not step.ok:
            for rest in checks[index + 1 :]:
                report.steps.append(_skip(rest.name, f"{check.name} failed — fail-fast"))
            break
    return report


def run_gate(
    *,
    targeted: list[str] | None = None,
    v2_flags: bool = False,
    write_report: bool | None = None,
    cwd: Path | None = None,
    exec_prefix: list[str] | None = None,
) -> GateReport:
    """Run the gate once. Default (no `targeted`) is the full-suite tier —
    venv-check -> pytest -> ruff check -> lint-imports, fail-fast, report
    written to docs/qa/. `targeted` is the mid-flight tier (pytest only on
    the given paths, no report file) — team policy: targeted tests mid-flight,
    full suite once at the batch gate.

    `exec_prefix` (#401): e.g. ``["docker", "compose", "exec", "-T",
    "gateway"]`` — delegates every tool invocation to that prefix instead of
    resolving a local `.venv`, for a project whose tests are designed to run
    inside a container rather than the host's own Python. Local venv-check
    and per-tool ENV_GAP detection are both skipped entirely in this mode:
    the exec target is trusted to own its own toolchain, so a missing tool
    there surfaces as a normal step FAIL (real subprocess output), not
    ENV_GAP.
    """
    cwd = cwd or Path.cwd()
    wroot = worktree_root(cwd)
    bin_dir = shared_venv_bin(cwd)
    if write_report is None:
        write_report = targeted is None

    env = dict(os.environ)
    # #349: zero-cost, and the exact difference between "died silently, no
    # clue why" and "a C-level traceback pointing at a line" next time a
    # native abort happens. setdefault so an explicit caller override wins.
    env.setdefault("PYTHONFAULTHANDLER", "1")
    if v2_flags:
        for name in V2_FLAG_ENV_VARS:
            env[name] = "1"

    report = GateReport(v2_flags=v2_flags, targeted=list(targeted) if targeted else None)

    def finish() -> GateReport:
        if write_report:
            report.report_path = _maybe_write_report(wroot, report)
        return report

    log_dir = None
    if write_report:
        log_dir = wroot / "runtime" / "exports" / f"qa-gate-{time.strftime('%Y%m%d-%H%M%S')}"

    kind = detect_project_kind(wroot)
    if kind != "python":
        _non_python_gate(kind, wroot, targeted, report, env, log_dir)
        return finish()

    if exec_prefix is not None:
        vc = StepResult(
            "venv-check",
            True,
            False,
            0.0,
            f"--exec {' '.join(exec_prefix)!r} — local venv resolution and ENV_GAP "
            "detection both skipped (#401); the exec target owns its own toolchain",
        )
    else:
        vc = _venv_check(bin_dir)
    report.steps.append(vc)
    if not vc.ok:
        report.steps.append(_skip("pytest", "venv-check failed"))
        report.steps.append(_skip("ruff", "venv-check failed"))
        report.steps.append(_skip("lint-imports", "venv-check failed"))
        return finish()

    py = _resolve_tool(bin_dir, "python") or sys.executable

    # #401: only the LOCAL venv path ever produces an ENV_GAP — --exec trusts
    # its target to have everything it needs (a missing tool there is a real
    # subprocess failure, surfaced normally).
    pytest_missing = (
        exec_prefix is None and bin_dir is not None and _resolve_tool(bin_dir, "pytest") is None
    )
    if pytest_missing:
        fallback_cmd = _unittest_discover_cmd(py, wroot)
        if fallback_cmd is None:
            pytest_step = _env_gap_step(
                "pytest",
                extra=" (no tests/test_*.py found for a `python -m unittest discover` fallback either)",
            )
        else:
            pytest_step = _run_step("pytest", fallback_cmd, env, wroot, log_dir)
            note = "[unittest discover fallback — pytest not installed, #401]"
            if targeted:
                note += " (unnarrowed — ran the whole tests/ tree, not just --targeted paths)"
            pytest_step.detail = f"{note} {pytest_step.detail}"
    else:
        pytest_step = _run_step(
            "pytest", _pytest_cmd(bin_dir, py, targeted, exec_prefix), env, wroot, log_dir
        )
    report.steps.append(pytest_step)
    if not pytest_step.ok:
        report.steps.append(_skip("ruff", "pytest failed — fail-fast"))
        report.steps.append(_skip("lint-imports", "pytest failed — fail-fast"))
        return finish()

    if targeted:
        # Mid-flight tier stops here by design — ruff/lint-imports are a
        # full-gate concern (once per wave), not a per-subtask one.
        return finish()

    ruff_missing = (
        exec_prefix is None and bin_dir is not None and _resolve_tool(bin_dir, "ruff") is None
    )
    if ruff_missing:
        ruff_step = _env_gap_step("ruff")
    else:
        ruff_step = _run_step("ruff", _ruff_cmd(bin_dir, py, exec_prefix), env, wroot, log_dir)
    report.steps.append(ruff_step)
    if not ruff_step.ok:
        report.steps.append(_skip("lint-imports", "ruff failed — fail-fast"))
        return finish()

    li_missing = (
        exec_prefix is None
        and bin_dir is not None
        and _resolve_tool(bin_dir, "lint-imports") is None
    )
    if li_missing:
        li_step = _env_gap_step("lint-imports")
    else:
        li_step = _run_step(
            "lint-imports", _lint_imports_cmd(bin_dir, exec_prefix), env, wroot, log_dir
        )
    report.steps.append(li_step)

    return finish()


def _step_result_label(s: StepResult) -> str:
    if s.skipped:
        return "skip"
    if s.env_gap:  # #401 — distinct from PASS: nothing actually ran
        return "GAP"
    return "PASS" if s.ok else "FAIL"


def render_table(report: GateReport) -> str:
    lines = ["step         result  time     detail", "-" * 72]
    for s in report.steps:
        result = _step_result_label(s)
        lines.append(f"{s.name:<12} {result:<7} {s.seconds:>6.1f}s  {s.detail[:100]}")
        if s.log_path:
            lines.append(f"             log: {s.log_path}")
        if s.memory_log_path:
            lines.append(f"             memory log: {s.memory_log_path}")
    lines.append("-" * 72)
    gate_line = "GATE: " + ("PASS" if report.ok else "FAIL")
    if report.env_gap:
        gate_line += "  (env gap present — see #401, not a code/test failure)"
    lines.append(gate_line)
    if report.report_path:
        lines.append(f"report: {report.report_path}")
    return "\n".join(lines)


def render_report_md(report: GateReport, head: str) -> str:
    lines = [f"# QA gate — {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    tag = f"**HEAD:** `{head}`"
    if report.v2_flags:
        tag += "  ·  **V2 flags ON:** " + ", ".join(V2_FLAG_ENV_VARS)
    lines.append(tag)
    lines.append("")
    result_heading = "PASS" if report.ok else "FAIL"
    if report.env_gap:
        result_heading += " (env gap present — see #401, not a code/test failure)"
    lines.append(f"## Result: {result_heading}")
    lines.append("")
    lines.append("| step | result | time | detail |")
    lines.append("|---|---|---|---|")
    for s in report.steps:
        result = _step_result_label(s)
        detail = s.detail.replace("|", "\\|").replace("\n", " ")[:200]
        lines.append(f"| {s.name} | {result} | {s.seconds:.1f}s | {detail} |")
        if s.log_path:
            lines.append(f"| | | | log: `{s.log_path}` |")
        if s.memory_log_path:
            lines.append(f"| | | | memory log: `{s.memory_log_path}` |")
    return "\n".join(lines) + "\n"


def _maybe_write_report(wroot: Path, report: GateReport) -> Path | None:
    if report.targeted:
        return None
    docs_dir = wroot / "docs" / "qa"
    docs_dir.mkdir(parents=True, exist_ok=True)
    suffix = "-v2flags" if report.v2_flags else ""
    path = docs_dir / f"{time.strftime('%Y-%m-%d-%H%M%S')}-qa-gate{suffix}.md"
    path.write_text(render_report_md(report, _head_sha(wroot)), encoding="utf-8")
    return path
