"""openviking_cleanup.py — reclaiming a leftover OpenViking managed-runtime
install a v1.5.0 Takkub left under `~/.agent-takkub/services/openviking/`
(docs/plans/remove-openviking-2026-08-24/07_RUNTIME_DATA_MIGRATION.md).
Never touches anything this cockpit's own PID file didn't name — no port
1933 probe, no arbitrary process kill.
"""

from __future__ import annotations

import json

import pytest

from agent_takkub import openviking_cleanup as cleanup


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    home = tmp_path / "openviking"
    monkeypatch.setattr(cleanup, "OPENVIKING_HOME", home)
    monkeypatch.setattr(cleanup, "VENV_DIR", home / "venv")
    monkeypatch.setattr(cleanup, "CONFIG_DIR", home / "config")
    monkeypatch.setattr(cleanup, "DATA_DIR", home / "data")
    monkeypatch.setattr(cleanup, "STATE_FILE", home / "state.json")
    monkeypatch.setattr(cleanup, "LOG_DIR", home / "logs")
    monkeypatch.setattr(cleanup, "PID_FILE", home / "openviking_pid.json")
    return home


def test_exists_false_when_nothing_installed(_isolate_home):
    assert cleanup.exists() is False


def test_report_reflects_size_and_no_owned_process(_isolate_home):
    (_isolate_home / "venv" / "bin").mkdir(parents=True)
    (_isolate_home / "venv" / "bin" / "openviking-server").write_bytes(b"x" * 1024)

    info = cleanup.report()
    assert info.exists is True
    assert info.size_bytes >= 1024
    assert info.owned_pid is None


def test_report_owned_pid_only_when_pid_alive(_isolate_home, monkeypatch):
    _isolate_home.mkdir(parents=True, exist_ok=True)
    (_isolate_home / "openviking_pid.json").write_text(json.dumps({"pid": 999999}))

    import psutil

    monkeypatch.setattr(psutil, "pid_exists", lambda pid: False)
    assert cleanup.report().owned_pid is None

    monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)
    assert cleanup.report().owned_pid == 999999


def test_stop_owned_process_never_touches_unrecorded_pid(_isolate_home, monkeypatch):
    """No PID_FILE at all — `stop_owned_process` must not call `_tree_kill`
    (nothing recorded as ours, so nothing to stop)."""
    calls = []
    monkeypatch.setattr("agent_takkub.pty_session._tree_kill", lambda pid: calls.append(pid))
    cleanup.stop_owned_process()
    assert calls == []


def test_stop_owned_process_kills_only_the_recorded_pid(_isolate_home, monkeypatch):
    _isolate_home.mkdir(parents=True, exist_ok=True)
    (_isolate_home / "openviking_pid.json").write_text(json.dumps({"pid": 4242}))

    import psutil

    monkeypatch.setattr(psutil, "pid_exists", lambda pid: pid == 4242)
    calls = []
    monkeypatch.setattr("agent_takkub.pty_session._tree_kill", lambda pid: calls.append(pid))

    cleanup.stop_owned_process()

    assert calls == [4242]
    assert not (_isolate_home / "openviking_pid.json").exists()


def test_remove_keeps_config_and_data_by_default(_isolate_home):
    (_isolate_home / "venv").mkdir(parents=True)
    (_isolate_home / "config").mkdir(parents=True)
    (_isolate_home / "data").mkdir(parents=True)
    (_isolate_home / "state.json").write_text("{}")

    cleanup.remove(purge_data=False)

    assert not (_isolate_home / "venv").exists()
    assert not (_isolate_home / "state.json").exists()
    assert (_isolate_home / "config").exists()
    assert (_isolate_home / "data").exists()


def test_remove_purge_data_deletes_everything(_isolate_home):
    (_isolate_home / "venv").mkdir(parents=True)
    (_isolate_home / "config").mkdir(parents=True)
    (_isolate_home / "data").mkdir(parents=True)

    cleanup.remove(purge_data=True)

    assert not _isolate_home.exists()
