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
from .lead_context import (  # re-exported for test + doctor.py imports
    _LEAD_GUARD_ALLOW_TOOLS,
    _LEAD_GUARD_WRITE_TOOLS,
    _SAFE_PLUGINS,
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
    _ECC_MUTED_HOOKS,
    _LEAD_ENV_EXTRA_ALLOWLIST,
    _PANE_ENV_ALLOWLIST,
    _apply_ecc_mute,
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
    "_ECC_MUTED_HOOKS",
    "_HARVEST_EXCLUDE_DIRS",
    "_JUNK_NOTE_EXACT",
    "_JUNK_NOTE_MIN_LEN",
    "_JUNK_PROJECT_PREFIXES",
    "_LEAD_ENV_EXTRA_ALLOWLIST",
    "_LEAD_GUARD_ALLOW_TOOLS",
    "_LEAD_GUARD_WRITE_TOOLS",
    "_PANE_ENV_ALLOWLIST",
    "_SAFE_PLUGINS",
    "_VAULT_ENV",
    "_allowed_project_roots",
    "_apply_ecc_mute",
    "_apply_mcp_timeout",
    "_apply_non_interactive_env",
    "_build_lead_env",
    "_build_pane_env",
    "_default_plugin_dirs",
    "_is_junk_note",
    "_is_junk_project",
    "_recent_session_brief",
    "_render_decision_note",
    "_render_lead_context",
    "_resolve_vault_dir",
    "inject_user_profile_env",
    "prune_old_transcripts",
    "render_lead_settings",
    "scan_artifacts",
]


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

# Malformed tool-call XML detection (issue #59). When a model outputs tool-call
# XML without the `antml:` namespace prefix the harness silently no-ops it and
# the pane appears to hang. Nudge the pane at most this often.
MALFORMED_XML_NOTICE_COOLDOWN_S = 60  # minimum gap between repeat nudges

# Continue-nudge injected after a *resumed* stuck-recovery. `--resume` restores
# the conversation but leaves claude idle at the ready prompt — it does NOT
# auto-continue the interrupted turn, so without a prompt the recovered pane
# silently stalls. Short by design: the full task is already in the restored
# history, and re-pasting it would double the work (Bug-5 gate).
_STUCK_RESUME_NUDGE = (
    "[auto-recovered] pane นี้ถูก restart อัตโนมัติเพราะค้างนานเกิน threshold — "
    "conversation เดิมถูกโหลดกลับมาแล้ว ทำงานต่อจากจุดที่ค้างไว้ได้เลย "
    "เสร็จแล้วรายงานด้วย takkub done"
)

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

# Auto-respawn on unexpected pane crash. The orchestrator notices when a
# pane exits without a corresponding takkub close/done (claude crashed,
# OOM, parent killed it) and gives it a clean respawn with --resume <uuid>
# so the conversation survives. AUTO_RESPAWN_MAX caps consecutive attempts
# per pane so a deterministically-crashing claude doesn't spawn-loop.
AUTO_RESPAWN_DELAY_MS = 2_500
AUTO_RESPAWN_MAX = 2

# Initial PTY geometry every pane session spawns with (FitAddon resizes it to the
# real widget once the page loads). Named so the four provider spawn branches
# can't drift apart. (M5#25)
_PANE_COLS = 110
_PANE_ROWS = 36

# Codex early-crash detection. If a codex pane exits within this many seconds
# of spawning, the orchestrator treats it as a suspicious early crash, logs a
# `codex_early_crash` event, and writes a diagnostic dump to
# runtime/codex_crash_dumps/<ts>-<project>-<role>.log containing the exit
# code, time-to-exit, last PTY output tail, and the filtered env keys.  Dumps
# let us falsify the MCP-boot-race vs env-missing hypotheses without needing a
# live debugger session.
CODEX_EARLY_CRASH_WINDOW_SEC = 90

# Tier 2: tight re-samples of InSendMessageEx immediately before each native
# ConPTY call to narrow the TOCTOU window between the early gate check and the
# actual winpty.PtyProcess.spawn().  Not a temporal quiet guarantee — use Tier 1
# event-loop-turn streak for that.
_TOCTOU_RESAMPLE_N = 3

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


@dataclass
class PaneState:
    """Per-pane transient state, keyed ``"{project}::{role}"`` in
    ``Orchestrator._pane_state``.

    Consolidates the ~15 per-pane dicts that used to live as separate
    ``dict[str, T]`` attributes on Orchestrator.  Created lazily by
    ``_ps(key)`` and popped atomically by ``close()`` / ``done()`` so
    teardown is a single ``_pane_state.pop(key)`` instead of ~15 individual
    dict pops (the root cause of state-divergence bugs).

    ``_idle_state`` and ``_recent_exits`` are intentionally **not** merged here:

    * ``_idle_state``: key-presence semantics (absent = "not tracking") relied
      on by the watchdog and tests.
    * ``_recent_exits``: persists through ``close()`` (needed for crash-resume
      logic); close() must NOT clear it so ``_do_respawn`` can still find the
      entry even when ``_on_session_exit`` fires after the 2 s delay.
    """

    # _session_uuids: uuid+cwd for the current/last session
    session_uuid: str | None = None
    session_uuid_cwd: str = ""
    # _blocked_on_lead: ts when teammate last sent to Lead (suppresses idle nag)
    blocked_on_lead_ts: float | None = None
    # _rate_limited_until: epoch at which the usage-rate limit resets (0 = no limit)
    rate_limited_until: float = 0.0
    # _auto_respawn_attempts: consecutive crash-respawn count (capped at AUTO_RESPAWN_MAX)
    auto_respawn_attempts: int = 0
    # _last_assigned_task: last task pasted by assign(); replayed after crash-respawn
    last_assigned_task: str | None = None
    # _requires_commit_on_done: warns Lead of uncommitted changes when done() fires
    requires_commit_on_done: bool = False
    # _auto_chain_panes: pane is tagged --auto-chain; done() fires verify-hop when last
    auto_chain: bool = False
    # _last_stuck_recover: cooldown ts for the stuck-pane auto-recover watchdog
    last_stuck_recover: float = 0.0
    # _stuck_recover_attempts: consecutive stuck-recover count (capped at STUCK_RECOVER_MAX, #41)
    stuck_recover_attempts: int = 0
    # _stuck_recover_gave_up: True once STUCK_RECOVER_MAX hit — watchdog stops recovering this pane
    stuck_recover_gave_up: bool = False
    # tty_blocked_since / last_tty_block_surface_ts: TTY-prompt block tracking (issue #54).
    # tty_blocked_since is set on first detection; last_tty_block_surface_ts gates re-surface.
    tty_blocked_since: float | None = None
    last_tty_block_surface_ts: float = 0.0
    # malformed_xml_notice_ts: last time a malformed-tool-call nudge was injected (issue #59).
    malformed_xml_notice_ts: float = 0.0
    # _codex_spawn_times: wall-clock at spawn for early-crash detection (None = not set)
    codex_spawn_ts: float | None = None
    # _last_send_ts: last delivery ts for stall detection
    last_send_ts: float = 0.0
    # _shard_total: total shards in the fan-out group (0 = not a shard pane)
    shard_total: int = 0
    # _harvest_hint_ts: cooldown for harvest-hint injection to Lead
    harvest_hint_ts: float = 0.0
    # _last_content_hash + _last_content_change_ts: content-delta stuck detection
    last_content_hash: str | None = None
    last_content_change_ts: float | None = None
    # _last_spawn_resumed: True when the last spawn used --resume (not --session-id)
    last_spawn_resumed: bool = False
    # throughput watchdog (issue #35) — snapshot of pane._tp_total_bytes taken
    # each watchdog tick, plus the wall-clock of that snapshot.
    tp_last_total: int = 0
    tp_last_ts: float = 0.0
    # Wall-clock when throughput first exceeded RUNAWAY_BYTES_S (None = not over)
    tp_runaway_since: float | None = None
    # Last time Lead was warned about this pane's runaway throughput
    tp_warn_ts: float = 0.0
    # pipeline_run_id: set when this pane was spawned as part of a pipeline hop
    pipeline_run_id: str | None = None


