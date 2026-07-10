"""Tests for `_apply_non_interactive_env` (issue #52 — Layer 1: Prevent).

Verifies that every cockpit-spawned pane gets npm_config_yes=true and
GIT_TERMINAL_PROMPT=0 so npx/npm/git never block on interactive y/N or
credential prompts.
"""

from __future__ import annotations

from agent_takkub.orchestrator import _apply_non_interactive_env, _build_pane_env


class TestApplyNonInteractiveEnv:
    def test_sets_npm_config_yes_on_empty_env(self) -> None:
        env: dict[str, str] = {}
        _apply_non_interactive_env(env)
        assert env["npm_config_yes"] == "true"

    def test_sets_git_terminal_prompt_on_empty_env(self) -> None:
        env: dict[str, str] = {}
        _apply_non_interactive_env(env)
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_preserves_existing_npm_config_yes(self) -> None:
        # Operator override wins (same contract as MCP_TOOL_TIMEOUT).
        env = {"npm_config_yes": "false"}
        _apply_non_interactive_env(env)
        assert env["npm_config_yes"] == "false"

    def test_preserves_existing_git_terminal_prompt(self) -> None:
        env = {"GIT_TERMINAL_PROMPT": "1"}
        _apply_non_interactive_env(env)
        assert env["GIT_TERMINAL_PROMPT"] == "1"

    def test_no_return_value(self) -> None:
        # Mutates in place — callers rely on this.
        env: dict[str, str] = {}
        result = _apply_non_interactive_env(env)
        assert result is None

    def test_does_not_remove_unrelated_keys(self) -> None:
        env = {"PATH": "/usr/bin", "HOME": "/home/user"}
        _apply_non_interactive_env(env)
        assert env["PATH"] == "/usr/bin"
        assert env["HOME"] == "/home/user"


class TestNonInteractiveEnvInAllowlist:
    def test_npm_config_yes_host_override_passes_through(self, monkeypatch) -> None:
        # npm_config_yes is NOT in the allowlist, so a host-level override
        # doesn't survive the filter. H1: _build_pane_env() now calls
        # _apply_non_interactive_env() internally (every spawn branch gets
        # it, not just claude's), so the default 'true' is already set on
        # the dict this function returns — no separate call needed.
        monkeypatch.setenv("npm_config_yes", "false")
        env = _build_pane_env()
        assert env["npm_config_yes"] == "true"

    def test_git_terminal_prompt_host_override_passes_through(self, monkeypatch) -> None:
        # GIT_TERMINAL_PROMPT is NOT in the allowlist — injected inside
        # _build_pane_env() itself now (H1), same reasoning as above.
        monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
        env = _build_pane_env()
        assert env["GIT_TERMINAL_PROMPT"] == "0"
