"""core.capabilities.permission_engine.PermissionEngine — the 2-layer tool
gate kept as two independent checks (Phase 5c, epic #309). Asserts BOTH
layers stay reachable through this façade and that neither one substitutes
for the other (matrix §3: "ห้ามยุบเหลือชั้นเดียว")."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_takkub import config as config_mod
from agent_takkub import pane_tools_policy
from agent_takkub.core.capabilities.permission_engine import PermissionEngine


@pytest.fixture
def engine() -> PermissionEngine:
    return PermissionEngine()


def test_mcp_allowed_delegates_to_pane_tools_policy_layer_1(
    monkeypatch: pytest.MonkeyPatch, engine: PermissionEngine
) -> None:
    monkeypatch.setattr(
        pane_tools_policy, "effective_mcps", lambda role, default=None: frozenset({"graft"})
    )

    assert engine.mcp_allowed("backend") == frozenset({"graft"})


def test_mcp_allowed_none_means_no_policy(
    monkeypatch: pytest.MonkeyPatch, engine: PermissionEngine
) -> None:
    monkeypatch.setattr(pane_tools_policy, "effective_mcps", lambda role, default=None: None)

    assert engine.mcp_allowed("backend") is None


def test_evaluate_shell_command_delegates_to_pane_guard_layer_2(
    engine: PermissionEngine,
) -> None:
    # A real pane_guard.classify() call (unmocked) — layer 2 must still be
    # the actual guard logic, not a stub, so this is deliberately not
    # monkeypatched: proves evaluate_shell_command really enforces the
    # browser_driver rule and doesn't quietly no-op.
    verdict = engine.evaluate_shell_command("npx --yes playwright install", "backend")

    assert verdict.allowed is False
    assert verdict.rule.startswith("browser_driver")


def test_evaluate_shell_command_allows_unguarded_role(engine: PermissionEngine) -> None:
    verdict = engine.evaluate_shell_command("npx --yes playwright install", "qa")

    assert verdict.allowed is True


def test_evaluate_shell_command_never_bypasses_layer_2_even_when_layer_1_open(
    monkeypatch: pytest.MonkeyPatch, engine: PermissionEngine
) -> None:
    """A role with NO cockpit MCP policy (layer 1 wide open — `None`) must
    still be denied at layer 2 for a guarded command — proves the two
    layers are genuinely independent, not short-circuited by each other."""
    monkeypatch.setattr(pane_tools_policy, "effective_mcps", lambda role, default=None: None)
    assert engine.mcp_allowed("backend") is None

    verdict = engine.evaluate_shell_command("taskkill /F /T /IM node.exe", "backend")

    assert verdict.allowed is False
    assert verdict.rule.startswith("host_destructive")


def test_evaluate_shell_command_audits_denied_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, engine: PermissionEngine
) -> None:
    events_log = tmp_path / "events.log"
    monkeypatch.setattr(config_mod, "EVENTS_LOG", events_log)
    monkeypatch.setattr(config_mod, "RUNTIME_DIR", tmp_path)

    verdict = engine.evaluate_shell_command(
        "npx --yes playwright install",
        "backend",
        agent="backend#1",
        provider="claude",
        account="default",
    )

    assert verdict.allowed is False
    lines = events_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "capability.shell_command_denied"
    assert payload["who"] == "backend"
    assert payload["agent"] == "backend#1"
    assert payload["provider"] == "claude"
    assert payload["account"] == "default"
    assert payload["tool"] == "bash"
    assert payload["rule"] == verdict.rule


def test_evaluate_shell_command_passes_mb_fallback_check_through(
    engine: PermissionEngine,
) -> None:
    """#309 Wave C gap: evaluate_shell_command must forward
    mb_fallback_check to pane_guard.classify verbatim — a command denied
    by the mb-shard rule is allowed once the callback reports a grant,
    exactly as calling pane_guard.classify directly would behave."""
    verdict_denied = engine.evaluate_shell_command(
        "mb go http://localhost:3000", "qa#1", mb_fallback_check=lambda: False
    )
    assert verdict_denied.allowed is False

    verdict_allowed = engine.evaluate_shell_command(
        "mb go http://localhost:3000", "qa#1", mb_fallback_check=lambda: True
    )
    assert verdict_allowed.allowed is True


def test_evaluate_shell_command_passes_cwd_through(engine: PermissionEngine) -> None:
    """#309 Wave C gap: evaluate_shell_command must forward cwd to
    pane_guard.classify verbatim — the #314 worktree git-commit carve-out
    only engages when cwd is threaded through."""
    worktree_cwd = r"C:\Users\dev\agent-takkub\worktrees\myproj\backend-3-1700000000"

    verdict_no_cwd = engine.evaluate_shell_command('git commit -m "x"', "backend")
    assert verdict_no_cwd.allowed is False

    verdict_with_cwd = engine.evaluate_shell_command(
        'git commit -m "x"', "backend", cwd=worktree_cwd
    )
    assert verdict_with_cwd.allowed is True


def test_evaluate_shell_command_does_not_audit_allowed_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, engine: PermissionEngine
) -> None:
    events_log = tmp_path / "events.log"
    monkeypatch.setattr(config_mod, "EVENTS_LOG", events_log)
    monkeypatch.setattr(config_mod, "RUNTIME_DIR", tmp_path)

    verdict = engine.evaluate_shell_command("git status", "backend")

    assert verdict.allowed is True
    assert not events_log.exists()
