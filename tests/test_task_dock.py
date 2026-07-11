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
from PyQt6.QtCore import QSize
from PyQt6.QtGui import QResizeEvent
from PyQt6.QtWidgets import QHeaderView

from agent_takkub import task_dock, task_ledger

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
        monkeypatch.setattr(task_dock, "list_project_names", lambda: [])
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
        monkeypatch.setattr(task_dock, "list_project_names", lambda: [])
        widget = task_dock.TaskDockWidget()
        widget.refresh_project("neverassigned")
        assert widget._tree.topLevelItemCount() == 0


# ──────────────────────────────────────────────────────────────
# A8-polish item 1: responsive word-wrap tree config
# ──────────────────────────────────────────────────────────────
class TestTreeWrapConfig:
    def test_tree_wraps_and_hides_horizontal_scrollbar(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(task_dock, "list_project_names", lambda: [])
        widget = task_dock.TaskDockWidget()
        assert widget._tree.wordWrap() is True
        from PyQt6.QtCore import Qt

        assert widget._tree.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff

    def test_header_stretches_single_column(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(task_dock, "list_project_names", lambda: [])
        widget = task_dock.TaskDockWidget()
        header = widget._tree.header()
        assert header.stretchLastSection() is True
        assert header.sectionResizeMode(0) == QHeaderView.ResizeMode.Stretch


# ──────────────────────────────────────────────────────────────
# A8-polish item 2: ProjectCardWidget responsiveness
# ──────────────────────────────────────────────────────────────
class TestProjectCardWidget:
    def _make_card(self, monkeypatch: pytest.MonkeyPatch) -> tuple:
        monkeypatch.setattr(task_dock, "list_project_names", lambda: [])
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
        monkeypatch.setattr(task_dock, "list_project_names", lambda: [])
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
        monkeypatch.setattr(task_dock, "list_project_names", lambda: [])
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
        monkeypatch.setattr(task_dock, "list_project_names", lambda: [])
        widget = task_dock.TaskDockWidget()
        assert widget._tree.uniformRowHeights() is False

    def test_refresh_project_triggers_relayout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(task_dock, "list_project_names", lambda: [])
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
        monkeypatch.setattr(task_dock, "list_project_names", lambda: [])
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
