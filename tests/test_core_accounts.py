"""core.accounts — registry, legacy reader, switch log, selector strategies
(docs/v2/V2_IMPLEMENTATION_PLAN.md §2 Phase 2 "Account", epic #309 Phase 3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub.core.accounts.legacy_reader import read_legacy_accounts, read_selected_account_id
from agent_takkub.core.accounts.registry import AccountPoolRegistry, AccountRegistry
from agent_takkub.core.accounts.selector import (
    CooldownFailoverSelector,
    ManualAccountSelector,
    PriorityAccountSelector,
    QuotaAwareAccountSelector,
    StickyAccountSelector,
    selector_for,
)
from agent_takkub.core.accounts.switch_log import log_switch, read_switches
from agent_takkub.core.models.account import (
    AccountPool,
    AccountStatus,
    ProviderAccount,
    SelectionStrategy,
)
from agent_takkub.core.storage.jsonl_store import JsonlStore


def _account(id_, provider="claude", status=AccountStatus.ACTIVE, priority=0):
    return ProviderAccount(id=id_, provider_id=provider, status=status, priority=priority)


def _pool(account_ids, strategy=SelectionStrategy.PRIORITY, provider="claude"):
    return AccountPool(
        id="pool-1",
        name="pool",
        provider_id=provider,
        account_ids=tuple(account_ids),
        strategy=strategy,
    )


# ── AccountRegistry / AccountPoolRegistry ────────────────────────────────


def test_account_registry_upsert_and_get(tmp_path):
    reg = AccountRegistry(JsonlStore(tmp_path / "accounts.jsonl"))
    reg.upsert(_account("a1"))
    got = reg.get("a1")
    assert got is not None
    assert got.provider_id == "claude"
    assert got.status == AccountStatus.ACTIVE


def test_account_registry_upsert_is_latest_wins(tmp_path):
    reg = AccountRegistry(JsonlStore(tmp_path / "accounts.jsonl"))
    reg.upsert(_account("a1", priority=0))
    reg.upsert(_account("a1", priority=5))
    accounts = reg.all()
    assert len(accounts) == 1
    assert accounts[0].priority == 5


def test_account_registry_delete_is_tombstone(tmp_path):
    reg = AccountRegistry(JsonlStore(tmp_path / "accounts.jsonl"))
    reg.upsert(_account("a1"))
    reg.delete("a1")
    assert reg.get("a1") is None
    assert reg.all() == []


def test_account_registry_for_provider_filters(tmp_path):
    reg = AccountRegistry(JsonlStore(tmp_path / "accounts.jsonl"))
    reg.upsert(_account("a1", provider="claude"))
    reg.upsert(_account("a2", provider="codex"))
    assert [a.id for a in reg.for_provider("codex")] == ["a2"]


def test_pool_registry_round_trip(tmp_path):
    reg = AccountPoolRegistry(JsonlStore(tmp_path / "pools.jsonl"))
    reg.upsert(_pool(["a1", "a2"], strategy=SelectionStrategy.STICKY))
    pool = reg.get("pool-1")
    assert pool is not None
    assert pool.account_ids == ("a1", "a2")
    assert pool.strategy == SelectionStrategy.STICKY


# ── LegacyReader ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolate_user_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from agent_takkub import user_profile as up

    monkeypatch.setattr(up, "_BASE_DIR", tmp_path)
    monkeypatch.setattr(up, "_REGISTRY_PATH", tmp_path / "user-profiles.json")
    monkeypatch.setattr(up, "_DEFAULT_CONFIG_DIR", tmp_path / "dot-claude")


def test_read_legacy_accounts_default_only():
    accounts = read_legacy_accounts()
    assert len(accounts) == 1
    assert accounts[0].id == "legacy-claude-default"
    assert accounts[0].provider_id == "claude"
    assert accounts[0].secret_ref == "secret://claude/default"
    assert accounts[0].priority == 0


def test_read_legacy_accounts_populates_config_dir(tmp_path):
    from agent_takkub import user_profile as up

    accounts = read_legacy_accounts()
    assert accounts[0].config_dir == str(up._DEFAULT_CONFIG_DIR)

    up.add_profile("work", str(tmp_path / "claude-work"))
    accounts = read_legacy_accounts()
    work = next(a for a in accounts if a.id == "legacy-claude-work")
    assert work.config_dir == str(tmp_path / "claude-work")


def test_read_legacy_accounts_includes_registered_profile():
    from agent_takkub import user_profile as up

    up.add_profile("work", "/home/user/.claude-work")
    accounts = read_legacy_accounts()
    ids = {a.id for a in accounts}
    assert ids == {"legacy-claude-default", "legacy-claude-work"}
    work = next(a for a in accounts if a.id == "legacy-claude-work")
    assert work.priority == 10


def test_read_selected_account_id_defaults_to_default_profile():
    assert read_selected_account_id("some-project") == "legacy-claude-default"


def test_read_selected_account_id_follows_project_selection():
    from agent_takkub import user_profile as up

    up.add_profile("work", "/home/user/.claude-work")
    up.set_profile("some-project", "work")
    assert read_selected_account_id("some-project") == "legacy-claude-work"


# ── switch_log ────────────────────────────────────────────────────────────


def test_log_switch_appends_and_reads_back(tmp_path):
    store = JsonlStore(tmp_path / "account_switches.jsonl")
    log_switch("pool-1", "claude", "a1", "a2", reason="test", store=store)
    records = read_switches(store)
    assert len(records) == 1
    assert records[0]["from_account_id"] == "a1"
    assert records[0]["to_account_id"] == "a2"
    assert records[0]["reason"] == "test"


# ── selectors ─────────────────────────────────────────────────────────────


def test_manual_selector_returns_pinned_active_account():
    accounts = [_account("a1"), _account("a2")]
    pool = _pool(["a1", "a2"])
    assert ManualAccountSelector("a2").select(pool, accounts).id == "a2"


def test_manual_selector_none_when_pinned_account_not_active():
    accounts = [_account("a1"), _account("a2", status=AccountStatus.DISABLED)]
    pool = _pool(["a1", "a2"])
    assert ManualAccountSelector("a2").select(pool, accounts) is None


def test_manual_selector_none_when_pinned_account_outside_pool():
    accounts = [_account("a1"), _account("a2")]
    pool = _pool(["a1"])
    assert ManualAccountSelector("a2").select(pool, accounts) is None


def test_priority_selector_picks_highest_priority_active():
    accounts = [_account("a1", priority=1), _account("a2", priority=5)]
    pool = _pool(["a1", "a2"])
    assert PriorityAccountSelector().select(pool, accounts).id == "a2"


def test_priority_selector_skips_non_active():
    accounts = [
        _account("a1", priority=5, status=AccountStatus.RATE_LIMITED),
        _account("a2", priority=1),
    ]
    pool = _pool(["a1", "a2"])
    assert PriorityAccountSelector().select(pool, accounts).id == "a2"


def test_priority_selector_none_when_pool_empty():
    assert PriorityAccountSelector().select(_pool([]), []) is None


def test_sticky_selector_keeps_same_account_across_calls():
    selector = StickyAccountSelector()
    accounts = [_account("a1", priority=5), _account("a2", priority=1)]
    pool = _pool(["a1", "a2"])
    first = selector.select(pool, accounts)
    assert first.id == "a1"
    # a2 now has the higher priority, but sticky must still return a1.
    accounts2 = [_account("a1", priority=1), _account("a2", priority=5)]
    second = selector.select(pool, accounts2)
    assert second.id == "a1"


def test_sticky_selector_falls_back_when_sticky_account_drops_out():
    selector = StickyAccountSelector()
    accounts = [_account("a1", priority=5), _account("a2", priority=1)]
    pool = _pool(["a1", "a2"])
    assert selector.select(pool, accounts).id == "a1"
    accounts_without_a1 = [_account("a1", status=AccountStatus.ERROR), _account("a2", priority=1)]
    assert selector.select(pool, accounts_without_a1).id == "a2"


def test_quota_aware_selector_prefers_lower_utilization():
    accounts = [_account("a1", provider="claude"), _account("a2", provider="codex")]
    pool = AccountPool(id="pool-1", name="pool", provider_id="claude", account_ids=("a1", "a2"))
    lookup = {"claude": 80.0, "codex": 10.0}
    selector = QuotaAwareAccountSelector(usage_lookup=lookup.get)
    assert selector.select(pool, accounts).id == "a2"


def test_quota_aware_selector_falls_back_to_priority_when_usage_unknown():
    accounts = [_account("a1", priority=1), _account("a2", priority=9)]
    pool = _pool(["a1", "a2"])
    selector = QuotaAwareAccountSelector(usage_lookup=lambda _p: None)
    assert selector.select(pool, accounts).id == "a2"


def test_quota_aware_selector_differentiates_same_provider_by_account(monkeypatch):
    # epic #309 Phase 3b: closes the old "same-provider accounts always see
    # the same figure" limitation — account A pinned near its limit must
    # lose to account B of the SAME provider once utilization differs.
    from agent_takkub.core.accounts.selector import _default_account_usage_lookup
    from agent_takkub.core.models.account import ProviderAccount

    a_hot = ProviderAccount(id="a1", provider_id="claude", config_dir="/cfg/a")
    b_cool = ProviderAccount(id="a2", provider_id="claude", config_dir="/cfg/b")
    pool = AccountPool(id="pool-1", name="pool", provider_id="claude", account_ids=("a1", "a2"))

    fake_usage = {"/cfg/a": 97.0, "/cfg/b": 12.0}

    class _FakeStore:
        def get(self, provider, config_dir=None):
            class _U:
                utilization = fake_usage[str(config_dir)]

            return _U() if str(config_dir) in fake_usage else None

        def refresh_now(self, provider, config_dir=None):
            pass

    monkeypatch.setattr("agent_takkub.provider_usage.get_store", lambda: _FakeStore())

    selector = QuotaAwareAccountSelector()
    picked = selector.select(pool, [a_hot, b_cool])
    assert picked.id == "a2"
    # Direct lookup-function proof, independent of the selector's own sort.
    assert _default_account_usage_lookup(a_hot) == 97.0
    assert _default_account_usage_lookup(b_cool) == 12.0


def test_quota_aware_selector_account_with_no_config_dir_falls_back_to_provider_lookup(
    monkeypatch,
):
    from agent_takkub.core.accounts.selector import _default_account_usage_lookup
    from agent_takkub.core.models.account import ProviderAccount

    account = ProviderAccount(id="a1", provider_id="claude", config_dir=None)

    class _FakeUsage:
        utilization = 42.0

    class _FakeStore:
        def get(self, provider, config_dir=None):
            assert config_dir is None
            return _FakeUsage()

    monkeypatch.setattr("agent_takkub.provider_usage.get_store", lambda: _FakeStore())
    assert _default_account_usage_lookup(account) == 42.0


def test_cooldown_failover_selector_picks_priority_first_time():
    accounts = [_account("a1", priority=5), _account("a2", priority=1)]
    pool = _pool(["a1", "a2"])
    assert CooldownFailoverSelector().select(pool, accounts).id == "a1"


def test_cooldown_failover_selector_logs_switch_when_account_drops_out(tmp_path, monkeypatch):
    store = JsonlStore(tmp_path / "account_switches.jsonl")
    monkeypatch.setattr(
        "agent_takkub.core.accounts.switch_log.log_switch",
        lambda *a, **kw: log_switch(*a, store=store, **kw),
    )
    selector = CooldownFailoverSelector()
    accounts = [_account("a1", priority=5), _account("a2", priority=1)]
    pool = _pool(["a1", "a2"])
    assert selector.select(pool, accounts).id == "a1"

    accounts_a1_limited = [
        _account("a1", priority=5, status=AccountStatus.RATE_LIMITED),
        _account("a2", priority=1),
    ]
    assert selector.select(pool, accounts_a1_limited).id == "a2"

    records = read_switches(store)
    assert len(records) == 1
    assert records[0] == {
        "ts": records[0]["ts"],
        "pool_id": "pool-1",
        "provider_id": "claude",
        "from_account_id": "a1",
        "to_account_id": "a2",
        "reason": "cooldown_failover",
    }


def test_selector_for_manual_raises():
    with pytest.raises(ValueError):
        selector_for(SelectionStrategy.MANUAL)


# ── AccountRegistry config_dir round-trip ────────────────────────────────


def test_account_registry_round_trips_config_dir(tmp_path):
    reg = AccountRegistry(JsonlStore(tmp_path / "accounts.jsonl"))
    reg.upsert(ProviderAccount(id="a1", provider_id="claude", config_dir="/home/u/.claude-work"))
    got = reg.get("a1")
    assert got.config_dir == "/home/u/.claude-work"


# ── resolve_account_for façade ───────────────────────────────────────────


def test_resolve_account_for_falls_back_to_legacy_when_no_pool(tmp_path):
    from agent_takkub.core.accounts.facade import resolve_account_for
    from agent_takkub.core.accounts.registry import AccountPoolRegistry
    from agent_takkub.core.storage.jsonl_store import JsonlStore as _JsonlStore

    empty_pools = AccountPoolRegistry(_JsonlStore(tmp_path / "pools.jsonl"))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent_takkub.core.accounts.facade.AccountPoolRegistry", lambda: empty_pools)
        account = resolve_account_for("claude", "some-project")
    assert account is not None
    assert account.id == "legacy-claude-default"


def test_resolve_account_for_prefers_registered_pool(tmp_path):
    from agent_takkub.core.accounts.facade import resolve_account_for
    from agent_takkub.core.accounts.registry import AccountPoolRegistry, AccountRegistry
    from agent_takkub.core.storage.jsonl_store import JsonlStore as _JsonlStore

    pools = AccountPoolRegistry(_JsonlStore(tmp_path / "pools.jsonl"))
    accounts = AccountRegistry(_JsonlStore(tmp_path / "accounts.jsonl"))
    accounts.upsert(_account("pool-a1", provider="claude", priority=5))
    pools.upsert(
        AccountPool(
            id="p1",
            name="p1",
            provider_id="claude",
            account_ids=("pool-a1",),
            strategy=SelectionStrategy.PRIORITY,
        )
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent_takkub.core.accounts.facade.AccountPoolRegistry", lambda: pools)
        mp.setattr("agent_takkub.core.accounts.facade.AccountRegistry", lambda: accounts)
        account = resolve_account_for("claude", "some-project")
    assert account.id == "pool-a1"


@pytest.mark.parametrize(
    "strategy,cls",
    [
        (SelectionStrategy.PRIORITY, PriorityAccountSelector),
        (SelectionStrategy.STICKY, StickyAccountSelector),
        (SelectionStrategy.QUOTA_AWARE, QuotaAwareAccountSelector),
        (SelectionStrategy.COOLDOWN_FAILOVER, CooldownFailoverSelector),
    ],
)
def test_selector_for_returns_matching_type(strategy, cls):
    assert isinstance(selector_for(strategy), cls)
