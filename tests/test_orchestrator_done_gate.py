"""Tests for the opt-in --requires-commit done handoff signal.

The handoff runs inside orchestrator.done() when assign() was called with
requires_commit=True. It shells out to `git status --porcelain` in the pane's
cwd; if the output is non-empty (working tree dirty) done still SUCCEEDS but
the Lead notice is augmented with an uncommitted-changes warning so Lead can
review and commit. Teammate ไม่ต้อง commit เอง.

Eight scenarios:
  1. No flag → done on dirty tree → OK, no warning
  2. Flag set → done on dirty tree → OK (passes), Lead notice contains warning
  3. Flag set → done on clean tree → OK, no warning in notice
  4. Flag set → done on dirty tree → flag cleared after done (single-use)
  5. close() → flag cleared (no stale gate on next assign)
  6. Two panes (one flagged, one not) → warning only on flagged one
  7. Flag set → done on dirty → Lead notice body contains files preview
  8. (#19) git unavailable → done still returns ok + done_commit_gate_skipped logged
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator, PaneState, _exit_key

TEST_PROJECT = "testproj"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda project: project or TEST_PROJECT),
    )
    o = Orchestrator()
    o._idle_watchdog.stop()
    return o


def _make_working_pane(cwd: str = "/repo") -> MagicMock:
    pane = MagicMock()
    pane.state = "working"
    pane.session = MagicMock()
    pane.session.is_alive = True
    pane.session.is_at_ready_prompt.return_value = True
    pane._session_cwd = cwd
    pane._transcript_path = None
    return pane


def _make_lead_pane() -> MagicMock:
    pane = MagicMock()
    pane.session = MagicMock()
    pane.session.is_alive = True
    return pane


def _dirty_result(files: str = "M src/foo.py\n") -> MagicMock:
    r = MagicMock()
    r.stdout = files
    return r


def _clean_result() -> MagicMock:
    r = MagicMock()
    r.stdout = ""
    return r


def _written_str(mock_session: MagicMock) -> str:
    """Collect all string args written to a session mock into one string."""
    parts: list[str] = []
    for c in mock_session.write.call_args_list:
        arg = c.args[0] if c.args else ""
        if isinstance(arg, bytes):
            parts.append(arg.decode("utf-8", errors="replace"))
        else:
            parts.append(str(arg))
    return "".join(parts)


class TestDoneGate:
    def test_done_no_flag_allows_dirty(self, orch: Orchestrator) -> None:
        """assign without flag → done on dirty tree → allowed, no warning."""
        pane = _make_working_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane

        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready"),
        ):
            orch.assign(
                "backend", cwd="/repo", task="do work", requires_commit=False, project=TEST_PROJECT
            )

        lead = _make_lead_pane()
        orch._panes_by_project[TEST_PROJECT]["lead"] = lead

        with patch("agent_takkub.orchestrator.subprocess.run", return_value=_dirty_result()):
            ok, msg = orch.done("backend", note="done", project=TEST_PROJECT)

        assert ok is True
        assert "rejected" not in msg
        assert "requires-commit" not in _written_str(lead.session)

    def test_done_with_flag_dirty_passes_with_warning(self, orch: Orchestrator) -> None:
        """assign with requires_commit=True → done on dirty tree → OK, Lead notice has warning."""
        pane = _make_working_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["frontend"] = pane
        lead = _make_lead_pane()
        orch._panes_by_project[TEST_PROJECT]["lead"] = lead

        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready"),
        ):
            orch.assign(
                "frontend", cwd="/repo", task="do work", requires_commit=True, project=TEST_PROJECT
            )

        with patch(
            "agent_takkub.orchestrator.subprocess.run",
            return_value=_dirty_result("M src/app.tsx\n"),
        ):
            ok, msg = orch.done("frontend", note="done", project=TEST_PROJECT)

        assert ok is True
        assert "rejected" not in msg
        injected = _written_str(lead.session)
        assert "requires-commit" in injected
        assert "uncommitted" in injected

    def test_done_with_flag_allows_clean(self, orch: Orchestrator) -> None:
        """assign with requires_commit=True → done on clean tree → OK, no warning."""
        pane = _make_working_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["mobile"] = pane
        lead = _make_lead_pane()
        orch._panes_by_project[TEST_PROJECT]["lead"] = lead

        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready"),
        ):
            orch.assign(
                "mobile", cwd="/repo", task="do work", requires_commit=True, project=TEST_PROJECT
            )

        with patch("agent_takkub.orchestrator.subprocess.run", return_value=_clean_result()):
            ok, msg = orch.done("mobile", note="shipped", project=TEST_PROJECT)

        assert ok is True
        assert "rejected" not in msg
        assert "requires-commit" not in _written_str(lead.session)

    def test_done_with_flag_dirty_clears_flag(self, orch: Orchestrator) -> None:
        """Flag is cleared after done succeeds (even on dirty tree)."""
        pane = _make_working_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["devops"] = pane

        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready"),
        ):
            orch.assign(
                "devops", cwd="/repo", task="deploy", requires_commit=True, project=TEST_PROJECT
            )

        ekey = _exit_key(TEST_PROJECT, "devops")
        assert (orch._pane_state.get(ekey) or PaneState()).requires_commit_on_done is True

        with patch("agent_takkub.orchestrator.subprocess.run", return_value=_dirty_result()):
            ok, _ = orch.done("devops", note="done", project=TEST_PROJECT)

        assert ok is True
        assert not (orch._pane_state.get(ekey) or PaneState()).requires_commit_on_done

    def test_close_clears_requires_commit_flag(self, orch: Orchestrator) -> None:
        """close() must pop the flag so a new assign doesn't inherit stale gate."""
        pane = _make_working_pane()
        pane.mark_expected_exit = MagicMock()
        pane.session.terminate = MagicMock()
        pane.set_state = MagicMock()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["qa"] = pane

        ekey = _exit_key(TEST_PROJECT, "qa")
        orch._ps(ekey).requires_commit_on_done = True

        orch.close("qa", project=TEST_PROJECT)

        assert orch._pane_state.get(ekey) is None

    def test_requires_commit_isolated_per_pane(self, orch: Orchestrator) -> None:
        """Flag on 'reviewer' must not affect 'designer' (no flag)."""
        pane_reviewer = _make_working_pane()
        pane_designer = _make_working_pane()
        lead = _make_lead_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["reviewer"] = pane_reviewer
        orch._panes_by_project[TEST_PROJECT]["designer"] = pane_designer
        orch._panes_by_project[TEST_PROJECT]["lead"] = lead

        ekey_reviewer = _exit_key(TEST_PROJECT, "reviewer")

        # Only reviewer gets the flag
        orch._ps(ekey_reviewer).requires_commit_on_done = True

        # designer done on dirty tree → allowed, no warning
        with patch("agent_takkub.orchestrator.subprocess.run", return_value=_dirty_result()):
            ok_designer, _ = orch.done("designer", note="done", project=TEST_PROJECT)
        assert ok_designer is True

        lead_text_after_designer = _written_str(lead.session)
        assert "requires-commit" not in lead_text_after_designer

        # reviewer done on dirty tree → also passes, but Lead notice has warning
        with patch("agent_takkub.orchestrator.subprocess.run", return_value=_dirty_result()):
            ok_reviewer, msg_reviewer = orch.done("reviewer", note="done", project=TEST_PROJECT)
        assert ok_reviewer is True
        assert "rejected" not in msg_reviewer

        lead_text_after_reviewer = _written_str(lead.session)
        assert "requires-commit" in lead_text_after_reviewer

    def test_done_with_flag_dirty_notice_contains_files_preview(self, orch: Orchestrator) -> None:
        """Lead notice must include the dirty-files preview (up to 200 chars)."""
        pane = _make_working_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["qa"] = pane
        lead = _make_lead_pane()
        orch._panes_by_project[TEST_PROJECT]["lead"] = lead

        ekey = _exit_key(TEST_PROJECT, "qa")
        orch._ps(ekey).requires_commit_on_done = True

        dirty_files = "M src/api.py\nA tests/test_api.py\n"
        with patch(
            "agent_takkub.orchestrator.subprocess.run",
            return_value=_dirty_result(dirty_files),
        ):
            ok, _ = orch.done("qa", note="done", project=TEST_PROJECT)

        assert ok is True
        injected = _written_str(lead.session)
        assert "src/api.py" in injected
        assert "tests/test_api.py" in injected


