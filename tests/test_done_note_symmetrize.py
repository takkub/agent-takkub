"""Tests for symmetrizing done()'s return path with the file-based task
handoff (issue #1): a long `done()` note gets pointed at its session-md
file instead of carrying the full text inline in the Lead notice.

- Notes below TASK_HANDOFF_THRESHOLD paste inline unchanged (no-op check).
- Notes at/above threshold condense to a first-line headline (~200 chars)
  + a "📄 รายงานเต็ม: <path>" pointer at the file `_save_decision_note`
  wrote — mirroring `_task_handoff_pointer`'s wording for the pane→Lead
  direction.
- The session-md file is written BEFORE the notice is built/sent (so the
  pointer is never dangling).
- `done --fail` always keeps the FULL note in the notice regardless of
  length (Lead's fix-loop propose + classify_failure both read it).
- Evidence lines ('📸 evidence:' / '⚠ no screenshot evidence') stay on the
  notice tail whether the note was condensed or not.
- The pointer path always uses forward slashes.
- Shard consolidated handoff applies the same condensation per shard.
"""

from __future__ import annotations

import pathlib
import time
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import LEAD, Orchestrator, PaneState
from agent_takkub.orchestrator_text import TASK_HANDOFF_THRESHOLD


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
    p.state = "working"
    p.set_state = MagicMock()
    return p


@pytest.fixture
def orch(qapp, tmp_path, monkeypatch) -> Orchestrator:
    """Minimal Orchestrator with a REAL `_save_decision_note` (writes into
    tmp_path) so the pointer path can be exercised end to end — only the
    vault mirror and hot.md refresh are stubbed out."""
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
    monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)
    monkeypatch.setattr(orch_mod, "_resolve_vault_dir", lambda: None)
    monkeypatch.setattr(orch_mod, "active_project", lambda: ("proj", {}))

    from unittest.mock import patch

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
    monkeypatch.setattr(o, "_write_hot_md", MagicMock())
    return o


def _register_pane(orch: Orchestrator, role: str, project: str, session=None) -> MagicMock:
    pane = _make_pane(session)
    orch._panes_by_project.setdefault(project, {})[role] = pane
    return pane


def _long_note(marker: str) -> str:
    """A single-line note comfortably at/above TASK_HANDOFF_THRESHOLD."""
    return f"{marker}: " + ("x" * TASK_HANDOFF_THRESHOLD)


class TestShortNoteUnchanged:
    def test_short_note_pastes_inline_no_pointer(self, orch, tmp_path):
        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "backend", proj, _make_alive_session())

        captured: list[str] = []
        orch._notify_lead = lambda ns, notice, **kw: captured.append(notice)  # type: ignore[assignment]

        note = "short note under threshold"
        assert len(note) < TASK_HANDOFF_THRESHOLD
        orch.done("backend", note=note, project=proj)

        assert captured == [f"[backend done] {note}"]
        assert "📄 รายงานเต็ม" not in captured[0]


class TestLongNoteCondenses:
    def test_long_note_condenses_to_headline_and_pointer(self, orch, tmp_path):
        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "backend", proj, _make_alive_session())

        captured: list[str] = []
        orch._notify_lead = lambda ns, notice, **kw: captured.append(notice)  # type: ignore[assignment]

        note = _long_note("long-note-condense")
        orch.done("backend", note=note, project=proj)

        assert captured
        notice = captured[0]
        assert notice.startswith("[backend done] long-note-condense:")
        assert "📄 รายงานเต็ม:" in notice
        assert "file-read tool" in notice
        # headline is bounded well under the full ~400+ char note
        assert len(notice) < len(note)

    def test_pointer_file_exists_and_holds_full_note(self, orch, tmp_path):
        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "backend", proj, _make_alive_session())

        captured: list[str] = []
        orch._notify_lead = lambda ns, notice, **kw: captured.append(notice)  # type: ignore[assignment]

        note = _long_note("long-note-fileproof")
        orch.done("backend", note=note, project=proj)

        notice = captured[0]
        marker = "📄 รายงานเต็ม: "
        assert marker in notice
        path_str = notice.split(marker, 1)[1].split(" (เปิดด้วย", 1)[0]
        path = pathlib.Path(path_str)
        assert path.exists()
        assert note in path.read_text(encoding="utf-8")

    def test_pointer_path_uses_forward_slashes(self, orch, tmp_path):
        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "backend", proj, _make_alive_session())

        captured: list[str] = []
        orch._notify_lead = lambda ns, notice, **kw: captured.append(notice)  # type: ignore[assignment]

        note = _long_note("long-note-slashes")
        orch.done("backend", note=note, project=proj)

        notice = captured[0]
        marker = "📄 รายงานเต็ม: "
        path_str = notice.split(marker, 1)[1].split(" (เปิดด้วย", 1)[0]
        assert "\\" not in path_str


class TestThresholdBoundary:
    def test_just_under_threshold_is_inline(self, orch, tmp_path):
        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "backend", proj, _make_alive_session())

        captured: list[str] = []
        orch._notify_lead = lambda ns, notice, **kw: captured.append(notice)  # type: ignore[assignment]

        note = "boundary-under-" + ("x" * (TASK_HANDOFF_THRESHOLD - 1 - len("boundary-under-")))
        assert len(note) == TASK_HANDOFF_THRESHOLD - 1
        orch.done("backend", note=note, project=proj)

        assert captured[0] == f"[backend done] {note}"

    def test_exactly_at_threshold_condenses(self, orch, tmp_path):
        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "backend", proj, _make_alive_session())

        captured: list[str] = []
        orch._notify_lead = lambda ns, notice, **kw: captured.append(notice)  # type: ignore[assignment]

        note = "boundary-atexactly-" + ("x" * (TASK_HANDOFF_THRESHOLD - len("boundary-atexactly-")))
        assert len(note) == TASK_HANDOFF_THRESHOLD
        orch.done("backend", note=note, project=proj)

        assert "📄 รายงานเต็ม:" in captured[0]
        assert captured[0] != f"[backend done] {note}"


