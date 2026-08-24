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


# `02_OPENVIKING_STRICT_SCOPE.md` — the GLOBAL sentinel matches the one
# `vault_mirror._MOC_PROJECT_ID` already uses for cross-project MOC pages,
# and `indexing.py`'s own pre-existing `project or "_global"` state-file
# fallback; kept here as the single canonical spelling every context
# source can import instead of re-inventing the string. `WORKSPACE_ID` is
# the single-tenant placeholder the V2 schema reserves a field for
# (`V2_IMPLEMENTATION_PLAN.md`: "ทุก persistent model มี user_id/
# workspace_id/project_id ... implement เป็น single-user") — every
# indexed resource stamps it so the field is real from day one even
# though this cockpit only ever runs as one workspace today.
GLOBAL_PROJECT_ID = "_global"
WORKSPACE_ID = "local"

# Trust vocabulary every writer in this codebase already uses
# (`obsidian_metadata.TRUST_*` plus `openviking_source`'s own "external")
# — kept as plain strings here (not an import of that module, which is
# not stdlib-pure) so `apply_scope_and_trust` can fail closed on a value
# outside this set without pulling in a heavier dependency.
_VALID_TRUST = frozenset({"auto", "distilled", "curated", "external"})

_MAX_REJECT_EXAMPLES = 3


@dataclass(frozen=True, slots=True)
class ContextItem:
    """One retrievable unit of context, already-scored and ready to be
    merged/budgeted by the Context Builder. `provenance` must always be
    something a human can trace back to the origin (a memory id, a vault-
    relative path, a `viking://` URI) — `16_CONTEXT_MERGE_POLICY.md`:
    "resources must cite provenance".

    `project_id`/`workspace_id` (issue #372 follow-up, `02_OPENVIKING_
    STRICT_SCOPE.md`) are the scope claim `apply_scope_and_trust` checks
    before injection — `None` on `brain`/`conversation` items (which never
    cross the OpenViking sidecar boundary and are never passed through
    that gate) is not a claim of any kind, just "not applicable"."""

    text: str
    tokens: int
    source: str  # "brain" | "conversation" | "resource" | "openviking"
    provenance: str
    trust: str
    score: float = 0.0
    project_id: str | None = None
    workspace_id: str | None = None


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


@dataclass(frozen=True, slots=True)
class ScopeRejects:
    """Tally from one `apply_scope_and_trust` call — `scope`/`trust` feed
    `SourceTrace`/`ContextTrace`'s reject counters (`08_OBSERVABILITY_
    FINAL.md`), `examples` a handful of human-readable "REJECTED ..."
    lines for `doctor`/debug (capped so a noisy source can't blow up the
    trace)."""

    scope: int = 0
    trust: int = 0
    examples: tuple[str, ...] = ()


def apply_scope_and_trust(
    items: list[ContextItem], *, allowed_project_id: str | None
) -> tuple[list[ContextItem], ScopeRejects]:
    """Fail-closed project/workspace/trust gate (issue #372 follow-up,
    `02_OPENVIKING_STRICT_SCOPE.md`) — Takkub is the control plane for
    scope (`14_OPENVIKING_INTEGRATION.md`: "Takkub remains control plane:
    scope... user/project identity"), never OpenViking, so this never
    trusts an item's tag without re-checking it here.

    An item passes iff its `trust` is a recognised value AND its
    `workspace_id` matches this install's own AND its `project_id` is
    either exactly `allowed_project_id` or the `GLOBAL_PROJECT_ID`
    sentinel. Anything else — including a `project_id` of `None`, i.e.
    "no scope claim at all" — is rejected: missing metadata is never
    treated as implicitly global.

    Applied twice on the live path: once inside each source's own
    `retrieve()` (layer b — `openviking_source.py`/`resource_source.py`),
    and again on the merged pool right before injection
    (`context_builder.merge_openviking_traced`, layer c) — a bug in one
    layer is still caught by the other."""
    kept: list[ContextItem] = []
    scope_rejects = trust_rejects = 0
    examples: list[str] = []

    def _example(msg: str) -> None:
        if len(examples) < _MAX_REJECT_EXAMPLES:
            examples.append(msg)

    for item in items:
        if item.trust not in _VALID_TRUST:
            trust_rejects += 1
            _example(f"REJECTED {item.provenance} reason=invalid trust ({item.trust!r})")
            continue
        if item.workspace_id != WORKSPACE_ID:
            scope_rejects += 1
            _example(f"REJECTED {item.provenance} reason=workspace scope mismatch")
            continue
        pid = item.project_id
        if not pid:
            scope_rejects += 1
            _example(f"REJECTED {item.provenance} reason=missing project metadata")
            continue
        if pid == GLOBAL_PROJECT_ID or (
            allowed_project_id is not None and pid == allowed_project_id
        ):
            kept.append(item)
            continue
        scope_rejects += 1
        _example(
            f"REJECTED {item.provenance} reason=project scope mismatch "
            f"(got={pid} want={allowed_project_id or GLOBAL_PROJECT_ID})"
        )

    return kept, ScopeRejects(scope=scope_rejects, trust=trust_rejects, examples=tuple(examples))


__all__ = [
    "GLOBAL_PROJECT_ID",
    "WORKSPACE_ID",
    "ContextItem",
    "ContextSource",
    "ScopeRejects",
    "apply_scope_and_trust",
    "collapse_near_duplicates",
    "estimate_tokens",
]
