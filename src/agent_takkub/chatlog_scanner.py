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
from collections.abc import Iterator
from datetime import datetime


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


# Substring tokens that signal user dissatisfaction or correction.
# Thai + English mix because the cockpit is Thai-led but the user
# also throws English at it occasionally. Case-sensitive: we want
# "wrong" to match but not "Wrongful" — narrow phrases over broad
# stems. Add new tokens conservatively; over-broad words like "no"
# would over-count benign answers.
_CORRECTION_PATTERNS: tuple[str, ...] = (
    "ไม่ใช่",
    "ไม่ถูก",
    "ผิด",
    "พังเลย",
    "เอาออก",
    "ลบให้หมด",
    "เลิก",
    "อะไรว่ะ",
    "บ่อยเกิ้น",
    "ใช้ ไม่ได้",
    "ใช้ไม่ได้",
    "เหมือนเดิม",
    "wrong",
    "not what i",
    "revert",
    "undo",
    "broke ",
    "broken",
    "stop doing",
)


def _user_text_only(rec: dict) -> str:
    """Return user-typed text only — skipping `tool_result` blocks
    which are user-role records but generated by claude's tools, not
    by the human. Used by friction counters that care about human
    dissatisfaction signals."""
    if not is_conversation_record(rec):
        return ""
    if role_of(rec) != "user":
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
    return "\n".join(parts)


def count_user_corrections(
    project_filter: str | None = None, *, since: datetime | None = None
) -> int:
    """Walk user messages and count ones containing any
    `_CORRECTION_PATTERNS` token. Each matching record counts once
    regardless of how many tokens it contains — a single frustrated
    message shouldn't multi-count.

    These are friction signals: the user had to push back on what
    claude did. A high count on a project says the workflow there
    isn't smooth and the project page / CLAUDE.md needs tightening.
    """
    n = 0
    for jsonl in iter_session_files(project_filter, since=since):
        for rec in iter_records(jsonl):
            text = _user_text_only(rec)
            if not text:
                continue
            t_lower = text.lower()
            for needle in _CORRECTION_PATTERNS:
                if needle in text or needle.lower() in t_lower:
                    n += 1
                    break
    return n


def _tool_use_signature(block: dict) -> tuple[str, str]:
    """Stable (tool_name, json-sorted-args) tuple used to detect when
    the same tool is fired with identical args back-to-back."""
    name = block.get("name") or ""
    args = block.get("input")
    try:
        sig = json.dumps(args, sort_keys=True, ensure_ascii=False)
    except TypeError:
        sig = repr(args)
    return name, sig


def count_tool_retries(project_filter: str | None = None, *, since: datetime | None = None) -> int:
    """Walk every assistant message and count "retry storms" —
    sequences where the same tool_use signature fires 3+ times in a
    row. Returns the total number of storm events (one per storm,
    not per repeat). Detects situations like a failing Edit that the
    assistant kept retrying without changing the args.
    """
    storms = 0
    for jsonl in iter_session_files(project_filter, since=since):
        last_sig: tuple[str, str] | None = None
        repeat = 0
        for rec in iter_records(jsonl):
            for block in tool_uses(rec):
                sig = _tool_use_signature(block)
                if sig == last_sig:
                    repeat += 1
                    if repeat == 2:
                        # 3rd identical call (counts: first sets sig,
                        # second bumps to 1, third bumps to 2) → storm
                        storms += 1
                else:
                    last_sig = sig
                    repeat = 0
    return storms


def _first_h2_heading(text: str) -> str | None:
    """Return the first H2 line (`## something`) from a multi-line
    string, stripped of the leading `## ` marker. Returns None if no
    H2 is present. Skipping H1 (`# `) is deliberate — our assistant
    side uses H1 for top-level reply titles which are usually noise
    for a decision timeline."""
    for line in text.splitlines():
        s = line.lstrip()
        if s.startswith("## "):
            return s[3:].strip()
    return None


