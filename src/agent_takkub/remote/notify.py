"""notify.py — bridges Lead-level orchestrator events into the SSE
broadcaster (§6.5, X-check 2.1 — hooks confirmed against the running
orchestrator, not guessed):

* done events: `orch.agentDone` (orchestrator.py, emitted on every
  `takkub done`).
* report-shared events (#390): `orch.reportShared`, emitted by `Orchestrator.
  push_report` on `takkub report publish --send` — forwarded to the PWA as a
  `report` SSE event so a published file can reach the phone as a native
  attachment instead of a link tapped in an in-app browser.
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
import re
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
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
# #234 regression guard: a provider with `requires_session_uuid=False`
# (gemini, codex) has an identity triple whose session_uuid is permanently
# `""` — it can never be evicted/re-proven by `_resync()`'s identity check,
# yet its resolver can still legitimately re-point to a *different* file
# under an unchanged identity (e.g. gemini's uuid-less lookup picks
# `max(glob(...), key=mtime)`, uncached — it silently re-points the instant
# gemini rotates to a new conversation file). Such an identity must keep
# being re-resolved, just not on every 200ms tick — throttled to at most
# once per this many seconds per project. A rotated file is a rare,
# user-driven event (starting a fresh conversation), so a few seconds of
# lag before Mobile notices is imperceptible, while this still bounds the
# #229 stat-storm risk to a ~25x reduction versus per-tick (5s / 200ms).
_UUIDLESS_RESYNC_THROTTLE_S = 5.0
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
    spawn_ts: float = 0.0
    offset: int = 0
    # bytes held back from the previous read because they didn't end in a
    # `\n` yet — Claude Code writes one JSON object per line, and a poll can
    # land mid-write.
    partial: bytes = b""
    # OpenCode's sqlite equivalent of `partial`: part ids already pushed for
    # this session. Its poll cursor deliberately stalls on a part that is
    # still streaming (see `opencode_helper.poll_opencode_delta`), which
    # re-queries settled neighbours — this makes those re-reads idempotent.
    emitted_parts: set[str] = field(default_factory=set)


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

# remote AskUserQuestion fix: a defensive cap, not a discovered protocol
# limit — AskUserQuestion can carry more than one question per tool call
# (verified live: the CLI renders one tab per question + a trailing
# "Submit" tab, see docs/audit/2026-08-20-remote-askuserquestion.md).
# Forwarding only `questions[0]` (the pre-fix behavior) silently dropped every question
# after the first — the phone could not even see them, let alone answer.
_MAX_ASK_QUESTIONS = 4


def _ask_question_options(rec: dict) -> dict | None:
    """Return `{"questions": [{"prompt": str, "options": [{"index",
    "label"}], "multiSelect": bool}, ...]}` for every question in `rec`'s
    `AskUserQuestion` tool_use, else None. Mirrors `_ask_question_prompt`'s
    record-shape checks but forwards option labels (B2 tappable picker)
    instead of dropping them. Always a list (a single question is a
    1-element list) so callers never special-case question count."""
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
        if not isinstance(questions, list) or not questions:
            return None
        out_questions: list[dict] = []
        for q in questions[:_MAX_ASK_QUESTIONS]:
            if not isinstance(q, dict):
                continue
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
            out_questions.append(
                {"prompt": prompt, "options": options, "multiSelect": bool(q.get("multiSelect"))}
            )
        if not out_questions:
            return None
        return {"questions": out_questions}
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


def _live_user_payload(text: str | None) -> list[dict]:
    """Normalize a provider-owned user record for the live SSE stream.

    ``remote`` lets the PWA suppress only its own optimistic echo while still
    showing identical prompts deliberately typed twice on the desktop.
    """
    if not text:
        return []
    remote = text.startswith(_REMOTE_PREFIX)
    clean = _strip_remote_prefix(text).strip()
    return [{"text": clean[:_MAX_EVENT_CHARS], "remote": remote}] if clean else []


def _claude_live_users(rec: dict) -> list[dict]:
    return _live_user_payload(_lead_user_text(rec))


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
        named = base / f"takkub-project-{project_ns}" / f"{session_uuid}.jsonl"
        if named.is_file():
            return named
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
    pane = None
    panes_by_project = getattr(orch, "_panes_by_project", None)
    if isinstance(panes_by_project, dict):
        panes = panes_by_project.get(project_ns)
        if isinstance(panes, dict):
            pane = panes.get("lead")
    model = getattr(pane, "model", None)
    spawn_ts = float(getattr(model, "spawn_ts", 0.0) or 0.0)
    return scanner.resolve_session(project_ns, session_uuid, spawn_ts)


# #192 (remote-blank-output): "no transcript resolved" used to reach the PWA
# as an undifferentiated None — the phone showed the same silent blank chat
# whether the provider has no scanner at all (opencode/kimi/cursor, #103),
# the Lead pane hasn't stamped a session_uuid yet, or a claude/codex/gemini
# session_uuid drifted from its actual transcript file (manual desktop
# `/resume`). All three are diagnosable in-process right now — this mirrors
# the same three-layer classification `doctor.check_remote_mirror_live` /
# cli_server._remote_mirror_status already prove out for `takkub doctor
# --live`, but computed directly against `orch` (already in-process here,
# like `lead_history_snapshot` — no loopback round trip needed) so it can
# never disagree with what `resolve_lead_jsonl` actually did.
# #348: a resolved file that the scanner parses into zero records is the
# sharpest signal available that the upstream CLI changed its transcript
# schema/layout — a store nothing writes to still stops changing, but it
# never starts non-empty-with-nothing-parseable on its own. Record-level
# fail-silent (skip one malformed line, keep the rest) stays exactly as it
# was; this only classifies the *whole-file* case the individual `except
# ValueError` guards in `codex_helper.py`/`cursor_helper.py`/etc. can never
# see themselves, since each only ever looks at one record at a time.
#
# A bare `size == 0` check is not enough: some providers (Codex) write a
# small bookkeeping preamble (`session_meta`) the moment a pane spawns,
# before the user has typed a single prompt — that record legitimately
# parses to zero messages and must read as "no messages yet", not drift. A
# lone preamble record is a few hundred bytes at most in every scanner this
# repo has today, so a small threshold well above that (and far below what
# even one real exchange accumulates) tells "just spawned" apart from "real
# content came in but nothing parsed" without needing per-provider knowledge
# of what a preamble record looks like.
_TRANSCRIPT_DRIFT_MIN_BYTES = 4096


def _transcript_unreadable(scanner: _HistoryScanner, path: Path, project_ns: str) -> bool:
    if not scanner.exclusive_store:
        # Shared store (OpenCode's one sqlite db): zero rows for THIS
        # session is the normal shape of a brand-new session, not drift.
        return False
    try:
        size = path.stat().st_size
    except OSError:
        return False  # resolve_lead_jsonl already proved the path existed
    if size < _TRANSCRIPT_DRIFT_MIN_BYTES:
        return False
    try:
        messages = scanner.read_messages(path, 1, project_ns)
    except (OSError, ValueError, TypeError):
        return True
    return not messages


def lead_mirror_diagnosis(orch, project_ns: str) -> dict:
    """Classify why the Lead pane currently has nothing to mirror.

    Returns `{"code": ..., "provider": ..., "session_uuid_short"?: ...}`.
    `code` is one of `"provider_unsupported"`, `"no_session_uuid"`,
    `"transcript_missing"`, `"transcript_unreadable"` (#348 — file resolved
    and has content, but the parser extracted zero records: the upstream CLI
    likely changed its schema), or `None` when a transcript resolved with at
    least one parseable record, or resolved empty (legitimately "no messages
    yet", not a fault to explain). `session_uuid_short` (first 8 chars, never
    the full uuid — data-min) is included for `transcript_missing` and
    `transcript_unreadable` when a uuid was recorded.
    """
    provider = lead_provider_name(orch, project_ns)
    scanner = history_scanner(provider)
    if scanner is None:
        return {"code": "provider_unsupported", "provider": provider}
    session_uuid = _lead_session_uuid(orch, project_ns)
    if scanner.requires_session_uuid and not session_uuid:
        return {"code": "no_session_uuid", "provider": provider}
    path = resolve_lead_jsonl(orch, project_ns, provider)
    if path is None:
        out = {"code": "transcript_missing", "provider": provider}
        if session_uuid:
            out["session_uuid_short"] = session_uuid[:8]
        return out
    if _transcript_unreadable(scanner, path, project_ns):
        out = {"code": "transcript_unreadable", "provider": provider}
        if session_uuid:
            out["session_uuid_short"] = session_uuid[:8]
        return out
    return {"code": None, "provider": provider}


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


# Every cockpit spawn that carries a one-shot task opens the session with
# this exact synthetic user line (`spawn_engine._CURRENT_TASK_TRIGGER` — the
# literal is mirrored here rather than imported, like the teammate prefixes
# above, because remote/ must not pull in the spawn engine; a test pins the
# two together). It is machine-written, identical across sessions, and made
# the Mobile resume picker a wall of the same sentence — never a preview.
_SPAWN_TASK_TRIGGER = "Start the current task from the one-shot system-prompt block now."
_GENERATED_FIRST_LINES = (_SPAWN_TASK_TRIGGER,)
# Claude records its own session title (the one its native `/resume` picker
# shows) as repeated `ai-title` records. Reading the file's TAIL finds the
# newest one cheaply — titles are rewritten as the conversation evolves, so
# the last is the current one, and a session long enough to matter always
# has one within this window.
_AI_TITLE_TAIL_BYTES = 64 * 1024


def _claude_ai_title(path: Path) -> str:
    """Claude's own title for this session, or "" when it has none yet."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > _AI_TITLE_TAIL_BYTES:
                fh.seek(size - _AI_TITLE_TAIL_BYTES)
            raw = fh.read()
    except OSError:
        return ""
    title = ""
    for raw_line in raw.split(b"\n"):
        if b'"ai-title"' not in raw_line:
            continue
        try:
            rec = json.loads(raw_line)
        except ValueError:
            continue  # first line of a mid-record seek window
        if isinstance(rec, dict) and rec.get("type") == "ai-title":
            value = rec.get("aiTitle")
            if isinstance(value, str) and value.strip():
                title = value.strip()
    return title[:_SESSION_PREVIEW_CHARS]


