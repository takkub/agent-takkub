"""Lead wait primitive (#242) — `takkub wait [--role R]... [--timeout S]`.

Before this, every Lead pane that wanted to block until a teammate's report
actually landed had to hand-roll a polling loop around `takkub status`
(unreliable timing — see #225/#236, already fixed) and had no way to stop a
stale loop from a previous turn before starting a new one. A real incident
on 2026-08-15: six such loops accumulated concurrently in one project tab,
each hitting the cockpit socket every 40s for nothing.

`LeadWaitMixin` (mixed into `Orchestrator`, same pattern as `LeadInboxMixin`)
gives Lead a single call that:
  - blocks until a role's done/FAILED report has actually left the
    digest/live/durable pipeline and been written into Lead's own pane —
    not just until the pane disappears from `takkub list` (#163's old
    failure mode) — reusing the exact `_has_pending_lead_notice` signal
    `takkub inbox` (#231) already trusts for the same question;
  - always carries a caller-supplied timeout (`begin_wait` requires one —
    see `cli.cmd_wait` for the default/cap) and reports WHY a role is still
    pending on timeout (working / stalled / stuck at a tty prompt / report
    queued but not yet delivered / role never spawned);
  - works for every provider — the poll loop only reads pane/queue state
    the orchestrator already tracks per role, nothing Claude-specific;
  - de-dupes concurrent waiters: at most one `_active_waits` registration
    per project. A second `begin_wait` call while one is outstanding
    attaches to it (unions the role set) instead of starting an
    independent poll loop, so a stray duplicate `takkub wait` can never
    multiply socket load the way the stacked hand-rolled loops did.

The actual blocking loop lives client-side in `cli.cmd_wait` (a single
`takkub wait` process's own sleep loop) — `CliServer` runs every request on
the Qt main thread (see cli_server.py's module docstring), so nothing here
may block that thread; each `poll_wait` call is a cheap, single dict/state
read, not a long-held connection.

Layer rule (mirrors `lead-inbox-layer`/`limit-autoresume-layer`, enforced by
the "lead-wait-layer" import-linter contract): this module MUST NOT import
orchestrator / main_window / app / cli.
"""

from __future__ import annotations

import time
import uuid

from .orchestrator_text import _exit_key
from .roles import LEAD

# Grace window added on top of a registration's own timeout before a stale
# `_active_waits` entry (whose owning CLI process died without calling
# `end_wait` — crash, Ctrl-C, killed pane) is treated as gone and a fresh
# `begin_wait` may replace it. Keeps a genuinely abandoned registration from
# blocking new waits forever without needing a periodic QTimer reaper.
_WAIT_STALE_GRACE_S = 120.0

# #249: a role with no pane at all yet is ambiguous for the first few seconds
# after `begin_wait` — `takkub assign` can return before the async spawn has
# actually created the pane entry. Only treat "no pane" as a hard "never
# spawned" verdict once this grace window has elapsed since the wait started.
_WAIT_NEVER_SPAWNED_GRACE_S = 15.0

# #249: pane states that can never produce a NEW report without a fresh
# spawn (which would flip the pane back to "active"/"working" on the very
# next poll tick). Before this fix `_resolve_role_wait_status` only special-
# cased "working" — every other state (including "done" itself, and "empty"/
# "exited" after the 2.5s done->auto-close) fell through to the generic
# "pending: pane state: X" branch and stayed pending until the full timeout
# elapsed, even though the role had already finished (or could never finish).
_WAIT_TERMINAL_PANE_STATES = frozenset({"empty", "done", "exited", "error"})

# #249 follow-up: how long a naturally-concluded registration's final result
# stays available (via `_wait_resolved_echo`) to a straggling attached
# client's next poll. Must comfortably exceed the CLI's own poll-interval
# backoff ceiling (`cli._WAIT_POLL_MAX_INTERVAL_S` = 15s) so a second attacher
# whose own poll simply landed a beat after the resolving one's still finds
# the echo instead of racing it.
_WAIT_RESOLVED_ECHO_GRACE_S = 30.0


