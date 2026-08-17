"""Targeted tests for #242 (`takkub wait`).

Covers the orchestrator-side registration/poll/resolve logic
(`LeadWaitMixin.begin_wait` / `poll_wait` / `end_wait`) directly — these
tests manipulate `_wait_done_events` / queue state the same way
`test_inbox_report.py` manipulates the notice queues, rather than driving
the full `done()` machinery (file writes, vault mirror, hot.md refresh)
which is already covered by `test_done_note_symmetrize.py`.
"""

from __future__ import annotations

import collections
import time

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import cli
from agent_takkub.orchestrator import Orchestrator, PaneState, _exit_key

PROJECT = "wait-test"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(qapp, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda project: project or PROJECT),
    )
    o = Orchestrator()
    o._idle_watchdog.stop()
    return o


def _register_working(orch: Orchestrator, role: str, project: str = PROJECT) -> None:
    from unittest.mock import MagicMock

    pane = MagicMock()
    pane.state = "working"
    pane.session = MagicMock()
    pane.session.is_blocked_on_tty_prompt.return_value = None
    orch._panes_by_project.setdefault(project, {})[role] = pane


def _register_state(orch: Orchestrator, role: str, state: str, project: str = PROJECT) -> None:
    """A pane sitting in a non-"working" state — used for the #249 terminal-
    state coverage (a pane that closed, crashed, or is about to auto-close
    after `done()` should never be indistinguishable from one that's still
    doing something)."""
    from unittest.mock import MagicMock

    pane = MagicMock()
    pane.state = state
    pane.session = None
    orch._panes_by_project.setdefault(project, {})[role] = pane


