"""Codex native denial audit reads provider outcomes without logging tool text."""

from __future__ import annotations

import json
from pathlib import Path

from agent_takkub.lead_enforcement import codex_denial_records


def test_codex_denial_records_exclude_commands_and_secrets(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    rollout = tmp_path / "rollout.jsonl"
    calls = [
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "patch",
                "name": "exec",
                "input": (
                    'tools.apply_patch("*** Begin Patch\\n*** Update File: '
                    + str(project / "sentinel.txt")
                    + '\\n-secret-value\\n+CHANGED\\n*** End Patch")'
                ),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "patch",
                "output": [{"text": "patch rejected: writing outside of the project"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "shell",
                "name": "exec",
                "input": "tools.exec_command({cmd:'Set-Content secret-value'})",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "shell",
                "output": [
                    {
                        "text": json.dumps(
                            {
                                "exit_code": 1,
                                "output": (
                                    f"Access to the path '{project / 'sentinel.txt'}' is denied."
                                ),
                            }
                        )
                    }
                ],
            },
        },
    ]
    rollout.write_text("".join(json.dumps(row) + "\n" for row in calls), encoding="utf-8")

    rows = codex_denial_records(rollout, str(project))

    assert rows == [
        {
            "tool": "apply_patch",
            "target": "sentinel.txt",
            "reason": "patch_permission_denied",
        },
        {
            "tool": "exec_command",
            "target": "sentinel.txt",
            "reason": "filesystem_permission_denied",
        },
    ]
    assert "secret-value" not in json.dumps(rows)