def extract_decisions(
    project_filter: str | None = None,
    *,
    since: datetime | None = None,
    limit: int = 10,
) -> list[dict]:
    """Pull assistant messages from today's sessions that look like
    decision/summary points — heuristic: the message contains at
    least one `## ` H2 heading. Returns a list (most recent first)
    of {timestamp, project, heading, snippet} suitable for embedding
    in the daily digest.

    The daily digest only has room for a handful of these, so
    `limit` keeps the section short. A noisy assistant turn might
    contain 5 H2s — we still count the *message* once, surfacing the
    first heading.
    """
    out: list[dict] = []
    for jsonl in iter_session_files(project_filter, since=since):
        for rec in iter_records(jsonl):
            if not is_conversation_record(rec):
                continue
            if role_of(rec) != "assistant":
                continue
            text = extract_text(rec)
            heading = _first_h2_heading(text)
            if heading is None:
                continue
            out.append(
                {
                    "timestamp": rec.get("timestamp") or "",
                    "project": jsonl.parent.name,
                    "heading": heading,
                    "snippet": _snippet_around(text, "## ", width=160),
                }
            )
    out.sort(key=lambda h: h.get("timestamp") or "", reverse=True)
    return out[:limit]


def build_resume_brief(
    project_filter: str | None = None,
    *,
    last_n: int = 20,
    since: datetime | None = None,
) -> str:
    """Return a markdown blob summarising the last `last_n`
    conversation records for `project_filter`. Drives the
    auto-resume brief written to vault on cockpit close so the next
    session can read it and pick up where the user left off.

    Format: a brief intro header, then a chronological bullet list
    (oldest first, most recent at the bottom) of user/assistant
    turns with timestamps and first-line snippets.

    Returns "" when no jsonls match or no conversation records were
    found — caller can decide whether to write an empty brief or
    skip the write entirely.
    """
    # Walk every jsonl matching the filter, collect every
    # conversation record with its timestamp. Sort by timestamp so
    # records from multiple jsonls (multi-session day) interleave
    # correctly.
    collected: list[tuple[str, str, str]] = []  # (ts, role, first_line)
    for jsonl in iter_session_files(project_filter, since=since):
        for rec in iter_records(jsonl):
            if not is_conversation_record(rec):
                continue
            role = role_of(rec) or "?"
            text = extract_text(rec)
            if not text:
                continue
            first_line = " ".join(text.split())[:160]
            ts = rec.get("timestamp") or ""
            collected.append((ts, role, first_line))
    if not collected:
        return ""
    collected.sort(key=lambda t: t[0])
    tail = collected[-last_n:]
    lines: list[str] = []
    proj_label = project_filter or "all projects"
    lines.append(f"# Resume brief — {proj_label}")
    lines.append("")
    lines.append(f"Last {len(tail)} exchanges (oldest first):")
    lines.append("")
    for ts, role, text in tail:
        ts_short = ts.replace("T", " ")[:16] if ts else ""
        lines.append(f"- `{ts_short}` **{role}** — {text}")
    return "\n".join(lines) + "\n"


def search_sessions(
    query: str,
    project_filter: str | None = None,
    *,
    since: datetime | None = None,
    limit: int = 20,
) -> list[dict]:
    """Substring grep across Claude Code session jsonls.

    Returns a list of dicts (most recent first) shaped like:
      {"project": "<encoded folder>", "path": "<file>",
       "timestamp": "<iso>", "role": "user|assistant",
       "snippet": "<text around the match>"}

    The snippet is the line's full text trimmed to ~200 chars
    centred-ish on the match so the CLI display has context. Case-
    insensitive. `limit` caps the result list so a generic word
    doesn't return thousands of hits — the CLI surfaces a footer
    when truncated.

    System reminders are skipped (the hook-noise meter is the right
    surface for those) so search results stay focused on real
    conversation content.
    """
    if not query:
        return []
    needle = query.lower()
    hits: list[dict] = []
    for jsonl in iter_session_files(project_filter, since=since):
        for rec in iter_records(jsonl):
            text = extract_text(rec)
            if not text:
                continue
            if needle not in text.lower():
                continue
            snippet = _snippet_around(text, query)
            hits.append(
                {
                    "project": jsonl.parent.name,
                    "path": str(jsonl),
                    "timestamp": rec.get("timestamp") or "",
                    "role": role_of(rec) or "",
                    "snippet": snippet,
                }
            )
    # Most recent first (by timestamp string — ISO8601 sorts right).
    hits.sort(key=lambda h: h.get("timestamp") or "", reverse=True)
    return hits[:limit]


def _snippet_around(text: str, query: str, width: int = 200) -> str:
    """Return ~width chars from `text` centred on the first match.
    Collapses whitespace so the result fits on one terminal line."""
    flat = " ".join(text.split())
    flat_lower = flat.lower()
    idx = flat_lower.find(query.lower())
    if idx < 0:
        return flat[:width]
    half = width // 2
    start = max(0, idx - half)
    end = min(len(flat), idx + len(query) + half)
    snippet = flat[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(flat):
        snippet = snippet + "…"
    return snippet


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
