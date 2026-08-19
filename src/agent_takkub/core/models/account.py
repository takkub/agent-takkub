"""Account/pool vocabulary — the "Intelligence" layer the audit found mostly
empty (REUSE_VS_REWRITE_MATRIX.md §6): today `user_profile.py` holds a single
implicit account per provider. `ProviderAccount`/`AccountPool` are NEW,
wired up for real in Phase 2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class AccountStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"


class SelectionStrategy(StrEnum):
    """How `AccountSelector` (core/contracts/account_selector.py) picks
    one account out of a pool — plan §2 Phase 2: "priority/sticky/quota-aware".
    MANUAL and COOLDOWN_FAILOVER added Phase 3 (epic #309) per the task's
    5-strategy list; additive to the Phase 1 enum, no existing member changed."""

    MANUAL = "manual"
    PRIORITY = "priority"
    STICKY = "sticky"
    QUOTA_AWARE = "quota_aware"
    COOLDOWN_FAILOVER = "cooldown_failover"


@dataclass(frozen=True, slots=True)
class AccountLimits:
    """Scheduler-facing cap for one account (epic #309 Phase 8a,
    docs/v2/07_SCHEDULER_RESOURCE_RUNTIME.md §6). `max_concurrent=None` is
    unlimited — the default, so an account built without limits behaves
    exactly as before `core.scheduling` existed."""

    max_concurrent: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderAccount:
    """One credentialed identity for a provider. `secret_ref` is a pointer
    into the future SecretManager (Phase 3) — never a raw credential."""

    id: str
    provider_id: str
    label: str = ""
    secret_ref: str | None = None
    status: AccountStatus = AccountStatus.ACTIVE
    priority: int = 0
    user_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None
    limits: AccountLimits = field(default_factory=AccountLimits)


@dataclass(frozen=True, slots=True)
class AccountPool:
    """A named group of accounts for one provider, resolved via `strategy`."""

    id: str
    name: str
    provider_id: str
    account_ids: tuple[str, ...] = field(default_factory=tuple)
    strategy: SelectionStrategy = SelectionStrategy.PRIORITY
    user_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None
