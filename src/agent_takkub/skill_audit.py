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
    """Return {role_name: doc_text} for every readable .md file in skills_dir.

    ``skills_dir`` defaults to a cwd-relative path (only correct when run
    from the repo root). If that doesn't exist, fall back to
    ``config.AGENTS_DIR`` — the cockpit's real role-file location in both a
    dev checkout and an installed build (see
    docs/audit/2026-07-05-installed-build-audit-gemini.md, finding 5).
    Filesystem races and unreadable entries are skipped; this function never
    raises for directory enumeration or file-read failures.
    """
    if not skills_dir.exists():
        from .config import AGENTS_DIR

        skills_dir = AGENTS_DIR
    if not skills_dir.exists():
        return {}
    try:
        entries = list(skills_dir.iterdir())
    except OSError:
        return {}
    docs: dict[str, str] = {}
    for path in entries:
        if path.suffix != ".md":
            continue
        try:
            docs[path.stem] = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return docs


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


def load_all_role_docs() -> dict[str, str]:
    """Merge built-in (`config.AGENTS_DIR`) + custom (`config.CUSTOM_AGENTS_DIR`,
    A6 user-created roles) docs into one corpus. Custom entries win on a name
    collision, though `custom_roles.validate_role_name` already rejects
    collisions with built-in names at creation time, so this should never
    actually trigger in practice."""
    from .config import AGENTS_DIR, CUSTOM_AGENTS_DIR

    docs = load_role_docs(AGENTS_DIR)
    docs.update(load_role_docs(CUSTOM_AGENTS_DIR))
    return docs


def audit_new_role_text(
    name: str,
    text: str,
    threshold: float = 0.6,
    existing: dict[str, str] | None = None,
) -> list[tuple[str, float]]:
    """Overlap of a NOT-YET-SAVED candidate role doc against every existing
    role. Used by the New-Role dialog to warn before the user commits a
    name/instructions that heavily duplicate an existing role's territory.

    Returns [(other_role, similarity), ...] >= threshold, sorted desc.
    `existing` lets callers pass an already-loaded corpus (e.g. the dialog
    caches it once per open) instead of re-reading the filesystem per
    keystroke; defaults to `load_all_role_docs()`.
    """
    docs = dict(existing if existing is not None else load_all_role_docs())
    docs[name] = text
    tfidf = compute_tfidf(docs)
    target = tfidf.get(name, {})
    out: list[tuple[str, float]] = []
    for other, vec in tfidf.items():
        if other == name:
            continue
        sim = cosine_similarity(target, vec)
        if sim >= threshold:
            out.append((other, sim))
    out.sort(key=lambda x: x[1], reverse=True)
    return out


def audit_existing_role(
    role: str,
    docs: dict[str, str],
    threshold: float = 0.6,
) -> list[tuple[str, float]]:
    """Overlap of an EXISTING role's doc (already a key in ``docs``) against
    every other role in the same corpus. Used by the Skill Catalog view to
    show "✓ won't overlap" for the currently-selected role.

    Returns [(other_role, similarity), ...] >= threshold, sorted desc.
    """
    tfidf = compute_tfidf(docs)
    target = tfidf.get(role, {})
    out: list[tuple[str, float]] = []
    for other, vec in tfidf.items():
        if other == role:
            continue
        sim = cosine_similarity(target, vec)
        if sim >= threshold:
            out.append((other, sim))
    out.sort(key=lambda x: x[1], reverse=True)
    return out


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
