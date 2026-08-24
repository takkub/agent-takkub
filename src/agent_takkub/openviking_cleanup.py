"""`takkub cleanup openviking` (docs/plans/remove-openviking-2026-08-24/
07_RUNTIME_DATA_MIGRATION.md). OpenViking itself was removed from the
product — this module only helps a v1.5.0 user reclaim the managed-runtime
install a previous version left under
`~/.agent-takkub/services/openviking/` (fixed home, independent of
`config.DATA_HOME` — see the old `openviking/installer.py`'s docstring for
why: a managed service install must not live inside a repo checkout a `git
clean`/`rm -rf worktree` could sweep away).

Never touches anything this cockpit did not itself create: `stop_owned_
process` only ever kills the PID `PID_FILE` names (written exclusively by
this cockpit's own now-removed process manager), and that file — never
whatever happens to be listening on port 1933 — is the sole ownership
proof. An externally-run OpenViking instance wrote no such file and is
never touched.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

SERVICES_ROOT = Path.home() / ".agent-takkub" / "services"
OPENVIKING_HOME = SERVICES_ROOT / "openviking"
VENV_DIR = OPENVIKING_HOME / "venv"
CONFIG_DIR = OPENVIKING_HOME / "config"
DATA_DIR = OPENVIKING_HOME / "data"
STATE_FILE = OPENVIKING_HOME / "state.json"
LOG_DIR = OPENVIKING_HOME / "logs"
# Same path the old `openviking/process.py` wrote to — kept identical so
# this module can still recognise (and safely reap) a PID file left behind
# by a v1.5.0 install.
PID_FILE = OPENVIKING_HOME / "openviking_pid.json"


def exists() -> bool:
    return OPENVIKING_HOME.is_dir()


def _dir_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _owned_running_pid() -> tuple[bool, int | None]:
    """True + pid only when `PID_FILE` names a process that is still
    alive. Never raises: any read/parse/psutil failure reads as "nothing to
    stop", the same fail-closed posture the old `process.is_process_alive`
    used."""
    try:
        if not PID_FILE.exists():
            return False, None
        data = json.loads(PID_FILE.read_text(encoding="utf-8"))
        pid = data.get("pid")
        if not isinstance(pid, int):
            return False, None
        import psutil

        if not psutil.pid_exists(pid):
            return False, None
        return True, pid
    except Exception:
        return False, None


@dataclass(frozen=True, slots=True)
class CleanupReport:
    exists: bool
    path: str
    size_bytes: int
    owned_pid: int | None


def report() -> CleanupReport:
    owned, pid = _owned_running_pid()
    return CleanupReport(
        exists=exists(),
        path=str(OPENVIKING_HOME),
        size_bytes=_dir_size(OPENVIKING_HOME),
        owned_pid=pid if owned else None,
    )


def stop_owned_process() -> None:
    """Kill only the process `PID_FILE` names — never anything else,
    including whatever else may be listening on port 1933."""
    owned, pid = _owned_running_pid()
    if not owned or pid is None:
        return
    from .pty_session import _tree_kill

    _tree_kill(pid)
    PID_FILE.unlink(missing_ok=True)


def remove(*, purge_data: bool) -> None:
    """Caller's job to `stop_owned_process()` first. Always removes the
    managed venv/log/state; `config/`/`data/` (indexed knowledge) are only
    removed when *purge_data* is True — matches the old installer's
    `uninstall(remove_data=...)` default of keeping data unless asked."""
    shutil.rmtree(VENV_DIR, ignore_errors=True)
    shutil.rmtree(LOG_DIR, ignore_errors=True)
    STATE_FILE.unlink(missing_ok=True)
    PID_FILE.unlink(missing_ok=True)
    if purge_data:
        shutil.rmtree(CONFIG_DIR, ignore_errors=True)
        shutil.rmtree(DATA_DIR, ignore_errors=True)
    try:
        if OPENVIKING_HOME.is_dir() and not any(OPENVIKING_HOME.iterdir()):
            OPENVIKING_HOME.rmdir()
    except OSError:
        pass


__all__ = [
    "OPENVIKING_HOME",
    "CleanupReport",
    "exists",
    "remove",
    "report",
    "stop_owned_process",
]
