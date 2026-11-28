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

import json
import math
import os
import threading
import time
from dataclasses import dataclass

from agent_takkub.bm25_search import tokenize
from agent_takkub.core.models.memory import (
    Confidence,
    MemoryRecord,
    RecordStatus,
    Scope,
    Trust,
)
from agent_takkub.token_estimate import estimate_tokens as _token_cost

from .store import BrainStore, _record_from_dict

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


# #1023: `recall` used to tokenize every record on every call — pure-Python
# CPU on a worker thread while the Qt thread waits on it (300 ms cap in
# `_inject_v2_context`). Measured on prod 2026-11-16: 11 of 15 assigns timed
# out, i.e. the recall delivered nothing and still held the GIL beside a
# spawn. Record content is immutable once written, so its tokens are kept;
# a cold pass sleeps briefly every few records so the GUI thread is not
# queued behind it (same reasoning as `graft_autobuild._yield_gil`, #973).
_TOKEN_CACHE: dict[str, list[str]] = {}
_TOKEN_CACHE_MAX = 20_000
_COLD_YIELD_EVERY = 16
_COLD_YIELD_S = 0.002


def _doc_tokens(contents: list[str]) -> list[list[str]]:
    out: list[list[str]] = []
    cold = 0
    for content in contents:
        tokens = _TOKEN_CACHE.get(content)
        if tokens is None:
            if len(_TOKEN_CACHE) >= _TOKEN_CACHE_MAX:
                _TOKEN_CACHE.clear()
            tokens = _TOKEN_CACHE[content] = tokenize(content)
            cold += 1
            if cold % _COLD_YIELD_EVERY == 0:
                time.sleep(_COLD_YIELD_S)
        out.append(tokens)
    return out


@dataclass(frozen=True, slots=True)
class ScoredRecord:
    record: MemoryRecord
    score: float


@dataclass(frozen=True, slots=True)
class _StoreIndex:
    """Everything `recall` derives from a store's records alone, i.e. all of
    it except the query. #1045: rebuilt per call it was ~90 % of a warm
    recall (2 file reads + term counts over every record) — enough, beside
    six working panes, to miss `_inject_v2_context`'s 300 ms on 64 % of
    assigns. Kept per store file until that file's size/mtime moves."""

    records: list[MemoryRecord]
    token_sets: list[frozenset[str]]
    doc_len: list[int]
    tfs: list[dict[str, int]]
    df: dict[str, int]


_INDEX_CACHE_MAX = 64


def _term_counts(docs_tokens: list[list[str]]) -> tuple[list[dict[str, int]], dict[str, int]]:
    df: dict[str, int] = {}
    tfs: list[dict[str, int]] = []
    for toks in docs_tokens:
        counts: dict[str, int] = {}
        for t in toks:
            counts[t] = counts.get(t, 0) + 1
        tfs.append(counts)
        for t in counts:
            df[t] = df.get(t, 0) + 1
    return tfs, df


def _build_index(records: list[MemoryRecord]) -> _StoreIndex:
    docs_tokens = _doc_tokens([r.content for r in records])
    tfs, df = _term_counts(docs_tokens)
    return _StoreIndex(
        records, [frozenset(t) for t in docs_tokens], [len(t) for t in docs_tokens], tfs, df
    )


_HEAD_BYTES = 256


@dataclass(slots=True)
class _IndexState:
    """#1069: what `_index_for` keeps per store so a changed store costs only
    its new lines. The store is an append-only event log (`JsonlStore.append`
    rewrites it as old bytes + one line), and every `done()` appends — so with
    #1045's whole-file rebuild on each change, a 5.1 MB / 3,883-record store
    (saas_admin_amb, measured 2026-11-28) paid ~220 ms idle (~1 s with a cold
    token cache) on nearly every assign and missed the 300 ms budget on 30 %.
    `latest` keeps every id's newest row in first-seen order (exactly
    `BrainStore.load_latest`'s order); `docs` holds term counts for the ACTIVE
    ones only, and `df` is kept in step with `docs`."""

    sig: tuple[int, int]
    offset: int
    head: bytes
    latest: dict[str, MemoryRecord]
    docs: dict[str, tuple[dict[str, int], frozenset[str], int]]
    df: dict[str, int]
    index: _StoreIndex
    tail: bytes = b""  # the `_HEAD_BYTES` right before `offset` (N-1)


_INDEX_CACHE: dict[str, _IndexState] = {}
# review-2 N-8: one lock per store path — a cold ~1 s warm of project A must
# not queue project B's `_inject_v2_context` past its 300 ms budget.
_INDEX_LOCKS: dict[str, threading.Lock] = {}
_INDEX_LOCKS_GUARD = threading.Lock()


def _index_lock(key: str) -> threading.Lock:
    with _INDEX_LOCKS_GUARD:
        lock = _INDEX_LOCKS.get(key)
        if lock is None:
            lock = _INDEX_LOCKS[key] = threading.Lock()
        return lock


