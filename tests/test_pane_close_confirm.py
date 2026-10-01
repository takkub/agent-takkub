"""Confirm-before-close gate for a USER-initiated pane × click.

`Orchestrator.confirm_manual_pane_close()` is the single dialog point shared
by both manual close entry points (pane-header × → `_on_pane_close_clicked`,
tab-bar × → `main_window._on_tab_pane_close_requested`). It must NEVER be
reached by an automated close path — `close()`/`close_all_teammates()` stay
untouched so CLI `takkub close`, done-report auto-close, close-all, and
shutdown never block on a click that will never come.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtWidgets import QApplication, QMessageBox

from agent_takkub.orchestrator import Orchestrator, PaneState
from agent_takkub.roles import LEAD

TEST_PROJECT = "testproj"


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    return app


@pytest.fixture
def orch(qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda project: project or TEST_PROJECT),
    )
    o = Orchestrator()
    o.shutdown_timers()
    return o


def _make_pane(state: str = "active") -> MagicMock:
    pane = MagicMock()
    pane.role.name = "frontend"
    pane.role.label = "Frontend"
    pane.state = state
    return pane


def _mock_box(exec_return):
    box = MagicMock()
    box.exec.return_value = exec_return
    return box


class TestConfirmManualPaneClose:
    def test_lead_replace_requires_confirmation(self, orch: Orchestrator) -> None:
        with patch(
            "agent_takkub.cockpit_theme.themed_message_box",
            return_value=_mock_box(QMessageBox.StandardButton.Ok),
        ) as mocked:
            result = orch.confirm_manual_pane_close(None, LEAD.name, TEST_PROJECT)
        assert result is True
        text = mocked.return_value.setText.call_args.args[0]
        assert "เปลี่ยน Lead" in text
        assert "pane อื่น" in text

    def test_ok_returns_true(self, orch: Orchestrator) -> None:
        pane = _make_pane("active")
        with patch(
            "agent_takkub.cockpit_theme.themed_message_box",
            return_value=_mock_box(QMessageBox.StandardButton.Ok),
        ):
            assert orch.confirm_manual_pane_close(pane, "frontend", TEST_PROJECT) is True

    def test_cancel_returns_false(self, orch: Orchestrator) -> None:
        pane = _make_pane("active")
        with patch(
            "agent_takkub.cockpit_theme.themed_message_box",
            return_value=_mock_box(QMessageBox.StandardButton.Cancel),
        ):
            assert orch.confirm_manual_pane_close(pane, "frontend", TEST_PROJECT) is False

    def test_default_button_is_cancel(self, orch: Orchestrator) -> None:
        """Enter on the dialog must not close the pane (spec item 4)."""
        pane = _make_pane("active")
        box = _mock_box(QMessageBox.StandardButton.Ok)
        with patch("agent_takkub.cockpit_theme.themed_message_box", return_value=box):
            orch.confirm_manual_pane_close(pane, "frontend", TEST_PROJECT)
        box.setDefaultButton.assert_called_once_with(QMessageBox.StandardButton.Cancel)

    def test_working_pane_gets_stronger_wording(self, orch: Orchestrator) -> None:
        idle_box = _mock_box(QMessageBox.StandardButton.Ok)
        working_box = _mock_box(QMessageBox.StandardButton.Ok)

        with patch("agent_takkub.cockpit_theme.themed_message_box", return_value=idle_box):
            orch.confirm_manual_pane_close(_make_pane("active"), "frontend", TEST_PROJECT)
        with patch("agent_takkub.cockpit_theme.themed_message_box", return_value=working_box):
            orch.confirm_manual_pane_close(_make_pane("working"), "frontend", TEST_PROJECT)

        idle_text = idle_box.setText.call_args.args[0]
        working_text = working_box.setText.call_args.args[0]
        assert idle_text != working_text
        assert "กำลังทำงานอยู่" in working_text
        assert "กำลังทำงานอยู่" not in idle_text

    def test_dirty_worktree_warns_with_file_count(self, orch: Orchestrator) -> None:
        key = f"{TEST_PROJECT}::frontend"
        ps = PaneState()
        ps.worktree = {
            "path": "/repo/wt/frontend",
            "branch": "wt/frontend-1",
            "git_root": "/repo",
            "base_sha": "abc123",
        }
        orch._pane_state[key] = ps

        box = _mock_box(QMessageBox.StandardButton.Ok)
        fake_mgr = MagicMock()
        fake_mgr.real_dirty.return_value = True
        fake_mgr.real_uncommitted_count.return_value = 3
        fake_mgr.commit_count.return_value = 0

        with (
            patch("agent_takkub.cockpit_theme.themed_message_box", return_value=box),
            patch("agent_takkub.worktree_manager.WorktreeManager", return_value=fake_mgr),
        ):
            orch.confirm_manual_pane_close(_make_pane("active"), "frontend", TEST_PROJECT)

        text = box.setText.call_args.args[0]
        assert "3 ไฟล์ที่ยังไม่ commit" in text
        assert "wt/frontend-1" in text

    def test_clean_worktree_no_extra_warning(self, orch: Orchestrator) -> None:
        key = f"{TEST_PROJECT}::frontend"
        ps = PaneState()
        ps.worktree = {
            "path": "/repo/wt/frontend",
            "branch": "wt/frontend-1",
            "git_root": "/repo",
            "base_sha": "abc123",
        }
        orch._pane_state[key] = ps

        box = _mock_box(QMessageBox.StandardButton.Ok)
        fake_mgr = MagicMock()
        fake_mgr.real_dirty.return_value = False
        fake_mgr.commit_count.return_value = 0

        with (
            patch("agent_takkub.cockpit_theme.themed_message_box", return_value=box),
            patch("agent_takkub.worktree_manager.WorktreeManager", return_value=fake_mgr),
        ):
            orch.confirm_manual_pane_close(_make_pane("active"), "frontend", TEST_PROJECT)

        text = box.setText.call_args.args[0]
        assert "ยังไม่ commit" not in text


class TestOnPaneCloseClickedGate:
    """Pane-header × (AgentPane's own close button)."""

    def test_cancelled_confirm_skips_close(self, orch: Orchestrator) -> None:
        with (
            patch.object(orch, "confirm_manual_pane_close", return_value=False) as mocked_confirm,
            patch.object(orch, "close") as mocked_close,
        ):
            orch._on_pane_close_clicked("frontend")
        mocked_confirm.assert_called_once()
        mocked_close.assert_not_called()

    def test_confirmed_proceeds_to_close(self, orch: Orchestrator) -> None:
        with (
            patch.object(orch, "confirm_manual_pane_close", return_value=True),
            patch.object(orch, "close") as mocked_close,
        ):
            orch._on_pane_close_clicked("frontend")
        mocked_close.assert_called_once_with("frontend")

    def test_lead_click_replaces_only_lead(self, orch: Orchestrator) -> None:
        lead = _make_pane("working")
        lead.role.name = "lead"
        lead.model.provider_name = "claude"
        lead.session.rate_limit_reset_at.return_value = None
        teammate = _make_pane("working")
        orch._project_panes(TEST_PROJECT).update({"lead": lead, "backend": teammate})
        with (
            patch.object(orch, "confirm_manual_pane_close", return_value=True),
            patch("agent_takkub.provider_config.pick_substitute_provider", return_value="codex"),
            patch.object(orch, "_reroute_pane_to_provider") as replace,
            patch.object(orch, "close") as close,
        ):
            orch._on_pane_close_clicked("lead")
        replace.assert_called_once()
        assert replace.call_args.args[:2] == (TEST_PROJECT, "lead")
        assert replace.call_args.args[3:5] == ("codex", "claude")
        assert replace.call_args.kwargs == {"manual": True}
        close.assert_not_called()
        assert orch._project_panes(TEST_PROJECT)["backend"] is teammate

    def test_second_lead_click_during_replacement_is_ignored(self, orch: Orchestrator) -> None:
        orch._ps(f"{TEST_PROJECT}::lead").quota_reroute_pending = True
        with patch.object(orch, "confirm_manual_pane_close") as confirm:
            orch._on_pane_close_clicked("lead")
        confirm.assert_not_called()

    def test_expired_quota_does_not_block_same_provider_replacement(
        self, orch: Orchestrator
    ) -> None:
        lead = _make_pane("working")
        lead.role.name = "lead"
        lead.model.provider_name = "claude"
        lead.session.rate_limit_reset_at.return_value = None
        orch._project_panes(TEST_PROJECT)["lead"] = lead
        ps = orch._ps(f"{TEST_PROJECT}::lead")
        ps.quota_provider = "claude"
        ps.rate_limited_until = time.time() - 60
        with (
            patch.object(orch, "confirm_manual_pane_close", return_value=True),
            patch("agent_takkub.provider_config.pick_substitute_provider", return_value=None),
            patch.object(orch, "_reroute_pane_to_provider") as replace,
        ):
            orch._on_pane_close_clicked("lead")
        assert replace.call_args.args[3:] == ("claude", "claude", 0.0)


class TestAutomatedClosePathsBypassConfirm:
    """CLI close / done-report auto-close / close-all must never dialog-gate."""

    def test_close_does_not_consult_confirm_gate(self, orch: Orchestrator) -> None:
        pane = _make_pane("active")
        pane.session = None
        orch._project_panes(TEST_PROJECT)["frontend"] = pane
        with patch.object(orch, "confirm_manual_pane_close") as mocked_confirm:
            orch.close("frontend", project=TEST_PROJECT)
        mocked_confirm.assert_not_called()

    def test_close_all_teammates_does_not_consult_confirm_gate(self, orch: Orchestrator) -> None:
        pane = _make_pane("active")
        pane.session = None
        orch._project_panes(TEST_PROJECT)["frontend"] = pane
        with patch.object(orch, "confirm_manual_pane_close") as mocked_confirm:
            orch.close_all_teammates(project=TEST_PROJECT)
        mocked_confirm.assert_not_called()

    def test_manual_close_reminds_lead_about_review_card(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from agent_takkub import backlog

        monkeypatch.setattr(backlog, "RUNTIME_DIR", tmp_path / "runtime")
        card, _ = backlog.ensure_for_assign(TEST_PROJECT, "backend", "ตรวจงาน")
        backlog.bind_task_id(TEST_PROJECT, "backend", "task-1", item_id=card["id"])
        backlog.on_ledger_done(TEST_PROJECT, "task-1", item_id=card["id"], role="backend")
        pane = _make_pane("active")
        pane.session = None
        orch._project_panes(TEST_PROJECT)["backend"] = pane
        with patch.object(orch, "_notify_lead") as notify:
            orch.close("backend", project=TEST_PROJECT)
        assert card["id"] in notify.call_args.args[1]
        assert "takkub backlog done" in notify.call_args.args[1]