def _first_user_preview(path: Path) -> str:
    """Best-effort label for one session in the Mobile resume picker.

    Claude's own `ai-title` wins when present, so the phone shows the same
    human-readable names as the desktop `/resume` picker ("โหลๆ") instead of
    the cockpit's synthetic opening line repeated down the whole list.
    Falls back to the first human-typed line, skipping generated openers.
    Returns "" on any read/parse failure or if the session has no user turn
    yet — never raises (this feeds a listing endpoint, one bad file must not
    break the whole picker)."""
    title = _claude_ai_title(path)
    if title:
        return title
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
                if not text:
                    continue
                text = _strip_remote_prefix(text).strip()
                if _is_generated_opener(text):
                    continue  # keep scanning for a line the human actually typed
                if text:
                    return text[:_SESSION_PREVIEW_CHARS]
    except OSError:
        pass
    return ""


def _is_generated_opener(text: str) -> bool:
    """True for a cockpit-written first user line (never a useful preview)."""
    stripped = (text or "").strip()
    return any(stripped.startswith(opener) for opener in _GENERATED_FIRST_LINES)


def _first_user_line(path: Path) -> str:
    """The raw first human-typed line, used for role classification only.

    Deliberately separate from `_first_user_preview`: the preview may now be
    Claude's `ai-title`, which says nothing about whether the session is a
    teammate's. Filtering on the displayed string instead of this one would
    put every teammate session back into the Lead picker.
    """
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
                    return _strip_remote_prefix(text).strip()
    except OSError:
        pass
    return ""


def _is_teammate_session_line(first_line: str) -> bool:
    """True when a session's opening line marks it as an assigned task."""
    return first_line.startswith(_TEAMMATE_TASK_PREFIXES)


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
    from ..token_meter import session_project_dirs_for_cwd

    cwd = _config.lead_cwd(project_ns)
    if not cwd:
        return []
    try:
        project_dirs = session_project_dirs_for_cwd(
            config_dir_for(project_ns), cwd, project_ns=project_ns
        )
    except OSError:
        return []
    found: list[tuple[float, Path]] = []
    for proj_dir in project_dirs:
        if not proj_dir.is_dir():
            continue
        try:
            for jsonl in proj_dir.glob("*.jsonl"):
                try:
                    found.append((jsonl.stat().st_mtime, jsonl))
                except OSError:
                    continue
        except OSError:
            continue
    found.sort(key=lambda t: t[0], reverse=True)
    capped = max(1, min(limit, _SESSION_LIST_MAX_LIMIT))
    out: list[dict] = []
    seen_uuids: set[str] = set()
    for mtime, jsonl in found:
        if jsonl.stem in seen_uuids:
            continue
        if _is_teammate_session_line(_first_user_line(jsonl)):
            continue
        seen_uuids.add(jsonl.stem)
        out.append({"uuid": jsonl.stem, "mtime": mtime, "preview": _first_user_preview(jsonl)})
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


def _resolve_gemini_jsonl_for_cwd(cwd: str, session_uuid: str | None) -> Path | None:
    return gemini_helper.resolve_gemini_jsonl_for_cwd(cwd, session_uuid)


def _resolve_gemini_jsonl_path(project_ns: str, session_uuid: str | None) -> Path | None:
    """Resolve an agy Lead transcript — new store first, legacy fallback.

    agy's 2026-08 layout is checked before `~/.gemini/tmp/.../chats/` because
    a machine that has used both still holds the (frozen) old files: picking
    the newest of the two stores by mtime would keep resolving a months-old
    conversation, which is exactly how this broke silently.
    """
    from .. import config as _config

    cwd = _config.lead_cwd(project_ns)
    if not cwd:
        return None
    transcript = gemini_helper.resolve_antigravity_transcript(cwd, session_uuid)
    if transcript is not None:
        return transcript
    return _resolve_gemini_jsonl_for_cwd(cwd, session_uuid)


# ── agy (Antigravity CLI) transcript records ──────────────────────────────
# One JSON object per line, e.g.
#   {"type":"USER_INPUT","source":"USER_EXPLICIT","content":"<USER_REQUEST>…"}
#   {"type":"PLANNER_RESPONSE","source":"MODEL","content":"…","thinking":"…"}
# `thinking` is never forwarded (same text-only contract as every other
# provider), and CHECKPOINT / CONVERSATION_HISTORY / ERROR_MESSAGE records
# are dropped — they carry state dumps, not conversation.
_ANTIGRAVITY_KINDS = {"USER_INPUT": "me", "PLANNER_RESPONSE": "lead"}
_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.DOTALL)


