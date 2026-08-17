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
    # list_status_detailed() refines "active" via _pane_display_state (#248/#247)
    _pane_display_state = Orchestrator._pane_display_state
    # list_status_detailed calls these (#163 pending-notice surfacing) —
    # bound so the fake stays a thin real-method wrapper instead of
    # duplicating the merge logic.
    _pending_notice_roles = Orchestrator._pending_notice_roles
    _has_pending_lead_notice = Orchestrator._has_pending_lead_notice
    # list_status_detailed also surfaces resource-governor-queued roles
    # (#240 point 3) — no `_resource_governor` attr on this fake, so
    # `_queued_resource_roles` safely no-ops via its `getattr(..., None)` guard.
    _queued_resource_roles = Orchestrator._queued_resource_roles


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

    def test_content_change_ts_wins_over_stale_transcript_mtime(
        self, runtime_tmp: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """#236: raw transcript mtime bumps on every PTY byte including an
        animated spinner or a permission-dialog redraw, so a pane wedged on
        either kept reading "last progress: 0s ago" forever — stall
        detection could never fire. `last_content_change_ts` (the same
        spinner-filtered hash clock `_check_stuck_panes` already maintains)
        must win over a freshly-bumped-but-spinner-only transcript mtime."""
        import os

        transcript = tmp_path / "backend-120000.transcript.log"
        transcript.write_bytes(b"spinner frame")
        # Transcript mtime is "now" (spinner redraw just happened)...
        now = time.time()
        os.utime(transcript, (now, now))
        orch = self._make_orch()
        pane = _FakePane(transcript_path=str(transcript))
        # ...but the content-delta clock says content actually froze 10
        # minutes ago (the genuinely last real progress).
        stale_content_ts = now - 600
        orch._ps("default::backend").last_content_change_ts = stale_content_ts
        ts = orch._compute_last_progress_ts("backend", "default", pane)
        assert ts == pytest.approx(stale_content_ts, abs=1)

    def test_content_change_ts_used_even_without_transcript_path(
        self, runtime_tmp: pathlib.Path
    ) -> None:
        orch = self._make_orch()
        pane = _FakePane(transcript_path=None)
        recent = time.time() - 3
        orch._ps("default::backend").last_content_change_ts = recent
        ts = orch._compute_last_progress_ts("backend", "default", pane)
        assert ts == pytest.approx(recent, abs=1)

    def test_falls_back_to_transcript_mtime_before_first_watchdog_tick(
        self, runtime_tmp: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """A freshly-assigned pane has no `last_content_change_ts` yet (the
        5s watchdog tick hasn't run) — must still fall back to transcript
        mtime rather than reading as 0.0/no-progress."""
        transcript = tmp_path / "backend-120000.transcript.log"
        transcript.write_bytes(b"output")
        orch = self._make_orch()
        pane = _FakePane(transcript_path=str(transcript))
        ts = orch._compute_last_progress_ts("backend", "default", pane)
        assert ts == pytest.approx(transcript.stat().st_mtime, abs=1)

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

    def test_blocked_reason_none_for_ordinary_working_pane(self, runtime_tmp: pathlib.Path) -> None:
        pane = _FakePane(state="working", transcript_path=None)
        pane.session.is_at_trust_prompt.return_value = False
        pane.session.is_blocked_on_permission_prompt.return_value = None
        pane.session.is_blocked_on_tty_prompt.return_value = None
        orch = self._setup_orch(pane)
        result = orch.list_status_detailed("default")
        assert result["qa"]["blocked_reason"] is None

    def test_blocked_reason_surfaces_permission_prompt(self, runtime_tmp: pathlib.Path) -> None:
        """#236: a `working` pane wedged on Claude Code's own tool-permission
        dialog must report `blocked_reason: "permission"` instead of reading
        as ordinary `working` with no distinguishing signal."""
        pane = _FakePane(state="working", transcript_path=None)
        pane.session.is_at_trust_prompt.return_value = False
        pane.session.is_blocked_on_permission_prompt.return_value = "1. Yes"
        orch = self._setup_orch(pane)
        result = orch.list_status_detailed("default")
        assert result["qa"]["blocked_reason"] == "permission"

    def test_blocked_reason_surfaces_permission_prompt_from_real_capture(
        self, runtime_tmp: pathlib.Path
    ) -> None:
        """#236 end-to-end: the test above mocks `is_blocked_on_permission_prompt`
        directly, which only proves list_status_detailed() *wires up* the
        return value — it never exercises the actual screen-parsing regex.
        This drives a real `PtySession` (fed the verbatim `git reset --hard`
        permission-dialog byte capture — see
        `TestIsBlockedOnPermissionPrompt.test_real_capture_git_reset_hard_dialog_detected`
        in test_pty_ready_prompt.py for the byte-for-byte provenance) through
        the actual `takkub status` data path, proving detection really does
        reach the dict `cli._print_status_report` renders — not just that a
        standalone function exists and returns something when hand-fed."""
        from agent_takkub.pty_session import PtySession

        raw = (
            b"Permission rule \x1b[1mBash(git reset --hard:*)\x1b[43G\x1b[22mrequires"
            b"\x1b[52Gconfirmation\x1b[65Gfor\x1b[69Gthis\x1b[74Gcommand.\r\x1b[1C\x1b[1B"
            b"\x1b[38;2;153;153;153m/perm\x1b[8Gssi\x1b[12Gns to update rules\r\x1b[2B"
            b"\x1b[39m Do you want to proceed?\x1b[K\r\x1b[1C\x1b[1B"
            b"\x1b[38;2;177;185;249m\xe2\x9d\xaf\x1b[4G\x1b[38;2;153;153;153m1. "
            b"\x1b[38;2;177;185;249mYes\r\x1b[1B\x1b[39m   \x1b[38;2;153;153;153m2. "
            b"\x1b[39mYes, and don\xe2\x80\x99t ask again for: rtk git *\x1b[K\r\x1b[1B  "
            b"\x1b[4G\x1b[38;2;153;153;153m3. \x1b[39mNo\r\x1b[1B\x1b[K\r\x1b[1C\x1b[1B"
            b"\x1b[38;2;153;153;153mEsc to cancel \xc2\xb7 Tab to amend \xc2\xb7 "
            b"ctrl+e to explain\x1b[39m\x1b[K\x1b[36;1H\x1b[32;2H\x1b[H\r\x1b[6B"
            b"\x1b[38;2;153;153;153m\xe2\x97\x8f\x1b[39m\x1b[36;1H\x1b[32;2H\x1b[H\r\x1b[6B"
            b"\x1b[38;2;153;153;153m \x1b[39m"
        )
        real_session = PtySession(cols=110, rows=36)
        real_session._feed_and_log(raw)
        real_session._alive = True  # bare-constructed session defaults to not-alive

        pane = _FakePane(state="working", transcript_path=None)
        pane.session = real_session  # replace the MagicMock stub with the real thing
        orch = self._setup_orch(pane, role="backend")

        result = orch.list_status_detailed("default")

        # The bug this guards: before #236, a pane wedged here reported
        # ordinary "working" with no distinguishing signal — a Lead reading
        # `takkub status` (which renders exactly this dict) had nothing
        # telling them the pane needed a keypress, not more wait time.
        assert result["backend"]["state"] == "working"
        assert result["backend"]["blocked_reason"] == "permission"

    def test_blocked_reason_none_for_non_working_state(self, runtime_tmp: pathlib.Path) -> None:
        pane = _FakePane(state="active", transcript_path=None)
        pane.session.is_at_trust_prompt.return_value = True  # would match if checked
        orch = self._setup_orch(pane)
        result = orch.list_status_detailed("default")
        assert result["qa"]["blocked_reason"] is None


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

    def test_transcript_tail_strips_private_mode_csi(
        self, runtime_tmp: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """#145: `\\x1b[?25h`/`\\x1b[?25l` (cursor show/hide) weren't stripped —
        the '?' private-mode parameter byte wasn't in the old char class."""
        transcript = tmp_path / "backend.transcript.log"
        transcript.write_text("\x1b[?25lworking\x1b[?25hplain line\n", encoding="utf-8")
        orch = _FakeOrch()
        pane = _FakePane(state="working", transcript_path=str(transcript))
        orch._panes_by_project["default"] = {"backend": pane}
        report = orch.pane_status_report("default")
        tail = report["panes"]["backend"]["transcript_tail"]
        assert "\x1b[" not in tail
        assert "working" in tail
        assert "plain line" in tail

    def test_transcript_tail_strips_cha_final_byte(
        self, runtime_tmp: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """#145: `\\x1b[3G` (CHA — cursor horizontal absolute) has a final
        byte 'G' that wasn't in the old allowlist `[mABCDHJKSThlsu]`."""
        transcript = tmp_path / "backend.transcript.log"
        transcript.write_text("\x1b[3Gplain line\n", encoding="utf-8")
        orch = _FakeOrch()
        pane = _FakePane(state="working", transcript_path=str(transcript))
        orch._panes_by_project["default"] = {"backend": pane}
        report = orch.pane_status_report("default")
        tail = report["panes"]["backend"]["transcript_tail"]
        assert "\x1b[" not in tail
        assert "plain line" in tail

    def test_transcript_tail_strips_osc_sequence(
        self, runtime_tmp: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """#145: OSC window-title sequences (`\\x1b]0;...\\x07`) are a
        different escape family the old CSI-only regex never matched."""
        transcript = tmp_path / "backend.transcript.log"
        transcript.write_text(
            "\x1b]0;my title\x07plain line\n\x1b]0;other\x1b\\second line\n",
            encoding="utf-8",
        )
        orch = _FakeOrch()
        pane = _FakePane(state="working", transcript_path=str(transcript))
        orch._panes_by_project["default"] = {"backend": pane}
        report = orch.pane_status_report("default")
        tail = report["panes"]["backend"]["transcript_tail"]
        assert "\x1b]" not in tail
        assert "plain line" in tail
        assert "second line" in tail

    def test_transcript_tail_leaves_plain_text_untouched(
        self, runtime_tmp: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """Plain text with no escape sequences must pass through unchanged
        (guards against an over-broad stripper eating real content)."""
        transcript = tmp_path / "backend.transcript.log"
        transcript.write_text(
            "just some [bracketed] text and a ? question mark\n", encoding="utf-8"
        )
        orch = _FakeOrch()
        pane = _FakePane(state="working", transcript_path=str(transcript))
        orch._panes_by_project["default"] = {"backend": pane}
        report = orch.pane_status_report("default")
        tail = report["panes"]["backend"]["transcript_tail"]
        assert tail == "just some [bracketed] text and a ? question mark"

    def test_transcript_tail_strips_issue_173_repro(
        self, runtime_tmp: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """#173 (duplicate of #145, migrated from local tracker before the
        #145 fix landed same-day): exact repro from the report — cursor
        show/hide toggles immediately followed by a CHA spinner line
        ('[?25h[?25l' / '[3GPondering…') must not leak into `takkub status`
        output. Already fixed by commit 8ad72e1f; this locks the regression."""
        transcript = tmp_path / "backend.transcript.log"
        transcript.write_text("\x1b[?25h\x1b[?25l\x1b[3GPondering…\n", encoding="utf-8")
        orch = _FakeOrch()
        pane = _FakePane(state="working", transcript_path=str(transcript))
        orch._panes_by_project["default"] = {"backend": pane}
        report = orch.pane_status_report("default")
        tail = report["panes"]["backend"]["transcript_tail"]
        assert "\x1b" not in tail
        assert "Pondering" in tail

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
