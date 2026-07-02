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

import collections
import hashlib
import json
import os
import pathlib
import re
import secrets
import threading
import time
import uuid as _uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from PyQt6.QtCore import QObject, QProcess, QTimer, pyqtSignal

from .agent_pane import AgentPane
from .broadcast_actions import BroadcastMixin
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
from .lead_context import (  # re-exported for test imports
    _LEAD_GUARD_ALLOW_TOOLS,
    _LEAD_GUARD_WRITE_TOOLS,
    BIG_FILE_GUARD,
    STALE_FILE_GUARD,
    _allowed_project_roots,
    _default_plugin_dirs,
    _recent_session_brief,
    _render_lead_context,
    render_lead_settings,
)
from .lead_inbox import (  # re-exported for test/compat imports; mixin provides methods
    _SUBMIT_MAX_RESENDS,
    _SUBMIT_VERIFY_GRACE_MS,
    LEAD_NOTIFY_BUSY_CAP,
    LeadInboxMixin,
    _delayed_enter,
    _delayed_enter_verified,
)
from .orchestrator_text import (  # re-exported for test/app/main_window imports
    _CODEX_TASK_NOTICE,
    _DEFAULT_TEAMMATE_TIER,
    _EVENTS_LOG_MAX_BYTES,
    _HARVEST_EXCLUDE_DIRS,
    _HOT_MD_INTERVAL_MS,
    _PASTE_END,
    _PASTE_ENTER_DELAY_MS,
    _PASTE_MAX_ENTER_DELAY_MS,
    _PASTE_START,
    _ROLE_MODEL_TIERS,
    _TYPING_ENTER_DELAY_MS,
    BRACKETED_PASTE_THRESHOLD,
    _build_transcript_path,
    _cwd_within_project,
    _enter_delay_ms,
    _exit_key,
    _lead_model_override,
    _log_event,
    _paste_payload,
    _read_tail_bytes,
    _render_daily_digest,
    _render_hot_md,
    _resolve_project_memory,
    _rewrite_task_for_codex,
    _sanitize_pane_text,
    _teammate_tier,
    prune_old_transcripts,
    scan_artifacts,
)
from .pane_env import (  # re-exported for test imports — see pane_env.py docstring
    _DEFAULT_MCP_TOOL_TIMEOUT_MS,
    _LEAD_ENV_EXTRA_ALLOWLIST,
    _PANE_ENV_ALLOWLIST,
    _apply_color_term,
    _apply_mcp_timeout,
    _apply_non_interactive_env,
    _build_lead_env,
    _build_pane_env,
    inject_user_profile_env,
)
from .pipeline_executor import (  # re-exported for test imports; mixin provides methods
    _SHARD_GROUP_TIMEOUT_MS,
    _SPAWN_STAGGER_MS,
    PipelineMixin,
    PipelineRun,
    ShardGroup,
    _split_shard,
)
from .pty_session import PtySession
from .roles import LEAD
from .spawn_engine import (  # re-exported for backward compat; mixin provides methods
    _PANE_COLS,
    _PANE_ROWS,
    _STUCK_RESUME_NUDGE,
    _TOCTOU_RESAMPLE_N,
    AUTO_RESPAWN_DELAY_MS,
    AUTO_RESPAWN_MAX,
    CODEX_EARLY_CRASH_WINDOW_SEC,
    RESUME_WINDOW_SEC,
    PaneRegistry,
    PaneState,
    SpawnEngineMixin,
)
from .vault_mirror import (  # re-exported for test + script imports
    _DEFAULT_VAULT,
    _JUNK_NOTE_EXACT,
    _JUNK_NOTE_MIN_LEN,
    _JUNK_PROJECT_PREFIXES,
    _VAULT_ENV,
    _is_dedup_note,
    _is_junk_note,
    _is_junk_project,
    _render_decision_note,
    _resolve_vault_dir,
    distill_session_facts,
    prune_vault_logs,
    write_obsidian_graph_filter,
)

_ANSI = re.compile(r"\x1b\[[0-9;]*[mABCDHJKSThlsu]")

# Bound on how many bytes of a pane transcript we read to extract its tail for
# `takkub status`. A long session's transcript grows to MBs; reading the whole
# file every status call (just to keep the last few lines) is an unbounded memory
# spike. 64 KiB is ample for the 5-line tail even with very long lines. (M4#22)
_TRANSCRIPT_TAIL_BYTES = 64 * 1024

# Harvest hint: inject a '[cockpit] <role> ไม่ active >Nm' message into Lead
# when a teammate pane has been idle this long. 0 = disabled.
HARVEST_HINT_SEC = int(os.environ.get("TAKKUB_HARVEST_HINT_SEC", "600"))

__all__ = [  # backwards-compat re-exports
    "HARVEST_HINT_SEC",
    "LEAD_NOTIFY_BUSY_CAP",
    "_DEFAULT_MCP_TOOL_TIMEOUT_MS",
    "_DEFAULT_VAULT",
    "_HARVEST_EXCLUDE_DIRS",
    "_JUNK_NOTE_EXACT",
    "_JUNK_NOTE_MIN_LEN",
    "_JUNK_PROJECT_PREFIXES",
    "_LEAD_ENV_EXTRA_ALLOWLIST",
    "_LEAD_GUARD_ALLOW_TOOLS",
    "_LEAD_GUARD_WRITE_TOOLS",
    "_PANE_ENV_ALLOWLIST",
    "_VAULT_ENV",
    "PaneRegistry",
    "_allowed_project_roots",
    "_apply_color_term",
    "_apply_mcp_timeout",
    "_apply_non_interactive_env",
    "_build_lead_env",
    "_build_pane_env",
    "_default_plugin_dirs",
    "_is_dedup_note",
    "_is_junk_note",
    "_is_junk_project",
    "_recent_session_brief",
    "_render_decision_note",
    "_render_lead_context",
    "_resolve_vault_dir",
    "inject_user_profile_env",
    "prune_old_transcripts",
    "prune_vault_logs",
    "render_lead_settings",
    "scan_artifacts",
    "write_obsidian_graph_filter",
]


# RESUME_WINDOW_SEC moved to spawn_engine.py; re-exported above

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

# Stuck-paste reaper. A task pasted at spawn renders "[Pasted text +N lines]"
# in the input box; under parallel-spawn CPU load the submitting Enter (and the
# delivery self-heal's 3 resends, all within ~3 s) can be swallowed while
# claude is still initialising — leaving the pane idle-at-ready with the task
# forever unsent. The idle reminder used to rescue this by accident (its
# trailing Enter submitted the stuck paste), but any reminder-suppression gate
# (e.g. a rate-limit flag) starves that rescue. This reaper is the DIRECT fix:
# a "working" pane at its ready prompt showing pending input for
# STUCK_PASTE_SUBMIT_AFTER_S gets a bare CR (harmless if a submit is somehow
# in flight), retried every STUCK_PASTE_SUBMIT_COOLDOWN_S up to
# STUCK_PASTE_SUBMIT_MAX times. It runs BEFORE every suppression gate so no
# false flag can starve it.
STUCK_PASTE_SUBMIT_AFTER_S = 15.0
STUCK_PASTE_SUBMIT_COOLDOWN_S = 30.0
STUCK_PASTE_SUBMIT_MAX = 4

# Structural stale-marker detector (#20). A pane that is alive, has produced no
# output for STALE_MARKER_QUIET_S (a generating CLI streams continuously, so
# this long a silence means it is NOT mid-generation), and is matched by NO
# state marker (not ready, not a known tty/trust/splash prompt) is almost
# certainly sitting at an idle prompt whose wording an upstream CLI update
# changed out from under our markers — the silent-break failure mode of #20.
# We log it (rate-limited per pane) WITH the bottom screen text so the operator
# can see the real footer and rescue detection via TAKKUB_EXTRA_READY_MARKERS.
STALE_MARKER_QUIET_S = 20.0
STALE_MARKER_COOLDOWN_S = 600.0
STALE_MARKER_TAIL_ROWS = 4

# A teammate pane in `working` state with no PTY output for this long
# is treated as hung — claude probably crashed silently, deadlocked on
# a tool call, or got wedged behind a slow MCP server. Orchestrator
# auto-recovers via close + respawn (which picks `--resume <uuid>` because
# the recent-exit timestamp and UUID are still fresh). 10 minutes is generous enough
# that a heavy `npm install` or a slow Lighthouse audit won't trip it.
STUCK_THRESHOLD_S = 10 * 60
# Once a recover fires for a pane, wait this long before another one
# is allowed — otherwise a chronically-stuck workload restarts on a loop.
STUCK_RECOVER_COOLDOWN_S = 5 * 60
# Hard cap on consecutive stuck-recover attempts for a single pane (#41).
# auto-respawn-attempts only caps *crash* respawns; a pane that is alive but
# wedged (deadlocked on a tool call, never reports done) never crashes, so the
# cooldown above would otherwise let the watchdog close→respawn it forever —
# stalling any pipeline hop it belongs to indefinitely. After this many
# recoveries we give up: warn Lead, fail+advance the pipeline hop, and leave the
# pane for the operator instead of looping. ~3 strikes ≈ 30 min of repeated
# wedging (STUCK_THRESHOLD_S + STUCK_RECOVER_COOLDOWN_S per cycle).
STUCK_RECOVER_MAX = 3

# TTY prompt block detection (issue #54). When a pane's subprocess is waiting
# for interactive input (y/N, passphrase, "press any key"), close→respawn won't
# help because the prompt comes from the subprocess, not claude. Suppress the
# idle forgot-done reminder (wrong context) and surface a notice to Lead instead.
# Auto-recover is deliberately opt-in / off-by-default.
TTY_BLOCK_SURFACE_AFTER_S = 2 * 60  # first surface after 2 min of continuous block
TTY_BLOCK_SURFACE_COOLDOWN_S = 3 * 60  # minimum gap between repeated surface notices

# Update-splash dismissal (issue #62). When a codex pane is stuck at the
# startup 'update available!' splash, send Enter once to dismiss it instead of
# close→respawn. SPLASH_DISMISS_COOLDOWN_S is the grace period after the Enter
# before we declare the dismiss a failure and fall back to close→respawn.
SPLASH_DISMISS_COOLDOWN_S = 30

# Malformed tool-call XML detection (issue #59). When a model outputs tool-call
# XML without the `antml:` namespace prefix the harness silently no-ops it and
# the pane appears to hang. Nudge the pane at most this often.
MALFORMED_XML_NOTICE_COOLDOWN_S = 60  # minimum gap between repeat nudges

# _STUCK_RESUME_NUDGE moved to spawn_engine.py; re-exported above

# Session-goal context header (issue #50). Prepended to every `assign`
# task while a goal is set. Also doubles as the idempotency marker that
# _apply_session_goal greps for to avoid double-prepending on respawn replay.
_SESSION_GOAL_HEADER = "[SESSION GOAL — ทุก role ในงานนี้ยึดเป้าหมายเดียวกัน]"

# A goal is a short objective + scope boundary; bound it so a pathological
# paste (review tok-3: worst case ~64 KiB) can't be re-prepended to every
# assign for the rest of the session. 4000 chars (~1k tokens) is far above any
# real objective. Truncation is at set-time so the stored value is already
# clean for every later prepend.
_SESSION_GOAL_MAX = 4000

# Throughput watchdog (issue #35): flag panes whose PTY output rate exceeds
# RUNAWAY_BYTES_S continuously for RUNAWAY_DURATION_S seconds.
#
# Rationale for thresholds:
#   500 KB/s — a fast build log (e.g. webpack) peaks around 100-200 KB/s;
#   500 KB/s sustained is essentially only seen when a loop prints without
#   any sleep (runaway agent).
#   60 s — a single burst (e.g. `npm install`) can look high for ~10 s;
#   requiring it to sustain 60 s eliminates transient spikes that are not
#   worth bothering Lead about.
RUNAWAY_BYTES_S = 500_000  # 500 KB/s sustained output rate
RUNAWAY_DURATION_S = 60.0  # seconds of sustained overrate before warning Lead
RUNAWAY_WARN_COOLDOWN_S = 300.0  # suppress repeat warnings for 5 min

# Over-capacity advisory (Queue-gap audit, docs/reviews/2026-06-30-queue-gap.md).
# When a fresh teammate spawn pushes the TOTAL live pane count over what the
# machine can comfortably run (exec_mode.machine_total_pane_cap()), warn the Lead
# once per this window so a burst of fan-out assigns doesn't spam. Non-blocking.
OVERCAP_WARN_COOLDOWN_S = 60.0


def _count_active_teammates(panes_by_project: dict) -> int:
    """Total live non-Lead panes across every project (machine-wide).

    Shared by the over-capacity advisory and the fan-out queue so the two can
    never drift on what "machine is full" means. Lead panes (one per tab) are
    excluded — they anchor the cockpit and aren't the resource hogs.
    """
    n = 0
    for _panes in panes_by_project.values():
        for _r, _p in _panes.items():
            if _r == LEAD.name:
                continue
            sess = getattr(_p, "session", None)
            if sess is not None and getattr(sess, "is_alive", False):
                n += 1
    return n


