from __future__ import annotations

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
