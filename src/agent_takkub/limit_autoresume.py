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
  TEAMMATE pane — not just a Lead notice — so work actually resumes.
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

import threading
import time
from pathlib import Path

from PyQt6.QtCore import QTimer

from . import auto_resume
from .agent_pane import AgentPane
from .lead_inbox import _delayed_enter
from .limit_status import UsageData, fetch_usage_shared
from .orchestrator_text import _log_event
from .provider_config import CLAUDE, effective_provider_for
from .roles import LEAD
from .spawn_engine import PaneState


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


class AutoResumeMixin:
    """Methods assume `self` is an `Orchestrator` (SpawnEngineMixin's
    `_ps`/`_pane_state`/`_panes_by_project`, LeadInboxMixin's `_notify_lead`,
    and the `limitUsageConfirmed` / `autoResumeChanged` signals declared on
    the class)."""

    # ── toggle (status-bar chip) ─────────────────────────────────────────
    def set_auto_resume(self, enabled: bool) -> tuple[bool, str]:
        """Persist the auto-resume toggle and broadcast it to every live
        Lead pane, mirroring `set_exec_mode`."""
        enabled = bool(enabled)
        auto_resume.set_enabled(enabled)
        notice = (
            "[system] auto-resume 🌙 ON — a teammate pane that hits its usage "
            "limit while a task is still pending is now parked and woken "
            "automatically when the window resets, instead of only "
            "notifying you."
            if enabled
            else "[system] auto-resume 🌙 OFF — usage-limit panes are notify-only again."
        )
        for _project_ns, panes in self._panes_by_project.items():
            lead = panes.get(LEAD.name)
            if lead and lead.session and lead.session.is_alive:
                _ar_sess = lead.session
                _ar_sess.write(notice)
                _delayed_enter(lead, _ar_sess, 150)
                self.leadInjected.emit(notice)
        self.autoResumeChanged.emit(enabled)
        _log_event("auto_resume_set", enabled=enabled)
        return True, f"auto-resume {'enabled' if enabled else 'disabled'}"

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
            self._park_pane_for_limit(project, role, ps)
            return

        ps.limit_confirm_pending = True
        self._confirm_limit_via_usage_async(project, role)

    def _give_up_auto_resume(self, project: str, role: str, ps: PaneState, *, reason: str) -> None:
        ps.limit_park_stopped = True
        why = (
            "ชน limit ซ้ำเร็วเกินไปหลังปลุก"
            if reason == "relimit_within_grace"
            else f"park/wake ครบ {auto_resume.MAX_PARK_ROUNDS} รอบแล้ว"
        )
        msg = (
            f"🌙⚠️ [auto-resume] {role} ({project}) หยุด auto-resume ให้ task นี้ "
            f"({why}) — ตัดสินใจต่อเอง (nudge ต่อ/มอบงานใหม่)"
        )
        self._notify_lead(project, msg, from_role=role, note=reason)
        _log_event("pane_limit_autoresume_stopped", role=role, project=project, reason=reason)

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

        self._park_pane_for_limit(project, role, ps)

    # ── park ──────────────────────────────────────────────────────────────
    def _park_pane_for_limit(self, project: str, role: str, ps: PaneState) -> None:
        ps.limit_parked = True
        ps.limit_park_rounds += 1
        reset_at = ps.rate_limited_until
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
        self._notify_lead(project, msg, from_role=role, note="limit_parked")
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

        ps.limit_parked = False
        ps.limit_park_wake_ts = time.time()
        ps.rate_limited_until = 0.0  # let the rate-limit watchdog run normally again
        ps.last_content_change_ts = time.time()  # #53: don't false-trigger the stuck detector

        msg = "⏰ quota reset แล้ว — ทำงานต่อจาก task ที่ค้างไว้ ถ้าเสร็จแล้วรายงานด้วย `takkub done`"
        _wake_sess = pane.session
        _wake_sess.write(msg)
        _delayed_enter(pane, _wake_sess, 150)
        _log_event("pane_limit_resumed", role=role, project=project, round=ps.limit_park_rounds)

        lead_msg = f"🌙 [auto-resume] {role} ({project}) ปลุกทำงานต่อแล้ว (task ค้าง resume)"
        self._notify_lead(project, lead_msg, from_role=role, note="limit_resumed")
