"""Tests for the opt-in --requires-commit done handoff signal.

The handoff runs inside orchestrator.done() when assign() was called with
requires_commit=True. M2: the dirty-tree check now runs ASYNCHRONOUSLY via
QProcess so a slow/large repo can't freeze the Qt main thread. done() returns
immediately and, if the working tree turns out dirty, a follow-up
`[requires-commit]` warning is delivered to Lead.

Split of concerns now:
  - `_uncommitted_warning(role, porcelain)` — pure: dirty → warning string
    (with files preview), clean/blank → None.
  - `_check_uncommitted_async(project, role, cwd)` — fires the git QProcess and
    delivers the warning; done() calls it iff the flag is set.
  - done() itself no longer blocks on git and no longer inlines the warning.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

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


def _written_str(mock_session: MagicMock) -> str:
    parts: list[str] = []
    for c in mock_session.write.call_args_list:
        arg = c.args[0] if c.args else ""
        parts.append(arg.decode("utf-8", "replace") if isinstance(arg, bytes) else str(arg))
    return "".join(parts)


# ──────────────────────────────────────────────────────────────────────
# Pure: _uncommitted_warning
# ──────────────────────────────────────────────────────────────────────


class TestUncommittedWarning:
    def test_dirty_returns_warning_with_preview(self) -> None:
        out = Orchestrator._uncommitted_warning("frontend", "M src/app.tsx\nA test.tsx\n")
        assert out is not None
        assert "requires-commit" in out
        assert "uncommitted" in out
        assert "frontend" in out
        assert "src/app.tsx" in out

    def test_clean_returns_none(self) -> None:
        assert Orchestrator._uncommitted_warning("qa", "") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert Orchestrator._uncommitted_warning("qa", "   \n  \n") is None

    def test_preview_capped_at_200(self) -> None:
        big = "M " + ("x" * 5000) + "\n"
        out = Orchestrator._uncommitted_warning("qa", big)
        assert out is not None
        # only the 200-char preview of the porcelain output is embedded ("M " + 198 x)
        assert "x" * 190 in out
        assert "x" * 300 not in out


# ──────────────────────────────────────────────────────────────────────
# done() wires the async check by flag
# ──────────────────────────────────────────────────────────────────────


class TestDoneGateAsync:
    def _assign_with_flag(self, orch: Orchestrator, role: str, flag: bool) -> None:
        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready"),
        ):
            orch.assign(
                role, cwd="/repo", task="do work", requires_commit=flag, project=TEST_PROJECT
            )

    def test_no_flag_skips_async_check(self, orch: Orchestrator) -> None:
        pane = _make_working_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane
        orch._panes_by_project[TEST_PROJECT]["lead"] = _make_lead_pane()
        self._assign_with_flag(orch, "backend", flag=False)

        with patch.object(orch, "_check_uncommitted_async") as chk:
            ok, _ = orch.done("backend", note="done", project=TEST_PROJECT)
        assert ok is True
        chk.assert_not_called()

    def test_flag_fires_async_check_with_cwd(self, orch: Orchestrator) -> None:
        pane = _make_working_pane(cwd="/repo")
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["frontend"] = pane
        orch._panes_by_project[TEST_PROJECT]["lead"] = _make_lead_pane()
        self._assign_with_flag(orch, "frontend", flag=True)

        with patch.object(orch, "_check_uncommitted_async") as chk:
            ok, msg = orch.done("frontend", note="done", project=TEST_PROJECT)
        assert ok is True
        assert "rejected" not in msg
        chk.assert_called_once_with(TEST_PROJECT, "frontend", "/repo")

    def test_main_notice_has_no_inline_warning(self, orch: Orchestrator) -> None:
        # The immediate done notice never carries the warning now — it arrives as
        # a follow-up from the async check.
        pane = _make_working_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["frontend"] = pane
        lead = _make_lead_pane()
        orch._panes_by_project[TEST_PROJECT]["lead"] = lead
        self._assign_with_flag(orch, "frontend", flag=True)

        with patch.object(orch, "_check_uncommitted_async"):
            orch.done("frontend", note="done", project=TEST_PROJECT)
        assert "requires-commit" not in _written_str(lead.session)

    def test_async_check_only_for_flagged_pane(self, orch: Orchestrator) -> None:
        reviewer = _make_working_pane()
        designer = _make_working_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["reviewer"] = reviewer
        orch._panes_by_project[TEST_PROJECT]["designer"] = designer
        orch._panes_by_project[TEST_PROJECT]["lead"] = _make_lead_pane()
        orch._ps(_exit_key(TEST_PROJECT, "reviewer")).requires_commit_on_done = True

        with patch.object(orch, "_check_uncommitted_async") as chk:
            orch.done("designer", note="done", project=TEST_PROJECT)
            chk.assert_not_called()
            orch.done("reviewer", note="done", project=TEST_PROJECT)
            chk.assert_called_once()
            assert chk.call_args.args[1] == "reviewer"


# ──────────────────────────────────────────────────────────────────────
# Flag lifecycle (independent of the git check)
# ──────────────────────────────────────────────────────────────────────


class TestRequiresCommitFlagLifecycle:
    def test_done_clears_flag(self, orch: Orchestrator) -> None:
        pane = _make_working_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["devops"] = pane
        ekey = _exit_key(TEST_PROJECT, "devops")
        orch._ps(ekey).requires_commit_on_done = True

        with patch.object(orch, "_check_uncommitted_async"):
            ok, _ = orch.done("devops", note="done", project=TEST_PROJECT)
        assert ok is True
        assert not (orch._pane_state.get(ekey) or PaneState()).requires_commit_on_done

    def test_close_clears_requires_commit_flag(self, orch: Orchestrator) -> None:
        pane = _make_working_pane()
        pane.mark_expected_exit = MagicMock()
        pane.session.terminate = MagicMock()
        pane.set_state = MagicMock()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["qa"] = pane
        ekey = _exit_key(TEST_PROJECT, "qa")
        orch._ps(ekey).requires_commit_on_done = True

        orch.close("qa", project=TEST_PROJECT)
        assert orch._pane_state.get(ekey) is None