def _antigravity_record_message(rec: object) -> tuple[str, str] | None:
    if not isinstance(rec, dict):
        return None
    kind = _ANTIGRAVITY_KINDS.get(str(rec.get("type") or "").strip())
    if kind is None:
        return None
    content = rec.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    if kind == "me":
        # agy wraps the human's text in <USER_REQUEST> and appends its own
        # <ADDITIONAL_METADATA>/<USER_SETTINGS_CHANGE> blocks — mirror only
        # what the user actually typed.
        match = _USER_REQUEST_RE.search(content)
        if match is None:
            return None
        content = match.group(1)
    text = content.strip()
    return (kind, text) if text else None


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
    antigravity: list[dict] = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue

        parsed = _antigravity_record_message(rec)
        if parsed is not None:
            kind, text = parsed
            if kind == "me":
                text = _strip_remote_prefix(text)
            antigravity.append({"text": text[:_MAX_EVENT_CHARS], "kind": kind})
            continue

        for message in _gemini_record_messages(rec):
            mid = str(message.get("id", "")).strip()
            if not mid:
                continue
            if mid not in messages_by_id:
                ordered_ids.append(mid)
            messages_by_id[mid] = message

    if antigravity:
        # A transcript is written by ONE agy version, so a file that yielded
        # new-store records has no legacy ones to merge in.
        return antigravity[-limit:]

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


