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
import time
from datetime import datetime

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from .agent_pane import AgentPane
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
    load_projects,
)
from .pty_session import PtySession
from .roles import LEAD


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


RESUME_WINDOW_SEC = 5 * 60  # respawn within this window → claude --continue

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
# auto-recovers via close + respawn (which picks `--continue` because
# the recent-exit timestamp is fresh). 10 minutes is generous enough
# that a heavy `npm install` or a slow Lighthouse audit won't trip it.
STUCK_THRESHOLD_S = 10 * 60
# Once a recover fires for a pane, wait this long before another one
# is allowed — otherwise a chronically-stuck workload restarts on a
# loop. Three strikes is the soft cap (auto-respawn-attempts already
# handles the hard cap separately).
STUCK_RECOVER_COOLDOWN_S = 5 * 60
IDLE_WATCHDOG_INTERVAL_MS = 5_000
IDLE_REMINDER_TEXT = (
    "🔔 [auto-reminder] task เสร็จแล้วใช่มั้ย? ถ้าใช่ run `takkub done [note]` "
    "รายงาน Lead เลย ถ้ายังทำต่อ ignore ข้อความนี้"
)

# Auto-respawn on unexpected pane crash. The orchestrator notices when a
# pane exits without a corresponding takkub close/done (claude crashed,
# OOM, parent killed it) and gives it a clean respawn with --continue so
# the conversation survives. AUTO_RESPAWN_MAX caps consecutive attempts
# per pane so a deterministically-crashing claude doesn't spawn-loop.
AUTO_RESPAWN_DELAY_MS = 2_500
AUTO_RESPAWN_MAX = 2

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


# ECC plugin hooks we mute in every pane. See cockpit CLAUDE.md
# "ECC plugin noise — auto-muted ใน pane env" for the rationale.
_ECC_MUTED_HOOKS: tuple[str, ...] = (
    "pre:edit-write:gateguard-fact-force",
    "post:ecc-context-monitor",
)


def _apply_ecc_mute(env: dict[str, str]) -> None:
    """Mutate `env` in place so spawned claude sessions skip ECC's two
    noisiest hooks: GateGuard fact-force and the cost-critical alerter.

    Invariants the wire-ups depend on:
      - Sets both `ECC_GATEGUARD=off` and `ECC_DISABLED_HOOKS`. Either
        knob alone is enough to silence GateGuard, but `ecc-context-
        monitor` only honours the disabled-hooks list, so both go in.
      - Never clobbers a user-provided `ECC_GATEGUARD` (e.g. if the
        operator deliberately set it elsewhere).
      - Appends to any existing `ECC_DISABLED_HOOKS` rather than
        replacing it, so a user-disabled hook stays disabled.
      - Skipped entirely when `TAKKUB_ECC_FULL=1` is set — escape
        hatch for the rare case a future ECC hook gets caught in the
        mute net.
    """
    if os.environ.get("TAKKUB_ECC_FULL") == "1":
        return
    env.setdefault("ECC_GATEGUARD", "off")
    extra = ",".join(_ECC_MUTED_HOOKS)
    existing = env.get("ECC_DISABLED_HOOKS", "").strip()
    env["ECC_DISABLED_HOOKS"] = f"{existing},{extra}" if existing else extra


# Where to look for the Obsidian vault that mirrors cockpit decision
# logs. Resolution order:
#   1. $TAKKUB_VAULT_DIR  — explicit override, wins over everything
#   2. ~/WebstormProjects/second-brain — author's default vault layout
# We require an existing `01-Projects/` folder inside the candidate before
# treating it as a vault: a stray empty dir at the default path mustn't
# silently absorb session logs. Returns None when nothing matches, which
# tells callers to skip the mirror without raising.
_VAULT_ENV = "TAKKUB_VAULT_DIR"
_DEFAULT_VAULT = pathlib.Path.home() / "WebstormProjects" / "second-brain"


def _resolve_vault_dir() -> pathlib.Path | None:
    """Return the configured Obsidian vault root, or None if missing."""
    candidates: list[pathlib.Path] = []
    override = os.environ.get(_VAULT_ENV, "").strip()
    if override:
        candidates.append(pathlib.Path(override))
    candidates.append(_DEFAULT_VAULT)
    for cand in candidates:
        if (cand / "01-Projects").is_dir():
            return cand
    return None


