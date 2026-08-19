"""Pure multi-dimension admission check — extends `resource_governor`'s
existing heavy/class/project checks with provider/account/project-agents/
project-panes/global-agents/global-panes dimensions
(docs/v2/07_SCHEDULER_RESOURCE_RUNTIME.md §4-7). Returns "" (allowed) for
every dimension whose `SlotPolicy` value is None/absent, so an all-defaults
`SlotPolicy()` denies nothing — the legacy `resource_governor` checks remain
the only gate when a caller doesn't opt into a limit."""

from __future__ import annotations

from .models import ActiveCounts, SlotPolicy, SlotRequest


def evaluate(request: SlotRequest, counts: ActiveCounts, slot_policy: SlotPolicy) -> str:
    if (
        slot_policy.max_agents_global is not None
        and counts.agents_global >= slot_policy.max_agents_global
    ):
        return "global_agents_limit"
    if (
        slot_policy.max_panes_global is not None
        and counts.panes_global >= slot_policy.max_panes_global
    ):
        return "global_panes_limit"
    if request.provider_id is not None:
        limit = slot_policy.provider_max_concurrent.get(request.provider_id)
        if limit is not None and counts.by_provider.get(request.provider_id, 0) >= limit:
            return "provider_limit"
    if request.account_id is not None:
        limit = slot_policy.account_max_concurrent.get(request.account_id)
        if limit is not None and counts.by_account.get(request.account_id, 0) >= limit:
            return "account_limit"
    agents_limit = slot_policy.project_max_agents.get(request.project_id)
    if (
        agents_limit is not None
        and counts.by_project_agents.get(request.project_id, 0) >= agents_limit
    ):
        return "project_agents_limit"
    panes_limit = slot_policy.project_max_panes.get(request.project_id)
    if (
        panes_limit is not None
        and counts.by_project_panes.get(request.project_id, 0) >= panes_limit
    ):
        return "project_panes_limit"
    return ""