class Orchestrator(PipelineMixin, BroadcastMixin, LeadInboxMixin, QObject):
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
        # Panes are namespaced per project so the upcoming multi-tab UI
        # (Plan B) can keep each project's Lead + teammates isolated. The
        # `panes` property below resolves to the *active* project's inner
        # dict so every existing caller (UI + tests) keeps the same shape.
        # Until tabs land, only one project namespace is populated at a
        # time and behavior is identical to the pre-refactor single-dict.
        self._panes_by_project: dict[str, dict[str, AgentPane]] = {}
        # Per-pane transient state. Created lazily by _ps(key) and popped
        # atomically by close()/done() — single dict.pop replaces what was
        # previously ~14 individual teardown pops (root cause of divergence bugs).
        self._pane_state: dict[str, PaneState] = {}
        # last-known cwd per role — kept as a separate dict because its lifecycle
        # differs: close() does NOT clear it (unlike _pane_state) so _do_respawn
        # can still read the entry after close() fires during stuck-recover.
        # Only cleared by a successful spawn() (del after attach).
        self._recent_exits: dict[str, dict] = {}  # "{project}::{role}" -> {cwd, ts}
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
        self._load_pending_done_notices()
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

        # Per-pane capability tokens.  token → (project_ns, role_name).
        # Injected as TAKKUB_PANE_TOKEN into every non-Lead pane at spawn so
        # the IPC server can derive caller identity from the token rather than
        # trusting the caller-supplied `from`/`from_project` fields.  Entries
        # are removed when the pane is closed.  Checked on `done` and `send`.
        self._pane_tokens: dict[str, tuple[str, str]] = {}

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
        # Per-(project::role) set of roles with a pending deferred-spawn timer.
        # Prevents duplicate QTimer callbacks while the gate is still blocked.
        self._spawn_deferred: set[str] = set()
        # FIFO queue: serialise ConPTY construction so only one session.spawn()
        # runs at a time (a second call while one is in progress is queued here
        # and re-dispatched by _drain_spawn_queue when the current one finishes).
        self._spawn_queue: collections.deque = collections.deque()
        # True while a ConPTY session.spawn() call is executing on this thread.
        self._spawn_in_progress: bool = False

    # ──────────────────────────────────────────────────────────────
    # project-aware view onto the pane registry
    # ──────────────────────────────────────────────────────────────
    # per-pane state helpers
    # ──────────────────────────────────────────────────────────────
    def _ps(self, key: str) -> PaneState:
        """Get-or-create the PaneState for *key* (``"{project}::{role}"``).

        Callers that only need to *read* without creating an entry should use
        ``self._pane_state.get(key)`` and guard against None.

        Lazily initialises ``_pane_state`` so test fixtures that create a bare
        ``Orchestrator.__new__`` instance without running ``__init__`` still work.
        """
        try:
            d = self._pane_state
        except AttributeError:
            d = {}
            self._pane_state = d
        try:
            return d[key]
        except KeyError:
            ps = PaneState()
            d[key] = ps
            return ps

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
    # spawn-gate helpers (injected predicate + deferred retry)
    # ──────────────────────────────────────────────────────────────

    def set_spawn_guard(self, pred: Callable[[], bool] | None) -> None:
        """Inject the Qt modal/popup predicate from main_window.

        pred() == True  → ConPTY spawn is unsafe (modal or popup active).
        Pass None to disable the guard (tests, headless).
        """
        self._spawn_gate_pred = pred  # type: ignore[assignment]

    def _is_spawn_blocked(self) -> bool:
        """3-layer gate: True when ConPTY spawn is unsafe right now."""
        from .spawn_gate import is_spawn_blocked

        return is_spawn_blocked(getattr(self, "_spawn_gate_pred", None))

    def _retry_deferred_spawn(
        self,
        role_name: str,
        cwd: str | None,
        project: str | None,
        _from_auto_respawn: bool,
        _shard_total: int,
    ) -> None:
        """QTimer callback: re-evaluate gate and spawn when safe, or re-defer."""
        project_ns = self._resolve_project(project)
        deferred_key = f"{project_ns}::{role_name}"
        _deferred = getattr(self, "_spawn_deferred", None)
        if _deferred is not None:
            _deferred.discard(deferred_key)

        pane = self._project_panes(project_ns).get(role_name)
        if pane is not None and pane.session is not None and pane.session.is_alive:
            _log_event("spawn_deferred_already_alive", role=role_name, project=project_ns)
            return
        if pane is None:
            _log_event("spawn_deferred_pane_gone", role=role_name, project=project_ns)
            return

        if self._is_spawn_blocked():
            if _deferred is not None:
                _deferred.add(deferred_key)
            _log_event("spawn_still_blocked", role=role_name, project=project_ns)
            QTimer.singleShot(
                50,
                lambda r=role_name, c=cwd, p=project, a=_from_auto_respawn, s=_shard_total: (
                    self._retry_deferred_spawn(r, c, p, a, s)
                ),
            )
            return

        # Gate cleared: wait ~35 ms (1 event-loop turn) then re-check to
        # close the check-to-call race, then re-enter spawn() which verifies once more.
        QTimer.singleShot(
            35,
            lambda r=role_name, c=cwd, p=project, a=_from_auto_respawn, s=_shard_total: self.spawn(
                r, cwd=c, project=p, _from_auto_respawn=a, _shard_total=s
            ),
        )

    def _drain_spawn_queue(self) -> None:
        """Pop and schedule the next queued spawn after the current one finishes."""
        _queue = getattr(self, "_spawn_queue", None)
        if not _queue:
            return
        role, cwd, project, from_auto_respawn, shard_total = _queue.popleft()
        project_ns = self._resolve_project(project)
        pane = self._project_panes(project_ns).get(role)
        if pane is not None and pane.session is not None and pane.session.is_alive:
            self._drain_spawn_queue()
            return
        _log_event("spawn_queue_drain", role=role, project=project_ns)
        QTimer.singleShot(
            0,
            lambda r=role, c=cwd, p=project, a=from_auto_respawn, s=shard_total: self.spawn(
                r, cwd=c, project=p, _from_auto_respawn=a, _shard_total=s
            ),
        )

    # ──────────────────────────────────────────────────────────────
    # Tier 2 final re-sample gate helpers
    # ──────────────────────────────────────────────────────────────

    def _final_gate_clear(self) -> bool:
        """Tight TOCTOU re-sample: True when InSendMessageEx stays clear for
        _TOCTOU_RESAMPLE_N consecutive synchronous reads in the same callback.

        Call immediately before session.spawn() (after all env/argv/transcript
        setup) and only proceed with the native call when this returns True.
        No yield, no QTimer, no processEvents between the check and the call.
        """
        from .spawn_gate import is_in_send_stable

        return is_in_send_stable(_TOCTOU_RESAMPLE_N)

    def _toctou_redefer(
        self,
        role_name: str,
        cwd: str | None,
        project: str | None,
        project_ns: str,
        _from_auto_respawn: bool,
        _shard_total: int,
        pane_tok: str | None = None,
    ) -> None:
        """Clean re-defer after Tier 2 final-gate failure.

        Revokes the pane token so it cannot be used by the abandoned attempt,
        re-adds the role to _spawn_deferred, and schedules _retry_deferred_spawn.
        _spawn_in_progress is reset by the calling try/finally block.
        """
        if pane_tok is not None:
            getattr(self, "_pane_tokens", {}).pop(pane_tok, None)
        _deferred = getattr(self, "_spawn_deferred", None)
        if _deferred is None:
            self._spawn_deferred = _deferred = set()
        _dk = f"{project_ns}::{role_name}"
        _deferred.add(_dk)
        _log_event(
            "spawn_toctou_redeferred",
            role=role_name,
            project=project_ns,
        )
        QTimer.singleShot(
            50,
            lambda r=role_name, c=cwd, p=project, a=_from_auto_respawn, s=_shard_total: (
                self._retry_deferred_spawn(r, c, p, a, s)
            ),
        )

    # ──────────────────────────────────────────────────────────────
    # high-level operations
    # ──────────────────────────────────────────────────────────────
    def _mint_pane_token(self, env: dict, project_ns: str, role_name: str) -> str:
        """Mint a fresh per-pane auth token for ``(project_ns, role_name)``.

        Revokes any prior token for that same pair first — so a respawn never
        leaves a crashed session's token valid — then registers the new one and
        stamps it into ``env["TAKKUB_PANE_TOKEN"]``. Returns the token; callers
        keep it to revoke explicitly if the spawn then fails. M5#24: this minting
        boilerplate was copy-pasted across all four provider branches of spawn().
        """
        if not hasattr(self, "_pane_tokens"):
            self._pane_tokens: dict[str, tuple[str, str]] = {}
        tok = secrets.token_urlsafe(32)
        for _t in [t for t, v in list(self._pane_tokens.items()) if v == (project_ns, role_name)]:
            self._pane_tokens.pop(_t, None)
        self._pane_tokens[tok] = (project_ns, role_name)
        env["TAKKUB_PANE_TOKEN"] = tok
        return tok

    def _launch_session(
        self,
        *,
        pane,
        role_name: str,
        project_ns: str,
        spawn_cwd: str,
        argv: list[str],
        env: dict,
        pane_tok: str,
        label: str,
        cwd: str | None,
        project: str | None,
        _from_auto_respawn: bool,
        _shard_total: int,
        codex_exit: bool = False,
        auto_trust: bool = False,
    ) -> tuple[bool, str]:
        """Common ConPTY launch tail for the non-claude spawn branches (shell,
        gemini, codex). Creates the PtySession, runs the final TOCTOU re-sample
        gate, does the native spawn under the _spawn_in_progress arbiter, attaches
        the pane, wires the exit handler, clears recent-exit state, and returns
        spawn()'s (ok, msg). M5#23: was copy-pasted ~50 lines x3 with real drift —
        the divergences are now explicit params:

          codex_exit : stamp ``codex_spawn_ts`` + wire ``_on_codex_exit`` (codex
                       early-crash detection) instead of the stale-guarded
                       ``_on_session_exit`` used by shell/gemini.
          auto_trust : call ``_auto_trust`` after attach (gemini/codex; NOT shell).

        The claude branch is intentionally NOT routed through here — it adds
        resume / session-uuid / MCP wiring and stays inline.
        """
        session = PtySession(cols=_PANE_COLS, rows=_PANE_ROWS, parent=self)
        _t_path = _build_transcript_path(project_ns, role_name)
        pane._transcript_path = _t_path
        self._spawn_in_progress = True
        try:
            # Tier 2 final re-sample: check InSendMessageEx immediately before the
            # native ConPTY call to narrow the TOCTOU window.
            if not self._final_gate_clear():
                session.setParent(None)
                session.deleteLater()
                self._toctou_redefer(
                    role_name,
                    cwd,
                    project,
                    project_ns,
                    _from_auto_respawn,
                    _shard_total,
                    pane_tok=pane_tok,
                )
                return True, f"{role_name} spawn deferred (final re-sample blocked)"
            _t0 = time.time()
            session.spawn(argv=argv, cwd=spawn_cwd, env=env, transcript_path=_t_path)
            _log_event(
                "spawn_native_ms",
                role=role_name,
                project=project_ns,
                ms=int((time.time() - _t0) * 1000),
            )
            pane.attach_session(session, cwd=spawn_cwd)
            _ekey = _exit_key(project_ns, role_name)
            _sess = session
            if codex_exit:
                self._ps(_ekey).codex_spawn_ts = time.time()
                session.processExited.connect(
                    lambda code, r=role_name, c=spawn_cwd, p=project_ns, sess=_sess: (
                        self._on_codex_exit(code, r, c, p, sess)
                    )
                )
            else:
                session.processExited.connect(
                    lambda _code, r=role_name, c=spawn_cwd, p=project_ns, s=_sess: (
                        self._on_session_exit(r, c, p)
                        if (pp := self._panes_by_project.get(p, {}).get(r)) is not None
                        and pp.session is s
                        else None
                    )
                )
            if _ekey in self._recent_exits:
                del self._recent_exits[_ekey]
            if auto_trust:
                self._auto_trust(role_name, project=project_ns)
            self.statusChanged.emit()
            _log_event("spawn", role=role_name, cwd=spawn_cwd, resumed=False)
            return True, f"{label} spawned in {spawn_cwd}"
        except Exception as e:
            self._pane_tokens.pop(pane_tok, None)
            return False, f"failed to spawn {label}: {e}"
        finally:
            self._spawn_in_progress = False
            self._drain_spawn_queue()

    def spawn(
        self,
        role_name: str,
        cwd: str | None = None,
        project: str | None = None,
        _from_auto_respawn: bool = False,
        _shard_total: int = 0,
    ) -> tuple[bool, str]:
        try:
            role_name = validate_name(role_name, "role")
        except ValueError as exc:
            return False, str(exc)
        # Separate instance key from behaviour key.
        # base_role ("qa") → role file / provider / cwd / env config
        # role_name ("qa#1") → registry key, pane_state key, TAKKUB_ROLE env
        base_role, shard_idx = _split_shard(role_name)
        project_ns = self._resolve_project(project)
        project_panes = self._project_panes(project_ns)
        pane = project_panes.get(role_name)
        if pane is None:
            # ask main_window to create + register the pane, then retry
            self.paneRequested.emit(role_name, project_ns)
            pane = project_panes.get(role_name)
            if pane is None:
                # Pane never landed in this project's registry — usually a
                # main_window routing desync (the pane got created under the
                # wrong tab, or a stale entry blocked creation). Log it instead
                # of failing silently so the assign drop is visible (#26).
                _log_event(
                    "spawn_failed",
                    role=role_name,
                    project=project_ns,
                    reason="pane not registered after paneRequested",
                )
                return False, f"could not create pane for {role_name} (registry desync)"

        if pane.session is not None and pane.session.is_alive:
            return True, f"{role_name} already running"

        # Fresh spawn — clear any stale watchdog tracking from a prior
        # session so the new claude conversation starts with a clean slate
        # (no leftover "blocked on lead" flag, no leftover idle streak).
        # Auto-respawn attempts are cleared on manual spawns so a pane that
        # was deliberately revived after working fine gets a clean recovery
        # budget.  Auto-respawn paths pass _from_auto_respawn=True to keep
        # the counter so a deterministically-crashing claude can't loop.
        key = f"{project_ns}::{role_name}"
        self._idle_state.pop(key, None)
        _ps_spawn_clear = getattr(self, "_pane_state", {}).get(key)
        if _ps_spawn_clear is not None:
            _ps_spawn_clear.blocked_on_lead_ts = None
            if not _from_auto_respawn:
                _ps_spawn_clear.auto_respawn_attempts = 0
                # #41: a deliberate (manual / fresh-assign) spawn also clears the
                # stuck-recover cap — a pane revived to do new work gets a clean
                # recovery budget. NOT cleared on the _from_auto_respawn path (the
                # stuck-recover respawn itself) or the cap would reset every
                # recovery and never bite a genuinely-wedged pane.
                _ps_spawn_clear.stuck_recover_attempts = 0
                _ps_spawn_clear.stuck_recover_gave_up = False
            # Auto-respawn path: recover shard_total so TAKKUB_SHARD_TOTAL is
            # re-injected correctly when _shard_total wasn't passed explicitly.
            if _shard_total == 0 and _ps_spawn_clear.shard_total > 0:
                _shard_total = _ps_spawn_clear.shard_total

        # Fix 1: validate explicit cwd stays within the project's configured paths.
        # "default" namespace (unit-test / no-project) is exempt since it has no
        # configured paths to validate against. The cockpit repo itself is always
        # allowed so Lead can self-edit cockpit files (CLAUDE.md, projects.json, …).
        if cwd and project_ns != "default" and not _cwd_within_project(cwd, project_ns, role_name):
            return False, f"cwd '{cwd}' is outside project '{project_ns}' paths"

        # ── Spawn gate + FIFO arbiter ────────────────────────────────
        # Prevent RPC_E_CANTCALLOUT_ININPUTSYNCCALL (Windows fatal 0x8001010d):
        # ConPTY construction is illegal when the Qt main thread is inside an
        # input-synchronous call context (modal dialog, QMenu, COM SendMessage).
        # Gate is checked at ONE point above all four spawn branches.
        # Callers receive True so _send_when_ready keeps polling until the
        # session becomes alive (see updated _check() below).
        _deferred = getattr(self, "_spawn_deferred", None)
        if _deferred is None:
            self._spawn_deferred = _deferred = set()
        _deferred_key = f"{project_ns}::{role_name}"

        if _deferred_key in _deferred:
            # A retry timer is already in flight for this role; don't add another.
            return True, f"{role_name} spawn already pending"

        if self._is_spawn_blocked():
            _deferred.add(_deferred_key)
            _log_event("spawn_deferred_gate", role=role_name, project=project_ns)
            QTimer.singleShot(
                50,
                lambda r=role_name, c=cwd, p=project, a=_from_auto_respawn, s=_shard_total: (
                    self._retry_deferred_spawn(r, c, p, a, s)
                ),
            )
            return True, f"{role_name} spawn deferred (gate blocked)"

        # Gate clear — discard any stale deferred marker from a prior retry cycle.
        _deferred.discard(_deferred_key)

        # FIFO serialisation: if another ConPTY session.spawn() is already
        # executing (GIL may be released, Qt can dispatch further timers),
        # queue this request and return True so callers start polling.
        if getattr(self, "_spawn_in_progress", False):
            _queue = getattr(self, "_spawn_queue", None)
            if _queue is None:
                self._spawn_queue = _queue = collections.deque()
            _queue.append((role_name, cwd, project, _from_auto_respawn, _shard_total))
            _log_event("spawn_queued_fifo", role=role_name, project=project_ns)
            return True, f"{role_name} spawn queued (arbiter busy)"
        # ── /Spawn gate + FIFO arbiter ──────────────────────────────

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
            inject_user_profile_env(env, project_ns)
            bin_dir = str(REPO_ROOT / "bin")
            env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
            _shell_tok = self._mint_pane_token(env, project_ns, role_name)
            shell_argv = [pwsh_basename, "-NoLogo"]
            return self._launch_session(
                pane=pane,
                role_name=role_name,
                project_ns=project_ns,
                spawn_cwd=spawn_cwd,
                argv=shell_argv,
                env=env,
                pane_tok=_shell_tok,
                label="shell",
                cwd=cwd,
                project=project,
                _from_auto_respawn=_from_auto_respawn,
                _shard_total=_shard_total,
            )

        # ── codex pane: non-claude path ─────────────────────────────
        # `codex` is OpenAI's TUI; it speaks a different protocol and
        # doesn't understand any of the claude flags below. Build a
        # minimal argv and short-circuit so we don't accidentally pass
        # `--dangerously-skip-permissions`, MCP configs, plugin dirs,
        # or `--session-id`/`--resume` (all claude-only) to it.
        #
        # Entry condition uses `effective_provider_for(role_name)` so the
        # user can remap any teammate role (e.g. "backend") to the codex
        # binary via `~/.takkub/role-providers.json`. The `codex` role
        # itself is forced toward codex by provider_config's
        # `_FORCED_PROVIDER` table.
        #
        # `effective_provider_for` (not plain `provider_for`) degrades a
        # codex/gemini role to claude when that provider is unavailable —
        # toggled off in the status bar OR its CLI isn't installed. When
        # that happens neither branch below matches and execution falls
        # through to the claude spawn path *with role_name unchanged*, so
        # a "gemini"/"codex" pane keeps its slot/identity but is powered by
        # claude ("Claude รับตำแหน่งแทน"). The in-branch `find_*_executable`
        # None-guards are now belt-and-suspenders (we only enter a branch
        # when the binary resolved), but kept in case PATH changes between
        # the availability probe and the spawn.
        from .provider_config import CODEX, GEMINI, effective_provider_for

        # Per-project role→CLI mapping (project_ns resolved at spawn entry):
        # the same role can be backed by a different CLI in different tabs.
        effective_provider = effective_provider_for(base_role, project=project_ns)

        if effective_provider == GEMINI:
            # The `gemini` role is now powered by Antigravity's `agy` binary
            # (Google retired the standalone Gemini CLI on 2026-06-18). `agy`
            # auto-discovers AGENTS.md/.agents/ — NOT GEMINI.md — so the pane
            # reuses codex's AGENTS.md cheatsheet (one manager, one marker, no
            # write race when codex + gemini share a project cwd).
            from .codex_agents_md import ensure_agents_md
            from .gemini_helper import find_agy_executable

            gemini_bin = find_agy_executable()
            if gemini_bin is None:
                return False, (
                    "agy binary not on PATH. Install the Antigravity CLI from "
                    "https://antigravity.google/download, then run `agy` once to sign in."
                )
            spawn_cwd = cwd or default_cwd_for_role(role_name, project=project_ns) or str(REPO_ROOT)
            ensure_agents_md(spawn_cwd)
            env = _build_pane_env()
            env["TAKKUB_ROLE"] = role_name
            env["TAKKUB_PROJECT"] = project_ns
            inject_user_profile_env(env, project_ns)
            bin_dir = str(REPO_ROOT / "bin")
            # Put agy's own dir on the pane PATH too — the Antigravity
            # installer doesn't reliably register it (find_agy_executable
            # may have resolved the binary via its fixed install location,
            # not PATH), and agy may shell out to itself / companion tools.
            agy_dir = os.path.dirname(gemini_bin)
            env["PATH"] = bin_dir + os.pathsep + agy_dir + os.pathsep + env.get("PATH", "")
            _gem_tok = self._mint_pane_token(env, project_ns, role_name)
            gemini_argv = [
                gemini_bin,
                # yolo: skip per-command approval prompts (parity with codex
                # --ask-for-approval never). Antigravity's flag is the long form.
                "--dangerously-skip-permissions",
            ]
            return self._launch_session(
                pane=pane,
                role_name=role_name,
                project_ns=project_ns,
                spawn_cwd=spawn_cwd,
                argv=gemini_argv,
                env=env,
                pane_tok=_gem_tok,
                label="gemini",
                cwd=cwd,
                project=project,
                _from_auto_respawn=_from_auto_respawn,
                _shard_total=_shard_total,
                auto_trust=True,
            )

        if effective_provider == CODEX:
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
            inject_user_profile_env(env, project_ns)
            bin_dir = str(REPO_ROOT / "bin")
            env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
            _cdx_tok = self._mint_pane_token(env, project_ns, role_name)
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
            return self._launch_session(
                pane=pane,
                role_name=role_name,
                project_ns=project_ns,
                spawn_cwd=spawn_cwd,
                argv=codex_argv,
                env=env,
                pane_tok=_cdx_tok,
                label="codex",
                cwd=cwd,
                project=project,
                _from_auto_respawn=_from_auto_respawn,
                _shard_total=_shard_total,
                codex_exit=True,
                auto_trust=True,
            )

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
                    project_ns,
                    post_compact_brief=post_compact_brief,
                    claude_cwd=spawn_cwd,
                )
        else:
            staging = agent_role_dir(base_role)
            spawn_cwd = cwd or default_cwd_for_role(base_role, project=project_ns) or str(staging)
            # When cwd is a project path, claude auto-discovers the project's
            # CLAUDE.md, not the role's specialist override. Pass the role's
            # markdown to --append-system-prompt-file so the specialist rules
            # always apply regardless of where we land. (Using the *file*
            # variant avoids command-line escaping problems with multiline
            # markdown containing backticks, asterisks, and Thai text.)
            role_md_path = staging / "CLAUDE.md"
            if role_md_path.exists():
                # agent_role_dir() always rewrites CLAUDE.md fresh from the source
                # .claude/agents/<role>.md, so these injections never accumulate.
                # Build the whole appendix, then write once.
                _existing_md = role_md_path.read_text(encoding="utf-8")
                _appendix = ""
                # Issue #33: pointer to Lead's project-memory so the teammate can
                # read domain rules (package manager, ports, vendor patterns) on
                # demand without relying on Lead to echo them in every task spec.
                _mem_path = _resolve_project_memory(lead_cwd(project_ns) or spawn_cwd)
                if _mem_path is not None:
                    _appendix += f"""

---

## 📋 Project memory (Lead's constraint registry)

Lead ของ project นี้มี auto-memory ที่บันทึก domain rules ไว้ที่:

`{_mem_path}`

**อ่านก่อนเริ่มงานที่แตะ:** dependency, lockfile, docker, ports, vendor pattern, หรือ tool ที่โปรเจ็คระบุว่าใช้:

```
Read("{_mem_path}")
```

MEMORY.md เป็น index — แต่ละ entry ชี้ไปยัง memory file ที่อธิบาย rule นั้นๆ อ่านเฉพาะ file ที่เกี่ยวกับงานของคุณ ไม่ต้องอ่านทั้งหมด
"""
                # Per-(role × project) learned memory: this role's OWN accumulated
                # notes for THIS project (conventions, gotchas, decisions; qa: test
                # login/flow). Read-on-spawn + append-on-learn so each role grows
                # into its project instead of starting cold every spawn.
                try:
                    from .role_memory import ensure_role_memory

                    _role_mem = ensure_role_memory(project_ns, base_role)
                except Exception:
                    _role_mem = None
                if _role_mem is not None:
                    # Inline the learned-notes CONTENT (not just a pointer) so the
                    # pane literally sees its project knowledge from token 0 and
                    # cannot skip a Read() under an urgent "เริ่มทันที" task — the
                    # root cause of teammates re-discovering known facts every spawn.
                    # Capped so a large accumulated file can't bloat the prompt; the
                    # pane is told to Read() the full file when truncated.
                    # NOTE: the notes text is *concatenated*, never f-string-
                    # interpolated, because role-memory legitimately contains literal
                    # braces (e.g. Go templates `{{.State.Health.Status}}`) that
                    # would raise on an f-string.
                    try:
                        _mem_text = _role_mem.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        _mem_text = ""
                    # tok-5: a freshly-seeded file is just the skeleton (bare `-`
                    # placeholders, no learned bullets). Inlining the whole empty
                    # skeleton + the long "อย่าเดา/ค้นใหม่" wrapper costs ~100-150
                    # tok/spawn for zero knowledge. When there's no real content
                    # yet, emit a one-line pointer instead; the full inline block
                    # returns the moment the role appends its first note.
                    from .role_memory import has_learned_content

                    if not has_learned_content(_mem_text, project_ns, base_role):
                        # No real notes yet — a one-line pointer instead of dumping
                        # the empty skeleton + long wrapper. The full inline block
                        # below returns the moment this role appends its first note.
                        _appendix += (
                            "\n\n---\n\n"
                            f"## 🧠 Your learned notes ({base_role} · this project)\n\n"
                            "ยังไม่มี learned notes สำหรับโปรเจคนี้ — เมื่อเจอสิ่งที่ไม่ obvious "
                            "(pattern, pitfall, login/flow, decision) → **append สั้นๆ** ลงไฟล์ "
                            f"`{_role_mem}` ด้วย Edit/Write เพื่อให้รอบหน้าเร็วขึ้น "
                            "(เก็บเฉพาะของจริงที่มีค่า อย่าซ้ำ code/git)\n"
                        )
                    else:
                        _MEM_MAX_LINES = 200
                        _mem_all = _mem_text.splitlines()
                        # Keep the TAIL (newest) — notes are appended at the bottom,
                        # so a head slice would drop the freshest learnings first
                        # (#43). role_memory curation normally keeps the file under
                        # this cap; this slice is just a safety net for a large file.
                        _mem_shown = "\n".join(_mem_all[-_MEM_MAX_LINES:])
                        _trunc = (
                            f"\n\n> ⚠️ ตัดมา {_MEM_MAX_LINES}/{len(_mem_all)} บรรทัดท้ายสุด — "
                            f'อ่านเต็มด้วย `Read("{_role_mem}")`'
                            if len(_mem_all) > _MEM_MAX_LINES
                            else ""
                        )
                        _appendix += (
                            "\n\n---\n\n"
                            f"## 🧠 Your learned notes ({base_role} · this project)\n\n"
                            f"ความรู้ที่ **คุณ ({base_role}) สะสมไว้กับโปรเจคนี้** "
                            "(สะสมข้ามรอบงาน) — **นี่คือสิ่งที่คุณรู้เกี่ยวกับโปรเจคนี้แล้ว "
                            "อย่าเดา/ค้นใหม่ในสิ่งที่อยู่ด้านล่างนี้:**\n\n"
                            "<learned-notes>\n"
                            + _mem_shown
                            + "\n</learned-notes>"
                            + _trunc
                            + "\n\n"
                            "**เมื่อเจอสิ่งที่ไม่ obvious** (pattern, pitfall, login/flow, "
                            "decision) ที่ยังไม่มีด้านบน → **append สั้นๆ** ลงไฟล์ "
                            f"`{_role_mem}` ด้วย Edit/Write เพื่อให้รอบหน้าเร็วขึ้น "
                            "เก็บเฉพาะของจริงที่มีค่า อย่าซ้ำ code/git\n"
                        )
                # Big-file hygiene: every teammate gets the same guard the Lead
                # does — a teammate assigned to port a 2.7MB game file would
                # otherwise Read it wholesale and balloon its own per-turn
                # context (re-charged as cache_read each turn). Always-on.
                _appendix += BIG_FILE_GUARD
                # Stale-file race guard (teammate-only): recognise the
                # "File has been modified since read" loop caused by a running
                # dev-server/IDE watcher and stop it instead of retry-looping
                # for minutes. Lead delegates and rarely bulk-edits, so it's
                # omitted there to save spawn tokens.
                _appendix += STALE_FILE_GUARD
                if _appendix:
                    role_md_path.write_text(_existing_md + _appendix, encoding="utf-8")
                role_md_file = str(role_md_path)

        try:
            claude = find_claude_executable()
        except RuntimeError as e:
            return False, str(e)

        env = _build_lead_env() if role_name == LEAD.name else _build_pane_env()
        env["TAKKUB_ROLE"] = role_name
        inject_user_profile_env(env, project_ns)
        # Shard env: let the agent know its instance identity vs behaviour identity.
        # TAKKUB_BASE_ROLE = base role name (loads qa.md, correct Chrome config, etc.)
        # TAKKUB_SHARD     = this shard's 1-based index (None-string when not a shard)
        # TAKKUB_SHARD_TOTAL = total shards in the fan-out group
        env["TAKKUB_BASE_ROLE"] = base_role
        if shard_idx is not None:
            env["TAKKUB_SHARD"] = str(shard_idx)
            if _shard_total > 0:
                env["TAKKUB_SHARD_TOTAL"] = str(_shard_total)
        # Tag the pane with its project so the `takkub` CLI inside the
        # session can stamp every JSON request with `from_project`. The
        # cli_server uses that to scope routing to panes in the *same*
        # project — under the multi-tab refactor a Lead in unirecon
        # mustn't accidentally send to a backend pane that belongs to pms.
        env["TAKKUB_PROJECT"] = project_ns
        # pane_tok is only assigned in the else branch (non-Lead).  Pre-bind to
        # None so the Lead path through _toctou_redefer can pass it safely
        # without triggering UnboundLocalError on a Tier 2 final-gate block.
        pane_tok = None
        # Inject the Lead capability token only into the Lead pane so its
        # takkub CLI can authenticate Lead-only server commands. Teammates
        # don't get this env var — the server will reject their Lead-only
        # requests even if they dial the TCP socket directly.
        if role_name == LEAD.name:
            env["TAKKUB_LEAD_TOKEN"] = self._lead_token
        else:
            # Per-pane capability token — injected into every non-Lead pane so its
            # takkub CLI can authenticate send/done requests. The IPC server derives
            # caller identity from this token instead of trusting the caller-supplied
            # `from`/`from_project` fields, preventing a compromised or forged pane
            # from impersonating another role or project.
            pane_tok = self._mint_pane_token(env, project_ns, role_name)
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
        if base_role == "qa" and "CHROME_BIN" not in env:
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
        _apply_non_interactive_env(env)
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

        # Teammate model tier — picked PER ROLE (see _ROLE_MODEL_TIERS above),
        # not one flat tier for everyone. Lead does orchestration and stays on
        # the user's default model + effort. Teammates default to Sonnet 4.6
        # medium (roughly 1.5-2x Opus speed with enough reasoning for refactors
        # / integrations without subtle-bug rework), except where a miss is
        # expensive: gate roles (reviewer, critic) run Opus high, and
        # correctness-sensitive impl (backend, devops) runs Sonnet high. The
        # cockpit owner is on Claude Max (per-token cost irrelevant), so the
        # only tradeoff for spending a bigger tier is latency — which we accept
        # on the low-frequency gate/correctness roles and avoid on the
        # high-frequency execution roles. The global env vars below override
        # the per-role default for ALL roles at once when explicitly set:
        #
        #   TAKKUB_TEAMMATE_MODEL=""                   → no --model (user default)
        #   TAKKUB_TEAMMATE_MODEL="claude-haiku-4-5"   → force fastest tier everywhere
        #   TAKKUB_TEAMMATE_MODEL="claude-opus-4-8"    → force Opus everywhere
        #   TAKKUB_TEAMMATE_EFFORT=""                  → no --effort
        #   TAKKUB_TEAMMATE_EFFORT="high"              → force high effort everywhere
        if role_name != LEAD.name:
            tier_model, tier_effort, tier_fallback = _teammate_tier(base_role)
            teammate_model = os.environ.get("TAKKUB_TEAMMATE_MODEL", tier_model).strip()
            if teammate_model:
                argv.extend(["--model", teammate_model])
            teammate_effort = os.environ.get("TAKKUB_TEAMMATE_EFFORT", tier_effort).strip()
            if teammate_effort:
                argv.extend(["--effort", teammate_effort])
            # Graceful degradation under load. When the teammate's model is
            # overloaded (HTTP 529) or not found, claude switches to this
            # model for the rest of the session instead of hard-failing the
            # turn (CC 2.1.152 made the switch session-wide; 2.1.144 made it
            # survive /bg + detach). Matters in a multi-pane cockpit where
            # 4-8 panes can hit the Max rate ceiling at the same instant —
            # a falling-back pane keeps working one tier down (per _teammate_tier:
            # Sonnet roles → Haiku, Opus gate roles → Sonnet) rather than
            # erroring mid-task and forcing a respawn. Set
            # TAKKUB_TEAMMATE_FALLBACK="" to disable, or to another model id.
            teammate_fallback = os.environ.get("TAKKUB_TEAMMATE_FALLBACK", tier_fallback).strip()
            if teammate_fallback:
                argv.extend(["--fallback-model", teammate_fallback])
        else:
            # Lead normally rides the user's default model (Opus on this
            # install) with no --model flag. Under a Pro plan that default may
            # be the [1m] 1M-context variant, which Pro can't reach (usage
            # credits required) and which hard-errors the turn. Pin the Lead
            # to a standard-context model in that case (see plan_tier).
            lead_model = _lead_model_override()
            if lead_model:
                argv.extend(["--model", lead_model])
            # Degrade to Sonnet on overload/not-found so orchestration keeps
            # moving during peak load instead of the Lead turn erroring out
            # — the Lead is the single pane the user is actually talking to,
            # so a hard failure there stalls the whole session. Set
            # TAKKUB_LEAD_FALLBACK="" to disable.
            lead_fallback = os.environ.get("TAKKUB_LEAD_FALLBACK", "claude-sonnet-4-6").strip()
            if lead_fallback:
                argv.extend(["--fallback-model", lead_fallback])

        # Explicit plugin allowlist (skip the broken claude-obsidian hook).
        # Set TAKKUB_EXTRA_PLUGINS env var to a `;`-separated list of plugin
        # root dirs (must each contain `.claude-plugin/plugin.json`) to add
        # more, or set it to empty string to suppress the defaults.
        plugin_default = ";".join(_default_plugin_dirs(base_role))
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
            from .shared_dev_tools import browser_profile_mcp_config_path

            # Every pane: browser roles (qa/critic/designer) get a PERSISTENT
            # per-(project, role[, shard]) browser profile so the browser remembers
            # its session/cookies across runs (no more re-login every test) and
            # parallel shards don't collide on one Chrome profile lock (#39).
            # Non-browser roles fall through to their plain role-variant config.
            mcp_cfg = browser_profile_mcp_config_path(base_role, shard_idx, project_ns)
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
            # Comma-join, NOT space: a space inside this single argv element
            # makes subprocess.list2cmdline (pty_session.spawn) wrap the value
            # in double quotes, and the ConPTY → claude.exe argument round-trip
            # on Windows leaks those quotes into the rule names — claude then
            # warns `Permission deny rule "Task / AskUserQuestion" matches no
            # known tool` on every spawn. The names themselves are valid; only
            # the leaked quotes break the match. Comma needs no quoting and
            # --disallowed-tools accepts comma- or space-separated lists.
            argv.extend(["--disallowed-tools", ",".join(denied)])

        # Session resume: if this same role exited recently from the same
        # cwd and we have its session UUID, use --resume <uuid> so claude
        # rejoins the exact conversation without CWD-based disambiguation.
        # This avoids bleed where Lead and a teammate sharing the same cwd
        # could each inherit the other's history via --continue.
        # On a fresh spawn (no prior UUID or expired window), generate a new
        # UUIDv4 and pass --session-id so claude tracks the session from the start.
        resumed = False
        _ekey_spawn = _exit_key(project_ns, role_name)
        _ps_pre = self._pane_state.get(_ekey_spawn)
        prior_uuid = _ps_pre.session_uuid if _ps_pre is not None else None
        prior_uuid_cwd = _ps_pre.session_uuid_cwd if _ps_pre is not None else ""
        prior_exit = self._recent_exits.get(_ekey_spawn)
        can_resume = (
            prior_uuid is not None
            and prior_uuid_cwd == spawn_cwd
            and prior_exit is not None
            and (time.time() - prior_exit.get("ts", 0)) < RESUME_WINDOW_SEC
        )
        if can_resume:
            argv.extend(["--resume", prior_uuid])
            resumed = True
        else:
            new_uuid = str(_uuid.uuid4())
            argv.extend(["--session-id", new_uuid])
            _ps_new = self._ps(_ekey_spawn)
            _ps_new.session_uuid = new_uuid
            _ps_new.session_uuid_cwd = spawn_cwd

        session = PtySession(cols=_PANE_COLS, rows=_PANE_ROWS, parent=self)
        _t_path = _build_transcript_path(project_ns, role_name)
        pane._transcript_path = _t_path
        self._spawn_in_progress = True
        try:
            # Tier 2 final re-sample: check InSendMessageEx immediately
            # before the native ConPTY call to narrow the TOCTOU window.
            if not self._final_gate_clear():
                session.setParent(None)
                session.deleteLater()
                self._toctou_redefer(
                    role_name,
                    cwd,
                    project,
                    project_ns,
                    _from_auto_respawn,
                    _shard_total,
                    pane_tok=pane_tok,
                )
                return True, f"{role_name} spawn deferred (final re-sample blocked)"
            _t0 = time.time()
            session.spawn(argv=argv, cwd=spawn_cwd, env=env, transcript_path=_t_path)
            _log_event(
                "spawn_native_ms",
                role=role_name,
                project=project_ns,
                ms=int((time.time() - _t0) * 1000),
            )
            pane.attach_session(session, cwd=spawn_cwd)
            # Record exits so the auto-respawn watcher knows which project
            # namespace owned the pane that just died.  Capture the session so
            # stale exit signals from an old session don't trigger respawn on a
            # replacement that's already attached.
            _sess_claude = session
            session.processExited.connect(
                lambda _code, r=role_name, c=spawn_cwd, p=project_ns, s=_sess_claude: (
                    self._on_session_exit(r, c, p)
                    if (pp := self._panes_by_project.get(p, {}).get(r)) is not None
                    and pp.session is s
                    else None
                )
            )
            # forget the prior exit record now that we've spawned successfully
            if _ekey_spawn in self._recent_exits:
                del self._recent_exits[_ekey_spawn]

            # The TAKKUB_PANE_TOKEN was already injected into env (above the
            # session.spawn call) and registered in self._pane_tokens at that time.
            # Nothing more to do here.

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
            # Record resume decision as a structured flag so _auto_respawn and
            # _do_respawn can read it directly without parsing the message string.
            # (Fix 1: eliminates the "(resumed)" in msg string-coupling fragility.)
            self._ps(_ekey_spawn).last_spawn_resumed = resumed
            suffix = " (resumed)" if resumed else ""
            # If a codex/gemini role reached the claude spawn path, its provider
            # was unavailable (toggled off or not installed) and claude is
            # standing in. Surface that so the user isn't surprised the pane
            # talks like Claude.
            if role_name in (CODEX, GEMINI):
                suffix += " — claude substitute (provider unavailable)"
            return True, f"{role_name} spawned in {spawn_cwd}{suffix}"
        except Exception as e:
            # Revoke the token if spawn failed — it was registered before the
            # try block so the pane never actually came up to use it.
            if role_name != LEAD.name:
                getattr(self, "_pane_tokens", {}).pop(pane_tok, None)
            return False, f"failed to spawn claude: {e}"
        finally:
            self._spawn_in_progress = False
            self._drain_spawn_queue()

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
        _ps_cx = self._pane_state.get(ekey)
        spawn_ts = _ps_cx.codex_spawn_ts if _ps_cx is not None else None

        # Guard: drop stale exit BEFORE resetting codex_spawn_ts.
        # If a new session has already spawned and registered its own spawn_ts,
        # clearing it here would clobber its crash-diagnostic window.
        _pane_cdx = self._panes_by_project.get(project, {}).get(role_name)
        if _pane_cdx is not None and _pane_cdx.session is not session:
            return

        if _ps_cx is not None:
            _ps_cx.codex_spawn_ts = None
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

        # Revoke pane token on session death so a crashed or exited pane cannot
        # continue to authenticate send/done after it terminates.
        _ptoks = getattr(self, "_pane_tokens", {})
        for _t in [t for t, v in list(_ptoks.items()) if v == (project, role_name)]:
            _ptoks.pop(_t, None)

        pane = self._panes_by_project.get(project, {}).get(role_name)
        if pane is None or pane.state != "exited":
            return

        key = f"{project}::{role_name}"
        ps = self._ps(key)
        attempts = ps.auto_respawn_attempts
        if attempts >= AUTO_RESPAWN_MAX:
            _log_event(
                "auto_respawn_capped",
                role=role_name,
                project=project,
                attempts=attempts,
            )
            # Bug-3 fix: notify Lead so the operator knows the pane gave up and
            # auto-chain doesn't deadlock waiting for a done event that never comes.
            self._warn_lead_respawn_capped(role_name, project)
            _had_ac_cap = ps.auto_chain
            ps.auto_chain = False
            ps.last_assigned_task = None
            # A capped pane never sends done; if it was the last auto-chain
            # blocker, release the chain so completed siblings still get verified
            # instead of the verify hop deadlocking forever (bug-1 orch).
            self._maybe_fire_auto_chain_handoff(project, _had_ac_cap)
            # Pipeline: a capped pane is gone for good. If it belonged to a
            # pipeline hop (e.g. a stuck-recovered hop role that then crash-looped
            # to the cap), mark it failed + advance so the hop doesn't stall
            # forever on a pane that will never report done. Mirrors the re-honor
            # in the stuck-recovery respawn-fail path (_do_respawn).
            pl_run_id = ps.pipeline_run_id
            if pl_run_id:
                pl_key = f"{project}::{pl_run_id}"
                pl_run = self._pipeline_runs.get(pl_key)
                if pl_run is not None and not pl_run.closed:
                    pl_run.hop_pending.discard(role_name)
                    pl_run.hop_failed.add(role_name)
                    if not pl_run.hop_pending:
                        self._advance_pipeline(project, pl_key, pl_run)
            return
        ps.auto_respawn_attempts = attempts + 1
        # Exponential back-off: a pane that keeps crashing (deterministic bug
        # triggered by its replayed task) shouldn't re-spawn at a fixed fast
        # interval and burn tokens. Each attempt waits 2x longer (issue #23).
        backoff_ms = AUTO_RESPAWN_DELAY_MS * (2**attempts)
        _log_event(
            "auto_respawn_scheduled",
            role=role_name,
            project=project,
            attempt=attempts + 1,
            delay_ms=backoff_ms,
        )
        QTimer.singleShot(
            backoff_ms,
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
        ok, msg = self.spawn(role_name, cwd=cwd, project=project, _from_auto_respawn=True)
        _log_event("auto_respawn_done", role=role_name, project=project, ok=ok, msg=msg[:160])
        if ok:
            # Bug-5 fix: a resumed session already holds the task in claude's
            # conversation history — re-pasting it risks duplicate work on
            # non-idempotent steps (file creates, migrations, etc.).
            # Fix 1: read structured flag set by spawn() instead of parsing msg.
            _ps_ar = self._pane_state.get(_exit_key(project, role_name))
            spawn_resumed = _ps_ar.last_spawn_resumed if _ps_ar is not None else False
            cached_task = _ps_ar.last_assigned_task if _ps_ar is not None else None
            if cached_task and not spawn_resumed:
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
        project: str | None = None,
    ) -> tuple[bool, str]:
        ok, msg = self.spawn(role_name, cwd=cwd, project=project, _shard_total=shard_total)
        if not ok:
            # The CLI already acked "task queued" to the Lead's shell before
            # this async spawn ran, so a failure here is invisible unless we
            # say so. Tell the Lead the task never landed (#26).
            self._warn_lead_spawn_failed(role_name, project, msg)
            # #5: record spawn-failed shard into its group so the aggregate
            # doesn't orphan forever (mirrors _warn_lead_respawn_capped path).
            if shard_total > 0:
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
        if requires_commit:
            ps_assign.requires_commit_on_done = True
        if auto_chain:
            ps_assign.auto_chain = True
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
            on_resend=lambda rem, r=to_role: _log_event(
                "send_enter_resend", project=project_ns, role=r, remaining=rem
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
        # Non-shard panes (had_shard_total == 0) use the normal notice path.
        if had_shard_total == 0:
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
            sessions = vault / "01-Projects" / safe_project / "sessions"
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
        # project_ns is already validated above so safe to use directly.
        vault = _resolve_vault_dir()
        if vault is not None:
            try:
                vault_sessions = vault / "01-Projects" / project_ns / "sessions"
                vault_sessions.mkdir(parents=True, exist_ok=True)
                stamp = now.strftime("%Y-%m-%dT%H%M%S")
                (vault_sessions / f"{stamp}-lead.md").write_text(body, encoding="utf-8")
            except OSError:
                pass

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
        briefs_dir = vault / "07-AI-Command-Center" / "briefs"
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
                    self._maybe_surface_malformed_xml(key, name, project_name, pane, now)

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
        pane = self.panes.get(role_name)
        if pane is None or pane.session is None:
            return
        pane.session.write(data)
