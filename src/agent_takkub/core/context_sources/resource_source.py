"""Curated Obsidian docs as a `ContextSource` (issue #372) — reads the SAME
default-deny allowlist `obsidian_boundary.is_indexable()` already enforces
(`01-Projects`/`02-Areas` only, everything else denied — see that module's
docstring), so this never surfaces `99-Logs`/`.obsidian`/raw transcripts
just because they happen to sit in the vault.

Local-only: reads the vault directly off disk and ranks it with the same
hand-rolled ranker `bm25_search.py` already uses for `takkub search` — no
network, no external service.
"""

from __future__ import annotations

import logging
import pathlib

from agent_takkub.bm25_search import _bm25_rank, tokenize
from agent_takkub.obsidian_boundary import is_indexable
from agent_takkub.obsidian_metadata import TRUST_CURATED
from agent_takkub.vault_mirror import _resolve_vault_dir

from .base import (
    GLOBAL_PROJECT_ID,
    WORKSPACE_ID,
    ContextItem,
    apply_scope_and_trust,
    estimate_tokens,
    wrap_untrusted_reference,
)

_log = logging.getLogger(__name__)

# Scale caps — a vault can grow unbounded over months of `takkub done`
# writes; this source must stay cheap enough to run on every context-build
# call, not just be *correct*. Neither cap needs to be exact: a doc past
# the byte cap just isn't searched this query, and a vault past the file
# cap only misses whatever sorts after the cap alphabetically (`rglob`
# order is not otherwise meaningful here).
_MAX_FILES_SCANNED = 500
_MAX_FILE_BYTES = 200_000
_SNIPPET_CHARS = 1200
_RESULT_LIMIT = 6


def _iter_allowlisted_docs(vault: pathlib.Path) -> list[tuple[dict, str]]:
    docs: list[tuple[dict, str]] = []
    try:
        paths = sorted(vault.rglob("*.md"))
    except OSError:
        return docs
    for path in paths:
        if len(docs) >= _MAX_FILES_SCANNED:
            break
        try:
            rel = path.relative_to(vault).as_posix()
        except ValueError:
            continue
        if not is_indexable(rel):
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not text.strip():
            continue
        docs.append(({"path": rel}, text))
    return docs


def resolve_vault_project_id(rel: str) -> str | None:
    """Derive which project a vault doc under `01-Projects`/`02-Areas`
    belongs to, purely from its path — the same boundary
    `obsidian_boundary.is_indexable()` already enforces, so every doc that
    reaches this function has already passed that allowlist. `02-Areas` is
    cross-project curated content (`base.GLOBAL_PROJECT_ID`, same sentinel
    `vault_mirror._MOC_PROJECT_ID` writes) — everything under
    `01-Projects` belongs to the project named by its first path segment,
    whether that's the flat `01-Projects/<project>.md` page
    `vault_mirror._ensure_project_page` writes, or a
    `01-Projects/<project>/...` subtree."""
    parts = rel.split("/")
    if not parts:
        return None
    if parts[0] == "02-Areas":
        return GLOBAL_PROJECT_ID
    if parts[0] == "01-Projects" and len(parts) >= 2:
        name = parts[1]
        return name[:-3] if name.endswith(".md") else name
    return None


class ResourceSource:
    name = "resource"

    def __init__(self) -> None:
        self.last_scope_rejects = 0
        self.last_trust_rejects = 0

    def retrieve(
        self, query: str, *, project: str | None, role: str, budget_tokens: int
    ) -> list[ContextItem]:
        self.last_scope_rejects = 0
        self.last_trust_rejects = 0
        if budget_tokens <= 0:
            return []
        query_tokens = tokenize(query)
        if not query_tokens:
            return []
        vault = _resolve_vault_dir()
        if vault is None:
            return []

        from agent_takkub.project_identity import resolve_project_id

        try:
            allowed_project_id = resolve_project_id(project) if project else None
        except ValueError:
            allowed_project_id = None

        try:
            docs = _iter_allowlisted_docs(vault)
            if not docs:
                return []
            scoped_docs = []
            for meta, text in docs:
                doc_project_id = resolve_vault_project_id(meta["path"])
                if doc_project_id is None:
                    self.last_scope_rejects += 1
                    continue
                if doc_project_id != GLOBAL_PROJECT_ID and doc_project_id != allowed_project_id:
                    self.last_scope_rejects += 1
                    continue
                scoped_docs.append(({**meta, "project_id": doc_project_id}, text))
            if not scoped_docs:
                return []
            ranked = _bm25_rank(scoped_docs, query_tokens, _RESULT_LIMIT)
        except Exception:
            _log.exception("resource_source: local vault search failed (fail-open)")
            return []

        items: list[ContextItem] = []
        for meta, score, text in ranked:
            snippet = text[:_SNIPPET_CHARS]
            provenance = meta.get("path", "?")
            # v2-hardening H: this snippet is retrieved document content, not
            # this cockpit's own state — frame it as untrusted data before it
            # can reach any pane prompt, so an "ignore previous instructions"
            # line planted inside a vault doc is never mistaken for a real
            # instruction (`14_SECURITY_RETRIEVAL.md`).
            wrapped = wrap_untrusted_reference(snippet, provenance)
            items.append(
                ContextItem(
                    text=wrapped,
                    tokens=estimate_tokens(wrapped),
                    source=self.name,
                    provenance=provenance,
                    trust=TRUST_CURATED,
                    score=score,
                    project_id=meta.get("project_id"),
                    workspace_id=WORKSPACE_ID,
                )
            )
        # Defense in depth: the loop above already filtered by path, this
        # re-checks the constructed items through the same gate
        # `context_builder` uses, so a future edit to that loop can't
        # silently regress scope.
        items, rejects = apply_scope_and_trust(items, allowed_project_id=allowed_project_id)
        self.last_scope_rejects += rejects.scope
        self.last_trust_rejects += rejects.trust
        return items


__all__ = ["ResourceSource", "resolve_vault_project_id"]
