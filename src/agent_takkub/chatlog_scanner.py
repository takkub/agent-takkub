"""Read-only scanner over Claude Code's per-project session logs.

Every Claude Code session writes a JSONL file under
`~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`. Each line is
one record from the conversation: user messages, assistant turns
(with `tool_use` blocks), `tool_result` outputs, system reminders
(hooks like ECC GateGuard, cost-critical), plus bookkeeping
(`queue-operation`, `ai-title`, `last-prompt`, `attachment`,
`file-history-snapshot`).

This module is the foundation other cockpit features (hook noise
meter, friction heatmap, `takkub search`, decision timeline,
auto-resume brief) stand on. It does NOT decide what to do with
the data — it just walks the files, parses records cleanly, and
exposes helpers for the text extraction every downstream feature
needs.

Design rules:
- Read-only. No writes to ~/.claude/ from here.
- Best-effort. A corrupt line skips, never raises.
- Lazy. Iterators all the way; full files are big (this session's
  jsonl is ~9k lines).
- No external deps beyond stdlib.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime
from typing import Iterator


def claude_projects_dir() -> pathlib.Path:
    """Where Claude Code stores per-project session jsonl files."""
    return pathlib.Path.home() / ".claude" / "projects"


def decode_project_dir(name: str) -> pathlib.Path:
    """Turn an encoded folder name back into the original cwd path.

    Claude Code encodes a cwd like `C:\\Users\\monch\\WebstormProjects\\foo`
    as the folder name `C--Users-monch-WebstormProjects-foo`. We
    reverse the dash-mapping: leading `C--` → drive letter + `:`, then
    every remaining `-` is a path separator.
    """
    if not name:
        return pathlib.Path(".")
    raw = name
    if len(raw) >= 3 and raw[1:3] == "--":
        # Drive letter prefix, e.g. "C--Users-monch-..." → "C:/Users/monch/..."
        drive = raw[0]
        rest = raw[3:].replace("-", "/")
        return pathlib.Path(f"{drive}:/{rest}")
    return pathlib.Path("/" + raw.replace("-", "/"))


def iter_session_files(
    project_filter: str | None = None,
    *,
    since: datetime | None = None,
) -> Iterator[pathlib.Path]:
    """Yield jsonl files across all projects.

    `project_filter` matches against the decoded cwd path as a
    substring (case-insensitive) so callers can say
    `iter_session_files("agent-takkub")`. None returns everything.

    `since` filters by mtime — only files modified at or after that
    moment are yielded. Useful for "today's sessions" sweeps.
    """
    base = claude_projects_dir()
    if not base.is_dir():
        return
    needle = project_filter.lower() if project_filter else None
    since_ts = since.timestamp() if since is not None else None
    for project_dir in base.iterdir():
        if not project_dir.is_dir():
            continue
        if needle is not None:
            decoded = str(decode_project_dir(project_dir.name)).lower()
            if needle not in decoded and needle not in project_dir.name.lower():
                continue
        for jsonl in project_dir.glob("*.jsonl"):
            if since_ts is not None:
                try:
                    if jsonl.stat().st_mtime < since_ts:
                        continue
                except OSError:
                    continue
            yield jsonl


def iter_records(path: pathlib.Path) -> Iterator[dict]:
    """Yield one parsed dict per line. Bad lines are skipped silently."""
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def is_conversation_record(rec: dict) -> bool:
    """True if the record is a real user/assistant message (vs.
    bookkeeping records like queue-operation, ai-title, etc.). The
    downstream features that mine text only care about these."""
    t = rec.get("type", "")
    if t not in ("user", "assistant"):
        return False
    msg = rec.get("message")
    return isinstance(msg, dict)


def is_system_reminder(rec: dict) -> bool:
    """True if the record is a `system` reminder line — typically a
    hook firing (ECC GateGuard, cost-critical, rtk, etc.)."""
    return rec.get("type") == "system"


def extract_text(rec: dict) -> str:
    """Concatenate all human-readable text in a conversation record.

    Walks `message.content` (list of blocks) and pulls `text` from
    every text block. Tool calls and tool results contribute their
    `input`/`output` text too so a `takkub search` for a file path
    or a flag name catches tool-use records as well.

    Returns "" for non-conversation records or empty content.
    """
    if not is_conversation_record(rec):
        return ""
    msg = rec["message"]
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        if btype == "text":
            text = block.get("text") or ""
            if text:
                parts.append(text)
        elif btype == "thinking":
            text = block.get("thinking") or block.get("text") or ""
            if text:
                parts.append(text)
        elif btype == "tool_use":
            # tool name + JSON-encoded args so a search for a file
            # path or a CLI flag catches tool calls too.
            name = block.get("name") or ""
            args = block.get("input")
            try:
                args_text = json.dumps(args, ensure_ascii=False) if args is not None else ""
            except TypeError:
                args_text = str(args) if args is not None else ""
            parts.append(f"[tool_use {name}] {args_text}")
        elif btype == "tool_result":
            out = block.get("content")
            if isinstance(out, list):
                for sub in out:
                    if isinstance(sub, dict) and sub.get("type") == "text":
                        text = sub.get("text") or ""
                        if text:
                            parts.append(text)
            elif isinstance(out, str):
                parts.append(out)
    return "\n".join(parts)


def record_timestamp(rec: dict) -> datetime | None:
    """Parse the record's ISO8601 timestamp into a datetime, or None
    if the field is missing/unparseable. Used by features that filter
    "today's" records or sort by chronology."""
    ts = rec.get("timestamp")
    if not isinstance(ts, str):
        return None
    # The files use trailing 'Z' for UTC; datetime.fromisoformat needs
    # +00:00 in older 3.10 builds.
    cleaned = ts.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def role_of(rec: dict) -> str | None:
    """Return `message.role` if present, else None."""
    msg = rec.get("message")
    if isinstance(msg, dict):
        role = msg.get("role")
        if isinstance(role, str):
            return role
    return None


def tool_uses(rec: dict) -> list[dict]:
    """Yield the `tool_use` blocks inside an assistant message. Used
    by friction-heatmap to detect retry storms (same tool called many
    times with similar args)."""
    if not is_conversation_record(rec):
        return []
    msg = rec["message"]
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    out: list[dict] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            out.append(block)
    return out


# Substring → bucket name. Order matters: more specific patterns
# first so a "COST CRITICAL" line under an ECC hook bucket as
# `ecc-cost-monitor`, not the generic catch-all. Buckets surface in
# hot.md so the user can spot which hook is noisier than useful and
# decide whether to add it to ECC_DISABLED_HOOKS.
_HOOK_PATTERNS: list[tuple[str, str]] = [
    ("Fact-Forcing Gate", "ecc-gateguard"),
    ("gateguard-fact-force", "ecc-gateguard"),
    ("COST CRITICAL", "ecc-cost-monitor"),
    ("ecc-context-monitor", "ecc-cost-monitor"),
    ("LOOP WARNING", "ecc-loop-warning"),
    ("StrategicCompact", "ecc-strategic-compact"),
    ("rtk init", "rtk-hint"),
    ("claude-obsidian", "claude-obsidian"),
    ("PostCompact", "post-compact-hook"),
    ("SessionStart:", "session-start-hook"),
    ("TodoWrite tool hasn't been used", "todowrite-nag"),
]


def classify_hook(text: str) -> str | None:
    """Map a system-reminder text to a hook bucket name, or None when
    none of the known patterns match. Case-sensitive on purpose —
    hook IDs and the bracket text Claude Code injects are stable, and
    a fuzzy match would over-bucket harmless system reminders."""
    if not text:
        return None
    for needle, bucket in _HOOK_PATTERNS:
        if needle in text:
            return bucket
    return None


def count_hook_fires(
    project_filter: str | None = None, *, since: datetime | None = None
) -> dict[str, int]:
    """Walk session jsonls and count system-reminder records per hook
    bucket. `since` is typically set to start-of-today so the hot.md
    section reflects "noise today" rather than lifetime.

    Returns a dict like `{"ecc-gateguard": 47, "ecc-cost-monitor": 62}`.
    Empty when no buckets matched (or no jsonls under the filter).
    """
    counts: dict[str, int] = {}
    for jsonl in iter_session_files(project_filter, since=since):
        for rec in iter_records(jsonl):
            if not is_system_reminder(rec):
                continue
            body = system_reminder_text(rec)
            bucket = classify_hook(body)
            if bucket is None:
                continue
            counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def system_reminder_text(rec: dict) -> str:
    """Extract the text body of a `system` reminder line. Hook fires
    surface here with the hook id buried in the body — the hook noise
    meter counts those substrings."""
    if not is_system_reminder(rec):
        return ""
    content = rec.get("content")
    if isinstance(content, str):
        return content
    msg = rec.get("message")
    if isinstance(msg, dict):
        body = msg.get("content")
        if isinstance(body, str):
            return body
        if isinstance(body, list):
            parts = []
            for b in body:
                if isinstance(b, dict) and b.get("type") == "text":
                    parts.append(b.get("text", ""))
            return "\n".join(parts)
    return ""
