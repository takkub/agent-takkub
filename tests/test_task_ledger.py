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
        warning, returned_path = task_ledger.create_assignment(
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
        assert returned_path == detail_path
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


class TestLedgerDirSanitized:
    """#294: `_ledger_dir` used the raw project name as a path segment with no
    sanitizing at all — a Thai or Windows-reserved project name would either
    collide with another project or fail to create the directory."""

    def test_distinct_thai_project_names_get_distinct_ledger_dirs(self) -> None:
        a = task_ledger._ledger_dir("โปรเจกต์ไทย")
        b = task_ledger._ledger_dir("ระบบขายของ")
        assert a != b

    def test_windows_reserved_project_name_is_guarded(self) -> None:
        assert task_ledger._ledger_dir("CON").name != "CON"

    def test_ascii_project_name_unchanged(self) -> None:
        assert task_ledger._ledger_dir(PROJECT).name == PROJECT


class TestQueuedStatus:
    """#303 item 1: a resource-gate-blocked assign writes its ledger row/
    detail file with status="queued" the moment it's queued, not only once
    a pane finally spawns for it — so Lead has something on disk to read
    (and edit) while it waits."""

    def test_queued_status_writes_detail_file_and_row(self) -> None:
        warning, detail_path = task_ledger.create_assignment(
            PROJECT, "qa", "/api", "run e2e", "goal", "feat", "claude", status="queued"
        )
        assert warning == ""
        assert detail_path is not None
        assert "status: queued" in detail_path.read_text(encoding="utf-8")

        state = task_ledger._load_state(PROJECT)
        row = state["groups"][0]["features"][0]["rows"][0]
        assert row["status"] == "queued"
        index_text = _index_text()
        assert "🕓 อยู่ในคิว" in index_text

    def test_admission_supersedes_the_queued_row(self) -> None:
        """Once the gate-blocked assign is actually admitted,
        `_assign_dispatch`'s normal (status="working") `create_assignment`
        call re-fires for the same role — same stale-open-row supersede
        path a plain re-assign already goes through."""
        task_ledger.create_assignment(
            PROJECT, "qa", "/api", "run e2e", "goal", "feat", "claude", status="queued"
        )
        task_ledger.create_assignment(PROJECT, "qa", "/api", "run e2e", "goal", "feat", "claude")

        state = task_ledger._load_state(PROJECT)
        rows = state["groups"][0]["features"][0]["rows"]
        assert rows[0]["status"] == "superseded"
        assert rows[1]["status"] == "working"

    def test_close_role_works_on_a_queued_row_with_no_live_pane(self) -> None:
        """#303 item 2: `takkub task cancel` on a still-queued (no pane at
        all) role reuses `close_role` — must succeed since the role is,
        by definition, never in `live_roles`."""
        task_ledger.create_assignment(
            PROJECT, "qa", "/api", "run e2e", "goal", "feat", "claude", status="queued"
        )
        ok, _msg = task_ledger.close_role(PROJECT, "qa", frozenset())
        assert ok is True
        state = task_ledger._load_state(PROJECT)
        assert "qa" not in state["open"]
        assert state["groups"][0]["features"][0]["rows"][0]["status"] == "closed"


class TestReadDetailTask:
    def test_reads_body_after_frontmatter(self) -> None:
        _warning, detail_path = task_ledger.create_assignment(
            PROJECT, "backend", "/api", "do the thing\nline two", "goal", "feat", "claude"
        )
        assert task_ledger.read_detail_task(detail_path) == "do the thing\nline two"

    def test_reflects_a_hand_edit_made_while_queued(self) -> None:
        """The exact scenario #303 item 1 exists for: Lead edits the file by
        hand (e.g. tightening a safety condition) before the task is ever
        admitted — the next read must see the edit, not the original text."""
        _warning, detail_path = task_ledger.create_assignment(
            PROJECT, "qa", "/api", "original task", "goal", "feat", "claude", status="queued"
        )
        edited = detail_path.read_text(encoding="utf-8").replace(
            "original task", "original task\nห้ามรัน rebuild ถ้า docker engine ยังไม่นิ่ง"
        )
        detail_path.write_text(edited, encoding="utf-8")

        assert "ห้ามรัน rebuild" in task_ledger.read_detail_task(detail_path)

    def test_missing_file_returns_none(self, tmp_path: pathlib.Path) -> None:
        assert task_ledger.read_detail_task(tmp_path / "nope.md") is None

    def test_no_frontmatter_returns_text_unchanged(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "plain.md"
        path.write_text("just a plain body, no frontmatter", encoding="utf-8")
        assert task_ledger.read_detail_task(path) == "just a plain body, no frontmatter"


class TestFlipDetailStatusFromQueued:
    def test_flip_from_queued_status_line_works(self) -> None:
        """Regression guard: the old `_flip_detail_status` only ever matched
        the literal string "status: working\\n", so a detail file created
        with a non-"working" initial status (queued) never got its
        frontmatter updated on close/mark_done — the on-disk status silently
        stuck at "queued" forever even after the ledger row itself moved on."""
        task_ledger.create_assignment(
            PROJECT, "qa", "/api", "run e2e", "goal", "feat", "claude", status="queued"
        )
        ok, _msg = task_ledger.close_role(PROJECT, "qa", frozenset())
        assert ok is True
        state = task_ledger._load_state(PROJECT)
        # close_role already popped "qa" from open — re-derive the row path
        # via the group/feature we know this test used.
        detail_rel = state["groups"][0]["features"][0]["rows"][0]["detail_rel"]
        detail_text = (task_ledger._ledger_dir(PROJECT) / detail_rel).read_text(encoding="utf-8")
        assert "status: closed" in detail_text
        assert "status: queued" not in detail_text


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
        warning, detail_path = task_ledger.create_assignment(
            PROJECT, "backend", "/api", "task", "goal", "feat", "claude"
        )
        assert warning != ""
        assert "backend" in warning
        assert detail_path is None

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
        task = "[ROLE: backend --mode subagent]"
        assert task_ledger._derive_summary(task) == task

    def test_declaration_plus_task_skips_declaration_line(self) -> None:
        task = (
            "[ROLE: backend developer — ทำงานเองโดยตรง ห้าม spawn subagent เอง เว้นแต่ Lead สั่งด้วย --mode subagent]\n"
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


def _backdate_open_row(project: str, role: str, date: str) -> None:
    """Test helper: rewrite a role's open row + its group to *date*, so
    reconcile-gate tests can simulate "assigned on a prior day" without
    waiting for a real day to pass."""
    state = task_ledger._load_state(project)
    ptr = state["open"][role]
    group = task_ledger._find_group(state, ptr["date"], ptr["goal"])
    group["date"] = date
    ptr["date"] = date
    task_ledger._save_state(project, state)


class TestReconcileOrphaned:
    """Issue #166: a "working" row used to stick forever once the cockpit
    process that owned it exited — mark_done only ever fires from a live
    pane's own done/close handler. reconcile_orphaned() is the startup-time
    fix; its date<today gate is the safety net that stops it from closing a
    same-day row an auto-respawn is about to resume."""

    def test_reconciles_row_backdated_before_today(self) -> None:
        task_ledger.create_assignment(PROJECT, "backend", "/api", "do X", "goal", "feat", "claude")
        _backdate_open_row(PROJECT, "backend", "2020-01-01")

        closed, warning = task_ledger.reconcile_orphaned(PROJECT, frozenset(), today="2099-01-01")

        assert closed == ["backend"]
        assert warning == ""
        state = task_ledger._load_state(PROJECT)
        assert "backend" not in state["open"]
        row = state["groups"][0]["features"][0]["rows"][0]
        assert row["status"] == "closed"
        assert row["reason"] == "orphaned"
        assert "orphaned" in _index_text()

    def test_leaves_same_day_row_untouched(self) -> None:
        """A row assigned today must survive reconcile even if no pane is
        currently live — auto-respawn may still bring it back."""
        task_ledger.create_assignment(PROJECT, "backend", "/api", "do X", "goal", "feat", "claude")
        today = task_ledger._load_state(PROJECT)["open"]["backend"]["date"]

        closed, _warning = task_ledger.reconcile_orphaned(PROJECT, frozenset(), today=today)

        assert closed == []
        state = task_ledger._load_state(PROJECT)
        assert "backend" in state["open"]

    def test_leaves_backdated_row_alone_when_role_still_has_a_live_pane(self) -> None:
        """Belt-and-suspenders: even a stale-dated row must never be closed
        while its role currently has a live pane."""
        task_ledger.create_assignment(PROJECT, "backend", "/api", "do X", "goal", "feat", "claude")
        _backdate_open_row(PROJECT, "backend", "2020-01-01")

        closed, _warning = task_ledger.reconcile_orphaned(
            PROJECT, frozenset({"backend"}), today="2099-01-01"
        )

        assert closed == []
        state = task_ledger._load_state(PROJECT)
        assert "backend" in state["open"]

    def test_no_open_rows_is_a_no_op(self) -> None:
        closed, warning = task_ledger.reconcile_orphaned(PROJECT, frozenset(), today="2099-01-01")
        assert closed == []
        assert warning == ""

    def test_preview_reports_candidates_without_mutating_state(self) -> None:
        task_ledger.create_assignment(PROJECT, "backend", "/api", "do X", "goal", "feat", "claude")
        _backdate_open_row(PROJECT, "backend", "2020-01-01")

        preview = task_ledger.preview_reconcile(PROJECT, frozenset(), today="2099-01-01")

        assert len(preview) == 1
        assert preview[0]["role"] == "backend"
        assert preview[0]["date"] == "2020-01-01"
        state = task_ledger._load_state(PROJECT)
        assert "backend" in state["open"]  # preview never mutates


class TestCloseRole:
    """Issue #166's user-facing escape hatch: `takkub task close --role`."""

    def test_closes_open_row(self) -> None:
        task_ledger.create_assignment(PROJECT, "qa", "/api", "smoke", "goal", "feat", "claude")

        ok, _msg = task_ledger.close_role(PROJECT, "qa", frozenset())

        assert ok is True
        state = task_ledger._load_state(PROJECT)
        assert "qa" not in state["open"]
        row = state["groups"][0]["features"][0]["rows"][0]
        assert row["status"] == "closed"
        assert row["reason"] == "manual"

    def test_refuses_role_with_a_live_pane(self) -> None:
        task_ledger.create_assignment(PROJECT, "qa", "/api", "smoke", "goal", "feat", "claude")

        ok, msg = task_ledger.close_role(PROJECT, "qa", frozenset({"qa"}))

        assert ok is False
        assert "live pane" in msg
        state = task_ledger._load_state(PROJECT)
        assert "qa" in state["open"]  # untouched

    def test_force_overrides_live_pane_guard(self) -> None:
        task_ledger.create_assignment(PROJECT, "qa", "/api", "smoke", "goal", "feat", "claude")

        ok, _msg = task_ledger.close_role(PROJECT, "qa", frozenset({"qa"}), force=True)

        assert ok is True
        state = task_ledger._load_state(PROJECT)
        assert "qa" not in state["open"]

    def test_role_with_no_open_row_errors(self) -> None:
        ok, msg = task_ledger.close_role(PROJECT, "nope", frozenset())
        assert ok is False
        assert "no open ledger row" in msg


class TestAtomicWriteWindowsRetry:
    """`_atomic_write` retries `os.replace` on PermissionError (Windows
    transient lock, CI flake in test_orchestrator_shard) — bounded, and the
    last failure still propagates so callers' OSError warnings keep working."""

    def test_retries_then_succeeds(self, tmp_path, monkeypatch):
        from agent_takkub import task_ledger

        calls = {"n": 0}
        real_replace = task_ledger.os.replace

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError(5, "Access is denied")
            return real_replace(src, dst)

        monkeypatch.setattr(task_ledger.os, "replace", flaky)
        monkeypatch.setattr(task_ledger, "_REPLACE_RETRY_SLEEP_S", 0.0)
        target = tmp_path / "state.json"
        task_ledger._atomic_write(target, "{}")
        assert target.read_text(encoding="utf-8") == "{}"
        assert calls["n"] == 3

    def test_gives_up_after_bounded_retries(self, tmp_path, monkeypatch):
        import pytest

        from agent_takkub import task_ledger

        calls = {"n": 0}

        def always(src, dst):
            calls["n"] += 1
            raise PermissionError(5, "Access is denied")

        monkeypatch.setattr(task_ledger.os, "replace", always)
        monkeypatch.setattr(task_ledger, "_REPLACE_RETRY_SLEEP_S", 0.0)
        with pytest.raises(PermissionError):
            task_ledger._atomic_write(tmp_path / "state.json", "{}")
        assert calls["n"] == task_ledger._REPLACE_RETRIES
