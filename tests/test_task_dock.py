"""Tests for the Task Tree dock (A8): `src/agent_takkub/task_dock.py`.

Pure-logic tests (status_glyph/project_progress/has_any_rows/feature_emoji)
need no QApplication. A small widget smoke test exercises TaskDockWidget end
to end against a real task_ledger state (offscreen QPA, session-scoped
QApplication already provided by tests/conftest.py) — full interactive/visual
verification is left to the user per the project's targeted-tests rule.
"""

from __future__ import annotations

import pathlib

import pytest
from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QResizeEvent
from PyQt6.QtWidgets import QHeaderView

from agent_takkub import cockpit_theme, task_dock, task_ledger

PROJECT = "taskdocktest"


@pytest.fixture(autouse=True)
def _isolate_runtime_dir(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(task_ledger, "RUNTIME_DIR", tmp_path)


# ──────────────────────────────────────────────────────────────
# pure helpers
# ──────────────────────────────────────────────────────────────
class TestStatusGlyph:
    def test_known_statuses_have_distinct_glyphs(self) -> None:
        results = [
            task_dock.status_glyph(s) for s in ("working", "ok", "fail", "closed", "superseded")
        ]
        glyphs = {glyph for glyph, _color in results}
        assert len(glyphs) == 5

    def test_unknown_status_falls_back_instead_of_raising(self) -> None:
        glyph, color = task_dock.status_glyph("queued")
        assert glyph == task_dock._STATUS_FALLBACK[0]
        assert color == task_dock._STATUS_FALLBACK[1]


class TestProjectProgress:
    def test_empty_state_is_zero_of_zero(self) -> None:
        assert task_dock.project_progress({"groups": []}) == (0, 0)

    def test_counts_ok_rows_as_done_others_as_open(self) -> None:
        state = {
            "groups": [
                {
                    "features": [
                        {
                            "rows": [
                                {"status": "ok"},
                                {"status": "working"},
                                {"status": "fail"},
                                {"status": "ok"},
                            ]
                        }
                    ]
                }
            ]
        }
        assert task_dock.project_progress(state) == (2, 4)

    def test_multiple_groups_and_features_accumulate(self) -> None:
        state = {
            "groups": [
                {"features": [{"rows": [{"status": "ok"}]}]},
                {"features": [{"rows": [{"status": "ok"}, {"status": "working"}]}]},
            ]
        }
        assert task_dock.project_progress(state) == (2, 3)


class TestHasAnyRows:
    def test_false_for_empty_state(self) -> None:
        assert task_dock.has_any_rows({"groups": []}) is False

    def test_true_once_a_row_exists(self) -> None:
        state = {"groups": [{"features": [{"rows": [{"status": "working"}]}]}]}
        assert task_dock.has_any_rows(state) is True


class TestFeatureEmoji:
    def test_empty_feature(self) -> None:
        assert task_dock.feature_emoji({"rows": []}) == "⏳"

    def test_any_working_row_wins(self) -> None:
        feat = {"rows": [{"status": "ok"}, {"status": "working"}]}
        assert task_dock.feature_emoji(feat) == "\U0001f528"

    def test_any_fail_without_working_shows_warning(self) -> None:
        feat = {"rows": [{"status": "ok"}, {"status": "fail"}]}
        assert task_dock.feature_emoji(feat) == "⚠️"

    def test_all_terminal_success_shows_check(self) -> None:
        feat = {"rows": [{"status": "ok"}, {"status": "closed"}, {"status": "superseded"}]}
        assert task_dock.feature_emoji(feat) == "✅"


# ──────────────────────────────────────────────────────────────
# widget smoke test
# ──────────────────────────────────────────────────────────────
class TestTaskDockWidget:
    def test_refresh_project_renders_row_and_reflects_done(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        widget = task_dock.TaskDockWidget()
        assert widget._tree.topLevelItemCount() == 0

        task_ledger.create_assignment(
            PROJECT, "backend", "/api", "add /health endpoint", "ship v1", "A8 dock", "claude"
        )
        widget.refresh_project(PROJECT)
        assert widget._tree.topLevelItemCount() == 1
        project_item = widget._tree.topLevelItem(0)
        # Item text is cleared on mount (A8-regression item 1) — the
        # ProjectCardWidget item-widget is the sole renderer now.
        card = widget._tree.itemWidget(project_item, 0)
        assert isinstance(card, task_dock.ProjectCardWidget)

        task_ledger.mark_done(PROJECT, "backend", "ok")
        widget.refresh_project(PROJECT)
        project_item = widget._tree.topLevelItem(0)
        row_item = project_item.child(0).child(0).child(0)
        assert row_item.text(0).startswith("✓")

    def test_project_with_no_rows_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        widget = task_dock.TaskDockWidget()
        widget.refresh_project("neverassigned")
        assert widget._tree.topLevelItemCount() == 0


# ──────────────────────────────────────────────────────────────
# Task List shows only the active project's tab, not every open project
# mixed together.
# ──────────────────────────────────────────────────────────────
class TestSetProject:
    OTHER = "othertaskdocktest"

    def test_set_project_shows_only_that_projects_card(self) -> None:
        widget = task_dock.TaskDockWidget()
        task_ledger.create_assignment(PROJECT, "backend", "/api", "task one", None, None, "claude")
        task_ledger.create_assignment(
            self.OTHER, "backend", "/api", "task two", None, None, "claude"
        )
        widget.set_project(PROJECT)
        assert widget._tree.topLevelItemCount() == 1
        item = widget._tree.topLevelItem(0)
        assert item.data(0, Qt.ItemDataRole.UserRole) == f"project:{PROJECT}"

    def test_switching_project_drops_the_previous_projects_card(self) -> None:
        widget = task_dock.TaskDockWidget()
        task_ledger.create_assignment(PROJECT, "backend", "/api", "task one", None, None, "claude")
        task_ledger.create_assignment(
            self.OTHER, "backend", "/api", "task two", None, None, "claude"
        )
        widget.set_project(PROJECT)
        widget.set_project(self.OTHER)
        assert widget._tree.topLevelItemCount() == 1
        item = widget._tree.topLevelItem(0)
        assert item.data(0, Qt.ItemDataRole.UserRole) == f"project:{self.OTHER}"

    def test_refresh_project_of_a_different_project_is_ignored_once_pinned(self) -> None:
        widget = task_dock.TaskDockWidget()
        task_ledger.create_assignment(PROJECT, "backend", "/api", "task one", None, None, "claude")
        widget.set_project(PROJECT)
        task_ledger.create_assignment(
            self.OTHER, "backend", "/api", "task two", None, None, "claude"
        )
        widget.refresh_project(self.OTHER)
        assert widget._tree.topLevelItemCount() == 1
        item = widget._tree.topLevelItem(0)
        assert item.data(0, Qt.ItemDataRole.UserRole) == f"project:{PROJECT}"

    def test_refresh_all_only_reloads_the_active_project(self) -> None:
        widget = task_dock.TaskDockWidget()
        task_ledger.create_assignment(PROJECT, "backend", "/api", "task one", None, None, "claude")
        widget.set_project(PROJECT)
        task_ledger.mark_done(PROJECT, "backend", "ok")
        widget.refresh_all()
        item = widget._tree.topLevelItem(0)
        card = widget._tree.itemWidget(item, 0)
        assert isinstance(card, task_dock.ProjectCardWidget)

    def test_set_project_none_clears_the_dock(self) -> None:
        widget = task_dock.TaskDockWidget()
        task_ledger.create_assignment(PROJECT, "backend", "/api", "task one", None, None, "claude")
        widget.set_project(PROJECT)
        widget.set_project(None)
        assert widget._tree.topLevelItemCount() == 0


# ──────────────────────────────────────────────────────────────
# walkthrough cluster D item 3: goalless assigns on different days used to
# render as N separate "(ไม่ระบุเป้าหมาย)" 🎯 headers — flatten them instead.
# ──────────────────────────────────────────────────────────────
class TestFallbackGoalFlattening:
    def test_is_fallback_goal(self) -> None:
        assert task_dock._is_fallback_goal("") is True
        assert task_dock._is_fallback_goal("   ") is True
        assert task_dock._is_fallback_goal(task_ledger._FALLBACK_GOAL) is True
        assert task_dock._is_fallback_goal("ship v1") is False

    def test_goalless_groups_have_no_goal_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        widget = task_dock.TaskDockWidget()
        task_ledger.create_assignment(PROJECT, "backend", "/api", "task one", None, None, "claude")
        widget.refresh_project(PROJECT)
        project_item = widget._tree.topLevelItem(0)
        # Feature row lands directly under the project — no intervening
        # 🎯 "(ไม่ระบุเป้าหมาย)" child.
        assert project_item.childCount() == 1
        feature_item = project_item.child(0)
        assert "ไม่ระบุเป้าหมาย" not in feature_item.text(0)
        assert feature_item.data(0, task_dock._ROLE_BASE_LABEL) is not None

    def test_goalless_groups_on_different_dates_merge_flat_not_duplicated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        widget = task_dock.TaskDockWidget()
        task_ledger.create_assignment(
            PROJECT, "backend", "/api", "task one", None, "feature A", "claude"
        )
        state = task_ledger.load_state(PROJECT)
        # Simulate a second goalless group from an earlier/later date so it
        # doesn't collide with today's group key (date, "(ไม่ระบุเป้าหมาย)").
        state["groups"].append(
            {
                "date": "2000-01-01",
                "goal": task_ledger._FALLBACK_GOAL,
                "features": [{"name": "feature B", "rows": [{"status": "ok", "role": "qa"}]}],
            }
        )
        item = widget._build_project_item(PROJECT, state)
        # Both goalless groups' features land as direct children — flat, no
        # duplicate "(ไม่ระบุเป้าหมาย)" headers for either date.
        assert item.childCount() == 2
        for i in range(item.childCount()):
            assert "ไม่ระบุเป้าหมาย" not in item.child(i).text(0)

    def test_real_goal_group_keeps_its_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        widget = task_dock.TaskDockWidget()
        task_ledger.create_assignment(
            PROJECT, "backend", "/api", "task one", "ship v1", "feature A", "claude"
        )
        widget.refresh_project(PROJECT)
        project_item = widget._tree.topLevelItem(0)
        assert project_item.childCount() == 1
        goal_item = project_item.child(0)
        assert "ship v1" in goal_item.text(0)


# ──────────────────────────────────────────────────────────────
# A8-polish item 1: responsive word-wrap tree config
# ──────────────────────────────────────────────────────────────
class TestTreeWrapConfig:
    def test_tree_wraps_and_hides_horizontal_scrollbar(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        widget = task_dock.TaskDockWidget()
        assert widget._tree.wordWrap() is True
        assert widget._tree.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff

    def test_header_stretches_single_column(self, monkeypatch: pytest.MonkeyPatch) -> None:
        widget = task_dock.TaskDockWidget()
        header = widget._tree.header()
        assert header.stretchLastSection() is True
        assert header.sectionResizeMode(0) == QHeaderView.ResizeMode.Stretch


# ──────────────────────────────────────────────────────────────
# A8-polish item 2: ProjectCardWidget responsiveness
# ──────────────────────────────────────────────────────────────
class TestProjectCardWidget:
    def _make_card(self, monkeypatch: pytest.MonkeyPatch) -> tuple:
        widget = task_dock.TaskDockWidget()
        task_ledger.create_assignment(
            PROJECT, "backend", "/api", "add /health endpoint", "ship v1", "A8 dock", "claude"
        )
        widget.refresh_project(PROJECT)
        item = widget._tree.topLevelItem(0)
        card = widget._tree.itemWidget(item, 0)
        assert isinstance(card, task_dock.ProjectCardWidget)
        return widget, item, card

    def test_card_bg_is_transparent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _widget, _item, card = self._make_card(monkeypatch)
        assert "background: transparent" in card.styleSheet()

    def test_narrow_width_hides_progress_and_open_button(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _widget, _item, card = self._make_card(monkeypatch)
        card.resizeEvent(QResizeEvent(QSize(300, 32), card.size()))
        assert card._progress_container.isHidden() is False
        assert card._open_btn.isHidden() is False

        card.resizeEvent(QResizeEvent(QSize(150, 32), QSize(300, 32)))
        assert card._progress_container.isHidden() is True
        assert card._open_btn.isHidden() is True

    def test_resize_propagates_height_into_item_size_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _widget, item, card = self._make_card(monkeypatch)
        card.resizeEvent(QResizeEvent(QSize(150, 32), card.size()))
        assert item.sizeHint(0).height() > 0


# ──────────────────────────────────────────────────────────────
# A8-regression: 3 bugs seen in the user's real screenshots
# ──────────────────────────────────────────────────────────────
class TestProjectItemTextClearedOnMount:
    """Item 1: the project item's own text used to bleed through the
    (partly transparent) ProjectCardWidget, doubling the project name/
    progress visually ("pms...5)pms")."""

    def test_item_text_is_empty_once_card_is_mounted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        widget = task_dock.TaskDockWidget()
        task_ledger.create_assignment(
            PROJECT, "backend", "/api", "add /health endpoint", "ship v1", "A8 dock", "claude"
        )
        widget.refresh_project(PROJECT)
        project_item = widget._tree.topLevelItem(0)
        assert project_item.text(0) == ""

    def test_chevron_toggle_still_works_without_item_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_apply_expanded_visual for a project item repaints the card's own
        chevron button (looked up by project name), not item.text(0) — so
        clearing the item text must not break expand/collapse."""
        widget = task_dock.TaskDockWidget()
        task_ledger.create_assignment(
            PROJECT, "backend", "/api", "add /health endpoint", "ship v1", "A8 dock", "claude"
        )
        widget.refresh_project(PROJECT)
        project_item = widget._tree.topLevelItem(0)
        widget._on_item_collapsed(project_item)
        assert widget._chevron_labels[PROJECT].text() == "▸"
        widget._on_item_expanded(project_item)
        assert widget._chevron_labels[PROJECT].text() == "▾"


class TestRowWrapRelayout:
    """Item 3: goal/feature/task rows need `updateGeometries()` +
    `scheduleDelayedItemsLayout()` forced right after a rebuild, not just on
    a later `sectionResized` — otherwise a wrapped 2-line label renders
    clipped to 1 line until the user happens to resize the dock."""

    def test_uniform_row_heights_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        widget = task_dock.TaskDockWidget()
        assert widget._tree.uniformRowHeights() is False

    def test_refresh_project_triggers_relayout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        widget = task_dock.TaskDockWidget()
        calls: list[str] = []
        monkeypatch.setattr(
            widget._tree, "updateGeometries", lambda: calls.append("updateGeometries")
        )
        monkeypatch.setattr(
            widget._tree,
            "scheduleDelayedItemsLayout",
            lambda: calls.append("scheduleDelayedItemsLayout"),
        )
        task_ledger.create_assignment(
            PROJECT, "backend", "/api", "add /health endpoint", "ship v1", "A8 dock", "claude"
        )
        widget.refresh_project(PROJECT)
        assert calls == ["updateGeometries", "scheduleDelayedItemsLayout"]


class TestWrapItemDelegate:
    """The bug Lead flagged from a real screenshot: goal/feature/task labels
    still showed `...` instead of reflowing to a 2nd line. Proven root cause —
    QTreeView's default delegate word-wraps the *painting* but never grows the
    row, so a long label stays one clipped line. `_WrapItemDelegate.sizeHint`
    returns the wrapped height so the view allocates a taller row.
    """

    def _delegate_and_tree(self, monkeypatch: pytest.MonkeyPatch):
        widget = task_dock.TaskDockWidget()
        tree = widget._tree
        tree.setFixedWidth(240)
        return tree, tree.itemDelegate(), widget

    def test_tree_uses_the_wrap_delegate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _tree, delegate, _w = self._delegate_and_tree(monkeypatch)
        assert isinstance(delegate, task_dock._WrapItemDelegate)

    def test_long_label_row_grows_taller_than_short_label_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from PyQt6.QtWidgets import QStyleOptionViewItem, QTreeWidgetItem

        tree, delegate, _w = self._delegate_and_tree(monkeypatch)
        short = QTreeWidgetItem(["🎯 short goal"])
        long = QTreeWidgetItem(
            [
                "🎯 a genuinely long goal label that clearly exceeds one narrow dock "
                "line and must reflow onto a second line instead of ending in an ellipsis"
            ]
        )
        tree.addTopLevelItem(short)
        tree.addTopLevelItem(long)
        opt = QStyleOptionViewItem()
        short_h = delegate.sizeHint(opt, tree.indexFromItem(short)).height()
        long_h = delegate.sizeHint(opt, tree.indexFromItem(long)).height()
        assert long_h > short_h

    def test_empty_text_row_uses_base_height(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The project row clears its text once its ProjectCardWidget mounts —
        # the delegate must not try to grow an empty row (it sizes itself).
        from PyQt6.QtWidgets import QStyleOptionViewItem, QTreeWidgetItem

        tree, delegate, _w = self._delegate_and_tree(monkeypatch)
        blank = QTreeWidgetItem([""])
        tree.addTopLevelItem(blank)
        opt = QStyleOptionViewItem()
        idx = tree.indexFromItem(blank)
        assert (
            delegate.sizeHint(opt, idx).height()
            == super(task_dock._WrapItemDelegate, delegate).sizeHint(opt, idx).height()
        )


# ──────────────────────────────────────────────────────────────
# 🌿 Git tab: pure helpers (no Qt import, duck-typed against a plain
# namespace instead of the real git_status.RepoStatus/Commit/Worktree).
# ──────────────────────────────────────────────────────────────
class _NS:
    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)


def _repo(**kw) -> _NS:
    base = dict(
        key="api",
        path="/repo/api",
        branch="main",
        upstream="origin/main",
        ahead=0,
        behind=0,
        staged=0,
        modified=0,
        untracked=0,
        commits=[],
        worktrees=[],
        error=None,
    )
    base.update(kw)
    return _NS(**base)


class TestRepoStateColor:
    def test_error_is_faint_not_red(self) -> None:
        assert task_dock.repo_state_color(_repo(error="not a git repo")) == cockpit_theme.TEXT_FAINT

    def test_dirty_or_untracked_is_warn(self) -> None:
        assert task_dock.repo_state_color(_repo(modified=1)) == cockpit_theme.STATE_WARN_BRIGHT
        assert task_dock.repo_state_color(_repo(untracked=1)) == cockpit_theme.STATE_WARN_BRIGHT

    def test_ahead_only_is_accent(self) -> None:
        assert task_dock.repo_state_color(_repo(ahead=2)) == cockpit_theme.ACCENT_GOLD

    def test_clean_is_muted(self) -> None:
        assert task_dock.repo_state_color(_repo()) == cockpit_theme.TEXT_MUTED


class TestRepoStatusSummary:
    def test_clean_repo_says_clean(self) -> None:
        assert task_dock.repo_status_summary(_repo()).endswith("clean")

    def test_dirty_repo_shows_dot_and_plus_counts(self) -> None:
        summary = task_dock.repo_status_summary(_repo(staged=2, modified=1, untracked=3))
        assert "●3" in summary
        assert "+3" in summary

    def test_ahead_behind_shown_when_upstream_set(self) -> None:
        summary = task_dock.repo_status_summary(_repo(ahead=2, behind=1))
        assert "↑2 ↓1" in summary

    def test_no_upstream_omits_ahead_behind(self) -> None:
        summary = task_dock.repo_status_summary(_repo(upstream=None))
        assert "↑" not in summary

    def test_error_repo_shows_the_error_message(self) -> None:
        assert task_dock.repo_status_summary(_repo(error="timeout")) == "timeout"


class TestRepoHeaderLabel:
    def test_includes_key_and_summary(self) -> None:
        label = task_dock.repo_header_label(_repo(key="api"))
        assert "api" in label
        assert "clean" in label


class TestCommitHelpers:
    def _commit(self, **kw) -> _NS:
        base = dict(sha="a1b2c3d4e5f6", subject="fix(#12): thing", author="pim", rel_time="2h ago")
        base.update(kw)
        return _NS(**base)

    def test_commit_row_label_shortens_sha(self) -> None:
        label = task_dock.commit_row_label(self._commit())
        assert "a1b2c3d" in label
        assert "a1b2c3d4e5f6" not in label
        assert "2h ago" in label

    def test_commit_tooltip_has_full_subject_and_author(self) -> None:
        tooltip = task_dock.commit_tooltip(self._commit())
        assert "fix(#12): thing" in tooltip
        assert "pim" in tooltip


class TestWorktreeHelpers:
    def _wt(self, **kw) -> _NS:
        base = dict(branch="wt/backend-178", path="/repo/wt", commits_ahead=0, dirty=False)
        base.update(kw)
        return _NS(**base)

    def test_clean_worktree_has_no_suffix(self) -> None:
        assert task_dock.worktree_row_label(self._wt()) == "wt/backend-178"

    def test_ahead_and_dirty_worktree_shows_both(self) -> None:
        label = task_dock.worktree_row_label(self._wt(commits_ahead=3, dirty=True))
        assert "↑3" in label
        assert "dirty" in label

    def test_dirty_worktree_is_warn_colored(self) -> None:
        assert task_dock.worktree_color(self._wt(dirty=True)) == cockpit_theme.STATE_WARN_BRIGHT

    def test_clean_worktree_is_muted(self) -> None:
        assert task_dock.worktree_color(self._wt()) == cockpit_theme.TEXT_MUTED


# ──────────────────────────────────────────────────────────────
# 🌿 Git tab: TaskDockWidget wiring — set_project propagates, tab lifecycle
# starts/stops the poll timer only while visible + selected.
# ──────────────────────────────────────────────────────────────
class TestGitTabWiring:
    def test_dock_has_two_tabs_labelled_tasks_and_git(self) -> None:
        widget = task_dock.TaskDockWidget()
        assert widget._tabs.count() == 2
        assert "Tasks" in widget._tabs.tabText(0)
        assert "Git" in widget._tabs.tabText(1)

    def test_set_project_propagates_to_git_view(self) -> None:
        widget = task_dock.TaskDockWidget()
        widget.set_project(PROJECT)
        assert widget._git_view._project == PROJECT

    def test_git_view_starts_inactive(self) -> None:
        view = task_dock.GitStatusView()
        assert view._timer.isActive() is False

    def test_set_active_true_with_project_starts_timer(self) -> None:
        view = task_dock.GitStatusView()
        view.set_project(PROJECT)
        view.set_active(True)
        assert view._timer.isActive() is True

    def test_set_active_false_stops_timer(self) -> None:
        view = task_dock.GitStatusView()
        view.set_project(PROJECT)
        view.set_active(True)
        view.set_active(False)
        assert view._timer.isActive() is False

    def test_set_active_without_project_does_not_start_timer(self) -> None:
        view = task_dock.GitStatusView()
        view.set_active(True)
        assert view._timer.isActive() is False

    def test_switching_to_git_tab_activates_it(self) -> None:
        widget = task_dock.TaskDockWidget()
        widget.set_project(PROJECT)
        widget.show()
        widget._tabs.setCurrentIndex(task_dock.TaskDockWidget._GIT_TAB_INDEX)
        assert widget._git_view._timer.isActive() is True
        widget._tabs.setCurrentIndex(0)
        assert widget._git_view._timer.isActive() is False

    def test_hiding_the_dock_deactivates_the_git_tab(self) -> None:
        widget = task_dock.TaskDockWidget()
        widget.set_project(PROJECT)
        widget.show()
        widget._tabs.setCurrentIndex(task_dock.TaskDockWidget._GIT_TAB_INDEX)
        assert widget._git_view._timer.isActive() is True
        widget.hide()
        assert widget._git_view._timer.isActive() is False


class TestGitStatusViewRendering:
    def test_render_error_repo_shows_error_and_no_children(self) -> None:
        view = task_dock.GitStatusView()
        view._render([_repo(error="not a git repo")])
        assert view._tree.topLevelItemCount() == 1
        item = view._tree.topLevelItem(0)
        assert item.childCount() == 0

    def test_render_repo_with_commits_and_worktrees(self) -> None:
        view = task_dock.GitStatusView()
        commit = _NS(sha="a1b2c3d", subject="fix: x", author="pim", rel_time="2h ago")
        wt = _NS(branch="wt/x", path="/x", commits_ahead=1, dirty=True)
        view._render([_repo(commits=[commit], worktrees=[wt])])
        item = view._tree.topLevelItem(0)
        # 1 commit child + 1 worktrees-group child
        assert item.childCount() == 2
        wt_group = item.child(1)
        assert wt_group.childCount() == 1

    def test_render_empty_list_shows_placeholder(self) -> None:
        view = task_dock.GitStatusView()
        view._render([])
        assert view._tree.topLevelItemCount() == 1
        assert "ไม่มี" in view._tree.topLevelItem(0).text(0)
