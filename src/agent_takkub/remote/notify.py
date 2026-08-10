"""notify.py — bridges Lead-level orchestrator events into the SSE
broadcaster (§6.5, X-check 2.1 — hooks confirmed against the running
orchestrator, not guessed):

* done events: `orch.agentDone` (orchestrator.py, emitted on every
  `takkub done`).
* live Lead output: dispatches through the current provider's registered
  history scanner. Claude's scanner tails its structured session JSONL —
  `<CLAUDE_CONFIG_DIR>/projects/<encoded-cwd>/<uuid>.jsonl` (the same store
  `chatlog_scanner.py` / `takkub search` read) — instead of scraping raw PTY
  bytes. Providers without a compatible scanner degrade to an empty history
  and working/idle state only; they never fall through to Claude's files.

Why the switch (mobile junk-elimination, proven not guessed): a raw Lead
transcript is TUI-redraw churn (`\\r`=4200, `\\n`=0 in a real capture) — the
spinner, startup splash, resume menu and cursor-redraw shrapnel a regex
filter can reduce but never fully eliminate. Claude Code's own JSONL event
log is the same conversation with none of that: `type=="assistant"` records
carry `message.content[]` blocks, and only `type=="text"` blocks are real
reply prose — no spinner, no box-drawing, no ANSI, ever. Reading that
instead of the pty stream makes the junk-filter obsolete rather than better.

Session resolution: `Orchestrator._pane_state[_exit_key(project_ns, "lead")]`
carries `session_uuid` (stamped at spawn — spawn_engine.py's `--session-id`/
`--resume`). A UUID is unique across the whole `~/.claude` (or profile-
isolated `<DATA_HOME>/claude-config`) store, so the file is found by
`glob("*/{uuid}.jsonl")` under `user_profile.config_dir_for(project_ns) /
"projects"` — the exact `CLAUDE_CONFIG_DIR` that project's panes were
spawned with (`pane_env.inject_user_profile_env`), so a project pinned to a
non-default profile still resolves correctly.

Runs entirely on the Qt main thread (constructed inside
`RemoteControl._start`) — a normal Qt object on a normal `QTimer`, not
something a handler thread ever touches. Each poll tick only reads the byte
range appended since the last tick (per-project `offset`), never re-reads
the whole file — a Lead session log can grow into the tens of MB over a
long run.

Multi-project (project picker): every open project's Lead session is tailed
independently, each stamped with its own `project_ns` at emit time — no
shared "current project" pointer a mid-poll switch could mis-stamp.

Cross-project isolation (H-A): `orch.agentDone` fires for *every* project,
not just the active one, so every push is stamped with the event's own
`project_ns` and `SSEBroadcaster.push` drops it for any client whose ticket
was issued for a different project. Live Lead output is stamped the same
way, per-project, for the same reason.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer

from .. import gemini_helper
from ..orchestrator_text import _exit_key
from ..user_profile import config_dir_for
from . import config as _remote_config

# Per-message cap for a single Lead reply (live SSE event, history entry, and
# done note). Generous on purpose — the phone should show the WHOLE message
# (long plans/tables/summaries included), not a cut-off fragment. Still bounded
# so one pathological megabyte reply can't blow up the SSE payload / mobile DOM.
_MAX_EVENT_CHARS = 16000
# JSONL tail poll cadence. 200 ms (down from 500) so a completed Lead text
# block reaches the phone ~2.5x sooner, closing the perceived lag vs the
# desktop's live stream. Only a stat + delta-read per open project — cheap.
# Cannot go token-by-token: the JSONL holds whole records, and streaming raw
# PTY bytes (the pre-rewrite source) is exactly the TUI junk we removed.
_POLL_MS = 200
_DEFAULT_HISTORY_LIMIT = 200
# History reads are one-shot (reconnect/project-switch), not the live poll
# tail, but a long-running Lead session's JSONL can grow into the tens of
# MB — bound how much of it a single request reads instead of loading the
# whole file every time. 8 MB comfortably covers 200 assistant replies plus
# the tool_use/tool_result/thinking records interleaved between them.
_HISTORY_MAX_BYTES = 8 * 1024 * 1024


@dataclass
class _Tail:
    """Per-project incremental-read state for one Lead session's JSONL."""

    path: Path
    session_uuid: str
    provider: str = "claude"
    offset: int = 0
    # bytes held back from the previous read because they didn't end in a
    # `\n` yet — Claude Code writes one JSON object per line, and a poll can
    # land mid-write.
    partial: bytes = b""


