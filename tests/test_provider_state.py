"""Unit tests for provider_state — per-provider enable/disable state."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub import provider_state


@pytest.fixture
def tmp_state_path(tmp_path, monkeypatch):
    """Redirect provider_state to a tmp file so tests don't touch the real config."""
    path = tmp_path / "disabled-providers.json"
    monkeypatch.setattr(provider_state, "_PATH", path)
    return path


def test_load_missing_file_returns_empty(tmp_state_path):
    assert provider_state.load() == {}


def test_save_then_load_roundtrip(tmp_state_path):
    provider_state.save({"codex": True, "gemini": False})
    assert provider_state.load() == {"codex": True, "gemini": False}


def test_save_writes_atomically_via_tmp_file(tmp_state_path):
    provider_state.save({"codex": True})
    # final file exists
    assert tmp_state_path.exists()
    # no leftover .tmp file
    assert not tmp_state_path.with_suffix(tmp_state_path.suffix + ".tmp").exists()


def test_corrupt_json_returns_empty_without_crash(tmp_state_path):
    tmp_state_path.write_text("{not valid json", encoding="utf-8")
    assert provider_state.load() == {}


def test_set_disabled_then_is_disabled(tmp_state_path):
    assert provider_state.is_disabled("codex") is False
    provider_state.set_disabled("codex", True)
    assert provider_state.is_disabled("codex") is True
    provider_state.set_disabled("codex", False)
    assert provider_state.is_disabled("codex") is False


def test_unknown_provider_dropped_on_save(tmp_state_path):
    provider_state.save({"codex": True, "bogus": True})
    assert provider_state.load() == {"codex": True}


def test_all_disabled_returns_set_of_truthy_providers(tmp_state_path):
    provider_state.save({"codex": True, "gemini": False})
    assert provider_state.all_disabled() == {"codex"}
    provider_state.save({"codex": True, "gemini": True})
    assert provider_state.all_disabled() == {"codex", "gemini"}


def test_set_disabled_unknown_provider_raises(tmp_state_path):
    with pytest.raises(ValueError):
        provider_state.set_disabled("bogus", True)


def test_default_path_is_under_home_takkub(monkeypatch, tmp_path):
    # Sanity check: the real default points at ~/.takkub/disabled-providers.json
    # (use the module-level _PATH before any monkeypatch from other fixtures)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    import importlib

    import agent_takkub.provider_state as ps_module

    importlib.reload(ps_module)
    assert ps_module._PATH == tmp_path / ".takkub" / "disabled-providers.json"
    # Reload back so other tests aren't affected
    importlib.reload(ps_module)
