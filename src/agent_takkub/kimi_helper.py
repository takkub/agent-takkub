"""Kimi CLI (`kimi-cli`) helper — transcript resolution and record parsing.

Investigated 2026-08-22 (`docs/v2/2.0.0-migration-plan.md` §1.1, epic #309):
`ProviderSpec.supports_remote_history=False`/`produces_jsonl_transcript=False`
predate this investigation and describe the `remote/notify.py` mobile-mirror
gap (#103) — a DIFFERENT, still-open consumer this module does not touch.
What this module confirms is narrower but real: Kimi CLI 1.49.0 DOES persist
a locally-readable, per-work-dir session store, verified by reading a real
`~/.kimi` on this machine (not a fixture) and by reading kimi-cli's own
installed source (`kimi_cli/metadata.py`, `kimi_cli/session.py`,
`kimi_cli/wire/{file,types}.py` under its `uv tool install` site-packages) —
authoritative, typed Pydantic source, not a guess:

  ~/.kimi/kimi.json                                  — work-dir registry:
    {"work_dirs": [{"path": "<canonical cwd>", "kaos": "local",
                     "last_session_id": "<uuid>|null"}]}
  ~/.kimi/sessions/<md5(work_dir.path)>/<session-id>/wire.jsonl
                                                      — per-turn event log

`~/.kimi` is overridable via the `KIMI_SHARE_DIR` env var
(`kimi_cli/share.py::get_share_dir`).

wire.jsonl line shape (`kimi_cli/wire/file.py::WireMessageRecord`, confirmed
against a real recorded line from a genuine prior teammate task on this
machine):
    {"timestamp": <float>, "message": {"type": "<WireMessage class name>",
                                        "payload": {...}}}
(the first line is a `{"type": "metadata", ...}` header, not a message).

User turns arrive as `TurnBegin`/`SteerInput`, whose `payload.user_input` is
either a plain string or a list of `ContentPart`s (`kosong.message.TextPart`
→ `{"type": "text", "text": "..."}`). Assistant text arrives as a top-level
`TextPart` message (`kimi_cli/wire/__init__.py::_WireRecorder` subscribes to
the MERGED queue, so a recorded `TextPart` is already a complete block, not
a streaming delta). `ThinkPart` (hidden reasoning) and `ToolCall`/
`ToolCallPart` also flow through the same log — these are deliberately NOT
surfaced as messages here (provider-integration skill's "text only" rule).

GAP, disclosed rather than hidden: this machine's Kimi CLI has no default
model configured (`LLM not set`, reproduced live via `kimi --print` during
this investigation — the exact same failure the one real prior recorded
session shows: `TurnBegin` immediately followed by `TurnEnd` with nothing
between them), so no real assistant-authored `TextPart` line has been
observed on this machine to verify against. The parser below is built
directly from kimi-cli's own typed wire-protocol source rather than a
hand-fixture, which is the strongest verification available without a
working model — but per the provider-integration skill's schema-drift rule,
a real live example should still be captured and checked against the moment
one becomes available (e.g. once `kimi login`/model selection is completed
on a real cockpit) rather than assumed permanently correct.

Design rules (mirror codex_helper.py / cursor_helper.py):
- Best-effort. Any file/parse failure returns clean fallback, never raises.
- Read-only. No modifications to ~/.kimi/ from here.
- Cross-platform paths via pathlib.Path (macOS, Linux, Windows).
"""

from __future__ import annotations

import json
import os
from hashlib import md5
from pathlib import Path

_MAX_EVENT_CHARS = 16000
_HISTORY_MAX_BYTES = 256 * 1024

# `WireMessage` class names that carry assistant-authored text. `ThinkPart`
# (hidden reasoning) and every ToolCall*/Approval*/Hook*/Status* control
# message are deliberately excluded — never surfaced as a message.
_ASSISTANT_TEXT_TYPES = {"TextPart"}
_USER_TURN_TYPES = {"TurnBegin", "SteerInput"}


