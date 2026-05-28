"""Orchestrator: owns all AgentPanes, exposes high-level operations.

Public API (called by main_window UI and cli_server JSON requests):

  spawn(role, cwd=None)          -> bool, message
  assign(role, cwd, task)        -> bool, message
  send(to_role, msg, from_role)  -> bool, message
  close(role)                    -> bool, message
  done(from_role, note)          -> bool, message
  list_status()                  -> dict[role, state]
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import secrets
import subprocess
import time
import uuid as _uuid
from datetime import datetime

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from .agent_pane import AgentPane
from .claude_auth_config import apply_claude_auth_overrides
from .config import (
    EVENTS_LOG,
    REPO_ROOT,
    RUNTIME_DIR,
    active_project,
    agent_role_dir,
    default_cwd_for_role,
    ensure_runtime,
    find_claude_executable,
    lead_cwd,
    validate_name,
)
from .lead_context import (  # re-exported for test + doctor.py imports
    _LEAD_GUARD_ALLOW_TOOLS,
    _LEAD_GUARD_WRITE_TOOLS,
    _SAFE_PLUGINS,
    _allowed_project_roots,
    _default_plugin_dirs,
    _recent_session_brief,
    _render_lead_context,
    render_lead_settings,
)
from .pane_env import (  # re-exported for test imports — see pane_env.py docstring
    _DEFAULT_MCP_TOOL_TIMEOUT_MS,
    _ECC_MUTED_HOOKS,
    _PANE_ENV_ALLOWLIST,
    _apply_ecc_mute,
    _apply_mcp_timeout,
    _build_pane_env,
)
from .pty_session import PtySession
from .roles import LEAD
from .vault_mirror import (  # re-exported for test + script imports
    _DEFAULT_VAULT,
    _JUNK_NOTE_EXACT,
    _JUNK_NOTE_MIN_LEN,
    _JUNK_PROJECT_PREFIXES,
    _VAULT_ENV,
    _is_junk_note,
    _is_junk_project,
    _render_decision_note,
    _resolve_vault_dir,
)

_ANSI = re.compile(r"\x1b\[[0-9;]*[mABCDHJKSThlsu]")

# Artifact dirs excluded from harvest scans.
_HARVEST_EXCLUDE_DIRS = frozenset(
    {
        "__pycache__",
        ".git",
        "node_modules",
        ".venv",
        ".next",
        "dist",
        "build",
    }
)

# Harvest hint: inject a '[cockpit] <role> ไม่ active >Nm' message into Lead
# when a teammate pane has been idle this long. 0 = disabled.
HARVEST_HINT_SEC = int(os.environ.get("TAKKUB_HARVEST_HINT_SEC", "600"))

__all__ = [  # backwards-compat re-exports
    "HARVEST_HINT_SEC",
    "_DEFAULT_MCP_TOOL_TIMEOUT_MS",
    "_DEFAULT_VAULT",
    "_ECC_MUTED_HOOKS",
    "_HARVEST_EXCLUDE_DIRS",
    "_JUNK_NOTE_EXACT",
    "_JUNK_NOTE_MIN_LEN",
    "_JUNK_PROJECT_PREFIXES",
    "_LEAD_GUARD_ALLOW_TOOLS",
    "_LEAD_GUARD_WRITE_TOOLS",
    "_PANE_ENV_ALLOWLIST",
    "_SAFE_PLUGINS",
    "_VAULT_ENV",
    "_allowed_project_roots",
    "_apply_ecc_mute",
    "_apply_mcp_timeout",
    "_build_pane_env",
    "_default_plugin_dirs",
    "_is_junk_note",
    "_is_junk_project",
    "_recent_session_brief",
    "_render_decision_note",
    "_render_lead_context",
    "_resolve_vault_dir",
    "render_lead_settings",
    "scan_artifacts",
]


def _log_event(event: str, **details) -> None:
    """Append a JSONL event line to runtime/events.log. Best-effort; never
    raises so an audit-log failure can't take down the orchestrator."""
    try:
        ensure_runtime()
        line = json.dumps(
            {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, **details},
            ensure_ascii=False,
        )
        with open(EVENTS_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def scan_artifacts(
    project_paths: list[pathlib.Path],
    since_ts: float,
    *,
    limit: int = 100,
) -> list[dict]:
    """Scan project paths for files modified at or after `since_ts`.

    Returns list[{path, mtime_ts, mtime_rel}] sorted by mtime descending,
    capped at `limit`. Skips symlinks, directories, and any path whose parts
    contain a name from _HARVEST_EXCLUDE_DIRS. Non-existent or unreadable
    paths are silently skipped.
    """
    found: list[tuple[float, pathlib.Path]] = []
    seen: set[pathlib.Path] = set()
    now = time.time()

    for base in project_paths:
        if not base.exists():
            continue
        try:
            for p in base.rglob("*"):
                if p.is_symlink() or p.is_dir():
                    continue
                if p in seen:
                    continue
                seen.add(p)
                if any(part in _HARVEST_EXCLUDE_DIRS for part in p.parts):
                    continue
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                if mtime >= since_ts:
                    found.append((mtime, p))
        except OSError:
            continue

    found.sort(key=lambda t: t[0], reverse=True)
    del found[limit:]

    result: list[dict] = []
    for mtime, p in found:
        age = now - mtime
        if age < 60:
            rel = f"{int(age)}s ago"
        elif age < 3600:
            rel = f"{int(age // 60)}m ago"
        else:
            rel = f"{int(age // 3600)}h ago"
        result.append({"path": str(p), "mtime_ts": mtime, "mtime_rel": rel})
    return result


RESUME_WINDOW_SEC = 5 * 60  # respawn within this window → claude --resume <uuid>

# Stall detection: if a `working` pane shows no detectable progress (no
# transcript bytes, no new screenshots, no takkub send received) for this long,
# `list_status_detailed()` marks it stalled and `takkub list` shows
# `active (stalled Nm)` instead of plain `active`.
# Overrideable via env so QA-heavy workflows can tune the threshold.
STALL_THRESHOLD_SEC = int(os.environ.get("TAKKUB_STALL_THRESHOLD_SEC", "300"))

# When `_LAST_SESSION_FILE` is newer than this and teammates are alive,
# the current Lead boot is treated as post-compact so a status snapshot
# is auto-injected into the Lead prompt.
_POST_COMPACT_DETECT_SEC = 5 * 60

# Idle watchdog: when a teammate pane sits at the ready prompt (claude is
# idle, no "esc to interrupt") while pane.state is still "working", the
# orchestrator assumes the agent finished its task but forgot to call
# `takkub done`. After IDLE_REMIND_AFTER_S of continuous idle we inject a
# one-line reminder, then back off for IDLE_REMIND_COOLDOWN_S before another.
# Set IDLE_REMIND_AFTER_S to 0 to disable the watchdog entirely.
IDLE_REMIND_AFTER_S = 45
IDLE_REMIND_COOLDOWN_S = 90

# A teammate pane in `working` state with no PTY output for this long
# is treated as hung — claude probably crashed silently, deadlocked on
# a tool call, or got wedged behind a slow MCP server. Orchestrator
# auto-recovers via close + respawn (which picks `--resume <uuid>` because
# the recent-exit timestamp and UUID are still fresh). 10 minutes is generous enough
# that a heavy `npm install` or a slow Lighthouse audit won't trip it.
STUCK_THRESHOLD_S = 10 * 60
# Once a recover fires for a pane, wait this long before another one
# is allowed — otherwise a chronically-stuck workload restarts on a
# loop. Three strikes is the soft cap (auto-respawn-attempts already
# handles the hard cap separately).
STUCK_RECOVER_COOLDOWN_S = 5 * 60
IDLE_WATCHDOG_INTERVAL_MS = 5_000
IDLE_REMINDER_TEXT = (
    "🔔 [auto-reminder] pane นี้ idle อยู่ — ถ้า task เสร็จแล้วต้อง run "
    '`takkub done "<summary>"` เป็นคำสั่ง shell **ตอนนี้** (ไม่ใช่พิมพ์เป็น text). '
    "Lead ไม่ได้รับ notice จนกว่าจะ run คำสั่งนี้จริง pane จะค้างจน auto-recover. "
    'ยังทำงานต่ออยู่ → ignore ข้อความนี้ ถ้าติด blocker → `takkub send --to lead "..."`'
)

# Auto-respawn on unexpected pane crash. The orchestrator notices when a
# pane exits without a corresponding takkub close/done (claude crashed,
# OOM, parent killed it) and gives it a clean respawn with --resume <uuid>
# so the conversation survives. AUTO_RESPAWN_MAX caps consecutive attempts
# per pane so a deterministically-crashing claude doesn't spawn-loop.
AUTO_RESPAWN_DELAY_MS = 2_500
AUTO_RESPAWN_MAX = 2

# Codex early-crash detection. If a codex pane exits within this many seconds
# of spawning, the orchestrator treats it as a suspicious early crash, logs a
# `codex_early_crash` event, and writes a diagnostic dump to
# runtime/codex_crash_dumps/<ts>-<project>-<role>.log containing the exit
# code, time-to-exit, last PTY output tail, and the filtered env keys.  Dumps
# let us falsify the MCP-boot-race vs env-missing hypotheses without needing a
# live debugger session.
CODEX_EARLY_CRASH_WINDOW_SEC = 90

# Bracketed-paste threshold for messages injected into a pane via the
# orchestrator (assign / send / slash-command). Below this length we
# write raw text — claude code's interactive input handles short typing
# fine. At or above, we wrap with `ESC [200~ ... ESC [201~` so claude
# treats the whole block as a single atomic paste instead of typing
# char-by-char. Without this, long task specs occasionally lose the
# head of the message when the pane is mid-render at write time (the
# bug behind teammates complaining about "ข้อความถูกตัดส่วนต้น").
BRACKETED_PASTE_THRESHOLD = 200
_PASTE_START = "\x1b[200~"
_PASTE_END = "\x1b[201~"


def _paste_payload(text: str) -> str:
    """Return `text` wrapped in bracketed-paste escapes when long enough.

    Used by every cockpit-driven write into a pane's PTY (Lead's task
    specs, peer-to-peer takkub send, slash-command injection). Short
    inputs are returned unchanged so single-character prompts still
    feel like typing rather than a paste burst.
    """
    if len(text) < BRACKETED_PASTE_THRESHOLD:
        return text
    return _PASTE_START + text + _PASTE_END


_CODEX_TASK_NOTICE = (
    "[orchestrator note] อ่านก่อนเริ่มงาน:\n"
    "- `ห้าม spawn subagent` ใน ROLE prefix หมายถึง AI subagent\n"
    "  เท่านั้น (Task tool / codex delegation flags) — ไม่รวม shell\n"
    "  command ที่คุณรันเองในเทอร์มินัลนี้\n"
    "- เมื่อเสร็จงาน ต้อง **รัน shell command** ผ่าน Bash tool:\n"
    '      takkub done "<one-line summary>"\n'
    '  ห้ามพิมพ์ "takkub done" เป็นข้อความตอบในแชท (orchestrator\n'
    "  มองไม่เห็น → Lead ไม่ทราบว่างานเสร็จ → pane idle ตลอด)\n"
    "- review / analysis tasks: save findings ลงไฟล์ docs/ ก่อน\n"
    "  แล้วค่อย `takkub done` (pane auto-close ~2.5s หลัง done)\n"
    "\n"
    "------ task ------\n"
)


def _rewrite_task_for_codex(task: str) -> str:
    """Prepend an unambiguous override notice when sending a task to a codex pane.

    Codex tends to over-interpret Lead's standard
    `[ROLE: ... ห้าม spawn subagent]` prefix as forbidding any external
    orchestration — including the mandatory `takkub done` shell command.
    The planted AGENTS.md tries to prevent this but loses to the more-
    proximal inline ROLE prefix. We inject a same-proximity clarification
    before the task so the override cannot be ranked below the constraint.
    Idempotent: if the notice marker is already present we return unchanged
    (e.g. orchestrator replays the stored task after auto-respawn).
    """
    if _CODEX_TASK_NOTICE in task:
        return task
    return _CODEX_TASK_NOTICE + task


# Delay between writing the payload and writing the submitting `\r`.
# Claude Code v2.1.x collapses a bracketed-paste block into a
# `[Pasted text #N +M lines]` placeholder before it accepts Enter as a
# submit. Rendering that placeholder takes noticeably longer than the
# 200 ms used for short typing-style writes; an Enter that lands
# mid-render is consumed as a soft newline inside the paste and the
# task never actually submits (the bug surfaced when a teammate pane
# sat at `[Pasted text #1 +15 lines]` forever instead of running the
# spec). Pick the longer delay only when the payload actually came
# back from `_paste_payload` wrapped, so slash-command and short
# message latency stay snappy.
_PASTE_ENTER_DELAY_MS = 800
_TYPING_ENTER_DELAY_MS = 200


def _enter_delay_ms(payload: str) -> int:
    """Pick the post-write delay before sending Enter to submit input."""
    return _PASTE_ENTER_DELAY_MS if payload.startswith(_PASTE_START) else _TYPING_ENTER_DELAY_MS


# Where teammate-pane state lives between cockpit restarts. Lead panes
# are already restored by the open_tabs mechanism in projects.json
# (one Lead per tab). Teammate panes — frontend/backend/qa/etc. that
# the user spawned manually — disappear when cockpit shuts down. The
# session snapshot file records which teammates were live in each tab
# at the moment of shutdown (or at the last periodic tick) so the next
# cockpit launch can re-spawn them; since session UUIDs are in-memory only,
# each role gets a fresh --session-id (clean slate, no cross-session bleed).
#
# Skip snapshots older than _LAST_SESSION_MAX_AGE_SEC: an hour-old
# snapshot is stale enough that the underlying claude conversations
# have probably been compacted out of usefulness and a fresh spawn is
# the right call.
_LAST_SESSION_FILE = RUNTIME_DIR / "last-session.json"
_LAST_SESSION_MAX_AGE_SEC = 60 * 60


# How often the orchestrator rewrites `<vault>/hot.md`. The hot file is
# a low-stakes status snapshot — open Obsidian, see what cockpit is
# doing right now — so the cadence trades freshness for write churn.
# A minute is plenty: the panes themselves render to xterm in real time.
_HOT_MD_INTERVAL_MS = 60_000


def _render_daily_digest(
    project: str,
    when: datetime,
    sessions: list[tuple[str, str, str]],
    decisions: list[dict] | None = None,
) -> str:
    """Render one Finish-Job digest section for a project.

    `sessions` is a list of (HHMMSS, role, note_first_line) tuples
    drawn from `runtime/sessions/<date>/<project>/*.md`. Most recent
    first so the user scanning the daily note sees the latest work
    at the top.

    `decisions` (optional) is a list of {timestamp, heading, ...}
    dicts from `chatlog_scanner.extract_decisions` — assistant
    messages with H2 headings that look like recap / structured
    output. Surfaces under a "Decisions today" sub-bullet so the
    user can scan what was decided without opening any pane.

    Output is a single H2 section so multiple Finish Job invocations
    on the same day (different projects, different times) can append
    without clobbering each other.
    """
    lines: list[str] = []
    lines.append(f"## `{project}` · wrapped at {when.strftime('%H:%M:%S')}")
    lines.append("")
    if not sessions:
        lines.append("_No `takkub done` events recorded today for this project._")
        lines.append("")
    else:
        lines.append(f"**Sessions completed today: {len(sessions)}**")
        lines.append("")
        for stamp, role, note in sessions:
            # First line of the note is the human summary; collapse multi-line
            # notes to one line so the daily file stays scannable.
            first = (note or "").strip().splitlines()[0] if (note or "").strip() else ""
            if first:
                lines.append(f"- `{stamp}` **{role}** — {first}")
            else:
                lines.append(f"- `{stamp}` **{role}**")
        lines.append("")
    if decisions:
        lines.append(f"**Decisions today: {len(decisions)}**")
        lines.append("")
        for d in decisions:
            ts = d.get("timestamp") or ""
            ts_short = ts.replace("T", " ")[:16] if ts else ""
            heading = (d.get("heading") or "").strip()
            if heading:
                lines.append(f"- `{ts_short}` {heading}")
        lines.append("")
    return "\n".join(lines)


def _render_hot_md(
    panes_by_project: dict[str, dict[str, str]],
    active_project_name: str | None,
    recent_sessions: list[tuple[str, str, str]],
    now: datetime,
    hook_counts: dict[str, int] | None = None,
    friction: dict[str, int] | None = None,
) -> str:
    """Compose the body of `<vault>/hot.md` — the "what's happening
    right now in cockpit" snapshot the user opens to orient themselves.

    Inputs are plain values (no Pane / PtySession refs) so this can be
    unit-tested without spinning up Qt. `panes_by_project` is
    `{project: {role: state}}`. `recent_sessions` is a list of
    `(project, role, filename)` tuples — most recent first.
    `hook_counts` is `{hook_bucket: count}` from
    `chatlog_scanner.count_hook_fires` — surfaces noisy hooks
    (GateGuard, cost-critical, loop-warning, etc.) so the user can
    spot which hook is more annoying than useful and decide whether
    to mute it via ECC_DISABLED_HOOKS.
    """
    lines: list[str] = []
    lines.append("# Hot — cockpit live state")
    lines.append("")
    lines.append(f"_Last updated: {now.isoformat(timespec='seconds')}_")
    lines.append("")

    if active_project_name:
        lines.append(f"**Active project:** `{active_project_name}`")
    else:
        lines.append("**Active project:** _(none — projects.json `active` unset)_")
    lines.append("")

    if not panes_by_project:
        lines.append("## Panes")
        lines.append("")
        lines.append("_No projects open in cockpit._")
        lines.append("")
    else:
        lines.append("## Panes")
        lines.append("")
        for project in sorted(panes_by_project):
            lines.append(f"### `{project}`")
            roles = panes_by_project[project]
            if not roles:
                lines.append("- _(no panes)_")
            else:
                for role in sorted(roles):
                    lines.append(f"- **{role}** — {roles[role]}")
            lines.append("")

    lines.append("## Recent `takkub done` (last 10)")
    lines.append("")
    if not recent_sessions:
        lines.append("_(no done events this session)_")
    else:
        for project, role, fname in recent_sessions[:10]:
            lines.append(f"- `{project}` · **{role}** · {fname}")
    lines.append("")

    # Hook noise meter — only render the section when there's
    # something to report so a quiet day doesn't get a wall of zeros.
    if hook_counts:
        lines.append("## Hook noise today")
        lines.append("")
        # Loudest hook first so the eye lands on the worst offender.
        for hook, count in sorted(hook_counts.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"- **{hook}** — {count}")
        lines.append("")

    # Friction heatmap — surface "user corrected claude" and
    # "claude retried the same tool 3+ times" so the user sees
    # where workflow was rough. Same omit-when-empty rule.
    if friction and any(friction.values()):
        lines.append("## Friction today")
        lines.append("")
        c = int(friction.get("corrections", 0))
        r = int(friction.get("tool_retries", 0))
        if c:
            lines.append(f"- **user corrections** — {c}")
        if r:
            lines.append(f"- **tool retry storms** — {r}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "_Auto-written by agent-takkub orchestrator every "
        f"{_HOT_MD_INTERVAL_MS // 1000}s. Edit-safe target is the project "
        "page; this file is overwritten on each tick._"
    )
    lines.append("")
    return "\n".join(lines)


def _cwd_within_project(cwd: str, project: str, role_name: str) -> bool:
    """True when `cwd` resolves under one of `project`'s configured roots.

    The cockpit repo-root bypass is intentionally restricted to Lead: teammates
    of unrelated projects must not inherit Lead's self-edit privileges.
    """
    target = pathlib.Path(cwd).resolve()
    if role_name == LEAD.name and (
        target == REPO_ROOT.resolve() or REPO_ROOT.resolve() in target.parents
    ):
        return True
    return any(target == root or root in target.parents for root in _allowed_project_roots(project))


def _exit_key(project: str, role: str) -> str:
    """Composite key for `_recent_exits` so the same role in different
    project tabs never shares a resume record."""
    return f"{project}::{role}"


def _build_transcript_path(project_ns: str, role_name: str) -> str:
    """Return an absolute path for the PTY byte-stream transcript file.

    The path mirrors the decision-log layout so the two artefacts live
    side-by-side under runtime/sessions/<date>/<project>/:
        <role>-<HHMMSS>.transcript.log   ← raw bytes (this function)
        <role>-<HHMMSS>.md               ← markdown summary (done())
    """
    now = datetime.now()
    day = RUNTIME_DIR / "sessions" / now.strftime("%Y-%m-%d") / project_ns
    try:
        day.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return str(day / f"{role_name}-{now.strftime('%H%M%S')}.transcript.log")


class Orchestrator(QObject):
    """Owns the pane registry and routes commands.

    Layout policy: Lead is always pre-registered (created by main_window) and
    fills the window initially. Teammate panes are created on demand the
    first time we spawn that role, via the `paneRequested` signal which
    main_window connects to its own add-pane logic.
    """

    statusChanged = pyqtSignal()
    leadInjected = pyqtSignal(str)
    # Emitted when user toggles a provider on/off via status bar. main_window
    # listens to refresh chip color/label without polling.
    providerStateChanged = pyqtSignal(str, bool)  # (provider, disabled)
    # Emitted at the tail of a successful spawn that picked up `--resume <uuid>`
    # (i.e. the role's previous session exited within RESUME_WINDOW_SEC).
    # main_window uses this to fire `/remote-control` only on resumes, so a
    # fresh project open doesn't spam the Lead pane with the bridge command.
    paneResumed = pyqtSignal(str, str)  # role_name, project
    paneRequested = pyqtSignal(
        str, str
    )  # role_name, project — main_window adds pane to the matching tab
    paneClosed = pyqtSignal(
        str, str
    )  # role_name, project — main_window removes pane from the matching tab
    agentDone = pyqtSignal(str, str)  # role_name, note — for desktop notifications
    # Emitted when a teammate's done() fires for a project that is NOT the
    # currently active tab. main_window connects this to show a status-bar
    # flash so the user sees background-tab activity without switching tabs.
    crossTabDone = pyqtSignal(str, str, str)  # project_ns, role, note

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # Browser MCPs (playwright + chrome-devtools) follow Lead into
        # every project. Merge them into runtime/shared-mcp.json before
        # any pane spawns — the orchestrator will then hand the file to
        # claude via `--mcp-config` and panes pick the servers up
        # uniformly across projects. Idempotent: safe to call on every
        # boot. Failure is non-fatal (logged once and panes spawn
        # without browser MCPs) so a readonly runtime never blocks
        # cockpit startup.
        try:
            from .shared_dev_tools import ensure_browser_mcps, warm_browser_mcps

            ok, msg = ensure_browser_mcps()
            _log_event("browser_mcp_init", ok=ok, msg=msg)
            # Kick the browser MCP servers in background daemon threads
            # so the npx cache is hot before claude tries to spawn them
            # lazily on first tool call. Non-blocking; failure here is
            # logged at the helper level and the MCPs still work on
            # the slower first call without warm-up.
            warm_browser_mcps()
        except Exception as e:
            _log_event("browser_mcp_init_error", error=repr(e))
        # Merge user's ~/.claude.json mcpServers (obsidian-vault, pms, etc.)
        # into shared-mcp.json so every pane inherits them automatically.
        # Browser MCP entries win on name collision. Non-fatal: failure logs
        # once and panes spawn without user MCPs until the issue is resolved.
        try:
            from .shared_dev_tools import ensure_user_mcps

            ok, msg = ensure_user_mcps()
            _log_event("user_mcp_init", ok=ok, msg=msg)
        except Exception as e:
            _log_event("user_mcp_init_error", error=repr(e))
        # Panes are namespaced per project so the upcoming multi-tab UI
        # (Plan B) can keep each project's Lead + teammates isolated. The
        # `panes` property below resolves to the *active* project's inner
        # dict so every existing caller (UI + tests) keeps the same shape.
        # Until tabs land, only one project namespace is populated at a
        # time and behavior is identical to the pre-refactor single-dict.
        self._panes_by_project: dict[str, dict[str, AgentPane]] = {}
        # last-known cwd per role, used to decide whether the pane's prior
        # session is within the resume window (must match previous cwd)
        self._recent_exits: dict[str, dict] = {}  # "{project}::{role}" -> {cwd, ts}
        # session-id per role: generated at each fresh spawn, kept so a
        # respawn within RESUME_WINDOW_SEC can pass --resume <uuid> and
        # bypass claude's CWD-based --continue resolution (prevents bleed
        # between Lead and teammate panes sharing the same cwd)
        self._session_uuids: dict[str, dict] = {}  # "{project}::{role}" -> {uuid, cwd}
        # Peer CC durability: messages queued when Lead is not alive.
        # Keyed by project namespace; flushed to Lead on next Lead spawn.
        self._pending_lead_cc: dict[str, list[dict]] = {}
        self._load_pending_cc()
        # Done-notice durability: `takkub done` notices queued when Lead is
        # not alive at the moment a teammate finishes. Pattern mirrors
        # _pending_lead_cc; flushed to Lead on next Lead spawn.
        self._pending_done_notices: dict[str, list[dict]] = {}

        # Per-cockpit-run capability token. Injected only into the Lead pane
        # env (TAKKUB_LEAD_TOKEN) so the Lead takkub CLI can authenticate
        # Lead-only server commands. Teammates don't get it — their CLI calls
        # will be rejected server-side even if they connect to the socket.
        # Generated fresh each boot; never written to disk, logs, or argv.
        self._lead_token: str = secrets.token_urlsafe(32)

        # Idle watchdog bookkeeping. Per-role:
        #   first_idle_ts   — when the pane was first seen idle in this streak
        #                     (None = currently processing or not "working")
        #   last_reminder_ts — last time we injected a reminder (0 = never)
        self._idle_state: dict[str, dict[str, float | None]] = {}
        # Per-pane "waiting for Lead's reply" timestamp. Keyed
        # `<project>::<role>`. Populated when a teammate sends a message
        # to Lead via `takkub send --to lead "..."` (see `send()`),
        # cleared when Lead sends back to that teammate or when the
        # pane is closed/respawned. The idle watchdog skips panes
        # whose key is in this dict so the auto-reminder doesn't fire
        # while a teammate is legitimately stuck waiting for spec.
        self._blocked_on_lead: dict[str, float] = {}
        # Per-pane consecutive auto-respawn counter. Keyed `<project>::<role>`.
        # Bumped on each unexpected exit + auto-respawn; reset on a clean
        # `close()` / `done()` / manual respawn. Capped at AUTO_RESPAWN_MAX
        # so the orchestrator gives up if claude refuses to come back.
        self._auto_respawn_attempts: dict[str, int] = {}
        # Last task sent via assign() per pane. Keyed `<project>::<role>`.
        # Used by _auto_respawn() to replay the task into the fresh session so
        # a crash-and-respawn cycle doesn't silently drop the work.
        # Cleared on manual close() so a deliberate restart doesn't replay.
        self._last_assigned_task: dict[str, str] = {}
        # Opt-in done gate: when assign() was called with requires_commit=True,
        # done() rejects the agent until git working tree is clean. Keyed
        # `<project>::<role>`, cleared by close() and on successful done().
        self._requires_commit_on_done: dict[str, bool] = {}

        # Opt-in auto-chain: when assign() was called with auto_chain=True,
        # done() injects a pre-authorisation handoff prompt to Lead AFTER
        # all auto-chain panes in the same project have reported done.
        # Keyed `<project>::<role>`, cleared by close() and on done().
        self._auto_chain_panes: dict[str, bool] = {}
        # Last stuck-recover wall-clock per pane (key `<project>::<role>`).
        # Prevents the watchdog from looping recover→stuck→recover on a
        # chronically wedged claude.
        self._last_stuck_recover: dict[str, float] = {}
        # Codex early-crash instrumentation. Keyed `<project>::<role>`.
        # Records wall-clock at spawn so _on_codex_exit() can compute
        # time-to-exit and decide whether to write a crash dump.
        self._codex_spawn_times: dict[str, float] = {}
        # Stall detection: last successful `takkub send` delivery timestamp.
        # Keyed `<project>::<role>`. One of three signals checked by
        # _compute_last_progress_ts(); the others are transcript mtime and
        # today's screenshot dir mtime. Cleared on close().
        self._last_send_ts: dict[str, float] = {}
        # Harvest hint cooldown. Keyed `<project>::<role>`. Records when
        # the last harvest hint was injected into Lead so the watchdog
        # doesn't spam the same message every tick.
        self._harvest_hint_ts: dict[str, float] = {}
        self._idle_watchdog = QTimer(self)
        self._idle_watchdog.setInterval(IDLE_WATCHDOG_INTERVAL_MS)
        self._idle_watchdog.timeout.connect(self._check_idle_teammates)
        if IDLE_REMIND_AFTER_S > 0:
            self._idle_watchdog.start()

        # Periodic snapshot of cockpit state to `<vault>/hot.md`. Skipped
        # silently when no vault is configured (see `_resolve_vault_dir`).
        # In-process list of the last few `takkub done` events drives the
        # "Recent" section without hitting disk on every tick.
        self._recent_done: list[tuple[str, str, str]] = []
        self._hot_md_timer = QTimer(self)
        self._hot_md_timer.setInterval(_HOT_MD_INTERVAL_MS)
        self._hot_md_timer.timeout.connect(self._write_hot_md)
        self._hot_md_timer.start()

    # ──────────────────────────────────────────────────────────────
    # project-aware view onto the pane registry
    # ──────────────────────────────────────────────────────────────
    @staticmethod
    def _resolve_project(project: str | None) -> str:
        """Pick a namespace key. Resolves None to the currently active
        project from projects.json, falling back to a sentinel "default"
        when no project is configured (typical in unit tests)."""
        if project:
            validate_name(project, "project")  # raises ValueError on traversal attempts
            return project
        name, _ = active_project()
        return name or "default"

    def _project_panes(self, project: str | None = None) -> dict[str, AgentPane]:
        """Return (and lazily create) the inner pane dict for `project`.

        Always returns the same dict instance for a given project, so
        callers can hold a reference and mutate it directly — that's how
        `self.panes` works for the active project."""
        return self._panes_by_project.setdefault(self._resolve_project(project), {})

    @property
    def panes(self) -> dict[str, AgentPane]:
        """Active project's pane dict. Backwards-compatible with the
        pre-Phase-1 single-namespace API — existing callers that read or
        write `orch.panes["backend"]` continue to operate on the active
        project's panes without knowing about the project dimension."""
        return self._project_panes()

    # ──────────────────────────────────────────────────────────────
    # registration (main_window builds panes and registers them)
    # ──────────────────────────────────────────────────────────────
    def register_pane(self, pane: AgentPane, project: str | None = None) -> None:
        self._project_panes(project)[pane.role.name] = pane
        pane.spawnRequested.connect(self._on_pane_spawn_clicked)
        pane.closeRequested.connect(self._on_pane_close_clicked)
        pane.inputBytes.connect(self._on_pane_input)
        self.statusChanged.emit()

    def unregister_pane(
        self, role_name: str, project: str | None = None, force: bool = False
    ) -> None:
        # registry never wipes Lead unless cockpit tearing down (tab close → force=True)
        if role_name == LEAD.name and not force:
            _log_event("unregister_pane_lead_refused")
            return
        # no paneClosed signal — caller (_remove_teammate_pane / tab close) coordinates UI removal
        pane = self._project_panes(project).pop(role_name, None)
        if pane is None:
            return
        if pane.session is not None:
            pane.session.terminate()
        self.statusChanged.emit()

    # ──────────────────────────────────────────────────────────────
    # high-level operations
    # ──────────────────────────────────────────────────────────────
    def spawn(
        self, role_name: str, cwd: str | None = None, project: str | None = None
    ) -> tuple[bool, str]:
        try:
            role_name = validate_name(role_name, "role")
        except ValueError as exc:
            return False, str(exc)
        project_ns = self._resolve_project(project)
        project_panes = self._project_panes(project_ns)
        pane = project_panes.get(role_name)
        if pane is None:
            # ask main_window to create + register the pane, then retry
            self.paneRequested.emit(role_name, project_ns)
            pane = project_panes.get(role_name)
            if pane is None:
                return False, f"unknown role: {role_name}"

        if pane.session is not None and pane.session.is_alive:
            return True, f"{role_name} already running"

        # Fresh spawn — clear any stale watchdog tracking from a prior
        # session so the new claude conversation starts with a clean slate
        # (no leftover "blocked on lead" flag, no leftover idle streak).
        # Auto-respawn attempts are *not* cleared here because spawn() is
        # also the path the auto-respawn watcher takes; clearing would
        # let a deterministically-crashing claude loop forever.
        key = f"{project_ns}::{role_name}"
        self._idle_state.pop(key, None)
        self._blocked_on_lead.pop(key, None)

        # Fix 1: validate explicit cwd stays within the project's configured paths.
        # "default" namespace (unit-test / no-project) is exempt since it has no
        # configured paths to validate against. The cockpit repo itself is always
        # allowed so Lead can self-edit cockpit files (CLAUDE.md, projects.json, …).
        if cwd and project_ns != "default" and not _cwd_within_project(cwd, project_ns, role_name):
            return False, f"cwd '{cwd}' is outside project '{project_ns}' paths"

        # ── shell pane: plain PowerShell, no agent ──────────────────
        # The "Open Shell" status-bar button drops the user into a raw
        # pwsh prompt inside the cockpit grid — handy for one-off git
        # pokes / log tails without losing context to another window.
        # Skips every claude/codex/gemini flag, every CLAUDE.md inject,
        # and every MCP/plugin wiring. The pane still emits processExited
        # through the generic _on_session_exit handler so a closed shell
        # leaves the slot in the same "exited" state as any other pane.
        if role_name == "shell":
            import shutil as _shutil

            # winpty's ConPTY backend can't handle full paths that contain
            # spaces (e.g. `"C:\Program Files\PowerShell\7\pwsh.EXE"` gets
            # split at the space before quoting takes effect, surfacing as
            # `command not found: C:\Program`). Detect the binary so we
            # fail fast with a clear message, then hand the **basename** to
            # winpty and let it resolve via PATH — which the cockpit
            # controls via _build_pane_env() + the bin/ prepend below.
            pwsh_full = _shutil.which("pwsh") or _shutil.which("powershell")
            if pwsh_full is None:
                return False, "PowerShell not on PATH (looked for pwsh / powershell)"
            pwsh_basename = (
                "pwsh.exe" if pwsh_full.lower().endswith("pwsh.exe") else "powershell.exe"
            )
            spawn_cwd = cwd or default_cwd_for_role(role_name, project=project_ns) or str(REPO_ROOT)
            env = _build_pane_env()
            env["TAKKUB_ROLE"] = role_name
            env["TAKKUB_PROJECT"] = project_ns
            bin_dir = str(REPO_ROOT / "bin")
            env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
            shell_argv = [pwsh_basename, "-NoLogo"]
            session = PtySession(cols=110, rows=36, parent=self)
            _t_path = _build_transcript_path(project_ns, role_name)
            pane._transcript_path = _t_path
            try:
                session.spawn(argv=shell_argv, cwd=spawn_cwd, env=env, transcript_path=_t_path)
            except Exception as e:
                return False, f"failed to spawn shell: {e}"
            pane.attach_session(session, cwd=spawn_cwd)
            session.processExited.connect(
                lambda _code, r=role_name, c=spawn_cwd, p=project_ns: self._on_session_exit(r, c, p)
            )
            _ekey = _exit_key(project_ns, role_name)
            if _ekey in self._recent_exits:
                del self._recent_exits[_ekey]
            self.statusChanged.emit()
            _log_event("spawn", role=role_name, cwd=spawn_cwd, resumed=False)
            return True, f"shell spawned in {spawn_cwd}"

        # ── codex pane: non-claude path ─────────────────────────────
        # `codex` is OpenAI's TUI; it speaks a different protocol and
        # doesn't understand any of the claude flags below. Build a
        # minimal argv and short-circuit so we don't accidentally pass
        # `--dangerously-skip-permissions`, MCP configs, plugin dirs,
        # or `--session-id`/`--resume` (all claude-only) to it.
        #
        # Entry condition uses `provider_for(role_name)` so the user
        # can remap any teammate role (e.g. "backend") to the codex
        # binary via `~/.takkub/role-providers.json`. The `codex` role
        # itself is forced into this branch by provider_config's
        # `_FORCED_PROVIDER` table.
        from .provider_config import CODEX, GEMINI, provider_for

        if provider_for(role_name) == GEMINI:
            from .gemini_helper import find_gemini_executable
            from .gemini_md import ensure_gemini_md

            gemini_bin = find_gemini_executable()
            if gemini_bin is None:
                return False, (
                    "gemini binary not on PATH. Install with "
                    "`npm install -g @google/gemini-cli`, then run `gemini` once to log in."
                )
            spawn_cwd = cwd or default_cwd_for_role(role_name, project=project_ns) or str(REPO_ROOT)
            ensure_gemini_md(spawn_cwd)
            env = _build_pane_env()
            env["TAKKUB_ROLE"] = role_name
            env["TAKKUB_PROJECT"] = project_ns
            bin_dir = str(REPO_ROOT / "bin")
            env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
            gemini_argv = [
                gemini_bin,
                "-y",  # yolo: skip per-command approval prompts (parity with codex --ask-for-approval never)
            ]
            session = PtySession(cols=110, rows=36, parent=self)
            _t_path = _build_transcript_path(project_ns, role_name)
            pane._transcript_path = _t_path
            try:
                session.spawn(argv=gemini_argv, cwd=spawn_cwd, env=env, transcript_path=_t_path)
            except Exception as e:
                return False, f"failed to spawn gemini: {e}"
            pane.attach_session(session, cwd=spawn_cwd)
            session.processExited.connect(
                lambda _code, r=role_name, c=spawn_cwd, p=project_ns: self._on_session_exit(r, c, p)
            )
            _ekey = _exit_key(project_ns, role_name)
            if _ekey in self._recent_exits:
                del self._recent_exits[_ekey]
            self._auto_trust(role_name, project=project_ns)
            self.statusChanged.emit()
            _log_event("spawn", role=role_name, cwd=spawn_cwd, resumed=False)
            return True, f"gemini spawned in {spawn_cwd}"

        if provider_for(role_name) == CODEX:
            from .codex_agents_md import ensure_agents_md
            from .codex_helper import find_codex_executable

            codex_bin = find_codex_executable()
            if codex_bin is None:
                return False, (
                    "codex binary not on PATH. Install with "
                    "`npm install -g @openai/codex`, then run `codex login` once."
                )
            spawn_cwd = cwd or default_cwd_for_role(role_name, project=project_ns) or str(REPO_ROOT)
            # Plant the takkub cheatsheet so Codex auto-discovers it on
            # boot and knows how to call `takkub send/done`. Safe: only
            # writes when the file is absent or already takkub-managed
            # (marker check inside the helper).
            ensure_agents_md(spawn_cwd)
            env = _build_pane_env()
            env["TAKKUB_ROLE"] = role_name
            env["TAKKUB_PROJECT"] = project_ns
            bin_dir = str(REPO_ROOT / "bin")
            env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
            # Autonomy flags so Codex can call `takkub done` and edit
            # workspace files without stopping for per-command approval —
            # mirrors claude's `--dangerously-skip-permissions`.
            #
            # Windows: codex 0.133 interactive TUI still spawns
            # `codex-windows-sandbox-setup.exe` on the first shell tool
            # call even with `-s danger-full-access`. That helper has a
            # `requireAdministrator` manifest, so under a non-elevated
            # cockpit it ENOENTs out as "windows sandbox: spawn setup
            # refresh" and the pane can't run any command (issue #5).
            # `--dangerously-bypass-approvals-and-sandbox` is the codex-
            # documented escape hatch that skips the helper entirely —
            # same net trust as `-s danger-full-access` we already use.
            #
            # Linux/macOS: keep workspace-write so the OS sandbox still
            # constrains an off-the-rails codex to its cwd.
            import sys

            if sys.platform == "win32":
                codex_argv = [
                    codex_bin,
                    "--dangerously-bypass-approvals-and-sandbox",
                ]
            else:
                codex_argv = [
                    codex_bin,
                    "--ask-for-approval",
                    "never",
                    "-s",
                    "workspace-write",
                ]
            session = PtySession(cols=110, rows=36, parent=self)
            _t_path = _build_transcript_path(project_ns, role_name)
            pane._transcript_path = _t_path
            try:
                session.spawn(argv=codex_argv, cwd=spawn_cwd, env=env, transcript_path=_t_path)
            except Exception as e:
                return False, f"failed to spawn codex: {e}"
            pane.attach_session(session, cwd=spawn_cwd)
            _ekey = _exit_key(project_ns, role_name)
            self._codex_spawn_times[_ekey] = time.time()
            session.processExited.connect(
                lambda code, r=role_name, c=spawn_cwd, p=project_ns, sess=session: (
                    self._on_codex_exit(code, r, c, p, sess)
                )
            )
            if _ekey in self._recent_exits:
                del self._recent_exits[_ekey]
            self._auto_trust(role_name, project=project_ns)
            self.statusChanged.emit()
            _log_event("spawn", role=role_name, cwd=spawn_cwd, resumed=False)
            return True, f"codex spawned in {spawn_cwd}"

        # Resolve cwd:
        #   Lead          → repo root (so CLAUDE.md auto-discovery picks up the
        #                   Lead instructions at agent-takkub/CLAUDE.md)
        #   teammate      → explicit --cwd, else active project's role-matched
        #                   path (frontend→web, backend→api, ...), else the
        #                   role's runtime staging dir
        role_md_file: str | None = None
        if role_name == LEAD.name:
            # Lead works *on* the active project, not on the cockpit's own
            # source. cwd defaults to the project's root (common parent of
            # its `paths`) so claude reads the project's CLAUDE.md, runs
            # `git status` against the right repo, and tools land in the
            # user's actual codebase. The cockpit's CLAUDE.md (takkub
            # cheatsheet + role guide) is appended as system prompt so
            # Lead still knows about `takkub assign / send / done / ...`.
            spawn_cwd = cwd or lead_cwd(project=project_ns) or str(REPO_ROOT)
            # Render Lead's system prompt fresh each spawn so BLOCKED_DIRS
            # tracks whatever project is active in projects.json right now.
            # Skip injection when Lead is anchored at the cockpit itself
            # (no project context to enforce).
            if spawn_cwd != str(REPO_ROOT):
                post_compact_brief = self._build_post_compact_brief(project_ns)
                role_md_file = _render_lead_context(
                    project_ns, post_compact_brief=post_compact_brief
                )
        else:
            staging = agent_role_dir(role_name)
            spawn_cwd = cwd or default_cwd_for_role(role_name, project=project_ns) or str(staging)
            # When cwd is a project path, claude auto-discovers the project's
            # CLAUDE.md, not the role's specialist override. Pass the role's
            # markdown to --append-system-prompt-file so the specialist rules
            # always apply regardless of where we land. (Using the *file*
            # variant avoids command-line escaping problems with multiline
            # markdown containing backticks, asterisks, and Thai text.)
            role_md_path = staging / "CLAUDE.md"
            if role_md_path.exists():
                role_md_file = str(role_md_path)

        try:
            claude = find_claude_executable()
        except RuntimeError as e:
            return False, str(e)

        env = os.environ.copy() if role_name == LEAD.name else _build_pane_env()
        env["TAKKUB_ROLE"] = role_name
        # Tag the pane with its project so the `takkub` CLI inside the
        # session can stamp every JSON request with `from_project`. The
        # cli_server uses that to scope routing to panes in the *same*
        # project — under the multi-tab refactor a Lead in unirecon
        # mustn't accidentally send to a backend pane that belongs to pms.
        env["TAKKUB_PROJECT"] = project_ns
        # Inject the Lead capability token only into the Lead pane so its
        # takkub CLI can authenticate Lead-only server commands. Teammates
        # don't get this env var — the server will reject their Lead-only
        # requests even if they dial the TCP socket directly.
        if role_name == LEAD.name:
            env["TAKKUB_LEAD_TOKEN"] = self._lead_token
        bin_dir = str(REPO_ROOT / "bin")
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")

        # If rtk lives somewhere `shutil.which` can't see (typical when
        # pythonw inherits a thinner PATH than the cmd that spawned the
        # cockpit), prepend its directory so the Bash PreToolUse hook that
        # may sit in the project's .claude/settings.json can still execute
        # `rtk hook claude` from within the pane.
        try:
            from .rtk_helper import find_rtk_binary

            rtk_path = find_rtk_binary()
        except Exception:
            rtk_path = None
        if rtk_path:
            rtk_dir = str(pathlib.Path(rtk_path).resolve().parent)
            if rtk_dir not in env["PATH"].split(os.pathsep):
                env["PATH"] = rtk_dir + os.pathsep + env["PATH"]

        # QA pane uses `@runablehq/mini-browser` for e2e/smoke flows.
        # The `mb-start-chrome` helper looks for $CHROME_BIN before
        # falling back to "Chrome not found". Probe the typical Windows
        # install paths once at spawn time so the QA agent doesn't have
        # to remember to export the variable in every shell. Skip if
        # the user already provides CHROME_BIN at the cockpit level.
        if role_name == "qa" and "CHROME_BIN" not in env:
            for cand in (
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                str(pathlib.Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe"),
            ):
                if pathlib.Path(cand).is_file():
                    env["CHROME_BIN"] = cand
                    break

        _apply_mcp_timeout(env)
        _apply_ecc_mute(env)
        apply_claude_auth_overrides(env)

        # --setting-sources controls which settings.json layers claude loads.
        # We default to `project,local` (skip ~/.claude/settings.json) because
        # the claude-obsidian plugin currently ships a SessionStart hook that
        # crashes with `ToolUseContext is required for prompt hooks. This is a
        # bug.` whenever it fires inside a cockpit-spawned session.
        #
        # To still give agents access to superpowers + agent-skills, we hand
        # those plugins to claude *explicitly* via --plugin-dir (see below).
        # Override the whole policy with TAKKUB_SETTING_SOURCES env var.
        sources = os.environ.get("TAKKUB_SETTING_SOURCES", "project,local")
        # Both Lead and teammates run with --dangerously-skip-permissions.
        # The write-boundary for Lead (don't touch project paths) is now a
        # soft policy in CLAUDE.md only — the previous deny-rule guard was
        # removed because the per-Bash / per-tool permission prompts it
        # exposed broke flow ("ต้องกด enter ตลอด ๆ งานไม่จบ").
        argv: list[str] = [
            claude,
            "--dangerously-skip-permissions",
            "--setting-sources",
            sources,
        ]

        # Teammate speed tier. Lead does orchestration (planning, multi-step
        # reasoning, coordinating teammates) and stays on the user's
        # default model + effort. Teammates execute focused specialist work
        # (edit files, run commands, verify) and benefit from running on a
        # faster model — but not as fast as Haiku, because the cockpit
        # owner runs on a Claude Max subscription (not the API) where
        # per-token cost is irrelevant and Sonnet's quality margin matters
        # more than Haiku's raw-speed margin. Sonnet 4.6 at medium effort
        # gives roughly 1.5-2x Opus speed while keeping enough reasoning
        # to handle refactors / integrations / code review without
        # subtle-bug rework cycles. Override via:
        #
        #   TAKKUB_TEAMMATE_MODEL=""                   → no --model (user default)
        #   TAKKUB_TEAMMATE_MODEL="claude-haiku-4-5"   → fastest tier
        #   TAKKUB_TEAMMATE_MODEL="claude-opus-4-7"    → match Lead
        #   TAKKUB_TEAMMATE_EFFORT=""                  → no --effort
        #   TAKKUB_TEAMMATE_EFFORT="high"              → match Lead's effort
        if role_name != LEAD.name:
            teammate_model = os.environ.get("TAKKUB_TEAMMATE_MODEL", "claude-sonnet-4-6").strip()
            if teammate_model:
                argv.extend(["--model", teammate_model])
            teammate_effort = os.environ.get("TAKKUB_TEAMMATE_EFFORT", "medium").strip()
            if teammate_effort:
                argv.extend(["--effort", teammate_effort])

        # Explicit plugin allowlist (skip the broken claude-obsidian hook).
        # Set TAKKUB_EXTRA_PLUGINS env var to a `;`-separated list of plugin
        # root dirs (must each contain `.claude-plugin/plugin.json`) to add
        # more, or set it to empty string to suppress the defaults.
        plugin_default = ";".join(_default_plugin_dirs())
        plugin_dirs_raw = os.environ.get("TAKKUB_EXTRA_PLUGINS", plugin_default)
        for pdir in [p.strip() for p in plugin_dirs_raw.split(";") if p.strip()]:
            if (pathlib.Path(pdir) / ".claude-plugin" / "plugin.json").exists():
                argv.extend(["--plugin-dir", pdir])
        if role_md_file:
            argv.extend(["--append-system-prompt-file", role_md_file])

        # (Lead write-boundary used to inject a per-project deny-rule
        # settings file here. Removed when Lead switched to
        # --dangerously-skip-permissions: deny rules are bypassed under
        # that flag anyway, so the file would have been ignored. The
        # write boundary is now a soft policy in CLAUDE.md.)

        # Inject the cockpit's shared MCP config so every spawned claude
        # session has the right MCPs available regardless of what the
        # project's own `.claude/settings.json` contains. Uses role-aware
        # filter so browser MCPs (~15k tokens of tool schemas) only load
        # for roles that actually do UI work (qa/critic/designer). Lead and
        # other roles get a smaller filtered config. Roles with no policy
        # fall back to the full master file. Skipped silently if there's
        # no config yet.
        try:
            from .shared_dev_tools import shared_mcp_config_path_for_role

            mcp_cfg = shared_mcp_config_path_for_role(role_name)
        except Exception:
            mcp_cfg = None
        if mcp_cfg:
            argv.extend(["--mcp-config", mcp_cfg])
            # Force claude to use *only* our cockpit-managed MCP config so
            # user-level entries registered via `claude mcp add` don't
            # shadow or duplicate what the cockpit provides.
            argv.append("--strict-mcp-config")

        # Hard-deny built-in tools that don't fit the cockpit's
        # delegation model:
        #
        #   Task             — every pane. Lead delegates via `takkub
        #                      assign`, never via the built-in subagent
        #                      dispatcher. Teammates are already
        #                      specialists and don't need to fan out
        #                      further. Override with TAKKUB_ALLOW_TASK=1
        #                      for workflows that genuinely need
        #                      superpowers' parallel-agents skill.
        #
        #   AskUserQuestion  — *teammate* panes only. The tool opens a
        #                      blocking interactive dropdown in the
        #                      pane, which the cockpit owner has to
        #                      click through manually. The whole point
        #                      of teammate panes is that *Lead* talks
        #                      to the user; teammates should bounce
        #                      questions to Lead via
        #                      `takkub send --to lead "..."`. Lead's
        #                      own pane keeps AskUserQuestion enabled
        #                      because that's the legitimate channel to
        #                      the cockpit owner.
        denied: list[str] = []
        if os.environ.get("TAKKUB_ALLOW_TASK", "0") != "1":
            denied.append("Task")
        if role_name != LEAD.name:
            denied.append("AskUserQuestion")
        if denied:
            argv.extend(["--disallowed-tools", " ".join(denied)])

        # Session resume: if this same role exited recently from the same
        # cwd and we have its session UUID, use --resume <uuid> so claude
        # rejoins the exact conversation without CWD-based disambiguation.
        # This avoids bleed where Lead and a teammate sharing the same cwd
        # could each inherit the other's history via --continue.
        # On a fresh spawn (no prior UUID or expired window), generate a new
        # UUIDv4 and pass --session-id so claude tracks the session from the start.
        resumed = False
        _ekey_spawn = _exit_key(project_ns, role_name)
        prior_uuid = self._session_uuids.get(_ekey_spawn)
        prior_exit = self._recent_exits.get(_ekey_spawn)
        can_resume = (
            prior_uuid is not None
            and prior_uuid.get("cwd") == spawn_cwd
            and prior_exit is not None
            and (time.time() - prior_exit.get("ts", 0)) < RESUME_WINDOW_SEC
        )
        if can_resume:
            argv.extend(["--resume", prior_uuid["uuid"]])
            resumed = True
        else:
            new_uuid = str(_uuid.uuid4())
            argv.extend(["--session-id", new_uuid])
            self._session_uuids[_ekey_spawn] = {"uuid": new_uuid, "cwd": spawn_cwd}

        session = PtySession(cols=110, rows=36, parent=self)
        _t_path = _build_transcript_path(project_ns, role_name)
        pane._transcript_path = _t_path
        try:
            session.spawn(argv=argv, cwd=spawn_cwd, env=env, transcript_path=_t_path)
        except Exception as e:
            return False, f"failed to spawn claude: {e}"

        pane.attach_session(session, cwd=spawn_cwd)
        # Record exits so the auto-respawn watcher knows which project
        # namespace owned the pane that just died.
        session.processExited.connect(
            lambda _code, r=role_name, c=spawn_cwd, p=project_ns: self._on_session_exit(r, c, p)
        )
        # forget the prior exit record now that we've spawned successfully
        if _ekey_spawn in self._recent_exits:
            del self._recent_exits[_ekey_spawn]

        self._auto_trust(role_name, project=project_ns)
        self.statusChanged.emit()
        if resumed:
            # main_window listens for this to auto-bridge `/remote-control`
            # exclusively on resumes — fresh boots stay silent.
            self.paneResumed.emit(role_name, project_ns)
        # Flush any CC messages queued while Lead was offline.
        # Give the session a few seconds to reach the ready prompt before
        # trying to write; if Lead isn't ready yet, _flush_pending_lead_cc
        # is a no-op and we rely on the next send() or a future spawn to retry.
        if role_name == LEAD.name and self._pending_lead_cc.get(project_ns):
            QTimer.singleShot(
                5_000,
                lambda p=project_ns: self._flush_pending_lead_cc(p),
            )
        if role_name == LEAD.name and self._pending_done_notices.get(project_ns):
            QTimer.singleShot(
                5_000,
                lambda p=project_ns: self._flush_pending_done_notices(p),
            )
        _log_event(
            "spawn",
            role=role_name,
            cwd=spawn_cwd,
            resumed=resumed,
        )
        suffix = " (resumed)" if resumed else ""
        return True, f"{role_name} spawned in {spawn_cwd}{suffix}"

    def _on_codex_exit(
        self,
        exit_code: int,
        role_name: str,
        cwd: str,
        project: str,
        session: PtySession,
    ) -> None:
        """Codex-specific exit handler. Detects early crashes and writes a
        diagnostic dump before delegating to the generic _on_session_exit.

        An 'early crash' is any exit within CODEX_EARLY_CRASH_WINDOW_SEC of
        spawning — exactly the pattern observed (codex dies ~50s after boot
        without any visible error).  The dump captures enough context to
        falsify the two top hypotheses: env-missing vars and MCP-boot race.
        """
        ekey = _exit_key(project, role_name)
        spawn_ts = self._codex_spawn_times.pop(ekey, None)
        time_to_exit = (time.time() - spawn_ts) if spawn_ts is not None else None

        if time_to_exit is not None and time_to_exit <= CODEX_EARLY_CRASH_WINDOW_SEC:
            self._write_codex_crash_dump(
                role_name=role_name,
                project=project,
                cwd=cwd,
                exit_code=exit_code,
                time_to_exit=time_to_exit,
                session=session,
            )

        self._on_session_exit(role_name, cwd, project)

    def _write_codex_crash_dump(
        self,
        *,
        role_name: str,
        project: str,
        cwd: str,
        exit_code: int,
        time_to_exit: float,
        session: PtySession,
    ) -> None:
        """Write a plaintext diagnostic dump for a codex early-crash to
        runtime/codex_crash_dumps/<ts>-<project>-<role>.log.

        The dump is human-readable (not JSONL) because the main consumer is
        a developer reading it in a text editor after a repro.
        """
        try:
            ensure_runtime()
            dump_dir = RUNTIME_DIR / "codex_crash_dumps"
            dump_dir.mkdir(parents=True, exist_ok=True)
            ts_str = datetime.now().strftime("%Y%m%dT%H%M%S")
            safe_project = project.replace("/", "_").replace("\\", "_")
            dump_path = dump_dir / f"{ts_str}-{safe_project}-{role_name}.log"

            # Last visible screen content from the pyte buffer (best-effort).
            try:
                output_tail = "\n".join(session.display_lines())
            except Exception:
                output_tail = "(unavailable)"

            # Env keys the filtered pane env would have contained at spawn time.
            # Re-build from _build_pane_env() — same logic as spawn, values omitted.
            env_keys = sorted(_build_pane_env().keys())

            lines = [
                f"# Codex early-crash dump — {ts_str}",
                f"role:         {role_name}",
                f"project:      {project}",
                f"cwd:          {cwd}",
                f"exit_code:    {exit_code}",
                f"time_to_exit: {time_to_exit:.1f}s",
                f"threshold:    {CODEX_EARLY_CRASH_WINDOW_SEC}s",
                "",
                "## env keys present in pane",
                ", ".join(env_keys) if env_keys else "(unavailable)",
                "",
                "## last PTY output (tail)",
                output_tail,
                "",
            ]
            dump_path.write_text("\n".join(lines), encoding="utf-8")

            _log_event(
                "codex_early_crash",
                role=role_name,
                project=project,
                exit_code=exit_code,
                time_to_exit_s=round(time_to_exit, 1),
                dump=str(dump_path),
            )
        except Exception:
            pass  # crash dump must never crash the orchestrator

    def _on_session_exit(self, role_name: str, cwd: str, project: str) -> None:
        """Track recent exits so a quick respawn can pass --resume <uuid>, then
        decide whether to auto-respawn.

        Auto-respawn fires only when the pane is in the `exited` state —
        that's the marker AgentPane sets when claude vanished without a
        matching `mark_expected_exit()` from `orchestrator.close()` /
        `done()`. Capped by AUTO_RESPAWN_MAX so a deterministically-
        crashing claude can't spawn-loop.
        """
        self._recent_exits[_exit_key(project, role_name)] = {"cwd": cwd, "ts": time.time()}

        pane = self._panes_by_project.get(project, {}).get(role_name)
        if pane is None or pane.state != "exited":
            return

        key = f"{project}::{role_name}"
        attempts = self._auto_respawn_attempts.get(key, 0)
        if attempts >= AUTO_RESPAWN_MAX:
            _log_event(
                "auto_respawn_capped",
                role=role_name,
                project=project,
                attempts=attempts,
            )
            return
        self._auto_respawn_attempts[key] = attempts + 1
        _log_event(
            "auto_respawn_scheduled",
            role=role_name,
            project=project,
            attempt=attempts + 1,
        )
        QTimer.singleShot(
            AUTO_RESPAWN_DELAY_MS,
            lambda r=role_name, c=cwd, p=project: self._auto_respawn(r, c, p),
        )

    def _auto_respawn(self, role_name: str, cwd: str, project: str) -> None:
        """Schedule a fresh spawn for a pane that crashed unexpectedly.
        `--resume <uuid>` is picked automatically by `spawn()` because the
        previous exit is still inside RESUME_WINDOW_SEC and UUID is cached."""
        # If the pane was already manually respawned during the delay,
        # bail. The new session would have already cleared `state`.
        pane = self._panes_by_project.get(project, {}).get(role_name)
        if pane is None or (pane.session is not None and pane.session.is_alive):
            return
        ok, msg = self.spawn(role_name, cwd=cwd, project=project)
        _log_event("auto_respawn_done", role=role_name, project=project, ok=ok, msg=msg[:160])
        if ok:
            cached_task = self._last_assigned_task.get(_exit_key(project, role_name))
            if cached_task:
                _log_event(
                    "auto_respawn_replay",
                    role=role_name,
                    project=project,
                    task_preview=cached_task[:120],
                )
                self._send_when_ready(role_name, cached_task, project=project)

    # ──────────────────────────────────────────────────────────────
    def _auto_trust(self, role_name: str, project: str | None = None) -> None:
        """Watch the pane and auto-press Enter on claude's trust folder modal.

        Polls every 500ms for up to 30s. Stops as soon as the prompt is
        accepted (or the session dies / never shows it).
        """
        pane = self._project_panes(project).get(role_name)
        if pane is None:
            return
        elapsed = [0]
        max_ms = 30_000

        def _check() -> None:
            if pane.session is None or not pane.session.is_alive:
                return
            if pane.session.is_at_trust_prompt():
                # option 1 (Yes) is preselected; just hit Enter
                pane.session.write("\r")
                return
            elapsed[0] += 500
            if elapsed[0] >= max_ms:
                return
            QTimer.singleShot(500, _check)

        QTimer.singleShot(1_000, _check)

    def assign(
        self,
        role_name: str,
        cwd: str | None,
        task: str,
        requires_commit: bool = False,
        auto_chain: bool = False,
        project: str | None = None,
    ) -> tuple[bool, str]:
        ok, msg = self.spawn(role_name, cwd=cwd, project=project)
        if not ok:
            return ok, msg

        from .provider_config import CODEX, provider_for

        if provider_for(role_name) == CODEX:
            task = _rewrite_task_for_codex(task)

        project_ns = self._resolve_project(project)
        key = _exit_key(project_ns, role_name)
        self._last_assigned_task[key] = task
        if requires_commit:
            self._requires_commit_on_done[key] = True
        if auto_chain:
            self._auto_chain_panes[key] = True
        self._send_when_ready(role_name, task, project=project)
        _log_event(
            "assign",
            role=role_name,
            cwd=cwd,
            task_preview=task[:120],
            requires_commit=requires_commit,
            auto_chain=auto_chain,
        )
        return True, f"task queued for {role_name} (sending when ready)"

    def inject_slash_command_when_ready(
        self,
        role_name: str,
        command: str,
        max_wait_ms: int = 45_000,
        project: str | None = None,
    ) -> None:
        """Type a Claude Code slash command (e.g. `/remote-control`) into a
        pane as soon as it reaches the idle prompt. Unlike `_send_when_ready`,
        this does *not* flip the pane to the `working` state — slash commands
        are housekeeping, not tasks. If the pane never becomes ready within
        `max_wait_ms`, the command is silently dropped (we'd rather skip than
        paste into a half-built UI).
        """
        pane = self._project_panes(project).get(role_name)
        if pane is None:
            return
        elapsed = [0]
        sent = [False]

        def _deliver() -> None:
            if sent[0]:
                return
            sent[0] = True
            if pane.session is None or not pane.session.is_alive:
                return
            payload = _paste_payload(command)
            pane.session.write(payload)
            QTimer.singleShot(
                _enter_delay_ms(payload),
                lambda: pane.session and pane.session.write("\r"),
            )
            _log_event("auto_slash_command", role=role_name, command=command)

        def _check() -> None:
            if sent[0]:
                return
            if pane.session is None or not pane.session.is_alive:
                return
            if pane.session.is_at_ready_prompt():
                _deliver()
                return
            elapsed[0] += 500
            if elapsed[0] >= max_wait_ms:
                # Quiet timeout: skip rather than paste while still booting.
                return
            QTimer.singleShot(500, _check)

        QTimer.singleShot(1_500, _check)

    def _send_when_ready(
        self,
        role_name: str,
        task: str,
        max_wait_ms: int = 45_000,
        project: str | None = None,
    ) -> None:
        """Poll until claude's main prompt is idle, then paste task + Enter.

        Replaces the old fixed 12s wait so we don't paste into the trust modal
        or while claude is still bootstrapping. Falls back to a hard timeout
        so a hung claude doesn't silently swallow the task.
        """
        pane = self._project_panes(project).get(role_name)
        if pane is None:
            return
        elapsed = [0]
        sent = [False]

        def _deliver() -> None:
            if sent[0]:
                return
            sent[0] = True
            if pane.session is None or not pane.session.is_alive:
                return
            pane.set_state("working", note=task[:60])
            payload = _paste_payload(task)
            pane.session.write(payload)
            QTimer.singleShot(
                _enter_delay_ms(payload),
                lambda: pane.session and pane.session.write("\r"),
            )

        def _check() -> None:
            if sent[0]:
                return
            if pane.session is None or not pane.session.is_alive:
                return
            if pane.session.is_at_ready_prompt():
                _deliver()
                return
            elapsed[0] += 500
            if elapsed[0] >= max_wait_ms:
                # hard timeout — paste anyway so user sees the task land
                _deliver()
                return
            QTimer.singleShot(500, _check)

        QTimer.singleShot(1_000, _check)

    # ------------------------------------------------------------------
    # Peer CC durability helpers
    # ------------------------------------------------------------------

    def _pending_cc_path(self, project_ns: str) -> pathlib.Path:
        return RUNTIME_DIR / f"pending-lead-cc-{project_ns}.json"

    def _save_pending_cc(self, project_ns: str) -> None:
        """Persist current queue for project_ns so it survives orchestrator restart."""
        try:
            ensure_runtime()
            self._pending_cc_path(project_ns).write_text(
                json.dumps(self._pending_lead_cc.get(project_ns, []), ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _load_pending_cc(self) -> None:
        """Restore queued CC messages from disk on startup."""
        try:
            ensure_runtime()
            for p in RUNTIME_DIR.glob("pending-lead-cc-*.json"):
                proj = p.stem[len("pending-lead-cc-") :]
                try:
                    items = json.loads(p.read_text(encoding="utf-8"))
                    if items:
                        self._pending_lead_cc[proj] = items
                except Exception:
                    pass
        except Exception:
            pass

    def _flush_pending_lead_cc(self, project_ns: str) -> None:
        """Deliver queued CC messages to Lead if it is currently alive.

        Called after Lead spawns. If Lead is not ready yet, the queue is
        left intact so the next explicit flush attempt (or next send()) can
        try again. A separate QTimer in spawn() retries until Lead is alive.
        """
        pending = self._pending_lead_cc.get(project_ns)
        if not pending:
            return
        lead = self._project_panes(project_ns).get(LEAD.name)
        if not (lead and lead.session and lead.session.is_alive):
            return  # Lead still not alive — keep queue, retry later
        items = self._pending_lead_cc.pop(project_ns)
        self._save_pending_cc(project_ns)
        for item in items:
            payload = _paste_payload(item["body"])
            lead.session.write(payload)
            QTimer.singleShot(
                _enter_delay_ms(payload),
                lambda s=lead.session: s and s.write(b"\r"),
            )
        _log_event("send_cc_flushed", project=project_ns, count=len(items))

    def _inject_auto_chain_handoff(self, project_ns: str) -> None:
        """Send a pre-authorisation prompt to Lead telling it to fire
        verify (qa + reviewer) in parallel WITHOUT proposing or waiting
        for user confirmation. Fires after all panes assigned with
        --auto-chain in the project have reported done.

        If the Lead pane is absent, the prompt is queued via
        _pending_done_notices and delivered when Lead next spawns.
        """
        lead = self._project_panes(project_ns).get(LEAD.name)
        prompt = (
            "[auto-chain handoff] impl panes spawned with --auto-chain "
            "in this project have all reported done.\n"
            "You are pre-authorized to fire verify (qa + reviewer) "
            "in parallel WITHOUT proposing or waiting for user confirmation.\n"
            "\n"
            "Steps:\n"
            "1. Re-read the recent [<role> done] notes above\n"
            "2. (Optional) `git -C <project_path> diff --stat` for changed files\n"
            "3. Write a verify spec covering the implemented changes\n"
            "4. Fire in parallel: takkub assign --role qa ... & "
            "takkub assign --role reviewer ... & wait\n"
            "\n"
            "Do NOT add --auto-chain on the verify fire (verify is the "
            "terminal hop). After qa+reviewer done events arrive, resume "
            "normal propose-then-confirm flow."
        )
        if lead and lead.session and lead.session.is_alive:
            lead.session.write(prompt)
            QTimer.singleShot(150, lambda: lead.session and lead.session.write(b"\r"))
            self.leadInjected.emit(prompt)
            _log_event("auto_chain_handoff", project=project_ns)
        else:
            self._pending_done_notices.setdefault(project_ns, []).append(
                {"role": "system", "note": "auto-chain handoff", "body": prompt}
            )
            _log_event("auto_chain_handoff_queued", project=project_ns)

    def _flush_pending_done_notices(self, project_ns: str) -> None:
        """Deliver queued done notices to Lead if it is currently alive.

        Called after Lead spawns. If Lead is not ready yet the queue is left
        intact so the next flush attempt can retry. Pattern mirrors
        _flush_pending_lead_cc."""
        pending = self._pending_done_notices.get(project_ns)
        if not pending:
            return
        lead = self._project_panes(project_ns).get(LEAD.name)
        if not (lead and lead.session and lead.session.is_alive):
            return
        items = self._pending_done_notices.pop(project_ns)
        for item in items:
            payload = _paste_payload(item["body"])
            lead.session.write(payload)
            QTimer.singleShot(
                _enter_delay_ms(payload),
                lambda s=lead.session: s and s.write(b"\r"),
            )
        _log_event("done_notices_flushed", project=project_ns, count=len(items))

    def send(
        self,
        to_role: str,
        msg: str,
        from_role: str | None = None,
        project: str | None = None,
    ) -> tuple[bool, str]:
        try:
            to_role = validate_name(to_role, "role")
        except ValueError as exc:
            return False, str(exc)
        project_ns = self._resolve_project(project)
        project_panes = self._project_panes(project_ns)
        pane = project_panes.get(to_role)
        if pane is None:
            return False, f"unknown role: {to_role}"
        if pane.session is None or not pane.session.is_alive:
            return False, f"{to_role} is not running (spawn it first)"

        header = f"[{from_role} → {to_role}] " if from_role and from_role != to_role else ""
        body = header + msg
        body_payload = _paste_payload(body)
        pane.session.write(body_payload)
        QTimer.singleShot(
            _enter_delay_ms(body_payload),
            lambda: pane.session and pane.session.write(b"\r"),
        )

        # Record delivery time for stall detection: receiving a message counts
        # as evidence the pane is still being monitored by the orchestrator.
        self._last_send_ts[f"{project_ns}::{to_role}"] = time.time()

        # CC Lead unless source was Lead and target was a teammate, or vice versa.
        # If Lead is not alive, queue the CC so it isn't silently lost — the
        # queue is flushed when Lead next spawns (see _flush_pending_lead_cc).
        if from_role and from_role not in (None, LEAD.name) and to_role != LEAD.name:
            lead = project_panes.get(LEAD.name)
            if lead and lead.session and lead.session.is_alive:
                cc_payload = _paste_payload(f"[CC] {body}")
                lead.session.write(cc_payload)
                QTimer.singleShot(
                    _enter_delay_ms(cc_payload),
                    lambda: lead.session and lead.session.write(b"\r"),
                )
            else:
                ts = datetime.now().isoformat(timespec="seconds")
                self._pending_lead_cc.setdefault(project_ns, []).append(
                    {"from_role": from_role, "to_role": to_role, "body": f"[CC] {body}", "ts": ts}
                )
                self._save_pending_cc(project_ns)
                _log_event(
                    "send_cc_queued",
                    project=project_ns,
                    from_=from_role,
                    to=to_role,
                    msg_preview=body[:120],
                )

        # Track teammate ↔ Lead conversation so the idle watchdog doesn't
        # fire its `[auto-reminder]` while a teammate is legitimately
        # waiting for Lead to reply. Two cases:
        #   - teammate → Lead: mark sender as blocked-on-lead
        #   - Lead → teammate: clear teammate's blocked-on-lead flag
        from_norm = (from_role or "").lower().strip()
        if from_norm and from_norm != LEAD.name and to_role == LEAD.name:
            self._blocked_on_lead[f"{project_ns}::{from_norm}"] = time.time()
        elif from_norm == LEAD.name and to_role != LEAD.name:
            self._blocked_on_lead.pop(f"{project_ns}::{to_role}", None)

        _MAX_LOG_BODY = 4_096
        _log_event(
            "send",
            to=to_role,
            from_=from_role,
            body=msg[:_MAX_LOG_BODY] + ("…" if len(msg) > _MAX_LOG_BODY else ""),
        )
        return True, f"sent to {to_role}"

    def close(
        self,
        role_name: str,
        project: str | None = None,
        force: bool = False,
        reason: str = "",
    ) -> tuple[bool, str]:
        """Terminate a pane's session and remove it from the layout.

        force=True is for legitimate cockpit lifecycle (tab close, project switch).
        Never expose to CLI — teammates can only call `takkub done`.
        """
        role_name = role_name.lower().strip()
        project_ns = self._resolve_project(project)
        pane = self._project_panes(project_ns).get(role_name)
        if pane is None:
            return False, f"unknown role: {role_name}"
        was_alive = pane.session is not None
        if was_alive:
            # Lead is permanent; only force=True (tab close, project switch) may terminate
            if role_name == LEAD.name and not force:
                _log_event("close_ignored", role=role_name, reason="lead_protected")
                return True, "lead close ignored (protected)"
            # mark exit as expected so the pane doesn't surface "exited"/crash
            pane.mark_expected_exit()
            pane.session.terminate()
            pane.set_state("empty", note=None)
        key = f"{project_ns}::{role_name}"
        self._idle_state.pop(key, None)
        self._blocked_on_lead.pop(key, None)
        self._auto_respawn_attempts.pop(key, None)
        self._last_assigned_task.pop(key, None)
        self._requires_commit_on_done.pop(key, None)
        self._auto_chain_panes.pop(key, None)
        self._session_uuids.pop(key, None)
        self._last_send_ts.pop(key, None)
        # For teammates, fully remove from the layout so the right column
        # collapses back. Lead stays as it always anchors the cockpit.
        # The project namespace travels with the signal so main_window
        # can route the removal to the correct tab even when the user
        # is viewing a different project at the moment of close (the
        # `done`-triggered close fires 2.5 s after the agent reports
        # done, plenty of time for a tab switch).
        # paneClosed never fires for Lead — tab close handles UI teardown separately via deleteLater
        if role_name != LEAD.name:
            self.paneClosed.emit(role_name, project_ns)
        self.statusChanged.emit()
        _log_event("close", role=role_name, force=force, reason=reason)
        return True, f"{role_name} closed"

    def toggle_provider(self, provider: str, disabled: bool) -> tuple[bool, str]:
        """Flip codex or gemini between enabled/disabled globally across all tabs.

        Persists to ~/.takkub/disabled-providers.json then broadcasts a
        `[system] <provider> ENABLED/DISABLED ...` message into every Lead
        pane in every project so live sessions notice the change without
        having to poll the file.

        Returns (ok, message). Currently only fails on unknown provider.
        """
        from .provider_state import TOGGLABLE, set_disabled

        provider = provider.lower().strip()
        if provider not in TOGGLABLE:
            return False, f"unknown provider: {provider!r}"

        set_disabled(provider, disabled)

        word = "DISABLED" if disabled else "ENABLED"
        suffix = (
            "Do not propose this in routing or cross-check." if disabled else "Available again."
        )
        notice = f"[system] {provider} provider {word}. {suffix}"

        # Broadcast to every Lead pane across all project tabs. Iterate
        # _panes_by_project directly because we want every Lead, not just
        # the active project's Lead.
        for _project_ns, panes in self._panes_by_project.items():
            lead = panes.get(LEAD.name)
            if lead and lead.session and lead.session.is_alive:
                lead.session.write(notice)
                # Same trailing-CR delay as done() so the inject lands
                # after the inline text not before it.
                QTimer.singleShot(150, lambda pane=lead: pane.session and pane.session.write(b"\r"))
                self.leadInjected.emit(notice)
            # If Lead isn't alive in this project, the next spawn's
            # _render_lead_context() will read the fresh state — no need
            # to queue per-message for this case (unlike done notices,
            # which carry per-event info that mustn't be lost).

        self.providerStateChanged.emit(provider, disabled)
        _log_event("provider_toggled", provider=provider, disabled=disabled)
        return True, f"{provider} {word.lower()}"

    def done(self, from_role: str, note: str = "", project: str | None = None) -> tuple[bool, str]:
        try:
            from_role = validate_name(from_role, "role")
        except ValueError as exc:
            return False, str(exc)
        if from_role == LEAD.name:
            return False, "lead cannot call done on itself"
        project_ns = self._resolve_project(project)
        project_panes = self._project_panes(project_ns)
        pane = project_panes.get(from_role)
        if pane is None:
            return False, f"unknown role: {from_role}"

        key = f"{project_ns}::{from_role}"

        # Opt-in commit gate: if assign() was called with requires_commit=True,
        # reject done() when git working tree is not clean so the agent is
        # forced to commit before reporting done to Lead.
        if self._requires_commit_on_done.get(key, False):
            spawn_cwd = getattr(pane, "_session_cwd", None) or str(REPO_ROOT)
            try:
                git_result = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=spawn_cwd,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                dirty = git_result.stdout.strip()
            except Exception:
                dirty = ""  # can't check; allow done
            if dirty:
                files_preview = dirty[:200]
                _log_event(
                    "done_rejected",
                    role=from_role,
                    project=project_ns,
                    reason="dirty_tree",
                    files=files_preview,
                )
                if pane.session and pane.session.is_alive:
                    reject_msg = (
                        f"[orchestrator] done rejected — git working tree ไม่ clean. "
                        f"commit ก่อนเรียก takkub done อีกครั้ง. Files:\n{files_preview}"
                    )
                    payload = _paste_payload(reject_msg)
                    pane.session.write(payload)
                    QTimer.singleShot(
                        _enter_delay_ms(payload),
                        lambda: pane.session and pane.session.write(b"\r"),
                    )
                return False, "done rejected: working tree dirty"

        # Agent finished cleanly — clear any pending watchdog state so
        # the next session starts fresh (no leftover idle streak, no
        # leftover "blocked on lead" flag, no carried auto-respawn count).
        self._idle_state.pop(key, None)
        self._blocked_on_lead.pop(key, None)
        self._auto_respawn_attempts.pop(key, None)
        self._last_assigned_task.pop(key, None)
        self._requires_commit_on_done.pop(key, None)
        self._session_uuids.pop(key, None)

        # notify Lead in the same project (a teammate in unirecon mustn't
        # nudge the Lead in pms by mistake)
        lead = project_panes.get(LEAD.name)
        notice = f"[{from_role} done] {note}".rstrip()
        if lead and lead.session and lead.session.is_alive:
            lead.session.write(notice)
            QTimer.singleShot(150, lambda: lead.session and lead.session.write(b"\r"))
            self.leadInjected.emit(notice)
        else:
            # Lead is absent — queue notice so it isn't silently lost.
            # Flushed when Lead next spawns via _flush_pending_done_notices.
            self._pending_done_notices.setdefault(project_ns, []).append(
                {"role": from_role, "note": note, "body": notice}
            )
            _log_event("done_notice_queued", project=project_ns, role=from_role)

        # Fix A: when this done event belongs to a background tab, emit a
        # cross-tab signal so main_window can flash the status bar even if
        # the user is currently looking at a different project's tab.
        try:
            active_ns, _ = active_project()
        except Exception:
            active_ns = None
        if active_ns and project_ns != active_ns:
            self.crossTabDone.emit(project_ns, from_role, note)

        # Auto-chain handoff: if this pane was tagged --auto-chain at
        # assign time, and it was the LAST pending auto-chain pane in
        # the project, inject a pre-authorisation prompt so Lead fires
        # verify (qa+reviewer) without proposing/confirming.
        if self._auto_chain_panes.pop(key, False):
            pending = [k for k in self._auto_chain_panes if k.startswith(f"{project_ns}::")]
            if not pending:
                self._inject_auto_chain_handoff(project_ns)

        # mark pane done, auto-close after a delay so user can see it
        pane.set_state("done", note=note[:80] if note else "done")
        QTimer.singleShot(2_500, lambda: self.close(from_role, project=project_ns))
        _log_event("done", role=from_role, note=note[:200])
        now = datetime.now()
        transcript_path = getattr(pane, "_transcript_path", None)
        self._save_decision_note(
            project_ns, from_role, note, now=now, transcript_path=transcript_path
        )
        stamp = now.strftime("%Y-%m-%dT%H%M%S")
        self._recent_done.insert(0, (project_ns, from_role, f"{stamp}-{from_role}.md"))
        del self._recent_done[20:]
        # Refresh hot.md immediately so Obsidian shows the done event
        # without waiting up to a minute for the periodic tick.
        self._write_hot_md()
        self.agentDone.emit(from_role, note)
        return True, f"{from_role} reported done"

    @staticmethod
    def _save_decision_note(
        project: str,
        role: str,
        note: str,
        now: datetime | None = None,
        transcript_path: str | None = None,
    ) -> None:
        """Persist a teammate's `takkub done` note as a small markdown
        file under `runtime/sessions/<YYYY-MM-DD>/<project>/<role>-<HHMMSS>.md`,
        then mirror the same file into the Obsidian vault (if one is
        configured) at
        `<vault>/01-Projects/<project>/sessions/<YYYY-MM-DD>T<HHMMSS>-<role>.md`
        so the user can browse the decision trail from Obsidian's
        Dataview / graph view alongside the project's wiki page.

        events.log already captures the same data but is one long
        machine-readable stream. The per-role markdown gives the user a
        human-friendly paper trail that survives cockpit restarts and
        is trivial to grep / link to from a wiki later. Best-effort:
        any IO error is swallowed so a disk hiccup never breaks the
        done flow.

        `now` is injected by `done()` so the caller and this writer
        agree on the timestamp — otherwise the hot.md "Recent" entry
        and the on-disk filename could disagree by a second under load.
        """
        if not (note or "").strip():
            return
        # Junk filter: skip 1-word "ok" / "wip" / "done" stubs and
        # scratch/test workspaces. Keeps the Obsidian vault from
        # filling up with content-less session files that don't
        # connect to anything (no useful note body to backlink from).
        if _is_junk_note(note):
            return
        if _is_junk_project(project):
            return
        if now is None:
            now = datetime.now()
        body = _render_decision_note(project, role, note, now, transcript_path=transcript_path)
        try:
            day = RUNTIME_DIR / "sessions" / now.strftime("%Y-%m-%d") / project
            day.mkdir(parents=True, exist_ok=True)
            path = day / f"{role}-{now.strftime('%H%M%S')}.md"
            path.write_text(body, encoding="utf-8")
        except OSError:
            pass

        vault = _resolve_vault_dir()
        if vault is None:
            return
        try:
            sessions = vault / "01-Projects" / project / "sessions"
            sessions.mkdir(parents=True, exist_ok=True)
            stamp = now.strftime("%Y-%m-%dT%H%M%S")
            (sessions / f"{stamp}-{role}.md").write_text(body, encoding="utf-8")
        except OSError:
            pass

    def end_session(self, project: str | None = None, note: str = "") -> tuple[bool, str]:
        """Write a Lead session summary to runtime/sessions and the vault mirror.

        Called via `takkub end-session [--note '...']` from the Lead pane.
        Never closes any pane — Lead stays open, teammates continue as-is.
        """
        project_ns = self._resolve_project(project)
        if not note.strip():
            note = "session ended"
        now = datetime.now()
        day_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H%M%S")

        # Gather teammate done-event files from today's session dir.
        session_day = RUNTIME_DIR / "sessions" / day_str / project_ns
        done_files: list[str] = []
        if session_day.is_dir():
            for f in sorted(session_day.iterdir()):
                if f.name.startswith("lead-"):
                    continue
                if f.suffix == ".md":
                    done_files.append(f"runtime/sessions/{day_str}/{project_ns}/{f.name}")

        # Gather still-open teammate panes.
        active_teammates: list[tuple[str, str]] = [
            (name, pane.state)
            for name, pane in self._project_panes(project_ns).items()
            if name != LEAD.name
        ]

        # Build markdown body.
        iso = now.isoformat(timespec="seconds")
        body = (
            f"---\n"
            f"role: lead\n"
            f"project: {project_ns}\n"
            f"date: {iso}\n"
            f"tags: [session, lead, {project_ns}]\n"
            f"---\n\n"
            f"# lead session end · {iso}\n\n"
            f"**Project:** [[01-Projects/{project_ns}|{project_ns}]]\n"
            f"**Role:** lead\n\n"
            f"## Note\n\n{note.strip()}\n"
        )
        if done_files:
            body += "\n## Teammate done events\n\n"
            body += "\n".join(f"- {p}" for p in done_files) + "\n"
        else:
            body += "\n## Teammate done events\n\n_(none today)_\n"

        if active_teammates:
            body += "\n## Active teammates at end-session\n\n"
            body += "\n".join(f"- {role}: {state}" for role, state in active_teammates) + "\n"
        else:
            body += "\n## Active teammates at end-session\n\n_(none)_\n"

        # Write local file.
        rel_path = f"runtime/sessions/{day_str}/{project_ns}/lead-{time_str}.md"
        try:
            session_day.mkdir(parents=True, exist_ok=True)
            local_path = RUNTIME_DIR / "sessions" / day_str / project_ns / f"lead-{time_str}.md"
            local_path.write_text(body, encoding="utf-8")
        except OSError as exc:
            return False, f"failed to write session file: {exc}"

        # Mirror to vault (best-effort, never fails the call).
        vault = _resolve_vault_dir()
        if vault is not None:
            try:
                vault_sessions = vault / "01-Projects" / project_ns / "sessions"
                vault_sessions.mkdir(parents=True, exist_ok=True)
                stamp = now.strftime("%Y-%m-%dT%H%M%S")
                (vault_sessions / f"{stamp}-lead.md").write_text(body, encoding="utf-8")
            except OSError:
                pass

        _log_event("end_session", project=project_ns, note=note[:200])
        return True, f"lead session summary written: {rel_path}"

    def list_status(self, project: str | None = None) -> dict[str, str]:
        """Snapshot of `role → state` for one project's panes.

        Defaults to the active project's view, so a Lead in unirecon never
        accidentally sees a backend pane that belongs to pms.
        """
        return {name: p.state for name, p in self._project_panes(project).items()}

    def _compute_last_progress_ts(self, role: str, project_ns: str, pane: AgentPane) -> float:
        """Return the most-recent activity timestamp for `pane` (0.0 = no baseline).

        Checks three signals and returns the largest (= most recent):
          1. Transcript file mtime — new PTY bytes written
          2. Today's screenshot directory mtime — QA captured a new shot
          3. Last `takkub send` delivery timestamp — orchestrator pushed a message
        """
        ts = 0.0

        transcript_path = getattr(pane, "_transcript_path", None)
        if transcript_path:
            try:
                mt = pathlib.Path(transcript_path).stat().st_mtime
                if mt > ts:
                    ts = mt
            except OSError:
                pass

        if role in ("qa", "critic", "designer"):
            today = datetime.now().strftime("%Y-%m-%d")
            shot_dir = RUNTIME_DIR / "exports" / today / project_ns / "screenshots"
            try:
                mt = shot_dir.stat().st_mtime
                if mt > ts:
                    ts = mt
            except OSError:
                pass

        send_ts = self._last_send_ts.get(f"{project_ns}::{role}", 0.0)
        if send_ts > ts:
            ts = send_ts

        return ts

    def list_status_detailed(self, project: str | None = None) -> dict[str, dict]:
        """Extended status snapshot with stall detection.

        Returns `{role: {"state": str, "stall_minutes": int|None, "last_progress_ts": float}}`.
        `stall_minutes` is set when the pane is `working` and no progress signal
        has been seen for more than STALL_THRESHOLD_SEC.
        """
        now = time.time()
        project_ns = self._resolve_project(project)
        result: dict[str, dict] = {}
        for role, pane in self._project_panes(project_ns).items():
            state = pane.state
            stall_minutes: int | None = None
            last_progress_ts = 0.0
            if state == "working" and pane.session is not None and pane.session.is_alive:
                last_progress_ts = self._compute_last_progress_ts(role, project_ns, pane)
                if last_progress_ts > 0:
                    silent_for = now - last_progress_ts
                    if silent_for >= STALL_THRESHOLD_SEC:
                        stall_minutes = int(silent_for // 60)
            result[role] = {
                "state": state,
                "stall_minutes": stall_minutes,
                "last_progress_ts": last_progress_ts,
            }
        return result

    def pane_status_report(
        self,
        project: str | None = None,
        since_ts: float | None = None,
    ) -> dict:
        """Per-pane summary for `takkub status`.

        Returns `{"panes": {role: {...}}, "any_stalled": bool, "project": str}`.
        Each pane entry includes state, stall info, last-progress timestamps,
        transcript tail, newest screenshot path, and done events in the window.
        `since_ts` defaults to one hour ago when omitted.
        """
        now = time.time()
        if since_ts is None:
            since_ts = now - 3600
        project_ns = self._resolve_project(project)
        detailed = self.list_status_detailed(project=project_ns)
        panes_out: dict[str, dict] = {}

        for role, info in detailed.items():
            state = info["state"]
            last_ts = info["last_progress_ts"]
            stall_min = info["stall_minutes"]

            if last_ts > 0:
                age_sec = now - last_ts
                if age_sec < 60:
                    human_ts = f"{int(age_sec)}s ago"
                elif age_sec < 3600:
                    human_ts = f"{int(age_sec // 60)}m ago"
                else:
                    human_ts = f"{int(age_sec // 3600)}h ago"
                abs_ts = datetime.fromtimestamp(last_ts).strftime("%H:%M:%S")
            else:
                human_ts = "unknown"
                abs_ts = "unknown"

            pane = self._project_panes(project_ns).get(role)
            transcript_tail = ""
            if pane is not None:
                transcript_path = getattr(pane, "_transcript_path", None)
                if transcript_path:
                    try:
                        raw = pathlib.Path(transcript_path).read_bytes()
                        lines = raw.decode("utf-8", errors="replace").splitlines()
                        tail_lines = [ln for ln in lines if ln.strip()][-5:]
                        tail_lines = [_ANSI.sub("", ln) for ln in tail_lines]
                        transcript_tail = "\n".join(tail_lines)
                    except OSError:
                        pass

            last_screenshot = ""
            if role in ("qa", "critic", "designer"):
                today = datetime.now().strftime("%Y-%m-%d")
                shot_dir = RUNTIME_DIR / "exports" / today / project_ns / "screenshots"
                try:
                    shots = sorted(
                        shot_dir.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True
                    )
                    if shots:
                        last_screenshot = str(shots[0])
                except OSError:
                    pass

            done_events: list[str] = []
            sessions_root = RUNTIME_DIR / "sessions"
            if sessions_root.is_dir():
                for day_dir in sorted(sessions_root.iterdir(), reverse=True):
                    if not day_dir.is_dir():
                        continue
                    proj_dir = day_dir / project_ns
                    if not proj_dir.is_dir():
                        continue
                    for f in sorted(proj_dir.iterdir()):
                        if f.suffix != ".md" or f.name.startswith("lead-"):
                            continue
                        if not f.name.startswith(f"{role}-"):
                            continue
                        try:
                            if f.stat().st_mtime >= since_ts:
                                done_events.append(f.name)
                        except OSError:
                            pass

            panes_out[role] = {
                "state": state,
                "stall_minutes": stall_min,
                "last_progress_ts": last_ts,
                "last_progress_human": human_ts,
                "last_progress_abs": abs_ts,
                "transcript_tail": transcript_tail,
                "last_screenshot": last_screenshot,
                "done_events": done_events,
            }

        any_stalled = any(info["stall_minutes"] is not None for info in panes_out.values())
        return {"panes": panes_out, "any_stalled": any_stalled, "project": project_ns}

    def harvest_info(
        self,
        role: str,
        project: str | None = None,
        since_ts: float | None = None,
        limit: int = 100,
    ) -> tuple[bool, str, dict]:
        """Return pane state + artifact list for `takkub harvest`.

        Returns (ok, msg, payload). When ok=False and the role is not running,
        msg contains 'role not running: <role>' so the CLI can set exit_code 2.
        payload keys: state, spawn_ts, since_ts, artifacts.
        """
        from .config import load_projects as _load_projects

        project_ns = self._resolve_project(project)
        pane = self._project_panes(project_ns).get(role)
        if pane is None:
            return False, f"role not running: {role}", {}

        spawn_ts_raw: float = getattr(pane, "_spawn_ts", 0.0) or 0.0
        if since_ts is None:
            since_ts = spawn_ts_raw if spawn_ts_raw > 0 else (time.time() - 3600)

        # Build scan paths: configured project paths + runtime/exports/<date>/<project>/
        try:
            data = _load_projects()
            paths_cfg: dict = data.get("projects", {}).get(project_ns, {}).get("paths", {})
        except Exception:
            paths_cfg = {}

        today = datetime.now().strftime("%Y-%m-%d")
        scan_bases: list[pathlib.Path] = [
            RUNTIME_DIR / "exports" / today / project_ns,
        ]
        for v in paths_cfg.values():
            scan_bases.append(pathlib.Path(str(v)))

        artifacts = scan_artifacts(scan_bases, since_ts, limit=limit)

        return (
            True,
            "ok",
            {
                "state": pane.state,
                "spawn_ts": spawn_ts_raw,
                "since_ts": since_ts,
                "artifacts": artifacts,
            },
        )

    def _build_post_compact_brief(self, project_ns: str) -> str | None:
        """Return a markdown snippet summarising alive teammates for post-compact injection.

        Fires when `_LAST_SESSION_FILE` was written within _POST_COMPACT_DETECT_SEC
        and live teammates exist — indicating a cockpit restart after session compact.
        Returns None when no snapshot is fresh enough or no teammates are running.
        """
        if not _LAST_SESSION_FILE.is_file():
            return None
        try:
            age = time.time() - _LAST_SESSION_FILE.stat().st_mtime
        except OSError:
            return None
        if age > _POST_COMPACT_DETECT_SEC:
            return None

        project_panes = self._project_panes(project_ns)
        alive_teammates = [
            (role, pane)
            for role, pane in project_panes.items()
            if role != LEAD.name and pane.session is not None and pane.session.is_alive
        ]
        if not alive_teammates:
            return None

        now = time.time()
        lines: list[str] = [
            "",
            "---",
            "",
            "## 🔄 Post-compact status (auto-injected)",
            "",
            "cockpit เพิ่ง restart จาก session snapshot — pane ที่ยังทำงานอยู่:",
            "",
        ]
        for role, pane in alive_teammates:
            state = pane.state
            last_ts = self._compute_last_progress_ts(role, project_ns, pane)
            if last_ts > 0:
                age_s = now - last_ts
                if age_s < 60:
                    age_str = f"{int(age_s)}s ago"
                elif age_s < 3600:
                    age_str = f"{int(age_s // 60)}m ago"
                else:
                    age_str = f"{int(age_s // 3600)}h ago"
                ts_abs = datetime.fromtimestamp(last_ts).strftime("%H:%M:%S")
            else:
                age_str = "unknown"
                ts_abs = "unknown"

            lines.append(f"### {role} ({state}) — last progress: {age_str} ({ts_abs})")
            lines.append("")

            transcript_path = getattr(pane, "_transcript_path", None)
            if transcript_path:
                try:
                    raw = pathlib.Path(transcript_path).read_bytes()
                    raw_lines = raw.decode("utf-8", errors="replace").splitlines()
                    tail = [ln for ln in raw_lines if ln.strip()][-5:]
                    if tail:
                        lines.append("```")
                        lines.extend(tail)
                        lines.append("```")
                except OSError:
                    pass
            lines.append("")

        brief = "\n".join(lines)
        if len(brief) > 2000:
            brief = brief[:2000] + "\n…(truncated)\n"
        return brief

    # ──────────────────────────────────────────────────────────────
    # `<vault>/hot.md` — periodic snapshot of cockpit live state
    # ──────────────────────────────────────────────────────────────
    # ──────────────────────────────────────────────────────────────
    # session snapshot — restore teammate panes across cockpit restarts
    # ──────────────────────────────────────────────────────────────
    def snapshot_state(self) -> dict:
        """Return a JSON-serialisable picture of every live teammate pane
        across every project. Lead panes are excluded because the tab
        restore in main_window (driven by `open_tabs` in projects.json)
        already brings Lead back. We only capture panes that are actively
        running and in a state worth resuming (active/working) — empty,
        exited, or error panes are intentionally skipped so a crashed
        run doesn't get re-spawned into the same crash.
        """
        projects: dict[str, list[dict]] = {}
        for project, panes in self._panes_by_project.items():
            entries: list[dict] = []
            for role, pane in panes.items():
                if role == LEAD.name:
                    continue
                if pane.session is None or not pane.session.is_alive:
                    continue
                if pane.state not in ("active", "working"):
                    continue
                entries.append(
                    {
                        "role": role,
                        "cwd": pane._session_cwd or "",
                        "state": pane.state,
                    }
                )
            if entries:
                projects[project] = entries
        return {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "projects": projects,
        }

    def write_session_snapshot(self) -> None:
        """Persist the current snapshot to disk. Best-effort: any error
        is swallowed so a disk hiccup never bubbles out of closeEvent or
        the periodic save timer."""
        try:
            ensure_runtime()
            _LAST_SESSION_FILE.write_text(
                json.dumps(self.snapshot_state(), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    def restore_teammates(self) -> int:
        """Read the snapshot and re-spawn the recorded teammate panes.
        Returns the number of panes scheduled to spawn (caller can show
        a status-bar hint). Skips silently when the snapshot is missing,
        unparseable, or older than `_LAST_SESSION_MAX_AGE_SEC`.

        The `_recent_exits` stamp is kept for crash-recovery bookkeeping,
        but since `_session_uuids` has no entry for these roles yet, each
        spawn here generates a fresh `--session-id` (no bleed from a prior
        cockpit run's sessions).
        """
        if not _LAST_SESSION_FILE.is_file():
            return 0
        try:
            snap = json.loads(_LAST_SESSION_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        saved_at = snap.get("saved_at") or ""
        try:
            age = (datetime.now() - datetime.fromisoformat(saved_at)).total_seconds()
        except ValueError:
            return 0
        if age > _LAST_SESSION_MAX_AGE_SEC:
            return 0
        scheduled = 0
        for project, entries in (snap.get("projects") or {}).items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                role = (entry or {}).get("role")
                cwd = (entry or {}).get("cwd") or None
                if not role:
                    continue
                # Stamp recent-exit for crash-recovery bookkeeping.
                # _session_uuids has no UUID for these roles yet, so
                # spawn() will issue --session-id (fresh session, no bleed).
                self._recent_exits[_exit_key(project, role)] = {"cwd": cwd, "ts": time.time()}
                ok, _ = self.spawn(role, cwd=cwd, project=project)
                if ok:
                    scheduled += 1
        return scheduled

    def write_resume_briefs(self) -> int:
        """For every project currently open in cockpit, write a
        Markdown "resume brief" capturing the last ~20 conversation
        exchanges to `<vault>/07-AI-Command-Center/briefs/<project>-
        <YYYY-MM-DD>T<HHMMSS>.md`. Called from MainWindow.closeEvent
        so the next launch's Lead can read the brief and recover
        context without scrolling the pane history.

        Returns the number of briefs written. 0 when no vault is
        configured or no open project had conversation records to
        summarise.
        """
        vault = _resolve_vault_dir()
        if vault is None:
            return 0
        try:
            from .chatlog_scanner import build_resume_brief
        except Exception:
            return 0
        now = datetime.now()
        stamp = now.strftime("%Y-%m-%dT%H%M%S")
        briefs_dir = vault / "07-AI-Command-Center" / "briefs"
        # Cap the scan window so a long-dormant project doesn't drag
        # months of jsonls into the brief — last 24 h is plenty for
        # "where did we leave off."
        from datetime import timedelta

        since = now - timedelta(hours=24)
        written = 0
        for project in self._panes_by_project.keys():
            body = build_resume_brief(project_filter=project, since=since)
            if not body:
                continue
            try:
                briefs_dir.mkdir(parents=True, exist_ok=True)
                (briefs_dir / f"{project}-{stamp}.md").write_text(body, encoding="utf-8")
                written += 1
            except OSError:
                continue
        return written

    def write_daily_digest(self, project: str) -> bool:
        """Append a Finish-Job digest for `project` to today's daily
        note in the configured Obsidian vault.

        Daily note path is `<vault>/05-Daily/<YYYY-MM-DD>.md`. If the
        file already exists (another project's Finish Job earlier the
        same day, or hand-written entries), the digest is appended at
        the end. Otherwise a fresh file is created with a top-level
        title.

        Returns True on success, False when no vault is configured or
        an IO error swallows the write. Caller can surface a status
        bar message based on the return value.
        """
        vault = _resolve_vault_dir()
        if vault is None:
            return False
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        sessions_dir = RUNTIME_DIR / "sessions" / today / project
        sessions: list[tuple[str, str, str]] = []
        if sessions_dir.is_dir():
            for path in sorted(sessions_dir.glob("*.md"), reverse=True):
                stem = path.stem  # "<role>-<HHMMSS>"
                if "-" not in stem:
                    continue
                role, stamp = stem.rsplit("-", 1)
                try:
                    body = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                # `_render_decision_note` writes "## Note\n\n<text>" — pull
                # the first non-empty line after the header.
                note = ""
                marker = "## Note"
                idx = body.find(marker)
                if idx >= 0:
                    tail = body[idx + len(marker) :].strip()
                    note = tail.splitlines()[0] if tail else ""
                sessions.append((stamp, role, note))
        # Decisions today — assistant H2-headed messages from this
        # project's claude session jsonls. Best-effort: any scan
        # error degrades to no decisions section.
        try:
            from .chatlog_scanner import extract_decisions

            start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
            decisions = extract_decisions(project_filter=project, since=start_of_today, limit=10)
        except Exception:
            decisions = []
        section = _render_daily_digest(project, now, sessions, decisions=decisions)

        daily_dir = vault / "05-Daily"
        try:
            daily_dir.mkdir(parents=True, exist_ok=True)
            daily_path = daily_dir / f"{today}.md"
            if daily_path.is_file():
                existing = daily_path.read_text(encoding="utf-8")
                if not existing.endswith("\n"):
                    existing += "\n"
                daily_path.write_text(existing + "\n" + section, encoding="utf-8")
            else:
                header = f"# Daily — {today}\n\n"
                daily_path.write_text(header + section, encoding="utf-8")
        except OSError:
            return False
        return True

    def _write_hot_md(self) -> None:
        """Rewrite `<vault>/hot.md` from the current pane registry plus
        the in-memory ring of recent `takkub done` events. Skipped
        silently when no vault is configured. Best-effort: swallow
        OSError so a vault permission glitch never bubbles out of a
        QTimer tick and kills the orchestrator."""
        vault = _resolve_vault_dir()
        if vault is None:
            return
        snapshot = {
            project: {role: pane.state for role, pane in panes.items()}
            for project, panes in self._panes_by_project.items()
        }
        try:
            active_name, _ = active_project()
        except Exception:
            active_name = None
        # Hook noise meter + friction heatmap — scan today's Claude
        # Code session jsonl files for system reminders and user-
        # correction signals. Quiet day → empty → renderer omits.
        try:
            from .chatlog_scanner import (
                count_hook_fires,
                count_tool_retries,
                count_user_corrections,
            )

            start_of_today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            hook_counts = count_hook_fires(since=start_of_today)
            friction = {
                "corrections": count_user_corrections(since=start_of_today),
                "tool_retries": count_tool_retries(since=start_of_today),
            }
        except Exception:
            hook_counts = {}
            friction = {}
        body = _render_hot_md(
            snapshot,
            active_name,
            list(self._recent_done),
            datetime.now(),
            hook_counts=hook_counts,
            friction=friction,
        )
        try:
            (vault / "hot.md").write_text(body, encoding="utf-8")
        except OSError:
            pass

    # ──────────────────────────────────────────────────────────────
    # idle watchdog — nudge teammates that forgot to `takkub done`
    # ──────────────────────────────────────────────────────────────
    def _check_idle_teammates(self) -> None:
        """Inject a `takkub done` reminder into any teammate pane that's been
        at the ready prompt for IDLE_REMIND_AFTER_S while still flagged
        'working'. Lead is exempt — only Lead is allowed to orchestrate, and
        Lead never calls `done` on itself.

        Scans every open project so a teammate in a background tab still
        gets nudged. Idle-state keys are namespaced `<project>::<role>`
        to keep two projects' state from colliding."""
        now = time.time()
        # Stuck-pane detection rides the same 5 s tick so we don't pay
        # for another QTimer. Runs before the idle-reminder logic so a
        # recover (which closes the pane) doesn't fight with reminder
        # injection on the same pane.
        self._check_stuck_panes(now)
        for project_name, project_panes in list(self._panes_by_project.items()):
            for name, pane in list(project_panes.items()):
                key = f"{project_name}::{name}"
                if name == LEAD.name:
                    continue
                if pane.state != "working":
                    self._idle_state.pop(key, None)
                    continue
                if pane.session is None or not pane.session.is_alive:
                    self._idle_state.pop(key, None)
                    continue

                # Suppress the reminder while this teammate is waiting on a
                # reply from Lead — they're not "stuck on `takkub done`",
                # they're genuinely blocked on clarification. The flag is
                # set in `send()` when the teammate runs
                # `takkub send --to lead "..."` and cleared when Lead
                # sends back. We also expire the suppression after 30
                # minutes so a Lead that crashed mid-reply doesn't leave
                # the teammate's watchdog disabled forever.
                blocked_at = self._blocked_on_lead.get(key)
                if blocked_at is not None and (now - blocked_at) < 30 * 60:
                    entry = self._idle_state.get(key)
                    if entry:
                        entry["first_idle_ts"] = None
                    continue

                entry = self._idle_state.setdefault(
                    key, {"first_idle_ts": None, "last_reminder_ts": 0.0}
                )

                if not pane.session.is_at_ready_prompt():
                    # claude is processing — reset the idle streak so a long
                    # build doesn't count toward the reminder threshold.
                    entry["first_idle_ts"] = None
                    continue

                if entry["first_idle_ts"] is None:
                    entry["first_idle_ts"] = now
                    continue

                idle_for = now - entry["first_idle_ts"]
                since_last_reminder = now - entry["last_reminder_ts"]
                if (
                    idle_for >= IDLE_REMIND_AFTER_S
                    and since_last_reminder >= IDLE_REMIND_COOLDOWN_S
                ):
                    self._inject_idle_reminder(name, pane)
                    entry["last_reminder_ts"] = now
                    # restart the idle streak so we don't fire again until
                    # the agent stays idle for another full
                    # IDLE_REMIND_AFTER_S past the cooldown.
                    entry["first_idle_ts"] = now

                # Harvest hint: if the pane has been idle much longer than
                # the reminder threshold, suggest `takkub harvest` to Lead.
                if HARVEST_HINT_SEC > 0 and idle_for >= HARVEST_HINT_SEC:
                    hint_key = f"{project_name}::{name}"
                    last_hint = self._harvest_hint_ts.get(hint_key, 0.0)
                    if now - last_hint >= HARVEST_HINT_SEC:
                        lead_pane = project_panes.get(LEAD.name)
                        if lead_pane and lead_pane.session and lead_pane.session.is_alive:
                            hint_min = HARVEST_HINT_SEC // 60
                            hint_msg = (
                                f"[cockpit] {name} ไม่ active >{hint_min}m. "
                                f"ลอง: takkub harvest --role {name}"
                            )
                            lead_pane.session.write(hint_msg)
                            QTimer.singleShot(
                                150,
                                lambda lp=lead_pane: lp.session and lp.session.write(b"\r"),
                            )
                            _log_event("harvest_hint", role=name, project=project_name)
                            self._harvest_hint_ts[hint_key] = now

    def _check_stuck_panes(self, now: float) -> None:
        """Walk every teammate pane and auto-recover any that's been
        sitting in `working` state with no PTY output for longer than
        STUCK_THRESHOLD_S. A recovered pane runs close→spawn and gets
        --resume <uuid> via the session-uuid + recent-exits machinery, so
        claude rejoins the conversation rather than restarting blank.

        Lead is exempt: Lead's "stuck" usually means waiting on user
        input, not a hang, and a forced restart would lose Lead's
        conversation with the operator. Teammates are the safe target."""
        for project_name, project_panes in list(self._panes_by_project.items()):
            for role, pane in list(project_panes.items()):
                if role == LEAD.name:
                    continue
                if pane.state != "working":
                    continue
                if pane.session is None or not pane.session.is_alive:
                    continue
                last_out = getattr(pane, "_last_output_ts", 0.0)
                if not isinstance(last_out, (int, float)) or last_out <= 0:
                    # Pane hasn't seen output yet — still in bootstrap,
                    # or the attribute was never initialised (legacy
                    # AgentPane subclass / test fixture). Skip; the next
                    # tick will pick it up once a real timestamp lands.
                    continue
                if (now - last_out) < STUCK_THRESHOLD_S:
                    continue
                key = f"{project_name}::{role}"
                last_recover = self._last_stuck_recover.get(key, 0.0)
                if (now - last_recover) < STUCK_RECOVER_COOLDOWN_S:
                    # Already tried to recover this pane recently;
                    # leave it alone so we don't loop close→spawn.
                    continue
                self._auto_recover_stuck(role, project_name, pane, now)

    def _auto_recover_stuck(self, role: str, project: str, pane: AgentPane, now: float) -> None:
        """Close the wedged pane and respawn it with --resume <uuid>. The
        spawn uses the pane's last-known cwd so claude rejoins the same
        project directory."""
        cwd = pane._session_cwd
        key = f"{project}::{role}"
        self._last_stuck_recover[key] = now
        silent_for_s = int(now - getattr(pane, "_last_output_ts", now))
        # Reset the output timestamp so the next tick doesn't re-trigger
        # before claude has had a chance to print anything from the new
        # session.
        pane._last_output_ts = now
        _log_event(
            "stuck_pane_recover",
            role=role,
            project=project,
            cwd=cwd or "",
            silent_for_s=silent_for_s,
        )
        self.close(role, project=project)
        # 2 s pause so the close has time to terminate the PTY and tear
        # down the WebEngine view before the respawn binds a new one
        # to the same role slot.
        QTimer.singleShot(2_000, lambda: self.spawn(role, cwd=cwd, project=project))

    def _inject_idle_reminder(self, role_name: str, pane: AgentPane) -> None:
        if pane.session is None or not pane.session.is_alive:
            return
        pane.session.write(IDLE_REMINDER_TEXT)
        QTimer.singleShot(150, lambda: pane.session and pane.session.write(b"\r"))
        _log_event("idle_reminder", role=role_name)

    def broadcast_bug_check(self, project: str | None = None) -> tuple[int, list[str]]:
        """Ask every active pane in `project` to introspect for cockpit bugs.

        Each live pane gets a prompt instructing the agent to either:
          * `takkub issue new ...` if it noticed a cockpit/orchestrator/CLI/UI bug
          * `takkub send --to lead 'no bugs to report'` if the session was clean

        Empty / dead-session slots are skipped silently. Cross-project panes
        are not touched (multi-tab isolation).

        Returns (count, role_names) for the cockpit's status-bar feedback.
        """
        project_ns = self._resolve_project(project)
        prompted: list[str] = []
        for role_name, pane in list(self._project_panes(project_ns).items()):
            if pane.session is None or not pane.session.is_alive:
                continue
            prompt = self._build_bug_check_prompt(role_name, project_ns)
            self._send_when_ready(role_name, prompt, project=project_ns)
            prompted.append(role_name)
        _log_event("broadcast_bug_check", project=project_ns, count=len(prompted), roles=prompted)
        return len(prompted), prompted

    @staticmethod
    def _build_bug_check_prompt(role: str, project: str) -> str:
        """Render the per-pane bug-introspection prompt.

        Static-method so the test suite can call it without a full
        Orchestrator + Qt event loop just to inspect the wording.
        """
        return (
            "🐛 **Bug check** (orchestrator broadcast)\n\n"
            "introspect session ของเรา — เจอบัค **ของ cockpit / orchestrator / CLI / UI** ไหม\n"
            "(ไม่ใช่บัคของ code ที่เรากำลังทำงาน — เฉพาะบัคของ cockpit เอง)\n\n"
            "**ถ้าเจอ:** เรียก\n"
            "```\n"
            f'takkub issue new "<title>" --severity <low|med|high> --noticed-in {project} --role {role} --tag <a,b,c> --body "<reproduce + impact>"\n'
            "```\n\n"
            "**ถ้าไม่เจอ:** เรียก\n"
            "```\n"
            'takkub send --to lead "no bugs to report"\n'
            "```\n\n"
            "รายงานกลับเมื่อเสร็จ"
        )

    def broadcast_design_review(self, project: str | None = None) -> tuple[int, list[str]]:
        """Spawn the design-review pipeline for `project` — critic + gemini parallel.

        Unlike `broadcast_bug_check` (prompts existing live panes), this
        method assigns fresh tasks to the design-review duo:
          * critic — read shots from runtime/exports/<date>/<project>/screenshots/
            and write a proposal to docs/design-review/<date>-<view>.md
          * gemini — prepare to view images critic will send via `takkub send`

        Respects the disabled-providers toggle: when gemini is off, critic
        still fires alone (degraded mode) so user can iterate solo.

        Returns (count, role_names) for status-bar feedback. Roles ordered
        consistently (critic first) so the UI message reads naturally.
        """
        from datetime import datetime as _dt

        from .provider_state import all_disabled

        project_ns = self._resolve_project(project)
        today = _dt.now().strftime("%Y-%m-%d")
        shot_dir = f"runtime/exports/{today}/{project_ns}/screenshots/"
        proposal_path = f"docs/design-review/{today}-<view>.md"
        cwd = default_cwd_for_role("critic", project=project_ns)

        critic_task = (
            "[ROLE: Design Critic — ทำงานเองโดยตรง ห้าม spawn subagent]\n\n"
            "🎨 **Design review** (orchestrator broadcast)\n\n"
            f"อ่าน screenshots ที่ `{shot_dir}` (ถ้าโฟลเดอร์ยังว่าง บอก Lead ผ่าน "
            "`takkub send --to lead` ขอให้ QA capture ก่อน) — เสนอ:\n"
            "  • **เพิ่ม** — element/affordance ที่ขาด\n"
            "  • **ลบ** — clutter หรือ widget ซ้ำซ้อน\n"
            "  • **ปรับ** — spacing / typography / color / interaction\n\n"
            "สื่อสารกับ gemini pane ผ่าน `takkub send --to gemini` เพื่อขอมุมที่ 2 "
            "จากภาพเดียวกัน (cross-check confirmation bias)\n\n"
            f"เขียน proposal markdown ไปที่ `{proposal_path}` พร้อม frontmatter "
            "(date / scope / shots) แล้ว report กลับผ่าน `takkub done`"
        )

        gemini_task = (
            "[ROLE: gemini — second opinion on visual design]\n\n"
            "🖼️ **Image review co-pilot**\n\n"
            "Design Critic pane จะส่ง path ของ screenshot images ให้ผ่าน "
            "`takkub send` — โหลดภาพอ่านดู แล้วตอบกลับ 1-3 จุดที่:\n"
            "  • รู้สึกขาด / clutter / ไม่ balance\n"
            "  • เป็นไปได้ที่ user จะใช้ผิด\n"
            "  • Heuristic ผิด (Nielsen / contrast / hierarchy)\n\n"
            "ตอบสั้น focus — critic จะรวบรวมเขียน proposal เอง "
            "report กลับผ่าน `takkub done` เมื่อ critic บอกว่าจบรอบ"
        )

        disabled = all_disabled()
        spawned: list[str] = []
        ok_critic, _ = self.assign("critic", cwd=cwd, task=critic_task, project=project_ns)
        if ok_critic:
            spawned.append("critic")
        if "gemini" not in disabled:
            ok_gemini, _ = self.assign("gemini", cwd=cwd, task=gemini_task, project=project_ns)
            if ok_gemini:
                spawned.append("gemini")
        _log_event(
            "broadcast_design_review",
            project=project_ns,
            count=len(spawned),
            roles=spawned,
            shot_dir=shot_dir,
        )
        return len(spawned), spawned

    def close_all_teammates(self, project: str | None = None) -> tuple[bool, str]:
        """Close every non-Lead pane in `project` (defaults to active).
        Used by Lead to reset the board and by the cockpit when a tab is
        closed."""
        project_ns = self._resolve_project(project)
        names = [n for n in list(self._project_panes(project_ns).keys()) if n != LEAD.name]
        if not names:
            return True, "no teammates to close"
        for n in names:
            self.close(n, project=project_ns)
        return True, f"closed {len(names)} teammate(s): {', '.join(names)}"

    # ──────────────────────────────────────────────────────────────
    # internal: handlers wired from AgentPane signals
    # ──────────────────────────────────────────────────────────────
    def _on_pane_spawn_clicked(self, role_name: str) -> None:
        self.spawn(role_name)

    def _on_pane_close_clicked(self, role_name: str) -> None:
        self.close(role_name)

    def _on_pane_input(self, role_name: str, data: bytes) -> None:
        pane = self.panes.get(role_name)
        if pane is None or pane.session is None:
            return
        pane.session.write(data)
