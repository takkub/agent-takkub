from __future__ import annotations

import sys

import pytest

from agent_takkub import job_object_manager as module


class _Kernel32:
    def __init__(self) -> None:
        self.closed: list[int] = []
        self.assigned: list[tuple[int, int]] = []

    def CreateJobObjectW(self, _attrs, _name):
        return 101

    def SetInformationJobObject(self, _job, _kind, _info, _size):
        return 1

    def OpenProcess(self, _access, _inherit, _pid):
        return 202

    def AssignProcessToJobObject(self, job, process):
        self.assigned.append((job, process))
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="_JOBOBJECT_*_INFORMATION structures are only defined at import time on "
    "win32 (#239); mocking ctypes.windll.kernel32 at runtime can't retrofit them "
    "on POSIX, so create()/assign() hit a NameError caught by the broad except.",
)
def test_windows_job_assigns_process_and_closes_kill_on_close_handle(monkeypatch) -> None:
    kernel = _Kernel32()
    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(
        module.ctypes, "windll", type("W", (), {"kernel32": kernel})(), raising=False
    )
    manager = module.JobObjectManager()

    assert manager.assign(4242)
    assert manager.active
    assert manager.assigned_pid == 4242
    assert kernel.assigned == [(101, 202)]
    assert manager.close()
    assert not manager.active
    assert kernel.closed == [202, 101]


def test_non_windows_job_manager_is_safe_noop(monkeypatch) -> None:
    monkeypatch.setattr(module.sys, "platform", "linux")
    manager = module.JobObjectManager()
    assert not manager.create()
    assert not manager.assign(1)
    assert not manager.close()


def test_protected_job_pids_spares_service_and_its_descendants_980(monkeypatch) -> None:
    """#980: members named like Docker, or descended from one, are spared;
    the pane's own processes are not."""
    import psutil

    from agent_takkub import pty_session

    names = {1: "claude.exe", 2: "cmd.exe", 3: "Docker Desktop.exe", 4: "wsl.exe", 5: "node.exe"}
    parents = {1: [], 2: [1], 3: [], 4: [3], 5: [1]}  # 3's launcher (2) already exited

    class _P:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def name(self) -> str:
            return names[self.pid]

        def parents(self) -> list[_P]:
            return [_P(p) for p in parents[self.pid]]

    monkeypatch.setattr(psutil, "Process", _P)
    job = type("J", (), {"member_pids": lambda self: [1, 2, 3, 4, 5]})()
    assert pty_session._protected_job_pids(job) == {3, 4}


def test_close_sparing_without_spare_is_plain_close(monkeypatch) -> None:
    manager = module.JobObjectManager()
    calls: list[str] = []
    monkeypatch.setattr(manager, "close", lambda: calls.append("close") or True)
    assert manager.close_sparing(set())
    assert calls == ["close"]
