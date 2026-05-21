"""TF-IDF role boundary audit — detect overlapping role responsibilities."""

from __future__ import annotations

import math
from pathlib import Path

_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "that",
        "this",
        "with",
        "from",
        "what",
        "when",
        "where",
        "which",
        "will",
        "would",
        "could",
        "should",
        "have",
        "has",
        "had",
        "are",
        "was",
        "were",
        "been",
        "being",
        "into",
        "onto",
        "upon",
        "but",
        "not",
        "you",
        "your",
        "all",
        "any",
        "can",
        "may",
        "much",
        "more",
        "most",
        "only",
        "also",
        "then",
        "than",
    }
)


def load_role_docs(skills_dir: Path = Path(".claude/agents")) -> dict[str, str]:
    """Return {role_name: doc_text} for every .md file in skills_dir."""
    if not skills_dir.exists():
        return {}
    return {
        p.stem: p.read_text(encoding="utf-8", errors="replace")
        for p in skills_dir.iterdir()
        if p.suffix == ".md"
    }


def tokenize(text: str) -> list[str]:
    """Lowercase, split on non-word chars, drop tokens < 3 chars and stopwords."""
    import re

    raw = re.split(r"\W+", text.lower())
    return [t for t in raw if len(t) >= 3 and t not in _STOPWORDS]


def compute_tf(tokens: list[str]) -> dict[str, float]:
    """Term frequency normalized by doc length."""
    if not tokens:
        return {}
    counts: dict[str, int] = {}
    for t in tokens:
        counts[t] = counts.get(t, 0) + 1
    n = len(tokens)
    return {t: c / n for t, c in counts.items()}


def compute_idf(docs_tokens: dict[str, list[str]]) -> dict[str, float]:
    """IDF: log(N / df) where df = number of docs containing the term."""
    n = len(docs_tokens)
    if n == 0:
        return {}
    df: dict[str, int] = {}
    for tokens in docs_tokens.values():
        for t in set(tokens):
            df[t] = df.get(t, 0) + 1
    return {t: math.log(n / d) for t, d in df.items()}


def compute_tfidf(docs: dict[str, str]) -> dict[str, dict[str, float]]:
    """Return {role: {term: tfidf_score}} for every role doc."""
    tokenized = {role: tokenize(text) for role, text in docs.items()}
    idf = compute_idf(tokenized)
    result: dict[str, dict[str, float]] = {}
    for role, tokens in tokenized.items():
        tf = compute_tf(tokens)
        result[role] = {t: tf[t] * idf.get(t, 0.0) for t in tf}
    return result


def cosine_similarity(vec_a: dict[str, float], vec_b: dict[str, float]) -> float:
    """Standard cosine similarity. Returns 0.0 if either norm is 0."""
    dot = sum(vec_a.get(t, 0.0) * v for t, v in vec_b.items())
    norm_a = math.sqrt(sum(v * v for v in vec_a.values()))
    norm_b = math.sqrt(sum(v * v for v in vec_b.values()))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def audit_skills(
    skills_dir: Path = Path(".claude/agents"),
    threshold: float = 0.6,
) -> list[tuple[str, str, float]]:
    """Return sorted-desc list of (role_a, role_b, similarity) pairs >= threshold.

    role_a < role_b alphabetically so no duplicate pairs.
    """
    docs = load_role_docs(skills_dir)
    tfidf = compute_tfidf(docs)
    roles = sorted(tfidf.keys())
    pairs: list[tuple[str, str, float]] = []
    for i, a in enumerate(roles):
        for b in roles[i + 1 :]:
            sim = cosine_similarity(tfidf[a], tfidf[b])
            if sim >= threshold:
                pairs.append((a, b, sim))
    pairs.sort(key=lambda x: x[2], reverse=True)
    return pairs


def format_report(pairs: list[tuple[str, str, float]], threshold: float) -> str:
    """Markdown report with header, threshold line, and table."""
    lines = [
        "# Skill boundary audit",
        "",
        f"Threshold: {threshold}",
        "",
    ]
    if not pairs:
        lines.append("No role overlaps above threshold.")
        return "\n".join(lines)
    lines += [
        "| Role A | Role B | Similarity |",
        "|--------|--------|------------|",
    ]
    for a, b, sim in pairs:
        lines.append(f"| {a} | {b} | {sim:.4f} |")
    return "\n".join(lines)