class TestDoneCommitGateSkipped:
    """#19 — gate logs done_commit_gate_skipped when git is unavailable."""

    def test_git_unavailable_done_still_ok(self, orch: Orchestrator, tmp_path, monkeypatch) -> None:
        """done() returns ok even when subprocess.run raises (git not available)."""
        monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")

        pane = _make_working_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane
        lead = _make_lead_pane()
        orch._panes_by_project[TEST_PROJECT]["lead"] = lead

        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready"),
        ):
            orch.assign(
                "backend", cwd="/repo", task="do work", requires_commit=True, project=TEST_PROJECT
            )

        with patch(
            "agent_takkub.orchestrator.subprocess.run",
            side_effect=FileNotFoundError("git not found"),
        ):
            ok, msg = orch.done("backend", note="done", project=TEST_PROJECT)

        assert ok is True
        assert "rejected" not in msg

    def test_git_unavailable_logs_gate_skipped_event(
        self, orch: Orchestrator, tmp_path, monkeypatch
    ) -> None:
        """done_commit_gate_skipped event is written to events.log when git raises."""
        log_path = tmp_path / "events.log"
        monkeypatch.setattr(orch_mod, "EVENTS_LOG", log_path)

        pane = _make_working_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["frontend"] = pane
        lead = _make_lead_pane()
        orch._panes_by_project[TEST_PROJECT]["lead"] = lead

        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready"),
        ):
            orch.assign(
                "frontend", cwd="/repo", task="build UI", requires_commit=True, project=TEST_PROJECT
            )

        with patch(
            "agent_takkub.orchestrator.subprocess.run",
            side_effect=OSError("no git"),
        ):
            orch.done("frontend", note="done", project=TEST_PROJECT)

        events = [
            json.loads(ln) for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()
        ]
        skipped = [e for e in events if e["event"] == "done_commit_gate_skipped"]
        assert len(skipped) == 1
        assert skipped[0]["role"] == "frontend"
        assert skipped[0]["project"] == TEST_PROJECT
        assert "reason" in skipped[0]
