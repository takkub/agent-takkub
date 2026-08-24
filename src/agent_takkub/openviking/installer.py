"""installer.py — managed local install of OpenViking (Wave 1,
`04_NO_DOCKER_INSTALL.md`). No Docker: a dedicated venv this codebase owns,
never mixed into Takkub's own PyQt venv, and OpenViking's (AGPL) source is
never vendored into this (MIT) repo — `pip install` only.

Layout (fixed home, independent of dev-checkout vs. installed-build
`config.DATA_HOME` — a managed service install must not live inside a repo
checkout that a `git clean`/`rm -rf worktree` could sweep away):

    Windows:      %USERPROFILE%\\.agent-takkub\\services\\openviking\\
    macOS/Linux:  ~/.agent-takkub/services/openviking/
        venv/       — dedicated Python env (`python -m venv`, pip-installed here)
        config/     — `ov.conf` (Setup Wizard's job, Wave 2 — this module only
                      owns the path, not the content)
        state.json  — installed version + timestamp, written by `ensure_installed`
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .._win_console import SUBPROCESS_NO_WINDOW

_log = logging.getLogger(__name__)

SERVICES_ROOT = Path.home() / ".agent-takkub" / "services"
OPENVIKING_HOME = SERVICES_ROOT / "openviking"
VENV_DIR = OPENVIKING_HOME / "venv"
CONFIG_DIR = OPENVIKING_HOME / "config"
CONFIG_FILE = CONFIG_DIR / "ov.conf"
STATE_FILE = OPENVIKING_HOME / "state.json"
# Setup Wizard points ov.conf's `storage.workspace` here (Wave 2,
# `openviking_setup_dialog.py`) — an absolute path so the child process's
# indexed data lands in the same place regardless of Cockpit's own cwd.
DATA_DIR = OPENVIKING_HOME / "data"

# Tested against the live upstream API today (docs/audit/2026-08-24-
# openviking-managed-local-phase0.md) — no exact version pin frozen yet, kept
# as one constant so a future wave can tighten it (e.g. "openviking==0.5.*")
# without touching call sites.
PACKAGE_SPEC = "openviking"

_VENV_TIMEOUT_S = 120.0
_PIP_TIMEOUT_S = 600.0
_DOCTOR_TIMEOUT_S = 30.0
_VERSION_PROBE_TIMEOUT_S = 15.0


class InstallerError(RuntimeError):
    pass


def _venv_python(venv: Path) -> Path:
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def server_executable() -> Path:
    name = "openviking-server.exe" if sys.platform == "win32" else "openviking-server"
    sub = "Scripts" if sys.platform == "win32" else "bin"
    return VENV_DIR / sub / name


def is_installed() -> bool:
    return server_executable().is_file()


def _run(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=SUBPROCESS_NO_WINDOW,
        timeout=timeout,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _create_venv(venv: Path) -> None:
    venv.parent.mkdir(parents=True, exist_ok=True)
    proc = _run([sys.executable, "-m", "venv", str(venv)], timeout=_VENV_TIMEOUT_S)
    if proc.returncode != 0:
        raise InstallerError(f"failed to create OpenViking venv: {proc.stdout}")


def _pip_install(venv: Path) -> None:
    python = _venv_python(venv)
    proc = _run(
        [str(python), "-m", "pip", "install", "--upgrade", PACKAGE_SPEC],
        timeout=_PIP_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise InstallerError(f"pip install {PACKAGE_SPEC} failed: {proc.stdout}")


def _installed_version(venv: Path) -> str | None:
    python = _venv_python(venv)
    try:
        proc = _run(
            [str(python), "-c", "import importlib.metadata as m; print(m.version('openviking'))"],
            timeout=_VERSION_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    version = proc.stdout.strip().splitlines()[-1].strip()
    return version or None


def _verify(venv: Path) -> str | None:
    """`openviking-server doctor` runs its own config/python/provider/disk
    checks without needing a running server — used here purely to prove the
    freshly-installed binary actually launches. Its own exit code/output
    format isn't a frozen contract this module parses, so a non-zero doctor
    exit (e.g. no config yet — Setup Wizard hasn't run) does not fail the
    install; only the binary being entirely absent does."""
    exe = server_executable()
    if not exe.is_file():
        raise InstallerError(f"openviking-server executable not found after install: {exe}")
    try:
        _run([str(exe), "doctor"], timeout=_DOCTOR_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise InstallerError(f"openviking-server doctor timed out: {exc}") from exc
    return _installed_version(venv)


def _write_state(version: str | None) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": version, "installed_at": time.time()}
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(STATE_FILE)


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def ensure_installed(*, force: bool = False) -> bool:
    """Idempotent: create venv, pip install, verify the binary launches,
    record installed version + timestamp. Returns True on success; raises
    `InstallerError` on any failure — callers (`manager.py`) are expected to
    catch it and fail-open rather than let it propagate. Blocking I/O only —
    never call from the Qt main thread."""
    if is_installed() and not force:
        return True
    try:
        _create_venv(VENV_DIR)
        _pip_install(VENV_DIR)
        version = _verify(VENV_DIR)
    except subprocess.TimeoutExpired as exc:
        raise InstallerError(f"installer step timed out: {exc}") from exc
    _write_state(version)
    return True


def uninstall(*, remove_data: bool = False) -> None:
    """Settings UI "Remove" (`08_SETTINGS_UI.md`). Caller's responsibility to
    stop any running managed process first — this module only owns paths,
    never process lifecycle (see module docstring). Always removes the venv
    + install-state marker; ov.conf and any indexed data under `DATA_DIR`
    are only removed when *remove_data* is True (a separate confirmation in
    the UI, defaulting to not-removed)."""
    shutil.rmtree(VENV_DIR, ignore_errors=True)
    STATE_FILE.unlink(missing_ok=True)
    if remove_data:
        shutil.rmtree(CONFIG_DIR, ignore_errors=True)
        shutil.rmtree(DATA_DIR, ignore_errors=True)
