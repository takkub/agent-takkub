"""Tests for issue #164: `takkub send --to <role>` said "unknown role: X"
for a role that IS in the registry but simply has no pane open right now
(closed, or restarted-and-not-yet-respawned). `send()` must tell the two
cases apart:

  - name not in `roles.by_name(...)` at all -> "unknown role: X"
  - name IS a real role, just no live pane -> the message is durably
    queued instead of rejected (#303 item 3 — a known role with no pane
    used to be a hard error with no way to hand it anything before it
    happened to spawn on its own), with a hint pointing at
    `takkub assign --role X ...` to open it right away instead of waiting.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator, PaneState


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
    return s


def _make_pane(session=None) -> MagicMock:
    p = MagicMock()
    p.session = session
    return p


@pytest.fixture
def orch(qapp, tmp_path, monkeypatch) -> Orchestrator:
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
    monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)

    with (
        patch.object(Orchestrator, "_start_hot_md_timer", lambda self: None, create=True),
        patch("agent_takkub.orchestrator.Orchestrator._load_pending_cc", lambda self: None),
        patch(
            "agent_takkub.orchestrator.Orchestrator._start_browser_mcps",
            lambda self: None,
            create=True,
        ),
    ):
        o = Orchestrator.__new__(Orchestrator)
        from PyQt6.QtCore import QObject

        QObject.__init__(o)
        o._panes_by_project = {}
        o._pane_state = {}
        o._idle_state = {}
        o._recent_exits = {}
        o._recent_done = []
        o._pending_lead_cc = {}
    return o


class TestUnknownRoleVsClosedPane:
    def test_name_not_in_registry_says_unknown_role(self, orch: Orchestrator) -> None:
        ok, msg = orch.send("totally-not-a-role", "hi", from_role="lead", project="p")
        assert ok is False
        assert msg == "unknown role: totally-not-a-role"

    def test_registered_role_with_no_pane_queues_and_hints_assign(self, orch: Orchestrator) -> None:
        # backend is a real DEFAULT_TEAMMATES role, but never registered/spawned here.
        ok, msg = orch.send("backend", "hi", from_role="lead", project="p")
        assert ok is True
        assert "unknown role" not in msg
        assert "backend" in msg
        assert "queued" in msg
        assert "takkub assign --role backend" in msg

    def test_sharded_role_name_resolves_against_base_role(self, orch: Orchestrator) -> None:
        """`qa#1` is a shard instance of the real `qa` role — the registry
        only knows base names, so the "known role, no pane" queuing (not
        "unknown role") must still fire, keyed off the base name."""
        ok, msg = orch.send("qa#1", "hi", from_role="lead", project="p")
        assert ok is True
        assert "unknown role" not in msg
        assert "takkub assign --role qa" in msg

    def test_pane_present_but_session_dead_keeps_existing_message(self, orch: Orchestrator) -> None:
        """Regression guard: a pane widget that DOES exist (registered) but
        whose session died must keep the pre-existing "is not running"
        message, not the new unknown/no-pane hint."""
        orch._panes_by_project.setdefault("p", {})["backend"] = _make_pane(session=None)
        ok, msg = orch.send("backend", "hi", from_role="lead", project="p")
        assert ok is False
        assert msg == "backend is not running (spawn it first)"

    def test_no_pane_queues_even_with_prior_worktree_history(self, orch: Orchestrator) -> None:
        """A gap in the queued-send flow: PaneState remembering a prior
        isolated worktree must not somehow break queuing for a role with no
        pane at all — the worktree-continuation hint is `assign`'s concern
        (`_unknown_pane_message`, still used by `takkub task cancel`'s
        nothing-to-cancel case), not `send`'s."""
        orch._pane_state["p::backend"] = PaneState(
            worktree={
                "path": "C:/repo/worktrees/backend-123",
                "branch": "wt/backend-123",
                "base_sha": "abc123",
                "git_root": "C:/repo",
            }
        )
        ok, msg = orch.send("backend", "hi", from_role="lead", project="p")
        assert ok is True
        assert "takkub assign --role backend" in msg

    def test_no_pane_no_worktree_history_still_queues(self, orch: Orchestrator) -> None:
        ok, msg = orch.send("qa", "hi", from_role="lead", project="p")
        assert ok is True
        assert "queued" in msg

    def test_live_pane_still_delivers_normally(self, orch: Orchestrator) -> None:
        pane = _make_pane(session=_make_alive_session())
        orch._panes_by_project.setdefault("p", {})["backend"] = pane
        ok, _msg = orch.send("backend", "hi", from_role="lead", project="p")
        assert ok is True
        pane.session.write.assert_called()


class TestQueuedMessageFlush:
    """#303 item 3: a message queued for a role with no pane must actually
    reach it once the pane spawns, not just sit recorded forever."""

    def test_queued_message_is_recorded_and_readable_back(self, orch: Orchestrator) -> None:
        from agent_takkub import role_messages

        ok, _msg = orch.send("backend", "safety note", from_role="lead", project="p")
        assert ok is True

        pending = role_messages.queued_no_pane_for_role(orch_mod.RUNTIME_DIR, "p", "backend")
        assert len(pending) == 1
        assert pending[0]["body"] == "safety note"
        assert pending[0]["from"] == "lead"

    def test_flush_delivers_once_pane_is_alive_and_clears_the_queue(
        self, orch: Orchestrator
    ) -> None:
        from agent_takkub import role_messages

        orch.send("backend", "safety note", from_role="lead", project="p")
        pane = _make_pane(session=_make_alive_session())
        orch._panes_by_project.setdefault("p", {})["backend"] = pane

        orch._flush_queued_no_pane_messages("p", "backend")

        pane.session.write.assert_called()
        assert role_messages.queued_no_pane_for_role(orch_mod.RUNTIME_DIR, "p", "backend") == []
        # The delivery itself went through send() again, landing a fresh
        # "sent" record distinct from the now-abandoned queued placeholder.
        all_records = role_messages.read(orch_mod.RUNTIME_DIR, "p", role="backend")
        assert any(r["state"] == "sent" for r in all_records)
        assert any(r["state"] == "abandoned" for r in all_records)

    def test_flush_is_a_noop_when_pane_still_not_alive(self, orch: Orchestrator) -> None:
        from agent_takkub import role_messages

        orch.send("backend", "safety note", from_role="lead", project="p")
        orch._flush_queued_no_pane_messages("p", "backend")

        pending = role_messages.queued_no_pane_for_role(orch_mod.RUNTIME_DIR, "p", "backend")
        assert len(pending) == 1  # untouched — still queued, retried on the next spawn