# Sessions whose `note` matches one of these (case-insensitive, after
# stripping) are treated as no-information events and never reach the
# vault. They still flow through `agentDone` / `_recent_done_events` so
# Lead's inbox + hot.md still surface them — we just don't pollute
# Obsidian with stubs that have no analytical value.
_JUNK_NOTE_EXACT = frozenset(
    {
        "",
        ".",
        "ok",
        "ok.",
        "ok done",
        "done",
        "done.",
        "wip",
        "wip.",
        "appended",
        "yes",
        "no",
        "fixed",
        "all green",
    }
)

# Notes shorter than this (after stripping) are treated as junk even if
# they don't match the exact-junk list. 15 chars is enough for a
# substantive 2-3 word summary like "added /login" but trims one-word
# acknowledgements.
_JUNK_NOTE_MIN_LEN = 15

# Project names matching one of these prefixes are also skipped from
# the vault mirror — typically scratch/test/throwaway workspaces that
# nobody wants in the Obsidian graph.
_JUNK_PROJECT_PREFIXES = ("test", "tmp", "scratch", "playground")


def _is_junk_note(note: str) -> bool:
    """Return True when the takkub-done note is too thin to keep."""
    s = (note or "").strip().lower()
    if s in _JUNK_NOTE_EXACT:
        return True
    return len(s) < _JUNK_NOTE_MIN_LEN


def _is_junk_project(project: str) -> bool:
    """Return True when the project name looks like a scratch workspace."""
    p = (project or "").strip().lower()
    if not p:
        return True
    return any(p.startswith(prefix) for prefix in _JUNK_PROJECT_PREFIXES)


def _render_decision_note(project: str, role: str, note: str, now: datetime) -> str:
    """Render the markdown body shared by the local session log and the
    vault mirror. Single source of truth so the two copies don't drift.

    Body layout (Obsidian-friendly):
      - YAML frontmatter: role / project / date / tags → enables
        Dataview queries and `tag:#session` filters.
      - `[[01-Projects/<project>|<project>]]` backlink in body so the
        graph view clusters each session under its project page.
      - Plain markdown `## Note` block so events.log/hot.md scrapers
        keep working with the existing pattern.
    """
    iso = now.isoformat(timespec="seconds")
    return (
        f"---\n"
        f"role: {role}\n"
        f"project: {project}\n"
        f"date: {iso}\n"
        f"tags: [session, {role}, {project}]\n"
        f"---\n\n"
        f"# {role} done · {iso}\n\n"
        f"**Project:** [[01-Projects/{project}|{project}]]\n"
        f"**Role:** {role}\n\n"
        f"## Note\n\n{note.strip()}\n"
    )


# Where teammate-pane state lives between cockpit restarts. Lead panes
# are already restored by the open_tabs mechanism in projects.json
# (one Lead per tab). Teammate panes — frontend/backend/qa/etc. that
# the user spawned manually — disappear when cockpit shuts down. The
# session snapshot file records which teammates were live in each tab
# at the moment of shutdown (or at the last periodic tick) so the next
# cockpit launch can re-spawn them with --continue and the user resumes
# right where they left off.
#
# Skip snapshots older than _LAST_SESSION_MAX_AGE_SEC: an hour-old
# snapshot is stale enough that the underlying claude conversations
# have probably been compacted out of usefulness and a fresh spawn is
# kinder than a confusing `--continue` against half-remembered context.
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


# Plugins we want spawned agents to inherit *explicitly* (skipping user-level
# settings to avoid claude-obsidian's broken SessionStart hook). Each entry
# is a *marketplace name* under ~/.claude/plugins/cache/. We pick the highest
# semver-ish version directory found.
_SAFE_PLUGINS: tuple[str, ...] = ("superpowers-dev", "addy-agent-skills", "pordee")


def _allowed_project_roots(project: str) -> list[pathlib.Path]:
    """Return resolved Path objects for every path configured in `project`."""
    data = load_projects()
    proj = (data.get("projects") or {}).get(project) or {}
    return [pathlib.Path(p).resolve() for p in (proj.get("paths") or {}).values()]


def _cwd_within_project(cwd: str, project: str) -> bool:
    """True when `cwd` resolves under one of `project`'s configured roots,
    or under the cockpit repo itself (needed for Lead self-edit tasks)."""
    target = pathlib.Path(cwd).resolve()
    if target == REPO_ROOT.resolve() or REPO_ROOT.resolve() in target.parents:
        return True
    return any(
        target == root or root in target.parents
        for root in _allowed_project_roots(project)
    )


