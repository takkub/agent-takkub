"""Regression: the usage meter must not crash the app when its QLabel dies.

boot.log showed `RuntimeError: wrapped C/C++ object of type QLabel has been
deleted` firing 387× — every 120 s usage poll after a project tab that was
hosting the meter got closed. `_on_tab_close_requested` deleted the tab (and
its corner-widget QLabel) without detaching the label, leaving `_limit_label`
a dead wrapper; the next `_refresh_limit_label` call threw, so the meter
vanished until restart.

These tests pin the defensive guard: `_refresh_limit_label` on a torn-down
QLabel is a silent no-op, for both the data-present and data-None paths.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from PyQt6 import sip
from PyQt6.QtWidgets import QLabel

from agent_takkub.limit_panel import LimitPanelMixin
from agent_takkub.limit_status import LimitWindow, UsageData


class _Holder(LimitPanelMixin):
    """Minimal carrier exposing just the attribute the mixin touches."""

    def __init__(self) -> None:
        self._limit_label = QLabel("—")


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
    # 42% util window → label carries the percentage; proves the guard doesn't
    # short-circuit a healthy label.
    assert "42%" in holder._limit_label.text()
