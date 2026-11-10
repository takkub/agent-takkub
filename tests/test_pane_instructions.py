"""#961: non-claude pane instructions travel over a provider channel, never
as a file in the project — and the `.git/info/exclude` line the remaining
AGENTS.md providers need is taken back out on cleanup."""

from __future__ import annotations

import json

from agent_takkub import codex_agents_md, pane_instructions


def test_codex_inline_literal_carries_the_text(tmp_path):
    argv, path, mode = pane_instructions.deliver(
        pane_instructions.CODEX_DEVELOPER_INSTRUCTIONS, 'rule ภาษาไทย "q"\n', "p", "qa#1", {}
    )
    assert mode == "inline"
    assert argv == ["-c", "developer_instructions='''rule ภาษาไทย \"q\"\n'''"]
    assert path.endswith("qa_1.md")


def test_codex_points_at_the_file_when_inline_cannot_carry_it():
    for text in ("x" * 30_000, "has ''' inside"):
        argv, path, mode = pane_instructions.deliver(
            pane_instructions.CODEX_DEVELOPER_INSTRUCTIONS, text, "p", "qa", {}
        )
        assert mode == "file_pointer"
        assert argv[1].startswith("developer_instructions=")
        assert path in json.loads(argv[1].split("=", 1)[1])
        with open(path, encoding="utf-8") as fh:
            assert fh.read() == text


def test_opencode_keeps_existing_config_content():
    env = {"OPENCODE_CONFIG_CONTENT": json.dumps({"model": "m", "instructions": ["a.md"]})}
    argv, path, _ = pane_instructions.deliver(
        pane_instructions.OPENCODE_CONFIG_INSTRUCTIONS, "body", "p", "backend", env
    )
    cfg = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert argv == []
    assert cfg == {"model": "m", "instructions": ["a.md", path]}


def test_cleanup_removes_only_our_tagged_exclude_line(tmp_path):
    info = tmp_path / ".git" / "info"
    info.mkdir(parents=True)
    exclude = info / "exclude"
    exclude.write_text("# user\nAGENTS.md\n", encoding="utf-8")
    assert codex_agents_md.ensure_agents_md(tmp_path) == (True, "written")
    # the user's own bare line already covers it: nothing added, nothing removed
    assert codex_agents_md.remove_managed_context_files(tmp_path) == ["AGENTS.md"]
    assert exclude.read_text(encoding="utf-8") == "# user\nAGENTS.md\n"

    exclude.write_text("# user\n*.log\n", encoding="utf-8")
    assert codex_agents_md.ensure_agents_md(tmp_path) == (True, "written")
    assert "takkub-managed" in exclude.read_text(encoding="utf-8")
    assert codex_agents_md.remove_managed_context_files(tmp_path) == ["AGENTS.md"]
    assert exclude.read_text(encoding="utf-8") == "# user\n*.log\n"


def test_agy_custom_agent_is_per_pane_and_removed_on_close():
    argv, _path, mode = pane_instructions.deliver(
        pane_instructions.AGY_CUSTOM_AGENT, "body\n---\nmore", "p", "gemini#1", {}
    )
    name = pane_instructions.agy_agent_name("p", "gemini#1")
    assert (argv, mode) == (["--agent", name], "custom_agent")
    md = pane_instructions.agy_agents_root() / name / "agent.md"
    text = md.read_text(encoding="utf-8")
    assert text.startswith(f"---\nname: {name}\n")
    assert text.endswith("body\n---\nmore")
    assert pane_instructions.remove_agy_agent("p", "gemini#1") is True
    assert not md.parent.exists()


def test_agy_cleanup_never_touches_a_foreign_agent():
    name = pane_instructions.agy_agent_name("p", "lead")
    foreign = pane_instructions.agy_agents_root() / name
    foreign.mkdir(parents=True)
    (foreign / "agent.md").write_text("---\nname: mine\n---\nuser's own", encoding="utf-8")
    assert pane_instructions.remove_agy_agent("p", "lead") is False
    assert foreign.is_dir()


def test_agy_agent_name_differs_per_cockpit(monkeypatch, tmp_path):
    from agent_takkub import config

    a = pane_instructions.agy_agent_name("p", "lead")
    monkeypatch.setattr(config, "DATA_HOME", tmp_path / "other-cockpit")
    assert pane_instructions.agy_agent_name("p", "lead") != a


