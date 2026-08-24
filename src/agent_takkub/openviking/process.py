"""process.py — OpenVikingProcess: subprocess lifecycle for the managed
local `openviking-server` (Wave 1, `05_PROCESS_LIFECYCLE.md`).

Same technique as `remote/tunnel.py`'s `Tunnel` (Windows Job Object
kill-on-close H-E layer, POSIX process-group + `pty_session._tree_kill`,
`owner_pid`/`owner_create_time` PID file for cross-session orphan reap) —
deliberately duplicated here rather than imported: `remote/` is a
delete-to-uninstall bolt-on (import-linter's `remote-bolt-on-isolation`
contract), while `openviking/` is a permanent feature module that must keep
working after `rm -rf remote/`.

No URL/stdout scraping (unlike `tunnel.py`'s Mode B): `openviking-server`'s
bind address is fully known upfront — `port.py` picks the port, the argv is
built entirely from paths/ints this codebase already controls (never a
user-supplied script), so there's no need for `tunnel.py`'s `cmd /c`/`sh -c`
wrapper either — a direct `Popen(argv, ...)` is enough, and `_tree_kill`
reaps the whole descendant tree by PID regardless of how it was spawned.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .._win_console import SUBPROCESS_NO_WINDOW
from ..core.secrets.redact import redact
from ..pty_session import _tree_kill
from . import credentials
from .installer import OPENVIKING_HOME, server_executable

_log = logging.getLogger(__name__)

_MAX_DRAINED_LINES = 20
_STARTUP_CHECK_S = 0.4

PID_FILE = OPENVIKING_HOME / "openviking_pid.json"
# Settings UI "View Logs" (`08_SETTINGS_UI.md`) reads this — `_last_output`
# alone doesn't survive a restart, so every drained line is also appended
# here. Rotated (not appended-to-unbounded) each time `start()` runs.
LOG_DIR = OPENVIKING_HOME / "logs"
LOG_FILE = LOG_DIR / "openviking.log"
_LOG_ROTATE_BYTES = 5 * 1024 * 1024


class ProcessError(RuntimeError):
    pass


if sys.platform == "win32":

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_void_p),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("RO", "WO", "OO", "RT", "WT", "OT")]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOBOBJECTINFOCLASS_EXTENDED_LIMIT = 9


def _create_kill_on_close_job() -> int | None:
    """Best-effort, never raises — kernel-enforced cleanup for the case this
    process dies (crash/hard-kill) without ever calling `stop()`. See
    `remote/tunnel.py`'s `_create_kill_on_close_job` docstring for the full
    rationale; identical technique, duplicated per this module's docstring."""
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            job,
            _JOBOBJECTINFOCLASS_EXTENDED_LIMIT,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            kernel32.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


def _assign_to_job(job: int, pid: int) -> bool:
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        _PROCESS_ALL_ACCESS = 0x1F0FFF
        handle = kernel32.OpenProcess(_PROCESS_ALL_ACCESS, False, pid)
        if not handle:
            return False
        try:
            return bool(kernel32.AssignProcessToJobObject(job, handle))
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def _spawn(argv: list[str], *, env: dict[str, str] | None = None) -> subprocess.Popen:
    if sys.platform == "win32":
        return subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=SUBPROCESS_NO_WINDOW,
            env=env,
        )
    return subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # own process group, mirrors tunnel.py's 5.2
        creationflags=SUBPROCESS_NO_WINDOW,
        env=env,
    )


