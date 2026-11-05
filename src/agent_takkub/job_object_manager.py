"""Windows Job Object ownership for pane process trees."""

from __future__ import annotations

import ctypes
import logging
import sys

_log = logging.getLogger(__name__)
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOBOBJECTINFOCLASS_EXTENDED_LIMIT = 9
_JOBOBJECTINFOCLASS_BASIC_PROCESS_ID_LIST = 3
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


if sys.platform == "win32":

    class _LARGE_INTEGER(ctypes.Structure):
        _fields_ = [("QuadPart", ctypes.c_longlong)]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", _LARGE_INTEGER),
            ("PerJobUserTimeLimit", _LARGE_INTEGER),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
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


class JobObjectManager:
    """One kill-on-close Job Object. No-op on non-Windows platforms."""

    def __init__(self) -> None:
        self._handle: int | None = None
        self._assigned_pid: int | None = None

    @property
    def active(self) -> bool:
        return self._handle is not None

    @property
    def assigned_pid(self) -> int | None:
        return self._assigned_pid

    def create(self) -> bool:
        if sys.platform != "win32":
            return False
        if self._handle is not None:
            return True
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return False
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = kernel32.SetInformationJobObject(
                handle,
                _JOBOBJECTINFOCLASS_EXTENDED_LIMIT,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                kernel32.CloseHandle(handle)
                return False
            self._handle = handle
            return True
        except Exception:
            return False

    def assign(self, pid: int) -> bool:
        if not self.create() or self._handle is None:
            return False
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            access = _PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION
            process = kernel32.OpenProcess(access, False, int(pid))
            if not process:
                return False
            try:
                assigned = bool(kernel32.AssignProcessToJobObject(self._handle, process))
            finally:
                kernel32.CloseHandle(process)
            if assigned:
                self._assigned_pid = int(pid)
            return assigned
        except Exception:
            return False

    def member_pids(self) -> list[int]:
        """PIDs currently in the job (every descendant inherits membership,
        including processes launched detached via `start` / Start-Process
        whose parent already exited — a tree walk cannot see those, #980)."""
        if self._handle is None or sys.platform != "win32":
            return []
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            capacity = 4096

            class _PID_LIST(ctypes.Structure):
                _fields_ = [
                    ("NumberOfAssignedProcesses", ctypes.c_uint32),
                    ("NumberOfProcessIdsInList", ctypes.c_uint32),
                    ("ProcessIdList", ctypes.c_size_t * capacity),
                ]

            info = _PID_LIST()
            ok = kernel32.QueryInformationJobObject(
                self._handle,
                _JOBOBJECTINFOCLASS_BASIC_PROCESS_ID_LIST,
                ctypes.byref(info),
                ctypes.sizeof(info),
                None,
            )
            if not ok:
                return []
            count = min(int(info.NumberOfProcessIdsInList), capacity)
            return [int(info.ProcessIdList[i]) for i in range(count)]
        except Exception as exc:
            _log.debug("job object pid list failed: %r", exc)
            return []

    def _disarm(self) -> bool:
        """Clear KILL_ON_JOB_CLOSE so closing the handle leaves members alive."""
        if self._handle is None:
            return False
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = 0
            return bool(
                kernel32.SetInformationJobObject(
                    self._handle,
                    _JOBOBJECTINFOCLASS_EXTENDED_LIMIT,
                    ctypes.byref(info),
                    ctypes.sizeof(info),
                )
            )
        except Exception as exc:
            _log.debug("job object disarm failed: %r", exc)
            return False

    def close_sparing(self, spare: set[int]) -> bool:
        """Close the job but keep `spare` PIDs alive (#980).

        With no PIDs to spare this is plain `close()`. Otherwise the
        kill-on-close limit is cleared first, every other member is terminated
        by PID, and only then is the handle closed — so a machine service the
        pane launched (Docker Desktop) outlives the pane. If the limit cannot
        be cleared, falls back to `close()` (old behavior) rather than leaking.
        """
        if not spare or self._handle is None:
            return self.close()
        members = self.member_pids()
        if not self._disarm():
            _log.warning("job object disarm failed; closing kills spared pids %s", sorted(spare))
            return self.close()
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            for pid in members:
                if pid in spare:
                    continue
                process = kernel32.OpenProcess(_PROCESS_TERMINATE, False, int(pid))
                if not process:
                    continue
                try:
                    kernel32.TerminateProcess(process, 1)
                finally:
                    kernel32.CloseHandle(process)
        except Exception as exc:
            _log.debug("job object sparing kill failed: %r", exc)
        return self.close()

    def close(self) -> bool:
        """Close the job handle; Windows terminates all assigned descendants."""
        handle, self._handle = self._handle, None
        self._assigned_pid = None
        if handle is None:
            return False
        try:
            if sys.platform == "win32":
                ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
            return True
        except Exception as exc:
            _log.debug("job object close failed: %r", exc)
            return False

    def __enter__(self) -> JobObjectManager:
        self.create()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
