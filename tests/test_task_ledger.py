"""Tests for the Task Ledger (A7): `src/agent_takkub/task_ledger.py`.

Covers: create_assignment writes a detail file + upserts an INDEX.md row,
mark_done flips ok/fail/closed markers, write-failure degrades without
raising, atomic writes leave no stray temp files, --feature groups rows
under distinct '### N. <feature>' sections, and a missing goal falls back
to a placeholder string instead of an empty/blank group header.
"""

from __future__ import annotations

import pathlib

import pytest

from agent_takkub import task_ledger

PROJECT = "ledgertest"


@pytest.fixture(autouse=True)
def _isolate_runtime_dir(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(task_ledger, "RUNTIME_DIR", tmp_path)


def _index_text() -> str:
    return task_ledger._index_path(PROJECT).read_text(encoding="utf-8")


class TestCreateAssignment:
    def test_writes_detail_file_and_index_row(self) -> None:
        warning = task_ledger.create_assignment(
            PROJECT,
            "backend",
            "/api",
            "[ROLE: backend] add /health endpoint",
            "ship v1",
            "A7 ledger",
            "claude",
        )
        assert warning == ""

        state = task_ledger._load_state(PROJECT)
        row = state["groups"][0]["features"][0]["rows"][0]
        detail_path = task_ledger._ledger_dir(PROJECT) / row["detail_rel"]
        assert detail_path.exists()
        detail_text = detail_path.read_text(encoding="utf-8")
        assert "role: backend" in detail_text
        assert "status: working" in detail_text
        assert "[ROLE: backend] add /health endpoint" in detail_text

        index_text = _index_text()
        assert "🎯 เป้าหมาย: ship v1" in index_text
        assert "### " in index_text and "A7 ledger" in index_text
        assert "[~]" in index_text
        assert "**backend**" in index_text
        assert "add /health endpoint" in index_text

    def test_no_goal_falls_back_to_placeholder(self) -> None:
        task_ledger.create_assignment(
            PROJECT, "qa", "/api", "smoke test", None, "general", "claude"
        )
        assert task_ledger._FALLBACK_GOAL in _index_text()

    def test_no_feature_falls_back_to_general(self) -> None:
        task_ledger.create_assignment(PROJECT, "qa", "/api", "smoke test", "goal", None, "claude")
        assert task_ledger._FALLBACK_FEATURE in _index_text()

    def test_feature_grouping_separates_sections(self) -> None:
        task_ledger.create_assignment(
            PROJECT, "backend", "/api", "task A", "shared goal", "feature-A", "claude"
        )
        task_ledger.create_assignment(
            PROJECT, "frontend", "/web", "task B", "shared goal", "feature-B", "claude"
        )
        state = task_ledger._load_state(PROJECT)
        # Same date+goal → one group, two distinct feature buckets.
        assert len(state["groups"]) == 1
        names = [f["name"] for f in state["groups"][0]["features"]]
        assert names == ["feature-A", "feature-B"]

        index_text = _index_text()
        assert "1. feature-A" in index_text
        assert "2. feature-B" in index_text


class TestReassignBeforeDone:
    """A7-followup: re-assign to the same role before its open row is `done`
    must not leave an orphaned `[~]` row or double-count progress."""

    def test_stale_open_row_is_superseded_not_orphaned(self) -> None:
        task_ledger.create_assignment(
            PROJECT, "backend", "/api", "first task", "goal", "feat", "claude"
        )
        task_ledger.create_assignment(
            PROJECT, "backend", "/api", "second task", "goal", "feat", "claude"
        )
        state = task_ledger._load_state(PROJECT)
        rows = state["groups"][0]["features"][0]["rows"]
        assert len(rows) == 2
        assert rows[0]["status"] == "superseded"
        assert rows[1]["status"] == "working"
        # Only the second (fresh) row is tracked as open for the role.
        assert state["open"]["backend"]["row_index"] == 1

        index_text = _index_text()
        assert "[>]" in index_text
        assert "🔁 แทนที่ด้วยงานใหม่" in index_text
        # No stray `[~]` row left for the superseded first row (the legend
        # line also contains the literal text `[~]`, so count row markers).
        assert index_text.count("- [~]") == 1

    def test_progress_counts_open_row_once_not_double(self) -> None:
        task_ledger.create_assignment(
            PROJECT, "backend", "/api", "first task", "goal", "feat", "claude"
        )
        task_ledger.create_assignment(
            PROJECT, "backend", "/api", "second task", "goal", "feat", "claude"
        )
        task_ledger.mark_done(PROJECT, "backend", "ok")
        index_text = _index_text()
        assert "progress: 1/2 เสร็จ · 0 กำลังทำ" in index_text

    def test_mark_done_after_reassign_flips_only_the_new_row(self) -> None:
        task_ledger.create_assignment(
            PROJECT, "backend", "/api", "first task", "goal", "feat", "claude"
        )
        task_ledger.create_assignment(
            PROJECT, "backend", "/api", "second task", "goal", "feat", "claude"
        )
        task_ledger.mark_done(PROJECT, "backend", "ok")
        state = task_ledger._load_state(PROJECT)
        rows = state["groups"][0]["features"][0]["rows"]
        assert rows[0]["status"] == "superseded"
        assert rows[1]["status"] == "ok"
        assert "backend" not in state.get("open", {})


class TestMarkDone:
    def test_flip_ok_shows_done_checkbox(self) -> None:
        task_ledger.create_assignment(
            PROJECT, "backend", "/api", "do the thing", "goal", "feat", "claude"
        )
        warning = task_ledger.mark_done(PROJECT, "backend", "ok")
        assert warning == ""
        index_text = _index_text()
        assert "[x]" in index_text
        assert "✅ done" in index_text

        state = task_ledger._load_state(PROJECT)
        assert "backend" not in state.get("open", {})

    def test_flip_fail_shows_failed_marker(self) -> None:
        task_ledger.create_assignment(PROJECT, "qa", "/api", "smoke test", "goal", "feat", "claude")
        task_ledger.mark_done(PROJECT, "qa", "fail")
        index_text = _index_text()
        assert "[!]" in index_text
        assert "❌ FAILED" in index_text

    def test_flip_closed_shows_closed_marker(self) -> None:
        task_ledger.create_assignment(
            PROJECT, "reviewer", "/api", "review pr", "goal", "feat", "claude"
        )
        task_ledger.mark_done(PROJECT, "reviewer", "closed")
        index_text = _index_text()
        assert "[-]" in index_text
        assert "➖ ปิด" in index_text

    def test_flip_updates_detail_file_frontmatter(self) -> None:
        task_ledger.create_assignment(
            PROJECT, "backend", "/api", "do the thing", "goal", "feat", "claude"
        )
        state = task_ledger._load_state(PROJECT)
        detail_rel = state["groups"][0]["features"][0]["rows"][0]["detail_rel"]
        task_ledger.mark_done(PROJECT, "backend", "ok")
        detail_text = (task_ledger._ledger_dir(PROJECT) / detail_rel).read_text(encoding="utf-8")
        assert "status: ok" in detail_text
        assert "status: working" not in detail_text

    def test_no_open_row_is_a_noop_not_a_crash(self) -> None:
        warning = task_ledger.mark_done(PROJECT, "nobody-assigned-this-role", "ok")
        assert warning == ""

    def test_invalid_status_raises(self) -> None:
        with pytest.raises(ValueError):
            task_ledger.mark_done(PROJECT, "backend", "bogus")


class TestWriteFailureDegrades:
    def test_create_assignment_detail_write_failure_degrades(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_a, **_kw):
            raise OSError("disk full")

        monkeypatch.setattr(pathlib.Path, "write_text", _boom)
        warning = task_ledger.create_assignment(
            PROJECT, "backend", "/api", "task", "goal", "feat", "claude"
        )
        assert warning != ""
        assert "backend" in warning

    def test_mark_done_detail_flip_failure_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        task_ledger.create_assignment(PROJECT, "backend", "/api", "task", "goal", "feat", "claude")

        real_replace = task_ledger._atomic_write

        def _boom_on_second_call(path, content):
            if "-ledger.md" in str(path):
                raise OSError("disk full")
            real_replace(path, content)

        monkeypatch.setattr(task_ledger, "_atomic_write", _boom_on_second_call)
        warning = task_ledger.mark_done(PROJECT, "backend", "ok")
        assert warning != ""
        # INDEX.md still gets regenerated (only the detail-file flip failed).
        assert "[x]" in _index_text()


class TestAtomicWrite:
    def test_no_stray_temp_files_left_behind(self) -> None:
        task_ledger.create_assignment(PROJECT, "backend", "/api", "task", "goal", "feat", "claude")
        task_ledger.mark_done(PROJECT, "backend", "ok")
        leftovers = list(task_ledger._ledger_dir(PROJECT).rglob("*.tmp*"))
        assert leftovers == []

    def test_index_write_uses_replace_not_truncate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = []
        import os as _os

        real_replace = _os.replace

        def _spy_replace(src, dst):
            calls.append((str(src), str(dst)))
            real_replace(src, dst)

        monkeypatch.setattr(task_ledger.os, "replace", _spy_replace)
        task_ledger.create_assignment(PROJECT, "backend", "/api", "task", "goal", "feat", "claude")
        assert calls  # os.replace was used at least once (temp → final)
        for src, dst in calls:
            assert src != dst
            assert ".tmp" in src


class TestDeriveSummary:
    def test_declaration_only_falls_back_to_first_line(self) -> None:
        task = "[ROLE: backend developer — ทำงานเองโดยตรง ห้าม spawn subagent]"
        assert task_ledger._derive_summary(task) == task

    def test_declaration_plus_task_skips_declaration_line(self) -> None:
        task = (
            "[ROLE: backend developer — ทำงานเองโดยตรง ห้าม spawn subagent]\n"
            "งาน A8-tweak: ปรับ Task dock ให้อ่านง่ายขึ้น\n"
        )
        assert task_ledger._derive_summary(task) == "งาน A8-tweak: ปรับ Task dock ให้อ่านง่ายขึ้น"

    def test_trailer_is_excluded_even_if_only_meaningful_looking_line(self) -> None:
        task = "[ROLE: backend developer — ...]\n\nรายงานกลับด้วย takkub done เมื่อเสร็จ\n"
        assert task_ledger._derive_summary(task) == "[ROLE: backend developer — ...]"

    def test_declaration_task_and_trailer_picks_middle_line(self) -> None:
        task = (
            "[ROLE: backend developer — ...]\n"
            "งาน: เพิ่ม endpoint /health\n"
            "รายงานกลับด้วย takkub done เมื่อเสร็จ\n"
        )
        assert task_ledger._derive_summary(task) == "งาน: เพิ่ม endpoint /health"

    def test_empty_task_returns_empty_string(self) -> None:
        assert task_ledger._derive_summary("") == ""
        assert task_ledger._derive_summary("   \n  \n") == ""

    def test_truncates_over_100_chars(self) -> None:
        long_line = "งาน: " + ("x" * 120)
        task = f"[ROLE: backend]\n{long_line}\n"
        result = task_ledger._derive_summary(task)
        assert len(result) == 101  # 100 chars + '…'
        assert result.endswith("…")
