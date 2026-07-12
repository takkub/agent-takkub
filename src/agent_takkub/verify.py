"""takkub verify — auto-detect stack + run lint/test gate."""

from __future__ import annotations

import glob
import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Check:
    name: str
    cmd: list[str]
    stack: str  # "python" | "node"


@dataclass
class CheckResult:
    check: Check
    exit_code: int
    stdout_tail: str
    stderr_tail: str
    duration_ms: float


@dataclass
class VerifyResult:
    checks: list[CheckResult] = field(default_factory=list)
    all_passed: bool = True


def _tail(text: str, lines: int = 50) -> str:
    """Return last `lines` lines of text."""
    return "\n".join(text.splitlines()[-lines:])


def detect_stack(cwd: Path) -> list[Check]:
    """Return checks appropriate for the project in cwd."""
    checks: list[Check] = []

    if (cwd / "pyproject.toml").exists():
        if (cwd / "tests").is_dir():
            checks.append(
                Check(
                    name="pytest",
                    cmd=["python", "-m", "pytest", "tests/", "-x", "--tb=short"],
                    stack="python",
                )
            )
        checks.append(
            Check(
                name="ruff-lint",
                cmd=["python", "-m", "ruff", "check", "src", "tests"],
                stack="python",
            )
        )
        checks.append(
            Check(
                name="ruff-format",
                cmd=["python", "-m", "ruff", "format", "--check", "src", "tests"],
                stack="python",
            )
        )

    if (cwd / "package.json").exists():
        npm = shutil.which("npm") or "npm"
        npx = shutil.which("npx") or "npx"
        try:
            pkg = json.loads((cwd / "package.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pkg = {}

        scripts = pkg.get("scripts", {})
        if "test" in scripts:
            checks.append(Check(name="npm-test", cmd=[npm, "test"], stack="node"))

        if (cwd / "tsconfig.json").exists():
            checks.append(Check(name="tsc", cmd=[npx, "tsc", "--noEmit"], stack="node"))

        eslintrc_patterns = [
            ".eslintrc",
            ".eslintrc.js",
            ".eslintrc.json",
            ".eslintrc.yaml",
            ".eslintrc.yml",
        ]
        if any((cwd / p).exists() for p in eslintrc_patterns) or glob.glob(str(cwd / ".eslintrc*")):
            checks.append(Check(name="eslint", cmd=[npx, "eslint", "."], stack="node"))

    return checks


def run_checks(checks: list[Check], cwd: Path, timeout: int = 600) -> VerifyResult:
    """Run each check subprocess and collect results."""
    from ._win_console import SUBPROCESS_NO_WINDOW

    results: list[CheckResult] = []
    all_passed = True

    for check in checks:
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                check.cmd,
                cwd=str(cwd),
                capture_output=True,
                shell=False,
                timeout=timeout,
                creationflags=SUBPROCESS_NO_WINDOW,
            )
            exit_code = proc.returncode
            stdout = proc.stdout.decode("utf-8", errors="replace")
            stderr = proc.stderr.decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            exit_code = -1
            stdout = ""
            stderr = f"timeout after {timeout}s"
        except Exception as exc:
            exit_code = -1
            stdout = ""
            stderr = str(exc)

        duration_ms = (time.monotonic() - t0) * 1000
        if exit_code != 0:
            all_passed = False

        results.append(
            CheckResult(
                check=check,
                exit_code=exit_code,
                stdout_tail=_tail(stdout),
                stderr_tail=_tail(stderr),
                duration_ms=duration_ms,
            )
        )

    return VerifyResult(checks=results, all_passed=all_passed)


def format_summary(result: VerifyResult) -> str:
    """One-line per check: name: PASS/FAIL (Xs)."""
    if not result.checks:
        return "No checks configured for this stack."
    lines = []
    for cr in result.checks:
        status = "PASS" if cr.exit_code == 0 else "FAIL"
        lines.append(f"{cr.check.name}: {status} ({cr.duration_ms / 1000:.1f}s)")
    return "\n".join(lines)
