"""Regression: the usage meter must not crash the app when its widget dies.

boot.log showed `RuntimeError: wrapped C/C++ object ... has been deleted`
firing 387× — every 120 s usage poll after a project tab that was hosting the
meter got closed. `_on_tab_close_requested` deleted the tab (and its
corner-widget) without detaching it, leaving `_limit_label` a dead wrapper; the
next `_refresh_limit_label` call threw, so the meter vanished until restart.

These tests pin the defensive guard: `_refresh_limit_label` on a torn-down
meter widget is a silent no-op, for both the data-present and data-None paths.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from PyQt6 import sip

from agent_takkub import provider_usage
from agent_takkub.limit_panel import LimitPanelMixin
from agent_takkub.limit_status import LimitWindow, UsageData
from agent_takkub.usage_meter import UsageMeter


class _StubOtherProviderStore:
    """No-op stand-in for the real `ProviderUsageStore` singleton — never
    spins up its background poll thread (real subprocess spawns / file
    reads) inside a unit test."""

    def get_all(self) -> dict:
        return {}


@pytest.fixture(autouse=True)
def _stub_provider_usage_store(monkeypatch):
    monkeypatch.setattr(provider_usage, "get_store", lambda: _StubOtherProviderStore())


class _Holder(LimitPanelMixin):
    """Minimal carrier exposing just the attribute the mixin touches."""

    def __init__(self) -> None:
        self._limit_label = UsageMeter()


def _usage() -> UsageData:
    return UsageData(
        plan="Max 20x",
        windows=[
            LimitWindow(
                name="five_hour",
                utilization=42.0,
                resets_at=datetime.now(tz=UTC) + timedelta(hours=2),
            )
        ],
        extra_usage_enabled=False,
    )


def test_refresh_on_deleted_label_is_noop_data_present() -> None:
    holder = _Holder()
    sip.delete(holder._limit_label)
    assert sip.isdeleted(holder._limit_label)
    # Must not raise "QLabel has been deleted".
    holder._refresh_limit_label(_usage())


def test_refresh_on_deleted_label_is_noop_data_none() -> None:
    holder = _Holder()
    sip.delete(holder._limit_label)
    assert sip.isdeleted(holder._limit_label)
    holder._refresh_limit_label(None)


def test_refresh_on_live_label_still_updates() -> None:
    holder = _Holder()
    holder._refresh_limit_label(_usage())
    # 42% util window → meter carries the percentage; proves the guard doesn't
    # short-circuit a healthy widget.
    assert "42%" in holder._limit_label._label.text()


# ── other-provider usages must come from the real store, never be fabricated ──


def test_no_fake_other_provider_usages_symbol_left_in_module() -> None:
    """Regression guard: a placeholder generator (`_fake_other_provider_usages`)
    used to fabricate codex/gemini/opencode/kimi/cursor numbers out of thin
    air. If anyone reintroduces a similarly-named fake-data generator this
    catches it before it ships again."""
    import agent_takkub.limit_panel as limit_panel_module

    assert not hasattr(limit_panel_module, "_fake_other_provider_usages")


def test_missing_provider_cache_entries_report_loading_not_fabricated_data() -> None:
    """When the real `ProviderUsageStore` has not fetched a provider yet
    (empty cache — the everyday state right after cockpit boot), the panel
    must show `status="loading"` for it, never a made-up utilization number."""
    holder = _Holder()
    holder._refresh_limit_label(_usage())
    usages = holder._limit_label._usages
    by_provider = {u.provider: u for u in usages}

    for name in provider_usage.PROVIDER_NAMES:
        if name == "claude":
            continue
        assert name in by_provider, f"{name} missing from rendered usages"
        u = by_provider[name]
        assert u.status == "loading"
        assert u.utilization is None
        assert u.plan is None


def test_provider_cache_entries_pass_through_real_store_data(monkeypatch) -> None:
    """When the store has real fetched data for a provider, the panel must
    render exactly that — no substitution, no fabricated override."""
    real_codex = provider_usage.ProviderUsage(
        provider="codex", status="active", utilization=17.5, plan="Plus"
    )

    class _StoreWithCodexData:
        def get_all(self) -> dict:
            return {"codex": real_codex}

    monkeypatch.setattr(provider_usage, "get_store", lambda: _StoreWithCodexData())

    holder = _Holder()
    holder._refresh_limit_label(_usage())
    usages = holder._limit_label._usages
    by_provider = {u.provider: u for u in usages}

    assert by_provider["codex"] is real_codex
    assert by_provider["codex"].utilization == 17.5
    # everything else still reports honest "loading", not a fake number.
    assert by_provider["gemini"].status == "loading"
    assert by_provider["gemini"].utilization is None
