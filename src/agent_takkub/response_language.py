"""Persisted team-response-language setting (#621).

Lead across projects answered/summarized in Thai sometimes, English other
times, with no rule anywhere telling any role which to use — grep across
`orchestrator_text.py`, `agents/*.md`, and Lead's rendered context turned up
nothing (#621 root cause). This module is the single source of truth for
"what language should the team answer in", consumed two ways:

1. `prompt_directive()` — the one-sentence instruction spawn-time context
   rendering (`lead_context.py`, `spawn_engine.py`) injects into every role's
   prompt, every provider, so the rule actually reaches the model.
2. `should_nudge(note)` — a soft heuristic `Orchestrator.done()`/`progress()`
   uses to warn (never reject) a pane whose note ignored the setting.

Mirrors `theme_settings`'s shape: durable JSON under `config.SETTINGS_HOME`,
one value per machine (no per-project override — same as the machine-mode
and pane-discard controls living on the same Settings → General page).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import config

MODES = ("th", "en", "as-typed")
DEFAULT_MODE = "th"

_LABELS = {
    "th": "ไทย (Thai)",
    "en": "English",
    "as-typed": "ตามที่ผู้ใช้พิมพ์ (match the user's message)",
}


def path() -> Path:
    return config.SETTINGS_HOME / "response-language.json"


def load(settings_path: Path | None = None) -> str:
    """Return the persisted mode, or :data:`DEFAULT_MODE` when the file is
    missing/invalid — never raises."""
    target = settings_path or path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        mode = str(payload.get("mode", "")).strip().lower()
        return mode if mode in MODES else DEFAULT_MODE
    except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return DEFAULT_MODE


def save(mode: str, settings_path: Path | None = None) -> bool:
    if mode not in MODES:
        raise ValueError(f"unknown response-language mode: {mode!r}")
    target = settings_path or path()
    target.parent.mkdir(parents=True, exist_ok=True)
    return config._write_json_atomic(target, {"schema_version": 1, "mode": mode})


def label(mode: str) -> str:
    return _LABELS.get(mode, mode)


# Injected verbatim into every role's spawn prompt (Lead + specialist, every
# provider) so the model actually sees the rule instead of guessing from
# whatever language the latest message/file happened to be in.
_TH_DIRECTIVE = (
    "ตอบ/สรุป/done note เป็นภาษาไทยเสมอ — คง identifier, คำสั่ง, path, ชื่อไฟล์, ข้อความ log เป็นอังกฤษ"
)
_EN_DIRECTIVE = (
    "Always respond, summarize, and write done/progress notes in English — "
    "keep identifiers, commands, paths, filenames, and log text unchanged."
)


def prompt_directive(mode: str | None = None) -> str | None:
    """The one-sentence language instruction for spawn-time context, or
    ``None`` under ``"as-typed"`` (match the user's own language — no
    override needed)."""
    mode = mode if mode is not None else load()
    if mode == "th":
        return _TH_DIRECTIVE
    if mode == "en":
        return _EN_DIRECTIVE
    return None


_THAI_CHAR_RE = re.compile(r"[฀-๿]")
# Below this length a note is plausibly just an identifier/command/path
# ("fixed #603", "tests green") — not worth nudging over.
_NUDGE_MIN_LEN = 200


def note_needs_language_nudge(note: str) -> bool:
    """Heuristic (#621): true when *note* is long and has no Thai
    characters at all — a done/progress note that's plausibly English prose,
    not just an identifier/command/path/log line quoted in an otherwise-Thai
    note (those keep the note legitimately mixed, not English-only)."""
    if not note or len(note) <= _NUDGE_MIN_LEN:
        return False
    return _THAI_CHAR_RE.search(note) is None


LANGUAGE_NUDGE = (
    "⚠️ ตั้งค่าภาษาที่ให้ทีมตอบไว้เป็นไทย (Settings → General) แต่ note นี้เป็นอังกฤษล้วน "
    "— ครั้งหน้าสรุปเป็นไทย (คง identifier/คำสั่ง/path/log เป็นอังกฤษได้ตามปกติ)"
)


def should_nudge(note: str, mode: str | None = None) -> bool:
    """True when the current setting is Thai and *note* trips the
    English-only heuristic above — the caller still never rejects on this,
    only appends :data:`LANGUAGE_NUDGE` to its reply."""
    mode = mode if mode is not None else load()
    return mode == "th" and note_needs_language_nudge(note)
