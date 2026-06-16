"""Tests for Phase 2b — peer-to-peer CC Lead durability.

Covers:
  - CC delivered immediately when Lead is alive
  - CC queued when Lead is not alive + "send_cc_queued" event logged
  - Pending queue flushed when Lead spawns (_flush_pending_lead_cc)
  - Queue persisted to / loaded from disk (survives orchestrator restart)
  - Multi-project isolation: project A queue != project B queue
  - send() logs full body, not msg_preview
  - Leader-involved messages (Lead→teammate, teammate→Lead) do NOT queue CC
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import LEAD, Orchestrator

# ─────────────────────────────────────────────────────────────
# Shared fixtures
# ─────────────────────────────────────────────────────────────


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


def _make_dead_session() -> MagicMock:
    s = MagicMock()
    s.is_alive = False
    return s


def _make_pane(session=None) -> MagicMock:
    p = MagicMock()
    p.session = session
    return p


@pytest.fixture
def orch(qapp, tmp_path, monkeypatch) -> Orchestrator:
    """Orchestrator with mocked startup I/O."""
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
        # manual minimal __init__ to avoid Qt widget creation
        from PyQt6.QtCore import QObject

        QObject.__init__(o)
        o._panes_by_project = {}
        o._pane_state = {}
        o._idle_state = {}
        o._recent_exits = {}
        o._recent_done = []
        o._pending_lead_cc = {}
    return o


def _register_pane(orch: Orchestrator, role: str, project: str, session=None) -> MagicMock:
    pane = _make_pane(session)
    orch._panes_by_project.setdefault(project, {})[role] = pane
    return pane


# ─────────────────────────────────────────────────────────────
# 1. CC delivered immediately when Lead is alive
# ─────────────────────────────────────────────────────────────


class TestCCDeliveredImmediately:
    def test_cc_to_lead_when_alive(self, orch, tmp_path, monkeypatch):
        """Peer message while Lead alive → CC written to Lead session, no queue."""
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
        monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
        monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)

        proj = "proj_a"
        lead_session = _make_alive_session()
        _register_pane(orch, LEAD.name, proj, lead_session)
        frontend_session = _make_alive_session()
        _register_pane(orch, "frontend", proj, frontend_session)
        backend_session = _make_alive_session()
        _register_pane(orch, "backend", proj, backend_session)

        ok, _msg = orch.send("backend", "API ready", from_role="frontend", project=proj)

        assert ok
        # Lead session must have received something
        assert lead_session.write.called
        # No queue entry
        assert not orch._pending_lead_cc.get(proj)

    def test_no_cc_when_lead_is_target(self, orch, tmp_path, monkeypatch):
        """teammate → Lead messages do NOT queue a CC (Lead IS the target)."""
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
        monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
        monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)
        monkeypatch.setattr(orch, "_save_pending_cc", lambda ns: None)

        proj = "proj_a"
        lead_session = _make_alive_session()
        _register_pane(orch, LEAD.name, proj, lead_session)
        _register_pane(orch, "backend", proj, _make_alive_session())

        orch.send(LEAD.name, "blocked: need clarification", from_role="backend", project=proj)
        # Lead received the message directly (the main delivery)
        assert lead_session.write.called
        # No CC was queued — Lead already got it directly as the recipient
        assert not orch._pending_lead_cc.get(proj)

    def test_no_cc_when_lead_is_sender(self, orch, tmp_path, monkeypatch):
        """Lead → teammate messages do NOT queue CC."""
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
        monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
        monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)

        proj = "proj_a"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "backend", proj, _make_alive_session())

        orch.send("backend", "go implement X", from_role=LEAD.name, project=proj)
        assert not orch._pending_lead_cc.get(proj)


# ─────────────────────────────────────────────────────────────
# 2. CC queued when Lead is not alive
# ─────────────────────────────────────────────────────────────


class TestCCQueuedWhenLeadDown:
    def test_queued_when_lead_dead(self, orch, tmp_path, monkeypatch):
        """Peer message while Lead down → CC queued, not lost."""
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
        monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
        monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)
        saved = {}
        monkeypatch.setattr(orch, "_save_pending_cc", lambda ns: saved.update({ns: True}))

        proj = "proj_a"
        _register_pane(orch, LEAD.name, proj, _make_dead_session())
        _register_pane(orch, "frontend", proj, _make_alive_session())
        _register_pane(orch, "backend", proj, _make_alive_session())

        orch.send("backend", "API ready", from_role="frontend", project=proj)

        pending = orch._pending_lead_cc.get(proj, [])
        assert len(pending) == 1
        assert "[CC]" in pending[0]["body"]
        assert "frontend" in pending[0]["from_role"]
        assert saved.get(proj)

    def test_queued_when_lead_absent(self, orch, tmp_path, monkeypatch):
        """Lead pane doesn't exist → CC still queued."""
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
        monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
        monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)
        monkeypatch.setattr(orch, "_save_pending_cc", lambda ns: None)

        proj = "proj_a"
        # Lead pane NOT registered (simulates Lead not spawned yet)
        _register_pane(orch, "frontend", proj, _make_alive_session())
        _register_pane(orch, "backend", proj, _make_alive_session())

        orch.send("backend", "spec attached", from_role="frontend", project=proj)

        pending = orch._pending_lead_cc.get(proj, [])
        assert len(pending) == 1

    def test_multiple_queued_messages(self, orch, tmp_path, monkeypatch):
        """Multiple peer messages while Lead down → all queued in order."""
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
        monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
        monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)
        monkeypatch.setattr(orch, "_save_pending_cc", lambda ns: None)

        proj = "proj_a"
        _register_pane(orch, LEAD.name, proj, _make_dead_session())
        _register_pane(orch, "frontend", proj, _make_alive_session())
        _register_pane(orch, "backend", proj, _make_alive_session())

        orch.send("backend", "msg1", from_role="frontend", project=proj)
        orch.send("frontend", "msg2", from_role="backend", project=proj)

        pending = orch._pending_lead_cc.get(proj, [])
        assert len(pending) == 2

    def test_send_cc_queued_event_logged(self, orch, tmp_path, monkeypatch):
        """send_cc_queued event written to events.log when CC is queued."""
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
        log_path = tmp_path / "events.log"
        monkeypatch.setattr(orch_mod, "EVENTS_LOG", log_path)
        monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)
        monkeypatch.setattr(orch, "_save_pending_cc", lambda ns: None)

        proj = "proj_a"
        _register_pane(orch, LEAD.name, proj, _make_dead_session())
        _register_pane(orch, "frontend", proj, _make_alive_session())
        _register_pane(orch, "backend", proj, _make_alive_session())

        orch.send("backend", "hello", from_role="frontend", project=proj)

        events = [
            json.loads(ln) for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()
        ]
        cc_events = [e for e in events if e["event"] == "send_cc_queued"]
        assert len(cc_events) == 1
        assert cc_events[0]["project"] == proj