def _write_pid_file(pid: int, port: int) -> None:
    """Best-effort — a failure here only means this start() won't be
    reapable if later orphaned, never a reason to fail the start itself.
    Mirrors `remote/tunnel.py`'s `_write_pid_file` exactly, including the
    `owner_pid`/`owner_create_time` PID-reuse guard (#197)."""
    try:
        import psutil

        owner = psutil.Process(os.getpid())
        payload = {
            "pid": pid,
            "port": port,
            "started_at": time.time(),
            "instance_lock_id": str(uuid.uuid4()),
            "owner_pid": os.getpid(),
            "owner_create_time": owner.create_time(),
        }
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PID_FILE.with_suffix(PID_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(PID_FILE)
    except Exception:
        _log.exception("openviking process: failed to write pid file")


def _clear_pid_file_if_matches(pid: int) -> None:
    try:
        if not PID_FILE.exists():
            return
        data = json.loads(PID_FILE.read_text(encoding="utf-8"))
        if data.get("pid") == pid:
            PID_FILE.unlink(missing_ok=True)
    except (OSError, json.JSONDecodeError):
        pass


def reap_orphan_process() -> None:
    """Boot-time cleanup, same shape as `remote.tunnel.reap_orphan_tunnel`
    (#197 pattern): a managed `openviking-server` left running by a cockpit
    session that died without calling `stop()` first. Never touches a
    process this cockpit didn't itself register — best-effort and silent on
    any internal failure."""
    try:
        if not PID_FILE.exists():
            return
        try:
            data = json.loads(PID_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            PID_FILE.unlink(missing_ok=True)
            return
        pid = data.get("pid")
        owner_pid = data.get("owner_pid")
        owner_create_time = data.get("owner_create_time")
        if not isinstance(pid, int):
            PID_FILE.unlink(missing_ok=True)
            return

        import psutil

        if not psutil.pid_exists(pid):
            PID_FILE.unlink(missing_ok=True)
            return

        owner_alive = False
        if isinstance(owner_pid, int) and psutil.pid_exists(owner_pid):
            try:
                owner_alive = psutil.Process(owner_pid).create_time() == owner_create_time
            except psutil.Error:
                owner_alive = False
        if owner_alive:
            return

        _log.warning("openviking process: reaping orphaned pid=%s (owner gone/reused)", pid)
        _tree_kill(pid)
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        _log.exception("openviking process: orphan reaper failed")


def is_process_alive() -> bool:
    """Disk-only liveness check, mirrors `remote.tunnel.is_tunnel_alive`.
    Never raises: any read/parse/psutil failure reads as "not alive"."""
    try:
        if not PID_FILE.exists():
            return False
        data = json.loads(PID_FILE.read_text(encoding="utf-8"))
        pid = data.get("pid")
        if not isinstance(pid, int):
            return False
        import psutil

        return psutil.pid_exists(pid)
    except Exception:
        return False


class OpenVikingProcess:
    """Owns one running `openviking-server` subprocess. `start()`/`stop()`
    only — `manager.py` decides when those fire."""

    def __init__(self, config_path: Path, port: int) -> None:
        self._config_path = config_path
        self._port = port
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._job: int | None = None
        self._last_output: list[str] = []

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    def _own_job_if_windows(self) -> None:
        if sys.platform != "win32" or self._proc is None:
            return
        job = _create_kill_on_close_job()
        if job is None:
            return
        if _assign_to_job(job, self._proc.pid):
            self._job = job
        else:
            _log.warning("openviking process: not assigned to kill-on-close job object")
            try:
                ctypes.windll.kernel32.CloseHandle(job)  # type: ignore[attr-defined]
            except Exception:
                pass

    def start(self) -> None:
        exe = server_executable()
        if not exe.is_file():
            raise ProcessError(f"openviking-server executable not found: {exe}")
        argv = [str(exe), "--config", str(self._config_path), "--port", str(self._port)]
        self._rotate_log_if_large()
        self._proc = _spawn(argv, env=credentials.subprocess_env())
        self._own_job_if_windows()
        self._drain_output()
        self._verify_started()
        if self._proc is not None:
            _write_pid_file(self._proc.pid, self._port)

    @staticmethod
    def _rotate_log_if_large() -> None:
        """Best-effort — a failed rotation only means the log grows past
        `_LOG_ROTATE_BYTES` once, never a reason to fail `start()`."""
        try:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > _LOG_ROTATE_BYTES:
                LOG_FILE.unlink()
        except OSError:
            pass

    def _drain_output(self) -> None:
        """The child's stdout pipe must be drained continuously or
        `openviking-server`'s own log output fills the pipe buffer and
        blocks it — only the last `_MAX_DRAINED_LINES` are kept in memory
        for a same-process startup failure report, but every line is also
        appended to `LOG_FILE` so "View Logs" has something after a
        restart. Every line passes through `redact()` before either sink —
        `openviking-server` echoing its own loaded config at startup would
        otherwise put the API key straight into `_last_output`'s failure
        report and `LOG_FILE`/"View Logs" in plaintext (the HIGH finding in
        `docs/audit/2026-08-24-openviking-managed-review.md`). A log-file
        write failure never stops the drain (best-effort, stdout draining
        itself must keep going regardless)."""

        def _drain() -> None:
            proc = self._proc
            if proc is None or proc.stdout is None:
                return
            log_fh = None
            try:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                log_fh = LOG_FILE.open("a", encoding="utf-8", errors="replace")
            except OSError:
                log_fh = None
            try:
                for line in proc.stdout:
                    text = redact(line.decode("utf-8", errors="replace").rstrip())
                    self._last_output.append(text)
                    del self._last_output[:-_MAX_DRAINED_LINES]
                    if log_fh is not None:
                        try:
                            log_fh.write(text + "\n")
                            log_fh.flush()
                        except OSError:
                            pass
            finally:
                if log_fh is not None:
                    log_fh.close()

        self._reader = threading.Thread(target=_drain, daemon=True)
        self._reader.start()

    def _verify_started(self) -> None:
        """`_spawn`'s `Popen(...)` only raises if the executable itself
        can't launch — it has no idea whether `openviking-server` then exits
        immediately (missing/broken config, port race). Best-effort and
        short: a process still alive after this window is assumed started;
        `manager.py`'s own `/health` polling is the real readiness check."""
        proc = self._proc
        if proc is None:
            return
        time.sleep(_STARTUP_CHECK_S)
        if proc.poll() is None:
            return
        if self._reader is not None:
            self._reader.join(timeout=1)
        self._proc = None
        detail = "\n".join(self._last_output) or f"exit code {proc.returncode}"
        raise ProcessError(f"openviking-server exited immediately: {detail}")

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def last_output(self) -> str:
        return "\n".join(self._last_output)

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        job = self._job
        self._job = None
        if job is not None:
            try:
                ctypes.windll.kernel32.CloseHandle(job)  # type: ignore[attr-defined]
            except Exception:
                pass
        if proc is None:
            return
        _tree_kill(proc.pid)
        _clear_pid_file_if_matches(proc.pid)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
