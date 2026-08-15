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

from .roles import LEAD

# Grace window added on top of a registration's own timeout before a stale
# `_active_waits` entry (whose owning CLI process died without calling
# `end_wait` — crash, Ctrl-C, killed pane) is treated as gone and a fresh
# `begin_wait` may replace it. Keeps a genuinely abandoned registration from
# blocking new waits forever without needing a periodic QTimer reaper.
_WAIT_STALE_GRACE_S = 120.0


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
        "pending", detail)``. *detail* is None for a resolved role, else a
        short human reason a pending role hasn't resolved yet.

        *panes* / *detailed* are computed once per `poll_wait` tick by the
        caller and passed in — each covers every role in the project, so
        recomputing them per-role here would turn one poll tick into O(role
        count²) work for nothing.

        A `_wait_done_events` entry timestamped BEFORE `started_ts` is a
        completion from an earlier cycle — deliberately ignored (#241's same
        staleness philosophy: `wait` tracks NEW completions from the moment
        it started watching, not whatever already happened before the Lead
        turn that called it).
        """
        event = getattr(self, "_wait_done_events", {}).get((project_ns, role))
        if event is not None and event.get("ts", 0.0) >= started_ts:
            if self._has_pending_lead_notice(project_ns, role):
                return "pending", "รายงานถูกสร้างแล้ว กำลังรอส่งเข้า Lead (ยังไม่ถึง pane)"
            return ("failed" if event.get("failed") else "done"), None

        pane = panes.get(role)
        if pane is None:
            return "pending", "role ไม่พบ — ยังไม่ถูก spawn ในโปรเจคนี้ (เช็คชื่อ role)"

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

        return "pending", f"pane state: {state or 'unknown'}"

    def poll_wait(self, project_ns: str, wait_id: str) -> dict:
        """One poll tick for an active wait registration.

        Returns ``{"ok": False, "msg": ...}`` if *wait_id* no longer matches
        the live registration (already ended, timed out, or superseded by a
        newer `begin_wait` in another project — registrations are per-
        project so this only happens on a genuine client bug). Otherwise
        ``{"ok": True, "done": {role: "delivered"}, "failed": {role:
        "delivered"}, "pending": {role: reason}, "elapsed": float,
        "expired": bool}``. The registration is auto-removed once every role
        resolves or the timeout is reached, so a client never needs to call
        `end_wait` on the success path — only on early abort (Ctrl-C).
        """
        active = self._active_waits.get(project_ns)
        if active is None or active["wait_id"] != wait_id:
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
        pending: dict[str, str] = {}
        for role in active["roles"]:
            kind, detail = self._resolve_role_wait_status(
                project_ns, role, started_ts, panes, detailed
            )
            if kind == "done":
                done[role] = "delivered"
            elif kind == "failed":
                failed[role] = "delivered"
            else:
                pending[role] = detail or "unknown"

        elapsed = now - started_ts
        expired = elapsed >= active["timeout_s"]
        if not pending or expired:
            self._active_waits.pop(project_ns, None)

        return {
            "ok": True,
            "msg": "resolved" if not pending else f"{len(pending)} role(s) still pending",
            "done": done,
            "failed": failed,
            "pending": pending,
            "elapsed": elapsed,
            "expired": expired,
        }

    def end_wait(self, project_ns: str, wait_id: str) -> bool:
        """Explicitly release a wait registration (early abort — Ctrl-C, the
        CLI process dying between polls). No-op if *wait_id* no longer
        matches (already resolved/expired/superseded)."""
        active = self._active_waits.get(project_ns)
        if active is not None and active["wait_id"] == wait_id:
            self._active_waits.pop(project_ns, None)
            return True
        return False
