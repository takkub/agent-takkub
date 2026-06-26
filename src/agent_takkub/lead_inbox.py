"""Lead-inbox queue cluster — ready-prompt-aware serialised delivery to Lead.

Extracted from orchestrator.py (lead_notify_pump cluster, refactor round 5).
Provides ``LeadInboxMixin`` which ``Orchestrator`` inherits so all queue
ownership stays in one module.

Also houses the pane submit helpers (_delayed_enter / _delayed_enter_verified)
that the cluster methods depend on and that were previously module-level in
orchestrator.py.

Layer rule (enforced by import-linter "lead-inbox-layer" contract):
  lead_inbox MUST NOT import orchestrator / main_window / app / cli.

State ownership rule: _lead_notify_queue, _pending_lead_cc,
_pending_done_notices, _lead_notify_pumping, _lead_notify_retry MUST stay in
Orchestrator.__init__.  This mixin only defines methods — never the initial
dict assignments — so queue ownership stays centralised and divergence bugs
cannot creep back in.
"""

from __future__ import annotations

import collections
import json
import pathlib
import sys as _sys
import time

from PyQt6.QtCore import QTimer

from .agent_pane import AgentPane
from .config import RUNTIME_DIR as _RUNTIME_DIR_DEFAULT
from .config import ensure_runtime as _ensure_runtime_default
from .orchestrator_text import (
    _enter_delay_ms,
    _log_event,
    _paste_payload,
    _sanitize_pane_text,
)
from .pipeline_executor import _split_shard
from .pty_session import PtySession
from .roles import LEAD


def _orch_attr(name: str, default):
    """Read an attribute from the orchestrator module facade at call time.

    Tests patch agent_takkub.orchestrator.<name>; using _orch_attr lets those
    patches propagate into lead_inbox methods without a top-level import cycle.
    Falls back to *default* when orchestrator is not yet in sys.modules.
    """
    m = _sys.modules.get("agent_takkub.orchestrator")
    return getattr(m, name, default) if m is not None else default


# ── Submit helpers (moved from orchestrator.py module scope) ─────────────────

# Self-healing submit constants (see _delayed_enter_verified docstring).
_SUBMIT_VERIFY_GRACE_MS = 600
_SUBMIT_MAX_RESENDS = 3

# Render-settle guard for the repaste branch (duplicate-paste spam fix).
# Under parallel-spawn CPU load claude renders its `[Pasted text +N lines]`
# placeholder slower than _SUBMIT_VERIFY_GRACE_MS, so the input box momentarily
# reads EMPTY and the self-heal mistakes a still-painting paste for a swallowed
# one — repasting up to _SUBMIT_MAX_RESENDS times (the visible "4× [Pasted
# text]" stack). A pane still rendering is producing output, so before repasting
# we re-poll while output stayed recent within _RENDER_ACTIVE_S, bounded by
# _RENDER_WAIT_MAX cycles. A truly swallowed paste leaves the pane idle (no
# recent output) → skips the wait and repastes at once, preserving the #26 fix.
_RENDER_ACTIVE_S = 1.0
_RENDER_WAIT_MAX = 6

# Ready-prompt poll cadence for _send_when_ready (task delivery). Lowered from
# 1000/500 → 300/150 so a task lands almost as soon as the pane hits idle — the
# old 1 s lead-in + 500 ms poll added up to ~1.5 s of avoidable lag even on an
# already-idle pane. elapsed[] still accumulates by the poll interval so the 45 s
# hard timeout stays wall-clock accurate.
_READY_POLL_FIRST_MS = 300
_READY_POLL_INTERVAL_MS = 150

# After this many consecutive 400-ms busy-retries (~30 s) the pump gives up
# and spills remaining items to the durable _pending_done_notices queue.
# Prevents unbounded memory growth and ensures delivery survives a crash that
# occurs while Lead is alive-but-wedged.
LEAD_NOTIFY_BUSY_CAP = 75

# Staleness escalation for the durable reaper (#70). When spilled done-notices
# can't be flushed because the Lead reads as not-ready for this long, the
# reaper force-delivers them anyway — guarding against an is_at_ready_prompt()
# false-negative (a blocker marker in the Lead's visible conversation makes an
# idle Lead read as busy → notices stranded forever, the #70 multi-project
# stall). 60 s ≫ a real Lead turn, so a genuinely-busy Lead is rarely hit; if it
# is, claude buffers the pasted input and processes it next.
_DONE_NOTICE_STALE_S = 60.0


