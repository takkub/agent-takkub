"""SpawnEngineMixin — pane registry, spawn arbiter, session lifecycle.

Mixed into ``Orchestrator`` — never imported by main_window, app, or cli.
See ``docs/architecture/godfile-map.md`` 'spawn_engine' cluster.

STATE contract: all 7 spawn-engine dicts live in ``Orchestrator._registry``
(a ``PaneRegistry`` dataclass).  Backward-compat properties on this mixin
expose them as ``self._pane_state``, ``self._recent_exits``, etc. so every
access site works unchanged.  This mixin never creates ``_registry``; it is
created by ``Orchestrator.__init__``.
"""

from __future__ import annotations

import collections
import hashlib
import logging
import os
import pathlib
import re
import secrets
import sys
import time
import uuid as _uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from PyQt6.QtCore import QTimer

from .agent_pane import AgentPane
from .claude_auth_config import apply_claude_auth_overrides
from .config import (
    CLI_BIN_DIR,
    DATA_HOME,
    REPO_ROOT,
    agent_role_dir,
    default_cwd_for_role,
    lead_cwd,
    validate_name,
)
from .headless_pane import HeadlessPane
from .lead_context import (
    BIG_FILE_GUARD,
    STALE_FILE_GUARD,
    _allowed_project_roots,
    _default_plugin_dirs,
    _render_lead_context,
    render_lead_agents_md,
)
from .orchestrator_text import (
    _cwd_within_project,
    _exit_key,
    _lead_model_override,
    _log_event,
    _resolve_project_memory,
    _teammate_tier,
)
from .pane_env import (
    _apply_artifacts_dir,
    inject_user_profile_env,
)
from .pipeline_executor import _split_shard
from .pty_session import PtySession
from .roles import LEAD

if TYPE_CHECKING:
    from .provider_spec import ProviderSpec

_log = logging.getLogger(__name__)

# ── test-mockability shim ────────────────────────────────────────
# Tests written before the spawn_engine extraction patch symbols at
# agent_takkub.orchestrator.* (e.g. `patch("agent_takkub.orchestrator.PtySession")`).
# This helper looks up these names from orchestrator's module-dict at CALL TIME
# so those patches still take effect inside spawn_engine methods.
# No static `import orchestrator` → no circular import; import-linter safe.
_SENTINEL = object()


def _from_orch(name: str):
    """Return `name` from orchestrator's live module dict (respects mock.patch),
    falling back to spawn_engine's own module-level binding."""
    import sys

    _orch = sys.modules.get("agent_takkub.orchestrator")
    if _orch is not None:
        val = getattr(_orch, name, _SENTINEL)
        if val is not _SENTINEL:
            return val
    return globals()[name]


def _append_provider_effort(
    argv: list[str],
    spec: ProviderSpec,
    effort: str,
) -> None:
    """Append one provider's session-scoped reasoning-effort override.

    ``effort_flag=None`` is the explicit unsupported-provider contract. Most
    providers receive ``<flag> <effort>``; config-backed providers such as
    Codex receive ``<flag> <config-key>=<effort>``.
    """
    if not effort or not spec.effort_flag:
        return
    value = f"{spec.effort_config_key}={effort}" if spec.effort_config_key is not None else effort
    argv.extend([spec.effort_flag, value])


_CURRENT_TASK_BEGIN = "\n\n<!-- takkub-current-spawn-task:start -->"
_CURRENT_TASK_END = "<!-- takkub-current-spawn-task:end -->"
_CURRENT_TASK_TRIGGER = "Start the current task from the one-shot system-prompt block now."


def _prepare_spawn_system_prompt(
    role_md_file: str | None,
    task: str | None,
    *,
    output_file: str | None = None,
) -> str | None:
    """Regenerate one spawn's appended system-prompt file.

    The role/Lead context file is rebuilt by the caller on every spawn.  This
    helper still strips its own prior block defensively before optionally
    appending *task*, so a retry/resume can never inherit an old task if a
    context renderer stops rewriting the file in the future.

    ``output_file`` receives a pane-scoped copy so concurrent shards sharing
    one role staging directory cannot overwrite each other's task. Rewriting
    that deterministic copy on every spawn also removes the prior task before
    a respawn/resume. The returned path is safe to pass to
    ``--append-system-prompt-file``.
    ``None`` means the file could not be prepared and the caller must use the
    existing pointer delivery.
    """
    if not role_md_file:
        return None
    path = pathlib.Path(role_md_file)
    try:
        existing = path.read_text(encoding="utf-8")
        base = existing.split(_CURRENT_TASK_BEGIN, 1)[0].rstrip()
        if task is None:
            rendered = base + ("\n" if base else "")
        else:
            rendered = (
                base
                + _CURRENT_TASK_BEGIN
                + "\n\n"
                + "## Current task for this spawn (one-shot)\n\n"
                + "The block below is the current task for this newly spawned pane. "
                + "Execute it now. It is not a standing instruction and must not be "
                + "reused on a later respawn/resume unless that spawn explicitly "
                + "contains a new block.\n\n"
                + "------ task ------\n"
                + task
                + "\n\n"
                + _CURRENT_TASK_END
                + "\n"
            )
        target = pathlib.Path(output_file) if output_file else path
        if target != path or rendered != existing:
            target.write_text(rendered, encoding="utf-8")
        return str(target)
    except OSError:
        _log.exception("could not prepare one-shot spawn task in %s", role_md_file)
        return None


# ── spawn constants ──────────────────────────────────────────────

RESUME_WINDOW_SEC = 5 * 60  # respawn within this window → claude --resume <uuid>


def _normalize_cwd_for_compare(cwd: str) -> str:
    """Normalize a cwd string for the 5-min auto-resume equality check (L5).

    Windows case-insensitivity + multiple string spellings of the same
    directory (`/` vs `\\`, trailing slash, short/long name), or a POSIX
    symlink, can make a raw string compare miss two paths that are actually
    the same directory — silently disabling `--resume` recovery (a fresh
    session starts instead) even though this is genuinely the same pane
    respawning into the same cwd. `Path.resolve()` collapses separators/
    relative segments/symlinks; `os.path.normcase()` folds case (Windows
    only — a no-op on POSIX, where case is significant). Falls back to a
    normcase'd raw string on any resolve failure (e.g. the dir no longer
    exists) rather than raising — a transient FS hiccup should degrade to
    the old exact-match behaviour for that one comparison, not crash a spawn.
    """
    try:
        return os.path.normcase(str(pathlib.Path(cwd).resolve()))
    except (OSError, ValueError):
        return os.path.normcase(cwd)


_SAFE_SESSION_UUID_RE = re.compile(r"^[0-9A-Za-z_-]+$")


def _skill_roots_for_project(project_ns: str) -> list[pathlib.Path]:
    """Where to look for real `.claude/skills/` at spawn time — every
    configured path of `project_ns` first (project-specific skills win a
    name collision), plus the cockpit's own checkout as a fallback (same
    roots `settings_window._new_role_skill_roots` scans, so the Skill
    Matrix and the actual spawn-time injection agree on what "exists").
    """
    roots = list(_allowed_project_roots(project_ns)) if project_ns else []
    roots.append(REPO_ROOT)
    return roots