class LeadWaitMixin:
    """Provides `begin_wait` / `poll_wait` / `end_wait` on `Orchestrator`.

    State ownership: `_active_waits` (per-project registration) and
    `_wait_done_events` (per (project_ns, role) last-resolution record) are
    initialised in `Orchestrator.__init__`, exactly like every other queue
    this mixin cluster depends on — this class only defines methods.
    """

    def begin_wait(self, project_ns: str, roles: list[str] | None, timeout_s: float) -> dict:
        """Register (or attach to) a wait for *roles* in *project_ns*.

        Empty/omitted *roles* defaults to every role currently tracked by
        `list_status` for this project (Lead's own pane excluded — it can't
        wait on itself). Returns ``{"ok": False, "msg": ...}`` when there is
        nothing to wait on; otherwise ``{"ok": True, "wait_id", "roles",
        "started_ts", "attached"}``.
        """
        clean_roles = [r for r in dict.fromkeys(roles or []) if r and r != LEAD.name]
        if not clean_roles:
            known = self.list_status(project=project_ns)
            clean_roles = sorted(r for r in known if r != LEAD.name)
            if not clean_roles:
                return {
                    "ok": False,
                    "msg": "nothing to wait on — no active roles in this project right now",
                }

        now = time.time()
        active = self._active_waits.get(project_ns)
        if active is not None:
            stale_after = active["timeout_s"] + _WAIT_STALE_GRACE_S
            if now - active["last_poll_ts"] < stale_after:
                # Attach: union the role sets instead of starting a second,
                # independent poll loop for the same project (the exact
                # duplication #242 exists to prevent).
                active["roles"] = sorted(set(active["roles"]) | set(clean_roles))
                active["timeout_s"] = max(active["timeout_s"], timeout_s)
                active["last_poll_ts"] = now
                return {
                    "ok": True,
                    "msg": f"attached to an existing wait ({len(active['roles'])} role(s))",
                    "wait_id": active["wait_id"],
                    "roles": list(active["roles"]),
                    "started_ts": active["started_ts"],
                    "attached": True,
                }
            # Previous registration's owner never called end_wait (crash,
            # Ctrl-C, killed pane) and has been silent well past its own
            # timeout — treat it as abandoned and replace it.

        wait_id = uuid.uuid4().hex[:12]
        self._active_waits[project_ns] = {
            "wait_id": wait_id,
            "roles": sorted(set(clean_roles)),
            "started_ts": now,
            "timeout_s": max(1.0, float(timeout_s)),
            "last_poll_ts": now,
        }
        return {
            "ok": True,
            "msg": f"watching {len(clean_roles)} role(s)",
            "wait_id": wait_id,
            "roles": clean_roles,
            "started_ts": now,
            "attached": False,
        }

    def _resolve_role_wait_status(
        self,
        project_ns: str,
        role: str,
        started_ts: float,
        panes: dict,
        detailed: dict,
    ) -> tuple[str, str | None]:
        """One role's status for an in-progress wait: ``("done"|"failed"|
        "gone"|"pending", detail)``. *detail* is None for a resolved role,
        else a short human reason. "gone" means this role will never
        produce a report for THIS wait (never spawned, or its pane already
        reached a terminal state with no report on record) — the caller
        treats it the same as resolved (not still-pending) so the wait
        doesn't block to the full timeout waiting for something that can
        never arrive (#249).

        *panes* / *detailed* are computed once per `poll_wait` tick by the
        caller and passed in — each covers every role in the project, so
        recomputing them per-role here would turn one poll tick into O(role
        count²) work for nothing.

        A `_wait_done_events` entry timestamped BEFORE `started_ts` is a
        completion from an earlier cycle — ignored for freshness purposes
        (#241's same staleness philosophy: `wait` tracks NEW completions
        from the moment it started watching, not whatever already happened
        before the Lead turn that called it) UNLESS the role's pane has
        since reached a terminal state (see `_WAIT_TERMINAL_PANE_STATES`
        below): once nothing further can happen without a fresh spawn, the
        stale event is the only report this lifecycle will ever produce, so
        it's surfaced instead of leaving the role pending forever (#249).

        That terminal-state carve-out has its own staleness bug (found in
        review of the #249 fix, closed here): `takkub assign --role X` then
        immediately `takkub wait --role X` is the exact sequence Lead uses
        everywhere — and `_send_when_ready`'s pane.set_state("working") only
        fires once the async ready-poll actually delivers the paste
        (lead_inbox.py), so the pane can still be sitting in its PREVIOUS
        cycle's terminal state (e.g. "done") for a few polls after the new
        assign already returned. Trusting *any* event once the pane looks
        terminal — as the #249 fix did — surfaces last cycle's report for a
        task that hasn't even started yet. The fix: an event only counts
        here if it is at least as new as `PaneState.assign_ts` (the wall-
        clock of the CURRENTLY active assign, stamped by `_assign_dispatch`
        and reset to a fresh pane-state entry on every new assign) — a
        report timestamped before the active assign is for a task this
        wait was never watching.
        """
        event = getattr(self, "_wait_done_events", {}).get((project_ns, role))
        fresh = event is not None and event.get("ts", 0.0) >= started_ts
        if fresh:
            if self._has_pending_lead_notice(project_ns, role):
                return "pending", "รายงานถูกสร้างแล้ว กำลังรอส่งเข้า Lead (ยังไม่ถึง pane)"
            return ("failed" if event.get("failed") else "done"), None

        pane = panes.get(role)
        if pane is None:
            # `takkub assign` can report "spawning async" before the pane
            # entry actually exists — a short grace window keeps a wait
            # started right after assign from mistaking that race for
            # "never spawned" (#249 item 2).
            if time.time() - started_ts < _WAIT_NEVER_SPAWNED_GRACE_S:
                return "pending", "ยังไม่พบ pane ของ role นี้ — กำลังรอ spawn"
            return "gone", "role ไม่พบ — ไม่เคยถูก spawn ในโปรเจคนี้ (เช็คชื่อ role)"

        info = detailed.get(role, {})
        state = info.get("state", pane.state)

        if state == "working":
            session = getattr(pane, "session", None)
            if session is not None:
                try:
                    blocked = session.is_blocked_on_tty_prompt()
                except Exception:
                    blocked = None
                if blocked:
                    return "pending", f"ค้างที่ prompt: {blocked}"
            stall_min = info.get("stall_minutes")
            if stall_min is not None:
                return "pending", f"ยังทำงานอยู่ แต่ไม่มีความคืบหน้า {stall_min}m"
            return "pending", "ยังทำงานอยู่"

        if state in _WAIT_TERMINAL_PANE_STATES:
            pane_state = getattr(self, "_pane_state", {}).get(_exit_key(project_ns, role))
            assign_ts = pane_state.assign_ts if pane_state is not None else 0.0
            covers_active_assign = event is not None and event.get("ts", 0.0) >= assign_ts
            if covers_active_assign:
                if self._has_pending_lead_notice(project_ns, role):
                    return "pending", "รายงานถูกสร้างแล้ว กำลังรอส่งเข้า Lead (ยังไม่ถึง pane)"
                return ("failed" if event.get("failed") else "done"), None
            if event is not None:
                # A newer assign superseded this event — the pane just
                # hasn't flipped out of its previous terminal state yet
                # (async ready-poll delivery race). The real report for
                # THIS assign is still in flight, not lost.
                return "pending", "assign ใหม่กำลังส่งเข้า pane — รอ report รอบใหม่"
            return "gone", f"pane ปิดไปแล้วโดยไม่มีรายงาน done (state: {state})"

        return "pending", f"pane state: {state or 'unknown'}"

    def poll_wait(self, project_ns: str, wait_id: str) -> dict:
        """One poll tick for an active wait registration.

        Returns ``{"ok": False, "msg": ...}`` if *wait_id* no longer matches
        the live registration AND no recent natural resolution is on record
        for it (already explicitly cancelled/ended, timed-out-and-expired
        past the echo grace window, or superseded by a newer `begin_wait` in
        another project — registrations are per-project so this only
        happens on a genuine client bug). Otherwise ``{"ok": True, "done":
        {role: "delivered"}, "failed": {role: "delivered"}, "gone": {role:
        reason}, "pending": {role: reason}, "elapsed": float, "expired":
        bool}``. "gone" roles (#249) will never report for this wait (never
        spawned past the grace window, or their pane already reached a
        terminal state with nothing on record) — the registration resolves
        them the same as done/failed rather than blocking on them. The
        registration is auto-removed once every role resolves (or is gone)
        or the timeout is reached, so a client never needs to call
        `end_wait` on the success path — only on early abort (Ctrl-C).

        #242 lets multiple `takkub wait` processes attach to one project's
        registration (unioning role sets) instead of each running an
        independent poll loop. That means several clients each poll the
        SAME registration on their own schedule — only the one poll call
        that happens to observe "every role resolved" (or the registration's
        own timeout) does the auto-removal above. Every OTHER attached
        client's next poll used to find `active is None` and get the same
        "no longer active" error a genuinely cancelled/superseded wait
        produces — indistinguishable from an actual failure, even though
        the wait it was attached to had just succeeded (or validly timed
        out with a real pending set, both of which are legitimate answers,
        not error conditions). `_wait_resolved_echo` (below) closes that:
        the poll call that pops the registration stashes its own returned
        payload for `_WAIT_RESOLVED_ECHO_GRACE_S`, so a straggling
        attacher's very next poll (comfortably inside that window — see the
        constant's docstring) gets the real terminal result instead. An
        explicit `end_wait`/`cancel_wait` never writes an echo, so a
        genuinely cancelled wait still errors out for any straggler — there
        is no terminal result to hand back for those.
        """
        active = self._active_waits.get(project_ns)
        if active is None or active["wait_id"] != wait_id:
            echo = getattr(self, "_wait_resolved_echo", {}).get(project_ns)
            if (
                echo is not None
                and echo["wait_id"] == wait_id
                and time.time() - echo["ts"] < _WAIT_RESOLVED_ECHO_GRACE_S
            ):
                return dict(echo["result"])
            return {
                "ok": False,
                "msg": "wait session no longer active (already ended, timed out, or superseded)",
            }
        now = time.time()
        active["last_poll_ts"] = now
        started_ts = active["started_ts"]

        # Computed once for the whole tick, not per role — see
        # _resolve_role_wait_status's docstring.
        panes = self._project_panes(project_ns)
        detailed = self.list_status_detailed(project=project_ns)

        done: dict[str, str] = {}
        failed: dict[str, str] = {}
        gone: dict[str, str] = {}
        pending: dict[str, str] = {}
        for role in active["roles"]:
            kind, detail = self._resolve_role_wait_status(
                project_ns, role, started_ts, panes, detailed
            )
            if kind == "done":
                done[role] = "delivered"
            elif kind == "failed":
                failed[role] = "delivered"
            elif kind == "gone":
                gone[role] = detail or "unresolvable"
            else:
                pending[role] = detail or "unknown"

        elapsed = now - started_ts
        expired = elapsed >= active["timeout_s"]
        result = {
            "ok": True,
            "msg": "resolved" if not pending else f"{len(pending)} role(s) still pending",
            "done": done,
            "failed": failed,
            "gone": gone,
            "pending": pending,
            "elapsed": elapsed,
            "expired": expired,
        }
        if not pending or expired:
            self._active_waits.pop(project_ns, None)
            # Stash this exact payload so any OTHER client still attached to
            # *wait_id* whose poll lands after this pop echoes the real
            # terminal result instead of a manufactured error — see this
            # method's docstring.
            self._wait_resolved_echo[project_ns] = {
                "wait_id": wait_id,
                "result": dict(result),
                "ts": now,
            }

        return result

    def end_wait(self, project_ns: str, wait_id: str) -> bool:
        """Explicitly release a wait registration (early abort — Ctrl-C, the
        CLI process dying between polls). No-op if *wait_id* no longer
        matches (already resolved/expired/superseded)."""
        active = self._active_waits.get(project_ns)
        if active is not None and active["wait_id"] == wait_id:
            self._active_waits.pop(project_ns, None)
            return True
        return False

    def cancel_wait(self, project_ns: str) -> tuple[bool, str]:
        """`takkub wait --cancel` (#249 item 5): release *project_ns*'s
        active wait registration regardless of which client owns it —
        unlike `end_wait`, the caller doesn't need to know `wait_id` (a
        fresh CLI invocation never has it). Also used by
        `close_all_teammates` so a board reset doesn't leave a stale
        registration behind for the next `takkub wait` to stumble over.
        """
        active = self._active_waits.pop(project_ns, None)
        if active is None:
            return False, "no active wait to cancel"
        return True, f"cancelled wait covering {len(active['roles'])} role(s): " + ", ".join(
            active["roles"]
        )