def _delayed_enter(pane: AgentPane, session: PtySession, delay_ms: int) -> None:
    """Schedule CR into pane after delay_ms, no-op if session has changed.

    Captures the session object at call time so the lambda cannot reach a
    replacement session if the pane is closed and respawned before the timer fires.
    """
    QTimer.singleShot(
        delay_ms,
        lambda: pane.session is session and pane.session.write(b"\r"),
    )


def _delayed_enter_verified(
    pane: AgentPane,
    session: PtySession,
    delay_ms: int,
    *,
    max_resends: int = _SUBMIT_MAX_RESENDS,
    on_resend=None,
    payload: str | None = None,
    content_fragment: str = "",
    on_repaste=None,
) -> None:
    """Like `_delayed_enter`, but recovers a submit that was swallowed.

    Sends the submitting CR after ``delay_ms`` (same as `_delayed_enter`), then
    ``_SUBMIT_VERIFY_GRACE_MS`` later checks `is_at_ready_prompt()`. If the pane
    is STILL at its ready prompt the submit did not land (a real submit would
    have flipped it to busy), so it recovers — bounded by ``max_resends``.

    Two failure modes are distinguished when ``payload`` is supplied (#79):
      • Enter swallowed mid-paste-render — the input box still holds the pasted
        content (``shows_pending_input`` True). Re-send the CR only. (#22)
      • Paste swallowed — the input box is empty (``shows_pending_input`` False),
        so a CR resend has nothing to submit and the pane sits idle forever
        ("pane stays empty" — the #26 symptom). Re-paste the payload, then submit
        after the render settles.
    When ``payload`` is None the old behaviour (CR resend only) is preserved.

    ``on_resend`` / ``on_repaste`` (optional) are invoked with the
    remaining-attempt count each time the respective recovery fires, so the
    caller can log/observe it.
    """

    # Output-timestamp baseline captured the instant we begin verifying (the
    # caller has just written the paste). Output that arrives AFTER this proves
    # claude received the paste, which the repaste branch uses to avoid the
    # duplicate-paste false-positive. A list cell so the repaste branch can
    # re-baseline against its own write without `nonlocal`. Best-effort: a fake
    # session without the accessor falls back to 0.0 (repaste path unchanged).
    paste_baseline = [
        session.last_output_monotonic() if hasattr(session, "last_output_monotonic") else 0.0
    ]

    def _send_then_verify(remaining: int) -> None:
        if pane.session is not session:
            return
        pane.session.write(b"\r")

        def _verify(render_waits: int = _RENDER_WAIT_MAX) -> None:
            if pane.session is not session or remaining <= 0:
                return
            # Submit landed → pane is busy → is_at_ready_prompt() is False → stop.
            if not session.is_at_ready_prompt():
                return
            # Still ready → submit didn't land. If we have the payload and the
            # input box is empty, the PASTE may have been swallowed (#26) — but
            # "ready prompt + empty box" is ALSO exactly what a paste that was
            # SUBMITTED successfully looks like once claude returns to idle, and
            # what a landed paste whose `[Pasted text]` placeholder scrolled out
            # of the scanned footer rows looks like. Repasting in those cases is
            # the false-positive that stacked duplicate tasks in the input box —
            # near-universal under concurrent multi-project load (the visible
            # "เบิ้ลตามจำนวนโปรเจค"). Decide with a structural signal instead of
            # the ambiguous box state.
            if payload is not None and not session.shows_pending_input(content_fragment):
                # Did claude produce ANY output since we pasted? A paste that
                # landed renders a placeholder / streams a reply (timestamp
                # advances past the baseline); a swallowed paste leaves the pane
                # completely silent (timestamp unchanged). Output-since-paste ⇒
                # the bytes were received, so an empty box now means it was
                # SUBMITTED, not lost. Recover with a bare CR only (harmless
                # no-op if already submitted; submits a swallowed-CR #22 paste) —
                # never a second [Pasted text]. A fake session without the
                # accessor degrades to the prior render-guard behaviour.
                produced_output = (
                    session.last_output_monotonic() > paste_baseline[0]
                    if hasattr(session, "last_output_monotonic")
                    else False
                )
                if produced_output:
                    if on_resend is not None:
                        on_resend(remaining)
                    _send_then_verify(remaining - 1)
                    return
                # No output yet since the paste. Could be a genuine swallow, or a
                # placeholder that hasn't started painting under load — wait out a
                # bounded grace for output to appear before concluding the bytes
                # were dropped (repasting into a slow-rendering box is what
                # stacked 4× [Pasted text]).
                if render_waits > 0 and session.seconds_since_output() < _RENDER_ACTIVE_S:
                    QTimer.singleShot(_SUBMIT_VERIFY_GRACE_MS, lambda: _verify(render_waits - 1))
                    return
                # Silent through the grace window → genuine swallow (#26):
                # re-paste, then submit once it renders. Re-baseline so the next
                # verify measures output produced by THIS repaste.
                if on_repaste is not None:
                    on_repaste(remaining)
                session.write(payload)
                paste_baseline[0] = session.last_output_monotonic()
                QTimer.singleShot(
                    _enter_delay_ms(payload),
                    lambda: _send_then_verify(remaining - 1),
                )
                return
            # Content present, CR swallowed mid-render (#22) — resend the CR.
            if on_resend is not None:
                on_resend(remaining)
            _send_then_verify(remaining - 1)

        QTimer.singleShot(_SUBMIT_VERIFY_GRACE_MS, _verify)

    QTimer.singleShot(delay_ms, lambda: _send_then_verify(max_resends))


