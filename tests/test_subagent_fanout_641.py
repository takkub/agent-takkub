"""#641 — subagent fan-out: ``assign --shards N`` on a non-browser role opens
ONE pane that dispatches N native subagents instead of N panes.

Covers the pure decision (`shard_fanout.resolve_shard_fanout`), the task
contract block, the CLI request shape, the auto-mode guard, and the
spawn-side tool-list adjustment (`spawn_engine._fanout_tool_lists`) — the
pieces that decide whether a fan-out pane can actually spawn children.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_takkub import cli
from agent_takkub import shard_fanout as sf
from agent_takkub.orchestrator import resolve_auto_assign_mode
from agent_takkub.provider_spec import (
    PROVIDER_REGISTRY,
    TEAMMATE_CUT_TOOLS,
    TEAMMATE_DEFAULT_BUILTIN_TOOLS,
)
from agent_takkub.spawn_engine import (
    CLAUDE_SUBAGENT_TOOL,
    _fanout_tool_lists,
    _stamp_subagent_fanout_env,
)


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch):
    """Pin the effective provider every role resolves to (isolates the tests
    from ~/.takkub role-providers config)."""
    import agent_takkub.provider_config as pc
    import agent_takkub.team_preset as tp

    def _pin(name: str) -> None:
        monkeypatch.setattr(pc, "effective_provider_for", lambda role, project=None: name)
        monkeypatch.setattr(tp, "settings_role_for", lambda role, project=None: role)

    return _pin


# ── ProviderSpec: which CLIs have a native subagent ──────────────────────────


def test_native_subagent_hint_per_provider():
    assert PROVIDER_REGISTRY["claude"].native_subagent_hint
    assert PROVIDER_REGISTRY["codex"].native_subagent_hint
    assert PROVIDER_REGISTRY["gemini"].native_subagent_hint
    assert PROVIDER_REGISTRY["opencode"].native_subagent_hint
    # Not verified → must fall back to pane fan-out, never guess.
    assert PROVIDER_REGISTRY["kimi"].native_subagent_hint == ""
    assert PROVIDER_REGISTRY["cursor"].native_subagent_hint == ""


def test_claude_deny_list_names_the_current_tool():
    # Claude Code renamed Task → Agent. The CLI still honours "Task" as an
    # alias (probed on 2.1.268), so this pins the spelling, not a behaviour
    # fix: the fan-out allowlist adds the SAME name back, and the two halves
    # must agree or an ordinary pane could end up able to fan out.
    assert PROVIDER_REGISTRY["claude"].disallowed_tools == ("Agent",)
    assert "Agent" not in TEAMMATE_DEFAULT_BUILTIN_TOOLS
    assert "Agent" in TEAMMATE_CUT_TOOLS


# ── resolve_shard_fanout ─────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["frontend", "backend", "mobile", "devops"])
def test_auto_picks_subagent_for_claude_implementer(provider, role):
    provider("claude")
    kind, note = sf.resolve_shard_fanout(role, 5, project="p")
    assert kind == "subagent"
    assert note and "5 native subagents" in note and "claude" in note


@pytest.mark.parametrize("name", ["codex", "gemini", "opencode"])
def test_auto_picks_subagent_for_other_supported_providers(provider, name):
    provider(name)
    kind, note = sf.resolve_shard_fanout("backend", 3, project="p")
    assert kind == "subagent"
    assert name in (note or "")


@pytest.mark.parametrize("name", ["kimi", "cursor"])
def test_auto_falls_back_to_pane_without_native_subagent(provider, name):
    provider(name)
    kind, note = sf.resolve_shard_fanout("backend", 3, project="p")
    assert kind == "pane"
    assert note and name in note and "3 pane" in note


def test_explicit_subagent_errors_without_native_subagent(provider):
    provider("kimi")
    kind, note = sf.resolve_shard_fanout("backend", 3, fanout="subagent", project="p")
    assert kind == "error"
    assert "kimi" in (note or "")


@pytest.mark.parametrize(
    ("role", "mode"),
    [("qa", None), ("critic", None), ("designer", None), ("reviewer", "e2e"), ("reviewer", "ui")],
)
def test_browser_qa_shards_stay_on_panes(provider, role, mode):
    provider("claude")
    kind, note = sf.resolve_shard_fanout(role, 4, mode=mode, project="p")
    assert kind == "pane"
    assert note and "browser" in note


def test_reviewer_code_mode_is_not_a_browser_shard(provider):
    provider("claude")
    kind, _ = sf.resolve_shard_fanout("reviewer", 3, mode="code", project="p")
    assert kind == "subagent"


def test_explicit_subagent_on_browser_role_errors(provider):
    provider("claude")
    kind, note = sf.resolve_shard_fanout("qa", 4, fanout="subagent", project="p")
    assert kind == "error"
    assert "#92" in (note or "")


def test_plan_and_lead_subagent_mode_keep_legacy_path(provider):
    provider("claude")
    assert sf.resolve_shard_fanout("qa", 4, plan=True, project="p")[0] == "pane"
    assert sf.resolve_shard_fanout("backend", 4, mode="subagent", project="p")[0] == "pane"
    assert sf.resolve_shard_fanout("backend", 4, plan=True, fanout="subagent")[0] == "error"
    assert sf.resolve_shard_fanout("backend", 4, mode="subagent", fanout="subagent")[0] == "error"


def test_fanout_pane_forces_legacy_and_single_shard_is_noop(provider):
    provider("claude")
    assert sf.resolve_shard_fanout("frontend", 5, fanout="pane", project="p") == ("pane", None)
    assert sf.resolve_shard_fanout("frontend", 1, project="p") == ("pane", None)
    assert sf.resolve_shard_fanout("frontend", 2, fanout="bogus")[0] == "error"


def test_provider_override_wins_over_role_setting(provider):
    provider("kimi")  # role would resolve to kimi …
    kind, note = sf.resolve_shard_fanout("backend", 3, provider="codex", project="p")
    assert kind == "subagent" and "codex" in (note or "")


# ── task contract block ──────────────────────────────────────────────────────


def test_wrap_task_carries_marker_count_tool_and_fallback():
    out = sf.wrap_subagent_fanout_task("build 5 pages", 5, "claude", "Agent tool")
    assert out.startswith("build 5 pages")
    assert f"{sf.TASK_MARKER} 5" in out
    assert "Agent tool" in out
    assert "takkub done" in out and "ครั้งเดียว" in out
    # Never stall: sequential fallback when the tool is missing.
    assert "ทำทีละชิ้นเองต่อจนจบ" in out
    # Subagents must not drive the browser or report on their own.
    assert "Playwright" in out


# ── CLI request shape ────────────────────────────────────────────────────────


@pytest.fixture
def fake_request(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    def _fake(payload: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        seen.append(payload)
        return {"ok": True, "msg": "task queued for backend (sending when ready)"}

    monkeypatch.setattr(cli, "_request", _fake)
    monkeypatch.delenv("TAKKUB_ROLE", raising=False)
    monkeypatch.delenv("TAKKUB_PROJECT", raising=False)
    return seen


def test_cli_shards_sends_one_assign_with_subagent_fanout(provider, fake_request, capsys):
    provider("claude")
    rc = cli.main(["assign", "--role", "backend", "--shards", "5", "build five endpoints"])
    assert rc == 0
    assert len(fake_request) == 1
    payload = fake_request[0]
    assert payload["role"] == "backend"
    assert payload["subagent_fanout"] == 5
    assert payload["shard_total"] == 0
    assert "fan-out #641" in capsys.readouterr().out


def test_cli_fanout_pane_keeps_n_shard_requests(provider, fake_request):
    provider("claude")
    cli.main(["assign", "--role", "backend", "--shards", "3", "--fanout", "pane", "x"])
    assert [p["role"] for p in fake_request] == ["backend#1", "backend#2", "backend#3"]
    assert all(p["shard_total"] == 3 for p in fake_request)
    assert all("subagent_fanout" not in p for p in fake_request)


def test_cli_browser_role_still_fans_out_panes(provider, fake_request):
    provider("claude")
    cli.main(["assign", "--role", "reviewer", "--mode", "e2e", "--shards", "2", "smoke"])
    assert len(fake_request) == 2


def test_cli_explicit_subagent_on_qa_is_rejected(provider, fake_request):
    provider("claude")
    rc = cli.main(["assign", "--role", "qa", "--shards", "2", "--fanout", "subagent", "smoke"])
    assert rc == 1
    assert fake_request == []


def test_cli_subagent_fanout_allows_auto_chain(provider, fake_request):
    provider("claude")
    rc = cli.main(["assign", "--role", "backend", "--shards", "2", "--auto-chain", "x"])
    assert rc == 0 and fake_request[0]["auto_chain"] is True


def test_cli_pane_fanout_still_rejects_auto_chain(provider, fake_request):
    provider("claude")
    rc = cli.main(
        ["assign", "--role", "backend", "--shards", "2", "--fanout", "pane", "--auto-chain", "x"]
    )
    assert rc == 1 and fake_request == []


def test_cli_explicit_subagent_widens_shard_cap(provider, fake_request):
    provider("claude")
    assert (
        cli.main(["assign", "--role", "backend", "--shards", "12", "--fanout", "subagent", "x"])
        == 0
    )
    assert cli.main(["assign", "--role", "backend", "--shards", "12", "x"]) == 1


# ── auto assign mode ─────────────────────────────────────────────────────────


def test_auto_mode_never_turns_fanout_into_lead_subagent(provider):
    provider("claude")
    mode, note = resolve_auto_assign_mode(
        "backend",
        "tiny",
        requested_mode=None,
        isolation="shared",
        model=None,
        provider=None,
        effort=None,
        plan=False,
        shard_total=0,
        project="p",
        subagent_fanout=4,
    )
    assert mode == "pane" and note is None


# ── spawn-side tool lists / env ──────────────────────────────────────────────


def test_fanout_tool_lists_unchanged_for_ordinary_pane():
    tools, denied = _fanout_tool_lists(["Bash", "Read"], ["Agent"], 0)
    assert tools == ["Bash", "Read"] and denied == ["Agent"]


def test_fanout_tool_lists_allow_agent_for_fanout_pane():
    tools, denied = _fanout_tool_lists(["Bash", "Read"], ["Agent", "Task"], 5)
    assert tools == ["Bash", "Read", CLAUDE_SUBAGENT_TOOL]
    assert denied == []
    # An empty allowlist (--tools disabled via env) stays empty — nothing to append to.
    assert _fanout_tool_lists([], ["Agent"], 5) == ([], [])


def test_stamp_env_sets_and_clears():
    env = {sf.ENV_SUBAGENT_FANOUT: "9"}
    _stamp_subagent_fanout_env(env, 0)
    assert sf.ENV_SUBAGENT_FANOUT not in env
    _stamp_subagent_fanout_env(env, 3)
    assert env[sf.ENV_SUBAGENT_FANOUT] == "3"
