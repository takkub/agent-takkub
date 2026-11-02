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