class TestFailAlwaysFull:
    def test_fail_keeps_full_note_regardless_of_length(self, orch, tmp_path):
        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "qa", proj, _make_alive_session())

        captured: list[str] = []
        orch._notify_lead = lambda ns, notice, **kw: captured.append(notice)  # type: ignore[assignment]

        note = _long_note("fail-note-full")
        orch.done("qa", note=note, project=proj, failed=True)

        assert captured
        notice = captured[0]
        assert "FAILED" in notice
        assert note in notice
        assert "📄 รายงานเต็ม:" not in notice


class TestEvidenceStillAppended:
    def test_evidence_appended_after_condensed_headline(self, orch, tmp_path):
        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "qa", proj, _make_alive_session())

        assign_ts = time.time() - 60
        orch._pane_state[f"{proj}::qa"] = PaneState(assign_ts=assign_ts)
        today = time.strftime("%Y-%m-%d")
        shots = tmp_path / "exports" / today / proj / "qa" / "screenshots"
        shots.mkdir(parents=True, exist_ok=True)
        img = shots / "evidence.png"
        img.write_bytes(b"fake-image-bytes")
        import os

        mt = assign_ts + 10
        os.utime(img, (mt, mt))

        captured: list[str] = []
        orch._notify_lead = lambda ns, notice, **kw: captured.append(notice)  # type: ignore[assignment]

        note = _long_note("long-note-with-evidence")
        orch.done("qa", note=note, project=proj)

        notice = captured[0]
        assert "📸 evidence:" in notice
        assert "evidence.png" in notice
        # evidence tail must survive even though the headline was truncated
        assert notice.rstrip().endswith("evidence.png")

    def test_evidence_appended_to_short_note_unchanged(self, orch, tmp_path):
        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "qa", proj, _make_alive_session())

        assign_ts = time.time() - 60
        orch._pane_state[f"{proj}::qa"] = PaneState(assign_ts=assign_ts)
        today = time.strftime("%Y-%m-%d")
        shots = tmp_path / "exports" / today / proj / "qa" / "screenshots"
        shots.mkdir(parents=True, exist_ok=True)
        img = shots / "short-evidence.png"
        img.write_bytes(b"fake-image-bytes")
        import os

        mt = assign_ts + 10
        os.utime(img, (mt, mt))

        captured: list[str] = []
        orch._notify_lead = lambda ns, notice, **kw: captured.append(notice)  # type: ignore[assignment]

        orch.done("qa", note="short note stays inline", project=proj)

        notice = captured[0]
        assert notice.startswith("[qa done] short note stays inline")
        assert "📸 evidence:" in notice
        assert "short-evidence.png" in notice


class TestWriteBeforeNotice:
    def test_session_md_written_before_notice_sent(self, orch, tmp_path):
        """The session-md file must exist by the time the Lead notice fires,
        so the pointer it carries is never dangling."""
        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "backend", proj, _make_alive_session())

        seen_paths: list[str] = []

        def _fake_notify(ns, notice, **kw):
            marker = "📄 รายงานเต็ม: "
            assert marker in notice
            path_str = notice.split(marker, 1)[1].split(" (เปิดด้วย", 1)[0]
            seen_paths.append(path_str)
            # At the moment the notice fires, the file must already be on disk.
            assert pathlib.Path(path_str).exists()

        orch._notify_lead = _fake_notify  # type: ignore[assignment]

        note = _long_note("write-before-notice")
        orch.done("backend", note=note, project=proj)

        assert seen_paths


class TestShardConsolidatedHandoffSymmetrizes:
    def test_shard_group_stores_condensed_note(self, orch, tmp_path):
        """A long shard note is condensed the same way before landing in
        the shard group's aggregate — the consolidated handoff shouldn't
        stitch N full notes together."""
        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "qa#1", proj, _make_alive_session())

        orch._pane_state[f"{proj}::qa#1"] = PaneState(shard_total=2)

        from agent_takkub.pipeline_executor import ShardGroup

        group = ShardGroup(base_role="qa", total=2)
        orch._shard_groups = {f"{proj}::qa": group}
        orch._inject_shard_fanout_handoff = MagicMock()  # type: ignore[assignment]

        note = _long_note("shard-condense")
        orch.done("qa#1", note=note, project=proj)

        stored = group.done["qa#1"]
        assert "📄 รายงานเต็ม:" in stored
        assert stored != note

    def test_shard_group_keeps_full_note_on_fail(self, orch, tmp_path):
        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "qa#1", proj, _make_alive_session())

        orch._pane_state[f"{proj}::qa#1"] = PaneState(shard_total=2)

        from agent_takkub.pipeline_executor import ShardGroup

        group = ShardGroup(base_role="qa", total=2)
        orch._shard_groups = {f"{proj}::qa": group}
        orch._inject_shard_fanout_handoff = MagicMock()  # type: ignore[assignment]

        note = _long_note("shard-fail-full")
        orch.done("qa#1", note=note, project=proj, failed=True)

        # Failed shards land in group.failed (not group.done) and keep the
        # FULL note uncondensed, mirroring the non-shard fail path so Lead's
        # fix-loop propose + classify_failure can read it in full.
        assert "qa#1" in group.failed
        assert "qa#1" not in group.done
        assert group.failed_notes["qa#1"] == note
