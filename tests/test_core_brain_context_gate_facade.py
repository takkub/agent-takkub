"""core.brain.facade.build_context_for_assign wired to the Context Gate
(closeout #C, `03_CONTEXT_TOKEN_EFFICIENCY.md`): small tasks never call
OpenViking/Resource, budgets clamp per task size, the gate trace records
task_size + the "inefficient" flag, and `TAKKUB_CONTEXT_GATE=0` reproduces
the exact pre-gate behavior.
"""

from __future__ import annotations

import pytest

from agent_takkub.core.brain import context_builder
from agent_takkub.core.brain.store import BrainStore
from agent_takkub.core.context_sources.doctor_section import build_findings
from agent_takkub.core.context_sources.trace_store import load_last_trace
from agent_takkub.core.models.memory import MemoryKind, MemoryRecord


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    import agent_takkub.config as config

    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_HOME", tmp_path / "data")
    monkeypatch.setenv("TAKKUB_V2_CONTEXT", "1")
    monkeypatch.setenv("TAKKUB_V2_CONVERSATION", "0")
    monkeypatch.delenv("TAKKUB_CONTEXT_GATE", raising=False)
    monkeypatch.delenv("TAKKUB_OPENVIKING_ENABLED", raising=False)
    monkeypatch.delenv("TAKKUB_OPENVIKING_MODE", raising=False)
    return tmp_path


def _rec(**kwargs) -> MemoryRecord:
    defaults = dict(id=kwargs.pop("id", "r"), kind=MemoryKind.PROJECT, content="x")
    defaults.update(kwargs)
    return MemoryRecord(**defaults)


