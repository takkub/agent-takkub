"""Tests for stall detection: last_progress_ts tracking + list_status_detailed.

What these tests pin down:
  - _compute_last_progress_ts returns 0.0 when no signals exist
  - transcript mtime is picked up as a progress signal
  - _last_send_ts is picked up as a progress signal
  - list_status_detailed returns stall_minutes=None for non-working panes
  - list_status_detailed returns stall_minutes=None when progress is recent
  - list_status_detailed returns stall_minutes=N when no progress > STALL_THRESHOLD_SEC
  - pane_status_report returns any_stalled=True when a stalled pane exists
"""

from __future__ import annotations

import pathlib
import time
from unittest.mock import MagicMock

import pytest

from agent_takkub.orchestrator import (
    LEAD,
    Orchestrator,
    PaneState,
)


class _FakePane:
    """Minimal pane stub for stall-detection tests."""

    def __init__(
        self,
        state: str = "working",
        session_alive: bool = True,
        transcript_path: str | None = None,
        cwd: str = "/x",
    ) -> None:
        self.state = state
        self._session_cwd = cwd
        self._transcript_path = transcript_path
        if session_alive:
            sess = MagicMock()
            sess.is_alive = True
            self.session = sess
        else:
            self.session = None


class _FakeOrch:
    """Minimal orchestrator stub — only the stall-detection methods."""

    def __init__(self) -> None:
        self._panes_by_project: dict[str, dict] = {}
        self._pane_state: dict[str, PaneState] = {}

    def _ps(self, key: str) -> PaneState:
        try:
            return self._pane_state[key]
        except KeyError:
            ps = PaneState()
            self._pane_state[key] = ps
            return ps

    def _resolve_project(self, project: str | None) -> str:
        return project or "default"

    def _project_panes(self, project: str | None = None) -> dict:
        ns = self._resolve_project(project)
        return self._panes_by_project.setdefault(ns, {})

    # Bind real orchestrator methods so we don't duplicate logic
    _compute_last_progress_ts = Orchestrator._compute_last_progress_ts
    list_status_detailed = Orchestrator.list_status_detailed
    pane_status_report = Orchestrator.pane_status_report