def kimi_share_dir() -> Path:
    """Return Kimi CLI's share directory (`~/.kimi`, or `$KIMI_SHARE_DIR`)."""
    override = os.environ.get("KIMI_SHARE_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".kimi"


def kimi_metadata_file() -> Path:
    return kimi_share_dir() / "kimi.json"


def normalize_kimi_cwd(value: object) -> str:
    """Best-effort match to `kaos.path.KaosPath.canonical()`: absolute,
    `.`/`..` normalized, symlinks NOT resolved, case and separators
    preserved as the OS reports them (`kimi.json` matches on exact string
    equality, so this must reproduce kimi-cli's own string, not a
    case/posix-folded one like `cursor_helper.normalize_cursor_cwd`)."""
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        return str(Path(os.path.abspath(value.strip())))
    except Exception:
        return value.strip()


def _load_work_dirs() -> list[dict]:
    try:
        data = json.loads(kimi_metadata_file().read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return []
    work_dirs = data.get("work_dirs") if isinstance(data, dict) else None
    return [wd for wd in work_dirs if isinstance(wd, dict)] if isinstance(work_dirs, list) else []


def _sessions_dir_for_cwd(cwd: str) -> Path | None:
    wanted = normalize_kimi_cwd(cwd)
    if not wanted:
        return None
    for wd in _load_work_dirs():
        if str(wd.get("path") or "") != wanted:
            continue
        digest = md5(wanted.encode("utf-8")).hexdigest()
        kaos = str(wd.get("kaos") or "local")
        basename = digest if kaos == "local" else f"{kaos}_{digest}"
        return kimi_share_dir() / "sessions" / basename
    return None


def resolve_kimi_session_dir(cwd: str, session_id: str | None = None) -> Path | None:
    """Resolve the session directory (containing `wire.jsonl`) for a Kimi
    session in *cwd*. With *session_id*, that exact session; otherwise the
    most recently modified session directory that has a `wire.jsonl`."""
    sessions_dir = _sessions_dir_for_cwd(cwd)
    if sessions_dir is None or not sessions_dir.is_dir():
        return None

    wanted_id = str(session_id or "").strip()
    if wanted_id:
        candidate = sessions_dir / wanted_id
        return candidate if (candidate / "wire.jsonl").is_file() else None

    best: Path | None = None
    best_mtime = -1.0
    try:
        for item in sessions_dir.iterdir():
            if not item.is_dir():
                continue
            wire = item / "wire.jsonl"
            if not wire.is_file():
                continue
            try:
                mtime = wire.stat().st_mtime
            except OSError:
                continue
            if mtime >= best_mtime:
                best = item
                best_mtime = mtime
    except OSError:
        return None
    return best


def _content_parts_text(parts: object) -> str:
    if isinstance(parts, str):
        return parts.strip()
    if not isinstance(parts, list):
        return ""
    out: list[str] = []
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                out.append(text.strip())
    return "\n".join(out)


def parse_kimi_wire_record(rec: object) -> tuple[str, str] | None:
    """Parse one decoded `wire.jsonl` data line (the `{"timestamp":...,
    "message": {...}}` envelope — NOT the first `{"type": "metadata", ...}`
    header line) into `(kind, text)`, `kind` one of `"me"`/`"lead"`. `None`
    for a control message (TurnEnd, StepBegin, ToolCall, ThinkPart, ...) or
    anything unparseable."""
    if not isinstance(rec, dict):
        return None
    message = rec.get("message")
    if not isinstance(message, dict):
        return None
    mtype = message.get("type")
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return None

    if mtype in _USER_TURN_TYPES:
        text = _content_parts_text(payload.get("user_input"))
        return ("me", text) if text else None

    if mtype in _ASSISTANT_TEXT_TYPES:
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            return "lead", text.strip()
        return None

    return None


def read_recent_kimi_messages(path: Path, limit: int = 200) -> list[dict]:
    """Read recent user/assistant text turns from a Kimi `wire.jsonl`."""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    truncated = size > _HISTORY_MAX_BYTES
    try:
        with path.open("rb") as fh:
            if truncated:
                fh.seek(size - _HISTORY_MAX_BYTES)
            raw = fh.read()
    except OSError:
        return []
    lines = raw.split(b"\n")
    if truncated:
        lines = lines[1:]

    out: list[dict] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        parsed = parse_kimi_wire_record(rec)
        if parsed is None:
            continue
        kind, text = parsed
        out.append({"text": text[:_MAX_EVENT_CHARS], "kind": kind})
    return out[-limit:]
