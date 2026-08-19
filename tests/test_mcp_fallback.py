"""Tests for `mcp_fallback.py` — the single, time-boxed `mb` escape hatch
for a browser-role shard whose Playwright MCP never connects (#146/#304).

Root safety property under test: at most one holder at a time (mb shares
one CDP endpoint machine-wide, #92), so a second requester is denied with
the current holder rather than silently displacing them.
"""

from __future__ import annotations

import pytest

from agent_takkub import mcp_fallback


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Redirect config.RUNTIME_DIR so these tests never touch a real
    cockpit's runtime/ dir, matching the isolation pattern other
    runtime-file tests already use."""
    from agent_takkub import config

    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)


class TestRequest:
    def test_first_request_is_granted(self):
        grant = mcp_fallback.request("qa#1", "proj")
        assert grant.granted
        assert grant.holder == "qa#1"
        assert grant.expires_at is not None

    def test_second_requester_denied_while_first_holds(self):
        mcp_fallback.request("qa#1", "proj")
        grant = mcp_fallback.request("qa#2", "proj")
        assert not grant.granted
        assert grant.holder == "qa#1"
        assert "qa#1" in grant.reason

    def test_same_role_refreshes_its_own_grant(self):
        mcp_fallback.request("qa#1", "proj", ttl_s=1)
        grant = mcp_fallback.request("qa#1", "proj", ttl_s=180)
        assert grant.granted
        assert grant.holder == "qa#1"

    def test_expired_grant_can_be_taken_by_another_role(self, monkeypatch: pytest.MonkeyPatch):
        import time

        mcp_fallback.request("qa#1", "proj", ttl_s=1)
        later = time.time() + 10
        monkeypatch.setattr(time, "time", lambda: later)
        grant = mcp_fallback.request("qa#2", "proj")
        assert grant.granted
        assert grant.holder == "qa#2"


class TestIsGranted:
    def test_false_when_nothing_held(self):
        assert mcp_fallback.is_granted("qa#1") is False

    def test_true_for_current_holder(self):
        mcp_fallback.request("qa#1", "proj")
        assert mcp_fallback.is_granted("qa#1") is True

    def test_false_for_non_holder(self):
        mcp_fallback.request("qa#1", "proj")
        assert mcp_fallback.is_granted("qa#2") is False

    def test_false_after_expiry(self, monkeypatch: pytest.MonkeyPatch):
        import time

        mcp_fallback.request("qa#1", "proj", ttl_s=1)
        later = time.time() + 10
        monkeypatch.setattr(time, "time", lambda: later)
        assert mcp_fallback.is_granted("qa#1") is False


class TestStatus:
    def test_none_when_nothing_held(self):
        assert mcp_fallback.status() is None

    def test_reports_current_holder(self):
        mcp_fallback.request("qa#1", "proj", reason="mcp not connected")
        info = mcp_fallback.status()
        assert info is not None
        assert info["holder"] == "qa#1"
        assert info["reason"] == "mcp not connected"

    def test_none_after_expiry(self, monkeypatch: pytest.MonkeyPatch):
        import time

        mcp_fallback.request("qa#1", "proj", ttl_s=1)
        later = time.time() + 10
        monkeypatch.setattr(time, "time", lambda: later)
        assert mcp_fallback.status() is None