def _fanout_queue_enabled() -> bool:
    """True iff the flag-gated fan-out queue is enabled. Default OFF, so the
    cockpit's spawn behaviour is unchanged unless the operator opts in via
    TAKKUB_QUEUE_FANOUT (the over-capacity advisory still fires regardless).

    When ON, a fresh teammate spawn that would exceed machine_total_pane_cap()
    is deferred to a per-project queue and spawned automatically once a pane
    frees a slot (done/close). See docs/reviews/2026-06-30-queue-gap.md.
    """
    return os.environ.get("TAKKUB_QUEUE_FANOUT", "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
        "off",
    )


# Spinner-line filtering for content-delta stuck detection (Fix 3).
# Lines matching any interrupt phrase or volatile counter pattern are excluded
# from the hash so a pane that only emits spinner bytes is still detected as stuck.
_SPINNER_INTERRUPT_PHRASES = ("esc to interrupt", "esc to stop", "ctrl-c to", "ctrl+c to")
_SPINNER_VOLATILE_RE = re.compile(
    r"\d+s[\s·]|[↑↓]\s*[\d.,]+k?\s*tokens?",
    re.IGNORECASE,
)
IDLE_WATCHDOG_INTERVAL_MS = 5_000
IDLE_REMINDER_TEXT = (
    "🔔 [auto-reminder] pane นี้ idle อยู่ — ถ้า task เสร็จแล้วต้อง run "
    '`takkub done "<summary>"` เป็นคำสั่ง shell **ตอนนี้** (ไม่ใช่พิมพ์เป็น text). '
    "Lead ไม่ได้รับ notice จนกว่าจะ run คำสั่งนี้จริง pane จะค้างจน auto-recover. "
    'ยังทำงานต่ออยู่ → ignore ข้อความนี้ ถ้าติด blocker → `takkub send --to lead "..."`'
)

# AUTO_RESPAWN_DELAY_MS, AUTO_RESPAWN_MAX, _PANE_COLS, _PANE_ROWS,
# CODEX_EARLY_CRASH_WINDOW_SEC, _TOCTOU_RESAMPLE_N moved to spawn_engine.py; re-exported above

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


# PaneState moved to spawn_engine.py; re-exported above via SpawnEngineMixin import


