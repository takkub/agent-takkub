"""Tests for the SOLO/PARALLEL execution-mode toggle (persist + defaults)."""

from __future__ import annotations

import pytest

from agent_takkub import exec_mode


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(exec_mode, "_PATH", tmp_path / "exec-mode.json")


def test_default_is_solo(_isolated):
    assert exec_mode.current() == exec_mode.SOLO
    assert exec_mode.is_parallel() is False


def test_set_and_read_parallel(_isolated):
    exec_mode.set_current(exec_mode.PARALLEL)
    assert exec_mode.current() == exec_mode.PARALLEL
    assert exec_mode.is_parallel() is True


def test_set_back_to_solo(_isolated):
    exec_mode.set_current(exec_mode.PARALLEL)
    exec_mode.set_current(exec_mode.SOLO)
    assert exec_mode.current() == exec_mode.SOLO


def test_unknown_mode_rejected(_isolated):
    with pytest.raises(ValueError):
        exec_mode.set_current("turbo")


def test_corrupt_file_falls_back_to_solo(_isolated):
    exec_mode.path().write_text("{not json", encoding="utf-8")
    assert exec_mode.current() == exec_mode.SOLO


def test_non_dict_json_falls_back_to_solo(_isolated):
    exec_mode.path().write_text("[1, 2, 3]", encoding="utf-8")
    assert exec_mode.current() == exec_mode.SOLO


def test_max_fanout_is_bounded():
    assert isinstance(exec_mode.MAX_FANOUT, int)
    assert 2 <= exec_mode.MAX_FANOUT <= 16


def test_machine_fanout_cap_bounds():
    cap = exec_mode.machine_fanout_cap()
    assert isinstance(cap, int)
    assert 1 <= cap <= exec_mode.MAX_FANOUT


def test_machine_fanout_cap_limited_by_low_ram(monkeypatch):
    # 1 GB free RAM → by_ram = 0 → clamped to 1, so the cap is 1 regardless of CPU.
    monkeypatch.setattr(exec_mode.os, "cpu_count", lambda: 32)

    class _VM:
        available = 1 * 1024**3

    import psutil

    monkeypatch.setattr(psutil, "virtual_memory", lambda: _VM())
    assert exec_mode.machine_fanout_cap() == 1


def test_machine_fanout_cap_capped_at_max(monkeypatch):
    # Huge machine → still never exceeds MAX_FANOUT.
    monkeypatch.setattr(exec_mode.os, "cpu_count", lambda: 64)

    class _VM:
        available = 256 * 1024**3

    import psutil

    monkeypatch.setattr(psutil, "virtual_memory", lambda: _VM())
    assert exec_mode.machine_fanout_cap() == exec_mode.MAX_FANOUT