def _exit_key(project: str, role: str) -> str:
    """Composite key for `_recent_exits` so the same role in different
    project tabs never shares a resume record."""
    return f"{project}::{role}"


def _render_lead_context(project: str | None = None) -> str | None:
    """Render Lead's spawn-time system prompt: cockpit CLAUDE.md + an
    auto-injected `BLOCKED_DIRS` paragraph listing the active project's paths.

    Lead's hybrid policy (see cockpit CLAUDE.md "Lead direct-edit policy")
    forbids direct file edits inside project paths; this function bakes the
    *current* project's paths into the prompt every spawn so the rule has
    teeth even when projects.json switches between sessions.

    Returns the rendered file path (string), or None if there's no cockpit
    CLAUDE.md to render from.
    """
    cockpit_md = REPO_ROOT / "CLAUDE.md"
    if not cockpit_md.exists():
        return None

    base = cockpit_md.read_text(encoding="utf-8")
    if project is not None:
        data = load_projects()
        proj = (data.get("projects") or {}).get(project) or {}
        name = project
    else:
        name, proj = active_project()
    paths = list((proj.get("paths") or {}).values()) if proj else []

    if paths:
        blocked = "\n".join(f"- `{p}`" for p in paths)
        header = f"active project: **{name}**" if name else "active project:"
    else:
        blocked = "- (no active project — projects.json `active` not set)"
        header = "active project:"

    suffix = f"""

---

## 🚫 BLOCKED_DIRS (auto-injected at spawn)

{header}

ไดเรกทอรีต่อไปนี้คือ project code — Lead **ห้ามใช้ Edit / Write / MultiEdit / NotebookEdit** ในไฟล์ใต้ paths เหล่านี้เด็ดขาด:

{blocked}

ถ้างานต้องแก้ไฟล์ในเส้นทางข้างบน → ใช้ `takkub assign --role <role> --cwd <path> "<task>"` เสมอ

✅ ทำเองได้:
- Read / Grep / Glob ทุกที่ (สำหรับวางแผน + เขียน task spec)
- Edit / Write ใน cockpit ({REPO_ROOT}) เช่น CLAUDE.md, projects.json, .claude/agents/*
- `git status` / `git log` / `git diff` (inspection ไม่กระทบไฟล์)

❌ ห้ามทำเองแม้แค่บรรทัดเดียว:
- ทุกไฟล์ใต้ BLOCKED_DIRS ข้างบน
- งานที่ touch > 1 ไฟล์
- งานที่ edit > 30 บรรทัดในรอบเดียว

ละเมิดข้อใดข้อหนึ่ง → หยุดทันทีแล้ว delegate ผ่าน `takkub assign`
"""
    ensure_runtime()
    out = RUNTIME_DIR / "lead-context.md"
    out.write_text(base + suffix, encoding="utf-8")
    return str(out)


def _default_plugin_dirs() -> list[str]:
    """Resolve ~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/ for
    each plugin in `_SAFE_PLUGINS`, returning the directories that actually
    contain a `.claude-plugin/plugin.json`. Best-effort; never raises."""
    home = pathlib.Path.home()
    cache = home / ".claude" / "plugins" / "cache"
    out: list[str] = []
    if not cache.exists():
        return out
    for marketplace in _SAFE_PLUGINS:
        mp_dir = cache / marketplace
        if not mp_dir.is_dir():
            continue
        # one plugin per marketplace; one version per plugin (typically)
        for plugin in sorted(mp_dir.iterdir()):
            if not plugin.is_dir():
                continue
            versions = sorted((v for v in plugin.iterdir() if v.is_dir()), reverse=True)
            for v in versions:
                if (v / ".claude-plugin" / "plugin.json").exists():
                    out.append(str(v))
                    break
    return out


