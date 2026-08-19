"""`RollingSummary` — structured per-conversation summary (plan §6:
"Summary ควรเป็น structured data ไม่ใช่ paragraph เดียว"). Field names match
the blueprint's `objective/currentState/decisions/completed/inProgress/
pending/importantFiles/warnings/next` (snake_case, Python convention).

`apply_done_note` extends `orchestrator._condense_done_note`'s "first line
is the headline" heuristic into an update rule instead of a one-shot
render: a clean `done()` promotes the headline into `completed` (and clears
any matching `in_progress`/`pending` entry), a failed one records it as a
`warning` and leaves it in `pending`. Bounded lists (`_MAX_ITEMS`) keep the
summary itself small — the whole point of a rolling summary vs. the raw log.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from agent_takkub.core.storage.legacy_reader import read_json

from ._json_io import write_json_atomic

_MAX_ITEMS = 20

# Prefixes a done note's author uses to flag a line as a decision/lesson
# worth surfacing in the structured summary instead of just riding along in
# `completed`/`current_state` as undifferentiated text. Shared with
# `core.brain.sources.reflection_source` (imports these directly) so the two
# call sites never drift on what counts as a decision line.
DECISION_PREFIXES: tuple[str, ...] = ("DECISION:", "ตัดสินใจ:", "decided:")
LESSON_PREFIXES: tuple[str, ...] = ("LESSON:", "บทเรียน:")


@dataclass(frozen=True, slots=True)
class RollingSummary:
    objective: str = ""
    current_state: str = ""
    decisions: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    in_progress: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    important_files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    next_action: str = ""


def _headline(note: str) -> str:
    stripped = note.strip()
    return stripped.splitlines()[0].strip() if stripped else ""


def _prepend_unique(items: list[str], entry: str) -> list[str]:
    rest = [x for x in items if x != entry]
    return ([entry, *rest])[:_MAX_ITEMS]


_ALL_PREFIXES: tuple[str, ...] = DECISION_PREFIXES + LESSON_PREFIXES


def _prefix_spans(text: str, prefixes: tuple[str, ...]) -> list[tuple[int, int]]:
    """(start, end) of every occurrence of a *prefixes* member in *text*,
    sorted by position. A match must sit on a word boundary — the char right
    before it (if any) must not be alnum — so "undecided:" doesn't match
    "decided:" and bare "decision" (no colon) never matches at all."""
    lowered = text.casefold()
    spans: list[tuple[int, int]] = []
    for prefix in prefixes:
        needle = prefix.casefold()
        start = 0
        while (idx := lowered.find(needle, start)) != -1:
            if idx == 0 or not text[idx - 1].isalnum():
                spans.append((idx, idx + len(prefix)))
            start = idx + 1
    spans.sort()
    return spans


def extract_prefixed_lines(note: str, prefixes: tuple[str, ...]) -> list[str]:
    """Every occurrence of one of *prefixes* in *note* (English matched
    case-insensitively, Thai exactly), whether it opens a line or sits mid
    line — a done note is often a single line like "E2E ok. DECISION: foo.
    LESSON: bar". Each match's segment runs from right after the prefix to
    the next DECISION/LESSON-family prefix (any of them, not just this
    call's *prefixes*) or end of text, with trailing "."/whitespace trimmed."""
    text = note or ""
    spans = _prefix_spans(text, _ALL_PREFIXES)
    wanted = {p.casefold() for p in prefixes}
    out: list[str] = []
    for i, (start, end) in enumerate(spans):
        if text[start:end].casefold() not in wanted:
            continue
        segment_end = spans[i + 1][0] if i + 1 < len(spans) else len(text)
        segment = text[end:segment_end].strip().rstrip(".").strip()
        if segment:
            out.append(segment)
    return out


def apply_done_note(
    summary: RollingSummary, *, role: str, note: str, failed: bool
) -> RollingSummary:
    headline = _headline(note)
    if not headline:
        return summary
    entry = f"[{role}] {headline}"
    decisions = summary.decisions
    for line in extract_prefixed_lines(note, DECISION_PREFIXES):
        decisions = _prepend_unique(decisions, f"[{role}] {line}")
    lessons = summary.lessons
    for line in extract_prefixed_lines(note, LESSON_PREFIXES):
        lessons = _prepend_unique(lessons, f"[{role}] {line}")
    if failed:
        return replace(
            summary,
            current_state=entry,
            pending=_prepend_unique(summary.pending, entry),
            warnings=_prepend_unique(summary.warnings, entry),
            next_action=f"fix: {entry}",
            decisions=decisions,
            lessons=lessons,
        )
    return replace(
        summary,
        current_state=entry,
        completed=_prepend_unique(summary.completed, entry),
        in_progress=[x for x in summary.in_progress if x != entry][:_MAX_ITEMS],
        pending=[x for x in summary.pending if x != entry][:_MAX_ITEMS],
        decisions=decisions,
        lessons=lessons,
    )


def summary_path(conversation_dir: Path) -> Path:
    return conversation_dir / "summary.json"


def load_summary(conversation_dir: Path) -> RollingSummary:
    raw = read_json(summary_path(conversation_dir))
    if not raw:
        return RollingSummary()
    return RollingSummary(**{f: raw.get(f, default) for f, default in _defaults().items()})


def save_summary(conversation_dir: Path, summary: RollingSummary) -> None:
    write_json_atomic(summary_path(conversation_dir), asdict(summary))


def _defaults() -> dict[str, object]:
    blank = RollingSummary()
    return {f: getattr(blank, f) for f in blank.__dataclass_fields__}
