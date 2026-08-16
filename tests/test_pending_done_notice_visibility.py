"""Targeted tests for issue #163: a pane's done() report can still be sitting
in the outbound-to-Lead pipeline (Lead Inbox Digest window, live notify
queue, or the durable pending store) when the reporting pane's own 2.5s
auto-close timer fires and removes it from the pane registry — so
`takkub list` used to show the role simply gone, with no sign its report was
still in flight.

`_has_pending_lead_notice` / `list_status` / `list_status_detailed` now keep
surfacing a just-closed role (tagged with `Orchestrator._PENDING_NOTICE_STATE`)
until its notice has genuinely left the pipeline.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import orchestrator as orch_mod
from agent_takkub.lead_inbox import _INBOX_DIGEST_ADAPTIVE_SETTLE_MS
from agent_takkub.orchestrator import LEAD, Orchestrator, PaneState

PENDING = Orchestrator._PENDING_NOTICE_STATE


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _make_alive_session() -> MagicMock:
    s = MagicMock()
    s.is_alive = True
    s.write = MagicMock()
    s.is_at_ready_prompt = MagicMock(return_value=True)
    return s


def _make_pane(session=None, state: str = "working") -> MagicMock:
    """Unlike test_done_evidence.py's helper, `set_state()` here actually
    mutates `.state` — the #163 repro depends on `_close_if_same_session`
    reading `pane.state == "done"` after `done()` calls `set_state`."""
    p = MagicMock()
    p.session = session
    p.state = state

    def _set_state(new_state, note=None):
        p.state = new_state

    p.set_state = MagicMock(side_effect=_set_state)
    return p


@pytest.fixture
def orch(qapp, tmp_path, monkeypatch) -> Orchestrator:
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
    monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)

    with patch("agent_takkub.orchestrator.Orchestrator._load_pending_cc", lambda self: None):
        o = Orchestrator.__new__(Orchestrator)
        from PyQt6.QtCore import QObject

        QObject.__init__(o)
        o._panes_by_project = {}
        o._pane_state = {}
        o._idle_state = {}
        o._recent_exits = {}
        o._recent_done = []
        o._pending_lead_cc = {}
        o._pending_done_notices = {}
    return o


def _register_pane(orch: Orchestrator, role: str, project: str, session=None, state="working"):
    pane = _make_pane(session, state=state)
    orch._panes_by_project.setdefault(project, {})[role] = pane
    return pane


def _mock_done(orch: Orchestrator) -> None:
    orch._save_decision_note = MagicMock()  # type: ignore[assignment]
    orch._write_hot_md = MagicMock()  # type: ignore[assignment]


# ─────────────────────────────────────────────────────────────
# _has_pending_lead_notice: matches a role's notice in every stage of the
# outbound pipeline
# ─────────────────────────────────────────────────────────────


class TestHasPendingLeadNotice:
    def test_matches_raw_body_in_digest_queue(self, orch):
        import collections

        orch._lead_digest_queue = {"proj": collections.deque(["[backend done] did stuff"])}

        assert orch._has_pending_lead_notice("proj", "backend") is True
        assert orch._has_pending_lead_notice("proj", "frontend") is False

    def test_matches_raw_failed_body_in_digest_queue(self, orch):
        import collections

        orch._lead_digest_queue = {"proj": collections.deque(["[backend FAILED] broke"])}

        assert orch._has_pending_lead_notice("proj", "backend") is True

    def test_matches_digest_formatted_line_in_live_queue(self, orch):
        """Once _flush_lead_digest folds the raw body into a combined digest
        line, the shape changes from '[role done] x' to '• [role] done: x'
        (see _format_digest_item) — the matcher must handle both."""
        import collections

        digest_text = "📬 [Lead Inbox Digest — 1 update]\n• [backend] done: did stuff"
        orch._lead_notify_queue = {"proj": collections.deque([digest_text])}

        assert orch._has_pending_lead_notice("proj", "backend") is True
        assert orch._has_pending_lead_notice("proj", "frontend") is False

    def test_matches_durable_pending_store_by_role_key(self, orch):
        orch._pending_done_notices = {
            "proj": [{"role": "backend", "note": "notify", "body": "[backend done] x"}]
        }

        assert orch._has_pending_lead_notice("proj", "backend") is True
        assert orch._has_pending_lead_notice("proj", "frontend") is False

    def test_false_when_nothing_queued_anywhere(self, orch):
        assert orch._has_pending_lead_notice("proj", "backend") is False

    def test_scoped_to_project_namespace(self, orch):
        import collections

        orch._lead_digest_queue = {"other-proj": collections.deque(["[backend done] x"])}

        assert orch._has_pending_lead_notice("proj", "backend") is False


# ─────────────────────────────────────────────────────────────
# list_status / list_status_detailed surface a pending-but-closed role
# ─────────────────────────────────────────────────────────────


class TestListStatusSurfacesPendingRole:
    def test_recent_done_role_with_pending_notice_is_surfaced(self, orch, monkeypatch):
        import collections

        proj = "proj"
        orch._recent_done = [(proj, "backend", "stamp-backend.md")]
        orch._lead_digest_queue = {proj: collections.deque(["[backend done] x"])}
        # backend has no live pane entry at all — simulates post-close state.

        status = orch.list_status(project=proj)
        assert status["backend"] == PENDING

        detailed = orch.list_status_detailed(project=proj)
        assert detailed["backend"]["state"] == PENDING
        assert detailed["backend"]["stall_minutes"] is None

    def test_recent_done_role_without_pending_notice_stays_absent(self, orch):
        """Once the notice has genuinely left every queue (delivered), the
        role should NOT be resurrected — it really is gone."""
        proj = "proj"
        orch._recent_done = [(proj, "backend", "stamp-backend.md")]
        # No queues populated — notice already delivered.

        assert "backend" not in orch.list_status(project=proj)
        assert "backend" not in orch.list_status_detailed(project=proj)

    def test_live_pane_state_wins_over_recent_done_entry(self, orch, monkeypatch):
        """A role that's alive and back in the registry (e.g. respawned)
        must show its real live state, never the synthetic pending tag."""
        import collections

        proj = "proj"
        _register_pane(orch, "backend", proj, _make_alive_session(), state="working")
        orch._recent_done = [(proj, "backend", "stamp-backend.md")]
        orch._lead_digest_queue = {proj: collections.deque(["[backend done] x"])}

        assert orch.list_status(project=proj)["backend"] == "working"

    def test_other_project_recent_done_not_leaked(self, orch):
        import collections

        orch._recent_done = [("other-proj", "backend", "x.md")]
        orch._lead_digest_queue = {"other-proj": collections.deque(["[backend done] x"])}

        assert "backend" not in orch.list_status(project="proj")


# ─────────────────────────────────────────────────────────────
# End-to-end repro: done()'s 2.5s auto-close races the digest window — the
# pane is gone from the raw registry well before Lead sees the report, but
# list_status/list_status_detailed keep it visible until then.
#
# This repro is a SOLO completion (only "backend" and Lead exist, no other
# role active) — #264's adaptive window applies its short
# `_INBOX_DIGEST_ADAPTIVE_SETTLE_MS` settle here rather than the full
# `_INBOX_DIGEST_WINDOW_MS`, but that settle (3s) is still comfortably
# longer than the 2.5s auto-close, so the race this test exists to prove —
# pane gone from the registry before its digest fires — still holds.
# ─────────────────────────────────────────────────────────────


class TestDoneCloseRacesDigestDelivery:
    def test_pane_vanishes_from_registry_before_digest_delivers_but_list_still_shows_it(
        self, orch, monkeypatch
    ):
        monkeypatch.setattr(orch_mod, "active_project", lambda: ("proj", {}))
        # The autouse _isolate_runtime fixture forces legacy immediate
        # delivery (TAKKUB_INBOX_DIGEST_MS=0) for the rest of the suite —
        # this repro is specifically about the production digest window
        # racing the pane's own 2.5s close, so restore the real default.
        monkeypatch.delenv("TAKKUB_INBOX_DIGEST_MS", raising=False)
        _mock_done(orch)

        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "backend", proj, _make_alive_session(), state="working")
        orch._pane_state[f"{proj}::backend"] = PaneState(assign_ts=time.time() - 60)

        # Mirrors production wiring (main_window.py / headless_window.py):
        # close() only emits paneClosed; the registry pop happens in the
        # signal handler.
        orch.paneClosed.connect(
            lambda role, project, o=orch: o.unregister_pane(role, project=project)
        )

        timers: list[tuple[int, object]] = []
        with patch(
            "agent_takkub.orchestrator.QTimer.singleShot",
            side_effect=lambda ms, cb: timers.append((ms, cb)),
        ):
            orch.done("backend", note="finished", project=proj)

        lead = orch._panes_by_project[proj][LEAD.name]
        # Clean done notice is digestible -> queued, not written yet.
        lead.session.write.assert_not_called()

        close_calls = [(ms, cb) for ms, cb in timers if ms == 2_500]
        digest_calls = [(ms, cb) for ms, cb in timers if ms == _INBOX_DIGEST_ADAPTIVE_SETTLE_MS]
        assert len(close_calls) == 1, "done() must schedule exactly one 2.5s auto-close"
        assert len(digest_calls) == 1, (
            "a solo clean done notice (no other role active) must arm the #264 "
            "adaptive settle window, not the full digest window"
        )

        # Fire the pane's own auto-close timer (simulates 2.5s elapsing)
        # BEFORE the digest window (simulates it not having elapsed yet).
        # Keep patching QTimer.singleShot so any nested scheduling stays
        # deterministic instead of touching the real Qt event loop.
        with patch(
            "agent_takkub.orchestrator.QTimer.singleShot",
            side_effect=lambda ms, cb: timers.append((ms, cb)),
        ):
            close_calls[0][1]()

        # Production-accurate: the pane is really gone from the raw registry.
        assert "backend" not in orch._panes_by_project[proj]
        # Lead still hasn't seen anything.
        lead.session.write.assert_not_called()

        # `takkub list` must not just show the role as vanished — its report
        # is still genuinely in flight.
        assert orch.list_status(project=proj)["backend"] == PENDING
        assert orch.list_status_detailed(project=proj)["backend"]["state"] == PENDING

        # Now the digest window elapses and delivery happens.
        with patch(
            "agent_takkub.orchestrator.QTimer.singleShot",
            side_effect=lambda ms, cb: timers.append((ms, cb)),
        ):
            digest_calls[0][1]()
        assert lead.session.write.called
        assert "[backend] done" in "".join(
            c.args[0].decode() if isinstance(c.args[0], bytes) else str(c.args[0])
            for c in lead.session.write.call_args_list
        )

        # Delivered -> the role is no longer resurrected by list_status.
        assert "backend" not in orch.list_status(project=proj)