# Map a Claude tool name → a coarse activity category the phone can show as
# "กำลัง<…>". Data-min on purpose: only the *kind* of work travels to the
# client — never the tool's arguments (file paths, command strings, query
# text), which would leak workstation detail the remote deliberately hides.
_TOOL_ACTIVITY = {
    "read": "reading",
    "glob": "reading",
    "grep": "reading",
    "edit": "editing",
    "write": "editing",
    "notebookedit": "editing",
    "bash": "running",
    "powershell": "running",
    "webfetch": "web",
    "websearch": "web",
    "task": "delegating",
    "agent": "delegating",
    "workflow": "delegating",
    "skill": "skill",
}


# W2a SHOULD-FIX: notify deliberately drops tool_use payloads (data-min), but
# a real `AskUserQuestion` picker leaves the remote user silently stuck — the
# Lead is waiting on a desktop-only TUI picker the phone can never drive. This
# extracts only the short question text (never the options payload) so the
# PWA can surface "Lead is waiting on a desktop picker" instead of hanging.
_MAX_ASK_QUESTION_CHARS = 200

# B2: per-option picker — unlike `_ask_question_prompt` above (data-min,
# question text only), this forwards the option labels too so the phone can
# render tappable chips instead of forcing the user to type a number blind.
# Only Claude's JSONL `AskUserQuestion` tool_use shape is understood here —
# codex/gemini panes have no equivalent structured event, so a picker on
# those panes still degrades to the plain "blocked on desktop" banner
# (multi-provider #103 gap, flagged not silently swallowed).
_MAX_ASK_OPTIONS = 6
_MAX_OPTION_LABEL_CHARS = 80


def _ask_question_options(rec: dict) -> dict | None:
    """Return `{"prompt": str, "options": [{"index": int, "label": str}],
    "multiSelect": bool}` for `rec`'s first `AskUserQuestion` question, else
    None. Mirrors `_ask_question_prompt`'s record-shape checks but forwards
    option labels (B2 tappable picker) instead of dropping them."""
    if rec.get("type") != "assistant":
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        if str(block.get("name") or "").lower() != "askuserquestion":
            continue
        inp = block.get("input")
        if not isinstance(inp, dict):
            return None
        questions = inp.get("questions")
        if not isinstance(questions, list) or not questions or not isinstance(questions[0], dict):
            return None
        q = questions[0]
        prompt = str(q.get("question") or "").strip()[:_MAX_ASK_QUESTION_CHARS]
        options: list[dict] = []
        raw_options = q.get("options")
        if isinstance(raw_options, list):
            for idx, opt in enumerate(raw_options[:_MAX_ASK_OPTIONS]):
                if not isinstance(opt, dict):
                    continue
                label = str(opt.get("label") or "").strip()[:_MAX_OPTION_LABEL_CHARS]
                if not label:
                    continue
                options.append({"index": idx, "label": label})
        return {"prompt": prompt, "options": options, "multiSelect": bool(q.get("multiSelect"))}
    return None


def _ask_question_prompt(rec: dict) -> str | None:
    """Return the short question text if `rec` is an assistant record whose
    content includes an `AskUserQuestion` tool_use block, else None. Only the
    first question's `question` field travels — never the `options` list."""
    if rec.get("type") != "assistant":
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        if str(block.get("name") or "").lower() != "askuserquestion":
            continue
        question = ""
        inp = block.get("input")
        if isinstance(inp, dict):
            questions = inp.get("questions")
            if isinstance(questions, list) and questions and isinstance(questions[0], dict):
                question = str(questions[0].get("question") or "").strip()
        return question[:_MAX_ASK_QUESTION_CHARS]
    return None


def _lead_activity(rec: dict) -> str | None:
    """Coarse activity category for a `type=="assistant"` record whose content
    is tool_use/thinking (no reply prose yet), or None if it isn't that. Used
    to give the PWA a readable "กำลัง…" status instead of a bare spinner."""
    if rec.get("type") != "assistant":
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = str(block.get("name") or "").lower()
            return _TOOL_ACTIVITY.get(name, "working")
    return None


def _lead_text_blocks(rec: dict) -> list[str]:
    """Return the reply prose in a `type=="assistant"` JSONL record.

    Only `type=="text"` content blocks qualify — `tool_use`, `tool_result`
    and `thinking` blocks are deliberately skipped (per spec: assistant text
    only, everything else is not conversation the Lead "said").
    """
    if rec.get("type") != "assistant":
        return []
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    out: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = (block.get("text") or "").strip()
        if text:
            out.append(text)
    return out


# Claude Code local-command / caveat wrapper markup — command internals and
# stdout that Claude Code itself injects as a `type=="user"` record, not
# something a human typed (e.g. running `/compact`).
_COMMAND_WRAPPER_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<local-command-caveat>",
)


