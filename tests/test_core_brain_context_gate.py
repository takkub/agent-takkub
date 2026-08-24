"""core.brain.context_gate — closeout #C task-size classification + per-size
source/budget policy (`03_CONTEXT_TOKEN_EFFICIENCY.md`). Pure/stdlib-only,
no fixtures needed beyond `monkeypatch.delenv`/`setenv` for the gate flag.
"""

from __future__ import annotations

import pytest

from agent_takkub.core.brain import context_gate

# ── classify_task_size: the plan doc's own examples ───────────────────────


@pytest.mark.parametrize(
    "task_text",
    ["change button color", "rename field", "fix spacing"],
)
def test_classify_small_examples(task_text):
    assert context_gate.classify_task_size(task_text) == "small"


@pytest.mark.parametrize(
    "task_text",
    ["refactor feature", "fix cross-file bug"],
)
def test_classify_medium_examples(task_text):
    assert context_gate.classify_task_size(task_text) == "medium"


@pytest.mark.parametrize(
    "task_text",
    ["new workflow", "review the system architecture", "add a new UI section"],
)
def test_classify_large_examples(task_text):
    assert context_gate.classify_task_size(task_text) == "large"


def test_classify_empty_text_is_small():
    assert context_gate.classify_task_size("") == "small"
    assert context_gate.classify_task_size("   ") == "small"


def test_classify_length_fallback_when_no_keyword_matches():
    short = "do the thing"
    long_medium = "please double check the whole onboarding flow works end to end " * 2
    long_large = "please double check the whole onboarding flow works end to end " * 6
    assert context_gate.classify_task_size(short) == "small"
    assert context_gate.classify_task_size(long_medium) == "medium"
    assert context_gate.classify_task_size(long_large) == "large"


def test_classify_ambiguous_overlap_resolves_to_larger_bucket():
    # contains a small-signal word ("rename") AND a medium-signal one
    # ("multiple files") — medium must win, not small.
    assert context_gate.classify_task_size("rename this field across multiple files") == "medium"


# ── explicit override via flags={"context": ...} ───────────────────────────


def test_explicit_override_wins_over_heuristic():
    assert (
        context_gate.classify_task_size("change button color", flags={"context": "large"})
        == "large"
    )


def test_explicit_override_invalid_value_falls_back_to_heuristic():
    assert (
        context_gate.classify_task_size("change button color", flags={"context": "huge"}) == "small"
    )


def test_explicit_override_missing_key_falls_back_to_heuristic():
    assert context_gate.classify_task_size("change button color", flags={}) == "small"
    assert context_gate.classify_task_size("change button color", flags=None) == "small"


# ── policy_for / gate_budget ────────────────────────────────────────────


def test_policy_for_small_disallows_reference_sources():
    policy = context_gate.policy_for("small")
    assert policy.allow_reference_sources is False


@pytest.mark.parametrize("size", ["medium", "large"])
def test_policy_for_medium_large_allow_reference_sources(size):
    assert context_gate.policy_for(size).allow_reference_sources is True


def test_gate_budget_clamps_down_to_size_ceiling():
    # a big model window gives a big base budget — small must still clamp.
    assert context_gate.gate_budget("small", 6000) == 4000


def test_gate_budget_never_exceeds_base_budget():
    # a tiny model window's base budget is the real ceiling even for large.
    assert context_gate.gate_budget("large", 500) == 500


def test_gate_budget_zero_or_negative_passthrough():
    assert context_gate.gate_budget("small", 0) == 0
    assert context_gate.gate_budget("medium", -1) == -1


# ── gate_enabled() env flag ─────────────────────────────────────────────


def test_gate_enabled_by_default(monkeypatch):
    monkeypatch.delenv("TAKKUB_CONTEXT_GATE", raising=False)
    assert context_gate.gate_enabled() is True


def test_gate_disabled_via_env_zero(monkeypatch):
    monkeypatch.setenv("TAKKUB_CONTEXT_GATE", "0")
    assert context_gate.gate_enabled() is False


def test_gate_enabled_any_other_value(monkeypatch):
    monkeypatch.setenv("TAKKUB_CONTEXT_GATE", "1")
    assert context_gate.gate_enabled() is True
