from __future__ import annotations

import pytest

from agent_takkub.settings_management import feature_flags


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAKKUB_SETTINGS_UI", raising=False)


def test_default_is_new() -> None:
    assert feature_flags.resolve() is feature_flags.SettingsUI.NEW


def test_unset_env_is_new(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAKKUB_SETTINGS_UI", raising=False)
    assert feature_flags.resolve() is feature_flags.SettingsUI.NEW


def test_unknown_value_falls_back_to_new(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAKKUB_SETTINGS_UI", "bogus")
    assert feature_flags.resolve() is feature_flags.SettingsUI.NEW


def test_legacy_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAKKUB_SETTINGS_UI", "LEGACY")
    assert feature_flags.resolve() is feature_flags.SettingsUI.LEGACY


def test_compare(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAKKUB_SETTINGS_UI", "compare")
    assert feature_flags.resolve() is feature_flags.SettingsUI.COMPARE
