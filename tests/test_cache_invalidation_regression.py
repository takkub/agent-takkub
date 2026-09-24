"""Regression tests for cache invalidation across stores and helpers touched for main thread stall fixes.

Ensures that whenever a writer updates state (save/set/append), in-process readers
immediately see the fresh data without serving stale cached reads (preventing lost updates).
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_provider_state_quota_resets_cache_invalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_takkub import provider_state

    quota_file = tmp_path / "provider-quota.json"
    monkeypatch.setattr(provider_state, "_QUOTA_PATH", quota_file)

    # Initially empty
    assert provider_state.load_quota_resets() == {}

    # Writer updates
    provider_state.set_quota_reset_at("codex", 1700000000.0)
    # Reader must see immediately
    res = provider_state.load_quota_resets()
    assert res.get("codex") == 1700000000.0

    # Another writer update
    provider_state.set_quota_reset_at("gemini", 1700005000.0)
    res2 = provider_state.load_quota_resets()
    assert res2.get("gemini") == 1700005000.0
    assert res2.get("codex") == 1700000000.0

    # Clear writer
    provider_state.clear_quota_reset("codex")
    res3 = provider_state.load_quota_resets()
    assert "codex" not in res3
    assert res3.get("gemini") == 1700005000.0


def test_config_projects_cache_invalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed_projects
) -> None:
    from agent_takkub import config

    monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "cockpit")
    seed_projects(tmp_path, {"proj_a": {"paths": {}}}, active="proj_a")

    loaded = config.load_projects()
    assert loaded["active"] == "proj_a"
    assert "proj_a" in loaded["projects"]

    # In-place mutation of returned dict must NOT pollute cache
    loaded["active"] = "mutated_in_place"
    assert config.load_projects()["active"] == "proj_a"

    # Writer updates projects
    config.set_active_project("proj_a")
    new_data = config.load_projects()
    new_data["projects"]["proj_b"] = {"paths": {}}
    assert config.save_projects_json(new_data) is True

    # Reader must immediately see proj_b
    fresh = config.load_projects()
    assert "proj_b" in fresh["projects"]


def test_user_profile_cache_invalidation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_takkub import user_profile

    reg_path = tmp_path / "profiles.json"
    monkeypatch.setattr(user_profile, "_REGISTRY_PATH", reg_path)
    monkeypatch.setattr(user_profile, "_BASE_DIR", tmp_path)

    user_profile.add_profile("work", str(tmp_path / "work"), "claude")
    user_profile.add_profile("personal", str(tmp_path / "personal"), "claude")

    # Initially default
    assert user_profile.profile_for("my_proj", "claude") == "default"

    # Set profile writes and invalidates
    user_profile.set_profile("my_proj", "work", "claude")
    assert user_profile.profile_for("my_proj", "claude") == "work"

    # Update to another profile
    user_profile.set_profile("my_proj", "personal", "claude")
    assert user_profile.profile_for("my_proj", "claude") == "personal"

    # Default provider
    assert user_profile.default_provider("my_proj") == "claude"
    user_profile.set_default_provider("my_proj", "codex")
    assert user_profile.default_provider("my_proj") == "codex"


def test_usage_ledger_jsonl_append_invalidates_cache(tmp_path: Path) -> None:
    from agent_takkub import usage_ledger

    jsonl_file = tmp_path / "test.jsonl"
    assert usage_ledger._read_jsonl(jsonl_file) == []

    # Append first row
    usage_ledger._append_jsonl(jsonl_file, {"id": 1, "val": "first"})
    rows = usage_ledger._read_jsonl(jsonl_file)
    assert len(rows) == 1
    assert rows[0]["val"] == "first"

    # Subsequent read is cached and fast
    rows2 = usage_ledger._read_jsonl(jsonl_file)
    assert len(rows2) == 1

    # In-place mutation of returned rows must not corrupt cache
    rows2[0]["val"] = "corrupted"
    assert usage_ledger._read_jsonl(jsonl_file)[0]["val"] == "first"

    # Append second row must invalidate cache immediately
    usage_ledger._append_jsonl(jsonl_file, {"id": 2, "val": "second"})
    rows3 = usage_ledger._read_jsonl(jsonl_file)
    assert len(rows3) == 2
    assert rows3[1]["val"] == "second"


def test_graft_cli_cache_invalidation(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_takkub import graft_store

    graft_store.invalidate_graft_cli_cache()
    calls = []

    def mock_which(cmd):
        calls.append(cmd)
        return f"/bin/{cmd}"

    monkeypatch.setattr(graft_store.shutil, "which", mock_which)
    p1 = graft_store.graft_cli_path()
    assert p1 == "/bin/graft.cmd"
    assert len(calls) == 1

    # Second call returns cached value without calling which
    p_cached = graft_store.graft_cli_path()
    assert p_cached == "/bin/graft.cmd"
    assert len(calls) == 1

    # Invalidate drops cache, causing re-probe
    graft_store.invalidate_graft_cli_cache()
    p2 = graft_store.graft_cli_path()
    assert p2 == "/bin/graft.cmd"
    assert len(calls) == 2


def test_gemini_helper_agy_cache_invalidation(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_takkub import gemini_helper

    gemini_helper.invalidate_agy_executable_cache()
    calls = []

    def mock_which(cmd):
        calls.append(cmd)
        return "/bin/agy" if cmd == "agy" else None

    monkeypatch.setattr(gemini_helper.shutil, "which", mock_which)
    p1 = gemini_helper.find_agy_executable()
    assert p1 == "/bin/agy"
    assert len(calls) == 1

    # Second call returns cached value without calling which
    p_cached = gemini_helper.find_agy_executable()
    assert p_cached == "/bin/agy"
    assert len(calls) == 1

    # Invalidate drops cache, causing re-probe
    gemini_helper.invalidate_agy_executable_cache()
    p2 = gemini_helper.find_agy_executable()
    assert p2 == "/bin/agy"
    assert len(calls) == 2
