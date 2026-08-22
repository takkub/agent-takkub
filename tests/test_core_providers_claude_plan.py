"""core.providers.claude_plan — pure claude argv assembly (epic #309 2.0.0
Wave A).

`test_assemble_claude_argv_matches_branch_order` is the golden-order proof:
it re-derives the argv the claude branch (spawn_engine.py ~2639-2947,
verified 2026-08-22) would build for a given set of resolved pieces BY HAND
(bin, --dangerously-skip-permissions, --setting-sources, hook --settings,
model, effort, fallback-model, teammate --disallowedTools, --plugin-dir,
system-prompt flag, MCP, --disallowed-tools deny list, --resume/
--session-id — in that literal order) and asserts `assemble_claude_argv`
produces exactly the same list. Same method
`tests/test_core_providers_plan.py` uses for the generic branch.
"""

from __future__ import annotations

from agent_takkub.core.providers.claude_plan import assemble_claude_argv


def test_assemble_claude_argv_matches_branch_order():
    claude_bin = "/usr/local/bin/claude"
    sources = "project,local"
    settings = ["--settings", "/tmp/hook-settings.json"]
    model = ["--model", "claude-sonnet-5"]
    effort = ["--effort", "high"]
    fallback = ["--fallback-model", "claude-haiku-4-5"]
    disallowed_tools = ["--disallowedTools", "WebSearch,WebFetch"]
    plugin_dirs = ["--plugin-dir", "/opt/plugins/a", "--plugin-dir", "/opt/plugins/b"]
    system_prompt = ["--append-system-prompt-file", "/tmp/role.md"]
    mcp = ["--mcp-config", "/tmp/mcp.json", "--strict-mcp-config"]
    denied_tools = ["--disallowed-tools", "Task,AskUserQuestion"]
    resume = ["--resume", "uuid-1"]

    expected: list[str] = [
        claude_bin,
        "--dangerously-skip-permissions",
        "--setting-sources",
        sources,
    ]
    expected.extend(settings)
    expected.extend(model)
    expected.extend(effort)
    expected.extend(fallback)
    expected.extend(disallowed_tools)
    expected.extend(plugin_dirs)
    expected.extend(system_prompt)
    expected.extend(mcp)
    expected.extend(denied_tools)
    expected.extend(resume)

    got = assemble_claude_argv(
        claude_bin,
        setting_sources=sources,
        settings_argv=settings,
        model_argv=model,
        effort_argv=effort,
        fallback_argv=fallback,
        disallowed_tools_argv=disallowed_tools,
        plugin_dir_argv=plugin_dirs,
        system_prompt_argv=system_prompt,
        mcp_argv=mcp,
        denied_tools_argv=denied_tools,
        resume_argv=resume,
    )
    assert got == expected


def test_assemble_claude_argv_omits_empty_pieces():
    got = assemble_claude_argv("/usr/local/bin/claude", setting_sources="project,local")
    assert got == [
        "/usr/local/bin/claude",
        "--dangerously-skip-permissions",
        "--setting-sources",
        "project,local",
    ]


def test_assemble_claude_argv_lead_shape_has_no_disallowed_tools_or_effort():
    # Lead branch never builds effort_argv or disallowed_tools_argv (teammate-
    # only flags — see spawn_engine.py's `if role_name != LEAD.name:` split).
    got = assemble_claude_argv(
        "/usr/local/bin/claude",
        setting_sources="project,local",
        model_argv=["--model", "claude-opus-5"],
        fallback_argv=["--fallback-model", "claude-sonnet-5"],
        resume_argv=["--session-id", "uuid-2"],
    )
    assert "--disallowedTools" not in got
    assert "--effort" not in got
    assert got == [
        "/usr/local/bin/claude",
        "--dangerously-skip-permissions",
        "--setting-sources",
        "project,local",
        "--model",
        "claude-opus-5",
        "--fallback-model",
        "claude-sonnet-5",
        "--session-id",
        "uuid-2",
    ]
