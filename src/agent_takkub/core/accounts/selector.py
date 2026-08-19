"""`AccountSelector` strategies (`core.contracts.account_selector.AccountSelector`)
— manual/priority/sticky/quota-aware/cooldown-failover
(`core.models.account.SelectionStrategy`, epic #309 Phase 3).

Every strategy only ever picks from `accounts` filtered to
`pool.account_ids` — a pool never "sees" an account outside its own list
even if the registry holds one more for the same provider — and only from
accounts currently `AccountStatus.ACTIVE`. An external signal (today:
nothing wired; a later phase's rate-limit/error detection) is what moves an
account OUT of `ACTIVE` in the first place; these selectors only consume
that status, never set it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from agent_takkub.core.models.account import (
    AccountPool,
    AccountStatus,
    ProviderAccount,
    SelectionStrategy,
)


def _in_pool(pool: AccountPool, accounts: Sequence[ProviderAccount]) -> list[ProviderAccount]:
    ids = set(pool.account_ids)
    return [a for a in accounts if a.id in ids]


def _active(accounts: Sequence[ProviderAccount]) -> list[ProviderAccount]:
    return [a for a in accounts if a.status == AccountStatus.ACTIVE]


def _highest_priority(accounts: Sequence[ProviderAccount]) -> ProviderAccount | None:
    if not accounts:
        return None
    # Ties broken by id for a deterministic, test-stable result.
    return max(accounts, key=lambda a: (a.priority, a.id))


class ManualAccountSelector:
    """Pins a single caller-chosen account id. Returns it only when it is
    both a member of `pool.account_ids` and currently ACTIVE — a manual pin
    is a preference, not an override of a genuinely unusable account."""

    def __init__(self, account_id: str) -> None:
        self._account_id = account_id

    def select(
        self, pool: AccountPool, accounts: Sequence[ProviderAccount]
    ) -> ProviderAccount | None:
        for account in _active(_in_pool(pool, accounts)):
            if account.id == self._account_id:
                return account
        return None


class PriorityAccountSelector:
    """Highest `priority` ACTIVE account in the pool."""

    def select(
        self, pool: AccountPool, accounts: Sequence[ProviderAccount]
    ) -> ProviderAccount | None:
        return _highest_priority(_active(_in_pool(pool, accounts)))


class StickyAccountSelector:
    """Keeps returning the same account for a given pool across calls
    (session affinity) as long as it stays ACTIVE; falls back to priority
    the first time, and again whenever the sticky pick drops out."""

    def __init__(self) -> None:
        self._last: dict[str, str] = {}

    def select(
        self, pool: AccountPool, accounts: Sequence[ProviderAccount]
    ) -> ProviderAccount | None:
        candidates = _active(_in_pool(pool, accounts))
        by_id = {a.id: a for a in candidates}
        sticky_id = self._last.get(pool.id)
        if sticky_id and sticky_id in by_id:
            return by_id[sticky_id]
        picked = _highest_priority(candidates)
        if picked is not None:
            self._last[pool.id] = picked.id
        return picked


def _default_usage_lookup(provider_id: str) -> float | None:
    from agent_takkub.provider_usage import get_store

    usage = get_store().get(provider_id)
    return usage.utilization if usage is not None else None


def _default_account_usage_lookup(account: ProviderAccount) -> float | None:
    """Per-account usage when the account carries a real `config_dir`
    (epic #309 Phase 3b — closes the "known limitation" below): reads the
    `provider_usage.ProviderUsageStore` cache keyed by `(provider,
    config_dir)`. A cache miss triggers a non-blocking background fetch
    (`refresh_now`) and returns `None` for THIS call, same
    never-block-here contract `_default_usage_lookup` already has — the
    figure lands on the account's next selection. Falls back to the plain
    per-provider lookup when the account has no `config_dir` (legacy
    single-account-per-provider case, unchanged behavior)."""
    from agent_takkub.provider_usage import get_store

    if not account.config_dir:
        return _default_usage_lookup(account.provider_id)
    store = get_store()
    usage = store.get(account.provider_id, config_dir=account.config_dir)
    if usage is None:
        store.refresh_now(account.provider_id, config_dir=account.config_dir)
        return None
    return usage.utilization


class QuotaAwareAccountSelector:
    """Prefers the ACTIVE account whose provider currently has the LOWEST
    known usage utilization (`provider_usage.ProviderUsage`, the same cache
    `/api/usage` reads — never a live fetch here).

    `account_usage_lookup` (per-`ProviderAccount`) differentiates two real
    accounts of the same provider via `ProviderAccount.config_dir` — the
    per-account gap this class used to carry as a documented limitation is
    closed by `_default_account_usage_lookup` when an account has one set;
    an account with no `config_dir` still falls back to the old
    per-provider figure. `usage_lookup` (per-provider-id string) is kept
    for backward compatibility — passing it alone (without
    `account_usage_lookup`) still selects by provider-level usage only,
    unchanged from before this phase.
    """

    def __init__(
        self,
        usage_lookup: Callable[[str], float | None] | None = None,
        account_usage_lookup: Callable[[ProviderAccount], float | None] | None = None,
    ) -> None:
        self._usage_lookup = usage_lookup
        self._account_usage_lookup = account_usage_lookup or (
            (lambda a: usage_lookup(a.provider_id))
            if usage_lookup
            else _default_account_usage_lookup
        )

    def select(
        self, pool: AccountPool, accounts: Sequence[ProviderAccount]
    ) -> ProviderAccount | None:
        candidates = _active(_in_pool(pool, accounts))
        if not candidates:
            return None
        known = [(self._account_usage_lookup(a), a) for a in candidates]
        known = [(u, a) for u, a in known if u is not None]
        if not known:
            return _highest_priority(candidates)
        known.sort(key=lambda item: (item[0], -item[1].priority, item[1].id))
        return known[0][1]


class CooldownFailoverSelector:
    """Priority selection with FAILOVER MEMORY: when the account this pool
    was last using is no longer ACTIVE (a later phase's rate-limit/error
    detection moved it to `RATE_LIMITED`/`ERROR`/`DISABLED`), picking a
    different one logs a `core.accounts.switch_log` event — the "account
    switch = log event ไว้ก่อน" scope this strategy specifically owns. A
    A move to `None` (pool exhausted) is logged too; the very first
    `select()` for a pool is an initial assignment, never a failover, and is
    never logged.
    """

    def __init__(self) -> None:
        self._last: dict[str, str] = {}

    def select(
        self, pool: AccountPool, accounts: Sequence[ProviderAccount]
    ) -> ProviderAccount | None:
        picked = _highest_priority(_active(_in_pool(pool, accounts)))
        picked_id = picked.id if picked is not None else None
        prior_id = self._last.get(pool.id)
        # Only a MOVE away from an already-known pick counts as a switch —
        # the very first select() for a pool (prior_id is None) is an
        # initial assignment, not a failover, and must not log.
        if prior_id is not None and prior_id != picked_id:
            from agent_takkub.core.conversation.facade import on_account_switch

            from .switch_log import log_switch

            log_switch(pool.id, pool.provider_id, prior_id, picked_id, reason="cooldown_failover")
            on_account_switch(
                pool.id, pool.provider_id, prior_id, picked_id, reason="cooldown_failover"
            )
        if picked_id is not None:
            self._last[pool.id] = picked_id
        elif pool.id in self._last:
            del self._last[pool.id]
        return picked


def selector_for(strategy: SelectionStrategy) -> object:
    """Factory: `SelectionStrategy` -> a fresh selector instance.

    `MANUAL` has no meaningful default (needs a caller-chosen account id) —
    raises rather than silently substituting a different strategy.
    """
    if strategy == SelectionStrategy.MANUAL:
        raise ValueError(
            "SelectionStrategy.MANUAL needs an account id — "
            "construct ManualAccountSelector(account_id) directly"
        )
    factory: dict[SelectionStrategy, Callable[[], object]] = {
        SelectionStrategy.PRIORITY: PriorityAccountSelector,
        SelectionStrategy.STICKY: StickyAccountSelector,
        SelectionStrategy.QUOTA_AWARE: QuotaAwareAccountSelector,
        SelectionStrategy.COOLDOWN_FAILOVER: CooldownFailoverSelector,
    }
    return factory[strategy]()
