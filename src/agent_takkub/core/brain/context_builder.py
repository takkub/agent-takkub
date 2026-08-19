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


def _recall_records(
    project: str | None, role: str, task_text: str, budget_tokens: int
) -> list[MemoryRecord]:
    scoped = RetrievalEngine(BrainStore(project)).recall(
        task_text, scope=Scope.PROJECT, budget_tokens=budget_tokens
    )[:_RECALL_LIMIT_SCOPED]
    # Role-scoping: drop another role's own AGENT-scoped working memory —
    # PROJECT/TASK/SESSION/GLOBAL-scoped records (agent_id=None) stay
    # shared. When project is None, the "scoped" bucket IS the global
    # bucket (`BrainStore(None)`'s `_global` file) — the global query below
    # can then reintroduce an other-role AGENT record this filter just
    # dropped; accepted as-is, the global bucket is shared-by-design and
    # this only matters for the rare pane with no active project.
    scoped = [r for r in scoped if r.agent_id in (None, role)]

    global_recs = RetrievalEngine(BrainStore(None)).recall(
        task_text, scope=Scope.GLOBAL, budget_tokens=budget_tokens
    )[:_RECALL_LIMIT_GLOBAL]

    seen = {r.id for r in scoped}
    return scoped + [r for r in global_recs if r.id not in seen]


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


__all__ = ["budget_tokens_for", "build_context"]
