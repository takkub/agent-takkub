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
import os
import pathlib
import secrets
import sys
import time
import uuid as _uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from PyQt6.QtCore import QTimer

from .agent_pane import AgentPane
from .claude_auth_config import apply_claude_auth_overrides
from .config import (
    REPO_ROOT,
    agent_role_dir,
    default_cwd_for_role,
    lead_cwd,
    validate_name,
)
from .lead_context import (
    BIG_FILE_GUARD,
    STALE_FILE_GUARD,
    _default_plugin_dirs,
    _render_lead_context,
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
    _apply_ecc_mute,
    _apply_mcp_timeout,
    _apply_non_interactive_env,
    inject_user_profile_env,
)
from .pipeline_executor import _split_shard
from .pty_session import PtySession
from .roles import LEAD

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


# ── spawn constants ──────────────────────────────────────────────

RESUME_WINDOW_SEC = 5 * 60  # respawn within this window → claude --resume <uuid>

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
    # splash_dismiss_ts: last time Enter was sent to dismiss an update-splash modal (#62)
    splash_dismiss_ts: float = 0.0


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
                lambda r=role_name, c=cwd, p=project, a=_from_auto_respawn, s=_shard_total: (
                    self._retry_deferred_spawn(r, c, p, a, s)
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
            spawn_cwd = cwd or default_cwd_for_role(role_name, project=project_ns) or str(REPO_ROOT)
            env = _build_pane_env()
            env["TAKKUB_ROLE"] = role_name
            env["TAKKUB_PROJECT"] = project_ns
            inject_user_profile_env(env, project_ns)
            bin_dir = str(REPO_ROOT / "bin")
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
