"""OpenViking sidecar resources as a `ContextSource` (issue #372). Thin —
all HTTP/fail-open/schema-drift handling lives in `openviking_adapter.py`;
this module only shapes `SearchHit` into `ContextItem` and gates on the
adapter's own `enabled()` flag so a disabled sidecar costs this source a
single boolean check, never a network attempt.

Project scope (issue #372 follow-up, `02_OPENVIKING_STRICT_SCOPE.md`): a
search hit's `uri` is OpenViking's own identifier, never trusted for scope
(`14_OPENVIKING_INTEGRATION.md`: "Do not hard-code mutable `viking://` URI
semantics as canonical Takkub IDs" / "Takkub remains control plane:
scope"). Instead every hit is looked up in `indexing.py`'s local registry
— the metadata Takkub itself attached at index time, keyed by the SAME
`uri` the sidecar actually confirmed for that resource at ingest (issue
#377 — see `indexing.py`'s module docstring for why that can differ from
the `to=` this codebase requested) — and a hit with no registry entry
(never indexed by this install, or indexed by a version that predates
this registry) fails closed rather than being treated as implicitly
global.
"""

from __future__ import annotations

from . import openviking_adapter as adapter
from .base import ContextItem, apply_scope_and_trust, estimate_tokens

_RESULT_LIMIT = 8
_TRUST_EXTERNAL = "external"


class OpenVikingSource:
    name = "openviking"

    def __init__(self) -> None:
        self.last_scope_rejects = 0
        self.last_trust_rejects = 0

    def retrieve(
        self, query: str, *, project: str | None, role: str, budget_tokens: int
    ) -> list[ContextItem]:
        self.last_scope_rejects = 0
        self.last_trust_rejects = 0
        if budget_tokens <= 0 or not adapter.enabled():
            return []
        hits = adapter.search_resources(query, limit=_RESULT_LIMIT)
        if not hits:
            return []

        from agent_takkub.project_identity import resolve_project_id

        from . import indexing

        try:
            allowed_project_id = resolve_project_id(project) if project else None
        except ValueError:
            allowed_project_id = None

        candidates = []
        for hit in hits:
            meta = indexing.resource_metadata_for_uri(hit.uri)
            candidates.append(
                ContextItem(
                    text=hit.text,
                    tokens=estimate_tokens(hit.text),
                    source=self.name,
                    provenance=hit.uri,
                    trust=_TRUST_EXTERNAL,
                    score=hit.score,
                    project_id=meta.get("project_id") if meta else None,
                    workspace_id=meta.get("workspace_id") if meta else None,
                )
            )

        kept, rejects = apply_scope_and_trust(candidates, allowed_project_id=allowed_project_id)
        self.last_scope_rejects = rejects.scope
        self.last_trust_rejects = rejects.trust
        return kept


__all__ = ["OpenVikingSource"]
