"""Second Brain as a `ContextSource` (issue #372) — a thin adapter over
`core.brain.context_builder`'s existing, already-tested recall pipeline
(`_recall_records`/`_fit_to_budget`/`_token_cost`/`_memory_lines`'s own
formatting convention). Deliberately does NOT reimplement recall/dedup: it
calls back into `context_builder`'s private helpers so there is exactly one
place that logic lives, and so `context_builder.build_context` (the legacy
entry point `core.brain.facade.build_context_for_assign` already calls) is
never at risk of drifting from what this source returns.
"""

from __future__ import annotations

from agent_takkub.core.brain import context_builder as _cb

from .base import ContextItem


class BrainSource:
    name = "brain"

    def retrieve(
        self, query: str, *, project: str | None, role: str, budget_tokens: int
    ) -> list[ContextItem]:
        if budget_tokens <= 0 or not (query or "").strip():
            return []
        records = _cb._fit_to_budget(
            _cb._recall_records(project, role, query, budget_tokens), budget_tokens
        )
        return [
            ContextItem(
                text=f"({r.kind.value}, {r.confidence.value}) {r.content}",
                tokens=_cb._token_cost(r.content),
                source=self.name,
                provenance=f"memory:{r.id}",
                trust=r.trust.value,
                score=0.0,
            )
            for r in records
        ]


__all__ = ["BrainSource"]
