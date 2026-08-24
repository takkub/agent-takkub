"""Context Builder (plan §19, epic #309 Phase 7c) — assembles a short
"## Context (Takkub brain)" text block from the Second Brain
(`RetrievalEngine`) plus the Conversation rolling summary, for the
Context-Injection hook in `orchestrator._assign_dispatch`.

Pure: no orchestrator/PyQt/UI import. Reads directly through
`RetrievalEngine`/`BrainStore` (not `facade.recall`) — deliberately: this
module answers to its OWN flag (`TAKKUB_V2_CONTEXT`, checked by
`facade.build_context_for_assign` before calling in here), so it must not
also depend on the Second Brain's separate write-path flag
(`TAKKUB_V2_BRAIN`, which gates `facade.recall`/`submit`). Going through
`facade` here would also be a circular import: `facade.build_context_for_
assign` is this module's own caller.

Both reads are bounded: one project's `BrainStore` is a single JSONL file
(never a directory walk — see `store.py`'s docstring), and the conversation
read is a single `summary.json`, never the raw message log.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_takkub.bm25_search import tokenize
from agent_takkub.core.models.memory import MemoryRecord, Scope

from .retrieval import RetrievalEngine
from .store import BrainStore

_HEADER = "## Context (Takkub brain)"

# ponytail: 1 token ≈ 4 chars — the same rough estimate `retrieval.py`
# already uses for its own budget trim (see that module's docstring). Fine
# for a soft prompt-injection budget; upgrade to a real tokenizer if a
# provider ever needs a hard guarantee.
_CHARS_PER_TOKEN = 4

# token_meter._DEFAULT_LIMIT's own fallback for "context window unknown".
_DEFAULT_CONTEXT_WINDOW = 200_000
_BUDGET_FRACTION = 0.12
_BUDGET_FLOOR = 400
_BUDGET_CEILING = 6000
# A provider whose CLI has no structured file-read tool (#273,
# `ProviderSpec.supports_agent_file_read=False`) can only receive context
# pasted inline, never handed off via the task-file pointer — halve the
# budget for it, with a smaller floor of its own.
_NO_FILE_READ_FLOOR = 200

# Record-count caps applied on top of RetrievalEngine's own token-budget
# trim — keeps the merge below bounded even if both queries independently
# hit the token budget.
_RECALL_LIMIT_SCOPED = 8
_RECALL_LIMIT_GLOBAL = 4


def budget_tokens_for(context_window: int | None, *, file_read_supported: bool = True) -> int:
    """Default per-provider/model budget: ~12% of the context window,
    clamped to a sane floor/ceiling. `context_window=None` (unknown model)
    falls back to the same default context size `token_meter.py` uses."""
    window = context_window if context_window and context_window > 0 else _DEFAULT_CONTEXT_WINDOW
    budget = int(window * _BUDGET_FRACTION)
    budget = max(_BUDGET_FLOOR, min(_BUDGET_CEILING, budget))
    if not file_read_supported:
        budget = max(_NO_FILE_READ_FLOOR, budget // 2)
    return budget


def _token_cost(content: str) -> int:
    return max(1, len(content) // _CHARS_PER_TOKEN)


def _fit_to_budget(records: list[MemoryRecord], budget_tokens: int) -> list[MemoryRecord]:
    """Trims an already-ranked/merged list to fit `budget_tokens`. The top
    record is always kept even if it alone exceeds the budget — mirrors
    `retrieval.py`'s own `_fit_budget` so a non-empty result never comes
    back empty just because the single best match is long."""
    out: list[MemoryRecord] = []
    used = 0
    for r in records:
        cost = _token_cost(r.content)
        if out and used + cost > budget_tokens:
            break
        out.append(r)
        used += cost
    return out


def _base_role(name: str | None) -> str | None:
    """`"qa#2"` -> `"qa"`; `None` stays `None`. Same `#`-suffix convention
    `config._safe_name`/`pipeline_executor` already parse for shard panes."""
    return name.partition("#")[0] if name else None


def _recall_records(
    project: str | None, role: str, task_text: str, budget_tokens: int
) -> list[MemoryRecord]:
    scoped = RetrievalEngine(BrainStore(project)).recall(
        task_text, scope=Scope.PROJECT, budget_tokens=budget_tokens
    )[:_RECALL_LIMIT_SCOPED]
    # Role-scoping: drop another role's own AGENT-scoped working memory, and
    # ONLY that. Keyed on the scope, not on `agent_id`: a PROJECT-scoped
    # record carries an `agent_id` too (`digest_facts_source.from_digest_facts`
    # stamps the reporting role on the cockpit-measured digest), so the older
    # `agent_id in (None, role)` test silently threw away every other role's
    # project-level findings — the highest-trust records in the store — and a
    # frontend pane could never see what qa or devops had measured.
    #
    # Shard instances share their base role's memory (`qa#2` reads `qa`'s):
    # they are the same teammate, just parallel panes.
    #
    # When project is None, the "scoped" bucket IS the global bucket
    # (`BrainStore(None)`'s `_global` file) — the global query below can then
    # reintroduce an other-role AGENT record this filter just dropped;
    # accepted as-is, the global bucket is shared-by-design and this only
    # matters for the rare pane with no active project.
    base_role = _base_role(role)
    scoped = [
        r
        for r in scoped
        if r.scope is not Scope.AGENT or _base_role(r.agent_id) in (None, base_role)
    ]

    global_recs = RetrievalEngine(BrainStore(None)).recall(
        task_text, scope=Scope.GLOBAL, budget_tokens=budget_tokens
    )[:_RECALL_LIMIT_GLOBAL]

    seen = {r.id for r in scoped}
    merged = scoped + [r for r in global_recs if r.id not in seen]
    return _drop_near_duplicates(merged)


# Token-set containment (|A n B| / min(|A|,|B|)) above which two records are
# the SAME event written twice, not two related facts. Measured over a real
# 34-record store: every agent-note/digest pair from one `done()` scored
# 0.73-1.00, while the closest genuinely-distinct pair (two separate rebuilds
# of the same service) scored 0.61 — so the boundary sits in a real gap, and
# deliberately above `pipeline._SEMANTIC_MATCH_THRESHOLD` (0.6), which would
# have collapsed those distinct events.
_NEAR_DUP_CONTAINMENT = 0.7


def _drop_near_duplicates(records: list[MemoryRecord]) -> list[MemoryRecord]:
    """Collapse the two records one `done()` writes for the same event (#332),
    keeping whichever carries more distinct tokens.

    By design `facade.on_pane_done` submits the agent's own headline
    (`reflection_source`, scope=AGENT, trust=AGENT_REPORTED) AND the cockpit-
    measured digest (`digest_facts_source`, scope=PROJECT), whose
    `_summarize` appends that same headline after the git facts.
    `pipeline`'s dedup can never collapse them: it only compares inside one
    `(kind, scope, project_id, agent_id)` bucket and these two differ in
    scope. So the read side does it, where it also cleans up every pair
    already on disk.

    Containment on TOKEN SETS rather than plain substring, because the digest
    truncates a long headline with an ellipsis — the note is then not a
    substring of the digest, but its tokens still overlap almost completely.

    O(n^2) over at most `_RECALL_LIMIT_SCOPED + _RECALL_LIMIT_GLOBAL` = 12
    records; ranked order is preserved, the survivor of a pair inheriting the
    earlier of the two ranks.
    """
    kept: list[MemoryRecord] = []
    kept_tokens: list[set[str]] = []
    for record in records:
        tokens = set(tokenize(record.content))
        if not tokens:
            continue
        merged_into_kept = False
        for i, other in enumerate(kept_tokens):
            containment = len(tokens & other) / min(len(tokens), len(other))
            if containment < _NEAR_DUP_CONTAINMENT:
                continue
            if len(tokens) > len(other):
                kept[i], kept_tokens[i] = record, tokens
            merged_into_kept = True
            break
        if not merged_into_kept:
            kept.append(record)
            kept_tokens.append(tokens)
    return kept


def _memory_lines(records: list[MemoryRecord]) -> list[str]:
    return [f"- ({r.kind.value}, {r.confidence.value}) {r.content}" for r in records]


def _recent_summary_lines(project: str | None, role: str) -> list[str]:
    from agent_takkub.core.conversation.flag import v2_conversation_enabled

    if not v2_conversation_enabled():
        return []
    from agent_takkub.core.conversation.store import ConversationStore, conversation_id_for
    from agent_takkub.core.conversation.summary import load_summary

    store = ConversationStore()
    conv_dir = store.conversation_dir(project, conversation_id_for(project, role))
    summary = load_summary(conv_dir)

    lines: list[str] = []
    if summary.current_state:
        lines.append(f"- current: {summary.current_state}")
    for item in summary.in_progress[:3]:
        lines.append(f"- in progress: {item}")
    for item in summary.pending[:3]:
        lines.append(f"- pending: {item}")
    if summary.next_action:
        lines.append(f"- next: {summary.next_action}")
    return lines


def build_context(project: str | None, role: str, task_text: str, budget_tokens: int) -> str:
    """Pure text assembly. Empty input (`budget_tokens<=0` or a blank
    `task_text`) or nothing found yields `""` — never raises for those
    caller-controllable reasons. A genuine I/O failure inside a store read
    propagates to the caller (`facade.build_context_for_assign` is the
    fail-open boundary, same layering as `pipeline.py`/`retrieval.py`
    versus `facade.recall`/`submit`)."""
    if budget_tokens <= 0 or not (task_text or "").strip():
        return ""

    records = _fit_to_budget(
        _recall_records(project, role, task_text, budget_tokens), budget_tokens
    )
    memory_lines = _memory_lines(records)

    used = sum(_token_cost(r.content) for r in records)
    remaining = budget_tokens - used
    summary_lines = _recent_summary_lines(project, role) if remaining > 0 else []

    if not memory_lines and not summary_lines:
        return ""

    parts = [_HEADER]
    if memory_lines:
        parts.append("### Relevant memory")
        parts.extend(memory_lines)
    if summary_lines:
        parts.append("### Recent summary")
        parts.extend(summary_lines)
    return "\n".join(parts)


# ── OpenViking hybrid merge (issue #372, GAP-013/017/018) ──────────────────
#
# Everything below is ADDITIVE and never called by `build_context` above,
# which stays exactly as it was (the legacy Brain+Conversation assembly
# `facade.build_context_for_assign` already calls first). `facade` feeds
# that result through `merge_openviking_traced` as a SEPARATE step — when
# OpenViking is disabled (default: `TAKKUB_OPENVIKING_ENABLED=0`) that step
# returns its input completely unchanged before touching anything else, so
# the pre-#372 output stays byte-for-byte identical by construction rather
# than by re-testing the whole assembly path again.
#
# This is also where "Context Builder = ONLY merge/budget/dedup/provenance
# policy" (`15_CONTEXT_SOURCE_ARCHITECTURE.md`) actually lives: the
# RETRIEVAL itself is `core.context_sources.*`'s job (imported lazily below
# to avoid a module-load-time dependency a disabled feature shouldn't pay
# for).


@dataclass(frozen=True, slots=True)
class SourceTrace:
    name: str
    count: int
    unit: str
    tokens: int


@dataclass(frozen=True, slots=True)
class ContextTrace:
    """Doctor/debug-facing summary of one `merge_openviking_traced` call —
    shape matches `15_CONTEXT_SOURCE_ARCHITECTURE.md`'s suggested trace
    format (`.render()`)."""

    mode: str
    sources: tuple[SourceTrace, ...]
    total_tokens: int
    budget_tokens: int
    dedup_count: int = 0
    latency_ms: float = 0.0

    def render(self) -> str:
        lines = ["Context:"]
        for s in self.sources:
            lines.append(f"- {s.name}: {s.count} {s.unit} / {s.tokens} tokens")
        lines.append(f"Total: {self.total_tokens} / budget {self.budget_tokens}")
        return "\n".join(lines)


# `16_CONTEXT_MERGE_POLICY.md` priority ordering, expressed as a trust
# weight for the injected-section sort: curated local Obsidian docs first,
# then external OpenViking resources — "exact project scope wins over
# global" / "explicit/user-confirmed trust outranks inferred".
_TRUST_WEIGHT: dict[str, int] = {"curated": 3, "distilled": 2, "external": 1, "auto": 0}


def _trust_weight(trust: str) -> int:
    return _TRUST_WEIGHT.get(trust, 0)


def merge_openviking(
    base_text: str,
    *,
    project: str | None,
    role: str,
    task_text: str,
    budget_tokens: int,
) -> str:
    """`base_text` is `build_context(...)`'s own output — this only ever
    APPENDS to it, never rewrites it. NOT itself a fail-open boundary: an
    error talking to the sidecar or scanning the vault propagates to the
    caller — `facade.build_context_for_assign` is where that's caught, so a
    bug HERE falls back to the `base_text` already computed rather than
    discarding a working Brain/Conversation build."""
    text, _trace = merge_openviking_traced(
        base_text, project=project, role=role, task_text=task_text, budget_tokens=budget_tokens
    )
    return text


def merge_openviking_traced(
    base_text: str,
    *,
    project: str | None,
    role: str,
    task_text: str,
    budget_tokens: int,
) -> tuple[str, ContextTrace | None]:
    """Same as `merge_openviking`, plus the `ContextTrace` `facade` persists
    for `doctor`/debug (`trace_store.save_last_trace`). Returns
    `(base_text, None)` untouched whenever OpenViking is disabled — the
    `None` trace is the caller's signal that nothing ran, not an error."""
    import time as _time

    from agent_takkub.core.context_sources import openviking_adapter
    from agent_takkub.core.context_sources.base import collapse_near_duplicates
    from agent_takkub.core.context_sources.openviking_source import OpenVikingSource
    from agent_takkub.core.context_sources.resource_source import ResourceSource

    if not openviking_adapter.enabled():
        return base_text, None

    started = _time.monotonic()
    ov_mode = openviking_adapter.mode()
    base_tokens = _token_cost(base_text) if base_text else 0
    remaining = max(0, budget_tokens - base_tokens)

    ov_items = OpenVikingSource().retrieve(
        task_text, project=project, role=role, budget_tokens=remaining
    )
    resource_items = (
        ResourceSource().retrieve(task_text, project=project, role=role, budget_tokens=remaining)
        if ov_mode == "hybrid"
        else []
    )

    pool, dedup_count = collapse_near_duplicates(resource_items + ov_items)
    pool.sort(key=lambda item: (_trust_weight(item.trust), item.score), reverse=True)

    injected = []
    used = 0
    for item in pool:
        if injected and used + item.tokens > remaining:
            continue
        injected.append(item)
        used += item.tokens

    sources = [
        SourceTrace("OpenViking", len(ov_items), "resources", sum(i.tokens for i in ov_items))
    ]
    if ov_mode == "hybrid":
        sources.append(
            SourceTrace(
                "Resource", len(resource_items), "docs", sum(i.tokens for i in resource_items)
            )
        )

    trace = ContextTrace(
        mode=ov_mode,
        sources=tuple(sources),
        total_tokens=base_tokens + sum(i.tokens for i in injected),
        budget_tokens=budget_tokens,
        dedup_count=dedup_count,
        latency_ms=(_time.monotonic() - started) * 1000,
    )

    # shadow: retrieve + trace, never inject (`14_OPENVIKING_INTEGRATION.md`
    # rollout stage B). Also the natural fallback when nothing was found.
    if ov_mode == "shadow" or not injected:
        return base_text, trace

    parts = [base_text] if base_text else [_HEADER]
    parts.append("### Knowledge (OpenViking)")
    parts.extend(f"- ({item.trust}) {item.text} [source: {item.provenance}]" for item in injected)
    return "\n".join(parts), trace


__all__ = [
    "ContextTrace",
    "SourceTrace",
    "budget_tokens_for",
    "build_context",
    "merge_openviking",
    "merge_openviking_traced",
]
