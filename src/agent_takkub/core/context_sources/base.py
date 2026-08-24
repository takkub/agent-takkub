"""Context Source vocabulary (issue #372, GAP-013/017/018) — `15_CONTEXT_
SOURCE_ARCHITECTURE.md`. One shared shape every source (`brain_source`,
`conversation_source`, `resource_source`, `openviking_source`) returns, so
`core.brain.context_builder`'s merge step never has to know which source a
result came from beyond the fields on `ContextItem` itself.

Pure/stdlib-only — no filesystem I/O, no config import beyond what a single
helper lazily reaches for (`bm25_search.tokenize`, itself pure) — safe for
every source module to depend on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# Same rough "1 token ~= 4 chars" estimate `core.brain.context_builder`
# already documents and uses for its own budget trim — kept in sync
# deliberately so a mixed Brain+OpenViking budget adds up consistently
# rather than each source using its own conversion.
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


@dataclass(frozen=True, slots=True)
class ContextItem:
    """One retrievable unit of context, already-scored and ready to be
    merged/budgeted by the Context Builder. `provenance` must always be
    something a human can trace back to the origin (a memory id, a vault-
    relative path, a `viking://` URI) — `16_CONTEXT_MERGE_POLICY.md`:
    "resources must cite provenance"."""

    text: str
    tokens: int
    source: str  # "brain" | "conversation" | "resource" | "openviking"
    provenance: str
    trust: str
    score: float = 0.0


@runtime_checkable
class ContextSource(Protocol):
    name: str

    def retrieve(
        self, query: str, *, project: str | None, role: str, budget_tokens: int
    ) -> list[ContextItem]: ...


# Containment threshold above which two items are treated as the same fact
# stated twice rather than two related-but-distinct ones — same boundary
# `core.brain.context_builder._NEAR_DUP_CONTAINMENT` uses for Brain-only
# dedup; kept identical here so a Brain memory and an OpenViking resource
# that restate the same fact collapse the same way a Brain/Brain pair does.
_NEAR_DUP_CONTAINMENT = 0.7


def _tokens_for_dedup(text: str) -> set[str]:
    from agent_takkub.bm25_search import tokenize

    return set(tokenize(text))


def collapse_near_duplicates(items: list[ContextItem]) -> tuple[list[ContextItem], int]:
    """Token-set containment collapse across an arbitrary mix of sources —
    the generic sibling of `context_builder._drop_near_duplicates` (which
    stays Brain-only and untouched). Order is preserved; the survivor of a
    collapsed pair is whichever carries more distinct tokens. Returns
    `(kept, dropped_count)` — the count feeds `ContextTrace.dedup_count`.
    """
    kept: list[ContextItem] = []
    kept_tokens: list[set[str]] = []
    dropped = 0
    for item in items:
        tokens = _tokens_for_dedup(item.text)
        if not tokens:
            kept.append(item)
            kept_tokens.append(tokens)
            continue
        merged = False
        for i, other in enumerate(kept_tokens):
            if not other:
                continue
            containment = len(tokens & other) / min(len(tokens), len(other))
            if containment < _NEAR_DUP_CONTAINMENT:
                continue
            if len(tokens) > len(other):
                kept[i], kept_tokens[i] = item, tokens
            merged = True
            dropped += 1
            break
        if not merged:
            kept.append(item)
            kept_tokens.append(tokens)
    return kept, dropped


__all__ = ["ContextItem", "ContextSource", "collapse_near_duplicates", "estimate_tokens"]
