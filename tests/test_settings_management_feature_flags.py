from __future__ import annotations

import pytest

from agent_takkub.settings_management import feature_flags


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAKKUB_SETTINGS_UI", raising=False)


def test_default_is_legacy() -> None:
    # Default rolled back to LEGACY 2026-07-11 evening — the new surface
    # passed critic review but the user rejected it in real use; it stays
    # opt-in behind TAKKUB_SETTINGS_UI=new until its UX rework lands.
    assert feature_flags.resolve() is feature_flags.SettingsUI.LEGACY


def test_unset_env_is_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAKKUB_SETTINGS_UI", raising=False)
    assert feature_flags.resolve() is feature_flags.SettingsUI.LEGACY


def test_unknown_value_falls_back_to_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAKKUB_SETTINGS_UI", "bogus")
    assert feature_flags.resolve() is feature_flags.SettingsUI.LEGACY


def test_new_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAKKUB_SETTINGS_UI", "new")
    assert feature_flags.resolve() is feature_flags.SettingsUI.NEW


def test_legacy_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAKKUB_SETTINGS_UI", "LEGACY")
    assert feature_flags.resolve() is feature_flags.SettingsUI.LEGACY


def test_compare_no_longer_a_recognized_value_falls_back_to_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # MED-4: `compare` was dropped (see feature_flags module docstring) — it
    # was never actually implemented (routing only special-cased `new`), so
    # an existing `TAKKUB_SETTINGS_UI=compare` env now degrades to the same
    # unknown-value fallback as any typo, not a distinct enum member.
    monkeypatch.setenv("TAKKUB_SETTINGS_UI", "compare")
    assert feature_flags.resolve() is feature_flags.SettingsUI.LEGACY
    assert not hasattr(feature_flags.SettingsUI, "COMPARE")
