"""`resolve_account_for` — the one façade call `spawn_engine.py` uses to
find which `ProviderAccount` should back a spawn (epic #309 Phase 3b), same
fail-open shape as `core.routing.facade.effective_provider_for_v2`.

Real `AccountPool`/`AccountRegistry` wins when one is configured for this
provider (`selector_for(pool.strategy)` — quota-aware/cooldown-failover/etc.
all apply for real here). Nothing populates a pool today (Phase 3 report
§4), so the practical path for every project right now is the legacy
fallback: whichever profile `user_profile`/`read_selected_account_id`
already has selected — the SAME account `pane_env.inject_user_profile_env`
resolves from, so wiring this in changes nothing observable until a pool is
actually registered.
"""

from __future__ import annotations

import logging

from agent_takkub.core.models.account import AccountPool, ProviderAccount, SelectionStrategy

from .registry import AccountPoolRegistry, AccountRegistry
from .selector import ManualAccountSelector, selector_for

_log = logging.getLogger(__name__)


def _pool_for(
    provider_id: str, project: str | None, pools: list[AccountPool]
) -> AccountPool | None:
    project_matches = [p for p in pools if p.provider_id == provider_id and p.project_id == project]
    if project_matches:
        return project_matches[0]
    global_matches = [p for p in pools if p.provider_id == provider_id and p.project_id is None]
    return global_matches[0] if global_matches else None


def _select_from_pool(pool: AccountPool, accounts: list[ProviderAccount]) -> ProviderAccount | None:
    if pool.strategy == SelectionStrategy.MANUAL:
        if not pool.account_ids:
            return None
        selector = ManualAccountSelector(pool.account_ids[0])
    else:
        selector = selector_for(pool.strategy)
    return selector.select(pool, accounts)


def resolve_account_for(
    provider_id: str, project: str, role: str | None = None
) -> ProviderAccount | None:
    """Which `ProviderAccount` should back a *provider_id* spawn for
    *project* — a real V2 pool if one is registered, else the legacy
    profile selection. Never raises: any failure resolves to `None` (the
    caller then applies no env override, i.e. today's behavior)."""
    try:
        pools = AccountPoolRegistry().all()
        pool = _pool_for(provider_id, project, pools)
        if pool is not None:
            accounts = AccountRegistry().for_provider(provider_id)
            picked = _select_from_pool(pool, accounts)
            if picked is not None:
                return picked
    except Exception:
        _log.exception(
            "core.accounts pool resolution failed for provider=%r project=%r role=%r "
            "(fail-open, falling back to legacy profile)",
            provider_id,
            project,
            role,
        )

    try:
        from .legacy_reader import read_legacy_accounts, read_selected_account_id

        account_id = read_selected_account_id(project, provider_id)
        for account in read_legacy_accounts(project):
            if account.id == account_id:
                return account
    except Exception:
        _log.exception(
            "core.accounts legacy fallback failed for provider=%r project=%r role=%r",
            provider_id,
            project,
            role,
        )
    return None
