"""OpenViking sidecar resources as a `ContextSource` (issue #372). Thin —
all HTTP/fail-open/schema-drift handling lives in `openviking_adapter.py`;
this module only shapes `SearchHit` into `ContextItem` and gates on the
adapter's own `enabled()` flag so a disabled sidecar costs this source a
single boolean check, never a network attempt.
"""

from __future__ import annotations

from . import openviking_adapter as adapter
from .base import ContextItem, estimate_tokens

_RESULT_LIMIT = 8
_TRUST_EXTERNAL = "external"


class OpenVikingSource:
    name = "openviking"

    def retrieve(
        self, query: str, *, project: str | None, role: str, budget_tokens: int
    ) -> list[ContextItem]:
        if budget_tokens <= 0 or not adapter.enabled():
            return []
        hits = adapter.search_resources(query, limit=_RESULT_LIMIT)
        return [
            ContextItem(
                text=hit.text,
                tokens=estimate_tokens(hit.text),
                source=self.name,
                provenance=hit.uri,
                trust=_TRUST_EXTERNAL,
                score=hit.score,
            )
            for hit in hits
        ]


__all__ = ["OpenVikingSource"]
