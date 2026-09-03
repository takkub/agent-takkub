"""Smoke test against a running stack for the Node qa-gate (#475).

Follow-up to #469's proposal 3: typecheck/test/prisma-drift are all static —
none of them ever talks to a running app, so #469's case 3 (a column renamed
in code but never migrated on a real DB — sync jobs silently no-op because
the unit test mocked the DB) had nothing in qa-gate to catch it. This runs
the project's own smoke script against a stack the caller (devops/qa)
already brought up — it never starts or stops one itself ("ห้ามเทสเปลือง";
booting a stack on every full gate would be far too slow to pay on every
`done`), so no docker-compose file / no running service is a visible skip,
never a FAIL. Opt-in by project shape (a `smoke`/`e2e:smoke`/`test:smoke`
script must exist) and can be turned off entirely with `TAKKUB_QA_SMOKE=0`.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ._win_console import SUBPROCESS_NO_WINDOW
from .verify import load_package_json, pm_run, workspace_dirs

_SMOKE_SCRIPT_NAMES: tuple[str, ...] = ("smoke", "e2e:smoke", "test:smoke")
_COMPOSE_FILENAMES: tuple[str, ...] = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)
_DOCKER_TIMEOUT_S = 15
_DEFAULT_SMOKE_TIMEOUT_S = 300.0


@dataclass
class SmokeFinding:
    ok: bool
    skipped: bool
    detail: str


def _tail(text: str, lines: int = 15) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def find_smoke_script(root: Path, pkg: dict) -> tuple[Path, str] | None:
    """The first (dir, script name) pair carrying a smoke script — root's own
    `package.json` first, then each workspace package, `_SMOKE_SCRIPT_NAMES`
    order within each."""
    scripts = pkg.get("scripts")
    if isinstance(scripts, dict):
        for name in _SMOKE_SCRIPT_NAMES:
            if name in scripts:
                return root, name
    for rel in workspace_dirs(root, pkg):
        wdir = root / rel
        wscripts = load_package_json(wdir).get("scripts")
        if isinstance(wscripts, dict):
            for name in _SMOKE_SCRIPT_NAMES:
                if name in wscripts:
                    return wdir, name
    return None


def _find_compose_file(root: Path) -> Path | None:
    for name in _COMPOSE_FILENAMES:
        candidate = root / name
        if candidate.exists():
            return candidate
    return None


def _stack_is_running(compose_file: Path, env: dict) -> bool:
    """At least one service reported running by `docker compose ps` — never
    raises: no docker daemon, no `docker` binary, and a timeout all just mean
    "not running" here, same as an absent compose file."""
    try:
        proc = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                compose_file.name,
                "ps",
                "--services",
                "--filter",
                "status=running",
            ],
            cwd=str(compose_file.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=_DOCKER_TIMEOUT_S,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    return any(line.strip() for line in proc.stdout.splitlines())


def run_smoke_check(root: Path, pkg: dict, pm: str, env: dict) -> SmokeFinding | None:
    """`None` = nothing to report at all — no smoke script found (the common
    case) or `TAKKUB_QA_SMOKE=0`. Otherwise a running-stack check gates
    whether the script actually runs; a FAIL here is a real gate FAIL (#469
    case 3's whole point — a mocked unit test can't catch this)."""
    if env.get("TAKKUB_QA_SMOKE") == "0":
        return None
    found = find_smoke_script(root, pkg)
    if found is None:
        return None
    script_dir, script_name = found

    compose_file = _find_compose_file(root)
    if compose_file is None or not _stack_is_running(compose_file, env):
        return SmokeFinding(
            True,
            True,
            f"{script_name}: stack ไม่ได้รัน — devops ยก stack ก่อนถ้าต้องการ smoke",
        )

    try:
        timeout_s = float(env.get("TAKKUB_QA_SMOKE_TIMEOUT_S", _DEFAULT_SMOKE_TIMEOUT_S))
    except ValueError:
        timeout_s = _DEFAULT_SMOKE_TIMEOUT_S

    try:
        proc = subprocess.run(
            pm_run(pm, script_name),
            cwd=str(script_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout_s,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return SmokeFinding(False, False, f"{script_name} timed out after {timeout_s:.0f}s")
    except OSError as e:
        return SmokeFinding(False, False, f"{script_name} refuse: {type(e).__name__}: {e}")

    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        return SmokeFinding(True, False, f"{script_name} passed: {_tail(output)}")
    return SmokeFinding(
        False, False, f"{script_name} FAILED (exit {proc.returncode}): {_tail(output)}"
    )