def _resume_uuid_matches_cwd(project_ns: str, session_uuid: str, cwd: str) -> bool:
    """W3: verify a caller-chosen resume session (mobile session picker)
    actually belongs to `cwd` before handing it to `--resume`.

    Same cwd-disambiguation guarantee the 5-min auto-resume path enforces via
    the in-memory pane-state cache — but this checks the JSONL store instead,
    since a user-picked session may belong to a pane that closed long ago (not
    something this run's `_pane_state` cache would still know about). Claude
    Code encodes a session's launch cwd into its jsonl parent directory name,
    so this checks for `<session_uuid>.jsonl` inside the exact encoded dir for
    `cwd` (`token_meter.session_project_dir_for_cwd`) — a read-only, forgery-
    proof check with no dependency on any in-memory state. Encoding `cwd`
    forward and checking the exact dir (rather than globbing every project
    dir and reverse-decoding names to compare) avoids `decode_project_dir()`'s
    lossiness: it maps every non-alnum char to '-', so a cwd containing '-',
    '_', '.', or a space can't be told apart from an encoded separator once
    decoded.

    `session_uuid` may originate from an unvalidated remote request (F1 fix:
    the old `base.glob(f"*/{session_uuid}.jsonl")` treated a `..` segment as a
    literal child name to match, so it was traversal-safe; the `Path / str`
    join below is not, so any path-separator/`..` shaped value is rejected
    up front before it ever reaches the filesystem).
    """
    if not _SAFE_SESSION_UUID_RE.match(session_uuid):
        return False

    from .token_meter import session_project_dir_for_cwd
    from .user_profile import config_dir_for

    try:
        proj_dir = session_project_dir_for_cwd(config_dir_for(project_ns), cwd)
    except OSError:
        return False
    return (proj_dir / f"{session_uuid}.jsonl").is_file()


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

# Dedup spawn_still_blocked log entries: only emit once per episode, then once
# every SPAWN_BLOCK_WARN_AFTER_S seconds while the block persists (#64).
SPAWN_BLOCK_WARN_AFTER_S = 5.0


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
    # _last_assigned_task: FULL composed task text (never a pointer); replayed
    # after crash-respawn regardless of whether the pane was only pasted a
    # pointer at assign time (issue #1 — file-based task handoff).
    last_assigned_task: str | None = None
    # last_assigned_task_file: path to the on-disk task-handoff file written
    # by _assign_dispatch when the composed payload was long enough to paste
    # a pointer instead of the full text. None when the task pasted directly
    # (short task, or the write failed and we fell back to inline). Forward
    # slashes always (`takkub task show` and the pointer text share this
    # value verbatim).
    last_assigned_task_file: str | None = None
    # One-shot delivery staged by _assign_dispatch before spawn(). Claude can
    # preload it through --append-system-prompt-file; providers without an
    # equivalent file-backed system-prompt flag keep the pointer/PTY flow.
    # State: "" (none), "requested" (assign prepared it), "pending" (spawn
    # accepted it), "delivered" (system prompt), "fallback" (pointer).
    spawn_initial_task: str | None = None
    spawn_initial_task_fallback: str | None = None
    spawn_initial_prompt_file: str | None = None
    spawn_initial_task_state: str = ""
    # assign_ts: wall-clock when this pane's current task was dispatched
    # (_assign_dispatch). done() reads this BEFORE popping the PaneState so it
    # can scan the artifacts dir for screenshots newer than the assignment
    # (issue #5 — screenshot evidence auto-attach). 0.0 = never assigned.
    assign_ts: float = 0.0
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
    # _plan_fanout: pending QA plan-then-fan-out config on a PLANNER pane
    # (None = not a planner). Set by assign(..., plan=True); read in done() to
    # spawn the shard fan-out from the plan file the planner just wrote.
    # Shape: {"shards": int, "cwd": str|None, "task": str, "plan_file": str}
    plan_fanout: dict | None = None
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
    # splash_dismiss_ts: last time Enter was sent to dismiss an update-splash modal (#62)
    splash_dismiss_ts: float = 0.0
    # stop_gate_notified: True once the Stop-hook done-gate has already blocked
    # this pane once for its CURRENT assignment. stop_hook_active alone only
    # stops Claude Code from recursively re-entering the same Stop event — it
    # does NOT stop a fresh Stop event a few seconds later if the model ignores
    # the nudge and stops again, which would otherwise block forever (codex
    # cross-check 2026-07-02, docs/reviews/2026-07-02-claude-hooks-design-crosscheck.md).
    # Reset on every fresh spawn (see spawn()'s fresh-spawn-clear block), on a
    # new assign(), and on a Lead→teammate send() that starts new real work —
    # each gets exactly one nudge. done()/close() pop the whole PaneState, which
    # implicitly clears this too.
    stop_gate_notified: bool = False
    # Stuck-paste reaper: a "working" pane sitting at the ready prompt with a
    # "[Pasted text +N lines]" placeholder never submitted (Enter swallowed at
    # spawn under parallel load and the delivery self-heal exhausted).
    # pending_input_since = when the stuck state was first observed;
    # last_pending_submit_ts gates the recovery-CR cadence;
    # pending_submit_attempts caps recovery so a pane a CR can't fix
    # doesn't get poked forever.
    pending_input_since: float | None = None
    last_pending_submit_ts: float = 0.0
    pending_submit_attempts: int = 0
    # worktree: set by assign(..., isolation="worktree") when a per-pane git
    # worktree was created (issue #81). Shape = WorktreeInfo.as_dict()
    # {"path","branch","base_sha","git_root"}; None = shared cwd (default).
    # Read (captured before the pop) in done()/close() to finalize the worktree:
    # propose a merge to Lead if the branch has commits, else safe-remove it.
    worktree: dict | None = None
    # ── auto-resume (🌙, limit_autoresume.py) — park/wake bookkeeping ────────
    # limit_parked: True while this pane is currently parked awaiting its
    # usage-limit window to reset (auto-resume ON path only).
    limit_parked: bool = False
    # limit_confirm_pending: True while a background fetch is confirming
    # signal (b) (limit_status utilization) before parking — guards against
    # firing a second confirm fetch on every watchdog tick while one is
    # already in flight for the same episode.
    limit_confirm_pending: bool = False
    # limit_park_rounds: park→wake cycles used so far for the CURRENT
    # assigned task (capped at auto_resume.MAX_PARK_ROUNDS). Reset to 0 on
    # every fresh assign() — a new task gets a fresh budget.
    limit_park_rounds: int = 0
    # limit_park_wake_ts: epoch of the last auto-resume wake injection, or 0.0
    # if never woken for this task. Used to detect "hit the limit again
    # within auto_resume.RELIMIT_GRACE_S of waking" — a signal the window
    # (or the task) is systemically stuck, at which point we stop retrying.
    limit_park_wake_ts: float = 0.0
    # limit_park_stopped: True once the round cap or the re-limit grace
    # tripped — the watchdog leaves auto-resume alone for this pane (falls
    # back to the pre-existing notify-only behaviour) until a new task is
    # assigned.
    limit_park_stopped: bool = False
    # shell_open_dialog_notified: True once the transcript watchdog has
    # warned Lead that this pane's transcript shows the Windows "How do you
    # want to open this file?" ShellExecute marker (issue #104) — a shell
    # one-liner mangled a bare file path into command position. One nudge
    # per pane's current assignment is enough; the dialog doesn't repeat the
    # marker text, so re-scanning after the first hit is just wasted I/O.
    shell_open_dialog_notified: bool = False


