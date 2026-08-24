"""core_v2_settings — Context Strategy persistence (v2-hardening C/G,
`13_SIMPLE_UX.md`): default "automatic", round-trips, rejects unknown
values, and survives being absent from an older settings file on disk.
No Qt needed — this is the plain JSON-store half `test_settings_core_v2.py`
covers with widget/Qt fixtures for the boolean `FLAG_NAMES` tuple; kept
separate so this module's tests never need a QApplication."""

from __future__ import annotations

import json

import pytest

from agent_takkub import config, core_v2_settings


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_HOME", tmp_path)
    core_v2_settings._reset_cache()
    yield
    core_v2_settings._reset_cache()


def test_default_is_automatic():
    assert core_v2_settings.load_context_strategy() == "automatic"


@pytest.mark.parametrize("value", ["fast", "automatic", "deep"])
def test_save_and_load_round_trips(value):
    assert core_v2_settings.save_context_strategy(value) is True
    assert core_v2_settings.load_context_strategy() == value


def test_save_unknown_value_raises():
    with pytest.raises(ValueError):
        core_v2_settings.save_context_strategy("ludicrous")


def test_missing_file_defaults_to_automatic():
    payload = core_v2_settings.load()
    assert payload["context_strategy"] == "automatic"


def test_corrupt_on_disk_value_falls_back_to_default():
    path = core_v2_settings.path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"context_strategy": "not-a-real-strategy"}), encoding="utf-8")
    core_v2_settings._reset_cache()
    assert core_v2_settings.load_context_strategy() == "automatic"


def test_missing_key_in_older_settings_file_defaults_to_automatic():
    path = core_v2_settings.path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"flags": {"router": True}}), encoding="utf-8")
    core_v2_settings._reset_cache()
    assert core_v2_settings.load_context_strategy() == "automatic"


def test_setting_strategy_does_not_clobber_flags():
    core_v2_settings.set_flag("router", False)
    core_v2_settings.save_context_strategy("deep")
    assert core_v2_settings.flag_enabled("router") is False
    assert core_v2_settings.load_context_strategy() == "deep"


# ── core.brain.flag.context_strategy — env-wins-then-setting precedence ───


def test_flag_reads_persisted_setting_when_env_unset(monkeypatch):
    from agent_takkub.core.brain import flag

    monkeypatch.delenv("TAKKUB_CONTEXT_STRATEGY", raising=False)
    core_v2_settings.save_context_strategy("deep")
    assert flag.context_strategy() == "deep"


def test_flag_env_wins_over_persisted_setting(monkeypatch):
    from agent_takkub.core.brain import flag

    core_v2_settings.save_context_strategy("deep")
    monkeypatch.setenv("TAKKUB_CONTEXT_STRATEGY", "fast")
    assert flag.context_strategy() == "fast"


def test_flag_invalid_env_value_falls_back_to_setting(monkeypatch):
    from agent_takkub.core.brain import flag

    monkeypatch.setenv("TAKKUB_CONTEXT_STRATEGY", "ludicrous")
    assert flag.context_strategy() == "automatic"