class Orchestrator(QObject):
    """Owns the pane registry and routes commands.

    Layout policy: Lead is always pre-registered (created by main_window) and
    fills the window initially. Teammate panes are created on demand the
    first time we spawn that role, via the `paneRequested` signal which
    main_window connects to its own add-pane logic.
    """

    statusChanged = pyqtSignal()
    leadInjected = pyqtSignal(str)
    # Emitted at the tail of a successful spawn that picked up `--continue`
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

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # Browser MCPs (playwright + chrome-devtools) follow Lead into
        # every project. Merge them into runtime/shared-mcp.json before
        # any pane spawns — the orchestrator will then hand the file to
        # claude via `--mcp-config` and panes pick the servers up
        # uniformly across projects. Idempotent: existing pms config
        # and bearer token are preserved untouched. Failure is
        # non-fatal (logged once and panes spawn without browser MCPs)
        # so a broken vault path or readonly runtime never blocks
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
        # Panes are namespaced per project so the upcoming multi-tab UI
        # (Plan B) can keep each project's Lead + teammates isolated. The
        # `panes` property below resolves to the *active* project's inner
        # dict so every existing caller (UI + tests) keeps the same shape.
        # Until tabs land, only one project namespace is populated at a
        # time and behavior is identical to the pre-refactor single-dict.
        self._panes_by_project: dict[str, dict[str, AgentPane]] = {}
        # last-known cwd per role, used to decide whether to pass --continue
        # on a fresh spawn (must match the previous cwd for resume to be valid)
        self._recent_exits: dict[str, dict] = {}  # "{project}::{role}" -> {cwd, ts}

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
        # Last stuck-recover wall-clock per pane (key `<project>::<role>`).
        # Prevents the watchdog from looping recover→stuck→recover on a
        # chronically wedged claude.
        self._last_stuck_recover: dict[str, float] = {}
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

    def unregister_pane(self, role_name: str, project: str | None = None) -> None:
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
        role_name = role_name.lower().strip()
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
        if cwd and project_ns != "default" and not _cwd_within_project(cwd, project_ns):
            return False, f"cwd '{cwd}' is outside project '{project_ns}' paths"

        # ── codex pane: non-claude path ─────────────────────────────
        # `codex` is OpenAI's TUI; it speaks a different protocol and
        # doesn't understand any of the claude flags below. Build a
        # minimal argv and short-circuit so we don't accidentally pass
        # `--dangerously-skip-permissions`, MCP configs, plugin dirs,
        # or `--continue` (all claude-only) to it.
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
            env = os.environ.copy()
            env["TAKKUB_ROLE"] = role_name
            env["TAKKUB_PROJECT"] = project_ns
            bin_dir = str(REPO_ROOT / "bin")
            env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
            gemini_argv = [
                gemini_bin,
                "-y",  # yolo: skip per-command approval prompts (parity with codex --ask-for-approval never)
            ]
            session = PtySession(cols=110, rows=36, parent=self)
            try:
                session.spawn(argv=gemini_argv, cwd=spawn_cwd, env=env)
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
            env = os.environ.copy()
            env["TAKKUB_ROLE"] = role_name
            env["TAKKUB_PROJECT"] = project_ns
            bin_dir = str(REPO_ROOT / "bin")
            env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
            # Autonomy flags so Codex can call `takkub done` and edit
            # workspace files without stopping for per-command approval —
            # mirrors claude's `--dangerously-skip-permissions`. Default
            # to workspace-write sandbox (no system-wide reach) so an
            # off-the-rails codex can still only touch its cwd.
            codex_argv = [
                codex_bin,
                "--ask-for-approval",
                "never",
                "-s",
                "workspace-write",
            ]
            session = PtySession(cols=110, rows=36, parent=self)
            try:
                session.spawn(argv=codex_argv, cwd=spawn_cwd, env=env)
            except Exception as e:
                return False, f"failed to spawn codex: {e}"
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
                role_md_file = _render_lead_context(project_ns)
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

        env = os.environ.copy()
        env["TAKKUB_ROLE"] = role_name
        # Tag the pane with its project so the `takkub` CLI inside the
        # session can stamp every JSON request with `from_project`. The
        # cli_server uses that to scope routing to panes in the *same*
        # project — under the multi-tab refactor a Lead in unirecon
        # mustn't accidentally send to a backend pane that belongs to pms.
        env["TAKKUB_PROJECT"] = project_ns
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

        _apply_ecc_mute(env)

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

        # Inject the cockpit's shared MCP config (pms MCP server with
        # bearer token) so every spawned claude session has the pms
        # tools available, regardless of what the project's own
        # `.claude/settings.json` contains. The file lives under
        # runtime/ which is gitignored, so the bearer never leaks via
        # checked-in config. Skipped silently if the user hasn't set
        # up the token yet (UI offers a "Setup pms MCP" prompt).
        try:
            from .shared_dev_tools import (
                ensure_browser_mcps,
                shared_mcp_config_path,
            )

            # Re-apply the browser-MCP merge on every spawn so a cockpit
            # instance that booted before the feature shipped still gives
            # newly-spawned panes the browser servers. Idempotent: if
            # they're already in the file this is a no-op disk read.
            ensure_browser_mcps()
            mcp_cfg = shared_mcp_config_path()
        except Exception:
            mcp_cfg = None
        if mcp_cfg:
            argv.extend(["--mcp-config", mcp_cfg])
            # Force claude to use *only* our cockpit-managed MCP config.
            # Without this flag claude *also* loads servers registered at
            # user-level via `claude mcp add` (stored in ~/.claude.json),
            # which is independent of --setting-sources. If a `pms` entry
            # is registered there too — typical when the user once ran
            # `claude mcp add pms ...` directly — the user-level config
            # wins and Lead ends up calling the old/revoked bearer no
            # matter how many times we rewrite runtime/shared-mcp.json.
            # Strict mode means the cockpit's file is the single source
            # of truth for MCP inside a pane.
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
        # cwd, ask claude to continue the previous conversation instead of
        # starting fresh. Useful for crash recovery + accidental closes.
        resumed = False
        prior = self._recent_exits.get(_exit_key(project_ns, role_name))
        if (
            prior
            and prior.get("cwd") == spawn_cwd
            and (time.time() - prior.get("ts", 0)) < RESUME_WINDOW_SEC
        ):
            argv.append("--continue")
            resumed = True

        session = PtySession(cols=110, rows=36, parent=self)
        try:
            session.spawn(argv=argv, cwd=spawn_cwd, env=env)
        except Exception as e:
            return False, f"failed to spawn claude: {e}"

        pane.attach_session(session, cwd=spawn_cwd)
        # Record exits so a fast respawn can pass --continue, and so the
        # auto-respawn watcher knows which project namespace owned the
        # pane that just died.
        session.processExited.connect(
            lambda _code, r=role_name, c=spawn_cwd, p=project_ns: self._on_session_exit(r, c, p)
        )
        # forget the prior exit record now that we've spawned successfully
        _ekey = _exit_key(project_ns, role_name)
        if _ekey in self._recent_exits:
            del self._recent_exits[_ekey]

        self._auto_trust(role_name, project=project_ns)
        self.statusChanged.emit()
        if resumed:
            # main_window listens for this to auto-bridge `/remote-control`
            # exclusively on resumes — fresh boots stay silent.
            self.paneResumed.emit(role_name, project_ns)
        _log_event(
            "spawn",
            role=role_name,
            cwd=spawn_cwd,
            resumed=resumed,
        )
        suffix = " (resumed)" if resumed else ""
        return True, f"{role_name} spawned in {spawn_cwd}{suffix}"

    def _on_session_exit(self, role_name: str, cwd: str, project: str) -> None:
        """Track recent exits so a quick respawn can pass --continue, then
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
        `--continue` is added automatically by `spawn()` because the
        previous exit is still inside RESUME_WINDOW_SEC."""
        # If the pane was already manually respawned during the delay,
        # bail. The new session would have already cleared `state`.
        pane = self._panes_by_project.get(project, {}).get(role_name)
        if pane is None or (pane.session is not None and pane.session.is_alive):
            return
        ok, msg = self.spawn(role_name, cwd=cwd, project=project)
        _log_event("auto_respawn_done", role=role_name, project=project, ok=ok, msg=msg[:160])

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
        self, role_name: str, cwd: str | None, task: str, project: str | None = None
    ) -> tuple[bool, str]:
        ok, msg = self.spawn(role_name, cwd=cwd, project=project)
        if not ok:
            return ok, msg

        self._send_when_ready(role_name, task, project=project)
        _log_event("assign", role=role_name, cwd=cwd, task_preview=task[:120])
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

    def send(
        self,
        to_role: str,
        msg: str,
        from_role: str | None = None,
        project: str | None = None,
    ) -> tuple[bool, str]:
        to_role = to_role.lower().strip()
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

        # CC Lead unless source was Lead and target was a teammate, or vice versa
        if from_role and from_role not in (None, LEAD.name) and to_role != LEAD.name:
            lead = project_panes.get(LEAD.name)
            if lead and lead.session and lead.session.is_alive:
                cc_payload = _paste_payload(f"[CC] {body}")
                lead.session.write(cc_payload)
                QTimer.singleShot(
                    _enter_delay_ms(cc_payload),
                    lambda: lead.session and lead.session.write(b"\r"),
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

        _log_event("send", to=to_role, from_=from_role, msg_preview=msg[:120])
        return True, f"sent to {to_role}"

    def close(self, role_name: str, project: str | None = None) -> tuple[bool, str]:
        role_name = role_name.lower().strip()
        project_ns = self._resolve_project(project)
        pane = self._project_panes(project_ns).get(role_name)
        if pane is None:
            return False, f"unknown role: {role_name}"
        was_alive = pane.session is not None
        if was_alive:
            # mark exit as expected so the pane doesn't surface "exited"/crash
            pane.mark_expected_exit()
            pane.session.terminate()
            pane.set_state("empty", note=None)
        key = f"{project_ns}::{role_name}"
        self._idle_state.pop(key, None)
        self._blocked_on_lead.pop(key, None)
        self._auto_respawn_attempts.pop(key, None)
        # For teammates, fully remove from the layout so the right column
        # collapses back. Lead stays as it always anchors the cockpit.
        # The project namespace travels with the signal so main_window
        # can route the removal to the correct tab even when the user
        # is viewing a different project at the moment of close (the
        # `done`-triggered close fires 2.5 s after the agent reports
        # done, plenty of time for a tab switch).
        if role_name != LEAD.name:
            self.paneClosed.emit(role_name, project_ns)
        self.statusChanged.emit()
        _log_event("close", role=role_name)
        return True, f"{role_name} closed"

    def done(self, from_role: str, note: str = "", project: str | None = None) -> tuple[bool, str]:
        from_role = from_role.lower().strip()
        project_ns = self._resolve_project(project)
        project_panes = self._project_panes(project_ns)
        pane = project_panes.get(from_role)
        if pane is None:
            return False, f"unknown role: {from_role}"
        # Agent finished cleanly — clear any pending watchdog state so
        # the next session starts fresh (no leftover idle streak, no
        # leftover "blocked on lead" flag, no carried auto-respawn count).
        key = f"{project_ns}::{from_role}"
        self._idle_state.pop(key, None)
        self._blocked_on_lead.pop(key, None)
        self._auto_respawn_attempts.pop(key, None)

        # notify Lead in the same project (a teammate in unirecon mustn't
        # nudge the Lead in pms by mistake)
        lead = project_panes.get(LEAD.name)
        notice = f"[{from_role} done] {note}".rstrip()
        if lead and lead.session and lead.session.is_alive:
            lead.session.write(notice)
            QTimer.singleShot(150, lambda: lead.session and lead.session.write(b"\r"))
            self.leadInjected.emit(notice)

        # mark pane done, auto-close after a delay so user can see it
        pane.set_state("done", note=note[:80] if note else "done")
        QTimer.singleShot(2_500, lambda: self.close(from_role, project=project_ns))
        _log_event("done", role=from_role, note=note[:200])
        now = datetime.now()
        self._save_decision_note(project_ns, from_role, note, now=now)
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
        project: str, role: str, note: str, now: datetime | None = None
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
        body = _render_decision_note(project, role, note, now)
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

    def list_status(self, project: str | None = None) -> dict[str, str]:
        """Snapshot of `role → state` for one project's panes.

        Defaults to the active project's view, so a Lead in unirecon never
        accidentally sees a backend pane that belongs to pms.
        """
        return {name: p.state for name, p in self._project_panes(project).items()}

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

        The orchestrator's existing `_recent_exits` machinery handles
        `--continue` — by stamping each role with a fresh `ts` here, the
        next spawn falls inside RESUME_WINDOW_SEC and claude rejoins the
        previous conversation instead of starting a blank one.
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
                # Stamp recent-exit so spawn() picks --continue. The
                # session file is the cwd it was last running in — the
                # one claude can resume from.
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

    def _check_stuck_panes(self, now: float) -> None:
        """Walk every teammate pane and auto-recover any that's been
        sitting in `working` state with no PTY output for longer than
        STUCK_THRESHOLD_S. A recovered pane runs close→spawn and gets
        --continue via the existing recent-exits machinery, so claude
        rejoins the conversation rather than restarting blank.

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
        """Close the wedged pane and respawn it with --continue. The
        spawn uses the pane's last-known cwd so claude rejoins the same
        project directory."""
        cwd = pane._session_cwd
        key = f"{project}::{role}"
        self._last_stuck_recover[key] = now
        # Reset the output timestamp so the next tick doesn't re-trigger
        # before claude has had a chance to print anything from the new
        # session.
        pane._last_output_ts = now
        _log_event(
            "stuck_pane_recover",
            role=role,
            project=project,
            cwd=cwd or "",
            silent_for_s=int(now - getattr(pane, "_last_output_ts", now)),
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