def _lead_user_text(rec: dict) -> str | None:
    """Return the user-typed text in a `type=="user"` JSONL record, or None
    if it carries no human-typed prose. Mirrors `chatlog_scanner._user_text_only`
    — only `text` content blocks (or a bare string `content`) count; a
    `tool_result` block is a "user"-role record generated by a tool, not
    something a human typed, and is deliberately skipped. `isMeta` records
    (image placeholders, resume injection, skill-injected prompts, caveats —
    which can leak absolute workstation paths) and Claude Code's own
    slash-command wrapper markup are also not human-typed prose and are
    skipped."""
    if rec.get("type") != "user":
        return None
    if rec.get("isMeta"):
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, str):
        text = content.strip()
        if not text or text.startswith(_COMMAND_WRAPPER_PREFIXES):
            return None
        return text
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = (block.get("text") or "").strip()
            if text:
                parts.append(text)
    joined = "\n".join(parts).strip()
    if not joined or joined.startswith(_COMMAND_WRAPPER_PREFIXES):
        return None
    return joined


# `orchestrator.send()`'s header for a remote-originated Lead message
# (`from_role="remote"`) — stripped from history so the PWA doesn't echo its
# own routing prefix back as part of the bubble text.
_REMOTE_PREFIX = "[remote → lead] "


def _strip_remote_prefix(text: str) -> str:
    return text[len(_REMOTE_PREFIX) :] if text.startswith(_REMOTE_PREFIX) else text


def _resolve_claude_jsonl_path(project_ns: str, session_uuid: str | None) -> Path | None:
    """Resolve Claude's JSONL for the pane's *exact* recorded session — and only
    that. The mobile console is a mirror of the desktop Lead pane, so it must
    show the session that pane is actually on, nothing else: if that file
    doesn't exist yet (a fresh pane the user hasn't resumed), the honest
    answer is "nothing", not a guess.

    An earlier newest-jsonl fallback lived here — it dug up the most-recently-
    modified JSONL in the cwd dir when the exact uuid didn't resolve, meant to
    rescue a blank chat after id drift. But that broke the mirror: on a fresh
    open with no resumed session it surfaced an unrelated *old* session on the
    phone the desktop wasn't showing. Removed on purpose — a genuine
    session-id drift is a bug to fix at its source (keep pane_state.session_uuid
    accurate), never to paper over with a guess here."""
    if not session_uuid:
        return None
    try:
        base = config_dir_for(project_ns) / "projects"
        matches = list(base.glob(f"*/{session_uuid}.jsonl"))
    except OSError:
        return None
    return matches[0] if matches else None


# Backward-compatible low-level name for callers/tests that explicitly scan
# Claude's store. Provider-aware code must use `resolve_lead_jsonl` instead.
_resolve_jsonl_path = _resolve_claude_jsonl_path


def _lead_session_uuid(orch, project_ns: str) -> str | None:
    panes_by_project = getattr(orch, "_panes_by_project", None)
    pane_state = getattr(orch, "_pane_state", None)
    if not isinstance(panes_by_project, dict) or not isinstance(pane_state, dict):
        return None
    if "lead" not in panes_by_project.get(project_ns, ()):
        return None
    ps = pane_state.get(_exit_key(project_ns, "lead"))
    return getattr(ps, "session_uuid", None) if ps is not None else None


def pane_provider_name(orch, project_ns: str | None, role: str, pane=None) -> str:
    """Return the provider actually backing a pane, with a config fallback.

    A live pane's model records the provider chosen at spawn time. Prefer it
    over re-reading mutable provider config so a settings change cannot
    relabel an already-running pane. Lightweight test/headless panes may not
    expose a model, in which case the effective provider resolver is the
    authoritative fallback.
    """
    if pane is None and project_ns:
        panes_by_project = getattr(orch, "_panes_by_project", None)
        if isinstance(panes_by_project, dict):
            panes = panes_by_project.get(project_ns)
            if isinstance(panes, dict):
                pane = panes.get(role)
    model = getattr(pane, "model", None)
    provider = getattr(model, "provider_name", None)
    if isinstance(provider, str) and provider.strip():
        return provider.strip().lower()
    from ..provider_config import effective_provider_for

    return effective_provider_for(role, project_ns)


def lead_provider_name(orch, project_ns: str | None) -> str:
    return pane_provider_name(orch, project_ns, "lead")


