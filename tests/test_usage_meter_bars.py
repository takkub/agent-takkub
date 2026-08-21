"""Regression: usage popover bars (2026-08-16 #see docs/audit).

Bar fill is always "% of quota already used" — same number `severity_color()`
scores and the same one the sibling text line already quotes. Providers with
no real quota denominator (opencode's token tally, unsupported/loading/error)
must never render a bar — that would fabricate a "% full" reading.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agent_takkub.provider_usage import ProviderUsage
from agent_takkub.usage_meter import _provider_body_entries


def _bars(entries):
    return [e for e in entries if e[0] == "bar"]


def test_claude_windows_render_two_labelled_bars():
    now = datetime.now(tz=UTC)
    usage = ProviderUsage(
        provider="claude",
        status="active",
        fetched_at=now,
        raw_data={
            "windows": {
                "five_hour": {"utilization": 99.0, "eta": "20น."},
                "seven_day": {"utilization": 75.0, "eta": "15น 10ชม."},
            }
        },
    )
    entries = _provider_body_entries(usage, now)
    bars = _bars(entries)
    assert [b[1] for b in bars] == ["5h", "7d"]
    assert [b[2] for b in bars] == [99.0, 75.0]
    assert all(b[4] is False for b in bars)  # not stale


def test_five_hour_near_limit_shows_auto_continue_hint():
    # #322: near/at the 5h window's limit, the text line should tell the
    # user cockpit's park→wake nudge is a safety net, not the primary
    # mechanism — Claude Code 2.1.234+ resumes the pane's own session on
    # its own once this window resets.
    now = datetime.now(tz=UTC)
    usage = ProviderUsage(
        provider="claude",
        status="active",
        fetched_at=now,
        raw_data={
            "windows": {
                "five_hour": {"utilization": 96.0, "eta": "20น."},
                "seven_day": {"utilization": 96.0, "eta": "3วัน"},
            }
        },
    )
    entries = _provider_body_entries(usage, now)
    texts = [e[1] for e in entries if e[0] == "text"]
    assert any("5h" in t and "auto-continue" in t for t in texts)
    # seven_day never blocks a pane mid-task the way five_hour does — must
    # not get the hint even at the same utilization.
    assert not any(t.startswith("7d") and "auto-continue" in t for t in texts)


def test_five_hour_below_threshold_no_auto_continue_hint():
    now = datetime.now(tz=UTC)
    usage = ProviderUsage(
        provider="claude",
        status="active",
        fetched_at=now,
        raw_data={"windows": {"five_hour": {"utilization": 50.0, "eta": "20น."}}},
    )
    entries = _provider_body_entries(usage, now)
    texts = [e[1] for e in entries if e[0] == "text"]
    assert not any("auto-continue" in t for t in texts)


def test_claude_window_missing_utilization_gets_no_bar():
    now = datetime.now(tz=UTC)
    usage = ProviderUsage(
        provider="claude",
        status="active",
        fetched_at=now,
        raw_data={"windows": {"five_hour": {"utilization": None, "eta": ""}}},
    )
    entries = _provider_body_entries(usage, now)
    assert _bars(entries) == []


def test_generic_utilization_bar_matches_pct_used_not_pct_left():
    now = datetime.now(tz=UTC)
    usage = ProviderUsage(provider="codex", status="active", fetched_at=now, utilization=33.0)
    entries = _provider_body_entries(usage, now)
    bars = _bars(entries)
    assert len(bars) == 1
    assert bars[0][2] == 33.0  # 33% used, not 67% left


def test_stale_flag_reaches_the_bar():
    now = datetime.now(tz=UTC)
    usage = ProviderUsage(
        provider="gemini",
        status="stale",
        fetched_at=now - timedelta(days=150),
        utilization=0.0,
    )
    entries = _provider_body_entries(usage, now)
    bars = _bars(entries)
    assert len(bars) == 1
    assert bars[0][4] is True


def test_unsupported_loading_error_never_render_a_bar():
    now = datetime.now(tz=UTC)
    for status in ("unsupported", "loading", "error"):
        usage = ProviderUsage(provider="kimi", status=status)
        entries = _provider_body_entries(usage, now)
        assert _bars(entries) == []


def test_stale_snapshot_renders_its_adapter_hint_instead_of_implying_a_refresh():
    # gemini's channel is a cache only the Antigravity desktop app writes, so
    # "stale" there never clears on its own. The old wording
    # ("ยังไม่อัปเดต") promised an update that will never arrive.
    now = datetime.now(tz=UTC)
    usage = ProviderUsage(
        provider="gemini",
        status="stale",
        fetched_at=now - timedelta(days=150),
        error="ต้องเปิดแอป Antigravity ถึงจะอัปเดต (agy CLI ไม่เขียน cache นี้)",
    )
    texts = [e[1] for e in _provider_body_entries(usage, now) if e[0] == "text"]
    assert any("Antigravity" in t for t in texts)
    assert not any("ยังไม่อัปเดต" in t for t in texts)


def test_stale_snapshot_without_a_hint_keeps_the_plain_expiry_line():
    now = datetime.now(tz=UTC)
    usage = ProviderUsage(provider="claude", status="stale", fetched_at=now - timedelta(days=2))
    texts = [e[1] for e in _provider_body_entries(usage, now) if e[0] == "text"]
    assert any("ข้อมูลโควต้าหมดอายุ" in t for t in texts)


def test_error_status_never_reaches_the_hint_line():
    # `status == "error"` short-circuits well before the hint block — an
    # error must render as a failure, never as a stale-but-usable snapshot.
    now = datetime.now(tz=UTC)
    usage = ProviderUsage(provider="gemini", status="error", error="boom")
    texts = [e[1] for e in _provider_body_entries(usage, now) if e[0] == "text"]
    assert texts == ["ดึงข้อมูลไม่สำเร็จ"]