# ─────────────────────────────────────────────────────────────
# 3. Flush pending CC when Lead is alive
# ─────────────────────────────────────────────────────────────


class TestFlushPendingCC:
    def test_flush_delivers_to_lead(self, orch, tmp_path, monkeypatch):
        """_flush_pending_lead_cc writes queued CC bodies to Lead session."""
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
        monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
        monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)

        proj = "proj_a"
        lead_session = _make_alive_session()
        _register_pane(orch, LEAD.name, proj, lead_session)

        orch._pending_lead_cc[proj] = [
            {
                "from_role": "frontend",
                "to_role": "backend",
                "body": "[CC] msg1",
                "ts": "2026-01-01T00:00:00",
            },
            {
                "from_role": "qa",
                "to_role": "backend",
                "body": "[CC] msg2",
                "ts": "2026-01-01T00:00:01",
            },
        ]

        save_calls = []
        monkeypatch.setattr(orch, "_save_pending_cc", lambda ns: save_calls.append(ns))

        orch._flush_pending_lead_cc(proj)

        assert lead_session.write.called
        assert proj not in orch._pending_lead_cc
        assert save_calls  # save was called to clear persisted queue

    def test_flush_clears_queue(self, orch, tmp_path, monkeypatch):
        """After flush, _pending_lead_cc[project] is empty."""
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
        monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
        monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)

        proj = "proj_a"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        orch._pending_lead_cc[proj] = [
            {"from_role": "qa", "to_role": "frontend", "body": "[CC] test", "ts": "2026-01-01"}
        ]
        monkeypatch.setattr(orch, "_save_pending_cc", lambda ns: None)

        orch._flush_pending_lead_cc(proj)

        assert proj not in orch._pending_lead_cc

    def test_flush_noop_when_lead_dead(self, orch, tmp_path, monkeypatch):
        """_flush_pending_lead_cc is a no-op when Lead is not alive (keeps queue)."""
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
        monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
        monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)

        proj = "proj_a"
        _register_pane(orch, LEAD.name, proj, _make_dead_session())
        orch._pending_lead_cc[proj] = [
            {"from_role": "qa", "to_role": "frontend", "body": "[CC] test", "ts": "2026-01-01"}
        ]

        orch._flush_pending_lead_cc(proj)

        # Queue preserved — Lead wasn't alive to receive it
        assert len(orch._pending_lead_cc.get(proj, [])) == 1

    def test_flush_noop_when_nothing_pending(self, orch, tmp_path, monkeypatch):
        """_flush_pending_lead_cc with empty queue makes no writes."""
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
        monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
        monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)

        proj = "proj_a"
        lead_session = _make_alive_session()
        _register_pane(orch, LEAD.name, proj, lead_session)

        orch._flush_pending_lead_cc(proj)

        lead_session.write.assert_not_called()


