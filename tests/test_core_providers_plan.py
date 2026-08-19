"""core.providers.plan — pure SpawnPlan builders (epic #309 Phase 3b).

`test_assemble_generic_argv_matches_branch_order` is the golden-order proof
the task asked for: it re-derives the argv the generic non-claude branch
(spawn_engine.py ~1873-1977, verified 2026-08-19) would build for a given
set of resolved pieces BY HAND (bin, autonomy, model, effort, mcp,
project-scope, resume — in that literal order) and asserts
`assemble_generic_argv` produces exactly the same list.
"""

from __future__ import annotations

from agent_takkub.core.accounts.legacy_reader import read_legacy_accounts
from agent_takkub.core.models.account import ProviderAccount
from agent_takkub.core.models.spawn_plan import SpawnPlan
from agent_takkub.core.providers.claude_adapter import ClaudeCliAdapter
from agent_takkub.core.providers.cli_adapter import CliProviderAdapter
from agent_takkub.core.providers.plan import (
    account_env_overrides,
    assemble_generic_argv,
    build_generic_spawn_plan,
)


def _account(provider="claude", config_dir=None):
    return ProviderAccount(id="a1", provider_id=provider, config_dir=config_dir)


# ── account_env_overrides ────────────────────────────────────────────────


def test_account_env_overrides_empty_for_no_account():
    assert account_env_overrides("claude", None) == {}


def test_account_env_overrides_empty_for_no_config_dir():
    assert account_env_overrides("claude", _account(config_dir=None)) == {}


def test_account_env_overrides_claude_sets_config_dir():
    got = account_env_overrides("claude", _account(config_dir="/home/u/.claude-work"))
    assert got == {"CLAUDE_CONFIG_DIR": "/home/u/.claude-work"}


def test_account_env_overrides_codex_sets_codex_home():
    got = account_env_overrides("codex", _account(provider="codex", config_dir="/home/u/codex-b"))
    assert got == {"CODEX_HOME": "/home/u/codex-b"}


def test_account_env_overrides_empty_for_provider_with_no_isolation_knob():
    got = account_env_overrides("gemini", _account(provider="gemini", config_dir="/whatever"))
    assert got == {}


# ── assemble_generic_argv (golden order) ─────────────────────────────────


def test_assemble_generic_argv_matches_branch_order():
    # Mirrors spawn_engine.py's generic branch: provider_argv = [bin, *autonomy];
    # .extend(model); _append_provider_effort(->extend effort);
    # .extend(mcp); .extend/.append(project_scope); .extend(resume).
    bin_path = "/usr/bin/codex"
    autonomy = ["--full-auto"]
    model = ["--model", "gpt-5"]
    effort = ["-c", "model_reasoning_effort=high"]
    mcp = ["-c", "mcp_servers={}"]
    project_scope = ["--project-id", "abc123"]
    resume = ["--resume", "uuid-1"]

    expected: list[str] = [bin_path]
    expected.extend(autonomy)
    expected.extend(model)
    expected.extend(effort)
    expected.extend(mcp)
    expected.extend(project_scope)
    expected.extend(resume)

    got = assemble_generic_argv(
        bin_path,
        autonomy_argv=autonomy,
        model_argv=model,
        effort_argv=effort,
        mcp_argv=mcp,
        project_scope_argv=project_scope,
        resume_argv=resume,
    )
    assert got == expected


def test_assemble_generic_argv_omits_empty_pieces():
    got = assemble_generic_argv("/usr/bin/agy", autonomy_argv=["--yolo"])
    assert got == ["/usr/bin/agy", "--yolo"]


# ── build_generic_spawn_plan / adapters ──────────────────────────────────


def test_build_generic_spawn_plan_applies_account_env_without_mutating_base():
    base_env = {"PATH": "/bin"}
    account = _account(provider="codex", config_dir="/home/u/codex-b")
    plan = build_generic_spawn_plan(
        "codex",
        "/usr/bin/codex",
        base_env,
        "/work",
        account=account,
        autonomy_argv=["--full-auto"],
    )
    assert isinstance(plan, SpawnPlan)
    assert plan.provider_id == "codex"
    assert plan.argv == ("/usr/bin/codex", "--full-auto")
    assert plan.env == {"PATH": "/bin", "CODEX_HOME": "/home/u/codex-b"}
    assert base_env == {"PATH": "/bin"}  # never mutated


def test_cli_provider_adapter_build_plan_matches_pure_builder():
    account = _account(provider="codex", config_dir="/home/u/codex-b")
    plan = CliProviderAdapter("codex").build_plan(
        provider_bin="/usr/bin/codex",
        base_env={"PATH": "/bin"},
        cwd="/work",
        account=account,
        autonomy_argv=["--full-auto"],
        resume_argv=["--resume", "uuid-1"],
    )
    assert plan.argv == ("/usr/bin/codex", "--full-auto", "--resume", "uuid-1")
    assert plan.env["CODEX_HOME"] == "/home/u/codex-b"


def test_claude_adapter_build_plan_wraps_caller_built_argv():
    account = _account(provider="claude", config_dir="/home/u/.claude-work")
    plan = ClaudeCliAdapter().build_plan(
        argv=["claude", "--dangerously-skip-permissions"],
        base_env={"PATH": "/bin"},
        cwd="/work",
        account=account,
    )
    assert plan.provider_id == "claude"
    assert plan.argv == ("claude", "--dangerously-skip-permissions")
    assert plan.env == {"PATH": "/bin", "CLAUDE_CONFIG_DIR": "/home/u/.claude-work"}


def test_account_env_overrides_matches_legacy_selected_profile(monkeypatch, tmp_path):
    # Parity proof: the config_dir a real legacy-reader account carries is
    # the SAME value pane_env.inject_user_profile_env would already have
    # set — so wiring account_env_overrides in changes nothing observable
    # for the untouched legacy (no V2 pool) case.
    from agent_takkub import user_profile as up

    monkeypatch.setattr(up, "_BASE_DIR", tmp_path)
    monkeypatch.setattr(up, "_REGISTRY_PATH", tmp_path / "user-profiles.json")
    monkeypatch.setattr(up, "_DEFAULT_CONFIG_DIR", tmp_path / "dot-claude")

    accounts = read_legacy_accounts()
    default_account = next(a for a in accounts if a.id == "legacy-claude-default")
    assert default_account.config_dir == str(tmp_path / "dot-claude")
    assert account_env_overrides("claude", default_account) == {
        "CLAUDE_CONFIG_DIR": str(tmp_path / "dot-claude")
    }