class Orchestrator(PipelineMixin, BroadcastMixin, LeadInboxMixin, SpawnEngineMixin, QObject):
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
    # Emitted when user flips the account plan (Pro/Max) via the status bar.
    # main_window listens to repaint the plan chip without polling.
    planTierChanged = pyqtSignal(str)  # "pro" | "max"
    execModeChanged = pyqtSignal(str)  # "solo" | "parallel"
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
    agentDone = pyqtSignal(
        str, str, str
    )  # project_ns, role_name, note — includes project to prevent cross-tab contamination
    # Emitted when a teammate's done() fires for a project that is NOT the
    # currently active tab. main_window connects this to show a status-bar
    # flash so the user sees background-tab activity without switching tabs.
    crossTabDone = pyqtSignal(str, str, str)  # project_ns, role, note
    # Emitted whenever a notice is queued for a live Lead pane (done handoffs,
    # peer-CCs, system messages). main_window connects this to put an unread
    # red dot on that project's Lead pane-tab when the user is looking at a
    # different pane — so a Lead notification can't slip by unseen now that the
    # panes-as-tabs layout shows only one pane at a time.
    leadNotified = pyqtSignal(str)  # project_ns

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
        # Reclaim disk: prune stale per-pane PTY transcripts so runtime/sessions
        # can't grow without bound (a runaway pane once left a 203 MB log).
        # Best-effort and non-fatal — a readonly runtime never blocks startup.
        try:
            prune_old_transcripts()
        except Exception as e:
            _log_event("transcript_prune_error", error=repr(e))
        # Reclaim disk: prune stale per-(project, role, shard, browser) Chromium
        # profile dirs (#39 fan-out) so runtime/browser-profiles/ can't grow
        # without bound (#42). Safe here: no pane is alive yet at startup, so no
        # browser owns a profile, and recently-used login profiles have a fresh
        # mtime and survive the age window. Best-effort / non-fatal.
        try:
            from .shared_dev_tools import prune_old_browser_profiles

            prune_old_browser_profiles()
        except Exception as e:
            _log_event("browser_profile_prune_error", error=repr(e))
        # PaneRegistry groups all 7 spawn-engine state dicts under one object.
        # Backward-compat properties on SpawnEngineMixin let every existing
        # access site (self._pane_state[...] etc.) work unchanged.
        # Lifecycle notes are in PaneRegistry's docstring (spawn_engine.py).
        self._registry: PaneRegistry = PaneRegistry()
        # Peer CC durability: messages queued when Lead is not alive.
        # Keyed by project namespace; flushed to Lead on next Lead spawn.
        self._pending_lead_cc: dict[str, list[dict]] = {}
        self._load_pending_cc()
        # Done-notice durability: `takkub done` notices queued when Lead is
        # not alive at the moment a teammate finishes. Pattern mirrors
        # _pending_lead_cc; flushed to Lead on next Lead spawn AND persisted to
        # disk so a teammate's done report survives a cockpit restart while the
        # Lead is down (issue #13).
        self._pending_done_notices: dict[str, list[dict]] = {}
        # Per-project timestamp of when the reaper first saw pending notices it
        # could not flush because the Lead read as not-ready. Drives the
        # staleness escalation that force-delivers when is_at_ready_prompt() is a
        # perpetual false-negative (e.g. a blocker marker in the Lead's visible
        # conversation reads as busy — #70/#20). Cleared on successful flush.
        self._pending_done_since: dict[str, float] = {}
        self._load_pending_done_notices()
        # Fan-out queue (flag-gated): per-project deque of deferred over-cap
        # assigns. Persisted to disk so a still-queued assign survives a cockpit
        # restart (parity with _pending_done_notices above). Loaded only when
        # TAKKUB_QUEUE_FANOUT is on, so a stale file is ignored when the feature
        # is off. See docs/reviews/2026-06-30-queue-gap.md.
        self._fanout_queue: dict = {}
        self._load_fanout_queue()
        # In-memory serialisation queue for live Lead writes (ready-prompt aware).
        # Keyed by project namespace.  Items are string bodies; a single pump
        # fires per project so concurrent done notices never overwrite each other
        # mid-generation.  Lead-absent items fall through to _pending_done_notices.
        self._lead_notify_queue: dict[str, collections.deque] = {}
        self._lead_notify_pumping: set[str] = set()
        # Busy-retry counter per project_ns; reset on delivery or Lead-dies path.
        self._lead_notify_retry: dict[str, int] = {}
        # Shard fan-out groups: keyed f"{project_ns}::{base_role}".
        # Created on first shard assign, closed when all N shards report.
        self._shard_groups: dict[str, ShardGroup] = {}
        # Pipeline runs: keyed f"{project_ns}::{run_id}".
        # Created by run_pipeline(); closed when last hop completes.
        self._pipeline_runs: dict[str, PipelineRun] = {}
        # Session objective per project (issue #50). Set by Lead via
        # `takkub goal "<objective>"`; prepended to every subsequent
        # `assign` task so parallel teammates share the big picture and
        # don't drift on scope. Volatile (never persisted) and per-project
        # so a goal set in one tab never leaks into another.
        self._session_goals: dict[str, str] = {}

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
        # Kept as a separate dict (not in PaneState) because its key-presence
        # semantics ("absent = not tracking") are relied on by the watchdog and
        # tests — pop() must remove the entry, not merely reset fields.
        self._idle_state: dict[str, dict[str, float | None]] = {}
        # Per-pane last-logged watchdog exception (err_str, ts) — dedups the
        # blind 5s-tick `idle_watchdog_pane_error` spam (was 3279 entries in one
        # events.log) so a persistent fault is logged once with detail, not
        # flooded. See _check_idle_teammates' except block.
        self._idle_err_last: dict[str, tuple[str, float]] = {}
        # Per-pane last-logged "ready marker possibly stale" timestamp — rate-
        # limits the #20 structural staleness detector (a pane alive + output-
        # quiet + matched by NO state marker = likely an upstream prompt reword
        # that silently broke detection). See _check_stale_markers.
        self._stale_marker_last: dict[str, float] = {}
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

        # ── Spawn arbiter (3-layer gate + FIFO serialiser) ──────────
        # Predicate injected by main_window; returns True when Qt has a modal
        # or popup widget active (QDialog/QWizard/QMenu).  None = no guard
        # (tests, headless paths).  Win32 InSendMessageEx is always checked
        # directly inside _is_spawn_blocked() regardless of this predicate.
        self._spawn_gate_pred: Callable[[], bool] | None = None
        # _spawn_deferred, _spawn_queue, _spawn_in_progress → self._registry

    # ──────────────────────────────────────────────────────────────
    # project-aware view onto the pane registry  (SpawnEngineMixin provides _ps + spawn methods)
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

    # ── spawn / registration methods provided by SpawnEngineMixin ──
    # ── Session goal (issue #50) ────────────────────────────────────
    def set_session_goal(self, text: str, project: str | None = None) -> tuple[bool, str]:
        """Set the session objective for `project`. Prepended to every
        subsequent `assign` task so teammates share the big picture."""
        project_ns = self._resolve_project(project)
        text = (text or "").strip()
        if not text:
            return False, "empty goal — pass an objective string, or use --clear to unset"
        if len(text) > _SESSION_GOAL_MAX:
            text = text[:_SESSION_GOAL_MAX].rstrip() + "\n…(goal truncated)"
        self._session_goals[project_ns] = text
        _log_event("goal-set", goal_preview=text[:120], project=project_ns)
        preview = text if len(text) <= 80 else text[:77] + "…"
        return True, f"goal set: {preview}"

    def clear_session_goal(self, project: str | None = None) -> tuple[bool, str]:
        """Unset the session objective for `project`."""
        project_ns = self._resolve_project(project)
        had = self._session_goals.pop(project_ns, None)
        if had is None:
            return True, "no goal was set"
        _log_event("goal-clear", project=project_ns)
        return True, "goal cleared"

    def get_session_goal(self, project: str | None = None) -> str | None:
        """Return the current session objective for `project`, or None."""
        project_ns = self._resolve_project(project)
        return self._session_goals.get(project_ns)

    def _apply_session_goal(self, task: str, project_ns: str) -> str:
        """Prepend the session-goal context block to `task` when one is set.

        No-op when no goal exists, or when the task already carries the
        block (idempotent — guards against double-prepend on auto-respawn
        replay, which re-sends the stored last_assigned_task)."""
        goal = self._session_goals.get(project_ns)
        if not goal:
            return task
        if _SESSION_GOAL_HEADER in task:
            return task
        return f"{_SESSION_GOAL_HEADER}\n{goal}\n\n{task}"

    def assign(
        self,
        role_name: str,
        cwd: str | None,
        task: str,
        requires_commit: bool = False,
        auto_chain: bool = False,
        shard_total: int = 0,
        plan: bool = False,
        project: str | None = None,
    ) -> tuple[bool, str]:
        # Fan-out queue (flag-gated, default off): defer a fresh teammate spawn
        # that would exceed the machine's total-pane budget. It spawns
        # automatically when a pane frees a slot (see _drain_fanout_queue). No-op
        # unless TAKKUB_QUEUE_FANOUT is set, so default behaviour is unchanged.
        if self._should_queue_assign(role_name, project):
            return self._enqueue_assign(
                role_name, cwd, task, requires_commit, auto_chain, shard_total, plan, project
            )

        # Plan mode spawns a single PLANNER pane (not a shard) — it carries
        # shard_total=0 so done() treats it as a normal pane; the fan-out it
        # triggers later assigns the real shards with shard_total=N.
        spawn_shard_total = 0 if plan else shard_total
        ok, msg = self.spawn(role_name, cwd=cwd, project=project, _shard_total=spawn_shard_total)
        if not ok:
            # The CLI already acked "task queued" to the Lead's shell before
            # this async spawn ran, so a failure here is invisible unless we
            # say so. Tell the Lead the task never landed (#26).
            self._warn_lead_spawn_failed(role_name, project, msg)
            # #5: record spawn-failed shard into its group so the aggregate
            # doesn't orphan forever (mirrors _warn_lead_respawn_capped path).
            # Plan mode has no shard group yet (the planner failed before
            # fan-out), so skip — the dead planner pane is visible to the user.
            if shard_total > 0 and not plan:
                pns_fail = self._resolve_project(project)
                base_fail = _split_shard(role_name)[0]
                gk_fail = f"{pns_fail}::{base_fail}"
                if gk_fail not in self._shard_groups:
                    self._shard_groups[gk_fail] = ShardGroup(base_role=base_fail, total=shard_total)
                    gen_fail = self._shard_groups[gk_fail].generation
                    QTimer.singleShot(
                        _SHARD_GROUP_TIMEOUT_MS,
                        lambda gk=gk_fail, pns=pns_fail, g=gen_fail: (
                            self._check_shard_group_timeout(pns, gk, g)
                        ),
                    )
                grp_fail = self._shard_groups[gk_fail]
                if not grp_fail.closed:
                    grp_fail.failed.add(role_name)
                    if len(grp_fail.done) + len(grp_fail.failed) >= grp_fail.total:
                        grp_fail.closed = True
                        self._inject_shard_fanout_handoff(pns_fail, grp_fail)
                        self._shard_groups.pop(gk_fail, None)
            return ok, msg

        from .provider_config import CODEX, effective_provider_for

        project_ns = self._resolve_project(project)

        # #50: prepend the session objective (if Lead set one) so every
        # teammate sees the big picture. Done before the codex rewrite and
        # before storing last_assigned_task, so the goal also rides along on
        # auto-respawn replay (the _apply_session_goal guard keeps it idempotent).
        task = self._apply_session_goal(task, project_ns)

        # Use the *effective* provider: a codex role substituted by claude
        # (provider unavailable) must keep the plain task — codex-specific
        # task rewriting would only confuse the standing-in claude pane.
        # Scoped to project_ns so the per-project role→CLI mapping decides.
        base_role_a = _split_shard(role_name)[0]
        if effective_provider_for(base_role_a, project=project_ns) == CODEX:
            task = _rewrite_task_for_codex(task)
        key = _exit_key(project_ns, role_name)
        ps_assign = self._ps(key)
        ps_assign.last_assigned_task = task
        # New task → fresh one-shot budget for the Stop-hook done-gate.
        ps_assign.stop_gate_notified = False
        if requires_commit:
            ps_assign.requires_commit_on_done = True
        if auto_chain:
            ps_assign.auto_chain = True
        if plan and shard_total > 0:
            # Planner pane: wrap the task with planning instructions, remember
            # the fan-out config, and let done() spawn the shards once the
            # plan file is written. The planner itself is NOT a shard.
            plan_file = self._qa_plan_file(project_ns, base_role_a)
            planner_task = self._wrap_planner_task(task, plan_file, shard_total)
            ps_assign.last_assigned_task = planner_task
            ps_assign.plan_fanout = {
                "shards": shard_total,
                "cwd": cwd,
                "task": task,
                "plan_file": str(plan_file),
            }
            self._send_when_ready(role_name, planner_task, project=project)
            _log_event(
                "assign_plan",
                role=role_name,
                cwd=cwd,
                shards=shard_total,
                plan_file=str(plan_file),
            )
            return True, f"planner queued for {role_name} (fan-out {shard_total} on done)"
        if shard_total > 0:
            ps_assign.shard_total = shard_total
            # Create/update shard group for aggregate tracking.
            group_key = f"{project_ns}::{base_role_a}"
            if group_key not in self._shard_groups:
                group = ShardGroup(base_role=base_role_a, total=shard_total)
                self._shard_groups[group_key] = group
                # #2: capture generation so stale timers from a previous
                # fan-out with the same key don't close this new group.
                gen_a = group.generation
                QTimer.singleShot(
                    _SHARD_GROUP_TIMEOUT_MS,
                    lambda gk=group_key, pns=project_ns, g=gen_a: self._check_shard_group_timeout(
                        pns, gk, g
                    ),
                )
            else:
                self._shard_groups[group_key].total = shard_total
        self._send_when_ready(role_name, task, project=project)
        _log_event(
            "assign",
            role=role_name,
            cwd=cwd,
            task_preview=task[:120],
            requires_commit=requires_commit,
            auto_chain=auto_chain,
            shard_total=shard_total,
        )
        return True, f"task queued for {role_name} (sending when ready)"

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
        body = header + _sanitize_pane_text(msg)
        _send_sess = pane.session
        body_payload = _paste_payload(body)
        _send_sess.write(body_payload)
        # Self-healing submit (issue #22): resend Enter if the peer message's
        # submit was swallowed mid-paste-render. Safe — a busy target isn't at
        # its ready prompt, so no resend fires into an in-flight turn.
        _delayed_enter_verified(
            pane,
            _send_sess,
            _enter_delay_ms(body_payload),
            payload=body_payload,
            content_fragment=body,
            on_resend=lambda rem, r=to_role: _log_event(
                "send_enter_resend", project=project_ns, role=r, remaining=rem
            ),
            on_repaste=lambda rem, r=to_role: _log_event(
                "send_repaste", project=project_ns, role=r, remaining=rem
            ),
        )

        # Record delivery time for stall detection: receiving a message counts
        # as evidence the pane is still being monitored by the orchestrator.
        self._ps(f"{project_ns}::{to_role}").last_send_ts = time.time()

        # CC Lead unless source was Lead and target was a teammate, or vice versa.
        # If Lead is not alive, queue the CC so it isn't silently lost — the
        # queue is flushed when Lead next spawns (see _flush_pending_lead_cc).
        if from_role and from_role not in (None, LEAD.name) and to_role != LEAD.name:
            lead = project_panes.get(LEAD.name)
            if lead and lead.session and lead.session.is_alive:
                self._notify_lead(project_ns, f"[CC] {body}")
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
            self._ps(f"{project_ns}::{from_norm}").blocked_on_lead_ts = time.time()
        elif from_norm == LEAD.name and to_role != LEAD.name:
            _ps_to = self._pane_state.get(f"{project_ns}::{to_role}")
            if _ps_to is not None:
                _ps_to.blocked_on_lead_ts = None
                # Lead sending new instructions counts as new real work —
                # give the Stop-hook done-gate a fresh one-shot budget.
                _ps_to.stop_gate_notified = False

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
        suppress_pipeline: bool = False,
        suppress_auto_chain: bool = False,
    ) -> tuple[bool, str]:
        """Terminate a pane's session and remove it from the layout.

        force=True is for legitimate cockpit lifecycle (tab close, project switch).
        Never expose to CLI — teammates can only call `takkub done`.

        suppress_pipeline=True skips the "pane closed without done → mark the
        pipeline role failed + advance" path. Used by the stuck-pane watchdog,
        which closes then *respawns* the same role 2 s later: without this guard a
        recovery-close on a single-role hop would empty hop_pending and spuriously
        advance/complete the whole pipeline before the recovered pane comes back
        (whose later done() would then be a no-op). The respawn path re-honors the
        failure only if the respawn itself fails.

        suppress_auto_chain=True skips the auto-chain handoff check. Used by the
        stuck-pane watchdog (close→respawn cycle) so a recovery-close never fires
        the verify-hop pre-authorisation prematurely. External / user-initiated
        closes (force=True, tab close) do NOT suppress so the #8 behaviour holds:
        if a user forcibly removes the last auto-chain pane the handoff still fires.
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
        # #8: read auto_chain flag BEFORE popping state so a pane that is
        # closed externally (e.g. forced close) still triggers the auto-chain
        # handoff if it was the last pending auto-chain pane in the project.
        _ps_close = getattr(self, "_pane_state", {}).get(key)
        had_auto_chain_close = _ps_close.auto_chain if _ps_close is not None else False
        had_pipeline_run_id_close = _ps_close.pipeline_run_id if _ps_close is not None else None

        self._idle_state.pop(key, None)
        getattr(self, "_pane_state", {}).pop(key, None)
        # Revoke the pane's capability token so stale done/send requests from
        # the closing pane are rejected after it terminates.
        _pane_tokens = getattr(self, "_pane_tokens", {})
        _revoke_keys = [t for t, v in _pane_tokens.items() if v == (project_ns, role_name)]
        for _tok in _revoke_keys:
            _pane_tokens.pop(_tok, None)

        if not suppress_auto_chain:
            self._maybe_fire_auto_chain_handoff(project_ns, had_auto_chain_close)

        # Pipeline: pane closed without done (crash / forced close) — mark failed.
        # Advance if all roles in the hop are now done or failed.
        # suppress_pipeline (stuck-watchdog recovery-close) skips this: the same
        # role respawns 2 s later, so a single-role hop must NOT advance here.
        if had_pipeline_run_id_close and not suppress_pipeline:
            pipeline_key_close = f"{project_ns}::{had_pipeline_run_id_close}"
            pl_run_close = self._pipeline_runs.get(pipeline_key_close)
            if pl_run_close and not pl_run_close.closed:
                pl_run_close.hop_pending.discard(role_name)
                pl_run_close.hop_failed.add(role_name)
                if not pl_run_close.hop_pending:
                    self._advance_pipeline(project_ns, pipeline_key_close, pl_run_close)

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
        # Fan-out queue (flag-gated): a genuine teammate close frees a slot —
        # drain one queued assign on the next event-loop tick (deferred so we
        # never re-enter the close/paneClosed emit stack, per the 0xc0000409
        # teardown-reentrancy lesson). Recovery-closes (suppress_pipeline) keep
        # the pane, so they don't free a slot and don't drain.
        if role_name != LEAD.name and not suppress_pipeline and _fanout_queue_enabled():
            QTimer.singleShot(0, lambda p=project_ns: self._drain_fanout_queue(p))
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
            f"Claude will substitute for the {provider} role (same slot, claude-backed); "
            "you may still propose/fire it — just note the substitution to the user."
            if disabled
            else f"{provider} CLI available again — it will back its role natively."
        )
        notice = f"[system] {provider} provider {word}. {suffix}"

        # Broadcast to every Lead pane across all project tabs. Iterate
        # _panes_by_project directly because we want every Lead, not just
        # the active project's Lead.
        for _project_ns, panes in self._panes_by_project.items():
            lead = panes.get(LEAD.name)
            if lead and lead.session and lead.session.is_alive:
                _tog_sess = lead.session
                _tog_sess.write(notice)
                # Same trailing-CR delay as done() so the inject lands
                # after the inline text not before it.
                _delayed_enter(lead, _tog_sess, 150)
                self.leadInjected.emit(notice)
            # If Lead isn't alive in this project, the next spawn's
            # _render_lead_context() will read the fresh state — no need
            # to queue per-message for this case (unlike done notices,
            # which carry per-event info that mustn't be lost).

        self.providerStateChanged.emit(provider, disabled)
        _log_event("provider_toggled", provider=provider, disabled=disabled)
        return True, f"{provider} {word.lower()}"

    def set_plan_tier(self, tier: str) -> tuple[bool, str]:
        """Set the account plan (pro/max) globally and persist it.

        Pins (or unpins) the Lead's model at the NEXT spawn: Pro forces a
        standard-context model so the 1M-context credit error can't bite,
        Max lets the Lead inherit the user default again. Already-running
        Lead panes keep their current model until respawn — we broadcast a
        `[system]` notice so the live session knows, and (under Pro) stops
        proposing 1M-context work.

        Returns (ok, message). Fails only on an unknown tier.
        """
        from . import plan_tier

        tier = tier.lower().strip()
        if tier not in plan_tier.TIERS:
            return False, f"unknown plan tier: {tier!r}"

        plan_tier.set_current(tier)

        if tier == plan_tier.PRO:
            notice = (
                "[system] account plan set to PRO. 1M-context model is "
                "unavailable (usage-credits gated) — do not propose or rely on "
                "it. New Lead panes pin to a standard-context model."
            )
        else:
            notice = (
                "[system] account plan set to MAX. Full model access restored "
                "(incl. 1M context). Applies to newly spawned panes."
            )

        # Broadcast to every Lead pane across all project tabs (same pattern
        # as toggle_provider). The model pin itself only lands at the next
        # spawn, but the notice keeps live sessions in sync.
        for _project_ns, panes in self._panes_by_project.items():
            lead = panes.get(LEAD.name)
            if lead and lead.session and lead.session.is_alive:
                _tier_sess = lead.session
                _tier_sess.write(notice)
                _delayed_enter(lead, _tier_sess, 150)
                self.leadInjected.emit(notice)

        self.planTierChanged.emit(tier)
        _log_event("plan_tier_set", tier=tier)
        return True, f"plan set to {tier}"

    def set_exec_mode(self, mode: str) -> tuple[bool, str]:
        """Set the execution mode (solo/parallel) globally and persist it.

        SOLO is the cockpit's original 1-agent-per-role behaviour. PARALLEL tells
        the Lead, on the NEXT task, to decompose an independent-multi-feature
        request and fan out several instances per role (frontend#1..#K, …) so the
        features finish concurrently. The instruction reaches the Lead via the
        system-prompt block in lead_context (read at spawn); we also broadcast a
        `[system]` notice so a live Lead switches planning style immediately.

        Returns (ok, message). Fails only on an unknown mode.
        """
        from . import exec_mode

        mode = mode.lower().strip()
        if mode not in exec_mode.MODES:
            return False, f"unknown execution mode: {mode!r}"

        exec_mode.set_current(mode)

        if mode == exec_mode.PARALLEL:
            cap = exec_mode.machine_fanout_cap()
            notice = (
                "[system] execution mode → PARALLEL (multi). When a request has "
                "K independent features, plan a decomposition and fan out one "
                f"instance per role per feature (frontend#1..#K, backend#1..#K). "
                f"Cap K at {cap} for this machine (CPU/RAM) — split bigger batches "
                "into waves. Independent features only; keep dependent work serial."
            )
        else:
            notice = (
                "[system] execution mode → SOLO (1:1). One agent per role; work "
                "features sequentially. (No multi-instance fan-out.)"
            )

        for _project_ns, panes in self._panes_by_project.items():
            lead = panes.get(LEAD.name)
            if lead and lead.session and lead.session.is_alive:
                _em_sess = lead.session
                _em_sess.write(notice)
                _delayed_enter(lead, _em_sess, 150)
                self.leadInjected.emit(notice)

        self.execModeChanged.emit(mode)
        _log_event("exec_mode_set", mode=mode)
        return True, f"execution mode set to {mode}"

    @staticmethod
    def _uncommitted_warning(from_role: str, porcelain_out: str) -> str | None:
        """Build the Lead `[requires-commit]` warning from `git status --porcelain`
        stdout, or None when the tree is clean. Pure → unit-tested. (M2)"""
        dirty = (porcelain_out or "").strip()
        if not dirty:
            return None
        files_preview = dirty[:200]
        return (
            f"⚠ [requires-commit] {from_role} มี uncommitted changes รอ Lead review + commit:\n"
            f"{files_preview}"
        )

    def _check_uncommitted_async(self, project_ns: str, from_role: str, cwd: str) -> None:
        """Run `git status --porcelain` WITHOUT blocking the Qt main thread, then
        deliver a follow-up warning to Lead if the tree is dirty. (M2)

        Uses QProcess (driven by the Qt event loop) rather than a worker thread,
        so the completion handler runs on the main thread exactly like any slot —
        there is NO cross-thread access to orchestrator / pane state, hence no
        race. A watchdog timer bounds a hung git the way the old timeout=10 did.
        """
        proc = QProcess(self)
        proc.setWorkingDirectory(cwd)
        timeout = QTimer(self)
        timeout.setSingleShot(True)
        timeout.setInterval(10_000)
        state = {"done": False}

        def _settle(reason: str | None) -> None:
            # reason is None on a clean finish; a string when we bailed (skip warn).
            if state["done"]:
                return
            state["done"] = True
            timeout.stop()
            # Don't leak the watchdog QTimer (parented to self → would live for the
            # whole cockpit run, accumulating one per requires-commit done).
            timeout.deleteLater()
            if reason is not None:
                _log_event(
                    "done_commit_gate_skipped", role=from_role, project=project_ns, reason=reason
                )
                try:
                    proc.kill()
                except Exception:
                    pass
                proc.deleteLater()
                return
            try:
                out = bytes(proc.readAllStandardOutput()).decode("utf-8", "replace")
            except Exception:
                out = ""
            proc.deleteLater()
            warning = self._uncommitted_warning(from_role, out)
            if warning is None:
                return
            _log_event(
                "done_with_uncommitted",
                role=from_role,
                project=project_ns,
                reason="dirty_tree",
                files=warning[-200:],
            )
            self._notify_lead(project_ns, warning, from_role=from_role, note="")

        proc.finished.connect(lambda _code, _status: _settle(None))
        proc.errorOccurred.connect(lambda _e: _settle("git_proc_error"))
        timeout.timeout.connect(lambda: _settle("timeout"))
        timeout.start()
        proc.start("git", ["status", "--porcelain"])

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

        # Read state before teardown so fields are available after the pop.
        _ps_done = getattr(self, "_pane_state", {}).get(key) or PaneState()
        had_requires_commit = _ps_done.requires_commit_on_done
        had_auto_chain = _ps_done.auto_chain
        had_shard_total = _ps_done.shard_total
        had_pipeline_run_id = _ps_done.pipeline_run_id
        had_plan_fanout = _ps_done.plan_fanout

        # Opt-in commit handoff: if assign() was called with requires_commit=True,
        # check for a dirty working tree and warn Lead (the agent isn't blocked —
        # Lead reviews + commits). M2: the check runs ASYNC via QProcess so a slow
        # or large repo can't freeze the Qt main thread for up to the git timeout.
        # The main done notice below goes out immediately; if the tree turns out
        # dirty, a follow-up `[requires-commit]` warning is delivered to Lead.
        if had_requires_commit:
            spawn_cwd = getattr(pane, "_session_cwd", None) or str(REPO_ROOT)
            self._check_uncommitted_async(project_ns, from_role, spawn_cwd)

        # Agent finished cleanly — pop all per-pane state atomically.
        # close() (scheduled 2.5 s below) will also pop; second pop is a no-op.
        self._idle_state.pop(key, None)
        getattr(self, "_pane_state", {}).pop(key, None)

        # notify Lead in the same project (a teammate in unirecon mustn't
        # nudge the Lead in pms by mistake)
        notice = f"[{from_role} done] {note}".rstrip()
        # Shard panes: suppress per-shard notice to Lead — consolidated handoff
        # (_inject_shard_fanout_handoff) is the single message Lead sees.
        # Planner panes: suppress too — the "[qa plan ready] fan-out …" message
        # from _fire_qa_plan_fanout is the meaningful one Lead acts on.
        # Non-shard, non-planner panes use the normal notice path.
        if had_shard_total == 0 and not had_plan_fanout:
            # Route through _notify_lead so concurrent done notices are serialised
            # and never injected while Lead is mid-generation (the root cause of the
            # "Lead goes silent after parallel dispatch" bug).
            self._notify_lead(project_ns, notice, from_role=from_role, note=note)

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
        self._maybe_fire_auto_chain_handoff(project_ns, had_auto_chain)

        # Plan-then-fan-out: this pane was a planner (--plan). Read the bucket
        # plan it just wrote and spawn the QA shards (each with its bucket).
        if had_plan_fanout:
            base_role_p, _ = _split_shard(from_role)
            self._fire_qa_plan_fanout(project_ns, base_role_p, had_plan_fanout, planner_note=note)

        # Shard aggregate: record this shard's note and check if all N done.
        if had_shard_total > 0:
            base_role_d, _ = _split_shard(from_role)
            group_key = f"{project_ns}::{base_role_d}"
            group = self._shard_groups.get(group_key)
            if group and not group.closed:
                group.done[from_role] = note
                if len(group.done) + len(group.failed) >= group.total:
                    group.closed = True
                    self._inject_shard_fanout_handoff(project_ns, group)
                    self._shard_groups.pop(group_key, None)
            else:
                # #3: group already closed (timeout) or popped — shard arrived
                # late.  Send a notice so Lead knows instead of silently dropping.
                late_msg = (
                    f"⚠️ [shard late-complete] {from_role} reported done after its "
                    f"shard group already closed (timeout or all-failed). "
                    f"note: {note!r:.120}"
                )
                self._notify_lead(project_ns, late_msg, from_role=from_role, note="late-complete")
                _log_event(
                    "shard_late_complete",
                    project=project_ns,
                    role=from_role,
                    note=note[:200],
                )

        # Pipeline hop advance: if this pane was part of a pipeline run, remove
        # it from the hop's pending set and fire the next hop when all done.
        if had_pipeline_run_id:
            pipeline_key = f"{project_ns}::{had_pipeline_run_id}"
            pl_run = self._pipeline_runs.get(pipeline_key)
            if pl_run and not pl_run.closed:
                pl_run.hop_pending.discard(from_role)
                if not pl_run.hop_pending:
                    self._advance_pipeline(project_ns, pipeline_key, pl_run)

        # mark pane done, auto-close after a delay so user can see it.
        # Capture current session so the delayed close is a no-op if the pane
        # has already been respawned with a new session by the time the timer fires.
        pane.set_state("done", note=note[:80] if note else "done")
        _done_sess = pane.session

        def _close_if_same_session() -> None:
            _pp = self._project_panes(project_ns).get(from_role)
            if _pp is not None and _pp.session is _done_sess:
                self.close(from_role, project=project_ns)

        QTimer.singleShot(2_500, _close_if_same_session)
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
        self.agentDone.emit(project_ns, from_role, note)
        return True, f"{from_role} reported done"

    def consume_pane_hook(
        self,
        from_role: str,
        project: str | None = None,
        event: str = "",
        notification_type: str = "",
    ) -> tuple[bool, bool, str]:
        """Consume a Claude Code hook signal (Stop / Notification) from a
        claude-backed pane's `takkub _hook`, as an authoritative turn-end/idle
        marker, and decide the Stop-hook done-gate.

        Returns ``(ok, block, reason)``. ``block=True`` tells the caller to
        emit a Stop-hook block decision nudging the pane to run `takkub done`.

        Idle-state signal: reuses the exact idempotent pattern the PTY-scraping
        watchdog (`_check_idle_teammates`) already uses — `first_idle_ts` is
        only set the first time it's seen `None`, so a hook firing milliseconds
        before/after the next poll tick is a no-op, not a double-count. PTY
        scraping stays the fallback/ground truth (non-claude panes, or a claude
        pane whose hook never fires) and self-corrects any staleness on its own
        next tick (`is_at_ready_prompt()` returning False resets first_idle_ts).

        Done-gate: one-shot per assignment via `PaneState.stop_gate_notified` —
        `stop_hook_active` alone only guards against Claude Code recursively
        re-entering the SAME Stop event, not a fresh Stop event a few seconds
        later if the model ignores the nudge and stops again (would otherwise
        block forever). Also honours the same suppressions the idle watchdog
        does: not blocked-on-lead, not rate-limited, not TTY-prompt-blocked, and
        only while the pane is live and `working` with an outstanding assigned
        task (see docs/reviews/2026-07-02-claude-hooks-design-crosscheck.md).
        """
        try:
            from_role = validate_name(from_role, "role")
        except ValueError:
            return False, False, "invalid role"
        project_ns = self._resolve_project(project)
        key = f"{project_ns}::{from_role}"

        if from_role != LEAD.name and event in ("Stop", "Notification"):
            entry = self._idle_state.setdefault(
                key, {"first_idle_ts": None, "last_reminder_ts": 0.0}
            )
            if entry["first_idle_ts"] is None:
                entry["first_idle_ts"] = time.time()

        # Lead never gets the done-gate (it never calls `done` on itself);
        # the gate only applies to a turn actually ending (Stop).
        if from_role == LEAD.name or event != "Stop":
            return True, False, ""

        pane = self._project_panes(project_ns).get(from_role)
        if pane is None or pane.session is None or not pane.session.is_alive:
            return True, False, ""
        if pane.state != "working":
            return True, False, ""

        ps = getattr(self, "_pane_state", {}).get(key)
        if ps is None or not ps.last_assigned_task or ps.stop_gate_notified:
            return True, False, ""

        now = time.time()
        # Same 30-minute window _check_idle_teammates uses to suppress the
        # forgot-done reminder while genuinely waiting on Lead's reply.
        if ps.blocked_on_lead_ts is not None and (now - ps.blocked_on_lead_ts) < 30 * 60:
            return True, False, ""
        if ps.rate_limited_until > now:
            return True, False, ""
        if ps.tty_blocked_since is not None:
            return True, False, ""

        ps.stop_gate_notified = True
        return True, True, "รายงานผลด้วย takkub done ก่อนจบ"

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
        if _is_dedup_note(project, role, note):
            return
        if now is None:
            now = datetime.now()
        body = _render_decision_note(project, role, note, now, transcript_path=transcript_path)
        try:
            safe_project = validate_name(project, "project")
        except ValueError:
            import logging

            logging.getLogger(__name__).warning(
                "_save_decision_note: rejected unsafe project name %r", project
            )
            return
        try:
            day = RUNTIME_DIR / "sessions" / now.strftime("%Y-%m-%d") / safe_project
            day.mkdir(parents=True, exist_ok=True)
            path = day / f"{role}-{now.strftime('%H%M%S')}.md"
            path.write_text(body, encoding="utf-8")
        except OSError:
            pass

        vault = _resolve_vault_dir()
        if vault is None:
            return
        try:
            sessions = vault / "99-Logs" / "sessions" / safe_project
            sessions.mkdir(parents=True, exist_ok=True)
            stamp = now.strftime("%Y-%m-%dT%H%M%S")
            (sessions / f"{stamp}-{role}.md").write_text(body, encoding="utf-8")
        except OSError:
            pass

        # Phase B: distill durable facts into 01-Projects/<project>.md (best-effort)
        distill_session_facts(project, role, note, vault, now=now)

    def end_session(self, project: str | None = None, note: str = "") -> tuple[bool, str]:
        """Write a Lead session summary to runtime/sessions and the vault mirror.

        Called via `takkub end-session [--note '...']` from the Lead pane.
        Never closes any pane — Lead stays open, teammates continue as-is.
        """
        project_ns = self._resolve_project(project)
        try:
            project_ns = validate_name(project_ns, "project")
        except ValueError:
            import logging

            logging.getLogger(__name__).warning(
                "end_session: rejected unsafe project name %r", project_ns
            )
            return False, f"unsafe project name rejected: {project_ns!r}"
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
        # project_ns is already validated above so safe to use directly.
        vault = _resolve_vault_dir()
        if vault is not None:
            try:
                vault_sessions = vault / "99-Logs" / "sessions" / project_ns
                vault_sessions.mkdir(parents=True, exist_ok=True)
                stamp = now.strftime("%Y-%m-%dT%H%M%S")
                (vault_sessions / f"{stamp}-lead.md").write_text(body, encoding="utf-8")
            except OSError:
                pass

            # Phase B: distill durable facts from the session note (best-effort)
            distill_session_facts(project_ns, "lead", note, vault, now=now)

        # Append today's Finish-Job digest to the vault's 05-Daily note.
        # Best-effort: the local session summary above is the contract, so a
        # digest failure (vault glitch, chatlog scan error) must never fail
        # end_session. write_daily_digest already no-ops when no vault is
        # configured and swallows its own IO errors; the try/except here is a
        # belt-and-braces guard against any unexpected raise.
        try:
            self.write_daily_digest(project_ns)
        except Exception:
            import logging

            logging.getLogger(__name__).debug(
                "end_session: write_daily_digest failed (non-fatal)", exc_info=True
            )

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

        if _split_shard(role)[0] in ("qa", "critic", "designer"):
            today = datetime.now().strftime("%Y-%m-%d")
            shot_dir = RUNTIME_DIR / "exports" / today / project_ns / "screenshots"
            try:
                mt = shot_dir.stat().st_mtime
                if mt > ts:
                    ts = mt
            except OSError:
                pass

        send_ts = (
            getattr(self, "_pane_state", {}).get(f"{project_ns}::{role}") or PaneState()
        ).last_send_ts
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
                        raw = _read_tail_bytes(
                            pathlib.Path(transcript_path), _TRANSCRIPT_TAIL_BYTES
                        )
                        lines = raw.decode("utf-8", errors="replace").splitlines()
                        tail_lines = [ln for ln in lines if ln.strip()][-5:]
                        tail_lines = [_ANSI.sub("", ln) for ln in tail_lines]
                        transcript_tail = "\n".join(tail_lines)
                    except OSError:
                        pass

            last_screenshot = ""
            if _split_shard(role)[0] in ("qa", "critic", "designer"):
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
                # #9: persist last_task + session_uuid so restore_teammates
                # can re-paste the task and (optionally) resume the session.
                ps_snap = getattr(self, "_pane_state", {}).get(_exit_key(project, role))
                entries.append(
                    {
                        "role": role,
                        "cwd": pane._session_cwd or "",
                        "state": pane.state,
                        "last_task": ((ps_snap.last_assigned_task if ps_snap else None) or ""),
                        "session_uuid": ((ps_snap.session_uuid if ps_snap else None) or ""),
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

        The ``exit_ts`` field is stamped for crash-recovery bookkeeping,
        but since ``session_uuid`` has no value for these roles yet, each
        spawn here generates a fresh ``--session-id`` (no bleed from a prior
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
                last_task = (entry or {}).get("last_task") or ""
                if not role:
                    continue
                # Stamp recent-exit for crash-recovery bookkeeping.
                # session_uuid has no value yet for these roles, so
                # spawn() will issue --session-id (fresh session, no bleed).
                self._recent_exits[_exit_key(project, role)] = {"cwd": cwd, "ts": time.time()}
                ok, _ = self.spawn(role, cwd=cwd, project=project)
                if ok:
                    scheduled += 1
                    # #9: re-paste the last task so the pane continues working;
                    # queue a Lead notice (delivered when Lead spawns) either
                    # way so the operator knows the pane was re-spawned.
                    if last_task:
                        self._send_when_ready(role, last_task, project=project)
                        notice_body = (
                            f"[cockpit restart] {role} pane restored from last session "
                            f"and last task re-sent automatically."
                        )
                    else:
                        notice_body = (
                            f"⚠️ [cockpit restart] {role} pane restored from last session "
                            f"but last task was not saved — pane started fresh. "
                            f"Re-assign if needed."
                        )
                    self._pending_done_notices.setdefault(project, []).append(
                        {"role": role, "note": "restore", "body": notice_body}
                    )
                    self._save_pending_done_notices(project)
                    _log_event(
                        "teammate_restored",
                        role=role,
                        project=project,
                        has_task=bool(last_task),
                    )
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
        briefs_dir = vault / "99-Logs" / "briefs"
        # Cap the scan window so a long-dormant project doesn't drag
        # months of jsonls into the brief — last 24 h is plenty for
        # "where did we leave off."
        from datetime import timedelta

        since = now - timedelta(hours=24)
        written = 0
        for project in self._panes_by_project.keys():
            try:
                safe_project = validate_name(project, "project")
            except ValueError:
                import logging

                logging.getLogger(__name__).warning(
                    "write_resume_briefs: rejected unsafe project name %r", project
                )
                continue
            body = build_resume_brief(project_filter=safe_project, since=since)
            if not body:
                continue
            try:
                briefs_dir.mkdir(parents=True, exist_ok=True)
                (briefs_dir / f"{safe_project}-{stamp}.md").write_text(body, encoding="utf-8")
                written += 1
            except OSError:
                continue
        # Prune stale log files and ensure graph filter is set — best-effort.
        try:
            prune_vault_logs(vault)
        except Exception:
            pass
        try:
            write_obsidian_graph_filter(vault)
        except Exception:
            pass
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
        # Snapshot live state on the main thread (cheap) — the heavy session-file
        # scan + render + write run off-thread so a large chatlog or slow vault
        # never blocks the Qt event loop (was a proven main_thread_stall source:
        # _write_hot_md → scan_hot_md_metrics → stat over every session file,
        # fired on EVERY `done` event + the 60 s timer).
        snapshot = {
            project: {role: pane.state for role, pane in panes.items()}
            for project, panes in self._panes_by_project.items()
        }
        try:
            active_name, _ = active_project()
        except Exception:
            active_name = None
        recent = list(self._recent_done)
        now = datetime.now()
        # Coalesce bursts: if the previous off-thread write is still running,
        # skip this tick — the next one picks up fresh state. Prevents a thread
        # pile-up when `done` events arrive back-to-back.
        if getattr(self, "_hot_md_writing", False):
            return
        self._hot_md_writing = True

        def _hot_md_worker() -> None:
            try:
                # Hook noise meter + friction heatmap — single pass over today's
                # Claude Code session jsonl files. Per-file (mtime, size) cache in
                # scan_hot_md_metrics avoids re-parsing unchanged files.
                try:
                    from .chatlog_scanner import scan_hot_md_metrics

                    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    hook_counts, _corrections, _tool_retries = scan_hot_md_metrics(
                        since=start_of_today
                    )
                    friction = {"corrections": _corrections, "tool_retries": _tool_retries}
                except Exception:
                    hook_counts = {}
                    friction = {}
                body = _render_hot_md(
                    snapshot,
                    active_name,
                    recent,
                    now,
                    hook_counts=hook_counts,
                    friction=friction,
                )
                try:
                    (vault / "hot.md").write_text(body, encoding="utf-8")
                except OSError:
                    pass
            finally:
                self._hot_md_writing = False

        threading.Thread(target=_hot_md_worker, daemon=True, name="hot-md-writer").start()

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
        # Flush durable done-notices for any project whose Lead is now idle.
        # Handles the case where notices spilled to _pending_done_notices while
        # Lead was busy — delivers them without requiring a Lead restart.
        self._reap_pending_done_notices()
        # Surface panes whose idle prompt no marker recognises (structural #20
        # staleness detector) — makes an upstream-reword silent break LOUD.
        self._check_stale_markers(now)
        for project_name, project_panes in list(self._panes_by_project.items()):
            for name, pane in list(project_panes.items()):
                try:
                    key = f"{project_name}::{name}"
                    if name == LEAD.name:
                        # Issue #59: malformed-tool-call detection covers Lead too even
                        # though Lead is exempt from the idle-done-reminder loop.
                        if (
                            pane.session
                            and pane.session.is_alive
                            and pane.session.is_at_ready_prompt()
                        ):
                            self._maybe_surface_malformed_xml(key, name, project_name, pane, now)
                        continue
                    if pane.state != "working":
                        self._idle_state.pop(key, None)
                        continue
                    if pane.session is None or not pane.session.is_alive:
                        self._idle_state.pop(key, None)
                        continue

                    # Stuck-paste reaper — MUST run before every suppression
                    # gate below (rate-limit, blocked-on-lead, tty-block). A
                    # swallowed task submit has to be recovered even when a
                    # gate silences the idle reminder: the 2026-07-02 QA
                    # fan-out incident was exactly this — a false rate-limit
                    # flag (Fable-5 promo text) suppressed the reminder whose
                    # trailing Enter used to rescue stuck pastes by accident,
                    # so panes sat on "[Pasted text +N lines]" for hours.
                    self._maybe_submit_stuck_paste(key, name, project_name, pane, now)

                    # Suppress the reminder while this pane is rate-limited: it's
                    # not idle-because-done, it physically can't work until the
                    # usage limit resets. Detection happens here (every tick) and
                    # schedules a one-shot reset notice the first time it's seen.
                    if self._rate_limit_suppressed(project_name, name, pane, now):
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
                    _ps_bl = getattr(self, "_pane_state", {}).get(key)
                    blocked_at = _ps_bl.blocked_on_lead_ts if _ps_bl is not None else None
                    if blocked_at is not None and (now - blocked_at) < 30 * 60:
                        entry = self._idle_state.get(key)
                        if entry:
                            entry["first_idle_ts"] = None
                        continue

                    entry = self._idle_state.setdefault(
                        key, {"first_idle_ts": None, "last_reminder_ts": 0.0}
                    )

                    # Issue #54: suppress the forgot-done reminder while a pane is
                    # blocked on an interactive subprocess prompt (y/N, passphrase,
                    # "press any key"). The idle reminder is the wrong context here
                    # and close→respawn (stuck recover) won't help — the prompt comes
                    # from the subprocess. Surface a separate notice to Lead instead.
                    _tty_prompt = pane.session.is_blocked_on_tty_prompt()
                    if _tty_prompt:
                        entry["first_idle_ts"] = None
                        self._maybe_surface_tty_block(key, name, project_name, _tty_prompt, now)
                        continue
                    # Clear block state when no longer blocked.
                    _ps_tty = getattr(self, "_pane_state", {}).get(key)
                    if _ps_tty is not None and _ps_tty.tty_blocked_since is not None:
                        _ps_tty.tty_blocked_since = None

                    if not pane.session.is_at_ready_prompt():
                        # claude is processing — reset the idle streak so a long
                        # build doesn't count toward the reminder threshold.
                        entry["first_idle_ts"] = None
                        continue

                    # Issue #59: pane is idle — check for malformed tool-call XML
                    # that the harness silently no-op'd (makes pane look hung).
                    # Defense-in-depth: this best-effort nudge sits in front of the
                    # critical forgot-`takkub done` reminder below. A bug in the
                    # detector (e.g. the pyte empty-stub IndexError, now fixed at
                    # source) must never starve the reminder — otherwise a teammate
                    # that forgot to report sits idle until the user closes it,
                    # never reaching Lead. Isolate it so the reminder always runs.
                    try:
                        self._maybe_surface_malformed_xml(key, name, project_name, pane, now)
                    except Exception as _mx_err:
                        _log_event(
                            "malformed_xml_check_error",
                            role=name,
                            project=project_name,
                            err=f"{type(_mx_err).__name__}: {_mx_err}",
                        )

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
                        _ps_hh = getattr(self, "_pane_state", {}).get(key)
                        last_hint = _ps_hh.harvest_hint_ts if _ps_hh is not None else 0.0
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
                                self._ps(key).harvest_hint_ts = now
                except Exception as e:
                    # This block runs every 5s per pane; a persistent fault used
                    # to re-log a bare role/project with NO exception detail on
                    # every tick (3279 blind entries in one events.log — zero
                    # diagnostic value). Capture the exception type+message and
                    # rate-limit: log only when the error changes or after a
                    # 5-min cooldown per pane, so the real cause surfaces once
                    # instead of flooding the log.
                    err = f"{type(e).__name__}: {e}"
                    last = self._idle_err_last.get(key)
                    if last is None or last[0] != err or (now - last[1]) >= 300:
                        _log_event(
                            "idle_watchdog_pane_error",
                            role=name,
                            project=project_name,
                            err=err,
                        )
                        self._idle_err_last[key] = (err, now)

    def _check_stale_markers(self, now: float) -> None:
        """Surface a pane whose idle prompt no state marker recognises — the
        silent-break signature of #20 (an upstream CLI reworded its prompt so
        is_at_ready_prompt no longer matches).

        Structural gate: only consider a pane that has been output-QUIET for
        STALE_MARKER_QUIET_S. A generating CLI streams continuously, so a long
        silence means the pane is genuinely settled, not mid-generation — a
        signal independent of the (fragile) text markers. If a settled pane is
        recognised by NO marker (not ready, not a known tty/trust/splash
        prompt), detection has gone blind; log it (rate-limited per pane) with
        the bottom screen text so the operator sees the real footer and can
        rescue it via TAKKUB_EXTRA_READY_MARKERS — a loud diagnostic instead of
        a silent idle-watchdog stall.
        """
        for project_name, project_panes in list(self._panes_by_project.items()):
            for name, pane in list(project_panes.items()):
                try:
                    sess = pane.session
                    if sess is None or not sess.is_alive:
                        continue
                    if sess.seconds_since_output() < STALE_MARKER_QUIET_S:
                        continue  # still streaming → genuinely busy, not blind
                    if sess.is_at_ready_prompt():
                        continue  # recognised idle → markers working
                    if sess.is_blocked_on_tty_prompt() or sess.is_at_trust_prompt():
                        continue  # recognised shell/trust prompt
                    if sess.is_at_update_splash():
                        continue  # recognised codex splash (handled elsewhere)
                    # Alive + settled + unrecognised → markers likely stale.
                    key = f"{project_name}::{name}"
                    last = self._stale_marker_last.get(key)
                    if last is not None and (now - last) < STALE_MARKER_COOLDOWN_S:
                        continue
                    tail = " | ".join(
                        ln.strip()
                        for ln in sess.display_lines()[-STALE_MARKER_TAIL_ROWS:]
                        if ln.strip()
                    )[:300]
                    _log_event(
                        "ready_marker_possibly_stale",
                        role=name,
                        project=project_name,
                        quiet_s=round(sess.seconds_since_output()),
                        footer=tail,
                    )
                    self._stale_marker_last[key] = now
                except Exception:
                    continue

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
                try:
                    if role == LEAD.name:
                        continue
                    if pane.state != "working":
                        continue
                    if pane.session is None or not pane.session.is_alive:
                        continue
                    # A rate-limited pane is silent on purpose — never force-respawn
                    # it (the fresh session would just hit the same limit). The idle
                    # walker owns detection; here we only read the recorded state.
                    key = f"{project_name}::{role}"
                    if (self._pane_state.get(key) or PaneState()).rate_limited_until > now:
                        continue
                    last_out = getattr(pane, "_last_output_ts", 0.0)
                    if not isinstance(last_out, (int, float)) or last_out <= 0:
                        # Pane hasn't seen output yet — still in bootstrap,
                        # or the attribute was never initialised (legacy
                        # AgentPane subclass / test fixture). Skip; the next
                        # tick will pick it up once a real timestamp lands.
                        continue
                    # Bug-2 fix: measure screen-content delta excluding the spinner
                    # region ('esc to interrupt').  Raw byte timestamps are bumped on
                    # every PTY byte including the animated spinner, so a claude
                    # wedged on a slow MCP call never trips STUCK_THRESHOLD_S with
                    # the old byte-only check.  Content-delta is immune to spinners.
                    ps_ck = self._ps(key)
                    try:
                        disp = pane.session.display_lines()
                        # Fix 3: filter spinner/status lines more broadly.
                        # Exclude lines matching any known interrupt phrase OR volatile
                        # counter patterns (elapsed seconds, token counters) so a
                        # counter-only spinner line doesn't keep resetting the hash.
                        _filtered_lines = "\n".join(
                            ln
                            for ln in disp
                            if not any(p in ln.lower() for p in _SPINNER_INTERRUPT_PHRASES)
                            and not _SPINNER_VOLATILE_RE.search(ln)
                        )
                        non_spinner_hash = hashlib.blake2b(
                            _filtered_lines.encode("utf-8", errors="replace"),
                            digest_size=8,
                        ).hexdigest()
                        prev_hash = ps_ck.last_content_hash
                        if prev_hash != non_spinner_hash:
                            ps_ck.last_content_hash = non_spinner_hash
                            if prev_hash is not None:
                                # Genuine content change (not first observation) →
                                # reset the change clock so the pane isn't recovered.
                                ps_ck.last_content_change_ts = now
                            elif ps_ck.last_content_change_ts is None:
                                # First time we see this pane: initialise from
                                # last_out so an already-stale pane is detected on
                                # the very first tick rather than getting a free
                                # STUCK_THRESHOLD_S grace period.
                                ps_ck.last_content_change_ts = last_out
                    except Exception:
                        # display_lines() failed (session torn down mid-tick); fall
                        # back to initialising the ts from last raw byte time.
                        if ps_ck.last_content_change_ts is None:
                            ps_ck.last_content_change_ts = last_out
                    last_content_ts = ps_ck.last_content_change_ts
                    if last_content_ts is None:
                        last_content_ts = last_out
                    # Throughput watchdog (issue #35): detect runaway output loops
                    # that flood the Qt main thread. The existing stuck detector
                    # only catches *silent* or *content-stable* panes; a pane in a
                    # runaway loop has ever-changing content and is never "stuck" by
                    # the existing metric. Here we measure byte rate and warn Lead.
                    _tp_total = getattr(pane, "_tp_total_bytes", 0)
                    if ps_ck.tp_last_ts > 0:
                        _tp_elapsed = now - ps_ck.tp_last_ts
                        if _tp_elapsed > 0:
                            _tp_delta = _tp_total - ps_ck.tp_last_total
                            _tp_rate = _tp_delta / _tp_elapsed
                            if _tp_rate > RUNAWAY_BYTES_S:
                                if ps_ck.tp_runaway_since is None:
                                    ps_ck.tp_runaway_since = now
                                elif (now - ps_ck.tp_runaway_since) >= RUNAWAY_DURATION_S:
                                    if (now - ps_ck.tp_warn_ts) >= RUNAWAY_WARN_COOLDOWN_S:
                                        self._warn_lead_runaway_pane(role, project_name, _tp_rate)
                                        ps_ck.tp_warn_ts = now
                            else:
                                ps_ck.tp_runaway_since = None
                    ps_ck.tp_last_total = _tp_total
                    ps_ck.tp_last_ts = now
                    if (now - last_content_ts) < STUCK_THRESHOLD_S:
                        continue
                    if ps_ck.stuck_recover_gave_up:
                        # Already hit STUCK_RECOVER_MAX for this pane and handed it
                        # to the operator (#41). Stop recovering — re-recovering a
                        # deterministically-wedged pane just loops + burns tokens.
                        continue
                    last_recover = ps_ck.last_stuck_recover
                    if (now - last_recover) < STUCK_RECOVER_COOLDOWN_S:
                        # Already tried to recover this pane recently;
                        # leave it alone so we don't loop close→spawn.
                        continue
                    if ps_ck.stuck_recover_attempts >= STUCK_RECOVER_MAX:
                        # Recovered MAX times and it wedged again — giving up beats
                        # an infinite close→respawn loop that stalls the pipeline (#41).
                        self._give_up_stuck(role, project_name, pane, now)
                        continue
                    # Issue #54: if the pane is blocked on a TTY prompt, close→respawn
                    # won't help (the prompt comes from a subprocess). Defer recovery
                    # and surface to Lead instead.
                    # Note: we're already past STUCK_THRESHOLD_S here, so we skip the
                    # TTY_BLOCK_SURFACE_AFTER_S grace period and surface immediately
                    # (only the repeat-spam cooldown applies).
                    try:
                        _tty_stuck = pane.session.is_blocked_on_tty_prompt()
                    except Exception:
                        _tty_stuck = None
                    if _tty_stuck:
                        _ps_tty = self._ps(key)
                        if _ps_tty.tty_blocked_since is None:
                            _ps_tty.tty_blocked_since = now
                        if (
                            now - _ps_tty.last_tty_block_surface_ts
                        ) >= TTY_BLOCK_SURFACE_COOLDOWN_S:
                            self._surface_tty_block_notice(role, project_name, _tty_stuck)
                            _ps_tty.last_tty_block_surface_ts = now
                        continue
                    # Issue #62: codex 'update available!' splash blocks ready-prompt.
                    # Send Enter once to dismiss; if it doesn't clear within
                    # SPLASH_DISMISS_COOLDOWN_S fall back to close→respawn.
                    try:
                        _at_splash = pane.session.is_at_update_splash()
                    except Exception:
                        _at_splash = False
                    if _at_splash:
                        _ps_sp = self._ps(key)
                        if _ps_sp.splash_dismiss_ts == 0.0:
                            pane.session.write(b"\r")
                            _log_event(
                                "pane_recovered_update_splash",
                                role=role,
                                project=project_name,
                            )
                            _ps_sp.splash_dismiss_ts = now
                            continue
                        if (now - _ps_sp.splash_dismiss_ts) < SPLASH_DISMISS_COOLDOWN_S:
                            continue
                        # Dismiss didn't clear the splash — fall through to close→respawn
                    self._auto_recover_stuck(role, project_name, pane, now)
                except Exception:
                    _log_event("stuck_watchdog_pane_error", role=role, project=project_name)

    def _auto_recover_stuck(self, role: str, project: str, pane: AgentPane, now: float) -> None:
        """Close the wedged pane and respawn it with --resume <uuid>. The
        spawn uses the pane's last-known cwd so claude rejoins the same
        project directory.

        Bug-1 fix: close() pops session UUID, last-task, auto-chain flag and
        requires-commit gate — without a snapshot/restore the respawned session
        starts blank (no --resume despite the docstring), drops the verify hop
        from auto-chain, and silently loses the commit gate.  We snapshot those
        four fields before teardown and restore them in the respawn callback so
        spawn() can_resume logic finds the UUID and the task/flags survive."""
        cwd = pane._session_cwd
        key = f"{project}::{role}"

        # Snapshot fields that close() will pop so _do_respawn can restore them.
        _ps_snap = self._pane_state.get(key)
        snap_uuid = _ps_snap.session_uuid if _ps_snap is not None else None
        snap_uuid_cwd = _ps_snap.session_uuid_cwd if _ps_snap is not None else ""
        snap_task = _ps_snap.last_assigned_task if _ps_snap is not None else None
        snap_auto_chain = _ps_snap.auto_chain if _ps_snap is not None else False
        snap_requires_commit = _ps_snap.requires_commit_on_done if _ps_snap is not None else False
        snap_shard_total = _ps_snap.shard_total if _ps_snap is not None else 0
        snap_pipeline_run_id = _ps_snap.pipeline_run_id if _ps_snap is not None else None
        # #41: carry the stuck-recover attempt count across the close→respawn so
        # the watchdog can enforce STUCK_RECOVER_MAX (close() pops the PaneState).
        snap_recover_attempts = _ps_snap.stuck_recover_attempts if _ps_snap is not None else 0

        self._ps(key).last_stuck_recover = now
        # silent_for_s = raw-byte silence. It is frequently 0 even on a genuine
        # recover because the animated spinner ("esc to interrupt") keeps
        # emitting bytes — so on its own it does NOT explain why the watchdog
        # fired and reads as a false alarm. The actual trigger is
        # content_static_s: how long the spinner-filtered screen content stayed
        # byte-for-byte identical (>= STUCK_THRESHOLD_S is what trips recovery).
        # Log both so the recover reason is unambiguous in events.log.
        silent_for_s = int(now - getattr(pane, "_last_output_ts", now))
        _content_ts = _ps_snap.last_content_change_ts if _ps_snap is not None else None
        content_static_s = int(now - _content_ts) if _content_ts is not None else -1
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
            content_static_s=content_static_s,
        )
        # suppress_pipeline + suppress_auto_chain: this close is the first half of a
        # close→respawn recovery, not a real pane death.  Neither the pipeline hop
        # nor the auto-chain handoff should advance here — the same role respawns
        # 2 s later with its auto_chain flag restored by _do_respawn.
        self.close(role, project=project, suppress_pipeline=True, suppress_auto_chain=True)

        def _do_respawn() -> None:
            # Restore snapshotted state before spawn() runs so:
            #   - session_uuid lets can_resume pick --resume <uuid>
            #   - last_assigned_task survives for replay (gated by Bug-5 fix)
            #   - auto_chain keeps the verify-hop tag alive
            #   - requires_commit_on_done preserves the commit gate
            # Cooldown stamp: close() pops the whole PaneState so last_stuck_recover
            # reverts to 0.0 — restore it here so the watchdog can't re-trigger
            # within STUCK_RECOVER_COOLDOWN_S of the recovery attempt.
            self._ps(key).last_stuck_recover = now
            # #41: persist the incremented stuck-recover count across the
            # close()-pop so the watchdog can enforce STUCK_RECOVER_MAX (a
            # wedged-but-alive pane never crashes, so auto_respawn_attempts —
            # which only counts crashes — never caps it).
            self._ps(key).stuck_recover_attempts = snap_recover_attempts + 1
            if snap_uuid is not None:
                _ps_r = self._ps(key)
                _ps_r.session_uuid = snap_uuid
                _ps_r.session_uuid_cwd = snap_uuid_cwd
            if snap_task is not None:
                self._ps(key).last_assigned_task = snap_task
            if snap_auto_chain:
                self._ps(key).auto_chain = snap_auto_chain
            if snap_requires_commit:
                self._ps(key).requires_commit_on_done = snap_requires_commit
            if snap_shard_total:
                self._ps(key).shard_total = snap_shard_total
            if snap_pipeline_run_id is not None:
                self._ps(key).pipeline_run_id = snap_pipeline_run_id
            # m3 fix: if PTY teardown hasn't fired _on_session_exit yet (takes
            # longer than the 2s singleShot on a slow machine), _recent_exits
            # has no entry and spawn()'s can_resume returns False → blank session.
            # Synthesise the entry from snap_uuid so we never depend on timing.
            if snap_uuid is not None and key not in self._recent_exits:
                self._recent_exits[key] = {
                    "cwd": snap_uuid_cwd or cwd or "",
                    "ts": time.time(),
                }
            ok, msg = self.spawn(
                role,
                cwd=cwd,
                project=project,
                _from_auto_respawn=True,
                _shard_total=snap_shard_total,
            )
            _log_event("stuck_recover_respawn", role=role, project=project, ok=ok, msg=msg[:160])
            if not ok:
                # Spawn failed — pop the whole PaneState (pane is dead, return
                # to post-close empty state) rather than resetting fields one by
                # one.  Matches the "popped atomically by close()/done()" contract
                # and avoids leaving an empty PaneState entry in _pane_state.
                self._pane_state.pop(key, None)
                # Recovery truly failed: the recovery-close suppressed the
                # pipeline fail/advance assuming the role would come back. It
                # won't — so now mark it failed and advance the hop, else the
                # pipeline stalls forever waiting on a pane that's gone.
                if snap_pipeline_run_id is not None:
                    pl_key = f"{project}::{snap_pipeline_run_id}"
                    pl_run = self._pipeline_runs.get(pl_key)
                    if pl_run is not None and not pl_run.closed:
                        pl_run.hop_pending.discard(role)
                        pl_run.hop_failed.add(role)
                        if not pl_run.hop_pending:
                            self._advance_pipeline(project, pl_key, pl_run)
                return
            # Drive the recovered pane so it actually continues the task:
            #   - blank respawn (no --resume): re-paste the original task verbatim.
            #   - resumed respawn (--resume): claude reloads the conversation but
            #     sits idle at the ready prompt — it does NOT auto-continue the
            #     interrupted turn, so the pane would silently stall ("ไม่ทำต่อ").
            #     Send a short continue-nudge instead of the full task (Bug-5
            #     gate: never re-paste the whole task into restored history — that
            #     would double the work).
            # Fix 1: read structured flag set by spawn() instead of parsing msg.
            _ps_after = self._pane_state.get(key)
            spawn_resumed = _ps_after.last_spawn_resumed if _ps_after is not None else False
            if snap_task:
                if not spawn_resumed:
                    self._send_when_ready(role, snap_task, project=project)
                else:
                    self._send_when_ready(role, _STUCK_RESUME_NUDGE, project=project)

        # 2 s pause so the close has time to terminate the PTY and tear
        # down the WebEngine view before the respawn binds a new one
        # to the same role slot.
        QTimer.singleShot(2_000, _do_respawn)

    def _give_up_stuck(self, role: str, project: str, pane: AgentPane, now: float) -> None:
        """STUCK_RECOVER_MAX hit (#41): stop auto-recovering a wedged-but-alive
        pane. Recovering it again just loops — it re-wedges deterministically —
        and, if it belongs to a pipeline hop, stalls that pipeline forever waiting
        on a done event that never comes. So we give up exactly ONCE: flag the
        pane so the watchdog leaves it alone, drop any auto-chain tag (siblings
        would otherwise wait forever), warn Lead, and — if it's a pipeline-hop
        role — mark it failed + advance the hop (mirrors the crash-cap branch in
        _do_respawn / _schedule_auto_respawn). The pane is left ALIVE so the
        operator can inspect it and reassign; nothing keeps recovering it."""
        key = f"{project}::{role}"
        ps = self._ps(key)
        if ps.stuck_recover_gave_up:
            return  # one-shot — never warn / advance more than once per pane
        ps.stuck_recover_gave_up = True
        ps.last_stuck_recover = now
        _log_event(
            "stuck_recover_capped",
            role=role,
            project=project,
            attempts=ps.stuck_recover_attempts,
        )
        # An auto-chain verify-hop sibling would wait forever for this pane's
        # done event; drop the tag so a capped pane can't keep a hop open. If it
        # was the last blocker, release the chain so the hop doesn't deadlock
        # (bug-1 orch).
        _had_ac_stuck = ps.auto_chain
        ps.auto_chain = False
        self._maybe_fire_auto_chain_handoff(project, _had_ac_stuck)
        # Pipeline hop: fail + advance so the run doesn't stall on a pane that
        # will never report done (same bookkeeping as the respawn-fail path).
        pl_run_id = ps.pipeline_run_id
        if pl_run_id is not None:
            pl_key = f"{project}::{pl_run_id}"
            pl_run = self._pipeline_runs.get(pl_key)
            if pl_run is not None and not pl_run.closed:
                pl_run.hop_pending.discard(role)
                pl_run.hop_failed.add(role)
                if not pl_run.hop_pending:
                    self._advance_pipeline(project, pl_key, pl_run)
            # Unlink the (still-alive) pane from the run so a later operator
            # close() can't re-enter the pipeline-fail branch and spuriously
            # advance a DIFFERENT (already-advanced) hop. The crash-cap path gets
            # this for free by popping the PaneState; we keep the pane alive for
            # inspection, so clear the linkage explicitly.
            ps.pipeline_run_id = None
        lead = self._project_panes(project).get(LEAD.name)
        if lead and lead.session and lead.session.is_alive:
            msg = (
                f"⚠️ [stuck-capped] {role} ({project}) wedged แต่ยังไม่ตาย — "
                f"auto-recover ครบ {STUCK_RECOVER_MAX} ครั้งแล้วยังค้าง เลิก recover "
                f"อัตโนมัติ (กัน loop + pipeline stall) — เช็ค `takkub list` แล้ว "
                f"close + assign ใหม่ถ้าต้องการให้ทำต่อ"
            )
            _cap_sess = lead.session
            _cap_sess.write(msg)
            _delayed_enter(lead, _cap_sess, 150)
            self.leadInjected.emit(msg)

    def _warn_lead_runaway_pane(self, role: str, project: str, rate_bps: float) -> None:
        """Inject a one-line warning into Lead's input when a teammate pane has
        sustained unusually high PTY throughput (issue #35 throughput watchdog).

        Does *not* auto-recover: runaway output is not necessarily an agent bug
        (e.g. a build streaming logs). We surface it so Lead can decide whether
        to close the pane or let it continue."""
        lead = self._project_panes(project).get(LEAD.name)
        if not (lead and lead.session and lead.session.is_alive):
            return
        rate_kb = rate_bps / 1024
        msg = (
            f"⚠️ [runaway-output] {role} pane พ่น output ≈ {rate_kb:.0f} KB/s "
            f"ต่อเนื่อง > {int(RUNAWAY_DURATION_S)}s — อาจติดลูป. "
            f"ตรวจสอบ pane /{role} หรือ `takkub close --role {role}` ถ้าต้องการหยุด"
        )
        _run_sess = lead.session
        _run_sess.write(msg)
        _delayed_enter(lead, _run_sess, 150)
        self.leadInjected.emit(msg)
        _log_event("runaway_pane_warn", role=role, project=project, rate_kb=int(rate_kb))

    def _warn_lead_over_cap(self, role: str, project: str) -> None:
        """Best-effort advisory: when a fresh teammate spawn pushes the TOTAL
        live pane count (across *all* projects) over what the machine can
        comfortably run (``exec_mode.machine_total_pane_cap()``), drop a one-line
        notice into the spawning project's Lead. **Non-blocking** — the spawn
        always proceeds; this only flags oversubscription so the Lead can split
        the batch into waves or close idle panes.

        machine_fanout_cap() is *per role* and ceilinged at MAX_FANOUT, so it
        can't catch the case where every role is within its per-role cap yet the
        aggregate (e.g. frontend#1..#3 + backend#1..#3 = 6 panes) overwhelms a
        small box. This total guard does (see docs/reviews/2026-06-30-queue-gap.md).

        Rate-limited machine-wide (OVERCAP_WARN_COOLDOWN_S) so a burst of fan-out
        assigns doesn't spam. Wrapped so a warning can never break spawning.
        """
        try:
            if role == LEAD.name:
                return  # spawning a Lead pane is never an over-capacity event
            from . import exec_mode  # local import: matches the toggle handler's pattern

            cap = exec_mode.machine_total_pane_cap()
            # The pane being spawned isn't alive yet, so `active` is the count
            # BEFORE it — active >= cap means this fresh spawn is the (cap+1)-th.
            active = _count_active_teammates(self._panes_by_project)
            if active < cap:
                return
            now = time.time()
            last = getattr(self, "_last_overcap_warn_ts", 0.0)
            if (now - last) < OVERCAP_WARN_COOLDOWN_S:
                return
            self._last_overcap_warn_ts = now
            lead = self._project_panes(project).get(LEAD.name)
            if not (lead and lead.session and lead.session.is_alive):
                return
            msg = (
                f"⚠️ [over-capacity] กำลังเปิด pane teammate ตัวที่ {active + 1} — "
                f"เกินที่เครื่องนี้รับไหวสบายๆ (~{cap} panes) · เสี่ยงช้า/ค้าง/RAM พุ่ง. "
                f"พิจารณาแบ่งงานเป็น waves หรือ `takkub close` pane ที่ไม่ใช้แล้ว"
            )
            _cap_sess = lead.session
            _cap_sess.write(msg)
            _delayed_enter(lead, _cap_sess, 150)
            self.leadInjected.emit(msg)
            _log_event("over_capacity_warn", role=role, project=project, active=active, cap=cap)
        except Exception:
            # A capacity advisory must never prevent a pane from spawning.
            pass

    # ── Fan-out queue (flag-gated, default OFF — TAKKUB_QUEUE_FANOUT) ─────────
    # The over-capacity advisory above only *warns*. With the flag on, this queue
    # actually *defers* a fresh teammate spawn that would exceed the machine's
    # total-pane budget and spawns it later when a pane frees a slot — turning the
    # Lead's self-limited fan-out into a machine-enforced wave executor. Default
    # off so the cockpit's spawn behaviour is 100% unchanged until the operator
    # opts in. See docs/reviews/2026-06-30-queue-gap.md.

    def _should_queue_assign(self, role_name: str, project: str | None) -> bool:
        """True iff this assign should be deferred to the fan-out queue rather
        than spawned now: the flag is on, it's a *new* teammate pane (not a
        re-assign to a live one, never Lead), and the machine is already at/over
        its total-pane budget."""
        if not _fanout_queue_enabled():
            return False
        base_role, _ = _split_shard(role_name)
        if base_role == LEAD.name or role_name == LEAD.name:
            return False
        project_ns = self._resolve_project(project)
        existing = self._project_panes(project_ns).get(role_name)
        if (
            existing is not None
            and getattr(existing, "session", None) is not None
            and getattr(existing.session, "is_alive", False)
        ):
            # Re-assigning a new task to an already-running pane spawns nothing,
            # so it must never be queued (queuing it would strand the task).
            return False
        from . import exec_mode

        cap = exec_mode.machine_total_pane_cap()
        return _count_active_teammates(self._panes_by_project) >= cap

    def _enqueue_assign(
        self,
        role_name: str,
        cwd: str | None,
        task: str,
        requires_commit: bool,
        auto_chain: bool,
        shard_total: int,
        plan: bool,
        project: str | None,
    ) -> tuple[bool, str]:
        """Park an over-cap assign on the per-project queue and tell the Lead.
        Replayed verbatim by `_drain_fanout_queue` once a slot frees, so every
        flag (commit gate, auto-chain, shards, plan) survives unchanged."""
        project_ns = self._resolve_project(project)
        q = getattr(self, "_fanout_queue", None)
        if q is None:
            self._fanout_queue = q = {}
        q.setdefault(project_ns, collections.deque()).append(
            {
                "role": role_name,
                "cwd": cwd,
                "task": task,
                "requires_commit": requires_commit,
                "auto_chain": auto_chain,
                "shard_total": shard_total,
                "plan": plan,
                "project": project,
            }
        )
        depth = len(q[project_ns])
        self._save_fanout_queue(project_ns)  # survive a restart with work still queued
        _log_event("assign_queued", role=role_name, project=project_ns, queue_depth=depth)
        lead = self._project_panes(project_ns).get(LEAD.name)
        if lead and lead.session and lead.session.is_alive:
            try:
                from . import exec_mode

                cap = exec_mode.machine_total_pane_cap()
            except Exception:
                cap = 0
            msg = (
                f"⏳ [queued] {role_name} เข้าคิวรอ slot ว่าง (เครื่องเต็ม ~{cap} panes) — "
                f"คิวตอนนี้ {depth} งาน · จะ spawn อัตโนมัติเมื่อมี pane done/close"
            )
            _q_sess = lead.session
            _q_sess.write(msg)
            _delayed_enter(lead, _q_sess, 150)
            self.leadInjected.emit(msg)
        return True, f"{role_name} queued (machine at capacity; {depth} in queue)"

    def _drain_fanout_queue(self, project: str | None) -> None:
        """Pop ONE pending assign for `project` and run it, if the flag is on,
        the queue is non-empty, and a slot is now free. Scheduled (deferred via
        singleShot) after a genuine teammate close so it never re-enters the
        close/emit stack. One slot freed → one dequeue; the next close drains the
        next. Best-effort: a failure here must not break close()."""
        try:
            if not _fanout_queue_enabled():
                return
            project_ns = self._resolve_project(project)
            q = getattr(self, "_fanout_queue", None)
            queue = q.get(project_ns) if q else None
            if not queue:
                return
            from . import exec_mode

            cap = exec_mode.machine_total_pane_cap()
            if _count_active_teammates(self._panes_by_project) >= cap:
                return  # still full — leave the item queued for the next close
            item = queue.popleft()
            self._save_fanout_queue(project_ns)  # persist the shrunk queue
            _log_event(
                "assign_dequeued", role=item["role"], project=project_ns, remaining=len(queue)
            )
            # Replay through the normal assign() path. Its own gate re-checks
            # capacity (now below cap) and proceeds to spawn rather than re-queue.
            self.assign(
                item["role"],
                item["cwd"],
                item["task"],
                requires_commit=item["requires_commit"],
                auto_chain=item["auto_chain"],
                shard_total=item["shard_total"],
                plan=item["plan"],
                project=item["project"],
            )
        except Exception:
            pass

    # ── Fan-out queue durability (mirrors _save/_load_pending_done_notices) ──

    def _fanout_queue_path(self, project_ns: str) -> pathlib.Path:
        return RUNTIME_DIR / f"fanout-queue-{project_ns}.json"

    def _save_fanout_queue(self, project_ns: str) -> None:
        """Persist one project's pending queue so a still-queued assign survives
        a cockpit restart. Empty queue → remove the file. Best-effort."""
        try:
            q = getattr(self, "_fanout_queue", None)
            items = list(q.get(project_ns, [])) if q else []
            path = self._fanout_queue_path(project_ns)
            if items:
                ensure_runtime()
                path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
            elif path.exists():
                path.unlink()
        except Exception:
            pass

    def _load_fanout_queue(self) -> None:
        """Reload persisted per-project queues on startup — only when the flag is
        on, so a stale file from a prior flag-on session is ignored once the
        feature is turned off. Best-effort; a corrupt file is skipped."""
        try:
            if not _fanout_queue_enabled():
                return
            runtime = RUNTIME_DIR
            if not runtime.exists():
                return
            prefix = "fanout-queue-"
            for p in runtime.glob(f"{prefix}*.json"):
                try:
                    proj = p.stem[len(prefix) :]
                    items = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(items, list) and items:
                        if getattr(self, "_fanout_queue", None) is None:
                            self._fanout_queue = {}
                        self._fanout_queue[proj] = collections.deque(items)
                except Exception:
                    continue
        except Exception:
            pass

    def _maybe_submit_stuck_paste(
        self, key: str, role: str, project: str, pane: AgentPane, now: float
    ) -> None:
        """Submit a task paste whose Enter was swallowed and never recovered.

        Fires when a "working" pane sits at its ready prompt with a
        "[Pasted text +N lines]" placeholder in the input box (structural
        signal: ``shows_pending_input()``) for STUCK_PASTE_SUBMIT_AFTER_S —
        the state a swallowed submit leaves behind once
        ``_delayed_enter_verified`` has exhausted its resends (~3 s window,
        too short under parallel-spawn CPU load). Sends a bare CR, which is
        harmless if the submit is actually mid-flight, and retries on a
        cooldown up to STUCK_PASTE_SUBMIT_MAX times so a pane a CR cannot fix
        (e.g. wedged upstream TUI) is escalated to the log instead of poked
        forever. State lives in PaneState and resets the moment the pending
        input clears (submit landed → pane goes busy)."""
        ps = self._ps(key)
        try:
            stuck = pane.session.is_at_ready_prompt() and pane.session.shows_pending_input()
        except Exception:
            stuck = False
        if not stuck:
            ps.pending_input_since = None
            ps.pending_submit_attempts = 0
            return
        if ps.pending_input_since is None:
            ps.pending_input_since = now
            return
        if (now - ps.pending_input_since) < STUCK_PASTE_SUBMIT_AFTER_S:
            return
        if (now - ps.last_pending_submit_ts) < STUCK_PASTE_SUBMIT_COOLDOWN_S:
            return
        if ps.pending_submit_attempts >= STUCK_PASTE_SUBMIT_MAX:
            return
        pane.session.write(b"\r")
        ps.last_pending_submit_ts = now
        ps.pending_submit_attempts += 1
        _log_event(
            "stuck_paste_submit",
            role=role,
            project=project,
            attempt=ps.pending_submit_attempts,
            stuck_for_s=int(now - ps.pending_input_since),
        )
        if ps.pending_submit_attempts >= STUCK_PASTE_SUBMIT_MAX:
            # CRs aren't landing — leave a loud breadcrumb for the operator
            # instead of silently giving up (no-silent-caps rule).
            _log_event("stuck_paste_gave_up", role=role, project=project)

    def _rate_limit_suppressed(self, project: str, role: str, pane: AgentPane, now: float) -> bool:
        """Return True if `pane` is rate-limited and the watchdog should leave
        it alone until the limit resets.

        On first detection it records the reset epoch and schedules a one-shot
        notice to the Lead (option A: notify only, no auto-resume). Once the
        reset time passes the state is cleared and the watchdog resumes."""
        key = f"{project}::{role}"
        _ps_rl = getattr(self, "_pane_state", {}).get(key)
        existing = _ps_rl.rate_limited_until if _ps_rl is not None else 0.0
        if existing > 0.0:
            if now < existing:
                return True
            # Reset time reached — clear and let the watchdog behave normally.
            # The notice fires from its own QTimer scheduled at detection time.
            if _ps_rl is not None:
                _ps_rl.rate_limited_until = 0.0
            return False

        if pane.session is None or not pane.session.is_alive:
            return False
        reset_at = pane.session.rate_limit_reset_at()
        if reset_at is None:
            return False

        self._ps(key).rate_limited_until = reset_at
        self._schedule_rate_limit_notice(project, role, reset_at)
        _log_event(
            "rate_limit_detected",
            role=role,
            project=project,
            resets_in_s=int(max(0, reset_at - now)),
        )
        return True

    def _schedule_rate_limit_notice(self, project: str, role: str, reset_at: float) -> None:
        """Fire a single reset notice when the usage limit lifts."""
        delay_ms = max(0, int((reset_at - time.time()) * 1000))
        QTimer.singleShot(delay_ms, lambda: self._emit_rate_limit_reset(project, role))

    def _emit_rate_limit_reset(self, project: str, role: str) -> None:
        """Tell the Lead a rate-limited pane's window has reset (notify-only)."""
        key = f"{project}::{role}"
        _ps_rr = self._pane_state.get(key)

        # De-dupe guard: if rate_limited_until is already 0, a previous timer
        # for the same episode already handled the reset — skip silently.
        if _ps_rr is None or _ps_rr.rate_limited_until == 0.0:
            _log_event(
                "rate_limit_reset_skipped",
                role=role,
                project=project,
                reason="already_handled",
            )
            return

        # Pane-alive guard: if the pane closed while the timer was pending,
        # there is nobody to assign work to — clear state but skip the notice.
        panes = self._project_panes(project)
        target_pane = panes.get(role)
        if target_pane is None or target_pane.session is None or not target_pane.session.is_alive:
            _ps_rr.rate_limited_until = 0.0
            _log_event(
                "rate_limit_reset_skipped",
                role=role,
                project=project,
                reason="pane_gone",
            )
            return

        # Pane is alive — clear state and reset the stuck-watchdog timestamp so
        # the very next tick doesn't see content_static_s >> STUCK_THRESHOLD_S
        # and trigger a spurious close→respawn (#53 fix, must stay here).
        _ps_rr.rate_limited_until = 0.0
        _ps_rr.last_content_change_ts = time.time()

        msg = (
            f"⏰ [rate-limit] {role} ({project}) — usage limit reset แล้ว "
            f"pane พร้อมทำงานต่อ (nudge/มอบงานต่อได้เลย)"
        )
        lead = panes.get(LEAD.name)
        if lead and lead.session and lead.session.is_alive:
            _rl_sess = lead.session
            _rl_sess.write(msg)
            _delayed_enter(lead, _rl_sess, 150)
            self.leadInjected.emit(msg)
        _log_event("rate_limit_reset", role=role, project=project)
        self.statusChanged.emit()

    def _maybe_surface_tty_block(
        self, key: str, role: str, project: str, prompt_line: str, now: float
    ) -> None:
        """Record the TTY block start time and call _surface_tty_block_notice
        once the block has lasted TTY_BLOCK_SURFACE_AFTER_S, then re-surface
        at most every TTY_BLOCK_SURFACE_COOLDOWN_S while still blocked."""
        ps = self._ps(key)
        if ps.tty_blocked_since is None:
            ps.tty_blocked_since = now
        if (
            now - ps.tty_blocked_since >= TTY_BLOCK_SURFACE_AFTER_S
            and now - ps.last_tty_block_surface_ts >= TTY_BLOCK_SURFACE_COOLDOWN_S
        ):
            self._surface_tty_block_notice(role, project, prompt_line)
            ps.last_tty_block_surface_ts = now

    def _surface_tty_block_notice(self, role: str, project: str, prompt_line: str) -> None:
        """Inject a notice into Lead's input when a teammate pane is blocked
        on an interactive subprocess prompt (issue #54).

        Does NOT auto-close or respawn — surface + nudge only. Lead (or the
        operator) decides whether to send a non-interactive flag or manually
        unblock the pane."""
        lead = self._project_panes(project).get(LEAD.name)
        if not (lead and lead.session and lead.session.is_alive):
            return
        msg = (
            f"⚠️ [{role}] ค้างรอ input: '{prompt_line}' — "
            f"subprocess รอคำตอบ interactive (y/N, passphrase, 'press any key'). "
            f"แก้: รัน subprocess แบบ non-interactive "
            f"(เช่น `-y`, `--no-input`, `DEBIAN_FRONTEND=noninteractive`) "
            f'หรือ `takkub send --to {role} "<คำแนะนำ>"` เพื่อปลด block'
        )
        _tty_sess = lead.session
        _tty_sess.write(msg)
        _delayed_enter(lead, _tty_sess, 150)
        self.leadInjected.emit(msg)
        _log_event("tty_block_surface", role=role, project=project, prompt=prompt_line)

    def _inject_idle_reminder(self, role_name: str, pane: AgentPane) -> None:
        if pane.session is None or not pane.session.is_alive:
            return
        _idle_sess = pane.session
        _idle_sess.write(IDLE_REMINDER_TEXT)
        _delayed_enter(pane, _idle_sess, 150)
        _log_event("idle_reminder", role=role_name)

    def _maybe_surface_malformed_xml(
        self, key: str, role: str, project: str, pane: AgentPane, now: float
    ) -> None:
        """Inject a nudge into `pane` if literal tool-call XML is visible on
        screen, indicating the harness silently no-op'd a malformed tool call
        (missing ``antml:`` prefix). Fires at most once per
        MALFORMED_XML_NOTICE_COOLDOWN_S (issue #59)."""
        ps = self._ps(key)
        if now - ps.malformed_xml_notice_ts < MALFORMED_XML_NOTICE_COOLDOWN_S:
            return
        if pane.session is None or not pane.session.is_alive:
            return
        matched = pane.session.has_unparsed_tool_call()
        if matched is None:
            return
        msg = (
            "⚠️ [cockpit] ตรวจพบ tool-call XML ที่ harness parse ไม่ได้ "
            "(หล่น `antml:` prefix) — คำสั่งไม่ได้รันจริงและไม่ถือว่า hang "
            "ลองพิมพ์ tool call ใหม่ให้ใช้ antml:invoke / antml:parameter ให้ครบ"
        )
        _xml_sess = pane.session
        _xml_sess.write(msg)
        _delayed_enter(pane, _xml_sess, 150)
        ps.malformed_xml_notice_ts = now
        _log_event("malformed_tool_call_detected", role=role, project=project)

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
        # Route the keystrokes to the pane that ACTUALLY emitted them, not to
        # `self.panes[role_name]`. `self.panes` resolves to the *active project*
        # only — a single-project-era assumption that predates multi-tab support.
        # With several project tabs open, every project's same-role pane (e.g.
        # both Leads) is wired to this one slot, so role-name lookup sent input
        # into whichever project happened to be active — misdelivering keystrokes
        # (incl. Shift+Tab, which cycles claude's permission mode) into the wrong
        # pane. Qt's sender() is the emitting AgentPane, so it is project-correct
        # by construction. Fall back to the role lookup for direct/test calls
        # where there is no signal sender.
        pane = self.sender()
        if not isinstance(pane, AgentPane):
            pane = self.panes.get(role_name)
        if pane is None or pane.session is None:
            return
        pane.session.write(data)
