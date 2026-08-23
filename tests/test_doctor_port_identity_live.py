"""Tests for `check_port_identity_live` — the #354 doctor cross-check that
the port file this instance resolved actually answers for its OWN
DATA_HOME, not a different cockpit instance's."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub import config
from agent_takkub.doctor import Status, check_port_identity_live


def test_no_port_file_skips() -> None:
    finding = check_port_identity_live(None)[0]
    assert finding.status is Status.SKIP


def test_unreachable_cockpit_warns() -> None:
    finding = check_port_identity_live({"ok": False, "msg": "connection refused"})[0]
    assert finding.status is Status.WARN
    assert "connection refused" in finding.detail


def test_matching_data_home_is_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "agent-takkub"
    monkeypatch.setattr(config, "DATA_HOME", home)
    finding = check_port_identity_live({"ok": True, "port": 54321, "data_home": str(home)})[0]
    assert finding.status is Status.OK
    assert "54321" in finding.detail


def test_mismatched_data_home_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "agent-takkub"
    other_home = tmp_path / ".agent-takkub"
    monkeypatch.setattr(config, "DATA_HOME", home)
    finding = check_port_identity_live({"ok": True, "port": 52161, "data_home": str(other_home)})[0]
    assert finding.status is Status.FAIL
    assert "52161" in finding.detail
    assert str(other_home) in finding.detail
    assert str(home) in finding.detail
    assert finding.fix_hint
