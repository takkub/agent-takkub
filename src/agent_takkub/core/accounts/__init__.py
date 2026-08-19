"""Account/pool registries + selector strategies (Phase 3, epic #309)."""

from __future__ import annotations

from .legacy_reader import read_legacy_accounts, read_selected_account_id
from .registry import AccountPoolRegistry, AccountRegistry
from .selector import (
    CooldownFailoverSelector,
    ManualAccountSelector,
    PriorityAccountSelector,
    QuotaAwareAccountSelector,
    StickyAccountSelector,
    selector_for,
)
from .switch_log import log_switch, read_switches

__all__ = [
    "AccountPoolRegistry",
    "AccountRegistry",
    "CooldownFailoverSelector",
    "ManualAccountSelector",
    "PriorityAccountSelector",
    "QuotaAwareAccountSelector",
    "StickyAccountSelector",
    "log_switch",
    "read_legacy_accounts",
    "read_selected_account_id",
    "read_switches",
    "selector_for",
]
