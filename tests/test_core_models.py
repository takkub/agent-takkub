"""Core V2 domain models — frozen/slots, defaults, and JSON round-trip via
`dataclasses.asdict` (docs/v2/V2_IMPLEMENTATION_PLAN.md Phase 1)."""

from __future__ import annotations

import dataclasses
import json

import pytest

from agent_takkub.core.models.account import AccountPool, AccountStatus, ProviderAccount
from agent_takkub.core.models.agent import AgentInstance, AgentStatus, AgentTemplate
from agent_takkub.core.models.capability import CapabilityScope, MCPServer, Plugin, Skill, Tool
from agent_takkub.core.models.conversation import Checkpoint, Conversation, ProviderSessionBinding
from agent_takkub.core.models.memory import Confidence, MemoryKind, MemoryRecord, Scope, Trust
from agent_takkub.core.models.model import ModelDefinition, ModelProfile
from agent_takkub.core.models.provider import ProviderDefinition, ProviderFeature, Transport
from agent_takkub.core.models.task import Task, TaskState
from agent_takkub.core.models.version import CompatibilityRule, ComponentVersion

ALL_MODEL_CLASSES = [
    ProviderDefinition,
    ProviderAccount,
    AccountPool,
    ModelDefinition,
    ModelProfile,
    AgentTemplate,
    AgentInstance,
    Task,
    Conversation,
    ProviderSessionBinding,
    Checkpoint,
    Skill,
    MCPServer,
    Plugin,
    Tool,
    MemoryRecord,
    ComponentVersion,
    CompatibilityRule,
]


@pytest.mark.parametrize("cls", ALL_MODEL_CLASSES)
def test_frozen_and_slotted(cls):
    assert dataclasses.is_dataclass(cls)
    assert cls.__dataclass_params__.frozen is True
    assert "__slots__" in cls.__dict__
    # slots=True dataclasses have no per-instance __dict__.
    instance = cls(**_minimal_kwargs(cls))
    assert not hasattr(instance, "__dict__")


@pytest.mark.parametrize("cls", ALL_MODEL_CLASSES)
def test_persistent_models_default_to_single_user(cls):
    fields = {f.name for f in dataclasses.fields(cls)}
    for scope_field in ("user_id", "workspace_id", "project_id"):
        assert scope_field in fields, f"{cls.__name__} missing {scope_field}"
    instance_kwargs = _minimal_kwargs(cls)
    instance = cls(**instance_kwargs)
    assert instance.user_id is None
    assert instance.workspace_id is None
    assert instance.project_id is None


def _minimal_kwargs(cls) -> dict:
    """Fill only fields without a default, using cheap valid stand-ins."""
    kwargs = {}
    for f in dataclasses.fields(cls):
        if f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
            continue
        if f.name == "kind":
            kwargs[f.name] = MemoryKind.PROJECT
        elif f.name.endswith("_id") or f.name == "id":
            kwargs[f.name] = "id-1"
        else:
            kwargs[f.name] = "x"
    return kwargs


def test_frozen_instance_is_immutable():
    account = ProviderAccount(id="a1", provider_id="claude")
    with pytest.raises(dataclasses.FrozenInstanceError):
        account.label = "changed"  # type: ignore[misc]


def test_provider_definition_round_trip():
    original = ProviderDefinition(
        id="p1",
        name="claude",
        display_name="Claude",
        transport=Transport.PTY,
        features=frozenset({ProviderFeature.RESUME, ProviderFeature.MIRROR}),
    )
    as_dict = dataclasses.asdict(original)
    # enums/frozensets survive asdict as their raw values — convert back explicitly,
    # the same shape a JSONL writer/reader pair would use.
    as_dict["transport"] = Transport(as_dict["transport"])
    as_dict["features"] = frozenset(ProviderFeature(v) for v in as_dict["features"])
    rebuilt = ProviderDefinition(**as_dict)
    assert rebuilt == original
    # asdict output must itself be JSON-serializable once enums are stringified.
    json_ready = dataclasses.asdict(original)
    json_ready["transport"] = original.transport.value
    json_ready["features"] = sorted(f.value for f in original.features)
    json.dumps(json_ready)


def test_task_state_and_agent_status_enums_are_str():
    assert TaskState.DONE == "done"
    assert AgentStatus.RUNNING == "running"
    assert AccountStatus.ACTIVE == "active"
    assert CapabilityScope.PROJECT == "project"


def test_memory_record_trust_has_five_levels():
    levels = {t.value for t in Trust}
    assert levels == {
        "cockpit_measured",
        "user_confirmed",
        "lead_confirmed",
        "agent_reported",
        "external_untrusted",
    }
    record = MemoryRecord(id="m1", kind=MemoryKind.FEEDBACK, content="never mock the db")
    assert record.trust == Trust.AGENT_REPORTED
    assert record.confidence == Confidence.MEDIUM
    assert record.scope == Scope.PROJECT
