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
