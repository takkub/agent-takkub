"""core.brain.facade.build_context_for_assign wired to the Context Gate
(closeout #C, `03_CONTEXT_TOKEN_EFFICIENCY.md`): budgets clamp per task
size, the gate trace records task_size + the "inefficient" flag, and
`TAKKUB_CONTEXT_GATE=0` reproduces the exact pre-gate behavior.
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
    # v2-hardening C: Context Strategy falls back to the real machine's
    # persisted core-v2-settings.json (SETTINGS_HOME isn't redirected here)
    # whenever unset — pin it explicitly so these tests never depend on
    # whatever a real dev machine's settings file happens to say.
    monkeypatch.setenv("TAKKUB_CONTEXT_STRATEGY", "automatic")
    return tmp_path


def _rec(**kwargs) -> MemoryRecord:
    defaults = dict(id=kwargs.pop("id", "r"), kind=MemoryKind.PROJECT, content="x")
    defaults.update(kwargs)
    return MemoryRecord(**defaults)


# ── budget clamps to the size ceiling, never above the base budget ────────


def test_small_task_budget_clamped_in_trace(runtime):
    import agent_takkub.core.brain.facade as facade

    BrainStore("proj").append_event(_rec(id="a", content="some project fact about spacing"))
    facade.build_context_for_assign("proj", "backend", "fix spacing", context_window=1_000_000)
    trace = load_last_trace()
    assert trace["task_size"] == "small"
    assert trace["budget_tokens"] <= 4000


# ── explicit override via flags={"context": ...} ───────────────────────────


def test_explicit_flags_override_classification(runtime):
    import agent_takkub.core.brain.facade as facade

    facade.build_context_for_assign(
        "proj", "backend", "change button color", flags={"context": "large"}
    )
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


def test_gate_disabled_writes_no_trace(runtime, monkeypatch):
    """`=0` skips the gate entirely — no gate trace is written (matches the
    pre-#372 behavior now that the OpenViking-only trace path is gone)."""
    import agent_takkub.core.brain.facade as facade

    monkeypatch.setenv("TAKKUB_CONTEXT_GATE", "0")
    BrainStore("proj").append_event(_rec(id="a", content="some fact"))
    facade.build_context_for_assign("proj", "backend", "fix spacing")
    assert load_last_trace() is None


# ── v2-hardening C: default automatic/no-retry matches Wave 1 exactly ─────


def test_default_strategy_and_retry_match_classifier_v2_exactly(runtime):
    """No `--context` override, `retry_count=0` (the default), strategy
    unset (falls back to "automatic") — the trace's size/score/confidence/
    reasons must equal calling Classifier v2 directly, with no escalation
    or strategy-forcing reason appended (equality test, item #5)."""
    import agent_takkub.core.brain.facade as facade
    from agent_takkub.core.brain.task_complexity import classify_task_complexity

    task = "refactor the checkout flow across multiple files"
    facade.build_context_for_assign("proj", "backend", task)
    trace = load_last_trace()
    expected = classify_task_complexity(task, "backend", None)

    assert trace["task_size"] == expected.size
    assert trace["score"] == expected.score
    assert trace["confidence"] == expected.confidence
    assert trace["reasons"] == list(expected.reasons)
    assert trace["strategy"] == "automatic"
    assert "escalation_reason" not in trace
    assert trace.get("retry_count", 0) == 0


# ── v2-hardening C: adaptive escalation (retry_count) ──────────────────────


def test_retry_count_escalates_size_by_one_bucket_in_trace(runtime):
    import agent_takkub.core.brain.facade as facade

    facade.build_context_for_assign("proj", "backend", "fix spacing", retry_count=1)
    trace = load_last_trace()
    assert trace["initial_size"] == "small"
    assert trace["final_size"] == "medium"
    assert trace["task_size"] == "medium"
    assert trace["retry_count"] == 1
    assert trace["escalated"] is True
    assert "retry 1 of same task" in trace["escalation_reason"]


def test_retry_count_zero_writes_no_escalation_reason(runtime):
    import agent_takkub.core.brain.facade as facade

    facade.build_context_for_assign("proj", "backend", "fix spacing", retry_count=0)
    trace = load_last_trace()
    assert trace["initial_size"] == "small"
    assert trace["final_size"] == "small"
    assert "escalation_reason" not in trace


# ── v2-hardening C/G: Context Strategy (Fast/Automatic/Deep) ───────────────


def test_strategy_fast_forces_small_absent_risk(runtime, monkeypatch):
    import agent_takkub.core.brain.facade as facade

    monkeypatch.setenv("TAKKUB_CONTEXT_STRATEGY", "fast")
    facade.build_context_for_assign("proj", "backend", "refactor cross-module feature")
    trace = load_last_trace()
    assert trace["task_size"] == "small"


def test_strategy_deep_forces_large(runtime, monkeypatch):
    import agent_takkub.core.brain.facade as facade

    monkeypatch.setenv("TAKKUB_CONTEXT_STRATEGY", "deep")
    facade.build_context_for_assign("proj", "backend", "fix spacing")
    trace = load_last_trace()
    assert trace["task_size"] == "large"
    assert trace["budget_tokens"] > 4000


def test_strategy_fast_floors_at_medium_for_risk_domain(runtime, monkeypatch):
    import agent_takkub.core.brain.facade as facade

    monkeypatch.setenv("TAKKUB_CONTEXT_STRATEGY", "fast")
    facade.build_context_for_assign("proj", "backend", "fix login auth spacing")
    trace = load_last_trace()
    assert trace["task_size"] == "medium"
    assert "auth" in trace["risk_flags"]


def test_explicit_context_flag_overrides_strategy(runtime, monkeypatch):
    import agent_takkub.core.brain.facade as facade

    monkeypatch.setenv("TAKKUB_CONTEXT_STRATEGY", "deep")
    facade.build_context_for_assign("proj", "backend", "fix spacing", flags={"context": "small"})
    trace = load_last_trace()
    assert trace["task_size"] == "small"


# ── v2-hardening E: explainable skipped-source trace ────────────────────


def test_skipped_sources_recorded_for_small_task(runtime):
    import agent_takkub.core.brain.facade as facade

    facade.build_context_for_assign("proj", "backend", "fix spacing")
    trace = load_last_trace()
    skipped = {s["name"]: s["reason"] for s in trace["skipped"]}
    assert "reference_sources" in skipped
    # TAKKUB_V2_CONVERSATION=0 in this fixture's own `runtime` setup.
    assert "conversation_summary" in skipped


def test_skipped_sources_empty_for_medium_task_with_conversation_on(runtime, monkeypatch):
    import agent_takkub.core.brain.facade as facade

    monkeypatch.setenv("TAKKUB_V2_CONVERSATION", "1")
    facade.build_context_for_assign("proj", "backend", "refactor cross-module feature")
    trace = load_last_trace()
    assert trace.get("skipped", []) == []
