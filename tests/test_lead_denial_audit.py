"""Codex native denial audit reads provider outcomes without logging tool text."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_takkub.lead_enforcement import codex_denial_records
from agent_takkub.spawn_engine import SpawnEngineMixin


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


def test_protected_lead_exit_emits_structured_denial(monkeypatch, tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        "\n".join(
            json.dumps(item)
            for item in (
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "call_id": "1",
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
                        "call_id": "1",
                        "output": [{"text": "patch rejected: writing outside of the project"}],
                    },
                },
            )
        ),
        encoding="utf-8",
    )
    events = []
    monkeypatch.setattr(
        "agent_takkub.spawn_engine._log_event",
        lambda name, **details: events.append((name, details)),
    )
    monkeypatch.setattr(
        "agent_takkub.codex_helper.resolve_codex_jsonl_for_cwd",
        lambda *_args, **_kwargs: rollout,
    )
    state = SimpleNamespace(
        codex_spawn_ts=0.0,
        lead_enforcement_mode="codex_sandbox",
        session_uuid="session-1",
        task_id="task-42",
    )
    fake = SimpleNamespace(
        _pane_state={"sample::lead": state},
        _panes_by_project={},
        _on_session_exit=MagicMock(),
    )

    SpawnEngineMixin._on_codex_exit(fake, 0, "lead", str(project), "sample", MagicMock())

    assert events == [
        (
            "lead_native_denied",
            {
                "role": "lead",
                "project": "sample",
                "task": "task-42",
                "tool": "apply_patch",
                "target": "sentinel.txt",
                "reason": "patch_permission_denied",
            },
        )
    ]
    fake._on_session_exit.assert_called_once()
