"""Tests for the auto-resume (🌙) toggle (persist + defaults)."""

from __future__ import annotations

import pytest

from agent_takkub import auto_resume


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(auto_resume, "_PATH", tmp_path / "autoresume.json")


def test_default_is_on(_isolated):
    assert auto_resume.current() is True
    assert auto_resume.is_enabled() is True


def test_set_and_read_on(_isolated):
    auto_resume.set_enabled(True)
    assert auto_resume.current() is True
    assert auto_resume.is_enabled() is True


def test_set_back_to_off_still_returns_on(_isolated):
    auto_resume.set_enabled(True)
    auto_resume.set_enabled(False)
    assert auto_resume.current() is True


def test_corrupt_file_returns_on(_isolated):
    auto_resume.path().write_text("{not json", encoding="utf-8")
    assert auto_resume.current() is True


def test_non_dict_json_returns_on(_isolated):
    auto_resume.path().write_text("[1, 2, 3]", encoding="utf-8")
    assert auto_resume.current() is True


def test_constants_are_sane():
    assert auto_resume.MAX_PARK_ROUNDS >= 1
    assert auto_resume.RELIMIT_GRACE_S > 0
    assert auto_resume.WAKE_BUFFER_S >= 0
    assert 0 < auto_resume.CONFIRM_UTILIZATION_PCT <= 100