@dataclass
class PaneRegistry:
    """Groups all 7 spawn-engine state dicts under one object.

    ``Orchestrator.__init__`` creates a single ``self._registry = PaneRegistry()``
    instead of 7 separate dict attributes.  Backward-compat properties on
    ``SpawnEngineMixin`` (``_pane_state``, ``_recent_exits``, …) delegate to the
    fields here so every existing access site works unchanged.

    Lifecycle notes (preserved from the original per-attribute comments):
    - ``pane_state``: popped atomically by close()/done(); NOT persisted across restart.
    - ``recent_exits``: NOT cleared by close() — persists so _do_respawn can find the
      cwd entry after close() fires during stuck-recover; only cleared by spawn().
    - ``pane_tokens``: entries removed when pane is closed; never written to disk.
    - ``spawn_in_progress``: True while a ConPTY session.spawn() is executing.
    - ``spawn_deferred``: per-(project::role) set; prevents duplicate QTimer callbacks.
    - ``spawn_queue``: FIFO — serialises ConPTY construction so only one runs at a time.
    - ``panes_by_project``: project-namespaced pane registry (multi-tab isolation).
    """

    pane_state: dict = field(default_factory=dict)
    recent_exits: dict = field(default_factory=dict)
    pane_tokens: dict = field(default_factory=dict)
    spawn_queue: collections.deque = field(default_factory=collections.deque)
    spawn_deferred: set = field(default_factory=set)
    spawn_in_progress: bool = False
    panes_by_project: dict = field(default_factory=dict)


def _teammate_disallowed_tools() -> list[str]:
    """Tools hard-blocked for teammate panes via claude ``--disallowedTools``.

    Default blocks ``Task`` so a teammate can't spawn invisible subagents — the
    cockpit policy "ห้าม spawn subagent" (CLAUDE.md), which until now was enforced
    only by the role-prompt and a model could ignore. A teammate fanning out its
    own Task subagents burns tokens and does work the cockpit can't see in a pane.

    Override or clear via ``TAKKUB_TEAMMATE_DISALLOWED_TOOLS`` (space/comma-
    separated tool names; empty string disables the block entirely). Only
    teammates are restricted — the Lead orchestrates via the ``takkub`` CLI, not
    the Task tool, and is left unrestricted.
    """
    raw = os.environ.get("TAKKUB_TEAMMATE_DISALLOWED_TOOLS", "Task").strip()
    return raw.replace(",", " ").split()


def _remap_pinned_model(model: str, env: dict[str, str]) -> str:
    """Translate a concrete ``claude-*`` model pin through the profile's remap.

    When a pane's Claude auth profile defines an ``ANTHROPIC_DEFAULT_<TIER>_MODEL``
    remap (proxy setups that only serve non-Anthropic model ids — e.g. a gateway
    exposing ``ocg/deepseek-v4-pro``), an explicit ``--model claude-sonnet-5``
    *bypasses* that remap: the remap only rewrites the bare ``sonnet``/``opus``/
    ``haiku`` tier alias, whereas a concrete id is sent verbatim. The proxy then
    sees a ``claude-*`` model, routes it to provider ``anthropic``, and — having
    no Anthropic credentials — returns ``404 model_not_found``. That killed every
    teammate pane (each pinned a tier model) while the Lead (no ``--model``,
    rides the alias) kept working.

    So classify the pinned model's tier by name and, if this pane's ``env`` (post
    ``apply_claude_auth_overrides``) carries the matching remap var, return the
    remapped value. With no remap present (the normal, non-proxy case) the pin is
    returned unchanged, so behavior is identical off-proxy.
    """
    if not model:
        return model
    lowered = model.lower()
    if "opus" in lowered:
        tier = "OPUS"
    elif "sonnet" in lowered:
        tier = "SONNET"
    elif "haiku" in lowered:
        tier = "HAIKU"
    else:
        # Already a proxy-native / non-Anthropic id (e.g. ocg/deepseek-v4-pro).
        return model
    remapped = env.get(f"ANTHROPIC_DEFAULT_{tier}_MODEL", "").strip()
    return remapped or model


