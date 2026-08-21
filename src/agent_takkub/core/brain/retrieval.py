"""RetrievalEngine — ranks `MemoryRecord`s for `recall(query, scope,
budget_tokens)`. Reuses `bm25_search.tokenize` (the ASCII-word + Thai-
trigram tokenizer, plan: "ใช้ bm25_search เดิม") but reimplements the BM25
sum itself: `bm25_search._bm25_rank` is private and scores `(meta, text)`
pairs read from disk-scanned session/archive files, not an in-memory
`MemoryRecord` list — same Okapi BM25 formula, different corpus.

Ranking is `bm25 + scope match + recency + confidence + importance`
(plan: "ห้าม vector-only") — never a single embedding-similarity score.

The bm25 term is `query-coverage x saturating(bm25_raw)`, and a record that
covers less than `_MIN_QUERY_COVERAGE` of the query's distinct tokens is not
returned at all (#333). Both exist because this corpus is one project's
JSONL — a few dozen documents — where plain BM25 is not a usable relevance
signal on its own:

* idf on a ~36-doc corpus makes any token that happens to be rare look
  informative. Measured on a real store, "add pagination to the users table
  API endpoint" scored 4.53 on a record about a git conflict whose ONLY
  overlap with the query was the word "the".
* the old normalisation was `bm25 / max(bm25)`, which rescales the best hit
  to 1.0 no matter how weak it is in absolute terms — so "the strongest of
  36 irrelevant records" and "an exact match" were indistinguishable.
* with everything scoring, the non-query signals could carry a record with
  zero term overlap: `0.5 scope + 0.3 recency + 0.3 confidence + 0.3 trust`
  = 1.4, above `_W_BM25` at full scale, i.e. recent+trusted always beat
  relevant.

Known residual: `bm25_search.tokenize` shingles Thai into trigrams, so a
short Thai query made mostly of function words can still clear the coverage
floor on trigrams alone ("ทำ QA gate ก่อน merge" matches "ก่อ"+"่อน" of an
unrelated record). Fixing that needs word segmentation, not a threshold.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from agent_takkub.bm25_search import tokenize
from agent_takkub.core.models.memory import Confidence, MemoryRecord, Scope, Trust

from .store import BrainStore

_BM25_K1 = 1.5
_BM25_B = 0.75

_RECENCY_HALF_LIFE_SECONDS = 30 * 86400.0

_CONFIDENCE_WEIGHT: dict[Confidence, float] = {
    Confidence.LOW: 0.4,
    Confidence.MEDIUM: 0.7,
    Confidence.HIGH: 1.0,
}

_TRUST_IMPORTANCE: dict[Trust, float] = {
    Trust.COCKPIT_MEASURED: 1.0,
    Trust.USER_CONFIRMED: 1.0,
    Trust.LEAD_CONFIRMED: 0.9,
    Trust.AGENT_REPORTED: 0.6,
    Trust.EXTERNAL_UNTRUSTED: 0.2,
}

# bm25_raw -> 0..1 without destroying absolute magnitude the way dividing by
# the batch maximum did: `raw / (raw + _BM25_SATURATION)`. Sized so a solid
# multi-term hit on this corpus (raw ~6) lands around 0.6 while a one-weak-
# token hit (raw ~1) stays near 0.2.
_BM25_SATURATION = 4.0

# Fraction of the query's DISTINCT tokens a record must contain to be
# eligible at all. 0.2 is the measured separation point: the single-stopword
# matches above cover 1/8 = 0.125, while a genuine hit on the same store
# ("theme layout admin design") covers 5/15 = 0.33.
_MIN_QUERY_COVERAGE = 0.2

_W_BM25 = 1.0
_W_SCOPE = 0.5
_W_RECENCY = 0.3
_W_CONFIDENCE = 0.3
_W_IMPORTANCE = 0.3

# ponytail: 1 token ≈ 4 chars is a rough estimate, not a real tokenizer
# count — fine for a soft recall budget; upgrade to a real token counter
# if the Context Builder (7c) ever needs a hard guarantee.
_CHARS_PER_TOKEN = 4


@dataclass(frozen=True, slots=True)
class ScoredRecord:
    record: MemoryRecord
    score: float


def _bm25_scores(docs_tokens: list[list[str]], query_tokens: list[str]) -> list[float]:
    n = len(docs_tokens)
    if n == 0 or not query_tokens:
        return [0.0] * n
    doc_len = [len(t) for t in docs_tokens]
    avgdl = (sum(doc_len) / n) if n else 0.0
    df: dict[str, int] = {}
    tfs: list[dict[str, int]] = []
    for toks in docs_tokens:
        counts: dict[str, int] = {}
        for t in toks:
            counts[t] = counts.get(t, 0) + 1
        tfs.append(counts)
        for t in counts:
            df[t] = df.get(t, 0) + 1
    idf = {
        t: math.log(1.0 + (n - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5))
        for t in set(query_tokens)
    }
    scores: list[float] = []
    for i in range(n):
        tf = tfs[i]
        dl = doc_len[i]
        s = 0.0
        for t in query_tokens:
            f = tf.get(t, 0)
            if f == 0:
                continue
            denom = f + _BM25_K1 * (1 - _BM25_B + _BM25_B * (dl / avgdl if avgdl else 0.0))
            s += idf.get(t, 0.0) * (f * (_BM25_K1 + 1)) / denom
        scores.append(s)
    return scores


def _scope_score(record_scope: Scope, requested_scope: Scope) -> float:
    if record_scope == requested_scope:
        return 1.0
    if record_scope == Scope.GLOBAL:
        return 0.5
    return 0.0


def _recency_score(created_at: float | None, now: float) -> float:
    if created_at is None:
        return 0.0
    age = max(0.0, now - created_at)
    return 0.5 ** (age / _RECENCY_HALF_LIFE_SECONDS)


def _fit_budget(records: list[MemoryRecord], budget_tokens: int) -> list[MemoryRecord]:
    """Trims the (already ranked) list to fit `budget_tokens`. The top hit
    is always kept even if it alone exceeds the budget — a non-empty
    request should never come back with zero results just because the
    single best match is long."""
    if budget_tokens <= 0:
        return []
    out: list[MemoryRecord] = []
    used = 0
    for r in records:
        cost = max(1, len(r.content) // _CHARS_PER_TOKEN)
        if out and used + cost > budget_tokens:
            break
        out.append(r)
        used += cost
    return out


class RetrievalEngine:
    def __init__(self, store: BrainStore) -> None:
        self._store = store

    def recall(
        self,
        query: str,
        *,
        scope: Scope,
        budget_tokens: int = 2000,
        now: float | None = None,
    ) -> list[MemoryRecord]:
        records = self._store.load_active()
        if not records:
            return []
        now = now if now is not None else time.time()
        query_tokens = tokenize(query)
        query_set = set(query_tokens)
        if not query_set:
            # No queryable terms (blank/punctuation-only): there is nothing to
            # be relevant TO, so rank nothing rather than handing back the
            # whole store ordered by recency/trust.
            return []
        docs_tokens = [tokenize(r.content) for r in records]
        bm25_raw = _bm25_scores(docs_tokens, query_tokens)

        scored: list[ScoredRecord] = []
        for record, bm25, doc_tokens in zip(records, bm25_raw, docs_tokens, strict=True):
            coverage = len(query_set & set(doc_tokens)) / len(query_set)
            if coverage < _MIN_QUERY_COVERAGE:
                continue
            relevance = coverage * (bm25 / (bm25 + _BM25_SATURATION)) if bm25 > 0 else 0.0
            if relevance <= 0.0:
                continue
            score = (
                _W_BM25 * relevance
                + _W_SCOPE * _scope_score(record.scope, scope)
                + _W_RECENCY * _recency_score(record.created_at, now)
                + _W_CONFIDENCE * _CONFIDENCE_WEIGHT.get(record.confidence, 0.0)
                + _W_IMPORTANCE * _TRUST_IMPORTANCE.get(record.trust, 0.0)
            )
            scored.append(ScoredRecord(record, score))

        if not scored:
            return []
        scored.sort(key=lambda sr: sr.score, reverse=True)
        ranked = [sr.record for sr in scored]
        return _fit_budget(ranked, budget_tokens)


__all__ = ["RetrievalEngine", "ScoredRecord"]
