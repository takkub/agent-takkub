"""Tests for optional Claude auth override config."""

from __future__ import annotations

from pathlib import Path

from agent_takkub import claude_auth_config as cfg


def test_missing_config_means_no_env_overrides(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cfg, "_CONFIG_PATH", tmp_path / "claude-auth.json")
    assert cfg.load_claude_auth().active_env() == {}


def test_blank_values_do_not_clobber_existing_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cfg, "_CONFIG_PATH", tmp_path / "claude-auth.json")
    cfg.save_claude_auth(cfg.ClaudeAuthConfig())

    env = {"ANTHROPIC_AUTH_TOKEN": "parent-token"}
    cfg.apply_claude_auth_overrides(env)

    assert env == {"ANTHROPIC_AUTH_TOKEN": "parent-token"}


def test_nonblank_values_apply_expected_anthropic_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cfg, "_CONFIG_PATH", tmp_path / "claude-auth.json")
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
    monkeypatch.setattr(cfg, "_CONFIG_PATH", tmp_path / "claude-auth.json")
    cfg.save_claude_auth(
        cfg.ClaudeAuthConfig(base_url="https://proxy.example", api_key="proxy-token")
    )

    env: dict[str, str] = {}
    cfg.apply_claude_auth_overrides(env)

    assert env["ANTHROPIC_API_KEY"] == "proxy-token"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "proxy-token"


def test_extra_env_round_trips_and_injects(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cfg, "_CONFIG_PATH", tmp_path / "claude-auth.json")
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
    monkeypatch.setattr(cfg, "_CONFIG_PATH", tmp_path / "claude-auth.json")
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
