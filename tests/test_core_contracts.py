"""Core V2 contracts — every Protocol is `runtime_checkable` and sync-only
(no `async def`), and a fake implementing every method satisfies isinstance()
(docs/v2/V2_IMPLEMENTATION_PLAN.md Phase 1, R1)."""

from __future__ import annotations

import inspect
import typing

from agent_takkub.core.contracts import (
    account_selector,
    brain_adapter,
    migration,
    provider_adapter,
    secret_manager,
    store,
)
from agent_takkub.core.models.account import AccountPool
from agent_takkub.core.models.agent import AgentInstance
from agent_takkub.core.models.memory import MemoryRecord, Scope

CONTRACT_MODULES = [
    account_selector,
    brain_adapter,
    migration,
    provider_adapter,
    secret_manager,
    store,
]


def _protocol_classes(module):
    return [
        obj
        for _name, obj in vars(module).items()
        if isinstance(obj, type) and issubclass(obj, typing.Protocol) and obj is not typing.Protocol
    ]


def test_every_contract_is_a_runtime_checkable_protocol():
    found_any = False
    for module in CONTRACT_MODULES:
        for cls in _protocol_classes(module):
            found_any = True
            assert typing.Protocol in cls.__mro__
            assert getattr(cls, "_is_runtime_protocol", False) is True
    assert found_any


def test_no_contract_method_is_async():
    for module in CONTRACT_MODULES:
        for cls in _protocol_classes(module):
            for name, member in vars(cls).items():
                if name.startswith("_"):
                    continue
                if inspect.isfunction(member):
                    assert not inspect.iscoroutinefunction(member), (
                        f"{cls.__name__}.{name} is async"
                    )


class _FakeProviderAdapter:
    def provider_id(self) -> str:
        return "claude"

    def is_available(self) -> bool:
        return True

    def spawn(self, instance: AgentInstance) -> None:
        pass

    def send(self, instance: AgentInstance, text: str) -> None:
        pass

    def is_ready(self, instance: AgentInstance) -> bool:
        return True

    def terminate(self, instance: AgentInstance) -> None:
        pass


def test_fake_provider_adapter_satisfies_protocol():
    assert isinstance(_FakeProviderAdapter(), provider_adapter.ProviderAdapter)


class _FakeAccountSelector:
    def select(self, pool: AccountPool, accounts):
        return accounts[0] if accounts else None


def test_fake_account_selector_satisfies_protocol():
    assert isinstance(_FakeAccountSelector(), account_selector.AccountSelector)


class _FakeBrainAdapter:
    def recall(self, query: str, *, scope: Scope, limit: int = 10):
        return []

    def remember(self, record: MemoryRecord) -> None:
        pass


def test_fake_brain_adapter_satisfies_protocol():
    assert isinstance(_FakeBrainAdapter(), brain_adapter.BrainAdapter)


class _FakeSecretManager:
    def get_secret(self, secret_ref: str) -> str:
        return "secret"

    def set_secret(self, secret_ref: str, value: str) -> None:
        pass

    def delete_secret(self, secret_ref: str) -> None:
        pass


def test_fake_secret_manager_satisfies_protocol():
    assert isinstance(_FakeSecretManager(), secret_manager.SecretManager)


class _FakeStateStore:
    def append(self, record: dict) -> None:
        pass

    def read_all(self) -> list:
        return []


def test_fake_state_store_satisfies_protocol():
    assert isinstance(_FakeStateStore(), store.StateStore)


class _FakeMigrationStep:
    def inspect(self):
        return {}

    def plan(self):
        return {}

    def dry_run(self):
        return {}

    def apply(self):
        return {}

    def validate(self) -> bool:
        return True

    def rollback(self) -> None:
        pass


def test_fake_migration_step_satisfies_protocol():
    assert isinstance(_FakeMigrationStep(), migration.MigrationStep)