# ── Mixin ─────────────────────────────────────────────────────────────────────


class LeadInboxMixin:
    """Lead-inbox queue and delivery methods for Orchestrator.

    All methods resolve through the combined MRO; state dicts
    (_lead_notify_queue, _pending_lead_cc, _pending_done_notices,
    _lead_notify_pumping, _lead_notify_retry) are initialised in
    Orchestrator.__init__ — never here — so ownership stays centralised.

    Dependencies on the host class (Orchestrator):
      _project_panes(project)          — spawn_engine cluster
      _resolve_project(project)        — static helper
      leadInjected.emit(body)          — Qt signal on Orchestrator
      _shard_groups: dict              — state, kept in Orchestrator.__init__
      _pane_state:   dict[str, ...]    — state, kept in Orchestrator.__init__
      _inject_shard_fanout_handoff()   — pipeline_executor cluster
    """

    # ------------------------------------------------------------------
    # Pipeline bridge (thin wrapper so pipeline_executor can call self.)
    # ------------------------------------------------------------------

    def _inject_to_lead(
        self, project_ns: str, message: str, log_event: str = "lead_inject"
    ) -> None:
        """Write *message* to the Lead pane. If Lead is absent, queue it in
        _pending_done_notices so it is delivered when Lead next spawns."""
        self._notify_lead(project_ns, message)
        _log_event(log_event, project=project_ns)

    # ------------------------------------------------------------------
    # Delivery helpers
    # ------------------------------------------------------------------

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
            _slash_sess = pane.session
            payload = _paste_payload(command)
            _slash_sess.write(payload)
            _orch_attr("_delayed_enter", _delayed_enter)(
                pane, _slash_sess, _enter_delay_ms(payload)
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

    def inject_lead_prompt(self, prompt: str, project: str | None = None) -> bool:
        """Paste a prompt into a project's Lead pane and submit it.

        For status-bar buttons that hand a task to the Lead instead of running
        a native GUI flow (e.g. the ⬆ Claude CLI button asks the Lead to
        check/report the CLI version rather than popping its own dialog).
        Writes immediately if the Lead is alive; otherwise queues via
        `_pending_done_notices` so it lands on the next Lead spawn. Returns
        True when delivered live, False when queued (no live Lead).
        """
        project_ns = self._resolve_project(project)
        lead = self._project_panes(project_ns).get(LEAD.name)
        if lead and lead.session and lead.session.is_alive:
            self._notify_lead(project_ns, prompt)
            _log_event("inject_lead_prompt", project=project_ns)
            return True
        self._pending_done_notices.setdefault(project_ns, []).append(
            {"role": "system", "note": "lead prompt", "body": prompt}
        )
        self._save_pending_done_notices(project_ns)
        _log_event("inject_lead_prompt_queued", project=project_ns)
        return False

    def _ready_wait_ms(self, role_name: str, project: str | None, max_wait_ms: int) -> int:
        """Effective ready-prompt wait window for a pane.

        agy (the gemini role's engine) cold-boots far slower than claude/codex
        — a 146 MB self-contained binary that also scans the workspace on launch
        routinely needs ~45-60s to render its first ready prompt, landing right
        at the default 45s edge and forcing a fragile blind paste (#26). Give
        gemini/agy panes a longer window so first-assign delivery is confirmed,
        not blind. An explicit non-default ``max_wait_ms`` from the caller always
        wins (e.g. the short-poll peer-send path).
        """
        if max_wait_ms != 45_000:
            return max_wait_ms
        try:
            from .provider_config import GEMINI, effective_provider_for

            if effective_provider_for(role_name, project=self._resolve_project(project)) == GEMINI:
                return 90_000
        except Exception:
            pass
        return max_wait_ms

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
        max_wait_ms = self._ready_wait_ms(role_name, project, max_wait_ms)
        pane = self._project_panes(project).get(role_name)
        if pane is None:
            return
        elapsed = [0]
        sent = [False]

        def _deliver(unconfirmed: bool = False) -> None:
            if sent[0]:
                return
            sent[0] = True
            if pane.session is None or not pane.session.is_alive:
                return
            pane.set_state("working", note=task[:60])
            _task_sess = pane.session
            payload = _paste_payload(_sanitize_pane_text(task))
            _task_sess.write(payload)
            # Self-healing submit: the task pastes as a `[Pasted text]` placeholder
            # and an Enter landing mid-render is swallowed, leaving the teammate
            # sitting on the placeholder forever instead of running the spec — the
            # original #22 symptom. _deliver only runs once the pane is at its ready
            # prompt (or blind on timeout), so verifying the submit landed and
            # resending is safe (a busy/booting pane is not "ready" → no resend).
            _orch_attr("_delayed_enter_verified", _delayed_enter_verified)(
                pane,
                _task_sess,
                _enter_delay_ms(payload),
                payload=payload,
                content_fragment=task,
                on_resend=lambda rem, r=role_name, p=project: _log_event(
                    "task_deliver_enter_resend",
                    project=self._resolve_project(p),
                    role=r,
                    remaining=rem,
                ),
                on_repaste=lambda rem, r=role_name, p=project: _log_event(
                    "task_deliver_repaste",
                    project=self._resolve_project(p),
                    role=r,
                    remaining=rem,
                ),
            )
            if unconfirmed:
                # Delivered blind — the pane never signalled ready, so on a cold
                # re-spawn the paste may have been swallowed (issue #26). Surface
                # it to the Lead instead of letting delegation fail silently.
                self._warn_lead_delivery_unconfirmed(role_name, project)

        def _check() -> None:
            if sent[0]:
                return
            if pane.session is None or not pane.session.is_alive:
                # Session absent or not yet alive — may be deferred by the
                # spawn gate.  Keep waiting rather than silently dropping the
                # task; the gate retry will attach the session within seconds.
                elapsed[0] += _READY_POLL_INTERVAL_MS
                if elapsed[0] < max_wait_ms:
                    QTimer.singleShot(_READY_POLL_INTERVAL_MS, _check)
                return
            if pane.session.is_at_ready_prompt():
                _deliver()
                return
            elapsed[0] += _READY_POLL_INTERVAL_MS
            if elapsed[0] >= max_wait_ms:
                # Hard timeout: pane never reached the ready prompt. Paste
                # best-effort (markers may be a false negative) but flag it as
                # unconfirmed so the Lead verifies/re-assigns rather than
                # assuming the task landed (issue #26).
                _deliver(unconfirmed=True)
                return
            QTimer.singleShot(_READY_POLL_INTERVAL_MS, _check)

        QTimer.singleShot(_READY_POLL_FIRST_MS, _check)

    def _warn_lead_delivery_unconfirmed(self, role_name: str, project: str | None) -> None:
        """Tell the Lead that an assign hit the 45s hard timeout without the
        target pane ever signalling ready. The task was pasted blind and may
        not have landed (cold re-spawn render differs from boot), so the Lead
        should verify / re-assign instead of trusting the 'task queued' reply
        (issue #26). No-op when warning the Lead about itself."""
        if role_name == LEAD.name:
            return
        project_ns = self._resolve_project(project)
        lead = self._project_panes(project_ns).get(LEAD.name)
        if not (lead and lead.session and lead.session.is_alive):
            return
        msg = (
            f"⚠️ [delivery-unconfirmed] {role_name} pane ไม่ถึง ready prompt ใน 45s — "
            f"task ถูก paste แบบ blind อาจไม่ติด (pane อาจค้าง empty). "
            f"เช็ค pane / re-assign ถ้ายังว่าง — อย่าถือว่าส่งสำเร็จ (issue #26)"
        )
        self._notify_lead(project_ns, msg)
        _log_event("delivery_unconfirmed", role=role_name, project=project_ns)

    def _warn_lead_spawn_failed(self, role_name: str, project: str | None, reason: str) -> None:
        """Tell the Lead that an assign's pane spawn failed. The CLI acks
        'task queued' to the Lead's shell before the async spawn runs, so a
        spawn failure is otherwise invisible and the delegation silently dies
        (#26). No-op when the failed role is the Lead itself."""
        if role_name == LEAD.name:
            return
        project_ns = self._resolve_project(project)
        lead = self._project_panes(project_ns).get(LEAD.name)
        if not (lead and lead.session and lead.session.is_alive):
            return
        msg = (
            f"⚠️ [spawn-failed] {role_name} pane สร้างไม่สำเร็จ — task ไม่ได้ส่ง ({reason}). "
            f"ลอง assign {role_name} ใหม่อีกครั้ง (ถ้ายิง parallel ลองยิงทีละตัว) — "
            f"อย่าถือว่า 'task queued' = สำเร็จ (issue #26)"
        )
        self._notify_lead(project_ns, msg)
        _log_event("spawn_failed_warned", role=role_name, project=project_ns)

    def _warn_lead_respawn_capped(self, role_name: str, project: str) -> None:
        """Bug-3 fix: tell Lead that a teammate hit AUTO_RESPAWN_MAX and gave up.

        Without this notice the Lead never learns the pane is permanently down,
        auto-chain siblings wait forever for a done event that never comes, and
        the operator stares at a deadlocked workflow.  No-op when Lead is absent
        (queuing is not needed — if Lead comes back, respawn is already capped
        and the operator will see the dead pane slot directly).

        #4 fix: shard failed-bookkeeping runs BEFORE the Lead-alive early return
        so the group closes (and fires a handoff via queue) even when Lead is down.
        """
        project_ns = self._resolve_project(project)

        # #4: hoist shard bookkeeping before the Lead-alive gate so the group
        # closes and queues its handoff regardless of whether Lead is running.
        base_role_c, shard_idx_c = _split_shard(role_name)
        if shard_idx_c is not None:
            key_c = f"{project_ns}::{role_name}"
            # Access pane state without importing PaneState (avoid orchestrator cycle).
            # The dict value is a PaneState instance when present; None means not tracked.
            ps_c = getattr(self, "_pane_state", {}).get(key_c)
            if ps_c is not None and ps_c.shard_total > 0:
                group_key_c = f"{project_ns}::{base_role_c}"
                group_c = self._shard_groups.get(group_key_c)
                if group_c and not group_c.closed:
                    group_c.failed.add(role_name)
                    if len(group_c.done) + len(group_c.failed) >= group_c.total:
                        group_c.closed = True
                        self._inject_shard_fanout_handoff(project_ns, group_c)
                        self._shard_groups.pop(group_key_c, None)

        lead = self._project_panes(project_ns).get(LEAD.name)
        if not (lead and lead.session and lead.session.is_alive):
            return
        # Read AUTO_RESPAWN_MAX from orchestrator module via sys.modules to avoid a
        # top-level import cycle (lead_inbox is forbidden from importing orchestrator).
        _orch_m = _sys.modules.get("agent_takkub.orchestrator")
        auto_respawn_max = getattr(_orch_m, "AUTO_RESPAWN_MAX", 2) if _orch_m else 2
        msg = (
            f"⚠️ [respawn-capped] {role_name} ({project_ns}) หยุด auto-respawn แล้ว "
            f"(crash {auto_respawn_max} ครั้งติด) — pane ดับถาวรจนกว่า Lead จะ assign ใหม่ "
            f"ถ้า pane นี้อยู่ใน auto-chain verify hop อาจค้างได้ — ตรวจสอบ takkub list"
        )
        self._notify_lead(project_ns, msg)
        _log_event("respawn_capped_warned", role=role_name, project=project_ns)

    # ------------------------------------------------------------------
    # Peer CC durability helpers
    # ------------------------------------------------------------------

    def _pending_cc_path(self, project_ns: str) -> pathlib.Path:
        return (
            _orch_attr("RUNTIME_DIR", _RUNTIME_DIR_DEFAULT) / f"pending-lead-cc-{project_ns}.json"
        )

    def _save_pending_cc(self, project_ns: str) -> None:
        """Persist current queue for project_ns so it survives orchestrator restart."""
        try:
            _orch_attr("ensure_runtime", _ensure_runtime_default)()
            queue = self._pending_lead_cc.get(project_ns, [])
            path = self._pending_cc_path(project_ns)
            if not queue:
                path.unlink(missing_ok=True)
                return
            path.write_text(json.dumps(queue, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _load_pending_cc(self) -> None:
        """Restore queued CC messages from disk on startup."""
        try:
            _orch_attr("ensure_runtime", _ensure_runtime_default)()
            runtime_dir = _orch_attr("RUNTIME_DIR", _RUNTIME_DIR_DEFAULT)
            for p in runtime_dir.glob("pending-lead-cc-*.json"):
                proj = p.stem[len("pending-lead-cc-") :]
                try:
                    items = json.loads(p.read_text(encoding="utf-8"))
                    if items:
                        self._pending_lead_cc[proj] = items
                except Exception:
                    pass
        except Exception:
            pass

    def _pending_done_path(self, project_ns: str) -> pathlib.Path:
        return (
            _orch_attr("RUNTIME_DIR", _RUNTIME_DIR_DEFAULT)
            / f"pending-done-notices-{project_ns}.json"
        )

    def _save_pending_done_notices(self, project_ns: str) -> None:
        """Persist queued done notices so they survive an orchestrator restart
        while the Lead is down (issue #13). Mirrors _save_pending_cc."""
        try:
            _orch_attr("ensure_runtime", _ensure_runtime_default)()
            queue = self._pending_done_notices.get(project_ns, [])
            path = self._pending_done_path(project_ns)
            if not queue:
                path.unlink(missing_ok=True)
                return
            path.write_text(json.dumps(queue, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _load_pending_done_notices(self) -> None:
        """Restore queued done notices from disk on startup. Mirrors
        _load_pending_cc."""
        try:
            _orch_attr("ensure_runtime", _ensure_runtime_default)()
            runtime_dir = _orch_attr("RUNTIME_DIR", _RUNTIME_DIR_DEFAULT)
            for p in runtime_dir.glob("pending-done-notices-*.json"):
                proj = p.stem[len("pending-done-notices-") :]
                try:
                    items = json.loads(p.read_text(encoding="utf-8"))
                    if items:
                        self._pending_done_notices[proj] = items
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
        # Deliver-then-dequeue: write each CC first and drop ONLY what was
        # actually delivered. The previous code popped + persisted-empty BEFORE
        # writing, so a write that raised mid-flush (Lead session torn down
        # between the liveness check and the write) silently lost the remaining
        # queued messages from both memory and disk — defeating the durability
        # the persistence exists for. (M4#22: blind CC flush / pop-before-write)
        items = list(pending)
        delivered = 0
        try:
            for item in items:
                payload = _paste_payload(item["body"])
                lead.session.write(payload)
                QTimer.singleShot(
                    _enter_delay_ms(payload),
                    lambda s=lead.session: s and s.write(b"\r"),
                )
                delivered += 1
        finally:
            remaining = items[delivered:]
            if remaining:
                self._pending_lead_cc[project_ns] = remaining
            else:
                self._pending_lead_cc.pop(project_ns, None)
            self._save_pending_cc(project_ns)
        if delivered:
            _log_event("send_cc_flushed", project=project_ns, count=delivered)

    # ------------------------------------------------------------------
    # Lead-notify queue: ready-prompt-aware serialised delivery
    # ------------------------------------------------------------------

    def _notify_lead(
        self,
        project_ns: str,
        body: str,
        *,
        from_role: str = "system",
        note: str = "notify",
    ) -> None:
        """Queue *body* for delivery to the Lead pane.

        If Lead is alive the item enters the in-memory per-project queue and an
        idempotent pump is armed.  The pump blocks until Lead is at its ready
        prompt before each write so concurrent done notices never overwrite each
        other mid-generation.

        If Lead is absent the item falls directly into the durable
        _pending_done_notices (survives a restart, delivered on next Lead spawn).

        *from_role* and *note* are stored only in the durable fallback record so
        callers that care about the audit trail can pass them through; the live
        delivery path only needs *body*.

        Lazy-initialises queue / pumping-set so partial test fixtures (those that
        use Orchestrator.__new__ and bypass __init__) don't need to pre-populate
        these attributes.
        """
        lead = self._project_panes(project_ns).get(LEAD.name)
        if lead and lead.session and lead.session.is_alive:
            if not hasattr(self, "_lead_notify_queue"):
                self._lead_notify_queue = {}
            if not hasattr(self, "_lead_notify_pumping"):
                self._lead_notify_pumping = set()
            self._lead_notify_queue.setdefault(project_ns, collections.deque()).append(body)
            self._arm_lead_notify_pump(project_ns)
            # Tell the UI Lead has new mail so it can red-dot the Lead pane-tab
            # when the user is on another pane. Best-effort: a partial test
            # fixture built via Orchestrator.__new__ won't have the bound signal.
            try:
                self.leadNotified.emit(project_ns)
            except Exception:
                pass
        else:
            if not hasattr(self, "_pending_done_notices"):
                self._pending_done_notices = {}
            self._pending_done_notices.setdefault(project_ns, []).append(
                {"role": from_role, "note": note, "body": body}
            )
            self._save_pending_done_notices(project_ns)
            _log_event("done_notice_queued", project=project_ns, role=from_role)

    def _arm_lead_notify_pump(self, project_ns: str) -> None:
        """Start a pump for *project_ns* if one is not already running.

        Calls _pump_lead_notify directly (synchronously) for the first attempt so
        tests that do not run a Qt event loop still see the write happen.  Retries
        when Lead is busy are scheduled via QTimer.singleShot.
        """
        pumping: set = getattr(self, "_lead_notify_pumping", set())
        if not hasattr(self, "_lead_notify_pumping"):
            self._lead_notify_pumping = pumping
        if project_ns in pumping:
            return
        pumping.add(project_ns)
        self._pump_lead_notify(project_ns)

    def _pump_lead_notify(self, project_ns: str) -> None:
        """Deliver one notice to Lead when it is at the ready prompt, then re-arm.

        Serialises concurrent done-notices so they never overwrite each other
        mid-generation.  Falls back to _pending_done_notices when Lead dies
        while items are still in the queue.

        Busy-retry cap: after LEAD_NOTIFY_BUSY_CAP consecutive retries (~30 s) the
        remaining items are spilled to _pending_done_notices so they survive a crash
        and the hot-loop stops.
        """
        queue = getattr(self, "_lead_notify_queue", {}).get(project_ns)
        if not queue:
            pumping: set = getattr(self, "_lead_notify_pumping", set())
            pumping.discard(project_ns)
            getattr(self, "_lead_notify_retry", {}).pop(project_ns, None)
            return

        lead = self._project_panes(project_ns).get(LEAD.name)
        if not (lead and lead.session and lead.session.is_alive):
            # Lead died — move remaining items to the durable queue.
            items = list(queue)
            queue.clear()
            pumping = getattr(self, "_lead_notify_pumping", set())
            pumping.discard(project_ns)
            getattr(self, "_lead_notify_retry", {}).pop(project_ns, None)
            if not hasattr(self, "_pending_done_notices"):
                self._pending_done_notices = {}
            for b in items:
                self._pending_done_notices.setdefault(project_ns, []).append(
                    {"role": "system", "note": "notify", "body": b}
                )
            if items:
                self._save_pending_done_notices(project_ns)
            return

        if not lead.session.is_at_ready_prompt():
            # Lead is busy — check retry cap before re-scheduling.
            if not hasattr(self, "_lead_notify_retry"):
                self._lead_notify_retry = {}
            count = self._lead_notify_retry.get(project_ns, 0) + 1
            self._lead_notify_retry[project_ns] = count
            if count > LEAD_NOTIFY_BUSY_CAP:
                # Lead has been wedged too long — spill to durable and stop.
                items = list(queue)
                queue.clear()
                pumping = getattr(self, "_lead_notify_pumping", set())
                pumping.discard(project_ns)
                self._lead_notify_retry.pop(project_ns, None)
                if not hasattr(self, "_pending_done_notices"):
                    self._pending_done_notices = {}
                for b in items:
                    self._pending_done_notices.setdefault(project_ns, []).append(
                        {"role": "system", "note": "notify_spill", "body": b}
                    )
                if items:
                    self._save_pending_done_notices(project_ns)
                _log_event("lead_notify_spill", project=project_ns, count=len(items))
                return
            # Retry after a short delay without consuming the item.
            QTimer.singleShot(400, lambda: self._pump_lead_notify(project_ns))
            return

        # Lead is alive and idle — deliver one item; reset retry counter.
        getattr(self, "_lead_notify_retry", {}).pop(project_ns, None)
        body = _sanitize_pane_text(queue.popleft())
        _notify_sess = lead.session
        payload = _paste_payload(body)
        _notify_sess.write(payload)
        delay = _enter_delay_ms(payload)
        # Self-healing submit: a done-report whose Enter is swallowed mid-paste-
        # render leaves Lead idle with the report unsubmitted — it "won't run on"
        # (issue #22 residual). Verify the submit landed and resend if not.
        _orch_attr("_delayed_enter_verified", _delayed_enter_verified)(
            lead,
            _notify_sess,
            delay,
            payload=payload,
            content_fragment=body,
            on_resend=lambda rem: _log_event(
                "lead_notify_enter_resend", project=project_ns, remaining=rem
            ),
            on_repaste=lambda rem: _log_event(
                "lead_notify_repaste", project=project_ns, remaining=rem
            ),
        )
        self.leadInjected.emit(body)

        if queue:
            # More items waiting — re-arm after this item has had time to submit
            # and Lead has begun processing it (so is_at_ready_prompt drops).
            QTimer.singleShot(delay + 300, lambda: self._pump_lead_notify(project_ns))
        else:
            pumping = getattr(self, "_lead_notify_pumping", set())
            pumping.discard(project_ns)

    def _flush_pending_done_notices(self, project_ns: str) -> None:
        """Deliver queued done notices to Lead if it is currently alive.

        Called after Lead spawns. Routes all items through _notify_lead so they
        enter the ready-prompt-aware queue instead of being dumped all at once."""
        pending = self._pending_done_notices.get(project_ns)
        if not pending:
            return
        lead = self._project_panes(project_ns).get(LEAD.name)
        if not (lead and lead.session and lead.session.is_alive):
            return
        items = self._pending_done_notices.pop(project_ns)
        self._save_pending_done_notices(project_ns)
        for item in items:
            self._notify_lead(project_ns, item["body"])
        _log_event("done_notices_flushed", project=project_ns, count=len(items))

    def _reap_pending_done_notices(self) -> None:
        """Flush durable done-notices for any project whose Lead is idle.

        Runs on every idle-watchdog tick so notices spilled while Lead was busy
        (durability-cap exceeded) are delivered as soon as Lead returns to the
        ready prompt — without needing a restart.  Skips projects whose Lead is
        absent or still busy to avoid ping-pong (flush → re-spill → flush loop).
        """
        pending = getattr(self, "_pending_done_notices", None)
        if not pending:
            return
        if not hasattr(self, "_pending_done_since"):
            self._pending_done_since = {}
        now = time.time()
        for project_ns in list(pending.keys()):
            lead = self._project_panes(project_ns).get(LEAD.name)
            if not (lead and lead.session and lead.session.is_alive):
                # Lead absent — leave items durable for the next spawn; reset the
                # staleness clock so it only counts time the Lead was actually up.
                self._pending_done_since.pop(project_ns, None)
                continue
            if lead.session.is_at_ready_prompt():
                self._pending_done_since.pop(project_ns, None)
                self._flush_pending_done_notices(project_ns)
            else:
                # Lead alive but not-ready. Normally transient (Lead mid-turn);
                # the next tick retries. But is_at_ready_prompt() can be a
                # perpetual false-negative (a blocker marker in the Lead's own
                # visible conversation reads as busy — #20), which silently
                # stranded notices forever (#70: a second active project's Lead
                # never reaped). Escalate: after _DONE_NOTICE_STALE_S of an
                # alive-but-never-ready Lead, force delivery regardless of the
                # gate so the chain can never stall indefinitely.
                since = self._pending_done_since.setdefault(project_ns, now)
                if now - since >= _DONE_NOTICE_STALE_S:
                    _log_event(
                        "done_notice_force_flush",
                        project=project_ns,
                        stalled_s=round(now - since),
                        count=len(pending.get(project_ns, [])),
                    )
                    self._pending_done_since.pop(project_ns, None)
                    self._force_deliver_done_notices(project_ns)

    def _force_deliver_done_notices(self, project_ns: str) -> None:
        """Last-resort delivery for spilled done-notices when the Lead reads as
        perpetually not-ready (is_at_ready_prompt() false-negative). Bypasses the
        ready gate that _flush/_pump honour, pasting the spilled notices as a
        single combined message (one paste + one verified submit, so no
        clobbering) into the live Lead. Used only by the reaper's staleness
        escalation — see _DONE_NOTICE_STALE_S (#70)."""
        pending = getattr(self, "_pending_done_notices", {}).get(project_ns)
        if not pending:
            return
        lead = self._project_panes(project_ns).get(LEAD.name)
        if not (lead and lead.session and lead.session.is_alive):
            return
        items = self._pending_done_notices.pop(project_ns)
        self._save_pending_done_notices(project_ns)
        body = "\n\n".join(_sanitize_pane_text(item["body"]) for item in items)
        sess = lead.session
        payload = _paste_payload(body)
        sess.write(payload)
        delay = _enter_delay_ms(payload)
        _orch_attr("_delayed_enter_verified", _delayed_enter_verified)(
            lead,
            sess,
            delay,
            payload=payload,
            content_fragment=body,
            on_resend=lambda rem: _log_event(
                "done_notice_force_enter_resend", project=project_ns, remaining=rem
            ),
            on_repaste=lambda rem: _log_event(
                "done_notice_force_repaste", project=project_ns, remaining=rem
            ),
        )
        self.leadInjected.emit(body)
