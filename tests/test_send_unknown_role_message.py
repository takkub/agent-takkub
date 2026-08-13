"""Tests for issue #164: `takkub send --to <role>` said "unknown role: X"
for a role that IS in the registry but simply has no pane open right now
(closed, or restarted-and-not-yet-respawned). `send()` must tell the two
cases apart:

  - name not in `roles.by_name(...)` at all -> "unknown role: X"
  - name IS a real role, just no live pane -> a hint pointing at
    `takkub assign --role X ...`, plus a worktree-continuation hint when
    PaneState still remembers an isolated worktree from a prior assignment.
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

    def test_registered_role_with_no_pane_gives_assign_hint(self, orch: Orchestrator) -> None:
        # backend is a real DEFAULT_TEAMMATES role, but never registered/spawned here.
        ok, msg = orch.send("backend", "hi", from_role="lead", project="p")
        assert ok is False
        assert "unknown role" not in msg
        assert "backend" in msg
        assert "takkub assign --role backend" in msg

    def test_sharded_role_name_resolves_against_base_role(self, orch: Orchestrator) -> None:
        """`qa#1` is a shard instance of the real `qa` role — the registry
        only knows base names, so the "known role, no pane" hint (not
        "unknown role") must still fire, keyed off the base name."""
        ok, msg = orch.send("qa#1", "hi", from_role="lead", project="p")
        assert ok is False
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

    def test_no_pane_but_prior_worktree_adds_cwd_hint(self, orch: Orchestrator) -> None:
        orch._pane_state["p::backend"] = PaneState(
            worktree={
                "path": "C:/repo/worktrees/backend-123",
                "branch": "wt/backend-123",
                "base_sha": "abc123",
                "git_root": "C:/repo",
            }
        )
        ok, msg = orch.send("backend", "hi", from_role="lead", project="p")
        assert ok is False
        assert "takkub assign --role backend" in msg
        assert "--isolation worktree" in msg
        assert "--cwd C:/repo/worktrees/backend-123" in msg

    def test_no_pane_no_worktree_history_omits_cwd_hint(self, orch: Orchestrator) -> None:
        ok, msg = orch.send("qa", "hi", from_role="lead", project="p")
        assert ok is False
        assert "--cwd" not in msg

    def test_live_pane_still_delivers_normally(self, orch: Orchestrator) -> None:
        pane = _make_pane(session=_make_alive_session())
        orch._panes_by_project.setdefault("p", {})["backend"] = pane
        ok, _msg = orch.send("backend", "hi", from_role="lead", project="p")
        assert ok is True
        pane.session.write.assert_called()