@pytest.fixture
def runtime_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    """Redirect RUNTIME_DIR into tmp_path so tests don't touch real session dirs."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    import agent_takkub.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", runtime)
    return runtime


class TestComputeLastProgressTs:
    def _make_orch(self) -> _FakeOrch:
        return _FakeOrch()

    def test_no_signals_returns_zero(self, runtime_tmp: pathlib.Path) -> None:
        orch = self._make_orch()
        pane = _FakePane(transcript_path=None)
        ts = orch._compute_last_progress_ts("qa", "default", pane)
        assert ts == 0.0

    def test_transcript_mtime_picked_up(
        self, runtime_tmp: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        transcript = tmp_path / "qa-120000.transcript.log"
        transcript.write_bytes(b"output bytes")
        orch = self._make_orch()
        pane = _FakePane(transcript_path=str(transcript))
        ts = orch._compute_last_progress_ts("qa", "default", pane)
        assert ts == pytest.approx(transcript.stat().st_mtime, abs=1)

    def test_send_ts_picked_up(self, runtime_tmp: pathlib.Path) -> None:
        orch = self._make_orch()
        pane = _FakePane(transcript_path=None)
        send_time = time.time() - 30
        orch._ps("default::qa").last_send_ts = send_time
        ts = orch._compute_last_progress_ts("qa", "default", pane)
        assert ts == pytest.approx(send_time, abs=1)

    def test_most_recent_signal_wins(
        self, runtime_tmp: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        import os

        transcript = tmp_path / "qa-120000.transcript.log"
        transcript.write_bytes(b"x")
        # Backdate transcript to 2 minutes ago so send_ts is clearly newer
        old_ts = time.time() - 120
        os.utime(transcript, (old_ts, old_ts))
        orch = self._make_orch()
        pane = _FakePane(transcript_path=str(transcript))
        # Send is 5 seconds ago — newer than transcript
        recent_send = time.time() - 5
        orch._ps("default::qa").last_send_ts = recent_send
        ts = orch._compute_last_progress_ts("qa", "default", pane)
        assert ts == pytest.approx(recent_send, abs=1)

    def test_screenshot_dir_mtime_picked_up(
        self, runtime_tmp: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datetime import datetime

        today = datetime.now().strftime("%Y-%m-%d")
        shot_dir = runtime_tmp / "exports" / today / "myproj" / "screenshots"
        shot_dir.mkdir(parents=True)
        (shot_dir / "s1-01.png").write_bytes(b"img")
        orch = self._make_orch()
        pane = _FakePane(transcript_path=None)
        ts = orch._compute_last_progress_ts("qa", "myproj", pane)
        assert ts == pytest.approx(shot_dir.stat().st_mtime, abs=1)

    def test_screenshot_dir_ignored_for_non_ui_roles(self, runtime_tmp: pathlib.Path) -> None:
        """backend/frontend/devops roles must NOT pick up screenshot dir mtime."""
        from datetime import datetime

        today = datetime.now().strftime("%Y-%m-%d")
        shot_dir = runtime_tmp / "exports" / today / "myproj" / "screenshots"
        shot_dir.mkdir(parents=True)
        (shot_dir / "s1-01.png").write_bytes(b"img")
        orch = self._make_orch()
        for non_ui_role in ("backend", "frontend", "devops", "mobile"):
            pane = _FakePane(transcript_path=None)
            ts = orch._compute_last_progress_ts(non_ui_role, "myproj", pane)
            assert ts == 0.0, f"role={non_ui_role} should not pick up screenshot mtime"


class TestListStatusDetailed:
    def _setup_orch(self, pane: _FakePane, role: str = "qa") -> _FakeOrch:
        orch = _FakeOrch()
        orch._panes_by_project["default"] = {role: pane}
        return orch

    def test_non_working_pane_no_stall(self, runtime_tmp: pathlib.Path) -> None:
        pane = _FakePane(state="active")
        orch = self._setup_orch(pane)
        result = orch.list_status_detailed("default")
        assert result["qa"]["stall_minutes"] is None

    def test_working_pane_no_baseline_no_stall(self, runtime_tmp: pathlib.Path) -> None:
        pane = _FakePane(state="working", transcript_path=None)
        orch = self._setup_orch(pane)
        result = orch.list_status_detailed("default")
        assert result["qa"]["stall_minutes"] is None

    def test_recent_send_no_stall(self, runtime_tmp: pathlib.Path) -> None:
        pane = _FakePane(state="working", transcript_path=None)
        orch = self._setup_orch(pane)
        orch._ps("default::qa").last_send_ts = time.time() - 60  # 1 min ago
        result = orch.list_status_detailed("default")
        assert result["qa"]["stall_minutes"] is None

    def test_stale_send_triggers_stall(
        self, runtime_tmp: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("agent_takkub.orchestrator.STALL_THRESHOLD_SEC", 300)
        pane = _FakePane(state="working", transcript_path=None)
        orch = self._setup_orch(pane)
        orch._ps("default::qa").last_send_ts = time.time() - 450  # 7.5 min ago
        result = orch.list_status_detailed("default")
        stall = result["qa"]["stall_minutes"]
        assert stall is not None
        assert stall >= 7

    def test_dead_session_not_stalled(self, runtime_tmp: pathlib.Path) -> None:
        pane = _FakePane(state="working", session_alive=False, transcript_path=None)
        orch = self._setup_orch(pane)
        orch._ps("default::qa").last_send_ts = time.time() - 600
        result = orch.list_status_detailed("default")
        assert result["qa"]["stall_minutes"] is None

    def test_lead_pane_included_as_is(self, runtime_tmp: pathlib.Path) -> None:
        pane = _FakePane(state="active")
        orch = _FakeOrch()
        orch._panes_by_project["default"] = {LEAD.name: pane}
        result = orch.list_status_detailed("default")
        assert LEAD.name in result
        assert result[LEAD.name]["stall_minutes"] is None


class TestPaneStatusReport:
    def test_any_stalled_false_when_no_stall(self, runtime_tmp: pathlib.Path) -> None:
        orch = _FakeOrch()
        pane = _FakePane(state="active")
        orch._panes_by_project["default"] = {"frontend": pane}
        report = orch.pane_status_report("default")
        assert report["any_stalled"] is False

    def test_any_stalled_true_when_stalled(
        self, runtime_tmp: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("agent_takkub.orchestrator.STALL_THRESHOLD_SEC", 300)
        orch = _FakeOrch()
        pane = _FakePane(state="working", transcript_path=None)
        orch._panes_by_project["default"] = {"qa": pane}
        orch._ps("default::qa").last_send_ts = time.time() - 400
        report = orch.pane_status_report("default")
        assert report["any_stalled"] is True

    def test_done_events_in_window(self, runtime_tmp: pathlib.Path) -> None:
        from datetime import datetime

        today = datetime.now().strftime("%Y-%m-%d")
        session_dir = runtime_tmp / "sessions" / today / "myproj"
        session_dir.mkdir(parents=True)
        (session_dir / "qa-120000.md").write_text("# qa done\n\nwork\n", encoding="utf-8")

        orch = _FakeOrch()
        pane = _FakePane(state="done", session_alive=False)
        orch._panes_by_project["myproj"] = {"qa": pane}
        since_ts = time.time() - 3600
        report = orch.pane_status_report("myproj", since_ts=since_ts)
        assert "qa-120000.md" in report["panes"]["qa"]["done_events"]

    def test_transcript_tail_returned(
        self, runtime_tmp: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        transcript = tmp_path / "qa.transcript.log"
        lines = [f"line {i}" for i in range(20)]
        transcript.write_text("\n".join(lines), encoding="utf-8")
        orch = _FakeOrch()
        pane = _FakePane(state="working", transcript_path=str(transcript))
        orch._ps("default::qa").last_send_ts = time.time() - 30
        orch._panes_by_project["default"] = {"qa": pane}
        report = orch.pane_status_report("default")
        tail = report["panes"]["qa"]["transcript_tail"]
        # Should end with the last 5 non-empty lines
        assert "line 19" in tail

    def test_transcript_tail_strips_ansi(
        self, runtime_tmp: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """ANSI escape codes in PTY transcript must be stripped before display."""
        transcript = tmp_path / "backend.transcript.log"
        transcript.write_text(
            "\x1b[32mgreen text\x1b[0m\nplain line\n\x1b[1;33mbold yellow\x1b[0m\n",
            encoding="utf-8",
        )
        orch = _FakeOrch()
        pane = _FakePane(state="working", transcript_path=str(transcript))
        orch._panes_by_project["default"] = {"backend": pane}
        report = orch.pane_status_report("default")
        tail = report["panes"]["backend"]["transcript_tail"]
        assert "\x1b[" not in tail
        assert "green text" in tail
        assert "plain line" in tail
        assert "bold yellow" in tail

    def test_non_ui_role_stall_not_suppressed_by_qa_screenshot(
        self, runtime_tmp: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """QA capturing screenshots must not suppress stall detection for backend."""
        from datetime import datetime

        monkeypatch.setattr("agent_takkub.orchestrator.STALL_THRESHOLD_SEC", 300)
        today = datetime.now().strftime("%Y-%m-%d")
        shot_dir = runtime_tmp / "exports" / today / "myproj" / "screenshots"
        shot_dir.mkdir(parents=True)
        (shot_dir / "s1-01.png").write_bytes(b"img")

        orch = _FakeOrch()
        # backend pane: stale send 8 min ago, no transcript
        backend_pane = _FakePane(state="working", transcript_path=None)
        orch._panes_by_project["myproj"] = {"backend": backend_pane}
        orch._ps("myproj::backend").last_send_ts = time.time() - 480

        result = orch.list_status_detailed("myproj")
        assert result["backend"]["stall_minutes"] is not None, (
            "backend should be stalled even when QA screenshots exist"
        )


class TestBuildPostCompactBrief:
    """Tests for _build_post_compact_brief — tied to _LAST_SESSION_FILE."""

    def _make_full_orch(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> Orchestrator:
        """Return an Orchestrator with Qt mocked out so we don't need a QApp."""
        monkeypatch.setattr(
            "agent_takkub.orchestrator.QTimer",
            MagicMock(),
        )
        monkeypatch.setattr(
            "agent_takkub.orchestrator.QObject.__init__",
            lambda self, parent=None: None,
        )
        import agent_takkub.orchestrator as orch_mod

        runtime = tmp_path / "runtime"
        runtime.mkdir()
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", runtime)
        snap = runtime / "last-session.json"
        monkeypatch.setattr(orch_mod, "_LAST_SESSION_FILE", snap)
        return orch_mod, runtime, snap

    def test_no_snapshot_file_returns_none(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orch = _FakeOrch()
        monkeypatch.setattr(
            "agent_takkub.orchestrator._LAST_SESSION_FILE",
            tmp_path / "nonexistent.json",
        )
        result = Orchestrator._build_post_compact_brief(orch, "myproj")  # type: ignore[arg-type]
        assert result is None

    def test_old_snapshot_returns_none(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        snap = tmp_path / "last-session.json"
        snap.write_text("{}", encoding="utf-8")
        # backdate mtime to 10 minutes ago
        old_ts = time.time() - 10 * 60
        import os

        os.utime(snap, (old_ts, old_ts))
        monkeypatch.setattr("agent_takkub.orchestrator._LAST_SESSION_FILE", snap)
        orch = _FakeOrch()
        result = Orchestrator._build_post_compact_brief(orch, "myproj")  # type: ignore[arg-type]
        assert result is None

    def test_no_alive_teammates_returns_none(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        snap = tmp_path / "last-session.json"
        snap.write_text("{}", encoding="utf-8")
        monkeypatch.setattr("agent_takkub.orchestrator._LAST_SESSION_FILE", snap)
        monkeypatch.setattr("agent_takkub.orchestrator._POST_COMPACT_DETECT_SEC", 600)
        orch = _FakeOrch()
        # No panes at all
        result = Orchestrator._build_post_compact_brief(orch, "myproj")  # type: ignore[arg-type]
        assert result is None

    def test_alive_teammate_brief_contains_role(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        snap = tmp_path / "last-session.json"
        snap.write_text("{}", encoding="utf-8")
        monkeypatch.setattr("agent_takkub.orchestrator._LAST_SESSION_FILE", snap)
        monkeypatch.setattr("agent_takkub.orchestrator._POST_COMPACT_DETECT_SEC", 600)
        monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", tmp_path / "runtime")
        (tmp_path / "runtime").mkdir(exist_ok=True)
        orch = _FakeOrch()
        qa_pane = _FakePane(state="working", transcript_path=None)
        orch._panes_by_project["myproj"] = {"qa": qa_pane}
        result = Orchestrator._build_post_compact_brief(orch, "myproj")  # type: ignore[arg-type]
        assert result is not None
        assert "qa" in result
        assert "Post-compact" in result