class TestBeginWait:
    def test_explicit_roles_register(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")

        result = orch.begin_wait(PROJECT, ["backend"], 60.0)

        assert result["ok"] is True
        assert result["roles"] == ["backend"]
        assert result["attached"] is False
        assert result["wait_id"]

    def test_empty_roles_default_to_active_roles(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        _register_working(orch, "frontend")

        result = orch.begin_wait(PROJECT, [], 60.0)

        assert result["ok"] is True
        assert set(result["roles"]) == {"backend", "frontend"}

    def test_empty_roles_and_nothing_active_fails(self, orch: Orchestrator) -> None:
        result = orch.begin_wait(PROJECT, [], 60.0)

        assert result["ok"] is False
        assert "nothing to wait on" in result["msg"]

    def test_lead_excluded_from_default_roles(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        _register_working(orch, "lead")

        result = orch.begin_wait(PROJECT, [], 60.0)

        assert result["roles"] == ["backend"]

    def test_second_call_attaches_and_unions_roles(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        _register_working(orch, "frontend")

        first = orch.begin_wait(PROJECT, ["backend"], 60.0)
        second = orch.begin_wait(PROJECT, ["frontend"], 60.0)

        assert second["attached"] is True
        assert second["wait_id"] == first["wait_id"]
        assert set(second["roles"]) == {"backend", "frontend"}
        # Only one registration exists — no duplicate poll loop was created.
        assert len(orch._active_waits) == 1

    def test_abandoned_registration_is_replaced(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        first = orch.begin_wait(PROJECT, ["backend"], 1.0)
        # Simulate the owning CLI process having died long ago without
        # calling end_wait: last_poll_ts is far in the past.
        orch._active_waits[PROJECT]["last_poll_ts"] = time.time() - 10_000

        second = orch.begin_wait(PROJECT, ["backend"], 60.0)

        assert second["attached"] is False
        assert second["wait_id"] != first["wait_id"]


class TestPollWait:
    def test_unknown_wait_id_fails(self, orch: Orchestrator) -> None:
        result = orch.poll_wait(PROJECT, "nope")

        assert result["ok"] is False
        assert "no longer active" in result["msg"]

    def test_still_working_role_is_pending(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        begin = orch.begin_wait(PROJECT, ["backend"], 60.0)

        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["ok"] is True
        assert result["pending"] == {"backend": "ยังทำงานอยู่"}
        assert not result["done"]
        assert not result["failed"]

    def test_role_resolves_to_done_after_started(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        begin = orch.begin_wait(PROJECT, ["backend"], 60.0)

        orch._wait_done_events[(PROJECT, "backend")] = {"ts": time.time(), "failed": False}
        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["done"] == {"backend": "delivered"}
        assert not result["pending"]

    def test_role_resolves_to_failed(self, orch: Orchestrator) -> None:
        _register_working(orch, "qa")
        begin = orch.begin_wait(PROJECT, ["qa"], 60.0)

        orch._wait_done_events[(PROJECT, "qa")] = {"ts": time.time(), "failed": True}
        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["failed"] == {"qa": "delivered"}
        assert not result["done"]

    def test_completion_before_wait_started_is_ignored(self, orch: Orchestrator) -> None:
        """#241-style staleness rule: wait only reacts to NEW completions."""
        _register_working(orch, "backend")
        orch._wait_done_events[(PROJECT, "backend")] = {
            "ts": time.time() - 1000,
            "failed": False,
        }

        begin = orch.begin_wait(PROJECT, ["backend"], 60.0)
        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["pending"]
        assert "backend" in result["pending"]

    def test_resolved_but_still_queued_stays_pending(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        begin = orch.begin_wait(PROJECT, ["backend"], 60.0)

        orch._wait_done_events[(PROJECT, "backend")] = {"ts": time.time(), "failed": False}
        orch._lead_digest_queue = {
            PROJECT: collections.deque([("[backend done] x", None, time.time())])
        }
        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert not result["done"]
        assert "backend" in result["pending"]
        assert "queued" in result["pending"]["backend"] or "รอ" in result["pending"]["backend"]

    def test_unknown_role_reports_not_found_reason(self, orch: Orchestrator) -> None:
        begin = orch.begin_wait(PROJECT, ["ghost"], 60.0)

        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert "ghost" in result["pending"]

    def test_registration_auto_removed_once_all_resolved(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        begin = orch.begin_wait(PROJECT, ["backend"], 60.0)
        orch._wait_done_events[(PROJECT, "backend")] = {"ts": time.time(), "failed": False}

        resolved = orch.poll_wait(PROJECT, begin["wait_id"])

        assert PROJECT not in orch._active_waits
        # A follow-up poll against the now-gone registration echoes the same
        # real terminal result (see TestResolvedEcho) rather than erroring —
        # it is not "stale", the registration just already succeeded.
        follow_up = orch.poll_wait(PROJECT, begin["wait_id"])
        assert follow_up == resolved

    def test_terminal_state_with_stale_event_resolves_done(self, orch: Orchestrator) -> None:
        """#249: a pane that already finished (state flipped to "empty" or
        still briefly "done" before the 2.5s auto-close) can never produce a
        NEW report — the stale `_wait_done_events` entry from before the
        wait even started is the only report it will ever have, so it must
        resolve now instead of sitting pending until the full timeout."""
        _register_state(orch, "backend", "empty")
        orch._wait_done_events[(PROJECT, "backend")] = {
            "ts": time.time() - 1000,
            "failed": False,
        }

        begin = orch.begin_wait(PROJECT, ["backend"], 60.0)
        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["done"] == {"backend": "delivered"}
        assert not result["pending"]

    def test_terminal_state_with_stale_failed_event_resolves_failed(
        self, orch: Orchestrator
    ) -> None:
        _register_state(orch, "qa", "exited")
        orch._wait_done_events[(PROJECT, "qa")] = {
            "ts": time.time() - 1000,
            "failed": True,
        }

        begin = orch.begin_wait(PROJECT, ["qa"], 60.0)
        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["failed"] == {"qa": "delivered"}
        assert not result["pending"]

    def test_reassigned_role_with_stale_terminal_event_stays_pending(
        self, orch: Orchestrator
    ) -> None:
        """Review follow-up to #249: `takkub assign --role X` immediately
        followed by `takkub wait --role X` is Lead's standard sequence.
        `_send_when_ready`'s pane.set_state("working") only lands once the
        async ready-poll actually delivers the paste, so at the FIRST poll
        tick the pane can still show its PREVIOUS cycle's terminal state
        (here: "empty") even though a brand new task was just dispatched.
        The #249 terminal-state carve-out used to trust ANY stale event once
        the pane looked terminal — surfacing last cycle's report as if it
        were this one's. A fresh assign must invalidate that stale event
        (PaneState.assign_ts, stamped by `_assign_dispatch`, is newer than
        the old event) so the wait keeps blocking for the real report."""
        _register_state(orch, "backend", "empty")
        orch._wait_done_events[(PROJECT, "backend")] = {
            "ts": time.time() - 1000,
            "failed": False,
        }
        # Simulate the new `takkub assign` that happened just before this
        # wait started — a fresh PaneState with a NEW assign_ts, exactly
        # what `_assign_dispatch` stamps right before spawn().
        key = _exit_key(PROJECT, "backend")
        orch._pane_state[key] = PaneState(assign_ts=time.time())

        begin = orch.begin_wait(PROJECT, ["backend"], 60.0)
        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert not result["done"]
        assert not result["failed"]
        assert not result["gone"]
        assert "backend" in result["pending"]

    def test_terminal_state_without_any_event_is_gone(self, orch: Orchestrator) -> None:
        """A pane closed/crashed with no done() ever recorded (manual close,
        crash before reporting) — unresolvable, but must not block the wait
        to the full timeout either (#249 item 1)."""
        _register_state(orch, "devops", "done")

        begin = orch.begin_wait(PROJECT, ["devops"], 60.0)
        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert "devops" in result["gone"]
        assert not result["pending"]
        assert not result["done"]
        assert not result["failed"]

    def test_never_spawned_role_stays_pending_within_grace(self, orch: Orchestrator) -> None:
        begin = orch.begin_wait(PROJECT, ["ghost"], 60.0)

        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert "ghost" in result["pending"]
        assert not result["gone"]

    def test_never_spawned_role_resolves_gone_after_grace(self, orch: Orchestrator) -> None:
        """#249 item 2: a role with no pane at all — past the async-spawn
        grace window — must resolve immediately instead of blocking to the
        full timeout."""
        begin = orch.begin_wait(PROJECT, ["ghost"], 3600.0)
        orch._active_waits[PROJECT]["started_ts"] = time.time() - 1000.0

        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert "ghost" in result["gone"]
        assert not result["pending"]
        assert not result["expired"]

    def test_timeout_marks_expired_and_removes_registration(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        begin = orch.begin_wait(PROJECT, ["backend"], 1.0)
        # begin_wait floors timeout_s at 1.0 — push started_ts into the past
        # instead of sleeping a full second in a unit test.
        orch._active_waits[PROJECT]["started_ts"] = time.time() - 10.0

        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["expired"] is True
        assert "backend" in result["pending"]
        assert PROJECT not in orch._active_waits


class TestPollWaitStaleEventVsCurrentAssign:
    """#262: `wait` resolved 'done: backend, devops' immediately while
    `takkub list` showed both panes still 'working' and no new report file
    existed on disk. Root cause: `_resolve_role_wait_status`'s freshness
    fast-path compared a role's `_wait_done_events` entry only against the
    WAIT REGISTRATION's own `started_ts` — which stays fixed at whenever
    `begin_wait` first created it for as long as ANY watched role is still
    pending (a multi-role registration doesn't shrink its 'roles' list as
    individual roles resolve) and is NOT updated when a role is attached to
    an already-open registration. A role reassigned to a brand-new task
    WHILE that registration is still open — the routine "Lead assigns
    devops task 2 while still waiting on backend's task 1" sequence — kept
    reading its stale task-1 `done()` event as fresh forever, because that
    event's timestamp still postdated the registration's original
    `started_ts` even though it predated the role's own NEW `assign_ts`.
    The fix floors the freshness threshold at `PaneState.assign_ts` too
    (`effective_start = max(started_ts, assign_ts)`), the same signal the
    adjacent terminal-state carve-out already trusted."""

    def test_role_reassigned_mid_open_registration_stale_event_stays_pending(
        self, orch: Orchestrator
    ) -> None:
        _register_working(orch, "backend")
        _register_working(orch, "devops")
        begin = orch.begin_wait(PROJECT, ["backend", "devops"], 3600.0)
        started_ts = orch._active_waits[PROJECT]["started_ts"]

        # devops's task-1 finished AFTER the wait started watching — the
        # old bug's `event.ts >= started_ts` check alone treats this as
        # "fresh" FOREVER, regardless of anything that happens afterward,
        # because the registration stays open (backend hasn't resolved).
        orch._wait_done_events[(PROJECT, "devops")] = {
            "ts": started_ts + 10.0,
            "failed": False,
        }
        # Lead then reassigns devops to a brand-new task-2 while this same
        # multi-role registration is still open — exactly what
        # `_assign_dispatch` stamps right before spawn(), newer than
        # task-1's done event.
        key = _exit_key(PROJECT, "devops")
        orch._pane_state[key] = PaneState(assign_ts=started_ts + 20.0)

        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert "devops" not in result["done"], (
            "task-1's stale done event must not resolve task-2 — #262 regressed"
        )
        assert "devops" in result["pending"]
        assert "backend" in result["pending"]

    def test_attach_does_not_let_stale_event_outrank_a_newer_assign(
        self, orch: Orchestrator
    ) -> None:
        """Same bug, via the OTHER path that leaves `started_ts` behind a
        role's real assign: `begin_wait`'s attach branch unions a newly
        added role into an already-open registration WITHOUT advancing
        `started_ts` — by design, so a genuinely still-pending role's
        deadline doesn't reset. That's fine as long as freshness is also
        checked against the role's own `assign_ts`, which this test pins
        down."""
        _register_working(orch, "backend")
        _register_working(orch, "devops")
        orch.begin_wait(PROJECT, ["backend"], 3600.0)
        original_started_ts = orch._active_waits[PROJECT]["started_ts"]

        # devops finishes task-1 while the registration is already open
        # (watching backend, unrelated to devops so far).
        orch._wait_done_events[(PROJECT, "devops")] = {
            "ts": original_started_ts + 5.0,
            "failed": False,
        }
        # Lead reassigns devops to task-2...
        key = _exit_key(PROJECT, "devops")
        orch._pane_state[key] = PaneState(assign_ts=original_started_ts + 15.0)
        # ...then attaches devops into the STILL-OPEN registration (backend
        # hasn't resolved) via a fresh `wait --role devops` call.
        second = orch.begin_wait(PROJECT, ["devops"], 3600.0)
        assert second["attached"] is True
        # Confirms the premise: attach does NOT advance started_ts.
        assert orch._active_waits[PROJECT]["started_ts"] == original_started_ts

        result = orch.poll_wait(PROJECT, second["wait_id"])

        assert "devops" not in result["done"], (
            "task-1's stale event outranked task-2's assign_ts via attach — #262 regressed"
        )
        assert "devops" in result["pending"]

    def test_event_after_current_assign_still_resolves_done(self, orch: Orchestrator) -> None:
        """Guard against an over-correction: a role that genuinely
        finished its CURRENT task (event newer than both the
        registration's started_ts AND the active assign_ts) must still
        resolve normally — this fix must not make wait blind to real
        completions."""
        _register_working(orch, "backend")
        begin = orch.begin_wait(PROJECT, ["backend"], 3600.0)
        started_ts = orch._active_waits[PROJECT]["started_ts"]
        key = _exit_key(PROJECT, "backend")
        orch._pane_state[key] = PaneState(assign_ts=started_ts - 1.0)
        orch._wait_done_events[(PROJECT, "backend")] = {
            "ts": started_ts + 5.0,
            "failed": False,
        }

        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["done"] == {"backend": "delivered"}
        assert not result["pending"]


class TestPollWaitInterrupt:
    """#253: a `wait --role qa` used to sit blind for the full --timeout
    while a blocking report from a role outside --role (e.g. devops FAILED)
    queued up unseen behind it. `poll_wait` must now wake early for that
    case instead of only ever resolving the roles it was told to watch."""

    def test_blocking_notice_from_unwatched_role_interrupts(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        begin = orch.begin_wait(PROJECT, ["backend"], 60.0)
        orch._lead_digest_queue = {
            PROJECT: collections.deque(
                [("[devops FAILED] docker build failed: exit 1", None, time.time())]
            )
        }

        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["interrupt"] == {
            "role": "devops",
            "detail": "[devops FAILED] docker build failed: exit 1",
        }
        # The interrupt ends the registration like a natural resolution —
        # "backend" is still genuinely pending in its own pane, only this
        # wait's tracking stops.
        assert "backend" in result["pending"]
        assert PROJECT not in orch._active_waits

    def test_plain_done_notice_from_unwatched_role_does_not_interrupt(
        self, orch: Orchestrator
    ) -> None:
        _register_working(orch, "backend")
        begin = orch.begin_wait(PROJECT, ["backend"], 60.0)
        orch._lead_digest_queue = {
            PROJECT: collections.deque([("[devops done] stack is up", None, time.time())])
        }

        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["interrupt"] is None
        assert "backend" in result["pending"]
        assert PROJECT in orch._active_waits

    def test_blocking_notice_from_a_watched_role_does_not_interrupt(
        self, orch: Orchestrator
    ) -> None:
        """A watched role's own FAILED report resolves normally through
        `failed` — it must never also fire as an "interrupt" against
        itself."""
        _register_working(orch, "qa")
        begin = orch.begin_wait(PROJECT, ["qa"], 60.0)
        orch._lead_digest_queue = {
            PROJECT: collections.deque([("[qa FAILED] assertion error", None, time.time())])
        }

        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["interrupt"] is None

    def test_no_pending_roles_never_computes_interrupt(self, orch: Orchestrator) -> None:
        """Once every watched role has already resolved, the poll is about
        to end the registration on its own — no need to also scan for an
        interrupt from elsewhere."""
        _register_working(orch, "backend")
        begin = orch.begin_wait(PROJECT, ["backend"], 60.0)
        orch._wait_done_events[(PROJECT, "backend")] = {"ts": time.time(), "failed": False}
        orch._lead_digest_queue = {
            PROJECT: collections.deque([("[devops FAILED] boom", None, time.time())])
        }

        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["interrupt"] is None
        assert result["done"] == {"backend": "delivered"}


class TestPollWaitInterruptForWatchedRole:
    """#259: `_pending_notice_outside` deliberately skips roles Lead IS
    watching — a role's own done/FAILED must resolve through the normal
    `_wait_done_events` path, not double-fire as an interrupt against
    itself. But `[delivery-unconfirmed]` / `[spawn-stuck]` /
    `[delivery-boot-stall]` / `[spawn-failed]` notices never call
    done()/failed() — nothing else ever resolved a watched role for THAT
    notice class, so `wait --role kimi` sat blind for the full --timeout
    even though a `[delivery-unconfirmed] kimi ...` notice had been
    sitting in the inbox since minute 1.5, explaining exactly why.

    A deeper, previously undiscovered half of the same bug is covered here
    too: these 4 markers are authored by the orchestrator itself, not by a
    teammate's `done()` call, so `_notice_role_tag` (which only parses the
    `[role done]`/`[role FAILED]` shape) can't attribute them to a role —
    `inbox_report` falls back to `"system"` for them, and the OLD
    `_pending_notice_outside` unconditionally skipped `role == "system"`
    BEFORE ever checking whether the body was blocking. That meant these 4
    notice kinds were invisible to `_pending_notice_outside` for EVERY
    role, watched or not — not only the watched-role gap originally
    reported.
    """

    def test_delivery_unconfirmed_for_a_watched_role_interrupts(self, orch: Orchestrator) -> None:
        _register_working(orch, "kimi")
        begin = orch.begin_wait(PROJECT, ["kimi"], 1800.0)
        orch._lead_digest_queue = {
            PROJECT: collections.deque(
                [
                    (
                        "⚠️ [delivery-unconfirmed] kimi pane ไม่ถึง ready prompt ใน เวลาที่กำหนด",
                        None,
                        time.time(),
                    )
                ]
            )
        }

        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["interrupt"] is not None
        assert result["interrupt"]["role"] == "kimi"
        assert "kimi" in result["pending"]
        assert PROJECT not in orch._active_waits

    def test_spawn_stuck_for_a_watched_role_interrupts(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        begin = orch.begin_wait(PROJECT, ["backend"], 1800.0)
        orch._lead_digest_queue = {
            PROJECT: collections.deque(
                [("⚠️ [spawn-stuck] backend assign ค้างใน spawn queue นานเกิน 90s", None, time.time())]
            )
        }

        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["interrupt"] == {
            "role": "backend",
            "detail": "⚠️ [spawn-stuck] backend assign ค้างใน spawn queue นานเกิน 90s",
        }

    def test_boot_stall_for_an_outside_role_now_interrupts_too(self, orch: Orchestrator) -> None:
        """The previously-undiscovered half: this notice class used to be
        invisible to `_pending_notice_outside` even for a genuinely
        unwatched role, because `role == "system"` was skipped before the
        blocking-marker check ever ran."""
        _register_working(orch, "backend")
        begin = orch.begin_wait(PROJECT, ["backend"], 1800.0)
        orch._lead_digest_queue = {
            PROJECT: collections.deque(
                [
                    (
                        "⛔ [delivery-boot-stall] devops pane ค้างอยู่ที่ boot phase",
                        None,
                        time.time(),
                    )
                ]
            )
        }

        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["interrupt"] == {
            "role": "devops",
            "detail": "⛔ [delivery-boot-stall] devops pane ค้างอยู่ที่ boot phase",
        }

    def test_watched_roles_own_failed_still_resolves_normally_not_as_interrupt(
        self, orch: Orchestrator
    ) -> None:
        """A watched role's own FAILED must keep resolving through
        `_wait_done_events` (existing #253 behaviour) — this new check is
        additive, not a replacement, and must never race the two
        resolutions against each other."""
        _register_working(orch, "qa")
        begin = orch.begin_wait(PROJECT, ["qa"], 1800.0)
        orch._wait_done_events[(PROJECT, "qa")] = {"ts": time.time(), "failed": True}

        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["interrupt"] is None
        assert result["failed"] == {"qa": "delivered"}

    def test_unrelated_system_notice_for_a_watched_role_does_not_interrupt(
        self, orch: Orchestrator
    ) -> None:
        """Only the 4 delivery-health system markers wake a watched role's
        own wait early — a routine digest line that happens to name the
        watched role must not."""
        _register_working(orch, "backend")
        begin = orch.begin_wait(PROJECT, ["backend"], 1800.0)
        orch._lead_digest_queue = {
            PROJECT: collections.deque(
                [("📬 [Lead Inbox Digest — 1 update] • [backend] done", None, time.time())]
            )
        }

        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["interrupt"] is None
        assert "backend" in result["pending"]


class TestPollWaitUserInputInterrupt:
    """#265: the Lead pane's OWNER outranks every teammate role — while
    `takkub wait` blocks, anything they type sits as queued CLI input,
    unprocessed, until `wait` returns (up to the full --timeout). Wakes on
    `Orchestrator._lead_last_user_input_ts` — stamped from the exact same
    `_on_pane_input` choke point the pre-existing #3 draft-typing guard
    uses, so engine-originated writes (notices/tasks) can never be
    mistaken for the owner interrupting (see `_on_pane_input`'s own
    comment: those go straight to `session.write()` and never pass
    through here)."""

    def test_input_after_wait_started_interrupts(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        begin = orch.begin_wait(PROJECT, ["backend"], 1800.0)
        started_ts = orch._active_waits[PROJECT]["started_ts"]
        orch._lead_last_user_input_ts[PROJECT] = started_ts + 1.0

        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["interrupt"] is not None
        assert result["interrupt"]["reason"] == "user_input"
        assert "backend" in result["pending"]
        assert PROJECT not in orch._active_waits

    def test_input_before_wait_started_does_not_interrupt(self, orch: Orchestrator) -> None:
        """A draft/keystroke the owner typed BEFORE calling `takkub wait`
        (e.g. the very command that spawned this wait) must not immediately
        re-trigger the interrupt on the very first poll."""
        _register_working(orch, "backend")
        orch._lead_last_user_input_ts[PROJECT] = time.time()
        # begin_wait's own started_ts is stamped strictly after the line
        # above, so it postdates this "stale" input.
        begin = orch.begin_wait(PROJECT, ["backend"], 1800.0)

        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["interrupt"] is None
        assert "backend" in result["pending"]

    def test_no_input_at_all_does_not_interrupt(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        begin = orch.begin_wait(PROJECT, ["backend"], 1800.0)

        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["interrupt"] is None

    def test_unsubmitted_draft_still_interrupts(self, orch: Orchestrator) -> None:
        """The issue's own explicit call: a still-unsubmitted draft counts
        too — it already proves the owner is about to act, so `wait`
        shouldn't keep them queued behind teammate polling waiting for
        Enter."""
        _register_working(orch, "backend")
        begin = orch.begin_wait(PROJECT, ["backend"], 1800.0)
        started_ts = orch._active_waits[PROJECT]["started_ts"]
        # Simulate _on_pane_input's real wiring: every byte (submitted or
        # not) stamps _lead_last_user_input_ts, independent of
        # _lead_draft_state's own submitted/unsubmitted bookkeeping.
        orch._lead_last_user_input_ts[PROJECT] = started_ts + 0.5

        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["interrupt"] is not None
        assert result["interrupt"]["reason"] == "user_input"

    def test_engine_originated_notice_write_does_not_feed_the_signal(
        self, orch: Orchestrator
    ) -> None:
        """The false-positive guard from the issue, proven structurally:
        `_notify_lead`/`_pump_lead_notify` write via `session.write()`
        directly and never call `_on_pane_input` — so queuing a notice
        must never move `_lead_last_user_input_ts` on its own. This test
        simulates that guarantee by asserting the timestamp stays whatever
        it already was after queuing a notice with no `_on_pane_input`
        call in between."""
        _register_working(orch, "backend")
        begin = orch.begin_wait(PROJECT, ["backend"], 1800.0)
        orch._lead_digest_queue = {
            PROJECT: collections.deque([("[devops done] stack is up", None, time.time())])
        }

        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["interrupt"] is None
        assert PROJECT not in orch._lead_last_user_input_ts

    def test_no_pending_roles_never_computes_user_input_interrupt(self, orch: Orchestrator) -> None:
        """Matches the existing #253/#259 gating: once every watched role
        has already resolved, the poll is about to end the registration on
        its own — no need to also check for a user-input interrupt."""
        _register_working(orch, "backend")
        begin = orch.begin_wait(PROJECT, ["backend"], 1800.0)
        started_ts = orch._active_waits[PROJECT]["started_ts"]
        orch._wait_done_events[(PROJECT, "backend")] = {"ts": time.time(), "failed": False}
        orch._lead_last_user_input_ts[PROJECT] = started_ts + 1.0

        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["interrupt"] is None
        assert result["done"] == {"backend": "delivered"}


class TestResolvedEcho:
    """Real incident (2026-08-15): two `takkub wait` processes attached to
    the same project registration — one poll call resolved every role and
    popped it; the OTHER attached process's very next poll landed a beat
    later and got `{"ok": False, "msg": "wait session no longer active..."}`
    printed as `err: ...` for a wait that had actually just SUCCEEDED. The
    registration is per-project, not per-client, so any attacher's poll must
    see the same real terminal result the resolving poll produced."""

    def test_straggling_attacher_gets_echoed_result_not_error(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        owner = orch.begin_wait(PROJECT, ["backend"], 60.0)
        attacher = orch.begin_wait(PROJECT, ["backend"], 60.0)
        assert attacher["wait_id"] == owner["wait_id"]

        orch._wait_done_events[(PROJECT, "backend")] = {"ts": time.time(), "failed": False}
        resolving_poll = orch.poll_wait(PROJECT, owner["wait_id"])
        assert resolving_poll["done"] == {"backend": "delivered"}
        assert PROJECT not in orch._active_waits

        straggler_poll = orch.poll_wait(PROJECT, attacher["wait_id"])

        assert straggler_poll["ok"] is True
        assert straggler_poll == resolving_poll

    def test_echoed_timeout_result_still_carries_real_pending_set(self, orch: Orchestrator) -> None:
        """A registration that pops via its OWN timeout (not full
        completion) is a legitimate terminal outcome too, not a cancel — the
        echo must carry the same real `pending`/`expired` payload, not
        silently upgrade it to a fake success."""
        _register_working(orch, "backend")
        owner = orch.begin_wait(PROJECT, ["backend"], 1.0)
        attacher = orch.begin_wait(PROJECT, ["backend"], 1.0)
        orch._active_waits[PROJECT]["started_ts"] = time.time() - 10.0

        resolving_poll = orch.poll_wait(PROJECT, owner["wait_id"])
        assert resolving_poll["expired"] is True
        assert "backend" in resolving_poll["pending"]

        straggler_poll = orch.poll_wait(PROJECT, attacher["wait_id"])

        assert straggler_poll == resolving_poll

    def test_echo_expires_after_grace_window(self, orch: Orchestrator) -> None:
        from agent_takkub import lead_wait as lead_wait_mod

        _register_working(orch, "backend")
        begin = orch.begin_wait(PROJECT, ["backend"], 60.0)
        orch._wait_done_events[(PROJECT, "backend")] = {"ts": time.time(), "failed": False}
        orch.poll_wait(PROJECT, begin["wait_id"])

        orch._wait_resolved_echo[PROJECT]["ts"] = time.time() - (
            lead_wait_mod._WAIT_RESOLVED_ECHO_GRACE_S + 1.0
        )

        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["ok"] is False
        assert "no longer active" in result["msg"]

    def test_cancelled_wait_gives_stragglers_a_real_error_not_an_echo(
        self, orch: Orchestrator
    ) -> None:
        """Explicit cancel/end (unlike natural resolution) has no terminal
        result to hand back — a straggler must still see the plain error."""
        _register_working(orch, "backend")
        orch.begin_wait(PROJECT, ["backend"], 60.0)
        attacher = orch.begin_wait(PROJECT, ["backend"], 60.0)

        ok, _msg = orch.cancel_wait(PROJECT)
        assert ok is True

        result = orch.poll_wait(PROJECT, attacher["wait_id"])

        assert result["ok"] is False
        assert "no longer active" in result["msg"]
        assert PROJECT not in orch._wait_resolved_echo


class TestEndWait:
    def test_end_wait_removes_matching_registration(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        begin = orch.begin_wait(PROJECT, ["backend"], 60.0)

        assert orch.end_wait(PROJECT, begin["wait_id"]) is True
        assert PROJECT not in orch._active_waits

    def test_end_wait_is_noop_for_mismatched_id(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        orch.begin_wait(PROJECT, ["backend"], 60.0)

        assert orch.end_wait(PROJECT, "not-the-id") is False
        assert PROJECT in orch._active_waits


class TestCancelWait:
    """#249 item 5: `takkub wait --cancel` — release whatever is active
    without needing to know its wait_id (a fresh CLI invocation never has
    one)."""

    def test_cancel_removes_active_registration(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        orch.begin_wait(PROJECT, ["backend"], 60.0)

        ok, msg = orch.cancel_wait(PROJECT)

        assert ok is True
        assert "backend" in msg
        assert PROJECT not in orch._active_waits

    def test_cancel_is_noop_when_nothing_active(self, orch: Orchestrator) -> None:
        ok, msg = orch.cancel_wait(PROJECT)

        assert ok is False
        assert "no active wait" in msg

    def test_close_all_teammates_cancels_active_wait(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        orch.begin_wait(PROJECT, ["backend"], 60.0)
        assert PROJECT in orch._active_waits

        orch.close_all_teammates(PROJECT)

        assert PROJECT not in orch._active_waits


class TestCliWaitCommand:
    def test_resolves_immediately_when_nothing_pending(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        calls: list[dict] = []

        def fake_request(payload: dict) -> dict:
            calls.append(payload)
            if payload["cmd"] == "wait-begin":
                return {
                    "ok": True,
                    "msg": "watching 1 role(s)",
                    "wait_id": "w1",
                    "roles": ["backend"],
                    "started_ts": time.time(),
                    "attached": False,
                }
            if payload["cmd"] == "wait-poll":
                return {
                    "ok": True,
                    "msg": "resolved",
                    "done": {"backend": "delivered"},
                    "failed": {},
                    "pending": {},
                    "elapsed": 1.0,
                    "expired": False,
                }
            raise AssertionError(f"unexpected cmd: {payload['cmd']}")

        monkeypatch.setattr(cli, "_request", fake_request)
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)

        rc = cli.main(["wait", "--role", "backend"])
        out = capsys.readouterr().out

        assert rc == 0
        assert "backend" in out
        assert [c["cmd"] for c in calls] == ["wait-begin", "wait-poll"]

    def test_begin_failure_short_circuits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            cli,
            "_request",
            lambda payload: {"ok": False, "msg": "nothing to wait on"},
        )
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)

        rc = cli.main(["wait"])

        assert rc == 1

    def test_timeout_is_clamped_into_range(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent: list[dict] = []

        def fake_request(payload: dict) -> dict:
            sent.append(payload)
            if payload["cmd"] == "wait-begin":
                return {
                    "ok": True,
                    "msg": "watching 0 role(s)",
                    "wait_id": "w1",
                    "roles": [],
                    "started_ts": time.time(),
                    "attached": False,
                }
            return {
                "ok": True,
                "msg": "resolved",
                "done": {},
                "failed": {},
                "pending": {},
                "elapsed": 0.0,
                "expired": False,
            }

        monkeypatch.setattr(cli, "_request", fake_request)
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)

        cli.main(["wait", "--timeout", "999999"])

        assert sent[0]["timeout"] == cli._WAIT_MAX_TIMEOUT_S

    def test_teammate_role_blocked_by_cli_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "backend")
        rc = cli.main(["wait"])
        assert rc == 1

    def test_interrupt_stops_the_loop_early_with_a_clear_reason(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """#253: an interrupted poll must stop the client-side loop
        immediately (not keep sleeping/polling toward --timeout) and print
        which role/report woke it so Lead can act without re-deriving it."""
        calls: list[dict] = []

        def fake_request(payload: dict) -> dict:
            calls.append(payload)
            if payload["cmd"] == "wait-begin":
                return {
                    "ok": True,
                    "msg": "watching 1 role(s)",
                    "wait_id": "w1",
                    "roles": ["backend"],
                    "started_ts": time.time(),
                    "attached": False,
                }
            if payload["cmd"] == "wait-poll":
                return {
                    "ok": True,
                    "msg": "1 role(s) still pending",
                    "done": {},
                    "failed": {},
                    "gone": {},
                    "pending": {"backend": "ยังทำงานอยู่"},
                    "elapsed": 5.0,
                    "expired": False,
                    "interrupt": {"role": "devops", "detail": "[devops FAILED] boom"},
                }
            if payload["cmd"] == "wait-end":
                return {"ok": True, "msg": "wait ended"}
            raise AssertionError(f"unexpected cmd: {payload['cmd']}")

        monkeypatch.setattr(cli, "_request", fake_request)
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)

        rc = cli.main(["wait", "--role", "backend"])
        out = capsys.readouterr().out

        # Like a timeout, an interrupt leaves watched roles still pending —
        # not full success — so rc must be non-zero, same as the pre-#253
        # timeout-with-pending case.
        assert rc == 1
        assert "interrupted" in out
        assert "devops" in out
        assert "[devops FAILED] boom" in out
        # Exactly one poll — the loop must not keep polling/sleeping after
        # an interrupt.
        assert [c["cmd"] for c in calls] == ["wait-begin", "wait-poll", "wait-end"]

    def test_user_input_interrupt_stops_the_loop_with_a_distinct_message(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """#265: same early-stop mechanics as #253's interrupt, but the
        printed message must clearly say the OWNER typed something — not
        read like a role's blocking report (there is no role to look up)."""
        calls: list[dict] = []

        def fake_request(payload: dict) -> dict:
            calls.append(payload)
            if payload["cmd"] == "wait-begin":
                return {
                    "ok": True,
                    "msg": "watching 1 role(s)",
                    "wait_id": "w1",
                    "roles": ["backend"],
                    "started_ts": time.time(),
                    "attached": False,
                }
            if payload["cmd"] == "wait-poll":
                return {
                    "ok": True,
                    "msg": "1 role(s) still pending",
                    "done": {},
                    "failed": {},
                    "gone": {},
                    "pending": {"backend": "ยังทำงานอยู่"},
                    "elapsed": 5.0,
                    "expired": False,
                    "interrupt": {
                        "role": "lead",
                        "detail": "มีข้อความใหม่จากคุณเข้ามาระหว่างที่ wait กำลังรออยู่",
                        "reason": "user_input",
                    },
                }
            if payload["cmd"] == "wait-end":
                return {"ok": True, "msg": "wait ended"}
            raise AssertionError(f"unexpected cmd: {payload['cmd']}")

        monkeypatch.setattr(cli, "_request", fake_request)
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)

        rc = cli.main(["wait", "--role", "backend"])
        out = capsys.readouterr().out

        assert rc == 1
        assert "interrupted" in out
        assert "พิมพ์ข้อความใหม่เข้ามา" in out
        assert "needs attention" not in out, "must not read like a role-report interrupt"
        assert [c["cmd"] for c in calls] == ["wait-begin", "wait-poll", "wait-end"]

    def test_cancel_flag_sends_wait_cancel_and_skips_poll(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        calls: list[dict] = []

        def fake_request(payload: dict) -> dict:
            calls.append(payload)
            assert payload["cmd"] == "wait-cancel"
            return {"ok": True, "msg": "cancelled wait covering 1 role(s): backend"}

        monkeypatch.setattr(cli, "_request", fake_request)
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)

        rc = cli.main(["wait", "--cancel"])
        out = capsys.readouterr().out

        assert rc == 0
        assert len(calls) == 1
        assert "cancelled" in out
