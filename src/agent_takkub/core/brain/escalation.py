"""Adaptive Escalation (v2-hardening C, `docs/plans/v2-hardening-2026-08-24/
03_ADAPTIVE_ESCALATION.md`) — the assign-time/re-assign-time half of "never
freeze complexity at assign time".

Agent-restart-level escalation (re-classifying mid-session while a pane is
already working) is out of scope per the phase0 audit — the pane is already
running by then and nothing calls back into this. What IS real at the
points this codebase actually has: a role getting reassigned to the SAME
live pane (`orchestrator._assign_dispatch`'s own `pane_is_running` check)
before its previous task ever closed out is the fix-loop/re-assign signal
`03_ADAPTIVE_ESCALATION.md` describes ("impacted files exceed prediction",
"tests expose cross-module effects", etc. all cash out, in this codebase,
as "Lead sent this role another task without closing the pane first").

Pure/stdlib-only, no I/O — same contract `context_gate.py` and
`task_complexity.py` both keep for themselves. `retry_count` is supplied by
the caller (`orchestrator._assign_dispatch` tracks it per role+project key
across live-pane reassigns via `next_retry_count`); this module only turns
a count into an escalation decision.

Rules (`03_ADAPTIVE_ESCALATION.md`):
- any retry (`retry_count >= 1`) escalates by exactly one bucket
  (small->medium->large) — never more, never less, regardless of how many
  retries have accumulated; the escalated bucket is the floor for every
  later retry, not a per-retry compounding step.
- never de-escalate: the result's size is always >= the input's size.
- large has no further bucket to escalate INTO, but the retry is still a
  real signal worth tracing.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .task_complexity import _RANK_SIZE, _SIZE_RANK, TaskComplexity


@dataclass(frozen=True, slots=True)
class EscalationResult:
    """`initial`/`final` are both the full `TaskComplexity` — the
    Explainable Trace (`07_EXPLAINABLE_TRACE.md`) wants to show initial and
    final complexity side by side, not just the winning size. `escalated`
    is a plain bool so a caller never has to diff `.size` on the two itself."""

    initial: TaskComplexity
    final: TaskComplexity
    retry_count: int
    escalated: bool
    reason: str | None = None


def next_retry_count(previous: int, *, pane_is_running: bool) -> int:
    """The fix-loop/re-assign signal: a NEW task dispatched to a role whose
    pane is still alive (not a fresh spawn) is being reassigned before its
    previous task ever closed out — exactly what `03_ADAPTIVE_ESCALATION.md`
    calls a retry. A fresh spawn (`pane_is_running=False`) starts the count
    over at 0, same as a genuinely new task would."""
    return previous + 1 if pane_is_running else 0


def escalate_for_retry(complexity: TaskComplexity, retry_count: int) -> EscalationResult:
    """`retry_count<=0` is a plain pass-through (no escalation, no reason) —
    the first assign of any task stays byte-identical to the pre-escalation
    (Wave 1) classification."""
    if retry_count <= 0:
        return EscalationResult(complexity, complexity, retry_count, escalated=False)

    original_rank = _SIZE_RANK[complexity.size]
    new_rank = min(original_rank + 1, _SIZE_RANK["large"])
    if new_rank <= original_rank:
        reason = f"retry {retry_count} of same task — already at max size (large)"
        final = replace(complexity, reasons=(*complexity.reasons, reason))
        return EscalationResult(complexity, final, retry_count, escalated=False, reason=reason)

    new_size = _RANK_SIZE[new_rank]
    reason = f"retry {retry_count} of same task — escalated {complexity.size} -> {new_size}"
    final = replace(complexity, size=new_size, reasons=(*complexity.reasons, reason))
    return EscalationResult(complexity, final, retry_count, escalated=True, reason=reason)


__all__ = ["EscalationResult", "escalate_for_retry", "next_retry_count"]
