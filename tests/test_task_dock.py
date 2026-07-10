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
        assert PROJECT in project_item.text(0)
        assert "(0/1)" in project_item.text(0)

        task_ledger.mark_done(PROJECT, "backend", "ok")
        widget.refresh_project(PROJECT)
        project_item = widget._tree.topLevelItem(0)
        assert "(1/1)" in project_item.text(0)
        row_item = project_item.child(0).child(0).child(0)
        assert row_item.text(0).startswith("✓")

    def test_project_with_no_rows_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(task_dock, "list_project_names", lambda: [])
        widget = task_dock.TaskDockWidget()
        widget.refresh_project("neverassigned")
        assert widget._tree.topLevelItemCount() == 0