def _fake_item(text: str, *, project_id: str = "proj"):
    """`project_id`/`workspace_id` default to matching every test in this
    file's own `build_context_for_assign(..., "proj", ...)` call — without
    them the fixture is indistinguishable from a real item with no scope
    claim at all, which `apply_scope_and_trust`'s layer-c re-check (issue
    #372 follow-up, `02_OPENVIKING_STRICT_SCOPE.md`) fails closed on."""
    from agent_takkub.core.context_sources.base import WORKSPACE_ID, ContextItem

    return ContextItem(
        text=text,
        tokens=max(1, len(text) // 4),
        source="openviking",
        provenance="p",
        trust="external",
        score=0.5,
        project_id=project_id,
        workspace_id=WORKSPACE_ID,
    )


# ── small task: reference sources skipped entirely ────────────────────────


def test_small_task_never_calls_openviking_or_resource(runtime, monkeypatch):
    import agent_takkub.core.brain.facade as facade

    monkeypatch.setenv("TAKKUB_OPENVIKING_ENABLED", "1")
    monkeypatch.setenv("TAKKUB_OPENVIKING_MODE", "hybrid")
    from agent_takkub.core.context_sources.openviking_source import OpenVikingSource as _OVS
    from agent_takkub.core.context_sources.resource_source import ResourceSource as _RS

    calls: list[str] = []

    def spy(name):
        def _retrieve(self, *a, **kw):
            calls.append(name)
            return []

        return _retrieve

    monkeypatch.setattr(_OVS, "retrieve", spy("openviking"))
    monkeypatch.setattr(_RS, "retrieve", spy("resource"))

    out = facade.build_context_for_assign("proj", "backend", "change button color")
    assert calls == []
    assert "OpenViking" not in out

    trace = load_last_trace()
    assert trace["task_size"] == "small"


def test_medium_task_calls_openviking(runtime, monkeypatch):
    import agent_takkub.core.brain.facade as facade

    monkeypatch.setenv("TAKKUB_OPENVIKING_ENABLED", "1")
    monkeypatch.setenv("TAKKUB_OPENVIKING_MODE", "read")
    from agent_takkub.core.context_sources.openviking_source import OpenVikingSource as _OVS

    monkeypatch.setattr(
        _OVS,
        "retrieve",
        lambda self, query, *, project, role, budget_tokens: [
            _fake_item("openviking hit for a medium task")
        ],
    )
    out = facade.build_context_for_assign("proj", "backend", "refactor the login feature")
    assert "openviking hit for a medium task" in out

    trace = load_last_trace()
    assert trace["task_size"] == "medium"


def test_large_task_calls_openviking(runtime, monkeypatch):
    import agent_takkub.core.brain.facade as facade

    monkeypatch.setenv("TAKKUB_OPENVIKING_ENABLED", "1")
    monkeypatch.setenv("TAKKUB_OPENVIKING_MODE", "read")
    from agent_takkub.core.context_sources.openviking_source import OpenVikingSource as _OVS

    monkeypatch.setattr(
        _OVS,
        "retrieve",
        lambda self, query, *, project, role, budget_tokens: [
            _fake_item("openviking hit for a large task")
        ],
    )
    out = facade.build_context_for_assign("proj", "backend", "design a new workflow module")
    assert "openviking hit for a large task" in out

    trace = load_last_trace()
    assert trace["task_size"] == "large"


# ── budget clamps to the size ceiling, never above the base budget ────────


def test_small_task_budget_clamped_in_trace(runtime):
    import agent_takkub.core.brain.facade as facade

    BrainStore("proj").append_event(_rec(id="a", content="some project fact about spacing"))
    facade.build_context_for_assign("proj", "backend", "fix spacing", context_window=1_000_000)
    trace = load_last_trace()
    assert trace["task_size"] == "small"
    assert trace["budget_tokens"] <= 4000


# ── explicit override via flags={"context": ...} ───────────────────────────


def test_explicit_flags_override_classification(runtime, monkeypatch):
    import agent_takkub.core.brain.facade as facade

    monkeypatch.setenv("TAKKUB_OPENVIKING_ENABLED", "1")
    monkeypatch.setenv("TAKKUB_OPENVIKING_MODE", "read")
    from agent_takkub.core.context_sources.openviking_source import OpenVikingSource as _OVS

    monkeypatch.setattr(
        _OVS,
        "retrieve",
        lambda self, query, *, project, role, budget_tokens: [_fake_item("forced large hit")],
    )
    out = facade.build_context_for_assign(
        "proj", "backend", "change button color", flags={"context": "large"}
    )
    assert "forced large hit" in out
    trace = load_last_trace()
    assert trace["task_size"] == "large"


# ── inefficient flag: small task, budget bypassed by an oversized summary ─


@pytest.fixture
def huge_summary(runtime, monkeypatch):
    from agent_takkub.core.conversation.store import ConversationStore, conversation_id_for
    from agent_takkub.core.conversation.summary import RollingSummary, save_summary

    monkeypatch.setenv("TAKKUB_V2_CONVERSATION", "1")
    store = ConversationStore()
    conv_id = conversation_id_for("proj", "backend")
    save_summary(
        store.conversation_dir("proj", conv_id),
        RollingSummary(current_state="huge unbudgeted summary content " * 3000),
    )


def test_inefficient_flag_set_when_small_task_exceeds_15k_tokens(runtime, huge_summary):
    import agent_takkub.core.brain.facade as facade

    out = facade.build_context_for_assign("proj", "backend", "fix spacing")
    assert len(out) // 4 > 15_000

    trace = load_last_trace()
    assert trace["task_size"] == "small"
    assert trace["inefficient"] is True

    findings = build_findings()
    row = {f.name: f for f in findings}["last-trace"]
    assert row.status == "warn"
    assert "inefficient" in row.detail


def test_inefficient_flag_not_set_for_medium_task_even_over_15k(runtime, huge_summary):
    import agent_takkub.core.brain.facade as facade

    out = facade.build_context_for_assign("proj", "backend", "refactor the whole feature")
    assert len(out) // 4 > 15_000

    trace = load_last_trace()
    assert trace["task_size"] == "medium"
    assert trace["inefficient"] is False


# ── TAKKUB_CONTEXT_GATE=0: reproduces the exact pre-gate behavior ─────────


def test_gate_disabled_calls_openviking_even_for_small_task(runtime, monkeypatch):
    """The whole point of the gate — small skips reference sources. `=0`
    must undo that and go back to "OpenViking runs whenever the sidecar
    itself is on", regardless of task size."""
    import agent_takkub.core.brain.facade as facade

    monkeypatch.setenv("TAKKUB_CONTEXT_GATE", "0")
    monkeypatch.setenv("TAKKUB_OPENVIKING_ENABLED", "1")
    monkeypatch.setenv("TAKKUB_OPENVIKING_MODE", "read")
    from agent_takkub.core.context_sources.openviking_source import OpenVikingSource as _OVS

    monkeypatch.setattr(
        _OVS,
        "retrieve",
        lambda self, query, *, project, role, budget_tokens: [_fake_item("legacy behavior hit")],
    )
    out = facade.build_context_for_assign("proj", "backend", "change button color")
    assert "legacy behavior hit" in out


def test_gate_disabled_budget_matches_legacy_budget_tokens_for(runtime, monkeypatch):
    import agent_takkub.core.brain.facade as facade

    monkeypatch.setenv("TAKKUB_CONTEXT_GATE", "0")
    BrainStore("proj").append_event(_rec(id="a", content="some fact"))
    out_gated_off = facade.build_context_for_assign(
        "proj", "backend", "fix spacing", context_window=1_000_000
    )

    legacy_budget = context_builder.budget_tokens_for(1_000_000)
    legacy_text = context_builder.build_context("proj", "backend", "fix spacing", legacy_budget)
    assert out_gated_off == legacy_text
