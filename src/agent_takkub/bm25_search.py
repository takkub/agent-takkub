"""Hand-rolled BM25 ranking for `takkub search` (#152).

Zero-infra by design (no vector DB, no new dependency): tokenize the same
corpus `chatlog_scanner.search_sessions` already grep-scans (today's — or
`--days`/`--all` scoped — session jsonls) plus any role-memory archive files
from #151 (`runtime/role-memory/**/*-archive.md`, read line-by-line since
they carry no per-record timestamp), score every document against the query
with Okapi BM25, and return the top-N ranked hits.

Tokenizer is mixed: ASCII word tokens (lowercased) for English/code text,
character trigrams for Thai runs (`\\u0E00-\\u0E7F`) since Thai has no spaces
between words and pulling in a segmenter would break the "no new dependency"
rule. A short Thai run (<3 chars) is kept whole rather than dropped.

Index is built fresh per query (corpus is small — today's sessions). Falls
back to the old grep path (`chatlog_scanner.search_sessions`) when the query
is too short to tokenize meaningfully or indexing raises for any reason —
`takkub search` must never hard-fail just because ranking broke.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime

from .chatlog_scanner import (
    extract_text,
    iter_records,
    iter_session_files,
    role_of,
    search_sessions,
)
from .role_memory import ROLE_MEMORY_DIR

_log = logging.getLogger(__name__)

MIN_QUERY_CHARS = 3
_BM25_K1 = 1.5
_BM25_B = 0.75

_TOKEN_RE = re.compile("[฀-๿]+|[A-Za-z0-9_]+")
_THAI_RE = re.compile("[฀-๿]")


def tokenize(text: str) -> list[str]:
    """ASCII word tokens (lowercase) + Thai character trigrams, interleaved
    in original order. A Thai run shorter than 3 chars is kept as one token
    rather than dropped (short words like "คน" still need to be searchable)."""
    if not text:
        return []
    tokens: list[str] = []
    for m in _TOKEN_RE.finditer(text.lower()):
        piece = m.group(0)
        if _THAI_RE.match(piece):
            n = len(piece)
            tokens.extend([piece] if n < 3 else [piece[i : i + 3] for i in range(n - 2)])
        else:
            tokens.append(piece)
    return tokens


def _iter_archive_docs(project_filter: str | None):
    """Yield (meta, text) for each non-blank line of every
    `runtime/role-memory/**/*-archive.md` file. Best-effort: a missing
    directory or unreadable file just yields nothing for that source."""
    needle = project_filter.lower() if project_filter else None
    try:
        paths = sorted(ROLE_MEMORY_DIR.glob("*/*-archive.md"))
    except OSError:
        return
    for path in paths:
        project = path.parent.name
        if needle is not None and needle not in project.lower():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            yield (
                {
                    "project": project,
                    "path": str(path),
                    "timestamp": "",
                    "role": "archive",
                    "line": i,
                },
                stripped,
            )


def _iter_corpus_docs(project_filter: str | None, since: datetime | None):
    """Yield (meta, text) for every candidate document: conversation
    records (same source `search_sessions` grep-scans) plus role-memory
    archive lines."""
    for jsonl in iter_session_files(project_filter, since=since):
        for rec in iter_records(jsonl):
            text = extract_text(rec)
            if not text:
                continue
            yield (
                {
                    "project": jsonl.parent.name,
                    "path": str(jsonl),
                    "timestamp": rec.get("timestamp") or "",
                    "role": role_of(rec) or "",
                },
                text,
            )
    yield from _iter_archive_docs(project_filter)


def _snippet(text: str, query: str, width: int = 200) -> str:
    """~width chars centred on the query (or its first word, as a fallback
    since a BM25 hit isn't guaranteed to contain the literal query
    substring). Falls back to a plain head when nothing matches literally."""
    flat = " ".join(text.split())
    flat_lower = flat.lower()
    needle = query
    idx = flat_lower.find(needle.lower())
    if idx < 0:
        for word in query.split():
            idx = flat_lower.find(word.lower())
            if idx >= 0:
                needle = word
                break
    if idx < 0:
        return flat[:width]
    half = width // 2
    start = max(0, idx - half)
    end = min(len(flat), idx + len(needle) + half)
    snippet = flat[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(flat):
        snippet += "…"
    return snippet


def _bm25_rank(
    docs: list[tuple[dict, str]], query_tokens: list[str], limit: int
) -> list[tuple[dict, float, str]]:
    """Okapi BM25 over `docs` ((meta, text) pairs), returning the top
    `limit` (meta, score, text) tuples ranked by descending score (ties
    broken by most-recent timestamp)."""
    n_docs = len(docs)
    if n_docs == 0:
        return []
    doc_tf: list[dict[str, int]] = []
    doc_len: list[int] = []
    df: dict[str, int] = {}
    for _meta, text in docs:
        counts: dict[str, int] = {}
        for tok in tokenize(text):
            counts[tok] = counts.get(tok, 0) + 1
        doc_tf.append(counts)
        doc_len.append(sum(counts.values()))
        for tok in counts:
            df[tok] = df.get(tok, 0) + 1
    avgdl = (sum(doc_len) / n_docs) if n_docs else 0.0
    idf = {
        tok: math.log(1.0 + (n_docs - n + 0.5) / (n + 0.5))
        for tok, n in df.items()
        if tok in query_tokens
    }
    scored: list[tuple[float, int]] = []
    for i in range(n_docs):
        tf = doc_tf[i]
        dl = doc_len[i]
        score = 0.0
        for tok in query_tokens:
            f = tf.get(tok, 0)
            if f == 0:
                continue
            denom = f + _BM25_K1 * (1 - _BM25_B + _BM25_B * (dl / avgdl if avgdl else 0.0))
            score += idf.get(tok, 0.0) * (f * (_BM25_K1 + 1)) / denom
        if score > 0:
            scored.append((score, i))
    scored.sort(key=lambda p: (p[0], docs[p[1]][0].get("timestamp") or ""), reverse=True)
    return [(docs[i][0], score, docs[i][1]) for score, i in scored[:limit]]


def search(
    query: str,
    *,
    project_filter: str | None = None,
    since: datetime | None = None,
    limit: int = 20,
) -> tuple[list[dict], bool]:
    """Ranked search over the session + role-memory-archive corpus.

    Returns `(hits, used_bm25)`. `hits` are shaped like
    `chatlog_scanner.search_sessions`'s output (project/path/timestamp/role/
    snippet) plus a `score` (and `line` for archive hits). Falls back to the
    grep path — `used_bm25=False` — when the query is too short to tokenize
    meaningfully or indexing raises for any reason.
    """
    query_tokens = tokenize(query) if len(query.strip()) >= MIN_QUERY_CHARS else []
    if not query_tokens:
        return search_sessions(
            query, project_filter=project_filter, since=since, limit=limit
        ), False
    try:
        docs = list(_iter_corpus_docs(project_filter, since))
        ranked = _bm25_rank(docs, query_tokens, limit)
    except Exception:
        _log.exception("bm25_search: indexing failed, falling back to grep")
        return search_sessions(
            query, project_filter=project_filter, since=since, limit=limit
        ), False
    hits: list[dict] = []
    for meta, score, text in ranked:
        hit = dict(meta)
        hit["score"] = score
        hit["snippet"] = _snippet(text, query)
        hits.append(hit)
    return hits, True
