"""core.context_sources.base — the shared `ContextItem` vocabulary and
`collapse_near_duplicates` dedup helper every `ContextSource` builds on."""

from __future__ import annotations

from agent_takkub.core.context_sources.base import ContextItem, collapse_near_duplicates

# ── base.py ──────────────────────────────────────────────────────────────


def _item(
    text: str, *, source: str = "resource", trust: str = "curated", score: float = 0.0
) -> ContextItem:
    return ContextItem(
        text=text,
        tokens=max(1, len(text) // 4),
        source=source,
        provenance="p",
        trust=trust,
        score=score,
    )


def test_collapse_near_duplicates_keeps_the_longer_of_a_restated_pair():
    a = _item("the deploy pipeline uses github actions for ci")
    b = _item("deploy pipeline uses github actions for ci and cd both")
    kept, dropped = collapse_near_duplicates([a, b])
    assert dropped == 1
    assert len(kept) == 1
    assert kept[0].text == b.text


def test_collapse_near_duplicates_keeps_distinct_items():
    a = _item("rebuild and restart admin and frontend, both healthy again")
    b = _item("rebuild and restart the api container, health check green")
    kept, dropped = collapse_near_duplicates([a, b])
    assert dropped == 0
    assert len(kept) == 2


def test_collapse_near_duplicates_empty_text_never_merges():
    a = _item("")
    b = _item("")
    kept, dropped = collapse_near_duplicates([a, b])
    assert dropped == 0
    assert len(kept) == 2
