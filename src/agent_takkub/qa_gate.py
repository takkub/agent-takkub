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
import subprocess
import sys
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


@dataclass
class StepResult:
    name: str
    ok: bool
    skipped: bool
    seconds: float
    detail: str
    returncode: int | None = None
    log_path: Path | None = None


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
    def exit_code(self) -> int:
        for s in self.steps:
            if not s.skipped and not s.ok:
                return s.returncode if s.returncode else 1
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
    t0 = time.monotonic()
    if bin_dir is None:
        return StepResult(
            "venv-check",
            True,
            False,
            time.monotonic() - t0,
            "no shared .venv found — trusting the running interpreter (CI/fresh install)",
        )
    missing = [
        n for n in ("python", "pytest", "ruff", "lint-imports") if _resolve_tool(bin_dir, n) is None
    ]
    if missing:
        return StepResult(
            "venv-check",
            False,
            False,
            time.monotonic() - t0,
            f"refuse: {bin_dir} missing {', '.join(missing)} — broken/incomplete venv. "
            "Do NOT fall back to system python + PYTHONPATH=src (known footgun, #202).",
        )
    return StepResult("venv-check", True, False, time.monotonic() - t0, f"using {bin_dir}")


def _pytest_cmd(bin_dir: Path | None, py: str, targeted: list[str] | None) -> list[str]:
    exe = _resolve_tool(bin_dir, "pytest")
    base = [exe] if exe else [py, "-m", "pytest"]
    return base + (list(targeted) if targeted else [])


def _ruff_cmd(bin_dir: Path | None, py: str) -> list[str]:
    exe = _resolve_tool(bin_dir, "ruff")
    base = [exe, "check"] if exe else [py, "-m", "ruff", "check"]
    return [*base, "src/", "tests/"]


def _lint_imports_cmd(bin_dir: Path | None) -> list[str]:
    exe = _resolve_tool(bin_dir, "lint-imports")
    return [exe] if exe else ["lint-imports"]


def _run_step(name: str, cmd: list[str], env: dict, cwd: Path, log_dir: Path | None) -> StepResult:
    t0 = time.monotonic()
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
        return StepResult(
            name, False, False, time.monotonic() - t0, f"refuse: {type(e).__name__}: {e}"
        )
    elapsed = time.monotonic() - t0
    output = (proc.stdout or "") + (proc.stderr or "")
    log_path = None
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{name}.log"
        log_path.write_text(output, encoding="utf-8")
    tail_lines = [ln for ln in output.strip().splitlines() if ln.strip()][-6:]
    detail = " / ".join(tail_lines) if tail_lines else "(no output)"
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
        print(f"\n----- {name} failure output (excerpt) -----")
        for ln in excerpt[-200:]:
            print(ln)
        print(f"----- end {name} failure output -----\n")
    # proc.returncode is read straight off the completed subprocess — never
    # inferred from a shell pipe's own $? (the #234-adjacent footgun this
    # gate exists to structurally rule out).
    return StepResult(name, proc.returncode == 0, False, elapsed, detail, proc.returncode, log_path)


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


def run_gate(
    *,
    targeted: list[str] | None = None,
    v2_flags: bool = False,
    write_report: bool | None = None,
    cwd: Path | None = None,
) -> GateReport:
    """Run the gate once. Default (no `targeted`) is the full-suite tier —
    venv-check -> pytest -> ruff check -> lint-imports, fail-fast, report
    written to docs/qa/. `targeted` is the mid-flight tier (pytest only on
    the given paths, no report file) — team policy: targeted tests mid-flight,
    full suite once at the batch gate."""
    cwd = cwd or Path.cwd()
    wroot = worktree_root(cwd)
    bin_dir = shared_venv_bin(cwd)
    if write_report is None:
        write_report = targeted is None

    env = dict(os.environ)
    if v2_flags:
        for name in V2_FLAG_ENV_VARS:
            env[name] = "1"

    report = GateReport(v2_flags=v2_flags, targeted=list(targeted) if targeted else None)

    def finish() -> GateReport:
        if write_report:
            report.report_path = _maybe_write_report(wroot, report)
        return report

    vc = _venv_check(bin_dir)
    report.steps.append(vc)
    if not vc.ok:
        report.steps.append(_skip("pytest", "venv-check failed"))
        report.steps.append(_skip("ruff", "venv-check failed"))
        report.steps.append(_skip("lint-imports", "venv-check failed"))
        return finish()

    log_dir = None
    if write_report:
        log_dir = wroot / "runtime" / "exports" / f"qa-gate-{time.strftime('%Y%m%d-%H%M%S')}"

    py = _resolve_tool(bin_dir, "python") or sys.executable

    pytest_step = _run_step("pytest", _pytest_cmd(bin_dir, py, targeted), env, wroot, log_dir)
    report.steps.append(pytest_step)
    if not pytest_step.ok:
        report.steps.append(_skip("ruff", "pytest failed — fail-fast"))
        report.steps.append(_skip("lint-imports", "pytest failed — fail-fast"))
        return finish()

    if targeted:
        # Mid-flight tier stops here by design — ruff/lint-imports are a
        # full-gate concern (once per wave), not a per-subtask one.
        return finish()

    ruff_step = _run_step("ruff", _ruff_cmd(bin_dir, py), env, wroot, log_dir)
    report.steps.append(ruff_step)
    if not ruff_step.ok:
        report.steps.append(_skip("lint-imports", "ruff failed — fail-fast"))
        return finish()

    li_step = _run_step("lint-imports", _lint_imports_cmd(bin_dir), env, wroot, log_dir)
    report.steps.append(li_step)

    return finish()


def render_table(report: GateReport) -> str:
    lines = ["step         result  time     detail", "-" * 72]
    for s in report.steps:
        result = "skip" if s.skipped else ("PASS" if s.ok else "FAIL")
        lines.append(f"{s.name:<12} {result:<7} {s.seconds:>6.1f}s  {s.detail[:100]}")
        if s.log_path:
            lines.append(f"             log: {s.log_path}")
    lines.append("-" * 72)
    lines.append("GATE: " + ("PASS" if report.ok else "FAIL"))
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
    lines.append(f"## Result: {'PASS' if report.ok else 'FAIL'}")
    lines.append("")
    lines.append("| step | result | time | detail |")
    lines.append("|---|---|---|---|")
    for s in report.steps:
        result = "skip" if s.skipped else ("PASS" if s.ok else "FAIL")
        detail = s.detail.replace("|", "\\|").replace("\n", " ")[:200]
        lines.append(f"| {s.name} | {result} | {s.seconds:.1f}s | {detail} |")
        if s.log_path:
            lines.append(f"| | | | log: `{s.log_path}` |")
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
