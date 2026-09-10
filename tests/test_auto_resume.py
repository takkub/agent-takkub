"""Tests for auto-resume (🌙) — always-on since #515 (see module docstring),
plus its tuning constants."""

from __future__ import annotations

from agent_takkub import auto_resume, config


def test_always_on():
    assert auto_resume.current() is True
    assert auto_resume.is_enabled() is True


def test_archives_legacy_toggle_file_once(tmp_path, monkeypatch):
    """A pre-#515 `autoresume.json` is moved to `backups/`, never deleted."""
    monkeypatch.setattr(config, "SETTINGS_HOME", tmp_path)
    legacy = tmp_path / "autoresume.json"
    legacy.write_text('{"enabled": true}', encoding="utf-8")  # real prod shape

    assert auto_resume.current() is True

    assert not legacy.exists()
    assert (tmp_path / "backups" / "autoresume.json").is_file()


def test_constants_are_sane():
    assert auto_resume.MAX_PARK_ROUNDS >= 1
    assert auto_resume.RELIMIT_GRACE_S > 0
    assert auto_resume.WAKE_BUFFER_S >= 0
    assert 0 < auto_resume.CONFIRM_UTILIZATION_PCT <= 100


# ── park-as-last-resort toggle (#514) ───────────────────────────────────────


def test_park_fallback_defaults_true_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_HOME", tmp_path)
    assert auto_resume.park_fallback_enabled() is True


def test_set_park_fallback_then_read_back(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_HOME", tmp_path)
    auto_resume.set_park_fallback_enabled(False)
    assert auto_resume.park_fallback_enabled() is False
    auto_resume.set_park_fallback_enabled(True)
    assert auto_resume.park_fallback_enabled() is True


def test_park_fallback_corrupt_file_defaults_true(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_HOME", tmp_path)
    (tmp_path / "park-fallback.json").write_text("{not valid json", encoding="utf-8")
    assert auto_resume.park_fallback_enabled() is True
