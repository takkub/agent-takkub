"""Docker Compose management per project — isolation + non-blocking operations.

Rules enforced here:
- Every command injects COMPOSE_PROJECT_NAME='<slug>-cockpit' to prevent
  stack collisions across projects sharing the same service names.
- up() always uses -d (detach). Never blocks the caller.
- logs() uses --tail=N, never --follow (no streaming block).
- Every subprocess.run has an explicit timeout.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ._win_console import SUBPROCESS_NO_WINDOW

_COMPOSE_FILENAMES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_DEFAULT_TIMEOUT = 30  # seconds


def _project_slug(project_path: Path) -> str:
    """Derive a stable, safe COMPOSE_PROJECT_NAME from the directory name.

    Appending '-cockpit' scopes the stack so two projects whose folders
    share the same name (e.g. 'api') produce distinct Compose namespaces
    and never clobber each other's containers.
    """
    name = project_path.resolve().name.lower()
    slug = _SLUG_RE.sub("-", name).strip("-") or "project"
    return f"{slug}-cockpit"


def detect_compose(project_path: Path | str) -> Path | None:
    """Return the compose file if one exists in *project_path*, else None.

    Checks all four common filename variants in priority order.
    """
    p = Path(project_path)
    for name in _COMPOSE_FILENAMES:
        candidate = p / name
        if candidate.is_file():
            return candidate
    return None


def _base_env(project_path: Path) -> dict[str, str]:
    """OS env copy with COMPOSE_PROJECT_NAME injected for isolation."""
    env = dict(os.environ)
    env["COMPOSE_PROJECT_NAME"] = _project_slug(project_path)
    return env


def _run(
    args: list[str],
    *,
    project_path: Path,
    timeout: int = _DEFAULT_TIMEOUT,
) -> tuple[bool, str]:
    """Run a docker compose sub-command in *project_path*.

    Returns ``(ok, output_or_error_message)``. Never raises.
    """
    try:
        proc = subprocess.run(
            args,
            cwd=str(project_path),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            encoding="utf-8",
            errors="replace",
            env=_base_env(project_path),
            creationflags=SUBPROCESS_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return False, f"docker compose timed out after {timeout}s"
    except FileNotFoundError:
        return False, "docker not found on PATH (is Docker Desktop installed?)"
    except Exception as e:
        return False, f"docker compose error: {e}"

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "docker compose failed").strip()
        return False, err
    return True, (proc.stdout or "").strip()


def up(project: str, path: Path | str, *, timeout: int = _DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """Start compose services detached (-d). Returns (ok, message).

    Never blocks — detach is mandatory. Callers that need to wait for
    services to be healthy should poll `ps()` or use a healthcheck.
    """
    p = Path(path)
    if detect_compose(p) is None:
        return False, f"no compose file found in {p}"
    return _run(["docker", "compose", "up", "-d"], project_path=p, timeout=timeout)


def down(project: str, path: Path | str, *, timeout: int = _DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """Stop and remove compose services. Returns (ok, message)."""
    p = Path(path)
    if detect_compose(p) is None:
        return False, f"no compose file found in {p}"
    return _run(["docker", "compose", "down"], project_path=p, timeout=timeout)


@dataclass
class ServiceHealth:
    name: str
    state: str  # "running", "exited", "restarting", etc.
    health: str  # "healthy", "unhealthy", "starting", "" (no healthcheck defined)


def ps(project: str, path: Path | str, *, timeout: int = _DEFAULT_TIMEOUT) -> list[ServiceHealth]:
    """Return per-service health for the compose stack at *path*.

    Uses ``docker compose ps --format json``. Returns an empty list when
    no services are running or on any error (callers treat empty as
    "stack not up" rather than a hard failure).

    Handles both Docker Compose v2 output shapes:
      - A JSON array (``[{...}, {...}]``)
      - Newline-delimited JSON objects (one per line)
    """
    p = Path(path)
    ok, output = _run(
        ["docker", "compose", "ps", "--format", "json"],
        project_path=p,
        timeout=timeout,
    )
    if not ok or not output.strip():
        return []

    entries: list[dict] = []
    try:
        parsed = json.loads(output)
        entries = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    services: list[ServiceHealth] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("Name") or entry.get("Service") or entry.get("name", "?")
        state = (entry.get("State") or entry.get("state", "")).lower()
        health = (entry.get("Health") or entry.get("health", "")).lower()
        services.append(ServiceHealth(name=name, state=state, health=health))
    return services


def logs(
    project: str,
    path: Path | str,
    *,
    tail: int = 50,
    timeout: int = _DEFAULT_TIMEOUT,
) -> tuple[bool, str]:
    """Fetch the last *tail* log lines (non-blocking). Returns (ok, log_text).

    Uses ``--tail`` only, never ``--follow``, so the subprocess terminates.
    """
    p = Path(path)
    if detect_compose(p) is None:
        return False, f"no compose file found in {p}"
    return _run(
        ["docker", "compose", "logs", f"--tail={tail}"],
        project_path=p,
        timeout=timeout,
    )
