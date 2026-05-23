"""Tests for `_apply_mcp_timeout`, the helper that raises the per-call
MCP tool timeout from CC's 60-second default to 3 minutes for every
cockpit-spawned claude session.

Why this matters: Playwright / chrome-devtools MCP operations (first
page load, Lighthouse audit, screenshot with network-idle) routinely
exceed 60s and used to fail with a generic MCP timeout — wasting an
entire QA / critic round. The fix is a per-pane default that the
operator can still override at the cockpit level.
"""

from __future__ import annotations

from agent_takkub.orchestrator import (
    _DEFAULT_MCP_TOOL_TIMEOUT_MS,
    _apply_mcp_timeout,
    _build_pane_env,
)


class TestApplyMcpTimeout:
    def test_sets_default_on_empty_env(self) -> None:
        env: dict[str, str] = {}
        _apply_mcp_timeout(env)
        assert env["MCP_TOOL_TIMEOUT"] == _DEFAULT_MCP_TOOL_TIMEOUT_MS

    def test_default_is_three_minutes_in_ms(self) -> None:
        # CC's MCP_TOOL_TIMEOUT is in milliseconds. Pin the value so a
        # well-meaning future tweak to "180" (seconds) doesn't silently
        # drop the ceiling back below the page-load threshold it was
        # raised to fix.
        assert _DEFAULT_MCP_TOOL_TIMEOUT_MS == "180000"

    def test_preserves_user_provided_value(self) -> None:
        # If the operator already set MCP_TOOL_TIMEOUT at the cockpit
        # level (e.g. a slow Lighthouse audit suite needs 10 minutes),
        # we must not silently clobber it. setdefault — first writer wins.
        env = {"MCP_TOOL_TIMEOUT": "600000"}
        _apply_mcp_timeout(env)
        assert env["MCP_TOOL_TIMEOUT"] == "600000"

    def test_no_return_value(self) -> None:
        # Mutates in place — callers in `spawn()` rely on this rather
        # than capturing a return. Pin that contract.
        env: dict[str, str] = {}
        result = _apply_mcp_timeout(env)
        assert result is None


class TestMcpTimeoutInAllowlist:
    def test_user_set_value_passes_through_pane_env(self, monkeypatch) -> None:
        # The allowlist gates which host env vars reach the pane.
        # MCP_TOOL_TIMEOUT must pass through so a cockpit-level override
        # actually reaches the spawned claude session (and the helper's
        # setdefault then becomes a no-op for that pane).
        monkeypatch.setenv("MCP_TOOL_TIMEOUT", "300000")
        env = _build_pane_env()
        assert env.get("MCP_TOOL_TIMEOUT") == "300000"