def _read_new_rows(
    path: str, state: _IndexState | None
) -> tuple[list[dict], int, bytes, bool, bytes]:
    """Rows after `state.offset` when the file still starts AND, up to the
    offset, ends with what was indexed (same head, same `_HEAD_BYTES` right
    before the offset — that tail ends in the line break), else every row.
    Returns `(rows, new_offset, head, appended_only, tail)`. Only complete
    lines are consumed — a write is an atomic `os.replace`, so a partial line
    is corruption and is skipped exactly like `JsonlStore.read_all` skips it."""
    with open(path, "rb") as f:
        head = f.read(_HEAD_BYTES)
        start = 0
        if state is not None and state.offset > 0 and head == state.head[: len(head)]:
            # review-2 N-1: a rewrite with the same head and a `\n` at
            # offset-1 used to pass as an append; the tail must match too.
            back = min(state.offset, _HEAD_BYTES)
            f.seek(state.offset - back)
            if f.read(back) == state.tail:
                start = state.offset
        f.seek(start)
        raw = f.read()
    end = raw.rfind(b"\n") + 1
    tail = ((state.tail if start else b"") + raw[:end])[-_HEAD_BYTES:]
    rows: list[dict] = []
    for line in raw[:end].decode("utf-8", errors="replace").split("\n"):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows, start + end, head, start > 0, tail


def _apply_rows(state: _IndexState, rows: list[dict]) -> None:
    recs: list[MemoryRecord] = []
    for row in rows:
        try:
            recs.append(_record_from_dict(row))
        except (KeyError, ValueError, TypeError):
            continue  # corrupt/foreign row — same skip as `BrainStore.load_latest`
    active = [r for r in recs if r.status == RecordStatus.ACTIVE]
    tokens_by_rec = dict(
        zip(map(id, active), _doc_tokens([r.content for r in active]), strict=True)
    )
    df = state.df
    for rec in recs:
        old = state.docs.pop(rec.id, None)
        if old is not None:
            for t in old[0]:
                n = df[t] - 1
                if n:
                    df[t] = n
                else:
                    del df[t]
        state.latest[rec.id] = rec
        toks = tokens_by_rec.get(id(rec))
        if toks is None:
            continue
        counts: dict[str, int] = {}
        for t in toks:
            counts[t] = counts.get(t, 0) + 1
        for t in counts:
            df[t] = df.get(t, 0) + 1
        state.docs[rec.id] = (counts, frozenset(toks), len(toks))


def _materialize(state: _IndexState) -> _StoreIndex:
    records: list[MemoryRecord] = []
    token_sets: list[frozenset[str]] = []
    doc_len: list[int] = []
    tfs: list[dict[str, int]] = []
    for rid, rec in state.latest.items():
        doc = state.docs.get(rid)
        if doc is None:
            continue
        records.append(rec)
        tfs.append(doc[0])
        token_sets.append(doc[1])
        doc_len.append(doc[2])
    # `df` is copied: the live dict keeps changing under the next append
    # while a recall on another thread may still be scoring with this index.
    return _StoreIndex(records, token_sets, doc_len, tfs, dict(state.df))


def _index_is_warm(store: BrainStore) -> bool:
    """One `stat`: True when `_index_for(store)` would be a cache hit (or the
    store file does not exist — nothing to build)."""
    try:
        st = os.stat(store.path)
    except OSError:
        return True
    hit = _INDEX_CACHE.get(str(store.path))
    return hit is not None and hit.sig == (st.st_mtime_ns, st.st_size)


def _index_for(store: BrainStore) -> _StoreIndex:
    path = getattr(store, "path", None)
    st = None
    if path is not None:
        try:
            st = os.stat(path)
        except OSError:
            st = None
    if st is None:
        return _build_index(store.load_active())
    sig = (st.st_mtime_ns, st.st_size)
    key = str(path)
    hit = _INDEX_CACHE.get(key)
    if hit is not None and hit.sig == sig:
        return hit.index
    with _index_lock(key):
        hit = _INDEX_CACHE.get(key)
        if hit is not None and hit.sig == sig:
            return hit.index
        rows, offset, head, appended, tail = _read_new_rows(key, hit)
        state = hit
        if state is None or not appended:
            state = _IndexState(sig, 0, b"", {}, {}, {}, _StoreIndex([], [], [], [], {}))
        _apply_rows(state, rows)
        index = _materialize(state)
        # review-2 N-2: publish the index before the sig — a lock-free reader
        # that sees the new sig must never get the previous index.
        state.offset, state.head, state.tail = offset, head, tail
        state.index = index
        state.sig = sig
        if key not in _INDEX_CACHE and len(_INDEX_CACHE) >= _INDEX_CACHE_MAX:
            _INDEX_CACHE.clear()
        _INDEX_CACHE[key] = state
        return index


def _bm25_scores(docs_tokens: list[list[str]], query_tokens: list[str]) -> list[float]:
    n = len(docs_tokens)
    if n == 0 or not query_tokens:
        return [0.0] * n
    tfs, df = _term_counts(docs_tokens)
    return _bm25_from_counts(tfs, df, [len(t) for t in docs_tokens], query_tokens)


def _bm25_from_counts(
    tfs: list[dict[str, int]], df: dict[str, int], doc_len: list[int], query_tokens: list[str]
) -> list[float]:
    n = len(tfs)
    if n == 0 or not query_tokens:
        return [0.0] * n
    avgdl = (sum(doc_len) / n) if n else 0.0
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
        cost = _token_cost(r.content)
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
        index = _index_for(self._store)
        records = index.records
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
        bm25_raw = _bm25_from_counts(index.tfs, index.df, index.doc_len, query_tokens)

        scored: list[ScoredRecord] = []
        for record, bm25, doc_tokens in zip(records, bm25_raw, index.token_sets, strict=True):
            coverage = len(query_set & doc_tokens) / len(query_set)
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
