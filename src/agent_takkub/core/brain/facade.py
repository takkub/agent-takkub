"""The Second Brain façade — `TAKKUB_V2_BRAIN` on by default since 1.0.84,
fail-open (plan §0 rules 3+4). The stable entry point `orchestrator.py`'s hooks call
into instead of reaching into `pipeline`/`retrieval`/`context_builder`
directly: `on_pane_done` (Reflection hook, Phase 7c) and
`build_context_for_assign` (Context-Injection hook, Phase 7c) join
`recall`/`submit` (Phase 7b) here.

Flag OFF: `recall()` returns `[]`, `submit()`/`on_pane_done()` are no-ops —
zero `MemoryManager`/`RetrievalEngine`/`BrainStore` construction, so a
disabled Second Brain touches no disk. Flag ON: any exception anywhere in
the path falls back to the same empty/no-op result instead of ever raising
into a caller (e.g. a spawn or `done()` path).

`build_context_for_assign` answers to a SEPARATE flag, `TAKKUB_V2_CONTEXT`
(see `flag.py`'s docstring for why) — it is fail-open the same way, but its
own flag check happens first so it stays a true no-op independent of
`TAKKUB_V2_BRAIN`.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import replace

from agent_takkub.core.models.memory import MemoryRecord, Scope
from agent_takkub.core.resilience.circuit_breaker import get_breaker

from . import context_builder, context_gate
from .candidate import MemoryCandidate
from .escalation import EscalationResult, escalate_for_retry
from .flag import context_strategy as _active_context_strategy
from .flag import v2_brain_enabled, v2_context_enabled
from .pipeline import MemoryManager, SubmitResult
from .retrieval import RetrievalEngine
from .store import BrainStore
from .task_complexity import TaskComplexity, classify_task_complexity

_log = logging.getLogger(__name__)

# Closeout #C, `03_CONTEXT_TOKEN_EFFICIENCY.md`'s "If a small task used 15k+
# context, flag it as inefficient" — checked only for `task_size == "small"`
# (medium/large tasks are meant to spend more).
_INEFFICIENT_SMALL_TOKENS = 15_000

# `recall()` reads a local disk store (`BrainStore`/`RetrievalEngine`), not a
# remote service — a single failure there is far more likely to be
# transient (a momentary file lock, one corrupt record skipped mid-scan)
# than "the dependency is down". `design_clients`' network clients open
# after 3 failures/60s (v2-hardening D/F default); this uses a much higher
# threshold and a shorter cooldown so a couple of unlucky reads never
# silences local recall — a real feature other panes reading the SAME
# project rely on — for a full minute over what was likely a blip.
_BRAIN_READ_BREAKER_NAME = "brain_read"
_BRAIN_READ_FAILURE_THRESHOLD = 10
_BRAIN_READ_COOLDOWN_S = 20.0


def recall(
    query: str, *, scope: Scope = Scope.PROJECT, project: str | None = None, limit: int = 10
) -> list[MemoryRecord]:
    if not v2_brain_enabled():
        return []
    breaker = get_breaker(
        _BRAIN_READ_BREAKER_NAME,
        failure_threshold=_BRAIN_READ_FAILURE_THRESHOLD,
        cooldown_s=_BRAIN_READ_COOLDOWN_S,
    )
    if not breaker.allow_call():
        _log.info("core.brain.facade.recall: circuit open — skipping (fail-open)")
        return []
    try:
        engine = RetrievalEngine(BrainStore(project))
        result = list(engine.recall(query, scope=scope)[:limit])
    except Exception:
        breaker.record_failure()
        _log.exception(
            "core.brain.facade.recall failed query=%r scope=%r project=%r (fail-open)",
            query,
            scope,
            project,
        )
        return []
    breaker.record_success()
    return result


def submit(candidate: MemoryCandidate, *, project: str | None = None) -> SubmitResult | None:
    if not v2_brain_enabled():
        return None
    try:
        manager = MemoryManager(BrainStore(project))
        return manager.submit_candidate(candidate)
    except Exception:
        _log.exception(
            "core.brain.facade.submit failed source=%r project=%r (fail-open)",
            candidate.source,
            project,
        )
        return None


def build_context_for_assign(
    project: str | None,
    role: str,
    task_text: str,
    *,
    context_window: int | None = None,
    file_read_supported: bool = True,
    flags: Mapping[str, object] | None = None,
    retry_count: int = 0,
    cancel_event: threading.Event | None = None,
) -> str:
    """Context-Injection hook (#309 Phase 7c) — `orchestrator._assign_
    dispatch`'s call site. Meant to run inside a timeout-bounded background
    thread (a stuck/slow recall must never delay a spawn — see the hook's
    own comment in orchestrator.py); this function itself is plain sync so
    it stays trivially unit-testable without a thread pool in the loop.

    `cancel_event` (#452): the caller sets this once its own `future.result
    (timeout=...)` has already given up on this call — its return value is
    discarded either way, so every heavy step below checks it and bails
    early rather than keep burning CPU/GIL time behind the caller's back.
    `None` (every caller before #452) reproduces the exact prior behaviour.

    Closeout #C (`context_gate.py`) sits in front of the assembly below:
    `TAKKUB_CONTEXT_GATE=0` skips it entirely and reproduces the exact
    pre-gate path byte-for-byte (unclamped budget) — `flags` (an explicit
    `{"context": "small"|"medium"|"large"}` override) is only ever
    consulted when the gate is on.

    The size decision itself comes from `task_complexity.classify_task_
    complexity` (Classifier v2, `02_CLASSIFIER_V2.md`) rather than Stage 1's
    `context_gate.classify_task_size` directly — Stage 2 still calls Stage 1
    internally (as one input signal / the empty-text fallback), so budget
    policy (`context_gate.policy_for`/`gate_budget`, keyed by `TaskSize`)
    is unaffected by the extra scoring.

    v2-hardening C layers on top of that classifier result, in order:
    1. Context Strategy (`13_SIMPLE_UX.md`) — read BEFORE touching the
       classifier's size, but skipped entirely when `flags` already carries
       an explicit `--context` override (narrowest choice always wins).
       `automatic` (the shipped default) is a no-op, so a caller that never
       sets a strategy reproduces Wave 1's classification exactly.
    2. Adaptive Escalation (`03_ADAPTIVE_ESCALATION.md`) — `retry_count`
       (from `orchestrator._assign_dispatch`'s own live-pane-reassign
       tracking) escalates by one bucket on top of whatever size step 1
       left, never de-escalates. `retry_count=0` (the default) is a no-op
       for the same reason.
    The Dynamic Token Controller's own retry input (`05_TOKEN_CONTROLLER.
    md`) is `context_gate.gate_budget`'s `retry_count` kwarg, applied AFTER
    both steps above have settled on a final size.
    """
    if not v2_context_enabled():
        return ""
    gate_on = context_gate.gate_enabled()
    complexity: TaskComplexity | None = None
    escalation: EscalationResult | None = None
    strategy = "automatic"
    try:
        base_budget = context_builder.budget_tokens_for(
            context_window, file_read_supported=file_read_supported
        )
        if gate_on:
            complexity = classify_task_complexity(task_text, role, flags)
            if not context_gate.has_explicit_override(flags):
                strategy = _active_context_strategy()
                forced_size, reason = context_gate.strategy_forced_size(
                    strategy, complexity.size, complexity.risk_flags
                )
                if reason is not None:
                    complexity = replace(
                        complexity, size=forced_size, reasons=(*complexity.reasons, reason)
                    )
            escalation = escalate_for_retry(complexity, retry_count)
            complexity = escalation.final
            budget = context_gate.gate_budget(complexity.size, base_budget, retry_count=retry_count)
        else:
            budget = base_budget
        text = context_builder.build_context(
            project, role, task_text, budget, cancel_event=cancel_event
        )
    except Exception:
        _log.exception(
            "core.brain.facade.build_context_for_assign failed role=%r project=%r (fail-open)",
            role,
            project,
        )
        return ""
    if cancel_event is not None and cancel_event.is_set():
        return ""
    if gate_on:
        _save_gate_trace(
            text,
            complexity=complexity,
            budget=budget,
            project=project,
            role=role,
            escalation=escalation,
            strategy=strategy,
        )
    return text


def _save_gate_trace(
    text: str,
    *,
    complexity: TaskComplexity | None,
    budget: int,
    project: str | None,
    role: str,
    escalation: EscalationResult | None = None,
    strategy: str = "automatic",
) -> None:
    """Persist a Context Gate trace whenever the gate is on, so `doctor`'s
    `[context]` section sees the task-size decision and total tokens for
    every gated build. Best-effort: never raises into the caller, same
    contract `trace_store.save_last_trace` already has. `complexity` carries
    Classifier v2's score/confidence/reasons/risk_flags through to the
    Explainable Trace (`07_EXPLAINABLE_TRACE.md`) — `trace_store` treats
    them as optional so a pre-Stage-2 reader (or `TAKKUB_CONTEXT_GATE=0`)
    still sees the exact same payload shape as before. `escalation`/
    `strategy` are v2-hardening C's own additions, same optional shape."""
    try:
        from agent_takkub.core.context_sources.base import estimate_tokens
        from agent_takkub.core.context_sources.trace_store import save_last_trace

        task_size = complexity.size if complexity is not None else None
        total_tokens = estimate_tokens(text) if text else 0
        trace = context_builder.ContextTrace(
            mode=f"gated:{task_size}",
            sources=(),
            total_tokens=total_tokens,
            budget_tokens=budget,
            dedup_count=0,
            latency_ms=0.0,
        )
        inefficient = task_size == "small" and total_tokens > _INEFFICIENT_SMALL_TOKENS
        skipped = _skipped_sources(complexity) if complexity is not None else None
        save_last_trace(
            trace,
            project=project,
            role=role,
            task_size=task_size,
            inefficient=inefficient,
            complexity=complexity,
            escalation=escalation,
            strategy=strategy,
            skipped=skipped,
        )
    except Exception:
        _log.debug("core.brain.facade: gate trace save failed (best-effort)", exc_info=True)


def _skipped_sources(complexity: TaskComplexity) -> list[dict[str, str]]:
    """Explainable Trace (`07_EXPLAINABLE_TRACE.md`) — every source this
    build did NOT call, and why. `context_gate.skipped_sources` covers the
    size-gated reference-source policy; conversation summary has its own
    separate flag (`core.conversation.flag`) that `context_builder.py`
    already checks internally, so this is the one other place worth
    surfacing it from without `context_gate.py` taking on a dependency it
    doesn't otherwise need."""
    skipped = context_gate.skipped_sources(complexity.size)
    try:
        from agent_takkub.core.conversation.flag import v2_conversation_enabled

        if not v2_conversation_enabled():
            skipped.append(
                {
                    "name": "conversation_summary",
                    "reason": "conversation V2 disabled (TAKKUB_V2_CONVERSATION=0)",
                }
            )
    except Exception:
        pass
    return skipped


def on_pane_done(
    project: str | None,
    role: str,
    *,
    note: str,
    digest_facts: object | None = None,
    failed: bool = False,
    task_id: str | None = None,
) -> None:
    """Reflection hook (#309 Phase 7c) — `orchestrator.done()`/`subagent_
    done()`'s call site (same `TAKKUB_V2_BRAIN` flag that already gates
    `recall`/`submit`; this is just another write path into the same Second
    Brain, not a separate feature). `digest_facts` is a `digest_facts.
    DigestFacts` — typed loosely here so this module never has to import
    that top-level module at load time (only inside the try, and only when
    a caller actually passed one)."""
    if not v2_brain_enabled():
        return
    try:
        from .sources.reflection_source import decisions_from_note, from_done_note

        note_candidate = from_done_note(
            note, project=project, role=role, failed=failed, task_id=task_id
        )
        if note_candidate is not None:
            submit(note_candidate, project=project)

        for decision_candidate in decisions_from_note(
            note, project=project, role=role, task_id=task_id
        ):
            submit(decision_candidate, project=project)

        if digest_facts is not None and project is not None:
            from .sources.digest_facts_source import from_digest_facts

            submit(from_digest_facts(digest_facts, project=project), project=project)
    except Exception:
        _log.exception(
            "core.brain.facade.on_pane_done failed role=%r project=%r (fail-open)", role, project
        )


__all__ = ["build_context_for_assign", "on_pane_done", "recall", "submit"]
