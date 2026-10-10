"""Tests for lead readiness policy enforcement and context bootstrapping."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_takkub import cli, lead_readiness


def test_prepare_and_read_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_takkub.config.RUNTIME_DIR", tmp_path / "runtime")
    path = lead_readiness.prepare("default", "claude", str(tmp_path))
    assert path.exists()
    state = lead_readiness.read_state(path)
    assert state["session"] == path.stem
    assert state["project"] == "default"
    assert state["provider"] == "claude"
    assert state["native_tool_gate"] == "supported"
    assert state["epoch"] == 0
    assert state["read_epoch"] is None
    assert "Mandatory Lead bootstrap" in state["text"]


def test_reason_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_takkub.config.RUNTIME_DIR", tmp_path / "runtime")
    path = lead_readiness.prepare("default", "claude", str(tmp_path))

    # Initial state: unread
    assert "has not been read" in lead_readiness.reason(path, project="default")

    # Wrong project
    assert "different project" in lead_readiness.reason(path, project="other")

    # Acknowledge read
    state = lead_readiness.read_state(path)
    lead_readiness.acknowledge(path, state)
    assert lead_readiness.reason(path, project="default") == ""

    # Invalidate by session-start / compaction
    lead_readiness.invalidate(path, "session-start")
    assert "has not been read" in lead_readiness.reason(path, project="default")

    # Refresh and acknowledge again
    state, _text = lead_readiness.refresh(path)
    assert state["epoch"] == 1
    lead_readiness.acknowledge(path, state)
    assert lead_readiness.reason(path, project="default") == ""


def test_bootstrap_allowed_tools() -> None:
    assert lead_readiness.bootstrap_allowed("Read", {"file_path": "CLAUDE.md"})
    assert lead_readiness.bootstrap_allowed("Glob", {"pattern": "*.md"})
    assert lead_readiness.bootstrap_allowed("Grep", {"pattern": "foo"})
    assert lead_readiness.bootstrap_allowed("Bash", {"command": "takkub context read"})
    assert lead_readiness.bootstrap_allowed("Bash", {"command": "takkub context status"})
    assert lead_readiness.bootstrap_allowed("Bash", {"command": "takkub team status"})
    assert lead_readiness.bootstrap_allowed("Bash", {"command": "takkub doctor"})
    assert lead_readiness.bootstrap_allowed("Bash", {"command": "takkub doctor --json"})

    # Disallowed tools and compound commands
    assert not lead_readiness.bootstrap_allowed("Write", {"file_path": "foo.py"})
    assert not lead_readiness.bootstrap_allowed("Edit", {"file_path": "foo.py"})
    assert not lead_readiness.bootstrap_allowed("Bash", {"command": "git status"})
    assert not lead_readiness.bootstrap_allowed(
        "Bash", {"command": "takkub context read && rm -rf /"}
    )
    assert not lead_readiness.bootstrap_allowed(
        "Bash", {"command": "takkub assign --role backend 'foo'"}
    )


def test_cli_context_read_and_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr("agent_takkub.config.RUNTIME_DIR", tmp_path / "runtime")
    path = lead_readiness.prepare("default", "codex", str(tmp_path))

    monkeypatch.setenv(lead_readiness.ENV_FILE, str(path))
    monkeypatch.setenv("TAKKUB_ROLE", "lead")
    monkeypatch.setenv("TAKKUB_PROJECT", "default")

    # Check status before reading
    assert cli.main(["context", "status"]) == 1
    out = capsys.readouterr().out
    status = json.loads(out)
    assert status["ready"] is False
    assert status["provider"] == "codex"
    assert status["native_tool_gate"] == "unsupported"

    # Read context
    assert cli.main(["context", "read"]) == 0
    read_out = capsys.readouterr().out
    assert "Mandatory Lead bootstrap" in read_out

    # Check status after reading
    assert cli.main(["context", "status"]) == 0
    out2 = capsys.readouterr().out
    status2 = json.loads(out2)
    assert status2["ready"] is True
    assert status2["reason"] == ""
