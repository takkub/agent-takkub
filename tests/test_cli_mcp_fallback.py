"""Tests for `takkub mcp-fallback` (cli.cmd_mcp_fallback) — the single,
time-boxed `mb` escape hatch for a browser-role shard whose Playwright MCP
never connected (#146/#304). Local-only, no orchestrator IPC.
"""

from __future__ import annotations

import pytest

from agent_takkub import cli


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path):
    from agent_takkub import config

    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)


@pytest.fixture
def as_shard(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TAKKUB_ROLE", "qa#1")
    monkeypatch.setenv("TAKKUB_PROJECT", "proj")


class TestRequest:
    def test_requires_pane_role(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        rc = cli.main(["mcp-fallback", "request"])
        assert rc == 1

    def test_first_request_is_granted(self, as_shard: None) -> None:
        rc = cli.main(["mcp-fallback", "request", "--reason", "mcp not connected"])
        assert rc == 0

    def test_second_role_denied_while_first_holds(
        self, as_shard: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli.main(["mcp-fallback", "request"])
        monkeypatch.setenv("TAKKUB_ROLE", "qa#2")
        rc = cli.main(["mcp-fallback", "request"])
        assert rc == 1

    def test_grant_is_visible_to_mcp_fallback_module(self, as_shard: None) -> None:
        from agent_takkub import mcp_fallback

        cli.main(["mcp-fallback", "request"])
        assert mcp_fallback.is_granted("qa#1") is True


class TestStatus:
    def test_status_reports_nothing_held(
        self, as_shard: None, capsys: pytest.CaptureFixture
    ) -> None:
        rc = cli.main(["mcp-fallback", "status"])
        assert rc == 0
        assert "no active" in capsys.readouterr().out

    def test_status_reports_current_holder(
        self, as_shard: None, capsys: pytest.CaptureFixture
    ) -> None:
        cli.main(["mcp-fallback", "request"])
        cli.main(["mcp-fallback", "status"])
        out = capsys.readouterr().out
        assert "qa#1" in out
