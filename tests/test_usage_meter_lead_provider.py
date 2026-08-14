from __future__ import annotations

from agent_takkub.provider_usage import ProviderUsage
from agent_takkub.usage_meter import UsageMeter


def test_compact_meter_follows_codex_lead_even_when_claude_is_more_used() -> None:
    meter = UsageMeter()
    meter.set_usages(
        [
            ProviderUsage(provider="claude", status="active", utilization=91),
            ProviderUsage(provider="codex", status="active", utilization=27),
        ],
        primary_provider="codex",
    )
    assert meter._label.text().startswith("Codex 27%")


def test_compact_meter_names_loading_lead_provider() -> None:
    meter = UsageMeter()
    meter.set_usages(
        [
            ProviderUsage(provider="claude", status="active", utilization=27),
            ProviderUsage(provider="codex", status="loading"),
        ],
        primary_provider="codex",
    )
    assert meter._label.text().startswith("Codex …")


def test_compact_meter_claude_primary_uses_5h_not_cross_window_max() -> None:
    # utilization is max(5h, 7d) per _claude_provider_usage — here the 7-day
    # window is the bigger number, so the old bug would show 91% instead of
    # the 5-hour 40% users actually watch mid-session.
    meter = UsageMeter()
    meter.set_usages(
        [
            ProviderUsage(
                provider="claude",
                status="active",
                utilization=91,
                raw_data={
                    "windows": {
                        "five_hour": {"utilization": 40, "eta": "2ชม. 14น."},
                        "seven_day": {"utilization": 91, "eta": "3วัน 1ชม."},
                    }
                },
            ),
        ],
        primary_provider="claude",
    )
    text = meter._label.text()
    assert text.startswith("Claude 40%")
    assert "91%" not in text


def test_compact_meter_claude_primary_shows_5h_countdown() -> None:
    meter = UsageMeter()
    meter.set_usages(
        [
            ProviderUsage(
                provider="claude",
                status="active",
                utilization=62,
                raw_data={
                    "windows": {
                        "five_hour": {"utilization": 62, "eta": "2ชม. 14น."},
                        "seven_day": {"utilization": 30, "eta": "3วัน 1ชม."},
                    }
                },
            ),
        ],
        primary_provider="claude",
    )
    assert meter._label.text() == "Claude 62% · 2ชม. 14น."


def test_compact_meter_claude_primary_without_windows_falls_back_to_utilization() -> None:
    meter = UsageMeter()
    meter.set_usages(
        [ProviderUsage(provider="claude", status="active", utilization=55)],
        primary_provider="claude",
    )
    assert meter._label.text().startswith("Claude 55%")
