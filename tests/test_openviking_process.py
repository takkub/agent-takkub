"""Tests for `agent_takkub.openviking.process`: OpenVikingProcess lifecycle,
PID-file bookkeeping, and orphan reap — same shape as `test_remote_tunnel.py`
(subprocess spawning itself is stubbed throughout)."""

from __future__ import annotations

import json
import os

import psutil
import pytest

from agent_takkub.openviking import process


@pytest.fixture(autouse=True)
def _isolate_pid_file(monkeypatch, tmp_path):
    monkeypatch.setattr(process, "PID_FILE", tmp_path / "openviking_pid.json")


class _FakeProc:
    def __init__(self, pid: int = 4242, lines: list[bytes] | None = None, returncode=None) -> None:
        self.pid = pid
        self._lines = lines or []
        self.stdout = iter(self._lines)
        self.waited = False
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waited = True


class _FakePsutilProcess:
    def __init__(self, create_time: float) -> None:
        self._create_time = create_time

    def create_time(self) -> float:
        return self._create_time


class TestStartStop:
    def test_start_raises_when_executable_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            process, "server_executable", lambda: tmp_path / "missing" / "ov-server"
        )
        p = process.OpenVikingProcess(tmp_path / "ov.conf", 1933)
        with pytest.raises(process.ProcessError, match="not found"):
            p.start()

    def test_start_builds_expected_argv_and_writes_pid_file(self, monkeypatch, tmp_path):
        proc = _FakeProc(pid=54321, returncode=None)
        captured = {}

        def _fake_spawn(argv):
            captured["argv"] = argv
            return proc

        exe = tmp_path / "venv" / "bin" / "openviking-server"
        exe.parent.mkdir(parents=True)
        exe.write_text("", encoding="utf-8")
        monkeypatch.setattr(process, "server_executable", lambda: exe)
        monkeypatch.setattr(process, "_spawn", _fake_spawn)
        monkeypatch.setattr(process.OpenVikingProcess, "_own_job_if_windows", lambda self: None)
        monkeypatch.setattr(process.time, "sleep", lambda s: None)

        config_path = tmp_path / "config" / "ov.conf"
        p = process.OpenVikingProcess(config_path, 1933)
        p.start()

        assert captured["argv"] == [str(exe), "--config", str(config_path), "--port", "1933"]
        assert process.PID_FILE.exists()
        data = json.loads(process.PID_FILE.read_text(encoding="utf-8"))
        assert data["pid"] == 54321
        assert data["port"] == 1933
        assert data["owner_pid"] == os.getpid()

    def test_start_raises_and_writes_no_pid_file_when_process_dies_immediately(
        self, monkeypatch, tmp_path
    ):
        proc = _FakeProc(returncode=1, lines=[b"config error: bad port\n"])
        exe = tmp_path / "venv" / "bin" / "openviking-server"
        exe.parent.mkdir(parents=True)
        exe.write_text("", encoding="utf-8")
        monkeypatch.setattr(process, "server_executable", lambda: exe)
        monkeypatch.setattr(process, "_spawn", lambda argv: proc)
        monkeypatch.setattr(process.OpenVikingProcess, "_own_job_if_windows", lambda self: None)
        monkeypatch.setattr(process.time, "sleep", lambda s: None)

        p = process.OpenVikingProcess(tmp_path / "ov.conf", 1933)
        with pytest.raises(process.ProcessError, match="bad port"):
            p.start()
        assert not process.PID_FILE.exists()

    def test_stop_tree_kills_and_clears_matching_pid_file(self, monkeypatch, tmp_path):
        killed = {}
        monkeypatch.setattr(process, "_tree_kill", lambda pid: killed.setdefault("pid", pid))
        p = process.OpenVikingProcess(tmp_path / "ov.conf", 1933)
        p._proc = _FakeProc(pid=9999)
        process.PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        process.PID_FILE.write_text(json.dumps({"pid": 9999}), encoding="utf-8")

        p.stop()

        assert killed["pid"] == 9999
        assert p._proc is None
        assert not process.PID_FILE.exists()

    def test_stop_on_never_started_process_is_a_no_op(self, tmp_path):
        p = process.OpenVikingProcess(tmp_path / "ov.conf", 1933)
        p.stop()  # must not raise

    def test_is_alive_reflects_fresh_poll(self, tmp_path):
        p = process.OpenVikingProcess(tmp_path / "ov.conf", 1933)
        assert p.is_alive is False
        p._proc = _FakeProc(returncode=None)
        assert p.is_alive is True
        p._proc = _FakeProc(returncode=0)
        assert p.is_alive is False