def test_first_task_paste_writes_no_argv_or_env():
    env: dict = {}
    argv, _path, mode = pane_instructions.deliver(
        pane_instructions.FIRST_TASK_PASTE, "body", "p", "cursor", env
    )
    assert (argv, env, mode) == ([], {}, "first_task_paste")


def test_codex_marker_leads_both_inline_and_pointer_values():
    marker = pane_instructions.session_marker("ab" * 16)
    argv, _p, mode = pane_instructions.deliver(
        pane_instructions.CODEX_DEVELOPER_INSTRUCTIONS, "body", "p", "lead", {}, marker=marker
    )
    assert mode == "inline"
    assert argv[1] == f"developer_instructions='''{marker}\nbody'''"
    argv, _p, mode = pane_instructions.deliver(
        pane_instructions.CODEX_DEVELOPER_INSTRUCTIONS, "x'''y", "p", "lead", {}, marker=marker
    )
    assert mode == "file_pointer"
    assert json.loads(argv[1].split("=", 1)[1]).startswith(marker + "\n")


def test_codex_lead_binds_the_rollout_that_carries_its_marker(monkeypatch, tmp_path):
    """#964: the real thread id comes from the marker, not the newest rollout."""
    import types

    from agent_takkub import config
    from agent_takkub.remote import notify

    cwd = str(tmp_path / "proj")
    day = tmp_path / "sessions" / "2026" / "11" / "02"
    day.mkdir(parents=True)

    def rollout(name, sid, dev_text):
        lines = [
            {"type": "session_meta", "payload": {"id": sid, "cwd": cwd, "originator": "codex-tui"}},
            {"type": "response_item", "payload": {"role": "developer", "content": dev_text}},
        ]
        (day / name).write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

    marker = pane_instructions.session_marker("cd" * 16)
    rollout("rollout-2026-11-02T09-00-00-aaaa.jsonl", "lead-thread", marker + "\npolicy")
    # a teammate spawned later in the same cwd is the newest file
    rollout("rollout-2026-11-02T10-00-00-bbbb.jsonl", "teammate-thread", "other text")
    monkeypatch.setattr(config, "lead_cwd", lambda _p: cwd)
    monkeypatch.setattr(notify, "_safe_codex_sessions_root", lambda _p="": tmp_path / "sessions")
    ps = types.SimpleNamespace(
        session_uuid=None,
        session_uuid_cwd="",
        session_provider="",
        codex_session_marker=marker,
        codex_marker_scan_ts=0.0,
    )
    notify._bind_codex_session_by_marker(ps, "p", 0.0)
    # #1005: the scan runs off the Qt main thread — the tick never blocks on it
    with notify._marker_scan_lock:
        thread = notify._marker_scan_threads.get(marker)
    if thread is not None:
        thread.join(5)
    notify._bind_codex_session_by_marker(ps, "p", 0.0)
    assert (ps.session_uuid, ps.session_provider) == ("lead-thread", "codex")


def test_agy_agent_lists_its_tools_so_the_tui_pane_has_a_shell():
    """#969: agy's TUI gives a custom agent without `tools:` no shell — the
    pane could not run `takkub done`. The list must carry run_command and
    must never carry a name agy 1.4.12 can't resolve for a custom agent (one
    unknown name fails the whole executor)."""
    pane_instructions.deliver(pane_instructions.AGY_CUSTOM_AGENT, "body", "p", "critic", {})
    name = pane_instructions.agy_agent_name("p", "critic")
    md = pane_instructions.agy_agents_root() / name / "agent.md"
    try:
        front = md.read_text(encoding="utf-8").split("\n---\n", 1)[0]
        tools_line = next(line for line in front.splitlines() if line.startswith("tools: ["))
        tools = [t.strip() for t in tools_line[len("tools: [") : -1].split(",")]
        assert "run_command" in tools and "view_file" in tools and "write_to_file" in tools
        unresolvable = {"sed_file", "call_mcp_tool", "notebook_execution", "browser_subagent"}
        assert not unresolvable & set(tools)
        assert not [t for t in tools if t.startswith("browser_")]
    finally:
        pane_instructions.remove_agy_agent("p", "critic")
