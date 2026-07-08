"""notify.py — bridges Lead-level orchestrator events into the SSE
broadcaster (§6.5, X-check 2.1 — hooks confirmed against the running
orchestrator, not guessed):

* done events: `orch.agentDone` (orchestrator.py, emitted on every
  `takkub done`).
* live Lead output: tails each open project's Lead pane **structured
  session JSONL** — `<CLAUDE_CONFIG_DIR>/projects/<encoded-cwd>/<uuid>.jsonl`
  (same store `chatlog_scanner.py` / `takkub search` read) — instead of
  scraping raw PTY bytes.

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
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer

from ..orchestrator_text import _exit_key
from ..user_profile import config_dir_for

# Per-message cap for a single Lead reply (live SSE event, history entry, and
# done note). Generous on purpose — the phone should show the WHOLE message
# (long plans/tables/summaries included), not a cut-off fragment. Still bounded
# so one pathological megabyte reply can't blow up the SSE payload / mobile DOM.
_MAX_EVENT_CHARS = 16000
_POLL_MS = 500
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


def _resolve_jsonl_path(project_ns: str, session_uuid: str) -> Path | None:
    try:
        base = config_dir_for(project_ns) / "projects"
        matches = list(base.glob(f"*/{session_uuid}.jsonl"))
    except OSError:
        return None
    return matches[0] if matches else None


def _lead_session_uuid(orch, project_ns: str) -> str | None:
    panes_by_project = getattr(orch, "_panes_by_project", None)
    pane_state = getattr(orch, "_pane_state", None)
    if not isinstance(panes_by_project, dict) or not isinstance(pane_state, dict):
        return None
    if "lead" not in panes_by_project.get(project_ns, ()):
        return None
    ps = pane_state.get(_exit_key(project_ns, "lead"))
    return getattr(ps, "session_uuid", None) if ps is not None else None


def resolve_lead_jsonl(orch, project_ns: str) -> Path | None:
    """Locate the open Lead pane's session JSONL for `project_ns` — used by
    the one-shot `/api/lead/history` endpoint (`api.lead_history`). Returns
    None if there is no open Lead pane, no session uuid yet, or the file
    hasn't been created/flushed."""
    session_uuid = _lead_session_uuid(orch, project_ns)
    if not session_uuid:
        return None
    return _resolve_jsonl_path(project_ns, session_uuid)


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


def read_recent_lead_messages(path: Path, limit: int = _DEFAULT_HISTORY_LIMIT) -> list[dict]:
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


class LeadNotifier(QObject):
    def __init__(self, orch, broadcaster) -> None:
        super().__init__()
        self._orch = orch
        self._broadcaster = broadcaster
        # project_ns -> _Tail for every open project's live Lead session.
        self._tails: dict[str, _Tail] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(self._poll_all)
        self._timer.start()

        orch.agentDone.connect(self._on_done)
        orch.statusChanged.connect(self._resync)
        self._resync()

    # ── discover / rediscover every open project's Lead session uuid ────
    def _lead_uuids_by_project(self) -> dict[str, str]:
        panes_by_project = getattr(self._orch, "_panes_by_project", None)
        pane_state = getattr(self._orch, "_pane_state", None)
        if not isinstance(panes_by_project, dict) or not isinstance(pane_state, dict):
            return {}
        found: dict[str, str] = {}
        for project_ns, panes in panes_by_project.items():
            if "lead" not in panes:
                continue
            ps = pane_state.get(_exit_key(project_ns, "lead"))
            uuid = getattr(ps, "session_uuid", None) if ps is not None else None
            if uuid:
                found[project_ns] = uuid
        return found

    def _resolve_jsonl(self, project_ns: str, session_uuid: str) -> Path | None:
        return _resolve_jsonl_path(project_ns, session_uuid)

    def _resync(self) -> None:
        wanted = self._lead_uuids_by_project()

        # drop projects that closed, or whose Lead session uuid changed
        # (respawn/resume) — a stale tail must never keep feeding events.
        for project_ns, tail in list(self._tails.items()):
            if wanted.get(project_ns) != tail.session_uuid:
                del self._tails[project_ns]

        # start tailing newly-discovered sessions only — a project already
        # tailing its current session is left untouched (offset preserved).
        # A project whose jsonl hasn't been created/flushed yet (path is
        # still None) simply stays out of `_tails` and is retried here on
        # every call — `_poll_all()` calls `_resync()` on every tick, so a
        # session that resolves late (fresh spawn/resume timing) is picked
        # up on the very next poll instead of only on the next
        # `statusChanged` signal.
        for project_ns, session_uuid in wanted.items():
            if project_ns in self._tails:
                continue
            path = self._resolve_jsonl(project_ns, session_uuid)
            if path is None:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            offset = _tail_start_offset(path, size)
            self._tails[project_ns] = _Tail(path=path, session_uuid=session_uuid, offset=offset)

    # ── incremental tail: read only the delta appended since last poll ──
    def _poll_all(self) -> None:
        self._resync()
        for project_ns, tail in list(self._tails.items()):
            self._poll_one(project_ns, tail)

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
        # Only signal "working" when this batch showed activity but produced no
        # reply text — a real text push already tells the PWA to drop the "…".
        if activity is not None and not pushed_text:
            self._broadcaster.push("working", activity, project_ns)

    # ── done events ───────────────────────────────────────────────────
    def _on_done(self, project_ns: str, role: str, note: str) -> None:
        # H-A: stamp the event's own project, not whatever project happens
        # to be active right now — `agentDone` fires for every project.
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
