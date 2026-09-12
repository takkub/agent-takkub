"""Tests for teammate built-in tool schema filtering (#581 Phase 1).

Covers:
- Tool list constants (default, cut, teammate baseline)
- ProviderSpec tools_flag capability mapping & gaps
- _teammate_builtin_tools resolver and TAKKUB_TEAMMATE_TOOLS env override
- Claude teammate spawn includes --tools with core tools and excludes cut tools
- TAKKUB_TEAMMATE_TOOLS="" disables --tools entirely (escape hatch)
- Lead pane is unrestricted (no --tools in argv)
- Non-supporting providers (codex, gemini) do not receive --tools
- Doctor silent drift check warns on unexpected new built-in tools
"""

from __future__ import annotations

import uuid as _uuid_mod
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.core.providers.plan import assemble_generic_argv
from agent_takkub.doctor import Status, check_claude_tools_drift
from agent_takkub.orchestrator import Orchestrator
from agent_takkub.provider_spec import (
    CLAUDE_DEFAULT_BUILTIN_TOOLS,
    TEAMMATE_CUT_TOOLS,
    TEAMMATE_DEFAULT_BUILTIN_TOOLS,
    claude_spec,
    codex_spec,
    cursor_spec,
    gemini_spec,
    kimi_spec,
    opencode_spec,
)
from agent_takkub.roles import LEAD
from agent_takkub.spawn_engine import _teammate_builtin_tools

CORE_TOOLS = ("Bash", "Read", "Edit", "Grep", "Write", "Glob", "PowerShell")
CUT_TOOLS_8 = (
    "BashOutput",
    "ExitPlanMode",
    "KillShell",
    "MultiEdit",
    "NotebookEdit",
    "SlashCommand",
    "Task",
    "TodoWrite",
)

TEST_PROJECT = "teammatebuiltinargvtest"
FAKE_CWD = "/tmp/takkub-test-teammate-tools-cwd"
FIXED_UUID = _uuid_mod.UUID("00000000-0000-4000-8000-000000000042")
FAKE_MCP_ARGV = ["--mcp-config", "/tmp/fake-mcp-config.json", "--strict-mcp-config"]


# ── Constants & Spec Checks ──────────────────────────────────────────────────


def test_tool_constants_definition():
    """Verify tool baseline, cut tools, and teammate default set."""
    assert set(TEAMMATE_CUT_TOOLS) == set(CUT_TOOLS_8)
    for t in CORE_TOOLS:
        assert t in CLAUDE_DEFAULT_BUILTIN_TOOLS
        assert t in TEAMMATE_DEFAULT_BUILTIN_TOOLS
    for t in CUT_TOOLS_8:
        assert t in CLAUDE_DEFAULT_BUILTIN_TOOLS
        assert t not in TEAMMATE_DEFAULT_BUILTIN_TOOLS
    # Also verify Skill and WebFetch are preserved (#581 scope)
    assert "Skill" in TEAMMATE_DEFAULT_BUILTIN_TOOLS
    assert "WebFetch" in TEAMMATE_DEFAULT_BUILTIN_TOOLS
    # Verify tools discovered from real session transcripts (#581)
    for t in ("ListAgents", "SendMessage", "PushNotification"):
        assert t in CLAUDE_DEFAULT_BUILTIN_TOOLS
        assert t in TEAMMATE_DEFAULT_BUILTIN_TOOLS


def test_provider_spec_tools_flag_mapping():
    """Claude has tools_flag='--tools'; unsupported providers are None."""
    assert claude_spec.tools_flag == "--tools"
    assert codex_spec.tools_flag is None
    assert gemini_spec.tools_flag is None
    assert opencode_spec.tools_flag is None
    assert kimi_spec.tools_flag is None
    assert cursor_spec.tools_flag is None


# ── _teammate_builtin_tools Resolver ─────────────────────────────────────────


def test_teammate_builtin_tools_default(monkeypatch):
    monkeypatch.delenv("TAKKUB_TEAMMATE_TOOLS", raising=False)
    tools = _teammate_builtin_tools()
    assert tools == list(TEAMMATE_DEFAULT_BUILTIN_TOOLS)


def test_teammate_builtin_tools_escape_hatch_empty(monkeypatch):
    monkeypatch.setenv("TAKKUB_TEAMMATE_TOOLS", "")
    assert _teammate_builtin_tools() == []


def test_teammate_builtin_tools_custom_override(monkeypatch):
    monkeypatch.setenv("TAKKUB_TEAMMATE_TOOLS", "Bash,Read,Edit")
    assert _teammate_builtin_tools() == ["Bash", "Read", "Edit"]

    monkeypatch.setenv("TAKKUB_TEAMMATE_TOOLS", "Bash Read Edit")
    assert _teammate_builtin_tools() == ["Bash", "Read", "Edit"]


