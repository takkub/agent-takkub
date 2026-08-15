"""Tests for the #235 stale-delivery supersede guard.

Two `assign`s for the same role can each start an independent
`_send_when_ready` poll loop — nothing dedupes them *before* `_deliver()`
runs (`DeliveryManager`'s single-flight gate only exists once a delivery_id
is created, inside `_deliver()`). Before this fix, an OLDER call's poll loop
kept running after a NEWER assign for the same role had already superseded
it, and could fire a stale "task ยังไม่ถึงมือ" busy-wait/unconfirmed warning
about the old attempt at the exact moment `takkub status` correctly showed
the NEW task already delivered and running — two contradicting sources of
truth reaching Lead at once (proven from a live saas_admin incident
transcript, issue #235).

Each `_send_when_ready` call now claims the newest "generation" for
(project, role) up front; every poll of a stale generation abandons quietly
the moment a later call supersedes it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _live_session() -> MagicMock:
    s = MagicMock()
    s.is_alive = True
    s.write = MagicMock()
    s.is_at_trust_prompt.return_value = False
    s.is_blocked_on_tty_prompt.return_value = None
    s.is_blocked_on_permission_prompt.return_value = None
    return s


def _pane(session=None) -> MagicMock:
    p = MagicMock()
    p.session = session
    return p


@pytest.fixture
def orch(qapp, monkeypatch) -> Orchestrator:
    o = Orchestrator.__new__(Orchestrator)
    QObject.__init__(o)
    o._panes_by_project = {}
    monkeypatch.setattr(o, "_resolve_project", lambda p=None: p or "P")
    monkeypatch.setattr(
        o, "_project_panes", lambda p=None: o._panes_by_project.get(o._resolve_project(p), {})
    )
    return o


def _written_strings(session: MagicMock) -> list[str]:
    return [
        c.args[0] for c in session.write.call_args_list if c.args and isinstance(c.args[0], str)
    ]


class TestStaleDispatchSupersede:
    def test_second_assign_bumps_generation_and_first_loop_self_aborts(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        lead = _pane(_live_session())
        backend = _pane(_live_session())
        backend.session.is_at_ready_prompt.return_value = False  # stays busy
        backend.session.seconds_since_output.return_value = 1.0  # busy, not stuck
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend}

        timers: list = []
        monkeypatch.setattr(
            orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: timers.append(fn))
        )

        with patch("agent_takkub.lead_inbox._log_event") as log:
            orch._send_when_ready("backend", "stale garbled task", max_wait_ms=100_000, project="P")
            assert len(timers) == 1
            stale_check = timers.pop(0)

            orch._send_when_ready("backend", "real task", max_wait_ms=100_000, project="P")
            assert len(timers) == 1  # the fresh call scheduled its own poll
            timers.pop(0)  # don't need to drive it for this assertion

            writes_before = len(_written_strings(backend.session))
            stale_check()  # drive the STALE generation's pending poll
            writes_after = len(_written_strings(backend.session))

        assert writes_after == writes_before, "superseded loop must never paste"
        assert timers == [], "superseded loop must not reschedule itself"
        superseded = [
            c for c in log.call_args_list if c.args and c.args[0] == "task_deliver_superseded"
        ]
        assert len(superseded) == 1
        assert superseded[0].kwargs.get("role") == "backend"
        # No contradictory busy-wait/unconfirmed warning from the stale
        # generation reaching Lead — the core #235 symptom.
        assert not any("[delivery-busy-wait]" in m for m in _written_strings(lead.session))
        assert not any("[delivery-unconfirmed]" in m for m in _written_strings(lead.session))

    def test_superseded_loop_never_warns_even_past_its_own_busy_wait_branch(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """The exact incident shape: the stale loop is deep enough into its
        own timeline to have reached the #144 busy-wait extension branch
        (past its hard timeout, target still busy) — even there, once
        superseded, it must not warn."""
        lead = _pane(_live_session())
        backend = _pane(_live_session())
        backend.session.is_at_ready_prompt.return_value = False
        backend.session.seconds_since_output.return_value = 1.0
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend}

        timers: list = []
        monkeypatch.setattr(
            orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: timers.append(fn))
        )

        with patch("agent_takkub.lead_inbox._log_event"):
            # Small max_wait_ms so a handful of drained polls reach the
            # busy-wait branch quickly.
            orch._send_when_ready("backend", "stale garbled task", max_wait_ms=300, project="P")
            stale_chain = list(timers)
            timers.clear()

            # A fresh assign supersedes it right away, before the stale
            # chain has been driven at all.
            orch._send_when_ready("backend", "real task", max_wait_ms=100_000, project="P")
            timers.clear()

            # Drive the stale chain forward — each fired tick may enqueue
            # the next one (it wouldn't, once superseded, but drain
            # defensively up to a small bound either way).
            guard = 0
            while stale_chain and guard < 20:
                fn = stale_chain.pop(0)
                fn()
                stale_chain.extend(timers)
                timers.clear()
                guard += 1

        assert not any("[delivery-busy-wait]" in m for m in _written_strings(lead.session))
        assert not any("[delivery-unconfirmed]" in m for m in _written_strings(lead.session))

    def test_sequential_non_overlapping_calls_never_log_superseded(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """A normal resend AFTER the previous delivery already completed
        (not concurrent) must behave exactly as before: each call's own poll
        loop finishes (`sent[0] = True`) before the next one's first poll
        ever runs, so the generation guard — whitebox-checked here directly
        — advances but never has anything stale left to abandon."""
        lead = _pane(_live_session())
        backend = _pane(_live_session())
        backend.session.is_at_ready_prompt.return_value = True
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event") as log:
            orch._send_when_ready("backend", "first task", max_wait_ms=1000, project="P")
            orch._send_when_ready("backend", "second task", max_wait_ms=1000, project="P")

        gens = orch._send_when_ready_generation
        assert gens[("P", "backend")] == 2, "each call claims the next generation"
        assert not any(
            c.args and c.args[0] == "task_deliver_superseded" for c in log.call_args_list
        )