class TestReapOrphanProcess:
    def _write_pid_file(self, pid: int, owner_pid: int, owner_create_time: float) -> None:
        process.PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        process.PID_FILE.write_text(
            json.dumps(
                {"pid": pid, "owner_pid": owner_pid, "owner_create_time": owner_create_time}
            ),
            encoding="utf-8",
        )

    def test_no_pid_file_is_a_no_op(self, monkeypatch):
        killed = []
        monkeypatch.setattr(process, "_tree_kill", lambda pid: killed.append(pid))
        process.reap_orphan_process()
        assert killed == []

    def test_corrupt_pid_file_is_cleared_without_raising(self):
        process.PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        process.PID_FILE.write_text("{not json", encoding="utf-8")
        process.reap_orphan_process()
        assert not process.PID_FILE.exists()

    def test_dead_process_just_clears_the_file(self, monkeypatch):
        self._write_pid_file(pid=999999, owner_pid=os.getpid(), owner_create_time=0.0)
        monkeypatch.setattr(psutil, "pid_exists", lambda pid: False)
        killed = []
        monkeypatch.setattr(process, "_tree_kill", lambda pid: killed.append(pid))

        process.reap_orphan_process()

        assert killed == []
        assert not process.PID_FILE.exists()

    def test_live_process_with_live_matching_owner_is_left_alone(self, monkeypatch):
        real_create_time = psutil.Process(os.getpid()).create_time()
        self._write_pid_file(pid=12345, owner_pid=os.getpid(), owner_create_time=real_create_time)
        monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)
        killed = []
        monkeypatch.setattr(process, "_tree_kill", lambda pid: killed.append(pid))

        process.reap_orphan_process()

        assert killed == []
        assert process.PID_FILE.exists()

    def test_live_process_with_dead_owner_is_reaped(self, monkeypatch):
        self._write_pid_file(pid=12345, owner_pid=999998, owner_create_time=111.0)

        monkeypatch.setattr(psutil, "pid_exists", lambda pid: pid == 12345)
        killed = []
        monkeypatch.setattr(process, "_tree_kill", lambda pid: killed.append(pid))

        process.reap_orphan_process()

        assert killed == [12345]
        assert not process.PID_FILE.exists()

    def test_live_process_with_reused_owner_pid_is_reaped(self, monkeypatch):
        self._write_pid_file(pid=12345, owner_pid=os.getpid(), owner_create_time=1.0)
        monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)
        monkeypatch.setattr(psutil, "Process", lambda pid: _FakePsutilProcess(create_time=999.0))
        killed = []
        monkeypatch.setattr(process, "_tree_kill", lambda pid: killed.append(pid))

        process.reap_orphan_process()

        assert killed == [12345]
        assert not process.PID_FILE.exists()

    def test_non_int_pid_clears_file_without_killing(self, monkeypatch):
        process.PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        process.PID_FILE.write_text(json.dumps({"pid": "nope"}), encoding="utf-8")
        killed = []
        monkeypatch.setattr(process, "_tree_kill", lambda pid: killed.append(pid))

        process.reap_orphan_process()

        assert killed == []
        assert not process.PID_FILE.exists()


class TestIsProcessAlive:
    def test_false_when_no_pid_file(self):
        assert process.is_process_alive() is False

    def test_true_when_pid_exists(self, monkeypatch):
        process.PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        process.PID_FILE.write_text(json.dumps({"pid": 12345}), encoding="utf-8")
        monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)
        assert process.is_process_alive() is True

    def test_false_when_pid_gone(self, monkeypatch):
        process.PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        process.PID_FILE.write_text(json.dumps({"pid": 12345}), encoding="utf-8")
        monkeypatch.setattr(psutil, "pid_exists", lambda pid: False)
        assert process.is_process_alive() is False