# ── Spawn Engine Integration ─────────────────────────────────────────────────


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _make_orchestrator(qapp, monkeypatch):
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda p: p or TEST_PROJECT),
    )
    o = Orchestrator()
    o.shutdown_timers()
    return o


_PATCHES: list[tuple[str, object]] = [
    ("agent_takkub.orchestrator.find_claude_executable", "fake-claude"),
    ("agent_takkub.orchestrator._build_transcript_path", None),
    ("agent_takkub.spawn_engine._default_plugin_dirs", []),
    ("agent_takkub.spawn_engine.apply_claude_auth_overrides", None),
    ("agent_takkub.spawn_engine.agent_role_dir", "/tmp/nonexistent-staging-claude-argv-plan"),
    ("agent_takkub.hook_wiring.ensure_hook_settings_file", "/tmp/fake-hook-settings.json"),
    ("agent_takkub.mcp_bridge.mcp_argv_for_provider", FAKE_MCP_ARGV),
    ("agent_takkub.mcp_bridge.describe_mcp_handshake", {}),
    ("agent_takkub.role_models.effort_for", ""),
    ("agent_takkub.core.routing.effective_provider_for_v2", "claude"),
]


def _spawn_role_and_capture_argv(
    qapp,
    monkeypatch,
    role_name: str = "backend",
    provider: str = "claude",
) -> list[str]:
    import pathlib
    from contextlib import ExitStack

    monkeypatch.setenv("TAKKUB_TEAMMATE_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("TAKKUB_TEAMMATE_EFFORT", "high")
    monkeypatch.setenv("TAKKUB_TEAMMATE_FALLBACK", "claude-haiku-4-5")
    monkeypatch.delenv("TAKKUB_TEAMMATE_DISALLOWED_TOOLS", raising=False)
    monkeypatch.delenv("TAKKUB_ALLOW_TASK", raising=False)
    monkeypatch.delenv("TAKKUB_EXTRA_PLUGINS", raising=False)
    monkeypatch.delenv("TAKKUB_SETTING_SOURCES", raising=False)

    project = "default"
    orch = _make_orchestrator(qapp, monkeypatch)

    mock_session = MagicMock()
    mock_session.processExited = MagicMock()
    mock_session.is_alive = True
    captured: dict[str, list[str]] = {}

    def _capture_spawn(argv, cwd, env, transcript_path=None):
        captured["argv"] = list(argv)

    mock_session.spawn = _capture_spawn

    pane = MagicMock()
    pane.session = None
    orch._panes_by_project.setdefault(project, {})[role_name] = pane

    patches = list(_PATCHES)
    # Update provider mock
    patches = [
        (tgt, val if tgt != "agent_takkub.core.routing.effective_provider_for_v2" else provider)
        for tgt, val in patches
    ]

    with ExitStack() as stack:
        for target, val in patches:
            if target.endswith("_build_transcript_path"):
                stack.enter_context(patch(target, return_value=pathlib.Path("/tmp/t.log")))
            elif target.endswith("agent_role_dir"):
                stack.enter_context(patch(target, return_value=pathlib.Path(val)))
            else:
                stack.enter_context(patch(target, return_value=val))
        stack.enter_context(
            patch("agent_takkub.orchestrator.PtySession", return_value=mock_session)
        )
        stack.enter_context(patch.object(orch, "_auto_trust"))
        stack.enter_context(patch("agent_takkub.spawn_engine._uuid.uuid4", return_value=FIXED_UUID))
        ok, msg = orch.spawn(role_name, cwd=FAKE_CWD, project=project)

    assert ok, f"spawn({role_name!r}) failed: {msg}"
    assert "argv" in captured, "PtySession.spawn was not called"
    return captured["argv"]


def test_teammate_argv_has_tools_flag_and_correct_tools(qapp, monkeypatch):
    """Teammate argv contains --tools with all 7 core tools and none of 8 cut tools."""
    monkeypatch.delenv("TAKKUB_TEAMMATE_TOOLS", raising=False)
    argv = _spawn_role_and_capture_argv(qapp, monkeypatch, role_name="backend")

    assert "--tools" in argv
    idx = argv.index("--tools")
    assert idx == len(argv) - 2  # --tools should be at the very end
    tools_csv = argv[idx + 1]
    tool_list = tools_csv.split(",")

    # All 7 core tools are present
    for ct in CORE_TOOLS:
        assert ct in tool_list, f"Core tool {ct} missing from --tools"

    # None of the 8 cut tools are present
    for cut in CUT_TOOLS_8:
        assert cut not in tool_list, f"Cut tool {cut} found in --tools"


def test_teammate_argv_escape_hatch_omits_tools_flag(qapp, monkeypatch):
    """TAKKUB_TEAMMATE_TOOLS='' disables --tools flag entirely."""
    monkeypatch.setenv("TAKKUB_TEAMMATE_TOOLS", "")
    argv = _spawn_role_and_capture_argv(qapp, monkeypatch, role_name="backend")
    assert "--tools" not in argv


def test_lead_pane_omits_tools_flag(qapp, monkeypatch):
    """Lead pane is unrestricted and never receives --tools flag."""
    monkeypatch.delenv("TAKKUB_TEAMMATE_TOOLS", raising=False)
    argv = _spawn_role_and_capture_argv(qapp, monkeypatch, role_name=LEAD.name)
    assert "--tools" not in argv


def test_unsupported_provider_omits_tools_flag():
    """assemble_generic_argv does not include --tools if tools_argv is empty."""
    argv = assemble_generic_argv(
        "/usr/bin/codex",
        autonomy_argv=["--full-auto"],
        model_argv=["--model", "gpt-5"],
    )
    assert "--tools" not in argv


# ── Doctor Drift Check ────────────────────────────────────────────────────────


def _write_session_transcript(path, tool_names: list[str]) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    content = [
        {"type": "text", "text": "let's do work"},
        *[{"type": "tool_use", "name": t, "input": {}} for t in tool_names],
    ]
    record = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": content,
        },
    }
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def test_doctor_drift_check_matches_known_baseline(tmp_path):
    """Transcript with known built-in tools and MCP tools reports Status.OK."""
    sess = tmp_path / "projects" / "p1" / "sess.jsonl"
    _write_session_transcript(sess, ["Bash", "Read", "Edit", "mcp__github__create_issue"])
    finding = check_claude_tools_drift(projects_dir=tmp_path)
    assert finding.status == Status.OK
    assert "in sync" in finding.detail
    assert "3 tools checked" in finding.detail


