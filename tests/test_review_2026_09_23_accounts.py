"""System review 2026-09-23, group "accounts" — the two findings fixed together:

* core/accounts/facade.py:74 — the V2 account override's legacy fallback
  rewrote CLAUDE_CONFIG_DIR back to the profile's base dir on every claude
  spawn, clobbering the #563 curated dir `inject_curated_claude_config_dir`
  had just prepared (skill gate silently inert fleet-wide).
* claude_auth_config.py:177 — once the curated dir survives, the Claude Auth
  override (Settings → Claude Auth, saved to the profile's BASE dir) must
  still be found: the curated dir never mirrors takkub-claude-auth.json.

These tests run the REAL functions in the exact order spawn_engine.py's
claude branch calls them (inject_user_profile_env → inject_curated_claude_
config_dir → _apply_v2_account_env_override → apply_claude_auth_overrides),
without the Qt/PTY spawn machinery around them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub import claude_auth_config as cfg
from agent_takkub import pane_env, skill_policy, user_profile
from agent_takkub.core.accounts.registry import AccountPoolRegistry
from agent_takkub.core.storage.jsonl_store import JsonlStore

PROJECT = "review-accounts-proj"


@pytest.fixture
def spawn_env_sandbox(monkeypatch, tmp_path: Path):
    """Hermetic profile registry + DATA_HOME + empty V2 pool + no skill inputs."""
    monkeypatch.setattr("agent_takkub.config.DATA_HOME", tmp_path / "data_home")
    monkeypatch.delenv("TAKKUB_SKILL_GATE", raising=False)
    monkeypatch.setattr(cfg, "_LEGACY_GLOBAL_PATH", tmp_path / "legacy-absent.json")
    monkeypatch.setattr("agent_takkub.lead_context._allowed_project_roots", lambda project: [])
    monkeypatch.setattr(skill_policy, "load_policy", lambda: {})
    empty_pools = AccountPoolRegistry(JsonlStore(tmp_path / "pools.jsonl"))
    monkeypatch.setattr(
        "agent_takkub.core.accounts.facade.AccountPoolRegistry", lambda: empty_pools
    )
    return tmp_path


def _claude_spawn_env_sequence(env: dict[str, str], project: str) -> None:
    """spawn_engine.py claude branch, env-shaping calls only, same order."""
    from agent_takkub.spawn_engine import _apply_v2_account_env_override

    pane_env.inject_user_profile_env(env, project)
    pane_env.inject_curated_claude_config_dir(env, project)
    _apply_v2_account_env_override(env, "claude", project, "backend")
    env["TAKKUB_PROJECT"] = project
    cfg.apply_claude_auth_overrides(env)


def test_claude_spawn_keeps_curated_dir_and_applies_base_profile_auth(spawn_env_sandbox):
    tmp_path = spawn_env_sandbox
    base = tmp_path / "claude-work"
    base.mkdir()
    (base / ".credentials.json").write_text('{"token": "t"}', encoding="utf-8")
    user_profile.add_profile("work", str(base))
    user_profile.set_profile(PROJECT, "work")
    cfg.save_claude_auth(cfg.ClaudeAuthConfig(base_url="https://proxy.example", api_key="k"), base)

    env: dict[str, str] = {}
    _claude_spawn_env_sequence(env, PROJECT)

    curated = user_profile.curated_config_dir_for(PROJECT)
    # Finding 5: the curated dir must survive the V2 account override.
    assert Path(env["CLAUDE_CONFIG_DIR"]) == curated
    assert curated.parent.name == "claude" and curated.parent.parent.name == "providers"
    assert (curated / ".credentials.json").is_file()
    assert not (curated / cfg._AUTH_FILENAME).exists()
    # Finding 3: the base profile's auth override still reaches the pane.
    assert env["ANTHROPIC_BASE_URL"] == "https://proxy.example"
    assert env["ANTHROPIC_API_KEY"] == "k"


def test_claude_spawn_default_project_is_not_curated(spawn_env_sandbox):
    """'default' is exempt from curation: the var stays whatever the legacy
    injector set (the sandbox has DATA_HOME != REPO_ROOT, i.e. the installed-
    build default dir) and the override does not move it either."""
    env: dict[str, str] = {}
    _claude_spawn_env_sequence(env, "default")
    assert Path(env["CLAUDE_CONFIG_DIR"]) == user_profile._DEFAULT_CONFIG_DIR
    assert "providers" not in Path(env["CLAUDE_CONFIG_DIR"]).parts


def test_codex_named_account_home_still_set_by_legacy_injector(spawn_env_sandbox, monkeypatch):
    """Parity: the override's legacy fallback was redundant for codex
    (`inject_provider_home_env` already applies the named account's home),
    so stripping config_dir from it changes nothing for codex panes."""
    from agent_takkub.spawn_engine import _apply_v2_account_env_override

    tmp_path = spawn_env_sandbox
    home = tmp_path / "codex-work"
    user_profile.add_profile("codex-work", str(home), provider="codex")
    user_profile.set_profile(PROJECT, "codex-work", provider="codex")

    env: dict[str, str] = {}
    pane_env.inject_provider_home_env(env, "codex", PROJECT)
    assert env["CODEX_HOME"] == str(home)
    before = dict(env)
    _apply_v2_account_env_override(env, "codex", PROJECT, "codex")
    assert env == before