# ─────────────────────────────────────────────────────────────
# 4. Persist roundtrip
# ─────────────────────────────────────────────────────────────


class TestPersistRoundtrip:
    def test_save_writes_json_file(self, orch, tmp_path, monkeypatch):
        """_save_pending_cc writes runtime/pending-lead-cc-<project>.json."""
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
        monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)

        proj = "proj_a"
        items = [{"from_role": "qa", "to_role": "backend", "body": "[CC] test", "ts": "2026-01-01"}]
        orch._pending_lead_cc[proj] = items

        orch._save_pending_cc(proj)

        saved_path = tmp_path / f"pending-lead-cc-{proj}.json"
        assert saved_path.exists()
        loaded = json.loads(saved_path.read_text())
        assert loaded == items

    def test_save_empty_deletes_file(self, orch, tmp_path, monkeypatch):
        """Saving empty queue deletes the file instead of writing [] (avoids accumulation)."""
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
        monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)

        proj = "proj_a"
        saved_path = tmp_path / f"pending-lead-cc-{proj}.json"
        # pre-create file to verify it gets removed
        saved_path.write_text("[]", encoding="utf-8")

        orch._pending_lead_cc[proj] = []
        orch._save_pending_cc(proj)

        assert not saved_path.exists()

    def test_save_empty_noop_when_file_missing(self, orch, tmp_path, monkeypatch):
        """Saving empty queue when file doesn't exist is a no-op (no error)."""
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
        monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)

        proj = "proj_b"
        orch._pending_lead_cc[proj] = []
        # Should not raise even though file doesn't exist
        orch._save_pending_cc(proj)

        assert not (tmp_path / f"pending-lead-cc-{proj}.json").exists()

    def test_load_restores_queue(self, orch, tmp_path, monkeypatch):
        """_load_pending_cc reads existing files and populates _pending_lead_cc."""
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
        monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)

        proj = "myproject"
        items = [
            {"from_role": "qa", "to_role": "backend", "body": "[CC] saved", "ts": "2026-01-01"}
        ]
        (tmp_path / f"pending-lead-cc-{proj}.json").write_text(json.dumps(items), encoding="utf-8")

        orch._pending_lead_cc.clear()
        orch._load_pending_cc()

        assert orch._pending_lead_cc.get(proj) == items

    def test_load_skips_empty_files(self, orch, tmp_path, monkeypatch):
        """Files with [] are not populated (nothing to replay)."""
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
        monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)

        proj = "emptyproject"
        (tmp_path / f"pending-lead-cc-{proj}.json").write_text("[]", encoding="utf-8")

        orch._pending_lead_cc.clear()
        orch._load_pending_cc()

        assert proj not in orch._pending_lead_cc


# ─────────────────────────────────────────────────────────────
# 5. Multi-project isolation
# ─────────────────────────────────────────────────────────────


class TestMultiProjectIsolation:
    def test_queues_are_separate(self, orch, tmp_path, monkeypatch):
        """CC queued in proj_a does not appear in proj_b's queue."""
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
        monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
        monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)
        monkeypatch.setattr(orch, "_save_pending_cc", lambda ns: None)

        for proj in ("proj_a", "proj_b"):
            _register_pane(orch, LEAD.name, proj, _make_dead_session())
            _register_pane(orch, "frontend", proj, _make_alive_session())
            _register_pane(orch, "backend", proj, _make_alive_session())

        orch.send("backend", "hello", from_role="frontend", project="proj_a")

        assert len(orch._pending_lead_cc.get("proj_a", [])) == 1
        assert len(orch._pending_lead_cc.get("proj_b", [])) == 0

    def test_flush_only_target_project(self, orch, tmp_path, monkeypatch):
        """Flushing proj_a queue doesn't touch proj_b's queue."""
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
        monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
        monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)
        monkeypatch.setattr(orch, "_save_pending_cc", lambda ns: None)

        for proj in ("proj_a", "proj_b"):
            _register_pane(orch, LEAD.name, proj, _make_alive_session())
            orch._pending_lead_cc[proj] = [
                {
                    "from_role": "qa",
                    "to_role": "backend",
                    "body": f"[CC] {proj}",
                    "ts": "2026-01-01",
                }
            ]

        orch._flush_pending_lead_cc("proj_a")

        assert "proj_a" not in orch._pending_lead_cc
        assert len(orch._pending_lead_cc.get("proj_b", [])) == 1


