"""AutoResumeMixin — limit-aware auto-resume (🌙).

Mixed into ``Orchestrator``. Builds on the existing rate-limit watchdog
(``_rate_limit_suppressed`` in orchestrator.py) which already detects
**signal (a)** — the usage-limit banner text on the pane, via
``pty_session.rate_limit_reset_at()`` and its marker list — and records the
reset epoch in ``PaneState.rate_limited_until``. This module adds:

* **signal (b)** — for Claude panes, an independent confirmation via the
  profile's ``limit_status`` telemetry (five-hour window utilization), fetched
  off the Qt thread so a slow/offline network call never blocks the watchdog
  tick. Both signals must agree before a Claude pane is parked. Other providers
  currently fall back to their provider-specific banner (signal (a)) alone.
* **park** — once confirmed, notify the Lead once and stop poking the pane
  (the idle-reminder suppression already in ``_rate_limit_suppressed``
  handles the "stop nagging" half).
* **wake** — a one-shot ``QTimer`` fires at the reported reset time (+
  buffer) and injects a "continue the pending task" nudge directly into the
  TEAMMATE pane — not just a Lead notice — so work actually resumes. #322:
  right before injecting, re-checks the limit banner is still showing — a
  Claude Code 2.1.234+ pane may have already auto-continued the interrupted
  turn on its own by wake time, and pasting a nudge on top of live
  generation would race it (same class as the A3 draft-hold incident). If
  the banner already cleared, the wake is a no-op beyond clearing state and
  notifying Lead that the CLI resumed itself.
* **caps** — at most ``auto_resume.MAX_PARK_ROUNDS`` park→wake cycles per
  pane per assigned task, and an immediate permanent stop if the pane
  re-hits the limit within ``auto_resume.RELIMIT_GRACE_S`` of waking (the
  fresh window is exhausted too, or the task itself is pathological) —
  either way auto-resume hands the decision back to the Lead instead of
  looping.

Entirely inert when ``auto_resume.is_enabled()`` is False (the default): the
pre-existing notify-only behaviour is completely unchanged.

Scope guard: only ever acts on a pane that has an outstanding assigned task
(``PaneState.last_assigned_task`` truthy) — never generates new work.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from PyQt6.QtCore import QTimer

from . import auto_resume
from .agent_pane import AgentPane
from .config import RUNTIME_DIR
from .lead_inbox import _delayed_enter
from .limit_status import UsageData, fetch_usage_shared
from .orchestrator_text import _human_duration, _log_event
from .provider_config import CLAUDE, CODEX, CURSOR, GEMINI, KIMI, OPENCODE, effective_provider_for
from .spawn_engine import PaneState

# #514: fixed priority order the reroute picker walks — claude first (the
# cockpit's always-available baseline), then the rest in registry order.
# Whichever candidates are disabled/uninstalled/still quota-hit/the
# --distinct-from counterpart's provider get skipped; see
# AutoResumeMixin._pick_reroute_provider.
_REROUTE_PRIORITY: tuple[str, ...] = (CLAUDE, CODEX, GEMINI, KIMI, OPENCODE, CURSOR)


def _usage_confirms_limit(
    usage: UsageData | None, threshold: float = auto_resume.CONFIRM_UTILIZATION_PCT
) -> bool:
    """Pure signal-(b) check: does the profile's own usage telemetry agree
    the five-hour window is (near-)exhausted?

    None (offline / no credentials / fetch error) or no matching window →
    False. Conservative on purpose: an unconfirmed signal (a) alone must
    never park a pane."""
    if usage is None:
        return False
    for window in usage.windows or ():
        if (
            window.name == "five_hour"
            and window.utilization is not None
            and window.utilization >= threshold
        ):
            return True
    return False


# ── status-dump helpers (#158) — pure, no Qt/network, safe on a mock pane ──


def _pane_cwd(pane: AgentPane | None) -> str | None:
    """Best-effort task cwd for *pane* — used to locate the working tree for
    the git-status half of the give-up dump. None on anything unexpected
    (no pane, torn-down pane, a bare mock in tests) rather than raising."""
    if pane is None:
        return None
    cwd = getattr(pane, "_session_cwd", None)
    return cwd if isinstance(cwd, str) and cwd else None


def _pane_output_tail(
    pane: AgentPane | None, *, max_lines: int = auto_resume.GIVE_UP_TAIL_LINES
) -> str:
    """Last non-blank lines of *pane*'s visible screen, newest at the bottom.

    Diagnostic only: this is what the Lead sees when auto-resume gives up, to
    judge whether the task actually finished before the pane went quiet.
    Never raises — a dead session or a bare mock pane in tests just yields ""."""
    if pane is None or pane.session is None:
        return ""
    try:
        lines = [ln.rstrip() for ln in pane.session.display_lines() if ln.strip()]
    except Exception:
        return ""
    return "\n".join(lines[-max_lines:])


def _progress_marker_path(project: str, role: str) -> Path:
    """``RUNTIME_DIR/progress/<project>/<role>.json`` — one file per pane,
    overwritten on every park/give-up/wake transition."""
    day_dir = RUNTIME_DIR / "progress" / project
    try:
        day_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return day_dir / f"{role}.json"


def _write_progress_marker(
    project: str,
    role: str,
    ps: PaneState,
    pane: AgentPane | None,
    *,
    status: str,
    reason: str = "",
) -> Path | None:
    """Persist a recovery snapshot of the parked task to disk (#158).

    Written by the orchestrator itself — never depends on the agent process
    cooperating — so the pending task's last-known state (task text, cwd,
    visible output) survives even if the pane later dies mid-park without
    ever reporting `takkub done`. Returns the path on success, None on a
    write failure (disk full, permissions) — diagnostic only, never fatal."""
    marker = {
        "status": status,  # "parked" | "gave_up" | "resumed" | "rerouted"
        "reason": reason,
        "role": role,
        "project": project,
        "ts": time.time(),
        "task": ps.last_assigned_task or "",
        "task_file": ps.last_assigned_task_file,
        "cwd": _pane_cwd(pane),
        "output_tail": _pane_output_tail(pane),
        "park_rounds": ps.limit_park_rounds,
        # #495: the wake QTimer + PaneState.rate_limited_until only live in
        # memory — a cockpit restart drops both. Persisting the reset epoch
        # (and which provider tripped it) here lets `_restore_parked_pane`
        # re-arm the same wake after a restart instead of the park silently
        # vanishing. Only meaningful while status == "parked"; left at 0.0
        # for "gave_up"/"resumed" writes since nothing should resume those.
        "reset_at": ps.rate_limited_until if status == "parked" else 0.0,
        "quota_provider": ps.quota_provider or "",
    }
    path = _progress_marker_path(project, role)
    try:
        path.write_text(json.dumps(marker, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        return None
    return path


def _read_progress_marker(project: str, role: str) -> dict | None:
    """Best-effort read-back of the marker `_write_progress_marker` writes.

    None on anything unexpected — no marker written yet, corrupt JSON, or a
    permissions error — never raises."""
    path = _progress_marker_path(project, role)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


class AutoResumeMixin:
    """Methods assume `self` is an `Orchestrator` (SpawnEngineMixin's
    `_ps`/`_pane_state`/`_panes_by_project`, LeadInboxMixin's `_notify_lead`,
    and the `limitUsageConfirmed` signal declared on the class)."""

    # ── entry point — called from the idle watchdog once signal (a) fired ──
    def _maybe_auto_resume_park(self, project: str, role: str, pane: AgentPane, now: float) -> None:
        """Called on every watchdog tick while `pane` is already known
        rate-limited (`_rate_limit_suppressed` returned True this tick).

        No-op unless auto-resume is ON, the pane has an outstanding task,
        and this episode hasn't already been parked/confirmed/given up."""
        if not auto_resume.is_enabled():
            return
        key = f"{project}::{role}"
        ps = self._ps(key)
        if not ps.last_assigned_task:
            return  # scope guard: never touch a pane with no pending task
        if ps.limit_park_stopped or ps.limit_parked or ps.limit_confirm_pending:
            return  # already parked, already confirming, or already gave up

        # Re-limited soon after being woken → the fresh window is exhausted
        # too (or the task is pathological). Stop for good instead of
        # looping park→wake forever.
        if ps.limit_park_wake_ts and (now - ps.limit_park_wake_ts) < auto_resume.RELIMIT_GRACE_S:
            self._give_up_auto_resume(project, role, ps, reason="relimit_within_grace")
            return

        if ps.limit_park_rounds >= auto_resume.MAX_PARK_ROUNDS:
            self._give_up_auto_resume(project, role, ps, reason="round_cap")
            return

        if not ps.rate_limited_until:
            return  # signal (a) not actually recorded yet on this pane state

        if effective_provider_for(role, project) != CLAUDE:
            # #103: Codex/Gemini do not yet expose usage telemetry here. Their
            # provider-specific limit banner (signal a) is the safe fallback;
            # never confirm it against an unrelated Anthropic usage window.
            self._reroute_or_park(project, role, ps)
            return

        ps.limit_confirm_pending = True
        self._confirm_limit_via_usage_async(project, role)

    def _give_up_auto_resume(self, project: str, role: str, ps: PaneState, *, reason: str) -> None:
        ps.limit_park_stopped = True
        if reason == "relimit_within_grace":
            why = "ชน limit ซ้ำเร็วเกินไปหลังปลุก"
        elif reason == "no_fallback_park_disabled":
            why = "ชนโควตา, ไม่มี provider อื่นให้ reroute และ park-fallback ปิดอยู่ใน Settings"
        else:
            why = f"park/wake ครบ {auto_resume.MAX_PARK_ROUNDS} รอบแล้ว"
        pane = self._panes_by_project.get(project, {}).get(role)
        cwd = _pane_cwd(pane)
        tail = _pane_output_tail(pane)
        marker_path = _write_progress_marker(
            project, role, ps, pane, status="gave_up", reason=reason
        )

        task = ps.last_assigned_task or ""
        task_preview = task[: auto_resume.GIVE_UP_TASK_PREVIEW_CHARS].strip()
        if len(task) > auto_resume.GIVE_UP_TASK_PREVIEW_CHARS:
            task_preview += "…"

        # #158: a pane that gave up mid-task isn't proof the task failed — it
        # may well have finished and just never got to run `takkub done`
        # before the window ran out again. Dump enough state (task, last
        # visible output, and — async below — a git-status check of its cwd)
        # for the Lead to verify before discarding or reassigning the work.
        dump = [
            f"🌙⚠️ [auto-resume] {role} ({project}) หยุด auto-resume ให้ task นี้ "
            f"({why}) — ตัดสินใจต่อเอง (nudge ต่อ/มอบงานใหม่)",
            "⚠️ hint: งานอาจเสร็จสมบูรณ์แล้วแต่ยังไม่ได้รายงานผ่าน `takkub done` "
            "(ชน limit ก่อนได้รายงาน) — ตรวจสอบสถานะจริงก่อน discard/reassign",
        ]
        if task_preview:
            dump.append(f"📋 task ที่ค้าง: {task_preview}")
        if tail:
            dump.append(f"🖥️ output ท้าย pane:\n{tail}")
        if marker_path is not None:
            dump.append(f"📄 status dump เต็ม: {marker_path}")
        msg = "\n".join(dump)

        self._notify_lead(
            project, msg, from_role=role, note=reason, kind="limit-autoresume-stopped"
        )
        _log_event("pane_limit_autoresume_stopped", role=role, project=project, reason=reason)
        if cwd:
            # Non-blocking (QProcess, not subprocess.run) — reuses the same
            # git-status-in-cwd check `done()` already runs for the
            # requires-commit warning, so a dirty tree gets its own follow-up
            # Lead message instead of racing the notice above.
            self._check_uncommitted_async(project, role, cwd)

    # ── quota-hit reroute (#514) ─────────────────────────────────────────
    # A quota-hit pane's task moves to another available provider
    # immediately instead of waiting out the window — the pre-#514 park
    # behaviour above is now only the fallback when NOTHING else can take
    # the task right now (forced-identity role, every other provider
    # disabled/uninstalled/itself still quota-hit, or the only remaining
    # candidate is the --distinct-from counterpart's own provider).
    def _reroute_or_park(self, project: str, role: str, ps: PaneState) -> None:
        hit_provider = ps.quota_provider or "claude"
        reset_at = ps.rate_limited_until
        if reset_at:
            from . import provider_state

            provider_state.set_quota_reset_at(hit_provider, reset_at)
            self._schedule_provider_quota_reset_notice(project, hit_provider, reset_at)

        candidate = self._pick_reroute_provider(project, role, ps, hit_provider)
        if candidate is not None:
            self._reroute_pane_to_provider(project, role, ps, candidate, hit_provider, reset_at)
            return

        if auto_resume.park_fallback_enabled():
            self._park_pane_for_limit(project, role, ps)
            return

        _log_event(
            "pane_quota_reroute_no_fallback",
            role=role,
            project=project,
            hit_provider=hit_provider,
        )
        self._give_up_auto_resume(project, role, ps, reason="no_fallback_park_disabled")

    def _pick_reroute_provider(
        self, project: str, role: str, ps: PaneState, hit_provider: str
    ) -> str | None:
        """The next available CLI this role's task can move to right now, or
        None when nothing qualifies.

        Forced-identity roles (`codex`/`gemini`/`opencode`/`kimi`/`cursor` as
        the role NAME — `provider_config.FORCED_ROLES`) never reroute: the
        role's whole identity IS that one CLI (see `provider_config`'s
        module docstring — "always X, the role's whole point"), so there is
        no other provider it could legitimately run as. Every other role
        (lead, backend, frontend, qa, reviewer, critic, custom roles, ...)
        can move to any registered provider that's actually usable."""
        from . import provider_state
        from .provider_config import FORCED_ROLES, VALID_PROVIDERS, _provider_available

        base_role = role.split("#", 1)[0].strip().lower()
        if base_role in FORCED_ROLES:
            return None

        now = time.time()
        exclude = {hit_provider}
        if ps.distinct_from:
            counterpart_key = f"{project}::{ps.distinct_from}"
            counterpart_ps = self._pane_state.get(counterpart_key)
            counterpart_provider = None
            if counterpart_ps is not None:
                counterpart_provider = counterpart_ps.provider_override or (
                    counterpart_ps.quota_provider or None
                )
            if not counterpart_provider:
                counterpart_provider = effective_provider_for(ps.distinct_from, project)
            exclude.add(counterpart_provider)

        for candidate in _REROUTE_PRIORITY:
            if candidate not in VALID_PROVIDERS or candidate in exclude:
                continue
            if not _provider_available(candidate):
                continue
            if not provider_state.is_quota_ready(candidate, now):
                continue
            return candidate
        return None

    def _reroute_pane_to_provider(
        self,
        project: str,
        role: str,
        ps: PaneState,
        new_provider: str,
        hit_provider: str,
        reset_at: float,
    ) -> None:
        """Close the quota-hit pane and respawn the SAME role on
        `new_provider`, resending its outstanding task with a short
        progress note. Mirrors `Orchestrator._auto_recover_stuck`'s
        close→snapshot→respawn shape — the closest existing precedent for
        "the pane itself is fine, only the provider under it needs to
        change" (no `--resume`, unlike that path: a different CLI can't
        resume another provider's session)."""
        pane = self._panes_by_project.get(project, {}).get(role)
        cwd = _pane_cwd(pane)
        task = ps.last_assigned_task or ""
        key = f"{project}::{role}"
        reroute_count = ps.quota_reroute_count + 1

        # Snapshot everything close() pops that must survive the respawn —
        # same fields _auto_recover_stuck snapshots, minus the session uuid
        # (not reusable across providers).
        snap_auto_chain = ps.auto_chain
        snap_requires_commit = ps.requires_commit_on_done
        snap_shard_total = ps.shard_total
        snap_pipeline_run_id = ps.pipeline_run_id
        snap_assign_base_sha = ps.assign_base_sha
        snap_assign_git_root = ps.assign_git_root
        snap_assign_dirty_snapshot = ps.assign_dirty_snapshot
        snap_assign_non_git = bool(ps.assign_non_git)
        snap_distinct_from = ps.distinct_from

        _write_progress_marker(
            project, role, ps, pane, status="rerouted", reason=f"{hit_provider}->{new_provider}"
        )
        human = _human_duration(max(0, reset_at - time.time())) if reset_at else "ไม่ทราบ"
        _log_event(
            "pane_quota_rerouted",
            role=role,
            project=project,
            from_provider=hit_provider,
            to_provider=new_provider,
            round=reroute_count,
        )
        lead_msg = (
            f"🔀 [auto-resume] {hit_provider} ชนโควตา → {role} ย้ายไป {new_provider} "
            f"ต่อจาก progress ล่าสุด, {hit_provider} กลับ {human}"
        )
        self._notify_lead(
            project, lead_msg, from_role=role, note="quota_rerouted", kind="quota-rerouted"
        )

        self.close(role, project=project, suppress_pipeline=True, suppress_auto_chain=True)

        def _do_reroute_respawn() -> None:
            _ps_r = self._ps(key)
            _ps_r.provider_override = new_provider
            _ps_r.last_assigned_task = task
            _ps_r.quota_reroute_count = reroute_count
            _ps_r.quota_reroute_from = hit_provider
            _ps_r.distinct_from = snap_distinct_from
            if snap_auto_chain:
                _ps_r.auto_chain = snap_auto_chain
            if snap_requires_commit:
                _ps_r.requires_commit_on_done = snap_requires_commit
            if snap_shard_total:
                _ps_r.shard_total = snap_shard_total
            if snap_pipeline_run_id is not None:
                _ps_r.pipeline_run_id = snap_pipeline_run_id
            if snap_assign_base_sha is not None:
                _ps_r.assign_base_sha = snap_assign_base_sha
            if snap_assign_git_root is not None:
                _ps_r.assign_git_root = snap_assign_git_root
            if snap_assign_dirty_snapshot is not None:
                _ps_r.assign_dirty_snapshot = snap_assign_dirty_snapshot
            _ps_r.assign_non_git = snap_assign_non_git

            ok, msg = self.spawn(
                role,
                cwd=cwd,
                project=project,
                _from_auto_respawn=True,
                _shard_total=snap_shard_total,
            )
            _log_event(
                "quota_reroute_respawn",
                role=role,
                project=project,
                ok=ok,
                msg=msg[:160],
                to_provider=new_provider,
            )
            if not ok:
                self._pane_state.pop(key, None)
                self._notify_lead(
                    project,
                    f"⚠️ [auto-resume] ย้าย {role} ไป {new_provider} ไม่สำเร็จ: {msg} — "
                    "ต้อง assign ใหม่เอง",
                    from_role=role,
                    note="quota_reroute_failed",
                    kind="quota-reroute-failed",
                )
                # The reroute-close suppressed the pipeline fail/advance
                # assuming the role would come back on the new provider. It
                # didn't — mark it failed + advance the hop now, mirroring
                # `Orchestrator._auto_recover_stuck`'s own respawn-failure
                # branch, or a pipeline hop stalls forever waiting on a pane
                # that's gone.
                if snap_pipeline_run_id is not None:
                    pl_key = f"{project}::{snap_pipeline_run_id}"
                    pl_run = self._pipeline_runs.get(pl_key)
                    if pl_run is not None and not pl_run.closed:
                        pl_run.hop_pending.discard(role)
                        pl_run.hop_failed.add(role)
                        if not pl_run.hop_pending:
                            self._advance_pipeline(project, pl_key, pl_run)
                return
            if task:
                note = (
                    f"\n\n[system] งานนี้ย้ายจาก provider {hit_provider} (ชนโควตา) มาที่ "
                    f"{new_provider} — ทำต่อจากจุดที่ค้างไว้ (ถ้าเพิ่งเริ่มงานให้เริ่มใหม่ได้เลย), "
                    "ถ้าเสร็จแล้วรายงานด้วย `takkub done`"
                )
                self._send_when_ready(role, task + note, project=project)

        QTimer.singleShot(2_000, _do_reroute_respawn)

    def _schedule_provider_quota_reset_notice(
        self, project: str, provider: str, reset_at: float
    ) -> None:
        """Once `provider`'s quota window actually resets, clear the
        recorded quota-hit and tell Lead once — the pane that fled the hit
        stays on whichever provider it rerouted to; this just says new/future
        work can route to `provider` again."""
        delay_ms = max(0, int((reset_at + auto_resume.WAKE_BUFFER_S - time.time()) * 1000))
        QTimer.singleShot(
            delay_ms,
            lambda: self._on_provider_quota_window_reset(project, provider, reset_at),
        )

    def _on_provider_quota_window_reset(self, project: str, provider: str, reset_at: float) -> None:
        from . import provider_state

        # De-dupe: only the timer for the CURRENTLY recorded reset fires the
        # notice — a later quota-hit on the same provider overwrites
        # set_quota_reset_at with a newer reset_at, and that newer timer
        # owns the notice instead.
        if provider_state.quota_reset_at(provider) != reset_at:
            return
        provider_state.clear_quota_reset(provider)
        msg = f"⏰ [auto-resume] {provider} quota reset แล้ว — กลับมาใช้ปกติได้"
        self._notify_lead(project, msg, note="quota_provider_reset", kind="quota-reset")
        _log_event("provider_quota_reset", project=project, provider=provider)

    # ── signal (b) confirmation (background thread → Qt signal) ─────────
    def _confirm_limit_via_usage_async(self, project: str, role: str) -> None:
        from . import user_profile

        config_dir = user_profile.config_dir_for(project)
        threading.Thread(
            target=self._do_confirm_usage_fetch,
            args=(project, role, config_dir),
            daemon=True,
            name=f"auto-resume-confirm-{role}",
        ).start()

    def _do_confirm_usage_fetch(self, project: str, role: str, config_dir: Path) -> None:
        """Runs in a background thread — network I/O, must never touch a Qt
        widget directly. Emits `limitUsageConfirmed` so the park decision
        itself runs back on the Qt thread."""
        if effective_provider_for(role, project) != CLAUDE:
            # Defensive re-check in the worker: provider selection may change
            # after the watchdog schedules this confirmation.
            self.limitUsageConfirmed.emit(project, role, True)
            return
        try:
            # Shared-state-aware: reuses a recent poller result and honours a
            # persisted 429 backoff instead of firing an extra request that
            # would re-arm the endpoint's penalty (see limit_status module
            # comment). A pane that just banner-reported a limit makes fresh
            # telemetry likely cached moments ago anyway.
            usage = fetch_usage_shared(config_dir, max_age_s=300.0)
        except Exception:
            usage = None
        confirmed = _usage_confirms_limit(usage)
        self.limitUsageConfirmed.emit(project, role, confirmed)

    def _on_limit_usage_confirmed(self, project: str, role: str, confirmed: bool) -> None:
        """Qt-thread slot for `limitUsageConfirmed`. Re-validates against
        current state since time passed while the fetch was in flight."""
        key = f"{project}::{role}"
        ps = self._pane_state.get(key)
        if ps is None:
            return  # pane torn down (done()/close()) while the fetch ran
        ps.limit_confirm_pending = False
        if ps.limit_park_stopped or ps.limit_parked:
            return
        if not ps.last_assigned_task or not ps.rate_limited_until:
            return  # task finished, or the limit already cleared meanwhile

        if not confirmed:
            _log_event("pane_limit_confirm_failed", role=role, project=project)
            return  # signal (b) disagreed — stay on the notify-only path

        self._reroute_or_park(project, role, ps)

    # ── park ──────────────────────────────────────────────────────────────
    def _park_pane_for_limit(self, project: str, role: str, ps: PaneState) -> None:
        ps.limit_parked = True
        ps.limit_park_rounds += 1
        reset_at = ps.rate_limited_until
        # #158: snapshot task/cwd/output to disk while parked, so a pane that
        # crashes (rather than cleanly waking) still leaves a recoverable
        # trail instead of silently losing the in-progress task.
        pane = self._panes_by_project.get(project, {}).get(role)
        _write_progress_marker(project, role, ps, pane, status="parked")
        _log_event(
            "pane_limit_parked",
            role=role,
            project=project,
            reset_at=reset_at,
            round=ps.limit_park_rounds,
        )
        msg = (
            f"🌙 [auto-resume] {role} ({project}) ชน usage limit — park ไว้ "
            f"(รอบ {ps.limit_park_rounds}/{auto_resume.MAX_PARK_ROUNDS}) "
            "ปลุกทำงานต่ออัตโนมัติตอน quota reset"
        )
        self._notify_lead(project, msg, from_role=role, note="limit_parked", kind="limit-parked")
        delay_ms = max(0, int((reset_at + auto_resume.WAKE_BUFFER_S - time.time()) * 1000))
        QTimer.singleShot(delay_ms, lambda: self._wake_parked_pane(project, role))

    # ── wake ──────────────────────────────────────────────────────────────
    def _wake_parked_pane(self, project: str, role: str) -> None:
        key = f"{project}::{role}"
        ps = self._pane_state.get(key)
        if ps is None or not ps.limit_parked:
            return  # torn down, or already handled by another path
        pane = self._panes_by_project.get(project, {}).get(role)
        if pane is None or pane.session is None or not pane.session.is_alive:
            ps.limit_parked = False
            _log_event("pane_limit_wake_skipped", role=role, project=project, reason="pane_gone")
            return
        if not ps.last_assigned_task:
            ps.limit_parked = False
            _log_event("pane_limit_wake_skipped", role=role, project=project, reason="task_done")
            return

        # #322: Claude Code 2.1.234+ auto-continues the session on its own
        # once the usage window resets — if the limit banner is already gone
        # by the time our WAKE_BUFFER_S-delayed timer fires, the CLI beat us
        # to it and the pane may already be mid-turn again. Blindly writing
        # our own nudge + Enter on top of that risks landing inside live
        # generation (same class of race as the A3 draft-hold incident).
        # Re-check signal (a) right before injecting instead of trusting the
        # stale rate_limited_until snapshot. Fails safe (still_limited=True,
        # legacy nudge path) if the re-check itself errors.
        try:
            still_limited = (
                pane.session.rate_limit_reset_at(ps.quota_provider or "claude") is not None
            )
        except Exception:
            still_limited = True
        ps.limit_parked = False
        ps.limit_park_wake_ts = time.time()
        ps.rate_limited_until = 0.0  # let the rate-limit watchdog run normally again
        ps.last_content_change_ts = time.time()  # #53: don't false-trigger the stuck detector
        if not still_limited:
            # #158: mark the on-disk snapshot resumed rather than deleting it —
            # cheap audit trail of the park→wake cycle, harmless if it's
            # overwritten again by the next park.
            _write_progress_marker(
                project, role, ps, pane, status="resumed", reason="cli_auto_continued"
            )
            _log_event(
                "pane_limit_resumed_by_cli",
                role=role,
                project=project,
                round=ps.limit_park_rounds,
            )
            lead_msg = (
                f"🌙 [auto-resume] {role} ({project}) — Claude ทำงานต่อเองแล้วก่อน cockpit ปลุก "
                "(auto-continue, claude 2.1.234+) — ไม่ต้อง nudge ซ้ำ"
            )
            self._notify_lead(
                project, lead_msg, from_role=role, note="limit_resumed_self", kind="limit-resumed"
            )
            return
        # #158: mark the on-disk snapshot resumed rather than deleting it —
        # cheap audit trail of the park→wake cycle, harmless if it's
        # overwritten again by the next park.
        _write_progress_marker(project, role, ps, pane, status="resumed")

        msg = "⏰ quota reset แล้ว — ทำงานต่อจาก task ที่ค้างไว้ ถ้าเสร็จแล้วรายงานด้วย `takkub done`"
        _wake_sess = pane.session
        _wake_sess.write(msg)
        _delayed_enter(pane, _wake_sess, 150)
        _log_event("pane_limit_resumed", role=role, project=project, round=ps.limit_park_rounds)

        lead_msg = f"🌙 [auto-resume] {role} ({project}) ปลุกทำงานต่อแล้ว (task ค้าง resume)"
        self._notify_lead(
            project, lead_msg, from_role=role, note="limit_resumed", kind="limit-resumed"
        )

    # ── restore across cockpit restart (#495) ───────────────────────────
    def _restore_parked_pane(self, project: str, role: str, last_task: str) -> dict | None:
        """Called by `restore_teammates()` (orchestrator.py) right before it
        would otherwise immediately re-send *last_task* to a freshly
        respawned pane.

        #495: `_park_pane_for_limit`'s wake `QTimer` and
        `PaneState.limit_parked`/`rate_limited_until` live only in memory —
        a cockpit restart drops both silently even though the on-disk
        progress marker still says "parked" and still knows the original
        reset time. This reads that marker back and, if the parked window
        hasn't actually elapsed yet, re-arms the same wake the pane would
        have gotten had cockpit never restarted — instead of either
        blasting the task at a pane that is still rate-limited, or the park
        just vanishing with no trace.

        Returns None when there is nothing to do (no marker for this pane,
        the marker isn't a "parked" one, or its task text no longer matches
        *last_task* — e.g. the role got reassigned to something else before
        crashing) — caller proceeds exactly as before. Otherwise returns
        ``{"skip_resend": bool, "notice": str}``: ``skip_resend`` True means
        the caller must NOT `_send_when_ready` right now — a QTimer has been
        armed to do that later; False means the window already reset (or
        can't be trusted) so the caller should resend immediately as usual,
        using ``notice`` in place of its own generic restore text."""
        marker = _read_progress_marker(project, role)
        if not marker or marker.get("status") != "parked":
            return None
        marker_task = marker.get("task")
        if marker_task and marker_task != last_task:
            return None  # stale marker from an earlier, unrelated task

        key = f"{project}::{role}"
        ps = self._ps(key)
        ps.last_assigned_task = last_task
        ps.quota_provider = marker.get("quota_provider") or ps.quota_provider
        try:
            ps.limit_park_rounds = max(ps.limit_park_rounds, int(marker.get("park_rounds") or 0))
        except (TypeError, ValueError):
            pass
        reset_at = float(marker.get("reset_at") or 0.0)
        now = time.time()

        if not auto_resume.is_enabled() or reset_at <= 0:
            _log_event(
                "pane_limit_park_restore_dropped",
                role=role,
                project=project,
                reason="disabled" if not auto_resume.is_enabled() else "no_reset_at",
            )
            return {
                "skip_resend": False,
                "notice": (
                    f"⚠️ [cockpit restart] {role} pane เจอ park ค้างจาก session ก่อนหน้า "
                    "(auto-resume ปิดอยู่ตอนนี้ หรือ marker เก่าไม่มีเวลา reset บันทึกไว้) "
                    "— ส่ง task ต่อทันทีแทนที่จะรอ ถ้ายังติด usage limit จริงให้ "
                    "park/มอบงานใหม่เอง"
                ),
            }

        if now >= reset_at + auto_resume.WAKE_BUFFER_S:
            # Quota already reset while cockpit was down — nothing to wait
            # for, just resend now (normal flow) with an explanatory note.
            _log_event("pane_limit_park_restored_elapsed", role=role, project=project)
            return {
                "skip_resend": False,
                "notice": (
                    f"🌙 [auto-resume] {role} pane เจอ park ค้างจาก session ก่อนหน้า — "
                    "quota reset ไปแล้วระหว่าง cockpit ปิดอยู่ ส่ง task ต่อทันที"
                ),
            }

        ps.rate_limited_until = reset_at
        ps.limit_parked = True
        delay_ms = max(0, int((reset_at + auto_resume.WAKE_BUFFER_S - now) * 1000))
        QTimer.singleShot(delay_ms, lambda: self._wake_parked_pane(project, role))
        _log_event(
            "pane_limit_park_restored",
            role=role,
            project=project,
            reset_at=reset_at,
            round=ps.limit_park_rounds,
        )
        return {
            "skip_resend": True,
            "notice": (
                f"🌙 [auto-resume] {role} pane ยัง park ค้างจาก session ก่อน cockpit "
                f"restart — quota ยังไม่ reset (รอบ {ps.limit_park_rounds}/"
                f"{auto_resume.MAX_PARK_ROUNDS}) ปลุกทำงานต่ออัตโนมัติตามเวลาเดิม "
                "ไม่ต้องสั่งเอง"
            ),
        }
