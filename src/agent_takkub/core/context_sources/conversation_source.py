"""Conversation rolling summary as a `ContextSource` (issue #372) — thin
adapter over `core.brain.context_builder._recent_summary_lines`, same
single-source-of-truth rationale as `brain_source.py`. One `ContextItem`
per summary line (current/in-progress/pending/next), so the merge/dedup
step downstream can budget and near-dup-collapse them individually instead
of as one opaque blob.
"""

from __future__ import annotations

from agent_takkub.core.brain import context_builder as _cb

from .base import ContextItem, estimate_tokens


class ConversationSource:
    name = "conversation"

    def retrieve(
        self, query: str, *, project: str | None, role: str, budget_tokens: int
    ) -> list[ContextItem]:
        if budget_tokens <= 0:
            return []
        lines = _cb._recent_summary_lines(project, role)
        return [
            ContextItem(
                text=line,
                tokens=estimate_tokens(line),
                source=self.name,
                provenance=f"conversation:{project or 'global'}/{role}",
                trust="reported",
                score=0.0,
            )
            for line in lines
        ]


__all__ = ["ConversationSource"]
