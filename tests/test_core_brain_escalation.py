"""core.brain.escalation — Adaptive Escalation (v2-hardening C, `docs/plans/
v2-hardening-2026-08-24/03_ADAPTIVE_ESCALATION.md`): retry-count bumping and
the one-bucket-up, never-de-escalate size rule."""

from __future__ import annotations

import pytest

from agent_takkub.core.brain import escalation
from agent_takkub.core.brain.task_complexity import TaskComplexity

# ── next_retry_count ────────────────────────────────────────────────────


def test_retry_count_bumps_when_pane_already_running():
    assert escalation.next_retry_count(0, pane_is_running=True) == 1
    assert escalation.next_retry_count(1, pane_is_running=True) == 2
    assert escalation.next_retry_count(5, pane_is_running=True) == 6


def test_retry_count_resets_on_fresh_spawn():
    assert escalation.next_retry_count(3, pane_is_running=False) == 0
    assert escalation.next_retry_count(0, pane_is_running=False) == 0


# ── escalate_for_retry ──────────────────────────────────────────────────


def _complexity(size: str, **kw) -> TaskComplexity:
    defaults = dict(size=size, score=0, confidence=0.5, reasons=())
    defaults.update(kw)
    return TaskComplexity(**defaults)


def test_retry_zero_is_a_pass_through():
    original = _complexity("small")
    result = escalation.escalate_for_retry(original, 0)
    assert result.initial is original
    assert result.final is original
    assert result.escalated is False
    assert result.reason is None


@pytest.mark.parametrize(
    "start,expected",
    [("small", "medium"), ("medium", "large")],
)
def test_retry_escalates_exactly_one_bucket(start, expected):
    original = _complexity(start)
    result = escalation.escalate_for_retry(original, 1)
    assert result.initial.size == start
    assert result.final.size == expected
    assert result.escalated is True
    assert result.retry_count == 1
    assert f"{start} -> {expected}" in result.reason
    assert result.reason in result.final.reasons


def test_multiple_retries_do_not_compound_past_one_bucket():
    original = _complexity("small")
    result = escalation.escalate_for_retry(original, 4)
    # still exactly one bucket up, not 4 buckets — there's no bucket past
    # "large" anyway, but "small" must land on "medium", not skip further.
    assert result.final.size == "medium"
    assert result.retry_count == 4


def test_large_has_no_further_bucket_but_still_traces():
    original = _complexity("large")
    result = escalation.escalate_for_retry(original, 2)
    assert result.final.size == "large"
    assert result.escalated is False
    assert "already at max size" in result.reason
    assert result.reason in result.final.reasons


def test_never_de_escalates():
    for size in ("small", "medium", "large"):
        original = _complexity(size)
        result = escalation.escalate_for_retry(original, 1)
        rank = {"small": 0, "medium": 1, "large": 2}
        assert rank[result.final.size] >= rank[original.size]


def test_escalation_preserves_risk_flags():
    original = _complexity("medium", risk_flags=("auth",))
    result = escalation.escalate_for_retry(original, 1)
    assert result.final.risk_flags == ("auth",)