def resolve_lead_jsonl(orch, project_ns: str, provider: str | None = None) -> Path | None:
    """Locate the open Lead pane's provider-owned history source.

    The historical name is retained for callers/tests, but resolution now
    dispatches through `_HistoryScanner` rather than assuming every provider
    writes Claude JSONL. Returns None when the provider has no registered,
    compatible scanner, there is no current session id, or its file has not
    been created yet.
    """
    provider = provider or lead_provider_name(orch, project_ns)
    scanner = history_scanner(provider)
    if scanner is None:
        return None
    session_uuid = _lead_session_uuid(orch, project_ns)
    # Do not bail out if session_uuid is None, as some providers (Gemini)
    # generate their own UUIDs and will resolve to the newest file instead.
    return scanner.resolve_session(project_ns, session_uuid)


_SESSION_LIST_DEFAULT_LIMIT = 10
_SESSION_LIST_MAX_LIMIT = 20
# First-user-line preview is deliberately short (data-min, W3): enough to
# recognize which session to resume, never a conversation excerpt.
_SESSION_PREVIEW_CHARS = 140
# Every cockpit task spec (any provider) starts a teammate pane's first
# user-typed line with this literal prefix (see CLAUDE.md's task-prompt
# template) — Lead sessions never do. Mirrors
# `chatlog_scanner._TEAMMATE_TASK_PREFIX` so the mobile picker filters
# teammate sessions out the same way the desktop one does.
_TEAMMATE_TASK_PREFIX = "[ROLE:"
# When a session goal is set (`Orchestrator._SESSION_GOAL_HEADER`), `assign()`
# prepends this header before `[ROLE:` on the *same* first user line — Lead
# spawns never go through `_apply_session_goal`, so this prefix is also
# assign-only. Mirrors `chatlog_scanner._SESSION_GOAL_TASK_PREFIX`.
_SESSION_GOAL_TASK_PREFIX = "[SESSION GOAL"
_TEAMMATE_TASK_PREFIXES = (_TEAMMATE_TASK_PREFIX, _SESSION_GOAL_TASK_PREFIX)