def _antigravity_first_user_line(path: Path) -> str:
    """Raw first human line of an agy transcript (role classification only)."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = _antigravity_record_message(json.loads(line))
                except ValueError:
                    continue
                if parsed is not None and parsed[0] == "me":
                    return _strip_remote_prefix(parsed[1]).strip()
    except OSError:
        pass
    return ""


def _first_gemini_user_preview(path: Path) -> tuple[str, str]:
    """Returns (session_uuid, preview).

    agy's new store keeps the session id in the transcript's directory name,
    not in a header record, so that shape is handled first.
    """
    if path.name == "transcript.jsonl":
        session_uuid = path.parent.parent.parent.name
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parsed = _antigravity_record_message(json.loads(line))
                    except ValueError:
                        continue
                    if parsed is None or parsed[0] != "me":
                        continue
                    text = _strip_remote_prefix(parsed[1]).strip()
                    if _is_generated_opener(text):
                        continue
                    if text:
                        return session_uuid, text[:_SESSION_PREVIEW_CHARS]
        except OSError:
            pass
        return session_uuid, ""
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

    antigravity = gemini_helper.find_antigravity_sessions(cwd, limit=_SESSION_LIST_MAX_LIMIT)
    if antigravity:
        capped = max(1, min(limit, _SESSION_LIST_MAX_LIMIT))
        out: list[dict] = []
        for session_uuid, transcript in antigravity[:capped]:
            try:
                mtime = transcript.stat().st_mtime
            except OSError:
                continue
            _, preview = _first_gemini_user_preview(transcript)
            if _is_teammate_session_line(_antigravity_first_user_line(transcript)):
                continue
            out.append({"uuid": session_uuid, "mtime": mtime, "preview": preview})
        return out

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


# ── Codex structured rollout adapter ──────────────────────────────────────
# Codex writes one JSON object per line below ~/.codex/sessions/YYYY/MM/DD.
# `event_msg.item_completed` (>= 0.147) and the legacy flat
# `event_msg.user_message` / `.agent_message` pair are the clean,
# provider-owned conversation stream; response_item/tool records are skipped
# so the phone never receives tool arguments, terminal paint bytes, or hidden
# reasoning.  Unlike Claude, Codex chooses its own session id after launch, so
# resolution is cwd + pane spawn time until the rollout file's id is known.


def _codex_sessions_root() -> Path:
    from ..codex_helper import codex_sessions_root

    return codex_sessions_root()


def _codex_archived_sessions_root() -> Path:
    from ..codex_helper import codex_archived_sessions_root

    return codex_archived_sessions_root()


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _codex_day_dirs(root: Path) -> list[tuple[object, Path]]:
    """`(date, dir)` for every `sessions/YYYY/MM/DD` leaf, newest first.

    Only directory names are read — no `stat()` — so this stays cheap however
    many rollout files have accumulated. Returns `[]` when the tree doesn't
    have the date layout, which tells the caller to fall back to a whole-tree
    walk rather than silently finding nothing.
    """
    days: list[tuple[object, Path]] = []
    try:
        years = [d for d in root.iterdir() if d.is_dir() and d.name.isdigit()]
    except OSError:
        return []
    for year in years:
        try:
            months = [d for d in year.iterdir() if d.is_dir() and d.name.isdigit()]
        except OSError:
            continue
        for month in months:
            try:
                day_dirs = [d for d in month.iterdir() if d.is_dir() and d.name.isdigit()]
            except OSError:
                continue
            for day in day_dirs:
                try:
                    stamp = datetime(int(year.name), int(month.name), int(day.name)).date()
                except ValueError:
                    continue
                days.append((stamp, day))
    days.sort(key=lambda item: item[0], reverse=True)
    return days


def _codex_rollout_candidates(root: Path, *, not_before: float = 0.0) -> Iterator[Path]:
    """Codex rollout files, newest first, yielded lazily (#293).

    The previous form was `sorted(root.rglob("rollout-*.jsonl"), key=mtime)`,
    which stat'ed every file in the store before looking at even one of them.
    Measured on the reference box (813 entries): **2716 ms** — paid on the Qt
    main thread every `_UUIDLESS_RESYNC_THROTTLE_S` seconds, because codex has
    `requires_session_uuid=False` and so can never be skipped the way #229
    lets claude's resolver be skipped. That made `Lead = codex` unusable in
    practice while looking like a working feature.

    Codex already partitions the store as `sessions/YYYY/MM/DD/`, so walking
    day directories newest-first turns the whole-store scan into "look at
    today, stop". Laziness matters as much as the ordering: every caller
    returns on its first match, so a hit in today's directory never pays for
    yesterday's.

    *not_before* (a spawn timestamp) bounds the walk by DATE rather than by
    the old mtime `break`. A day directory is a reliable stopping point; an
    mtime comparison inside a lazily-ordered stream is not, because a file
    touched later than its own day directory would end the scan early and
    hide a session that is really there.
    """
    day_dirs = _codex_day_dirs(root)
    if not day_dirs:
        # Unknown/flat layout — keep the original whole-tree behaviour rather
        # than reporting "no sessions" for a store we simply don't recognise.
        try:
            files = list(root.rglob("rollout-*.jsonl"))
        except OSError:
            return
        yield from sorted(files, key=_safe_mtime, reverse=True)
        return
    cutoff = None
    if not_before:
        # One full day of slack: file dates come from the local clock, the
        # spawn timestamp from ours, and a session started just before
        # midnight must not fall off the edge of the window.
        cutoff = datetime.fromtimestamp(max(0.0, float(not_before) - 86400.0)).date()
    for stamp, day_dir in day_dirs:
        if cutoff is not None and stamp < cutoff:
            return
        try:
            files = list(day_dir.glob("rollout-*.jsonl"))
        except OSError:
            continue
        yield from sorted(files, key=_safe_mtime, reverse=True)


def _norm_cwd(value: object) -> str:
    from ..codex_helper import normalize_codex_cwd

    return normalize_codex_cwd(value)


def _codex_session_meta(path: Path) -> dict:
    from ..codex_helper import read_codex_session_meta

    return read_codex_session_meta(path)


_CODEX_RESOLVE_CACHE: dict[tuple[str, str, int], Path] = {}


def _resolve_codex_jsonl_path(
    project_ns: str, session_uuid: str | None, not_before: float = 0.0
) -> Path | None:
    from .. import config as _config

    cwd = _config.lead_cwd(project_ns)
    wanted_cwd = _norm_cwd(cwd)
    if not wanted_cwd:
        return None
    wanted_uuid = str(session_uuid or "").strip()
    cache_key = (wanted_cwd, wanted_uuid, int(not_before or 0.0))
    cached = _CODEX_RESOLVE_CACHE.get(cache_key)
    if cached is not None and cached.is_file():
        return cached

    root = _codex_sessions_root()
    # A picker/desktop resume supplies Codex's authoritative thread id. Its
    # rollout can be days old and may not receive a new write until the user
    # submits another prompt, so spawn-time filtering must not hide it from
    # Mobile history immediately after resume — this lookup is deliberately
    # unbounded by date, unlike the spawn-time one below.
    if wanted_uuid:
        # `codex archive` (0.148+) moves the rollout out of the day-sharded
        # `sessions/` tree into a flat `archived_sessions/` dir; check both so
        # an id-based resume/mirror lookup for an archived-mid-conversation
        # session doesn't go silently blank. `_codex_rollout_candidates`
        # already falls back to a flat whole-tree walk for a non-date layout,
        # so no extra branching is needed for the archived root's shape.
        for search_root in (root, _codex_archived_sessions_root()):
            if not search_root.is_dir():
                continue
            for path in _codex_rollout_candidates(search_root):
                meta = _codex_session_meta(path)
                meta_id = str(meta.get("id") or meta.get("session_id") or "").strip()
                if meta_id != wanted_uuid or _norm_cwd(meta.get("cwd")) != wanted_cwd:
                    continue
                _CODEX_RESOLVE_CACHE[cache_key] = path
                return path

    if not root.is_dir():
        return None
    # Fresh Codex launches have no provider session id in pane state yet. The
    # file may be created just before AgentPane stamps spawn_ts after attach,
    # so allow a small clock/order tolerance without admitting old sessions.
    earliest = max(0.0, float(not_before or 0.0) - 15.0)
    for path in _codex_rollout_candidates(root, not_before=not_before):
        # `continue`, not `break` (#293): the candidate stream is ordered by
        # day directory then mtime, so a single file whose mtime trails its
        # own day would otherwise cut the scan short and hide a live session.
        # The generator's date cutoff already bounds how far this walks.
        if earliest and _safe_mtime(path) < earliest:
            continue
        meta = _codex_session_meta(path)
        if _norm_cwd(meta.get("cwd")) != wanted_cwd:
            continue
        _CODEX_RESOLVE_CACHE[cache_key] = path
        return path
    return None


# Codex 0.147 replaced the flat `event_msg.agent_message` / `.user_message`
# events with `event_msg.item_completed`, whose `item` carries the typed
# message (`AgentMessage` / `UserMessage`) plus a content-block list. Both
# forms are parsed, because the old one is not dead: `codex exec` (headless,
# originator `codex_exec`) still writes it in 0.147 while `codex-tui` — the
# mode every cockpit pane actually runs — writes only the new one. That split
# is exactly why the schema flip went unnoticed: every exec-based probe kept
# passing while Mobile went silent for `Lead = codex`.
_CODEX_ITEM_KINDS = {"AgentMessage": "lead", "UserMessage": "me"}


def _codex_item_text(item: dict) -> str:
    """Join the text blocks of a Codex >= 0.147 message item.

    Non-text blocks (`local_image`, …) are dropped — the phone mirrors prose
    only. Block-type casing differs per item (`Text` on agent messages,
    `text` on user ones), so the comparison is case-insensitive.
    """
    blocks = item.get("content")
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if str(block.get("type") or "").strip().lower() != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts)


def _codex_record_message(rec: object) -> tuple[str, str] | None:
    if not isinstance(rec, dict) or rec.get("type") != "event_msg":
        return None
    payload = rec.get("payload")
    if not isinstance(payload, dict):
        return None
    ptype = payload.get("type")
    if ptype == "item_completed":  # codex >= 0.147 (TUI)
        item = payload.get("item")
        if not isinstance(item, dict):
            return None
        # Reasoning/CommandExecution/tool items land here too and are dropped
        # by the kind map, so the text-only contract still holds.
        kind = _CODEX_ITEM_KINDS.get(str(item.get("type") or "").strip())
        if kind is None:
            return None
        text = _codex_item_text(item)
        return (kind, text) if text else None
    if ptype == "agent_message":  # codex <= 0.146, and `codex exec` on 0.147
        kind = "lead"
    elif ptype == "user_message":
        kind = "me"
    else:
        return None
    text = payload.get("message")
    if not isinstance(text, str) or not text.strip():
        return None
    return kind, text.strip()


def _codex_live_text_blocks(rec: dict) -> list[str]:
    parsed = _codex_record_message(rec)
    return [parsed[1]] if parsed is not None and parsed[0] == "lead" else []


def _first_codex_user_preview(path: Path) -> str:
    """Return Codex's first user event for picker identity/filtering."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    parsed = _codex_record_message(json.loads(line))
                except (ValueError, TypeError):
                    continue
                if parsed is not None and parsed[0] == "me":
                    text = _strip_remote_prefix(parsed[1]).strip()
                    if _is_generated_opener(text):
                        continue  # cockpit-written opener, not a preview
                    if text:
                        return text[:_SESSION_PREVIEW_CHARS]
    except OSError:
        pass
    return ""


def _read_recent_codex_messages(path: Path, limit: int = _DEFAULT_HISTORY_LIMIT) -> list[dict]:
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
        try:
            parsed = _codex_record_message(json.loads(raw_line))
        except (ValueError, TypeError):
            continue
        if parsed is None:
            continue
        kind, text = parsed
        if kind == "me":
            text = _strip_remote_prefix(text)
        out.append({"text": text[:_MAX_EVENT_CHARS], "kind": kind})
    return out[-limit:]


def _list_recent_codex_sessions(
    project_ns: str, limit: int = _SESSION_LIST_DEFAULT_LIMIT
) -> list[dict]:
    """List cwd-matched Codex chats for Remote Mobile's session picker.

    Codex keeps every role below one date-sharded store, so metadata cwd and
    the first user event are both checked.  This mirrors the Claude/Gemini
    picker contract: sessions from other projects and teammate task sessions
    never enter the capped Lead list.
    """
    from .. import config as _config

    wanted_cwd = _norm_cwd(_config.lead_cwd(project_ns))
    root = _codex_sessions_root()
    if not wanted_cwd or not root.is_dir():
        return []
    found: list[tuple[float, Path, dict]] = []
    try:
        for path in root.rglob("rollout-*.jsonl"):
            meta = _codex_session_meta(path)
            if _norm_cwd(meta.get("cwd")) != wanted_cwd:
                continue
            session_uuid = str(meta.get("id") or meta.get("session_id") or "").strip()
            if not session_uuid:
                continue
            try:
                found.append((path.stat().st_mtime, path, meta))
            except OSError:
                continue
    except OSError:
        return []
    found.sort(key=lambda item: item[0], reverse=True)
    capped = max(1, min(limit, _SESSION_LIST_MAX_LIMIT))
    out: list[dict] = []
    for mtime, path, meta in found:
        preview = _first_codex_user_preview(path)
        if preview.startswith(_TEAMMATE_TASK_PREFIXES):
            continue
        out.append(
            {
                "uuid": str(meta.get("id") or meta.get("session_id")),
                "mtime": mtime,
                "preview": preview,
            }
        )
        if len(out) >= capped:
            break
    return out


def _gemini_live_text_blocks(rec: dict) -> list[str]:
    parsed = _antigravity_record_message(rec)
    if parsed is not None:
        return [parsed[1]] if parsed[0] == "lead" else []
    out: list[str] = []
    for message in _gemini_record_messages(rec):
        if message.get("type") != "gemini":
            continue
        text = _gemini_message_text(message)
        if text:
            out.append(text)
    # A `$set.messages` snapshot may repeat the entire conversation; only its
    # newest Gemini message can be new at the tail. Incremental records contain
    # one message already, so the same rule covers both without duplicate SSE.
    return out[-1:]


def _gemini_live_users(rec: dict) -> list[dict]:
    parsed = _antigravity_record_message(rec)
    if parsed is not None:
        return _live_user_payload(parsed[1]) if parsed[0] == "me" else []
    messages = _gemini_record_messages(rec)
    # Snapshot records repeat the full conversation. Only a snapshot whose
    # newest message is a user turn represents a new desktop submission.
    if not messages or messages[-1].get("type") != "user":
        return []
    text = _gemini_message_text(messages[-1])
    if not text or text.startswith("<session_context>"):
        return []
    return _live_user_payload(text)


def _codex_live_users(rec: dict) -> list[dict]:
    parsed = _codex_record_message(rec)
    return _live_user_payload(parsed[1]) if parsed is not None and parsed[0] == "me" else []


# ── OpenCode SQLite transcript adapter ────────────────────────────────────
_LAST_OPENCODE_SESSION_BY_PROJECT: dict[str, str] = {}


def _resolve_opencode_db_path(
    project_ns: str, session_uuid: str | None, not_before: float = 0.0
) -> Path | None:
    from .. import config as _config
    from ..opencode_helper import resolve_opencode_session

    cwd = _config.lead_cwd(project_ns)
    if not cwd:
        return None
    res = resolve_opencode_session(cwd, session_uuid, not_before=not_before)
    if res is None:
        return None
    db_path, sid = res
    _LAST_OPENCODE_SESSION_BY_PROJECT[project_ns] = sid
    return db_path


def _read_recent_opencode_messages(
    path: Path, limit: int = _DEFAULT_HISTORY_LIMIT, project_ns: str = ""
) -> list[dict]:
    """Messages for *project_ns*'s OpenCode session inside the shared DB.

    The session id has to come from the resolver's per-project record, keyed
    by the project being read. The first version of this adapter took
    `list(_LAST_OPENCODE_SESSION_BY_PROJECT.values())[-1]` instead — the value
    of whichever project was inserted into the dict *first-most-recently*,
    which is not the same thing: re-assigning an existing key leaves its
    position alone, so with projects A then B every later read for A returned
    B's id. `opencode_db_path()` is one shared database for all projects, so
    the session id is the ONLY thing separating them — picking the wrong one
    means `/api/history` for A serves B's transcript.
    """
    from ..opencode_helper import read_opencode_session_messages

    sid = _LAST_OPENCODE_SESSION_BY_PROJECT.get(project_ns) if project_ns else None
    return read_opencode_session_messages(path, sid, limit)


def _list_recent_opencode_sessions(
    project_ns: str, limit: int = _SESSION_LIST_DEFAULT_LIMIT
) -> list[dict]:
    from .. import config as _config
    from ..opencode_helper import list_recent_opencode_sessions

    cwd = _config.lead_cwd(project_ns)
    if not cwd:
        return []
    return list_recent_opencode_sessions(cwd, limit)


# ── Cursor JSONL transcript adapter ──────────────────────────────────────────
def _resolve_cursor_jsonl_path(
    project_ns: str, session_uuid: str | None, not_before: float = 0.0
) -> Path | None:
    from .. import config as _config
    from ..cursor_helper import resolve_cursor_jsonl_for_cwd

    cwd = _config.lead_cwd(project_ns)
    if not cwd:
        return None
    return resolve_cursor_jsonl_for_cwd(cwd, session_uuid, not_before=not_before)


def _read_recent_cursor_messages(path: Path, limit: int = _DEFAULT_HISTORY_LIMIT) -> list[dict]:
    from ..cursor_helper import read_recent_cursor_messages

    return read_recent_cursor_messages(path, limit)


def _list_recent_cursor_sessions(
    project_ns: str, limit: int = _SESSION_LIST_DEFAULT_LIMIT
) -> list[dict]:
    from .. import config as _config
    from ..cursor_helper import list_recent_cursor_sessions

    cwd = _config.lead_cwd(project_ns)
    if not cwd:
        return []
    return list_recent_cursor_sessions(cwd, limit)


def _cursor_live_text_blocks(rec: dict) -> list[str]:
    from ..cursor_helper import cursor_live_text_blocks

    return cursor_live_text_blocks(rec)


def _cursor_live_users(rec: dict) -> list[dict]:
    from ..cursor_helper import cursor_live_users

    return cursor_live_users(rec)


def _cursor_live_activity(rec: dict) -> str | None:
    from ..cursor_helper import cursor_live_activity

    return cursor_live_activity(rec)


@dataclass(frozen=True)
class _HistoryScanner:
    """Provider adapter for remote history/session reads.

    A provider is registered only when its transcript location and record
    parser are known. This keeps the dispatcher format-neutral: a future
    Codex/Gemini adapter can supply its own resolver/parser without adding a
    provider-name branch to the API or notifier.
    """

    resolve_session: Callable[[str, str | None, float], Path | None]
    # (path, limit, project_ns). The project is part of the contract, not an
    # optional extra: a provider whose store is shared across projects (opencode
    # keeps every session in ONE sqlite db) can only tell them apart by the
    # session id recorded for that project. Adapters that resolve to a
    # per-project file ignore it.
    read_messages: Callable[[Path, int, str], list[dict]]
    list_sessions: Callable[[str, int], list[dict]]
    live_texts: Callable[[dict], list[str]]
    live_users: Callable[[dict], list[dict]] = lambda _rec: []
    live_activity: Callable[[dict], str | None] = lambda _rec: None
    live_ask: Callable[[dict], dict | None] = lambda _rec: None
    requires_session_uuid: bool = True
    # False only for a store shared across sessions/projects (OpenCode's one
    # sqlite db): there, a non-empty file with zero rows *for this session*
    # is the normal state of a brand-new session, not a drift signal — the
    # #348 unreadable-file guard below only applies where the resolved path
    # belongs exclusively to this session's transcript.
    exclusive_store: bool = True


_HISTORY_SCANNERS: dict[str, _HistoryScanner] = {
    "claude": _HistoryScanner(
        resolve_session=lambda project, uuid, _spawn_ts: _resolve_claude_jsonl_path(project, uuid),
        read_messages=lambda path, limit, _project: _read_recent_claude_messages(path, limit),
        list_sessions=_list_recent_claude_sessions,
        live_texts=_lead_text_blocks,
        live_users=_claude_live_users,
        live_activity=_lead_activity,
        live_ask=_ask_question_options,
    ),
    "gemini": _HistoryScanner(
        resolve_session=lambda project, uuid, _spawn_ts: _resolve_gemini_jsonl_path(project, uuid),
        read_messages=lambda path, limit, _project: _read_recent_gemini_messages(path, limit),
        list_sessions=_list_recent_gemini_sessions,
        live_texts=_gemini_live_text_blocks,
        live_users=_gemini_live_users,
        requires_session_uuid=False,
    ),
    "codex": _HistoryScanner(
        resolve_session=_resolve_codex_jsonl_path,
        read_messages=lambda path, limit, _project: _read_recent_codex_messages(path, limit),
        list_sessions=_list_recent_codex_sessions,
        live_texts=_codex_live_text_blocks,
        live_users=_codex_live_users,
        requires_session_uuid=False,
    ),
    "opencode": _HistoryScanner(
        resolve_session=_resolve_opencode_db_path,
        read_messages=_read_recent_opencode_messages,
        list_sessions=_list_recent_opencode_sessions,
        live_texts=lambda _rec: [],
        live_users=lambda _rec: [],
        requires_session_uuid=False,
        exclusive_store=False,
    ),
    "cursor": _HistoryScanner(
        resolve_session=_resolve_cursor_jsonl_path,
        read_messages=lambda path, limit, _project: _read_recent_cursor_messages(path, limit),
        list_sessions=_list_recent_cursor_sessions,
        live_texts=_cursor_live_text_blocks,
        live_users=_cursor_live_users,
        live_activity=_cursor_live_activity,
        requires_session_uuid=False,
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


def _read_from_conversation_store_v2(project_ns: str, limit: int) -> list[dict] | None:
    """Core V2 Conversation read-through (#309 Phase 6). Flag
    `TAKKUB_V2_CONVERSATION` is on by default since 1.0.84; this is the ONE touch point
    `remote/` makes into `core.*` (plan §6d "แก้ remote/ น้อยที่สุด 1 จุด
    fail-open") — the dependency direction is remote -> core only, which
    `core-is-bottom-layer`/`remote-bolt-on-isolation` both allow (neither
    forbids `core` from being imported, only from importing PyQt6/engine/UI
    or being imported the other way round).

    Returns `None` — "fall through to the normal scanner path" — whenever
    the flag is off, nothing has been ingested into the store yet for this
    project's Lead conversation, or anything raises. Never lets a Core V2
    bug break the existing scanner-backed mirror.
    """
    try:
        from ..core.conversation.flag import v2_conversation_enabled

        if not v2_conversation_enabled() or not project_ns:
            return None
        from ..core.conversation.store import ConversationStore, conversation_id_for

        store = ConversationStore()
        conversation_id = conversation_id_for(project_ns, "lead")
        messages = store.read_messages(project_ns, conversation_id)
        if not messages:
            return None
        kind_by_role = {"user": "me", "assistant": "lead", "system": "lead"}
        return [
            {
                "text": m.text[:_MAX_EVENT_CHARS],
                "kind": kind_by_role.get(str(m.role), "lead"),
            }
            for m in messages[-limit:]
        ]
    except Exception:
        return None


def read_recent_lead_messages(
    path: Path,
    limit: int = _DEFAULT_HISTORY_LIMIT,
    *,
    provider: str = "claude",
    project_ns: str = "",
) -> list[dict]:
    """Provider-dispatched history reader; unsupported providers return []."""
    v2_messages = _read_from_conversation_store_v2(project_ns, limit)
    if v2_messages is not None:
        return v2_messages
    scanner = history_scanner(provider)
    if scanner is None:
        return []
    try:
        return scanner.read_messages(path, limit, project_ns)
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
    return provider, read_recent_lead_messages(
        path, limit, provider=provider, project_ns=project_ns
    )


def lead_sessions_snapshot(orch, project_ns: str, limit: int) -> tuple[str, list[dict]]:
    """Return `(provider, sessions)` with a clean unsupported-provider fallback."""
    provider = lead_provider_name(orch, project_ns)
    return provider, list_recent_lead_sessions(project_ns, limit, provider=provider)


# remote AskUserQuestion fix answer-picker guard: only the tail needs scanning — a live picker's
# tool_use record is always near EOF, never buried deep in a long session.
_ASK_STATE_SCAN_BYTES = 200_000


def current_ask_state(orch, project_ns: str) -> dict | None:
    """Fresh (uncached) read of whether the Lead pane is CURRENTLY sitting at
    an unanswered `AskUserQuestion` picker, and if so, its full question
    list (same shape `_ask_question_options` returns).

    Deliberately independent of `LeadNotifier`'s poll-tail state: the
    answer-picker endpoint calls this right before injecting key presses,
    so it must reflect the true *current* state of the pane, not whatever a
    poll tick last pushed over SSE. Walks the JSONL from the end backward to
    the single most recent meaningful record — a real reply (`live_texts`)
    or a non-ask tool_use (`live_activity`) both mean the picker, if there
    ever was one, is no longer the pane's current state, so this returns
    None rather than let a stale picker answer get typed into a live turn.

    Only providers with a `live_ask` scanner (currently just Claude) can
    ever return non-None here — every other provider's scanner defaults
    `live_ask` to a no-op lambda, so this naturally returns None for them
    (same gap `_ask_question_options`'s docstring already flags, #103)."""
    provider = lead_provider_name(orch, project_ns)
    scanner = history_scanner(provider)
    if scanner is None:
        return None
    path = resolve_lead_jsonl(orch, project_ns, provider)
    if path is None:
        return None
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - _ASK_STATE_SCAN_BYTES))
            chunk = fh.read()
    except OSError:
        return None
    lines = chunk.split(b"\n")
    if size > _ASK_STATE_SCAN_BYTES:
        lines = lines[1:]  # first line may be a mid-record fragment
    for raw in reversed(lines):
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if scanner.live_texts(rec):
            return None  # a real reply already superseded any picker
        ask = scanner.live_ask(rec)
        if ask is not None:
            return ask
        if scanner.live_activity(rec) is not None:
            return None  # mid-turn on a DIFFERENT tool, not a picker
    return None


def read_resume_session_messages(
    project_ns: str,
    provider: str,
    session_uuid: str,
    limit: int = _DEFAULT_HISTORY_LIMIT,
) -> list[dict]:
    """Read the exact picker-selected transcript before replacing the pane.

    Returning this in the resume POST response removes a timing dependency
    between pane replacement, notifier resync, and the PWA's next history GET.
    The caller has already validated the provider-owned id/cwd pair; the
    scanner repeats exact resolution and applies the same clean-message and
    size limits as the normal history endpoint.
    """
    scanner = history_scanner(provider)
    if scanner is None:
        return []
    path = scanner.resolve_session(project_ns, session_uuid, 0.0)
    if path is None:
        return []
    capped = max(1, min(int(limit), _DEFAULT_HISTORY_LIMIT))
    return scanner.read_messages(path, capped, project_ns)


_FALLBACK_UI_MARKERS = (
    "esc to interrupt",
    "esc to cancel",
    "fast off",
    "fast on",
    "ctrl+p commands",
    "? for shortcuts",
    "shift+tab to cycle",
    "bypass permissions",
    "update available!",
    "write tests for @filename",
    "weekly 100% left",
)


def _pane_screen_lines(pane) -> list[str]:
    session = getattr(pane, "session", None)
    display_lines = getattr(session, "display_lines", None)
    if not callable(display_lines):
        return []
    try:
        return [str(line).rstrip() for line in display_lines()]
    except Exception:
        return []


def _fallback_visible_delta(before: list[str], after: list[str]) -> str:
    """Best-effort clean reply for providers without a structured log.

    The snapshot taken when a turn enters ``working`` already contains the
    submitted user prompt and old conversation.  Multiset subtraction leaves
    only rows painted during the turn; known composer/status chrome is then
    removed.  This is intentionally a last resort — structured provider logs
    always win and suppress this fallback.
    """
    remaining = Counter(line.strip() for line in before if line.strip())
    kept: list[str] = []
    for raw in after:
        line = raw.strip()
        if not line:
            continue
        if remaining[line] > 0:
            remaining[line] -= 1
            continue
        lower = line.lower()
        if any(marker in lower for marker in _FALLBACK_UI_MARKERS):
            continue
        if lower.startswith(("model:", "directory:", "permissions:", "tip:")):
            continue
        if line.startswith(("> ", "› ", "❯ ", _REMOTE_PREFIX)):
            continue
        if all(ch in "─━│┃┌┐└┘├┤┬┴┼╭╮╰╯═║╔╗╚╝╠╣╦╩╬-_| " for ch in line):
            continue
        if kept and kept[-1] == line:
            continue
        kept.append(line)
    return "\n".join(kept).strip()[:_MAX_EVENT_CHARS]


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
        # Provider-neutral safety net: capture the visible terminal before a
        # turn and emit only its clean delta at idle when no structured event
        # arrived. This keeps every registered provider usable in Remote.
        self._screen_baselines: dict[str, list[str]] = {}
        self._structured_text_seen: set[str] = set()
        # Session changes can happen on the desktop while Mobile keeps the
        # same project SSE connection. Hold the notification until the new
        # provider-owned history file resolves, then tell the PWA to reload
        # only that project's cached history.
        self._session_keys: dict[str, tuple[str, str, float]] = {}
        self._pending_session_changes: set[str] = set()
        # project_ns -> monotonic time of the last re-resolve for a
        # uuid-less provider's already-tailed session (throttle state, see
        # `_UUIDLESS_RESYNC_THROTTLE_S`).
        self._uuidless_resolved_at: dict[str, float] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(self._poll_all)

        orch.agentDone.connect(self._on_done)
        orch.statusChanged.connect(self._resync)
        # #390's reportShared postdates some test/provider-harness fake orchs
        # (no signal drift break — see `stop()` below for the disconnect side).
        report_shared = getattr(orch, "reportShared", None)
        if report_shared is not None:
            report_shared.connect(self._on_report_shared)

        # Start the poll timer only after every connect above succeeds — a
        # constructor that raises partway through (e.g. a future signal drift)
        # must not leave a running QTimer orphaned on this object (#344/#345).
        self._timer.start()
        self._resync()

    # ── discover / rediscover every open project's Lead session uuid ────
    def _lead_uuids_by_project(self) -> dict[str, tuple[str, str, float]]:
        panes_by_project = getattr(self._orch, "_panes_by_project", None)
        pane_state = getattr(self._orch, "_pane_state", None)
        if not isinstance(panes_by_project, dict) or not isinstance(pane_state, dict):
            return {}
        found: dict[str, tuple[str, str, float]] = {}
        for project_ns, panes in panes_by_project.items():
            if "lead" not in panes:
                continue
            provider = pane_provider_name(self._orch, project_ns, "lead", panes.get("lead"))
            scanner = history_scanner(provider)
            if scanner is None:
                continue
            ps = pane_state.get(_exit_key(project_ns, "lead"))
            uuid = getattr(ps, "session_uuid", None) if ps is not None else None
            if uuid or not scanner.requires_session_uuid:
                model = getattr(panes.get("lead"), "model", None)
                spawn_ts = float(getattr(model, "spawn_ts", 0.0) or 0.0)
                found[project_ns] = (provider, str(uuid or ""), spawn_ts)
        return found

    def _resolve_jsonl(
        self,
        project_ns: str,
        session_uuid: str,
        provider: str = "claude",
        spawn_ts: float = 0.0,
    ) -> Path | None:
        scanner = history_scanner(provider)
        return (
            scanner.resolve_session(project_ns, session_uuid or None, spawn_ts)
            if scanner is not None
            else None
        )

    def _resync(self) -> None:
        wanted = self._lead_uuids_by_project()

        for project_ns, key in wanted.items():
            previous = self._session_keys.get(project_ns)
            if previous is not None and previous != key:
                self._pending_session_changes.add(project_ns)
        for gone in set(self._session_keys) - set(wanted):
            self._pending_session_changes.discard(gone)
        self._session_keys = dict(wanted)

        # drop projects that closed, or whose Lead session uuid changed
        # (respawn/resume), or whose provider changed — a stale tail must
        # never keep feeding events from another provider's store.
        for project_ns, tail in list(self._tails.items()):
            w = wanted.get(project_ns)
            if w is None:
                del self._tails[project_ns]
            elif w[0] != tail.provider or w[2] != tail.spawn_ts:
                del self._tails[project_ns]
            elif w[1] and w[1] != tail.session_uuid:
                del self._tails[project_ns]

        # start tailing newly-discovered sessions — a project already
        # tailing its current session under a session-uuid-anchored provider
        # (claude) is left untouched (offset preserved) and — #229 —
        # skipped *before* touching the filesystem at all: the eviction
        # loop just above already deleted any tail whose identity no longer
        # matches `wanted`, so a project_ns still in `self._tails` under
        # such a provider is proof its resolved path can't have changed
        # (`resolve_session` is a pure function of
        # `(project_ns, session_uuid)` for these providers) — no glob
        # needed. Without this, every project got a fresh provider-store
        # glob (recursive stat) on *every* tick of this 200ms QTimer, on the
        # Qt main thread, regardless of whether anything changed — the
        # `_resolve_claude_jsonl_path` stat storm behind the #229 1.5-1.8s
        # SOFT stalls.
        #
        # #234: that proof does NOT extend to a provider with
        # `requires_session_uuid=False` (gemini, codex) — its identity
        # triple's session_uuid is permanently `""`, so it can never be
        # evicted/re-proven by the loop above, yet its resolver can still
        # legitimately re-point to a different file under that unchanged
        # identity (see `_UUIDLESS_RESYNC_THROTTLE_S`). Those stay live,
        # throttled instead of skipped outright.
        #
        # A project whose jsonl hasn't been created/flushed yet (path is
        # still None) simply stays out of `_tails` and is retried here on
        # every call — `_poll_all()` calls `_resync()` on every tick, so a
        # session that resolves late (fresh spawn/resume timing) is picked
        # up on the very next poll instead of only on the next
        # `statusChanged` signal.
        now = time.monotonic()
        for project_ns, (provider, session_uuid, spawn_ts) in wanted.items():
            existing = self._tails.get(project_ns)
            scanner = history_scanner(provider)
            uuidless = scanner is not None and not scanner.requires_session_uuid
            if existing is not None:
                if not uuidless:
                    continue
                last = self._uuidless_resolved_at.get(project_ns, 0.0)
                if now - last < _UUIDLESS_RESYNC_THROTTLE_S:
                    continue
            if uuidless:
                self._uuidless_resolved_at[project_ns] = now
            path = self._resolve_jsonl(project_ns, session_uuid, provider, spawn_ts)
            if path is None:
                continue
            if existing is not None and path == existing.path:
                if provider == "opencode":
                    sid = _LAST_OPENCODE_SESSION_BY_PROJECT.get(project_ns, session_uuid)
                    if sid and sid != existing.session_uuid:
                        pass  # session rotated -> recreate tail
                    else:
                        continue
                else:
                    continue
            if provider == "opencode":
                from ..opencode_helper import get_opencode_latest_part_time

                sid = _LAST_OPENCODE_SESSION_BY_PROJECT.get(project_ns, session_uuid)
                offset = get_opencode_latest_part_time(path, sid)
                self._tails[project_ns] = _Tail(
                    path=path,
                    session_uuid=sid,
                    provider=provider,
                    spawn_ts=spawn_ts,
                    offset=offset,
                )
            else:
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                offset = _tail_start_offset(path, size)
                self._tails[project_ns] = _Tail(
                    path=path,
                    session_uuid=session_uuid,
                    provider=provider,
                    spawn_ts=spawn_ts,
                    offset=offset,
                )
            if project_ns in self._pending_session_changes or existing is not None:
                self._broadcaster.push("session_changed", {"provider": provider}, project_ns)
                self._pending_session_changes.discard(project_ns)

        for gone in set(self._uuidless_resolved_at) - set(wanted):
            del self._uuidless_resolved_at[gone]

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
            if working:
                self._structured_text_seen.discard(project_ns)
                self._screen_baselines[project_ns] = _pane_screen_lines(pane)
                self._broadcaster.push("working", "", project_ns)
            else:
                if project_ns not in self._structured_text_seen:
                    fallback = _fallback_visible_delta(
                        self._screen_baselines.get(project_ns, []), _pane_screen_lines(pane)
                    )
                    if fallback:
                        self._broadcaster.push("lead", fallback, project_ns)
                self._screen_baselines.pop(project_ns, None)
                self._structured_text_seen.discard(project_ns)
                self._broadcaster.push("idle", "", project_ns)
        for gone in [p for p in self._lead_working if p not in seen]:
            del self._lead_working[gone]
            self._screen_baselines.pop(gone, None)
            self._structured_text_seen.discard(gone)

    # ── incremental tail: read only the delta appended since last poll ──
    def _poll_all(self) -> None:
        self._resync()
        for project_ns, tail in list(self._tails.items()):
            self._poll_one(project_ns, tail)
        self._emit_lead_working_transitions()

    def _poll_one(self, project_ns: str, tail: _Tail) -> None:
        if tail.provider == "opencode":
            from ..opencode_helper import poll_opencode_delta

            new_offset, events = poll_opencode_delta(
                tail.path,
                tail.session_uuid,
                since_time_ms=tail.offset,
                emitted=tail.emitted_parts,
            )
            if new_offset > tail.offset:
                tail.offset = new_offset
            activity: str | None = None
            ask_payload: dict | None = None
            pushed_text = False
            for ev_type, payload in events:
                if ev_type == "user":
                    self._broadcaster.push("user", payload, project_ns)
                elif ev_type == "lead":
                    joined = str(payload)[:_MAX_EVENT_CHARS]
                    self._broadcaster.push("lead", joined, project_ns)
                    pushed_text = True
                    self._structured_text_seen.add(project_ns)
                    ask_payload = None
                elif ev_type == "working":
                    activity = str(payload)
                elif ev_type == "blocked_on_picker" and isinstance(payload, dict):
                    ask_payload = payload
            if ask_payload is not None and not pushed_text:
                self._broadcaster.push("blocked_on_picker", ask_payload, project_ns)
            elif activity is not None and not pushed_text:
                if not self._lead_working.get(project_ns, False):
                    self._structured_text_seen.discard(project_ns)
                    panes = getattr(self._orch, "_panes_by_project", {}).get(project_ns, {})
                    pane = panes.get("lead") if isinstance(panes, dict) else None
                    self._screen_baselines[project_ns] = _pane_screen_lines(pane)
                self._lead_working[project_ns] = True
                self._broadcaster.push("working", activity, project_ns)
            return

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
        scanner = history_scanner(tail.provider)
        if scanner is None:
            return
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            for user_payload in scanner.live_users(rec):
                self._broadcaster.push("user", user_payload, project_ns)
            texts = scanner.live_texts(rec)
            if texts:
                joined = "\n".join(texts)[:_MAX_EVENT_CHARS]
                self._broadcaster.push("lead", joined, project_ns)
                pushed_text = True
                self._structured_text_seen.add(project_ns)
                ask_payload = None  # a real reply supersedes any earlier picker
            else:
                # Assistant record with only tool_use/thinking blocks (no reply
                # prose yet) = the Lead is mid-turn, actively working. We never
                # forward the tool junk itself (user asked for text-only), but a
                # coarse activity category ("reading"/"running"/…) lets the PWA
                # show a readable "กำลัง…" status so a long tool-heavy turn
                # doesn't look frozen. Last activity in the batch wins.
                found = scanner.live_activity(rec)
                if found is not None:
                    activity = found
                ask = scanner.live_ask(rec)
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
            # Record this edge as well as sending it. Otherwise tool activity
            # can emit `working` between pane-state polls while the dedupe map
            # remains False; the following idle poll suppresses `idle` and the
            # phone spinner stays up forever.
            if not self._lead_working.get(project_ns, False):
                self._structured_text_seen.discard(project_ns)
                panes = getattr(self._orch, "_panes_by_project", {}).get(project_ns, {})
                pane = panes.get("lead") if isinstance(panes, dict) else None
                self._screen_baselines[project_ns] = _pane_screen_lines(pane)
            self._lead_working[project_ns] = True
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

    # ── report-shared events (#390) ──────────────────────────────────────
    def _on_report_shared(self, project_ns: str, payload: dict) -> None:
        """`Orchestrator.push_report`'s `reportShared` -> SSE `report` event.

        H-A (same as `_on_done` above): stamp the event's own `project_ns`,
        not whatever project happens to be active — `push_report` can be
        called for any open project. `payload` is already the small,
        pre-shaped `{name,url,label,size_bytes,attachment}` dict
        `Orchestrator.push_report` built; forwarded as-is (a structured SSE
        payload, same as `blocked_on_picker`'s dict shape) rather than
        wrapped as text."""
        self._broadcaster.push("report", payload, project_ns)

    def stop(self) -> None:
        connections = [
            (self._orch.agentDone, self._on_done),
            (self._orch.statusChanged, self._resync),
        ]
        report_shared = getattr(self._orch, "reportShared", None)
        if report_shared is not None:
            connections.append((report_shared, self._on_report_shared))
        for signal, slot in connections:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._tails.clear()
        self._screen_baselines.clear()
        self._structured_text_seen.clear()
        self._session_keys.clear()
        self._pending_session_changes.clear()
        self._timer.stop()