# ─────────────────────────────────────────────────────────────
# 6. Log event uses full body
# ─────────────────────────────────────────────────────────────


class TestSendEventLogsFullBody:
    def test_send_logs_body_not_preview(self, orch, tmp_path, monkeypatch):
        """send() logs 'body' field (not old 'msg_preview') in events.log."""
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
        log_path = tmp_path / "events.log"
        monkeypatch.setattr(orch_mod, "EVENTS_LOG", log_path)
        monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)
        monkeypatch.setattr(orch, "_save_pending_cc", lambda ns: None)

        proj = "proj_a"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "frontend", proj, _make_alive_session())
        _register_pane(orch, "backend", proj, _make_alive_session())

        orch.send("backend", "hello world", from_role="frontend", project=proj)

        events = [
            json.loads(ln) for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()
        ]
        send_events = [e for e in events if e["event"] == "send"]
        assert send_events, "Expected a 'send' event"
        ev = send_events[0]
        assert "body" in ev, f"Expected 'body' field in event, got: {ev.keys()}"
        assert "msg_preview" not in ev, "Old 'msg_preview' field should be removed"

    def test_long_body_truncated_with_marker(self, orch, tmp_path, monkeypatch):
        """send() truncates very long messages in the log but marks the truncation."""
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
        log_path = tmp_path / "events.log"
        monkeypatch.setattr(orch_mod, "EVENTS_LOG", log_path)
        monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)
        monkeypatch.setattr(orch, "_save_pending_cc", lambda ns: None)

        proj = "proj_a"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "backend", proj, _make_alive_session())

        long_msg = "x" * 10_000
        orch.send("backend", long_msg, from_role=LEAD.name, project=proj)

        events = [
            json.loads(ln) for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()
        ]
        send_events = [e for e in events if e["event"] == "send"]
        ev = send_events[0]
        assert len(ev["body"]) <= 4_200  # 4096 chars + ellipsis
        assert ev["body"].endswith("…")


# ─────────────────────────────────────────────────────────────
# 7. Write-failure durability (M4#22: pop-before-write)
# ─────────────────────────────────────────────────────────────


class TestFlushWriteFailureKeepsRemainder:
    def test_undelivered_tail_preserved_on_write_error(self, orch, tmp_path, monkeypatch):
        """If a session.write raises mid-flush, the messages not yet written stay
        queued + re-persisted instead of being lost (the bug pop-before-write had)."""
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
        monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
        monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)

        proj = "proj_a"
        lead_session = _make_alive_session()
        # write succeeds for item 1, raises for item 2 → item 2 must survive
        lead_session.write = MagicMock(side_effect=[None, RuntimeError("session gone")])
        _register_pane(orch, LEAD.name, proj, lead_session)

        orch._pending_lead_cc[proj] = [
            {"from_role": "frontend", "to_role": "backend", "body": "[CC] m1", "ts": "t1"},
            {"from_role": "qa", "to_role": "backend", "body": "[CC] m2", "ts": "t2"},
        ]
        save_calls: list[str] = []
        monkeypatch.setattr(orch, "_save_pending_cc", lambda ns: save_calls.append(ns))

        with pytest.raises(RuntimeError):
            orch._flush_pending_lead_cc(proj)

        # the undelivered message stays queued (not lost) and was re-persisted
        remaining = orch._pending_lead_cc.get(proj, [])
        assert len(remaining) == 1
        assert remaining[0]["body"] == "[CC] m2"
        assert proj in save_calls

    def test_all_delivered_clears_queue(self, orch, tmp_path, monkeypatch):
        """The happy path still fully clears + persists the empty queue."""
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
        monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
        monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)

        proj = "proj_a"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        orch._pending_lead_cc[proj] = [
            {"from_role": "qa", "to_role": "backend", "body": "[CC] m1", "ts": "t1"},
            {"from_role": "qa", "to_role": "backend", "body": "[CC] m2", "ts": "t2"},
        ]
        monkeypatch.setattr(orch, "_save_pending_cc", lambda ns: None)

        orch._flush_pending_lead_cc(proj)

        assert proj not in orch._pending_lead_cc
