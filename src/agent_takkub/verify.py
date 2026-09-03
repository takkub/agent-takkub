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
    # Where to run `cmd`; None = the project root the check was detected in.
    # Set for per-workspace-package typechecks in a monorepo without a root
    # tsconfig (#368) so `tsc` resolves that package's own node_modules.
    cwd: Path | None = None


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
        checks.extend(node_checks(cwd))

    return checks


# ---------------------------------------------------------------------------
# Node detection (#329 delegate, #368 typecheck-always)
# ---------------------------------------------------------------------------

# Lockfile → package manager. Checked in this order; first hit wins. Never
# hardcode npm: `npm run verify` in a pnpm workspace either fails outright or
# resolves a different dependency tree than the one CI uses (#368).
_LOCKFILE_PM: tuple[tuple[str, str], ...] = (
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("bun.lockb", "bun"),
    ("bun.lock", "bun"),
    ("package-lock.json", "npm"),
    ("npm-shrinkwrap.json", "npm"),
)


def detect_package_manager(cwd: Path, pkg: dict | None = None) -> str:
    """``"pnpm"`` | ``"yarn"`` | ``"bun"`` | ``"npm"`` for the project at *cwd*.

    Lockfile first (what's actually installed), then package.json's
    `packageManager` field (corepack pin), then npm as the last resort.
    """
    for lock, pm in _LOCKFILE_PM:
        if (cwd / lock).exists():
            return pm
    pin = (pkg or {}).get("packageManager")
    if isinstance(pin, str):
        name = pin.split("@", 1)[0].strip()
        if name in {"pnpm", "yarn", "bun", "npm"}:
            return name
    return "npm"


def _exe(name: str) -> str:
    """Resolve a Node-ecosystem launcher cross-platform: on Windows the
    global installs are `.cmd` shims that `shell=False` subprocess can't
    find by bare name, so probe that spelling first."""
    return shutil.which(f"{name}.cmd") or shutil.which(name) or name


def pm_run(pm: str, script: str) -> list[str]:
    """`<pm> run <script>` for the detected package manager."""
    return [_exe(pm), "run", script]


def pm_exec(pm: str, *args: str) -> list[str]:
    """Run a locally-installed binary (`tsc`, `eslint`) through the package
    manager's own exec path so it resolves from node_modules/.bin."""
    if pm == "pnpm":
        return [_exe("pnpm"), "exec", *args]
    if pm == "yarn":
        return [_exe("yarn"), *args]
    if pm == "bun":
        return [_exe("bunx"), *args]
    return [_exe("npx"), "--no-install", *args]


def load_package_json(cwd: Path) -> dict:
    """Parsed `package.json` at *cwd*, or `{}` if absent/unparsable — the one
    place this read happens so `node_checks` and the Prisma gate (#469) never
    drift on how a malformed file is tolerated."""
    try:
        pkg = json.loads((cwd / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pkg = {}
    return pkg if isinstance(pkg, dict) else {}


def _workspace_globs(cwd: Path, pkg: dict) -> list[str]:
    """Workspace package globs from pnpm-workspace.yaml (`packages:` list) or
    package.json `workspaces` (array, or `{"packages": [...]}`). Negations
    (`!…`) are dropped — they only ever exclude, and an over-inclusive
    typecheck is the safe failure here."""
    globs: list[str] = []
    ws_yaml = cwd / "pnpm-workspace.yaml"
    if ws_yaml.exists():
        try:
            in_packages = False
            for raw in ws_yaml.read_text(encoding="utf-8").splitlines():
                line = raw.split("#", 1)[0].rstrip()
                if not line.strip():
                    continue
                if not line.startswith((" ", "\t", "-")):
                    in_packages = line.strip().rstrip(":") == "packages"
                    continue
                if in_packages and line.strip().startswith("- "):
                    globs.append(line.strip()[2:].strip().strip("'\""))
        except OSError:
            pass
    ws = pkg.get("workspaces")
    if isinstance(ws, dict):
        ws = ws.get("packages")
    if isinstance(ws, list):
        globs.extend(str(g) for g in ws if isinstance(g, str))
    return [g for g in globs if g and not g.startswith("!")]


def workspace_dirs(cwd: Path, pkg: dict) -> list[Path]:
    """Every workspace package directory (relative to *cwd*), regardless of
    what it contains — the base glob other detectors (tsconfig, prisma
    schema, #469) filter further themselves."""
    found: list[Path] = []
    for g in _workspace_globs(cwd, pkg):
        for d in sorted(cwd.glob(g)):
            if not d.is_dir() or "node_modules" in d.parts:
                continue
            found.append(d.relative_to(cwd))
    return found


def workspace_tsconfigs(cwd: Path, pkg: dict) -> list[Path]:
    """Every workspace package directory (relative to *cwd*) that carries its
    own tsconfig.json — the monorepo shape where the root has none, so a root
    `tsc --noEmit` would typecheck nothing (#368: lottery/turbo)."""
    return [
        d
        for d in workspace_dirs(cwd, pkg)
        if (cwd / d / "tsconfig.json").exists() and (cwd / d / "package.json").exists()
    ]


def node_checks(cwd: Path) -> list[Check]:
    """The Node gate (#329 + #368). Order matters — typecheck runs BEFORE test
    because the whole point is that vitest/jest transpile through esbuild and
    never see a type error: a spec written against an old signature passes
    locally and only fails at CI. Detection (first hit wins):

      1. `verify` script            → `<pm> run verify`   (project's own combo)
      2. `typecheck` script         → `<pm> run typecheck` + `test`
      3. root tsconfig.json         → `<pm exec> tsc -p tsconfig.json --noEmit` + `test`
         no root tsconfig, monorepo → one tsc per workspace package that has one
      4. no TypeScript at all       → `test` alone, as before
    Then `lint` (eslint) if an eslint config exists.
    """
    pkg = load_package_json(cwd)
    scripts = pkg.get("scripts") or {}
    if not isinstance(scripts, dict):
        scripts = {}
    pm = detect_package_manager(cwd, pkg)
    checks: list[Check] = []

    if "verify" in scripts:
        checks.append(Check(name="verify", cmd=pm_run(pm, "verify"), stack="node"))
    else:
        if "typecheck" in scripts:
            checks.append(Check(name="typecheck", cmd=pm_run(pm, "typecheck"), stack="node"))
        elif (cwd / "tsconfig.json").exists():
            checks.append(
                Check(
                    name="typecheck",
                    cmd=pm_exec(pm, "tsc", "-p", "tsconfig.json", "--noEmit"),
                    stack="node",
                )
            )
        else:
            for rel in workspace_tsconfigs(cwd, pkg):
                checks.append(
                    Check(
                        name=f"typecheck:{rel.as_posix()}",
                        cmd=pm_exec(pm, "tsc", "-p", "tsconfig.json", "--noEmit"),
                        stack="node",
                        cwd=cwd / rel,
                    )
                )
        if "test" in scripts:
            checks.append(Check(name="test", cmd=pm_run(pm, "test"), stack="node"))

    eslintrc_patterns = [
        ".eslintrc",
        ".eslintrc.js",
        ".eslintrc.json",
        ".eslintrc.yaml",
        ".eslintrc.yml",
        "eslint.config.js",
        "eslint.config.mjs",
        "eslint.config.cjs",
        "eslint.config.ts",
    ]
    if any((cwd / p).exists() for p in eslintrc_patterns) or glob.glob(str(cwd / ".eslintrc*")):
        checks.append(Check(name="lint", cmd=pm_exec(pm, "eslint", "."), stack="node"))
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
                cwd=str(check.cwd or cwd),
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
