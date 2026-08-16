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
