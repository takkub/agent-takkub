"""core.capabilities.audit.log_capability_event — appends to the SAME
EVENTS_LOG file the existing `_log_event` copies write to (Phase 5c, epic
#309); never raises."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_takkub import config as config_mod
from agent_takkub.core.capabilities.audit import log_capability_event


def test_log_capability_event_writes_jsonl_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events_log = tmp_path / "events.log"
    monkeypatch.setattr(config_mod, "EVENTS_LOG", events_log)
    monkeypatch.setattr(config_mod, "RUNTIME_DIR", tmp_path)

    log_capability_event(
        "capability.mcp_denied",
        who="backend",
        agent="backend#2",
        provider="codex",
        account="default",
        tool="graft",
        rule="mcp_deny_by_default",
    )

    lines = events_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "capability.mcp_denied"
    assert payload["who"] == "backend"
    assert payload["agent"] == "backend#2"
    assert payload["provider"] == "codex"
    assert payload["account"] == "default"
    assert payload["tool"] == "graft"
    assert payload["rule"] == "mcp_deny_by_default"
    assert "ts" in payload


def test_log_capability_event_appends_without_clobbering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events_log = tmp_path / "events.log"
    monkeypatch.setattr(config_mod, "EVENTS_LOG", events_log)
    monkeypatch.setattr(config_mod, "RUNTIME_DIR", tmp_path)

    log_capability_event("capability.a")
    log_capability_event("capability.b")

    lines = events_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "capability.a"
    assert json.loads(lines[1])["event"] == "capability.b"


def test_log_capability_event_never_raises_on_write_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocker = tmp_path / "blocker-file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(config_mod, "RUNTIME_DIR", blocker / "runtime")
    monkeypatch.setattr(config_mod, "EVENTS_LOG", blocker / "runtime" / "events.log")

    log_capability_event("capability.will_fail")  # must not raise