class SpawnEngineMixin:
    """Pane registry and session spawn/lifecycle mixin.

    Mixed into ``Orchestrator(SpawnEngineMixin, ...)``.  All state lives in
    ``Orchestrator.__init__`` — this mixin only provides the methods that
    operate on that state.
    """

    # ── PaneRegistry backward-compat properties ──────────────────────────────
    # Delegate to self._registry so every existing access site
    # (self._pane_state[...], self._spawn_in_progress = True, …) works unchanged.
    # Setters handle all reassignment patterns found in spawn() and _ps().
    # If _registry doesn't exist yet (test fixtures using Orchestrator.__new__
    # without __init__), the setter creates it on first write.

    @property
    def _pane_state(self):  # type: ignore[override]
        return self._registry.pane_state

    @_pane_state.setter
    def _pane_state(self, v):
        try:
            self._registry.pane_state = v
        except (AttributeError, RuntimeError):
            object.__setattr__(self, "_registry", PaneRegistry())
            self._registry.pane_state = v

    @property
    def _recent_exits(self):  # type: ignore[override]
        return self._registry.recent_exits

    @_recent_exits.setter
    def _recent_exits(self, v):
        try:
            self._registry.recent_exits = v
        except (AttributeError, RuntimeError):
            object.__setattr__(self, "_registry", PaneRegistry())
            self._registry.recent_exits = v

    @property
    def _pane_tokens(self):  # type: ignore[override]
        return self._registry.pane_tokens

    @_pane_tokens.setter
    def _pane_tokens(self, v):
        try:
            self._registry.pane_tokens = v
        except (AttributeError, RuntimeError):
            object.__setattr__(self, "_registry", PaneRegistry())
            self._registry.pane_tokens = v

    @property
    def _spawn_queue(self):  # type: ignore[override]
        return self._registry.spawn_queue

    @_spawn_queue.setter
    def _spawn_queue(self, v):
        try:
            self._registry.spawn_queue = v
        except (AttributeError, RuntimeError):
            object.__setattr__(self, "_registry", PaneRegistry())
            self._registry.spawn_queue = v

    @property
    def _spawn_deferred(self):  # type: ignore[override]
        return self._registry.spawn_deferred

    @_spawn_deferred.setter
    def _spawn_deferred(self, v):
        try:
            self._registry.spawn_deferred = v
        except (AttributeError, RuntimeError):
            object.__setattr__(self, "_registry", PaneRegistry())
            self._registry.spawn_deferred = v

    @property
    def _spawn_in_progress(self):  # type: ignore[override]
        return self._registry.spawn_in_progress

    @_spawn_in_progress.setter
    def _spawn_in_progress(self, v):
        try:
            self._registry.spawn_in_progress = v
        except (AttributeError, RuntimeError):
            object.__setattr__(self, "_registry", PaneRegistry())
            self._registry.spawn_in_progress = v

    @property
    def _panes_by_project(self):  # type: ignore[override]
        return self._registry.panes_by_project

    @_panes_by_project.setter
    def _panes_by_project(self, v):
        try:
            self._registry.panes_by_project = v
        except (AttributeError, RuntimeError):
            object.__setattr__(self, "_registry", PaneRegistry())
            self._registry.panes_by_project = v

    # ──────────────────────────────────────────────────────────────
    # per-pane state helpers
    # ──────────────────────────────────────────────────────────────
    def _ps(self, key: str) -> PaneState:
        """Get-or-create the PaneState for *key* (``"{project}::{role}"``).

        Callers that only need to *read* without creating an entry should use
        ``self._pane_state.get(key)`` and guard against None.

        Lazily initialises ``_registry`` (and thus ``_pane_state``) so test
        fixtures that create a bare ``Orchestrator.__new__`` instance without
        running ``__init__`` still work.
        """
        try:
            d = self._pane_state
        except (AttributeError, RuntimeError):
            d = {}
            self._pane_state = d  # setter creates _registry on first write
        try:
            return d[key]
        except KeyError:
            ps = PaneState()
            d[key] = ps
            return ps

    # ──────────────────────────────────────────────────────────────
    # registration (main_window builds panes and registers them)
    # ──────────────────────────────────────────────────────────────
    def register_pane(self, pane: AgentPane | HeadlessPane, project: str | None = None) -> None:
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
        resume_uuid: str | None = None,
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
            # Dedup: log once per episode, then every SPAWN_BLOCK_WARN_AFTER_S (#64).
            _now = time.time()
            _bts: dict = getattr(self, "_spawn_blocked_first_ts", None)  # type: ignore[assignment]
            if _bts is None:
                _bts = {}
                self._spawn_blocked_first_ts = _bts
            if deferred_key not in _bts or (_now - _bts[deferred_key]) >= SPAWN_BLOCK_WARN_AFTER_S:
                _log_event("spawn_still_blocked", role=role_name, project=project_ns)
                _bts[deferred_key] = _now
            QTimer.singleShot(
                50,
                lambda r=role_name, c=cwd, p=project, a=_from_auto_respawn, s=_shard_total, u=resume_uuid: (
                    self._retry_deferred_spawn(r, c, p, a, s, u)
                ),
            )
            return

        # Gate cleared: clear the block-episode tracker for this key.
        _bts2: dict | None = getattr(self, "_spawn_blocked_first_ts", None)  # type: ignore[assignment]
        if _bts2 is not None:
            _bts2.pop(deferred_key, None)

        # Gate cleared: wait ~35 ms (1 event-loop turn) then re-check to
        # close the check-to-call race, then re-enter spawn() which verifies once more.
        QTimer.singleShot(
            35,
            lambda r=role_name, c=cwd, p=project, a=_from_auto_respawn, s=_shard_total, u=resume_uuid: (
                self.spawn(r, cwd=c, project=p, _from_auto_respawn=a, _shard_total=s, resume_uuid=u)
            ),
        )

    def _drain_spawn_queue(self) -> None:
        """Pop and schedule the next queued spawn after the current one finishes."""
        _queue = getattr(self, "_spawn_queue", None)
        if not _queue:
            return
        # Tolerate a legacy 5-item entry (no resume_uuid) — some call sites /
        # tests still push the pre-W3 shape.
        item = _queue.popleft()
        resume_uuid = item[5] if len(item) > 5 else None
        role, cwd, project, from_auto_respawn, shard_total = item[:5]
        project_ns = self._resolve_project(project)
        pane = self._project_panes(project_ns).get(role)
        if pane is not None and pane.session is not None and pane.session.is_alive:
            self._drain_spawn_queue()
            return
        _log_event("spawn_queue_drain", role=role, project=project_ns)
        QTimer.singleShot(
            0,
            lambda r=role, c=cwd, p=project, a=from_auto_respawn, s=shard_total, u=resume_uuid: (
                self.spawn(r, cwd=c, project=p, _from_auto_respawn=a, _shard_total=s, resume_uuid=u)
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
        resume_uuid: str | None = None,
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
            lambda r=role_name, c=cwd, p=project, a=_from_auto_respawn, s=_shard_total, u=resume_uuid: (
                self._retry_deferred_spawn(r, c, p, a, s, u)
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

    def _finish_spawn_initial_task(
        self,
        role_name: str,
        project_ns: str,
        *,
        preloaded: bool,
    ) -> None:
        """Finalize a one-shot task accepted by this native spawn.

        A successful system-prompt preload needs only a tiny turn-start trigger,
        never the task body or pointer. If the prompt file could not be
        prepared, or the effective provider changed while a spawn was deferred,
        deliver the already-created handoff pointer through the established
        ready-polling path instead. Clearing the payload here prevents a later
        manual respawn from seeing a stale task.
        """
        ps = self._ps(_exit_key(project_ns, role_name))
        if ps.spawn_initial_task_state != "pending":
            return
        fallback = ps.spawn_initial_task_fallback
        ps.spawn_initial_task = None
        ps.spawn_initial_task_fallback = None
        ps.spawn_initial_prompt_file = None
        if preloaded:
            ps.spawn_initial_task_state = "delivered"
            _log_event(
                "spawn_initial_task_preloaded",
                role=role_name,
                project=project_ns,
            )
            # A system-prompt block supplies context but does not itself create
            # a user turn. Submit one tiny trigger so the first inference acts
            # on the task directly instead of spending a round-trip deciding
            # to read the handoff file.
            self._send_when_ready(role_name, _CURRENT_TASK_TRIGGER, project=project_ns)
            return
        ps.spawn_initial_task_state = "fallback"
        _log_event(
            "spawn_initial_task_pointer_fallback",
            role=role_name,
            project=project_ns,
        )
        if fallback:
            self._send_when_ready(role_name, fallback, project=project_ns)

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
        PtySession = _from_orch("PtySession")
        _build_pane_env = _from_orch("_build_pane_env")
        _build_transcript_path = _from_orch("_build_transcript_path")
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
                # resume_uuid omitted (defaults None): shell/gemini/codex never
                # carry a resume_uuid — that's a claude-branch-only concept
                # (`--resume` support, #103) and this helper is never called
                # for the claude branch (see docstring).
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
            # FU1 (cross-platform audit 2026-07-10 followup): shell/gemini/codex
            # have no `--resume` concept (claude-branch-only, see docstring
            # above), so this spawn is never a resume. Explicit False (not a
            # no-op) matters when the SAME role slot previously spawned via the
            # claude branch (provider substitution toggled a codex/gemini role
            # to claude, which sets this True on an actual --resume) — without
            # this reset, a later crash-respawn on this branch would read the
            # stale True and _auto_respawn would wrongly skip replaying the
            # cached last_assigned_task (#103 multi-provider parity).
            self._ps(_ekey).last_spawn_resumed = False
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
            # Non-Claude providers currently have no file-backed append-system-
            # prompt capability. This is normally a no-op because assign() keeps
            # them on pointer delivery; it only fires if the provider changed
            # while a Claude-capable spawn was deferred.
            self._finish_spawn_initial_task(role_name, project_ns, preloaded=False)
            self.statusChanged.emit()
            _log_event("spawn", role=role_name, cwd=spawn_cwd, resumed=False)
            return True, f"{label} spawned in {spawn_cwd}"
        except Exception as e:
            _ps_failed = self._ps(_exit_key(project_ns, role_name))
            if _ps_failed.spawn_initial_task_state == "pending":
                _ps_failed.spawn_initial_task = None
                _ps_failed.spawn_initial_task_fallback = None
                _ps_failed.spawn_initial_prompt_file = None
                _ps_failed.spawn_initial_task_state = ""
            try:
                session.terminate(wait=False)
            except Exception:
                pass
            try:
                session.setParent(None)
                session.deleteLater()
            except Exception:
                pass
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
        resume_uuid: str | None = None,
    ) -> tuple[bool, str]:
        # Resolve test-mockable deps from orchestrator's live namespace so that
        # tests using `patch("agent_takkub.orchestrator.PtySession")` etc. work.
        PtySession = _from_orch("PtySession")
        find_claude_executable = _from_orch("find_claude_executable")
        _build_pane_env = _from_orch("_build_pane_env")
        _build_lead_env = _from_orch("_build_lead_env")
        _build_transcript_path = _from_orch("_build_transcript_path")
        _log_event = _from_orch("_log_event")
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

        # A task-bearing assign stages a one-shot payload before entering
        # spawn(). Accept it only for a provider with a confirmed file-backed
        # append-system-prompt flag. Marking it pending above the gate/FIFO
        # returns lets deferred spawns retain the task without adding it to a
        # long PTY paste or command-line argument.
        from .provider_config import CLAUDE, effective_provider_for
        from .provider_spec import PROVIDER_REGISTRY

        effective_provider = effective_provider_for(base_role, project=project_ns)
        _ps_initial = self._ps(_exit_key(project_ns, role_name))
        if (
            _ps_initial.spawn_initial_task_state == "requested"
            and PROVIDER_REGISTRY[effective_provider].system_prompt_flag is not None
        ):
            _ps_initial.spawn_initial_task_state = "pending"

        # Fresh teammate spawn — flag machine oversubscription to Lead before we
        # construct the session (best-effort, non-blocking; the warn method
        # excludes Lead panes and is wrapped so it can never break spawning).
        # See docs/reviews/2026-06-30-queue-gap.md (no central cap; advisory only).
        _warn_over_cap = getattr(self, "_warn_lead_over_cap", None)
        if _warn_over_cap is not None and not _from_auto_respawn:
            try:
                _warn_over_cap(role_name, project_ns)
            except Exception:
                pass

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
            # A fresh session (manual spawn OR auto-respawn) gets its own
            # one-shot Stop-hook done-gate budget — a prior session's block
            # must not suppress a nudge in the new one.
            _ps_spawn_clear.stop_gate_notified = False
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
                lambda r=role_name, c=cwd, p=project, a=_from_auto_respawn, s=_shard_total, u=resume_uuid: (
                    self._retry_deferred_spawn(r, c, p, a, s, u)
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
            _queue.append((role_name, cwd, project, _from_auto_respawn, _shard_total, resume_uuid))
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

            # L1 (cross-platform audit 2026-07-10): _pty_backend.py's
            # _WinptyBackend.spawn now passes argv as a list (not a
            # pre-quoted cmdline string), so a spaced full path like
            # `C:\Program Files\PowerShell\7\pwsh.EXE` spawns fine — repro'd
            # fixed on this machine. This basename+PATH indirection predates
            # that fix and is kept as-is (still correct, zero behaviour
            # change, one less thing to touch in the same audit pass) rather
            # than swapped for the full path now that both work.
            #
            # Detect the binary so we fail fast with a clear message if
            # PowerShell isn't installed at all, then hand the **basename**
            # to winpty and let it resolve via PATH — which the cockpit
            # controls via _build_pane_env() + the bin/ prepend below.
            if sys.platform == "win32":
                pwsh_full = _shutil.which("pwsh") or _shutil.which("powershell")
                if pwsh_full is None:
                    return False, "PowerShell not on PATH (looked for pwsh / powershell)"
                pwsh_basename = (
                    "pwsh.exe" if pwsh_full.lower().endswith("pwsh.exe") else "powershell.exe"
                )
                shell_argv = [pwsh_basename, "-NoLogo"]
            else:
                # POSIX (macOS/Linux): use the user's login shell, falling back
                # to zsh (macOS default) then bash. Interactive (-i) so it loads
                # rc files and behaves like a normal terminal.
                posix_shell = (
                    os.environ.get("SHELL")
                    or _shutil.which("zsh")
                    or _shutil.which("bash")
                    or "/bin/sh"
                )
                shell_argv = [posix_shell, "-i"]
            spawn_cwd = cwd or default_cwd_for_role(role_name, project=project_ns) or str(DATA_HOME)
            env = _build_pane_env()
            env["TAKKUB_ROLE"] = role_name
            env["TAKKUB_PROJECT"] = project_ns
            _apply_artifacts_dir(env, project_ns)
            inject_user_profile_env(env, project_ns)
            bin_dir = str(CLI_BIN_DIR)
            env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
            _shell_tok = self._mint_pane_token(env, project_ns, role_name)
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

        # ── non-claude providers (codex / gemini / future CLIs) ─────
        # These TUIs speak different protocols and understand none of the
        # claude flags below — build a minimal spec-driven argv and
        # short-circuit so we never pass `--dangerously-skip-permissions`,
        # MCP configs, plugin dirs, or `--session-id`/`--resume` (all
        # claude-only) to them.
        #
        # Entry condition uses `effective_provider_for(role_name)` so the
        # user can remap any teammate role (e.g. "backend") to another CLI
        # via `~/.takkub/role-providers.json`. The `codex`/`gemini` roles
        # themselves are forced toward their CLIs by provider_config's
        # `_FORCED_PROVIDER` table.
        #
        # `effective_provider_for` (not plain `provider_for`) degrades a
        # non-claude role to claude when that provider is unavailable —
        # toggled off in the status bar OR its CLI isn't installed. When
        # that happens the branch below doesn't match and execution falls
        # through to the claude spawn path *with role_name unchanged*, so
        # a "gemini"/"codex" pane keeps its slot/identity but is powered by
        # claude ("Claude รับตำแหน่งแทน"). The in-branch discovery
        # None-guard is belt-and-suspenders (we only enter the branch when
        # the binary resolved), but kept in case PATH changes between the
        # availability probe and the spawn.
        # Per-project role→CLI mapping (project_ns resolved at spawn entry):
        # the same role can be backed by a different CLI in different tabs.
        # #101 degraded-mode unlock: `lead` is no longer forced to claude, so
        # it can enter the non-claude branch below too. It needs a
        # Lead-specific cwd (project root, not a teammate staging dir) and
        # Lead-specific context content (BLOCKED_DIRS/session-brief, not the
        # generic teammate cheatsheet) — see the `_is_lead` forks inside.
        _is_lead = role_name == LEAD.name

        if effective_provider != CLAUDE:
            # ── generic non-claude provider branch (#103 Phase 1) ──────
            # One spec-driven path replaces the old hand-written GEMINI and
            # CODEX branches (which had drifted into ~110 nearly identical
            # lines each). Everything provider-specific now lives on the
            # ProviderSpec: binary discovery, autonomy flags (platform-keyed
            # — codex's Windows sandbox-helper escape hatch #5 vs the
            # workspace-write + network_access loopback unblock #26 Mode B),
            # PATH handling (gemini: agy's installer doesn't reliably
            # register it on PATH → prepend_bin_dir_to_path), first-boot
            # trust driving (auto_trust), and early-crash detection
            # (early_exit_watch → _on_codex_exit wiring). Adding a NEW
            # provider = a PROVIDER_REGISTRY entry + ready markers; this
            # branch should not need to change.
            #
            # Providers with confirmed native AGENTS.md discovery share one
            # cheatsheet file and manager marker, avoiding write races in a
            # shared cwd. A provider whose discovery is still unconfirmed
            # uses context_strategy="none"; context is skipped with a log line
            # so the #103 gap stays visible instead of being guessed.
            from .codex_agents_md import ensure_agents_md

            spec = PROVIDER_REGISTRY[effective_provider]
            provider_bin = spec.custom_discovery_fn() if spec.custom_discovery_fn else None
            if provider_bin is None:
                return False, spec.install_instructions
            if _is_lead:
                spawn_cwd = cwd or lead_cwd(project=project_ns) or str(DATA_HOME)
            else:
                spawn_cwd = (
                    cwd or default_cwd_for_role(role_name, project=project_ns) or str(DATA_HOME)
                )
            if _is_lead:
                # Lead-specific AGENTS.md content (cockpit CLAUDE.md +
                # BLOCKED_DIRS + session brief) — NOT the teammate cheatsheet
                # ensure_agents_md() plants for every other role (#101).
                if spawn_cwd != str(DATA_HOME):
                    try:
                        post_compact_brief = self._build_post_compact_brief(project_ns)
                        render_lead_agents_md(
                            project_ns, spawn_cwd, post_compact_brief=post_compact_brief
                        )
                    except Exception:
                        _log.exception(
                            "could not render %s Lead context; spawning without it",
                            spec.name,
                        )
            elif spec.context_strategy == "agents_md_file":
                # Plant the takkub cheatsheet so the provider auto-discovers
                # it on boot and knows how to call `takkub send/done`. Safe:
                # only writes when the file is absent or already
                # takkub-managed (marker check inside the helper). `extra`
                # bridges this role's Skill Matrix assignment (#103 phase 4)
                # as an instruction-style block (non-claude CLIs have no
                # Skill tool).
                try:
                    from . import skill_policy

                    _skill_extra = skill_policy.render_skill_appendix(
                        base_role, _skill_roots_for_project(project_ns), spec.context_strategy
                    )
                    ensure_agents_md(spawn_cwd, extra=_skill_extra)
                except Exception:
                    _log.exception(
                        "could not render %s role context for %s; spawning without it",
                        spec.name,
                        base_role,
                    )
            else:
                _log.warning(
                    "provider %s has context_strategy=%r — teammate cheatsheet skipped (#103)",
                    spec.name,
                    spec.context_strategy,
                )
            env = _build_lead_env() if _is_lead else _build_pane_env()
            env["TAKKUB_ROLE"] = role_name
            env["TAKKUB_PROJECT"] = project_ns
            _apply_artifacts_dir(env, project_ns)
            inject_user_profile_env(env, project_ns)
            bin_dir = str(CLI_BIN_DIR)
            if spec.prepend_bin_dir_to_path:
                provider_dir = os.path.dirname(provider_bin)
                env["PATH"] = bin_dir + os.pathsep + provider_dir + os.pathsep + env.get("PATH", "")
            else:
                env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
            # Lead gets the Lead capability token (authorises Lead-only
            # takkub CLI commands) instead of a per-pane teammate token —
            # mirrors the claude branch below (#101).
            if _is_lead:
                env["TAKKUB_LEAD_TOKEN"] = self._lead_token
                _prov_tok = None
            else:
                _prov_tok = self._mint_pane_token(env, project_ns, role_name)
            provider_argv = [
                provider_bin,
                *spec.autonomy_flags.get(sys.platform, spec.autonomy_flags.get("default", [])),
            ]
            if spec.model_flag:
                from .provider_models import model_for
                from .role_models import model_for as role_model_for

                # A per-role model wins over the provider-level default; empty
                # falls through to the provider default, then the CLI's own.
                # The role model is resolved against the provider ACTUALLY
                # spawning (spec.name == effective_provider here), so a role
                # re-pointed at another CLI — or substituted to claude because
                # its own CLI is off/missing — never inherits a model id that
                # belongs to a different CLI.
                provider_model = role_model_for(base_role, spec.name) or model_for(spec.name)
                if provider_model:
                    provider_argv.extend([spec.model_flag, provider_model])
            if not _is_lead:
                # Role tiers are provider-neutral for effort: a pane mapped to
                # agy or Codex should retain the role's low/medium/high setting
                # just like a Claude-backed pane. Explicitly empty env disables
                # the argument for every provider.
                _, tier_effort, _ = _teammate_tier(base_role)
                provider_effort = os.environ.get("TAKKUB_TEAMMATE_EFFORT", tier_effort).strip()
                _append_provider_effort(provider_argv, spec, provider_effort)
            # MCP injection (#100): dispatched per spec.mcp_adapter_variant —
            # codex gets native `-c mcp_servers.<name>.<key>=…` session
            # overrides; agy's "plugin_import" resolves to a documented no-op
            # (see mcp_bridge.py). Called for every provider so each branch
            # goes through the same adapter dispatch.
            from .mcp_bridge import mcp_argv_for_provider

            provider_argv.extend(mcp_argv_for_provider(spec.name, base_role, shard_idx, project_ns))
            return self._launch_session(
                pane=pane,
                role_name=role_name,
                project_ns=project_ns,
                spawn_cwd=spawn_cwd,
                argv=provider_argv,
                env=env,
                pane_tok=_prov_tok,
                label=spec.name,
                cwd=cwd,
                project=project,
                _from_auto_respawn=_from_auto_respawn,
                _shard_total=_shard_total,
                codex_exit=spec.early_exit_watch,
                auto_trust=spec.auto_trust,
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
            spawn_cwd = cwd or lead_cwd(project=project_ns) or str(DATA_HOME)
            # Render Lead's system prompt fresh each spawn so BLOCKED_DIRS
            # tracks whatever project is active in projects.json right now.
            # Skip injection when Lead is anchored at the cockpit itself
            # (no project context to enforce).
            if spawn_cwd != str(DATA_HOME):
                try:
                    post_compact_brief = self._build_post_compact_brief(project_ns)
                    role_md_file = _render_lead_context(
                        project_ns,
                        post_compact_brief=post_compact_brief,
                        claude_cwd=spawn_cwd,
                    )
                except Exception:
                    _log.exception("could not render Claude Lead context; spawning without it")
                    role_md_file = None
        else:
            try:
                staging = agent_role_dir(base_role)
                role_context_available = True
            except OSError:
                _log.exception(
                    "could not prepare role context for %s; spawning without it", base_role
                )
                staging = DATA_HOME
                role_context_available = False
            spawn_cwd = cwd or default_cwd_for_role(base_role, project=project_ns) or str(staging)
            # When cwd is a project path, claude auto-discovers the project's
            # CLAUDE.md, not the role's specialist override. Pass the role's
            # markdown to --append-system-prompt-file so the specialist rules
            # always apply regardless of where we land. (Using the *file*
            # variant avoids command-line escaping problems with multiline
            # markdown containing backticks, asterisks, and Thai text.)
            role_md_path = staging / "CLAUDE.md"
            try:
                role_context_exists = role_context_available and role_md_path.exists()
            except Exception:
                _log.exception(
                    "could not inspect role context for %s; spawning without it", base_role
                )
                role_context_exists = False
                role_md_file = None
            if role_context_exists:
                # agent_role_dir() always rewrites CLAUDE.md fresh from the source
                # .claude/agents/<role>.md, so these injections never accumulate.
                # Build the whole appendix, then write once.
                try:
                    _existing_md = role_md_path.read_text(encoding="utf-8")
                except Exception:
                    _log.exception(
                        "could not read role context for %s; spawning without it", base_role
                    )
                    _existing_md = ""
                    role_context_available = False
                _appendix = ""
                # Issue #33: pointer to Lead's project-memory so the teammate can
                # read domain rules (package manager, ports, vendor patterns) on
                # demand without relying on Lead to echo them in every task spec.
                try:
                    _mem_path = _resolve_project_memory(lead_cwd(project_ns) or spawn_cwd)
                except Exception:
                    _log.exception(
                        "could not resolve project memory for %s; spawning without role context",
                        base_role,
                    )
                    _mem_path = None
                    role_context_available = False
                    role_md_file = None
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
                    except Exception:
                        _mem_text = ""
                    # tok-5: a freshly-seeded file is just the skeleton (bare `-`
                    # placeholders, no learned bullets). Inlining the whole empty
                    # skeleton + the long "อย่าเดา/ค้นใหม่" wrapper costs ~100-150
                    # tok/spawn for zero knowledge. When there's no real content
                    # yet, emit a one-line pointer instead; the full inline block
                    # returns the moment the role appends its first note.
                    try:
                        from .role_memory import has_learned_content

                        has_role_memory = has_learned_content(_mem_text, project_ns, base_role)
                    except Exception:
                        _log.exception(
                            "could not render learned role context for %s; spawning without it",
                            base_role,
                        )
                        has_role_memory = False
                        role_context_available = False
                        role_md_file = None

                    if not has_role_memory:
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
                # Skill Matrix (#103 phase 4): a role's own Skill tool already
                # auto-discovers .claude/skills/ project-wide regardless of
                # this policy — this appendix just names which ones the
                # operator flagged as relevant to THIS role, so it reads them
                # proactively instead of waiting to stumble onto them.
                try:
                    from . import skill_policy

                    _appendix += skill_policy.render_skill_appendix(
                        base_role,
                        _skill_roots_for_project(project_ns),
                        PROVIDER_REGISTRY["claude"].context_strategy,
                    )
                except Exception:
                    _log.exception(
                        "could not render skill role context for %s; spawning without it",
                        base_role,
                    )
                    role_context_available = False
                    role_md_file = None
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
                if role_context_available:
                    try:
                        if _appendix:
                            role_md_path.write_text(_existing_md + _appendix, encoding="utf-8")
                        role_md_file = str(role_md_path)
                    except Exception:
                        _log.exception(
                            "could not write role context for %s; spawning without it", base_role
                        )
                        role_md_file = None

        _ps_prompt = self._ps(_exit_key(project_ns, role_name))
        _pending_spawn_task = (
            _ps_prompt.spawn_initial_task
            if _ps_prompt.spawn_initial_task_state == "pending"
            else None
        )
        # Always run the regeneration helper, even with no pending task: its
        # strip-first contract guarantees respawn/resume never inherits a task
        # block left by a prior spawn.
        _prepared_prompt_file = None
        _cached_prompt_file = _ps_prompt.spawn_initial_prompt_file
        if (
            _pending_spawn_task is not None
            and _cached_prompt_file
            and pathlib.Path(_cached_prompt_file).is_file()
        ):
            # A Tier-2 gate retry is still the same pending native spawn; reuse
            # its immutable per-spawn file instead of leaking one copy per poll.
            _prepared_prompt_file = _cached_prompt_file
        else:
            _spawn_prompt_output = None
            if role_md_file:
                _role_prompt_path = pathlib.Path(role_md_file)
                _pane_scope = hashlib.sha256(f"{project_ns}\0{role_name}".encode()).hexdigest()[:16]
                _spawn_prompt_output = str(
                    _role_prompt_path.with_name(
                        f"{_role_prompt_path.stem}.spawn-{_pane_scope}{_role_prompt_path.suffix}"
                    )
                )
            _prepared_prompt_file = _prepare_spawn_system_prompt(
                role_md_file,
                _pending_spawn_task,
                output_file=_spawn_prompt_output,
            )
            if _pending_spawn_task is not None and _prepared_prompt_file is not None:
                _ps_prompt.spawn_initial_prompt_file = _prepared_prompt_file
        _initial_task_preloaded = (
            _pending_spawn_task is not None and _prepared_prompt_file is not None
        )
        if _prepared_prompt_file is not None:
            role_md_file = _prepared_prompt_file

        # LOW (codex full-system review 2026-07-11): validate an explicit
        # resume_uuid BEFORE minting the per-pane capability token below.
        # spawn_cwd is settled for both the Lead and teammate branches above,
        # so this can reject a mismatched uuid without ever registering a
        # token that a later early-return would leave orphaned in
        # self._pane_tokens (the token is never revoked because it was never
        # minted in the first place).
        if resume_uuid and not _resume_uuid_matches_cwd(project_ns, resume_uuid, spawn_cwd):
            return False, f"resume_uuid does not match cwd for {role_name}"

        try:
            claude = find_claude_executable()
        except RuntimeError as e:
            return False, str(e)

        env = _build_lead_env() if role_name == LEAD.name else _build_pane_env()
        env["TAKKUB_ROLE"] = role_name
        _apply_artifacts_dir(env, project_ns)
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
        # project — under the multi-tab refactor a Lead in project-a
        # mustn't accidentally send to a backend pane that belongs to project-b.
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
        bin_dir = str(CLI_BIN_DIR)
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
            if sys.platform == "win32":
                chrome_candidates = (
                    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                    str(pathlib.Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe"),
                )
            elif sys.platform == "darwin":
                chrome_candidates = (
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    str(
                        pathlib.Path.home()
                        / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
                    ),
                )
            else:  # linux
                chrome_candidates = (
                    "/usr/bin/google-chrome",
                    "/usr/bin/chromium",
                    "/usr/bin/chromium-browser",
                )
            for cand in chrome_candidates:
                if pathlib.Path(cand).is_file():
                    env["CHROME_BIN"] = cand
                    break

        # H1: MCP timeout / non-interactive env / truecolor term are applied
        # inside _build_pane_env()/_build_lead_env() itself now (pane_env.py)
        # so every branch (shell/codex/gemini/claude) gets them, not just
        # this claude path.
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

        # Wire Stop/Notification hooks → `takkub _hook` so the orchestrator gets
        # an authoritative turn-end/idle signal for THIS pane instead of relying
        # solely on PTY-scraping's next poll tick. `--settings` takes a file path
        # here (not inline JSON) to sidestep Windows argv-quoting risk for a
        # JSON string (see hook_wiring.py docstring). Applies to every claude-
        # backed pane (Lead + teammates); codex/gemini/shell panes returned
        # earlier and never reach this branch.
        try:
            from .hook_wiring import ensure_hook_settings_file

            argv.extend(["--settings", ensure_hook_settings_file()])
        except Exception:
            pass  # hook wiring must never block a spawn

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
            if "TAKKUB_TEAMMATE_MODEL" in os.environ:
                # An explicitly empty value means "use the Claude CLI default"
                # and must not be replaced by provider configuration.
                teammate_model = os.environ["TAKKUB_TEAMMATE_MODEL"].strip()
            else:
                from .provider_config import CLAUDE
                from .provider_models import model_for
                from .role_models import model_for as role_model_for

                # Precedence: per-role model > claude provider-level model >
                # the role's tier default. Reaching this branch means claude is
                # the effective provider (a non-claude role that degraded to a
                # claude substitute lands here too), so the role model only
                # applies when it was chosen FOR claude.
                teammate_model = (
                    role_model_for(base_role, CLAUDE) or model_for(CLAUDE) or tier_model
                ).strip()
            teammate_model = _remap_pinned_model(teammate_model, env)
            if teammate_model:
                argv.extend(["--model", teammate_model])
            teammate_effort = os.environ.get("TAKKUB_TEAMMATE_EFFORT", tier_effort).strip()
            _append_provider_effort(
                argv,
                PROVIDER_REGISTRY[CLAUDE],
                teammate_effort,
            )
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
            teammate_fallback = _remap_pinned_model(teammate_fallback, env)
            if teammate_fallback:
                argv.extend(["--fallback-model", teammate_fallback])
            # Hard-enforce "ห้าม spawn subagent" (CLAUDE.md) at the CLI level so a
            # teammate can't fan out invisible Task subagents (was prompt-only).
            # Teammates only; the Lead is left unrestricted. Override/clear via
            # TAKKUB_TEAMMATE_DISALLOWED_TOOLS (see _teammate_disallowed_tools).
            _disallowed_tools = _teammate_disallowed_tools()
            if _disallowed_tools:
                argv.extend(["--disallowedTools", *_disallowed_tools])
        else:
            # Lead normally rides the user's default model (Opus on this
            # install) with no --model flag. Under a Pro plan that default may
            # be the [1m] 1M-context variant, which Pro can't reach (usage
            # credits required) and which hard-errors the turn. Pin the Lead
            # to a standard-context model in that case (see plan_tier).
            #
            # Settings renders a model picker for Lead like any other role, so
            # honour it here with the same precedence teammates use — role >
            # provider > plan-tier pin — otherwise choosing a Lead model is a
            # silent no-op on a claude Lead while it works on a non-claude one
            # (that path goes through the generic branch above).
            from .provider_config import CLAUDE as _CLAUDE
            from .provider_models import model_for as _provider_model_for
            from .role_models import model_for as _role_model_for

            lead_model = (
                _role_model_for(role_name, _CLAUDE)
                or _provider_model_for(_CLAUDE)
                or _lead_model_override()
            )
            if lead_model:
                lead_model = _remap_pinned_model(lead_model, env)
                argv.extend(["--model", lead_model])
            # Degrade to Sonnet on overload/not-found so orchestration keeps
            # moving during peak load instead of the Lead turn erroring out
            # — the Lead is the single pane the user is actually talking to,
            # so a hard failure there stalls the whole session. Set
            # TAKKUB_LEAD_FALLBACK="" to disable.
            lead_fallback = os.environ.get("TAKKUB_LEAD_FALLBACK", "claude-sonnet-5").strip()
            lead_fallback = _remap_pinned_model(lead_fallback, env)
            if lead_fallback:
                argv.extend(["--fallback-model", lead_fallback])

        # Explicit plugin allowlist (skip the broken claude-obsidian hook).
        # Set TAKKUB_EXTRA_PLUGINS env var to a `;`-separated list of plugin
        # root dirs (must each contain `.claude-plugin/plugin.json`) to add
        # more, or set it to empty string to suppress the defaults.
        plugin_default = ";".join(_default_plugin_dirs(base_role, project=project_ns))
        plugin_dirs_raw = os.environ.get("TAKKUB_EXTRA_PLUGINS", plugin_default)
        for pdir in [p.strip() for p in plugin_dirs_raw.split(";") if p.strip()]:
            if (pathlib.Path(pdir) / ".claude-plugin" / "plugin.json").exists():
                argv.extend(["--plugin-dir", pdir])
        _claude_system_prompt_flag = PROVIDER_REGISTRY[CLAUDE].system_prompt_flag
        if role_md_file and _claude_system_prompt_flag:
            argv.extend([_claude_system_prompt_flag, role_md_file])

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
        #
        # Every pane: browser roles (qa/critic/designer) get a PERSISTENT
        # per-(project, role[, shard]) browser profile so the browser remembers
        # its session/cookies across runs (no more re-login every test) and
        # parallel shards don't collide on one Chrome profile lock (#39).
        # Non-browser roles fall through to their plain role-variant config.
        #
        # Routed through mcp_bridge.py (issue #100) — same adapter call every
        # provider branch uses, dispatched by PROVIDER_REGISTRY's
        # mcp_adapter_variant; claude's own resulting argv is byte-identical
        # to the pre-#100 inline `--mcp-config`/`--strict-mcp-config` code.
        from .mcp_bridge import mcp_argv_for_provider

        argv.extend(mcp_argv_for_provider("claude", base_role, shard_idx, project_ns))

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
        #
        # W3: `resume_uuid` is a caller-chosen override (mobile Resume/session
        # picker) that bypasses the 5-min auto-resume window entirely — it can
        # rejoin a session from hours/days ago. Validated against the JSONL
        # store (not the in-memory pane-state cache, which only knows sessions
        # from panes that were open *this run*) so a forged/mismatched uuid can
        # never bleed another project's conversation into this cwd, same
        # cwd-disambiguation guarantee the auto-resume path below relies on.
        # (Actual validation now happens earlier, before pane-token minting —
        # see the resume_uuid check above find_claude_executable(). By this
        # point resume_uuid is already known-valid or None.)
        resumed = False
        _ekey_spawn = _exit_key(project_ns, role_name)
        if resume_uuid:
            argv.extend(["--resume", resume_uuid])
            resumed = True
            _ps_new = self._ps(_ekey_spawn)
            _ps_new.session_uuid = resume_uuid
            _ps_new.session_uuid_cwd = spawn_cwd
        else:
            _ps_pre = self._pane_state.get(_ekey_spawn)
            prior_uuid = _ps_pre.session_uuid if _ps_pre is not None else None
            prior_uuid_cwd = _ps_pre.session_uuid_cwd if _ps_pre is not None else ""
            prior_exit = self._recent_exits.get(_ekey_spawn)
            # L5: normalize both sides before comparing — see
            # _normalize_cwd_for_compare's docstring for why a raw string
            # compare can miss two spellings of the same directory.
            can_resume = (
                prior_uuid is not None
                and bool(prior_uuid_cwd)
                and _normalize_cwd_for_compare(prior_uuid_cwd)
                == _normalize_cwd_for_compare(spawn_cwd)
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
                    resume_uuid=resume_uuid,
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
            self._finish_spawn_initial_task(
                role_name,
                project_ns,
                preloaded=_initial_task_preloaded,
            )
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
            # (The /remote-control auto-bridge was removed 2026-07-10 — it kept
            # racing claude's /resume picker. Type /remote-control by hand.)
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
            # If a forced non-claude role (codex/gemini/...) reached the claude
            # spawn path, its provider was unavailable (toggled off or not
            # installed) and claude is standing in. Surface that so the user
            # isn't surprised the pane talks like Claude.
            from .provider_config import FORCED_ROLES

            if role_name in FORCED_ROLES:
                suffix += " — claude substitute (provider unavailable)"
            return True, f"{role_name} spawned in {spawn_cwd}{suffix}"
        except Exception as e:
            _ps_failed = self._ps(_exit_key(project_ns, role_name))
            if _ps_failed.spawn_initial_task_state == "pending":
                _ps_failed.spawn_initial_task = None
                _ps_failed.spawn_initial_task_fallback = None
                _ps_failed.spawn_initial_prompt_file = None
                _ps_failed.spawn_initial_task_state = ""
            try:
                session.terminate(wait=False)
            except Exception:
                pass
            try:
                session.setParent(None)
                session.deleteLater()
            except Exception:
                pass
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
        ensure_runtime = _from_orch("ensure_runtime")
        RUNTIME_DIR = _from_orch("RUNTIME_DIR")
        _log_event = _from_orch("_log_event")
        _build_pane_env = _from_orch("_build_pane_env")
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