def test_doctor_drift_check_warns_on_new_tools(tmp_path):
    """Transcript with unexpected new built-in tools produces Status.WARN."""
    sess = tmp_path / "projects" / "p1" / "sess.jsonl"
    _write_session_transcript(sess, ["Bash", "AutonomousSubtask", "NewSecretTool"])
    finding = check_claude_tools_drift(projects_dir=tmp_path)
    assert finding.status == Status.WARN
    assert "AutonomousSubtask" in finding.detail
    assert "NewSecretTool" in finding.detail
    assert finding.fix_hint != ""


def test_doctor_drift_check_info_when_no_transcripts(tmp_path):
    """When no transcripts exist, reports Status.INFO without failing."""
    empty_dir = tmp_path / "empty_projects"
    empty_dir.mkdir()
    finding = check_claude_tools_drift(projects_dir=empty_dir)
    assert finding.status == Status.INFO
    assert "could not probe" in finding.detail


def test_doctor_drift_check_current_defaults_override():
    """Explicit current_defaults evaluates directly without reading transcripts."""
    finding_ok = check_claude_tools_drift(current_defaults=list(CLAUDE_DEFAULT_BUILTIN_TOOLS))
    assert finding_ok.status == Status.OK
    assert "in sync" in finding_ok.detail

    drifted = [*CLAUDE_DEFAULT_BUILTIN_TOOLS, "NewSecretTool"]
    finding_warn = check_claude_tools_drift(current_defaults=drifted)
    assert finding_warn.status == Status.WARN
    assert "NewSecretTool" in finding_warn.detail


def test_toolsearch_stays_in_the_teammate_allowlist():
    """ToolSearch is the deferral switch, not a tool we happen to ship (#581).

    `--tools` replaces the default set wholesale, and whether ToolSearch is in
    that set decides whether every OTHER tool's schema is deferred out of the
    prompt or inlined into it. Measured on 2026-09-12 with the shipped list:
    33,779 input tokens with ToolSearch, 46,503 without — and the without
    number is worse than passing no `--tools` flag at all (38,661), i.e.
    dropping it would make the cockpit spend MORE than before this feature
    existed. Its own call count (188 in 14 days) invites exactly that mistake,
    so pin it here rather than trusting the comment to be read.
    """
    from agent_takkub.provider_spec import (
        TEAMMATE_CUT_TOOLS,
        TEAMMATE_DEFAULT_BUILTIN_TOOLS,
    )

    assert "ToolSearch" in TEAMMATE_DEFAULT_BUILTIN_TOOLS
    assert "ToolSearch" not in TEAMMATE_CUT_TOOLS
