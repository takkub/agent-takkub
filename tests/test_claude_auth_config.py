"""Tests for optional Claude auth override config."""

from __future__ import annotations

from pathlib import Path

from agent_takkub import claude_auth_config as cfg


def test_missing_config_means_no_env_overrides(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cfg, "_DEFAULT_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "_LEGACY_GLOBAL_PATH", tmp_path / "legacy-absent.json")
    assert cfg.load_claude_auth().active_env() == {}


def test_blank_values_do_not_clobber_existing_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cfg, "_DEFAULT_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "_LEGACY_GLOBAL_PATH", tmp_path / "legacy-absent.json")
    cfg.save_claude_auth(cfg.ClaudeAuthConfig())

    env = {"ANTHROPIC_AUTH_TOKEN": "parent-token"}
    cfg.apply_claude_auth_overrides(env)

    assert env == {"ANTHROPIC_AUTH_TOKEN": "parent-token"}


def test_nonblank_values_apply_expected_anthropic_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cfg, "_DEFAULT_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "_LEGACY_GLOBAL_PATH", tmp_path / "legacy-absent.json")
    cfg.save_claude_auth(
        cfg.ClaudeAuthConfig(
            base_url=" https://proxy.example ",
            api_key=" api-key ",
            auth_token=" auth-token ",
        )
    )

    env: dict[str, str] = {}
    cfg.apply_claude_auth_overrides(env)

    assert env == {
        "ANTHROPIC_BASE_URL": "https://proxy.example",
        "ANTHROPIC_API_KEY": "api-key",
        "ANTHROPIC_AUTH_TOKEN": "auth-token",
    }


def test_proxy_api_key_also_sets_auth_token_when_token_blank(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cfg, "_DEFAULT_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "_LEGACY_GLOBAL_PATH", tmp_path / "legacy-absent.json")
    cfg.save_claude_auth(
        cfg.ClaudeAuthConfig(base_url="https://proxy.example", api_key="proxy-token")
    )

    env: dict[str, str] = {}
    cfg.apply_claude_auth_overrides(env)

    assert env["ANTHROPIC_API_KEY"] == "proxy-token"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "proxy-token"


def test_extra_env_round_trips_and_injects(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cfg, "_DEFAULT_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "_LEGACY_GLOBAL_PATH", tmp_path / "legacy-absent.json")
    cfg.save_claude_auth(
        cfg.ClaudeAuthConfig(extra_env={"DEEPSEEK_API_KEY": "sk-123", " ": "dropped", "X": ""})
    )

    loaded = cfg.load_claude_auth()
    # Blank-name rows are dropped on save; blank-VALUE rows persist but are not
    # emitted into the env (active_env skips empty values).
    assert loaded.extra_env == {"DEEPSEEK_API_KEY": "sk-123", "X": ""}

    env: dict[str, str] = {}
    cfg.apply_claude_auth_overrides(env)
    assert env == {"DEEPSEEK_API_KEY": "sk-123"}


def test_structured_fields_win_over_extra_env_collision(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cfg, "_DEFAULT_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "_LEGACY_GLOBAL_PATH", tmp_path / "legacy-absent.json")
    cfg.save_claude_auth(
        cfg.ClaudeAuthConfig(
            base_url="https://proxy.example",
            extra_env={"ANTHROPIC_BASE_URL": "https://stray.example", "FOO": "bar"},
        )
    )

    env: dict[str, str] = {}
    cfg.apply_claude_auth_overrides(env)

    assert env["ANTHROPIC_BASE_URL"] == "https://proxy.example"
    assert env["FOO"] == "bar"


def test_auth_is_isolated_per_profile_dir(tmp_path: Path) -> None:
    """A base URL saved for one profile dir must not leak into another."""
    dir_a = tmp_path / "profile-a"
    dir_b = tmp_path / "profile-b"
    cfg.save_claude_auth(cfg.ClaudeAuthConfig(base_url="https://a.example"), dir_a)

    # Profile B never had anything saved → blank config → no overrides.
    assert cfg.load_claude_auth(dir_b).active_env() == {}
    assert cfg.load_claude_auth(dir_a).base_url == "https://a.example"


def test_apply_resolves_profile_from_claude_config_dir(tmp_path: Path) -> None:
    """apply_claude_auth_overrides keys off the pane's CLAUDE_CONFIG_DIR."""
    dir_a = tmp_path / "profile-a"
    dir_b = tmp_path / "profile-b"
    cfg.save_claude_auth(cfg.ClaudeAuthConfig(base_url="https://a.example"), dir_a)

    env_a = {"CLAUDE_CONFIG_DIR": str(dir_a)}
    cfg.apply_claude_auth_overrides(env_a)
    assert env_a["ANTHROPIC_BASE_URL"] == "https://a.example"

    # A pane pointed at profile B (no auth file) gets no override.
    env_b = {"CLAUDE_CONFIG_DIR": str(dir_b)}
    cfg.apply_claude_auth_overrides(env_b)
    assert "ANTHROPIC_BASE_URL" not in env_b


def test_apply_reads_base_profile_when_pane_runs_on_curated_dir(
    tmp_path: Path, monkeypatch
) -> None:
    """System review 2026-09-23 (claude_auth_config.py:177): a pane spawned on
    the #563 curated CLAUDE_CONFIG_DIR must still pick up the auth override
    Settings saved to the profile's BASE dir — the curated dir never mirrors
    takkub-claude-auth.json."""
    from agent_takkub import user_profile as up

    monkeypatch.setattr("agent_takkub.config.DATA_HOME", tmp_path / "data_home")
    monkeypatch.setattr(cfg, "_LEGACY_GLOBAL_PATH", tmp_path / "legacy-absent.json")
    base = tmp_path / "claude-work"
    up.add_profile("work", str(base))
    up.set_profile("proj", "work")
    # What Settings → Claude Auth does: save to the profile's config_dir.
    cfg.save_claude_auth(cfg.ClaudeAuthConfig(base_url="https://proxy.example", api_key="k"), base)

    curated = up.curated_config_dir_for("proj")
    curated.mkdir(parents=True)
    assert not (curated / cfg._AUTH_FILENAME).exists()

    env = {"CLAUDE_CONFIG_DIR": str(curated), "TAKKUB_PROJECT": "proj"}
    cfg.apply_claude_auth_overrides(env)
    assert env["ANTHROPIC_BASE_URL"] == "https://proxy.example"
    assert env["ANTHROPIC_API_KEY"] == "k"
    # The remap is read-only: the pane keeps running on the curated dir.
    assert env["CLAUDE_CONFIG_DIR"] == str(curated)


def test_apply_curated_dir_maps_to_default_profile_dir(tmp_path: Path, monkeypatch) -> None:
    """Default profile on the curated dir → the default profile's own file."""
    from agent_takkub import user_profile as up

    monkeypatch.setattr("agent_takkub.config.DATA_HOME", tmp_path / "data_home")
    default_dir = tmp_path / "dot-claude"
    monkeypatch.setattr(up, "_DEFAULT_CONFIG_DIR", default_dir)
    monkeypatch.setattr(cfg, "_DEFAULT_CONFIG_DIR", default_dir)
    monkeypatch.setattr(cfg, "_LEGACY_GLOBAL_PATH", tmp_path / "legacy-absent.json")
    cfg.save_claude_auth(cfg.ClaudeAuthConfig(base_url="https://default.example"))

    curated = up.curated_config_dir_for("proj")
    env = {"CLAUDE_CONFIG_DIR": str(curated), "TAKKUB_PROJECT": "proj"}
    cfg.apply_claude_auth_overrides(env)
    assert env["ANTHROPIC_BASE_URL"] == "https://default.example"


def test_apply_non_curated_dir_is_read_as_is_even_with_project(tmp_path: Path, monkeypatch) -> None:
    """Narrow remap: any other CLAUDE_CONFIG_DIR (e.g. a V2 pool account's own
    home) keeps reading ITS file, never the project's base profile."""
    from agent_takkub import user_profile as up

    monkeypatch.setattr("agent_takkub.config.DATA_HOME", tmp_path / "data_home")
    base = tmp_path / "claude-work"
    up.add_profile("work", str(base))
    up.set_profile("proj", "work")
    cfg.save_claude_auth(cfg.ClaudeAuthConfig(base_url="https://base.example"), base)
    pool_home = tmp_path / "pool-home"
    cfg.save_claude_auth(cfg.ClaudeAuthConfig(base_url="https://pool.example"), pool_home)

    env = {"CLAUDE_CONFIG_DIR": str(pool_home), "TAKKUB_PROJECT": "proj"}
    cfg.apply_claude_auth_overrides(env)
    assert env["ANTHROPIC_BASE_URL"] == "https://pool.example"

    # No project tag at all (non-spawn callers) → unchanged historical lookup.
    env2 = {"CLAUDE_CONFIG_DIR": str(base)}
    cfg.apply_claude_auth_overrides(env2)
    assert env2["ANTHROPIC_BASE_URL"] == "https://base.example"


def test_default_profile_falls_back_to_legacy_global(tmp_path: Path, monkeypatch) -> None:
    """Existing ~/.takkub/claude-auth.json keeps working for the default profile."""
    default_dir = tmp_path / "dot-claude"
    default_dir.mkdir()
    legacy = tmp_path / "legacy" / "claude-auth.json"
    legacy.parent.mkdir()
    legacy.write_text('{"base_url": "https://legacy.example"}', encoding="utf-8")
    monkeypatch.setattr(cfg, "_DEFAULT_CONFIG_DIR", default_dir)
    monkeypatch.setattr(cfg, "_LEGACY_GLOBAL_PATH", legacy)

    # Default profile (config_dir=None) inherits the legacy file...
    assert cfg.load_claude_auth().base_url == "https://legacy.example"
    # ...but a non-default profile never touches the legacy fallback.
    assert cfg.load_claude_auth(tmp_path / "other").base_url == ""
