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
    next_action: str = ""


def _headline(note: str) -> str:
    stripped = note.strip()
    return stripped.splitlines()[0].strip() if stripped else ""


def _prepend_unique(items: list[str], entry: str) -> list[str]:
    rest = [x for x in items if x != entry]
    return ([entry, *rest])[:_MAX_ITEMS]


def apply_done_note(
    summary: RollingSummary, *, role: str, note: str, failed: bool
) -> RollingSummary:
    headline = _headline(note)
    if not headline:
        return summary
    entry = f"[{role}] {headline}"
    if failed:
        return replace(
            summary,
            current_state=entry,
            pending=_prepend_unique(summary.pending, entry),
            warnings=_prepend_unique(summary.warnings, entry),
            next_action=f"fix: {entry}",
        )
    return replace(
        summary,
        current_state=entry,
        completed=_prepend_unique(summary.completed, entry),
        in_progress=[x for x in summary.in_progress if x != entry][:_MAX_ITEMS],
        pending=[x for x in summary.pending if x != entry][:_MAX_ITEMS],
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
