"""Tests for the opt-in --requires-commit done gate.

The gate runs inside orchestrator.done() when assign() was called with
requires_commit=True. It shells out to `git status --porcelain` in the
pane's cwd; if the output is non-empty (working tree dirty) done is
rejected and an error message is written into the pane session.

Six scenarios:
  1. No flag → done on dirty tree → OK
  2. Flag set → done on dirty tree → rejected, error injected into pane
  3. Flag set → done on clean tree → OK
  4. Flag set → done rejected on dirty → commit (simulate clean) → done → OK
  5. close() → flag cleared (no stale gate on next assign)
  6. Two panes (one flagged, one not) → gate applies only to the flagged one
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import Orchestrator, _exit_key

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


def _dirty_result(files: str = "M src/foo.py\n") -> MagicMock:
    r = MagicMock()
    r.stdout = files
    return r


def _clean_result() -> MagicMock:
    r = MagicMock()
    r.stdout = ""
    return r


class TestDoneGate:
    def test_done_no_flag_allows_dirty(self, orch: Orchestrator) -> None:
        """assign without flag → done on dirty tree → allowed."""
        pane = _make_working_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane

        # assign WITHOUT requires_commit
        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready"),
        ):
            orch.assign(
                "backend", cwd="/repo", task="do work", requires_commit=False, project=TEST_PROJECT
            )

        with patch("agent_takkub.orchestrator.subprocess.run", return_value=_dirty_result()):
            ok, msg = orch.done("backend", note="done", project=TEST_PROJECT)

        assert ok is True
        assert "rejected" not in msg

    def test_done_with_flag_rejects_dirty(self, orch: Orchestrator) -> None:
        """assign with requires_commit=True → done on dirty tree → rejected."""
        pane = _make_working_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["frontend"] = pane

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

        assert ok is False
        assert "dirty" in msg
        # error message was written into the pane session
        pane.session.write.assert_called()
        written = pane.session.write.call_args_list
        injected = b"".join(
            (a[0] if isinstance(a[0], bytes) else a[0].encode("utf-8"))
            for call in written
            for a in [call.args]
            if a
        )
        assert (
            b"rejected" in injected
            or b"done rejected" in injected.lower()
            or b"clean" in injected.lower()
        )

    def test_done_with_flag_allows_clean(self, orch: Orchestrator) -> None:
        """assign with requires_commit=True → done on clean tree → allowed."""
        pane = _make_working_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["mobile"] = pane

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

    def test_done_with_flag_then_commit_then_retry(self, orch: Orchestrator) -> None:
        """First done rejected (dirty) → simulate commit → second done passes."""
        pane = _make_working_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["devops"] = pane

        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready"),
        ):
            orch.assign(
                "devops", cwd="/repo", task="deploy", requires_commit=True, project=TEST_PROJECT
            )

        # First attempt: dirty
        with patch("agent_takkub.orchestrator.subprocess.run", return_value=_dirty_result()):
            ok1, _ = orch.done("devops", note="done", project=TEST_PROJECT)
        assert ok1 is False

        # Flag must still be set so the gate still applies on retry
        ekey = _exit_key(TEST_PROJECT, "devops")
        assert orch._requires_commit_on_done.get(ekey) is True

        # Second attempt: clean (agent committed)
        with patch("agent_takkub.orchestrator.subprocess.run", return_value=_clean_result()):
            ok2, msg2 = orch.done("devops", note="done", project=TEST_PROJECT)
        assert ok2 is True
        assert "rejected" not in msg2
        # Flag cleared after successful done
        assert ekey not in orch._requires_commit_on_done

    def test_close_clears_requires_commit_flag(self, orch: Orchestrator) -> None:
        """close() must pop the flag so a new assign doesn't inherit stale gate."""
        pane = _make_working_pane()
        pane.mark_expected_exit = MagicMock()
        pane.session.terminate = MagicMock()
        pane.set_state = MagicMock()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["qa"] = pane

        ekey = _exit_key(TEST_PROJECT, "qa")
        orch._requires_commit_on_done[ekey] = True

        orch.close("qa", project=TEST_PROJECT)

        assert ekey not in orch._requires_commit_on_done

    def test_requires_commit_isolated_per_pane(self, orch: Orchestrator) -> None:
        """Flag on 'reviewer' must not affect 'designer' (no flag)."""
        pane_reviewer = _make_working_pane()
        pane_designer = _make_working_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["reviewer"] = pane_reviewer
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["designer"] = pane_designer

        ekey_reviewer = _exit_key(TEST_PROJECT, "reviewer")

        # Only reviewer gets the flag
        orch._requires_commit_on_done[ekey_reviewer] = True

        # designer done on dirty tree → allowed (no flag)
        with patch("agent_takkub.orchestrator.subprocess.run", return_value=_dirty_result()):
            ok_designer, _ = orch.done("designer", note="done", project=TEST_PROJECT)
        assert ok_designer is True

        # reviewer done on dirty tree → rejected (flag set)
        with patch("agent_takkub.orchestrator.subprocess.run", return_value=_dirty_result()):
            ok_reviewer, msg_reviewer = orch.done("reviewer", note="done", project=TEST_PROJECT)
        assert ok_reviewer is False
        assert "dirty" in msg_reviewer
