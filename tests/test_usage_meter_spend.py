"""Regression: opencode's self-tallied `spend` (real shape from
`provider_usage.fetch_opencode_usage`) must render in the usage meter —
this used to only understand a fake `raw_data["tokens_used"]`/`cost_usd`
shape that no real adapter ever produced, so opencode's real numbers
silently rendered as "no data available" once the fake placeholder was
removed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agent_takkub.provider_usage import ProviderUsage
from agent_takkub.usage_meter import _provider_body_lines


def _opencode_usage(**spend_overrides) -> ProviderUsage:
    spend = {
        "cost_usd": 4.32,
        "input_tokens": 100_000,
        "output_tokens": 28_430,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "message_count": 12,
    }
    spend.update(spend_overrides)
    return ProviderUsage(
        provider="opencode", status="active", fetched_at=datetime.now(tz=UTC), spend=spend
    )


def test_opencode_spend_renders_tokens_and_cost():
    lines = _provider_body_lines(_opencode_usage(), datetime.now(tz=UTC))
    text = lines[0][0]
    assert "128,430" in text
    assert "$4.32" in text
    assert "ไม่ใช่โควต้า" in text


def test_opencode_never_shown_as_utilization_percent():
    """`spend` must never be mistaken for a quota percentage."""
    usage = _opencode_usage()
    assert usage.utilization is None
    lines = _provider_body_lines(usage, datetime.now(tz=UTC))
    assert "%" not in lines[0][0]


def test_provider_with_no_spend_and_no_utilization_shows_no_data():
    usage = ProviderUsage(provider="opencode", status="active", fetched_at=datetime.now(tz=UTC))
    lines = _provider_body_lines(usage, datetime.now(tz=UTC))
    assert lines[0][0] == "ไม่มีข้อมูลให้ดู"