def _first_user_preview(path: Path) -> str:
    """Best-effort: the first human-typed line in `path`, truncated. Returns
    "" on any read/parse failure or if the session has no user turn yet —
    never raises (this feeds a listing endpoint, one bad file must not break
    the whole picker)."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                text = _lead_user_text(rec)
                if text:
                    return _strip_remote_prefix(text).strip()[:_SESSION_PREVIEW_CHARS]
    except OSError:
        pass
    return ""


def _list_recent_claude_sessions(
    project_ns: str, limit: int = _SESSION_LIST_DEFAULT_LIMIT
) -> list[dict]:
    """W3 (resume/session picker): recent Lead sessions for `project_ns`'s cwd,
    newest first. Unlike `resolve_lead_jsonl` (which only knows the *currently
    open* pane's session uuid), this scans every JSONL under the project's
    cwd-encoded directory so a closed or crashed Lead can still be resumed
    from the mobile picker.

    Data-min: each entry is only `{uuid, mtime, preview}` — preview is the
    first user-typed line, truncated (`_first_user_preview`), never the full
    conversation. Corrupt/empty files and directories that don't decode to
    this project's cwd are skipped silently — best-effort, matches
    `chatlog_scanner`'s read-only contract.

    Lists the cwd's encoded project dir directly (`token_meter.
    session_project_dir_for_cwd`) instead of scanning every project dir and
    reverse-decoding names for an equality check — `decode_project_dir()` is
    lossy (every non-alnum char, not just separators, becomes '-'), so a cwd
    containing '-', '_', '.', or a space (e.g. `agent-takkub`) would silently
    match zero directories under the old scan-and-decode approach.

    Teammate panes (backend/reviewer/qa/…) share the Lead's cwd, so their
    session jsonls land in this same encoded dir and would otherwise crowd
    out genuine Lead sessions from the capped list. They're filtered out by
    reading each candidate's first human-typed line (newest mtime first,
    stopping as soon as `limit` non-teammate sessions are found) and
    skipping any that start with a mandatory teammate task prefix
    (`_TEAMMATE_TASK_PREFIXES` — the `[ROLE:` declaration itself, or the
    `[SESSION GOAL` header `assign()` prepends ahead of it when a session
    goal is set) — this avoids reading all of a project's jsonls on every
    picker poll when most are teammate sessions. A session whose first
    line can't be read (or has none yet) is kept as a Lead-candidate
    rather than silently dropped."""
    from .. import config as _config
    from ..token_meter import session_project_dir_for_cwd

    cwd = _config.lead_cwd(project_ns)
    if not cwd:
        return []
    try:
        proj_dir = session_project_dir_for_cwd(config_dir_for(project_ns), cwd)
    except OSError:
        return []
    if not proj_dir.is_dir():
        return []
    found: list[tuple[float, Path]] = []
    try:
        for jsonl in proj_dir.glob("*.jsonl"):
            try:
                found.append((jsonl.stat().st_mtime, jsonl))
            except OSError:
                continue
    except OSError:
        return []
    found.sort(key=lambda t: t[0], reverse=True)
    capped = max(1, min(limit, _SESSION_LIST_MAX_LIMIT))
    out: list[dict] = []
    for mtime, jsonl in found:
        preview = _first_user_preview(jsonl)
        if preview.startswith(_TEAMMATE_TASK_PREFIXES):
            continue
        out.append({"uuid": jsonl.stem, "mtime": mtime, "preview": preview})
        if len(out) >= capped:
            break
    return out


def _tail_start_offset(path: Path, size: int) -> int:
    """Where a newly-created tail should start reading from: the current
    EOF, backed up to the last complete line boundary if EOF currently
    lands mid-record (Claude Code is still writing that JSON object and
    hasn't appended its trailing `\\n` yet). Without this, the tail's first
    read would only ever see the *tail end* of that record once the
    newline finally lands, fail to parse as JSON, and drop it for good."""
    if size == 0:
        return 0
    try:
        with path.open("rb") as fh:
            fh.seek(size - 1)
            if fh.read(1) == b"\n":
                return size
            chunk_size = 65536
            pos = size
            while pos > 0:
                read_size = min(chunk_size, pos)
                pos -= read_size
                fh.seek(pos)
                chunk = fh.read(read_size)
                idx = chunk.rfind(b"\n")
                if idx != -1:
                    return pos + idx + 1
            return 0
    except OSError:
        return size


def _read_recent_claude_messages(path: Path, limit: int = _DEFAULT_HISTORY_LIMIT) -> list[dict]:
    """Read (at most the last `_HISTORY_MAX_BYTES` of) `path` and return the
    last `limit` conversation turns, oldest first, **in the exact order they
    occurred** in the JSONL — assistant reply text (`kind: "lead"`) and
    user-typed prompts (`kind: "me"`) interleaved. `tool_result`/`tool_use`/
    `thinking` blocks and other non-conversation records never produce an
    entry (mobile junk-elimination — same contract the live tail enforces)."""
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
        lines = lines[1:]  # first fragment after an arbitrary seek may be mid-line
    out: list[dict] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        lead_texts = _lead_text_blocks(rec)
        if lead_texts:
            out.append({"text": "\n".join(lead_texts)[:_MAX_EVENT_CHARS], "kind": "lead"})
            continue
        user_text = _lead_user_text(rec)
        if user_text:
            user_text = _strip_remote_prefix(user_text)
            out.append({"text": user_text[:_MAX_EVENT_CHARS], "kind": "me"})
    return out[-limit:]


# Compatibility aliases for callers/tests that imported the original private
# remote helpers. The implementation and cache now live in provider/core code.
_gemini_chats_cache = gemini_helper._gemini_chats_cache


def _find_gemini_chats_dir(cwd: str) -> Path | None:
    return gemini_helper.find_gemini_chats_dir(cwd)


def _gemini_session_uuid(path: Path) -> str:
    return gemini_helper.gemini_session_uuid(path)


def _resolve_gemini_jsonl_for_cwd(cwd: str, session_uuid: str | None) -> Path | None:
    return gemini_helper.resolve_gemini_jsonl_for_cwd(cwd, session_uuid)


def _resolve_gemini_jsonl_path(project_ns: str, session_uuid: str | None) -> Path | None:
    from .. import config as _config

    cwd = _config.lead_cwd(project_ns)
    return _resolve_gemini_jsonl_for_cwd(cwd, session_uuid) if cwd else None


def _gemini_record_messages(rec: object) -> list[dict]:
    """Return messages represented by one snapshot or incremental record."""
    if not isinstance(rec, dict):
        return []
    patch = rec.get("$set")
    if isinstance(patch, dict) and isinstance(patch.get("messages"), list):
        return [m for m in patch["messages"] if isinstance(m, dict)]
    if rec.get("id") and rec.get("type") in ("user", "gemini"):
        return [rec]
    return []


def _gemini_message_text(message: dict) -> str:
    """Extract visible text while excluding function call/response payloads."""
    content = message.get("content", [])
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(part.strip() for part in parts if part.strip()).strip()


def _read_recent_gemini_messages(path: Path, limit: int = _DEFAULT_HISTORY_LIMIT) -> list[dict]:
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

    messages_by_id: dict[str, dict] = {}
    ordered_ids: list[str] = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue

        for message in _gemini_record_messages(rec):
            mid = str(message.get("id", "")).strip()
            if not mid:
                continue
            if mid not in messages_by_id:
                ordered_ids.append(mid)
            messages_by_id[mid] = message

    out: list[dict] = []
    for mid in ordered_ids:
        m = messages_by_id[mid]
        mtype = m.get("type")
        text = _gemini_message_text(m)
        if not text:
            continue
        if mtype == "user":
            if not text.startswith("<session_context>"):
                text = _strip_remote_prefix(text)
                out.append({"text": text[:_MAX_EVENT_CHARS], "kind": "me"})
        elif mtype == "gemini":
            out.append({"text": text[:_MAX_EVENT_CHARS], "kind": "lead"})

    return out[-limit:]


def _first_gemini_user_preview(path: Path) -> tuple[str, str]:
    """Returns (session_uuid, preview)."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            first_line = fh.readline().strip()
            if not first_line:
                return "", ""
            try:
                rec1 = json.loads(first_line)
                session_uuid = str(rec1.get("sessionId", "")).strip()
            except ValueError:
                return "", ""

            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue

                for m in _gemini_record_messages(rec):
                    if m.get("type") == "user":
                        text = _gemini_message_text(m)
                        if text and not text.startswith("<session_context>"):
                            return session_uuid, _strip_remote_prefix(text)[:_SESSION_PREVIEW_CHARS]
    except OSError:
        pass
    return "", ""


def _list_recent_gemini_sessions(
    project_ns: str, limit: int = _SESSION_LIST_DEFAULT_LIMIT
) -> list[dict]:
    from .. import config as _config

    cwd = _config.lead_cwd(project_ns)
    if not cwd:
        return []

    base = _find_gemini_chats_dir(cwd)
    if base is None:
        return []

    found: list[tuple[float, Path]] = []
    try:
        for jsonl in base.glob("session-*.jsonl"):
            try:
                found.append((jsonl.stat().st_mtime, jsonl))
            except OSError:
                continue
    except OSError:
        return []

    found.sort(key=lambda t: t[0], reverse=True)
    capped = max(1, min(limit, _SESSION_LIST_MAX_LIMIT))
    out: list[dict] = []

    for mtime, jsonl in found:
        session_uuid, preview = _first_gemini_user_preview(jsonl)
        if not session_uuid:
            continue
        if preview.startswith(_TEAMMATE_TASK_PREFIXES):
            continue
        out.append({"uuid": session_uuid, "mtime": mtime, "preview": preview})
        if len(out) >= capped:
            break
    return out


@dataclass(frozen=True)
class _HistoryScanner:
    """Provider adapter for remote history/session reads.

    A provider is registered only when its transcript location and record
    parser are known. This keeps the dispatcher format-neutral: a future
    Codex/Gemini adapter can supply its own resolver/parser without adding a
    provider-name branch to the API or notifier.
    """

    resolve_session: Callable[[str, str | None], Path | None]
    read_messages: Callable[[Path, int], list[dict]]
    list_sessions: Callable[[str, int], list[dict]]


_HISTORY_SCANNERS: dict[str, _HistoryScanner] = {
    "claude": _HistoryScanner(
        resolve_session=_resolve_claude_jsonl_path,
        read_messages=_read_recent_claude_messages,
        list_sessions=_list_recent_claude_sessions,
    ),
    "gemini": _HistoryScanner(
        resolve_session=_resolve_gemini_jsonl_path,
        read_messages=_read_recent_gemini_messages,
        list_sessions=_list_recent_gemini_sessions,
    ),
}


def history_scanner(provider: str) -> _HistoryScanner | None:
    """Return a verified scanner for `provider`, or None for clean fallback.

    Both the registry entry and ProviderSpec capability must opt in. The
    double gate prevents an accidentally registered experimental parser (or
    a capability flag flipped without a parser) from exposing the wrong
    provider's local transcript. A scanner may use any format; Claude's is
    JSONL, but future providers are not required to copy it.
    """
    from ..provider_spec import PROVIDER_REGISTRY

    name = str(provider or "").strip().lower()
    spec = PROVIDER_REGISTRY.get(name)
    if spec is None or not spec.supports_remote_history:
        return None
    return _HISTORY_SCANNERS.get(name)


def supports_remote_history(provider: str) -> bool:
    """Whether remote history is both declared and actually scannable."""
    return history_scanner(provider) is not None


def read_recent_lead_messages(
    path: Path, limit: int = _DEFAULT_HISTORY_LIMIT, *, provider: str = "claude"
) -> list[dict]:
    """Provider-dispatched history reader; unsupported providers return []."""
    scanner = history_scanner(provider)
    if scanner is None:
        return []
    try:
        return scanner.read_messages(path, limit)
    except (OSError, ValueError, TypeError):
        return []


def list_recent_lead_sessions(
    project_ns: str,
    limit: int = _SESSION_LIST_DEFAULT_LIMIT,
    *,
    provider: str | None = None,
) -> list[dict]:
    """Provider-dispatched Lead session list; unsupported providers return []."""
    if provider is None:
        from ..provider_config import effective_provider_for

        provider = effective_provider_for("lead", project_ns)
    scanner = history_scanner(provider)
    if scanner is None:
        return []
    try:
        return scanner.list_sessions(project_ns, limit)
    except (OSError, ValueError, TypeError):
        return []


def lead_history_snapshot(orch, project_ns: str, limit: int) -> tuple[str, list[dict]]:
    """Return `(provider, messages)` without ever crossing provider stores."""
    provider = lead_provider_name(orch, project_ns)
    path = resolve_lead_jsonl(orch, project_ns, provider)
    if path is None:
        return provider, []
    return provider, read_recent_lead_messages(path, limit, provider=provider)


def lead_sessions_snapshot(orch, project_ns: str, limit: int) -> tuple[str, list[dict]]:
    """Return `(provider, sessions)` with a clean unsupported-provider fallback."""
    provider = lead_provider_name(orch, project_ns)
    return provider, list_recent_lead_sessions(project_ns, limit, provider=provider)


class LeadNotifier(QObject):
    def __init__(self, orch, broadcaster) -> None:
        super().__init__()
        self._orch = orch
        self._broadcaster = broadcaster
        # project_ns -> _Tail for every open project's live Lead session.
        self._tails: dict[str, _Tail] = {}
        # project_ns -> last-emitted Lead-pane working state, so a 'working' /
        # 'idle' transition is pushed to the phone only on change (not every
        # tick). Drives the persistent "…" indicator (see
        # `_emit_lead_working_transitions`).
        self._lead_working: dict[str, bool] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(self._poll_all)
        self._timer.start()

        orch.agentDone.connect(self._on_done)
        orch.statusChanged.connect(self._resync)
        self._resync()

    # ── discover / rediscover every open project's Lead session uuid ────
    def _lead_uuids_by_project(self) -> dict[str, tuple[str, str]]:
        panes_by_project = getattr(self._orch, "_panes_by_project", None)
        pane_state = getattr(self._orch, "_pane_state", None)
        if not isinstance(panes_by_project, dict) or not isinstance(pane_state, dict):
            return {}
        found: dict[str, tuple[str, str]] = {}
        for project_ns, panes in panes_by_project.items():
            if "lead" not in panes:
                continue
            provider = pane_provider_name(self._orch, project_ns, "lead", panes.get("lead"))
            if history_scanner(provider) is None:
                continue
            ps = pane_state.get(_exit_key(project_ns, "lead"))
            uuid = getattr(ps, "session_uuid", None) if ps is not None else None
            if uuid:
                found[project_ns] = (provider, uuid)
        return found

    def _resolve_jsonl(
        self, project_ns: str, session_uuid: str, provider: str = "claude"
    ) -> Path | None:
        scanner = history_scanner(provider)
        return scanner.resolve_session(project_ns, session_uuid) if scanner is not None else None

    def _resync(self) -> None:
        wanted = self._lead_uuids_by_project()

        # drop projects that closed, or whose Lead session uuid changed
        # (respawn/resume), or whose provider changed — a stale tail must
        # never keep feeding events from another provider's store.
        for project_ns, tail in list(self._tails.items()):
            if wanted.get(project_ns) != (tail.provider, tail.session_uuid):
                del self._tails[project_ns]

        # start tailing newly-discovered sessions only — a project already
        # tailing its current session is left untouched (offset preserved).
        # A project whose jsonl hasn't been created/flushed yet (path is
        # still None) simply stays out of `_tails` and is retried here on
        # every call — `_poll_all()` calls `_resync()` on every tick, so a
        # session that resolves late (fresh spawn/resume timing) is picked
        # up on the very next poll instead of only on the next
        # `statusChanged` signal.
        for project_ns, (provider, session_uuid) in wanted.items():
            if project_ns in self._tails:
                continue
            path = self._resolve_jsonl(project_ns, session_uuid, provider)
            if path is None:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            offset = _tail_start_offset(path, size)
            self._tails[project_ns] = _Tail(
                path=path,
                session_uuid=session_uuid,
                provider=provider,
                offset=offset,
            )

    def _emit_lead_working_transitions(self) -> None:
        """Push a 'working' / 'idle' SSE event whenever the Lead pane's own
        working state flips — the same signal the desktop header spinner and
        `/api/activity` read (`pane.state == "working"`).

        The old indicator was driven purely off JSONL tool_use batches, so a
        pure-thinking stretch between two text blocks (which writes no record)
        let the phone's "…" vanish while the Lead was clearly still Honking —
        it looked idle/done. Tying it to the pane's live state keeps "…" up for
        the whole turn and drops it the instant the Lead goes idle."""
        panes_by_project = getattr(self._orch, "_panes_by_project", None)
        if not isinstance(panes_by_project, dict):
            return
        seen: set[str] = set()
        for project_ns, panes in panes_by_project.items():
            pane = panes.get("lead") if isinstance(panes, dict) else None
            working = getattr(pane, "state", None) == "working"
            seen.add(project_ns)
            if working == self._lead_working.get(project_ns, False):
                continue
            self._lead_working[project_ns] = working
            self._broadcaster.push("working" if working else "idle", "", project_ns)
        for gone in [p for p in self._lead_working if p not in seen]:
            del self._lead_working[gone]

    # ── incremental tail: read only the delta appended since last poll ──
    def _poll_all(self) -> None:
        self._resync()
        for project_ns, tail in list(self._tails.items()):
            self._poll_one(project_ns, tail)
        self._emit_lead_working_transitions()

    def _poll_one(self, project_ns: str, tail: _Tail) -> None:
        try:
            size = tail.path.stat().st_size
        except OSError:
            return
        if size <= tail.offset:
            return
        try:
            with tail.path.open("rb") as fh:
                fh.seek(tail.offset)
                chunk = fh.read(size - tail.offset)
        except OSError:
            return
        tail.offset = size
        data = tail.partial + chunk
        lines = data.split(b"\n")
        tail.partial = lines.pop()  # last line may be mid-write; hold it back
        activity: str | None = None
        ask_payload: dict | None = None
        pushed_text = False
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            texts = _lead_text_blocks(rec)
            if texts:
                joined = "\n".join(texts)[:_MAX_EVENT_CHARS]
                self._broadcaster.push("lead", joined, project_ns)
                pushed_text = True
                ask_payload = None  # a real reply supersedes any earlier picker
            else:
                # Assistant record with only tool_use/thinking blocks (no reply
                # prose yet) = the Lead is mid-turn, actively working. We never
                # forward the tool junk itself (user asked for text-only), but a
                # coarse activity category ("reading"/"running"/…) lets the PWA
                # show a readable "กำลัง…" status so a long tool-heavy turn
                # doesn't look frozen. Last activity in the batch wins.
                found = _lead_activity(rec)
                if found is not None:
                    activity = found
                ask = _ask_question_options(rec)
                if ask is not None:
                    ask_payload = ask
        # W2a/B2: a real AskUserQuestion picker fired and nothing has answered
        # it yet in this batch — surface the tappable-option payload instead
        # of the phone hanging silently. Takes priority over the generic
        # "working" signal for the same batch (AskUserQuestion's tool_use
        # would otherwise also map to a coarse "working" activity — the
        # picker payload is the more specific, more useful signal).
        if ask_payload is not None and not pushed_text:
            self._broadcaster.push("blocked_on_picker", ask_payload, project_ns)
        elif activity is not None and not pushed_text:
            # Only signal "working" when this batch showed activity but
            # produced no reply text — a real text push already tells the
            # PWA to drop the "…".
            self._broadcaster.push("working", activity, project_ns)

    # ── done events ───────────────────────────────────────────────────
    def _on_done(self, project_ns: str, role: str, note: str) -> None:
        # H-A: stamp the event's own project, not whatever project happens
        # to be active right now — `agentDone` fires for every project.
        #
        # LEAD_ONLY_STREAM (2026-07-23): a teammate's `takkub done` is not news
        # on the phone. The user delegated that work; Lead receives the same
        # note as a handoff and folds it into its own reply, which reaches the
        # phone through the JSONL tail below. Pushing both meant every fan-out
        # produced a burst of near-duplicate notifications — the "ขยะ" the
        # directive names. Lead's own done still goes through.
        if _remote_config.LEAD_ONLY_STREAM and role != "lead":
            return
        self._broadcaster.push("done", f"{role}: {note}"[:_MAX_EVENT_CHARS], project_ns)

    def stop(self) -> None:
        for signal, slot in (
            (self._orch.agentDone, self._on_done),
            (self._orch.statusChanged, self._resync),
        ):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._tails.clear()
        self._timer.stop()
