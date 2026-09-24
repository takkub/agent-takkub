"""#714: backlog is the mandatory entry point for new work.

- every assign runs under a card (linked via --backlog, or auto-created)
- the card follows the task through assign → done (links + bound task ids)
- Lead may not edit project files until a card is `doing`
- starting new work does NOT push the pending list at the owner (it nagged);
  `takkub backlog pending` shows it on demand
"""

from __future__ import annotations

import pathlib
import time

import pytest

from agent_takkub import backlog


@pytest.fixture
def runtime(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    monkeypatch.setattr(backlog, "RUNTIME_DIR", tmp_path / "runtime")
    return tmp_path


class TestTitleFromTask:
    def test_strips_role_tags_and_markdown(self) -> None:
        task = "[lead → backend] [ROLE: backend]\n\n## แก้ปุ่ม login ค้าง\nรายละเอียด"
        assert backlog.title_from_task(task) == "แก้ปุ่ม login ค้าง"

    def test_caps_length(self) -> None:
        title = backlog.title_from_task("ก" * 300)
        assert len(title) == backlog._AUTO_TITLE_MAX and title.endswith("…")

    def test_empty_task(self) -> None:
        assert backlog.title_from_task("   \n") == "(งานไม่มีชื่อ)"


class TestEnsureForAssign:
    def test_auto_creates_doing_card_with_link(self, runtime) -> None:
        item, is_new = backlog.ensure_for_assign("p", "backend", "แก้ API timeout\nรายละเอียด")
        assert is_new
        assert item["status"] == "doing"
        assert item["title"] == "แก้ API timeout"
        assert "รายละเอียด" in item["detail"]
        assert [ln["role"] for ln in item["links"]] == ["backend"]

    def test_shard_requests_share_one_card(self, runtime) -> None:
        a, new_a = backlog.ensure_for_assign("p", "qa#1", "ทดสอบหน้า checkout")
        b, new_b = backlog.ensure_for_assign("p", "qa#2", "ทดสอบหน้า checkout")
        assert a["id"] == b["id"]
        assert (new_a, new_b) == (True, False)
        assert [ln["role"] for ln in backlog.get_item("p", a["id"])["links"]] == ["qa#1", "qa#2"]

    def test_same_task_after_window_is_a_new_card(self, runtime, monkeypatch) -> None:
        a, _ = backlog.ensure_for_assign("p", "backend", "งานเดิม")
        later = time.time() + backlog._AUTO_REUSE_WINDOW_S + 5
        monkeypatch.setattr(backlog, "_now", lambda: later)
        b, is_new = backlog.ensure_for_assign("p", "backend", "งานเดิม")
        assert b["id"] != a["id"] and is_new

    def test_explicit_id_links_existing_card(self, runtime) -> None:
        card = backlog.add_item("p", "งานจาก audit")
        item, is_new = backlog.ensure_for_assign("p", "frontend", "whatever", card["id"])
        assert item["id"] == card["id"] and is_new
        assert item["status"] == "doing"
        # assigning the same doing card again (fix loop) is not new work
        _again, is_new_again = backlog.ensure_for_assign("p", "frontend", "fix", card["id"])
        assert not is_new_again

    def test_explicit_id_must_exist_and_be_open(self, runtime) -> None:
        with pytest.raises(ValueError, match="ไม่พบ"):
            backlog.ensure_for_assign("p", "backend", "x", "nope")
        card = backlog.add_item("p", "ปิดแล้ว")
        backlog.mark_done("p", card["id"])
        with pytest.raises(ValueError, match="ปิดไปแล้ว"):
            backlog.ensure_for_assign("p", "backend", "x", card["id"])


class TestTaskBinding:
    def test_bind_then_done_flips_review(self, runtime) -> None:
        item, _ = backlog.ensure_for_assign("p", "backend", "งาน")
        backlog.bind_task_id("p", "backend", "t1")
        backlog.on_ledger_done("p", "t1")
        assert backlog.get_item("p", item["id"])["status"] == "review"

    def test_reroute_moves_link_to_new_task_id(self, runtime) -> None:
        item, _ = backlog.ensure_for_assign("p", "backend", "งาน")
        backlog.bind_task_id("p", "backend", "t1")
        # quota reroute re-assigns the same work: new task id, no new card
        backlog.bind_task_id("p", "backend", "t2")
        backlog.on_ledger_done("p", "t2")
        assert backlog.get_item("p", item["id"])["status"] == "review"

    def test_new_card_for_same_role_gets_the_new_task_id(self, runtime) -> None:
        old, _ = backlog.ensure_for_assign("p", "backend", "งานแรก")
        backlog.bind_task_id("p", "backend", "t1")
        new, _ = backlog.ensure_for_assign("p", "backend", "งานที่สอง")
        backlog.bind_task_id("p", "backend", "t2")
        assert backlog.get_item("p", old["id"])["links"][0]["task_id"] == "t1"
        assert backlog.get_item("p", new["id"])["links"][0]["task_id"] == "t2"

    def test_shards_flip_only_when_every_shard_done(self, runtime) -> None:
        item, _ = backlog.ensure_for_assign("p", "qa#1", "ทดสอบ")
        backlog.ensure_for_assign("p", "qa#2", "ทดสอบ")
        backlog.bind_task_id("p", "qa#1", "s1")
        backlog.bind_task_id("p", "qa#2", "s2")
        backlog.on_ledger_done("p", "s1")
        assert backlog.get_item("p", item["id"])["status"] == "doing"
        backlog.on_ledger_done("p", "s2")
        assert backlog.get_item("p", item["id"])["status"] == "review"

    def test_legacy_ledger_task_id_still_flips(self, runtime) -> None:
        card = backlog.add_item("p", "เก่า")
        backlog.assign_item("p", card["id"], "legacy")
        backlog.on_ledger_done("p", "legacy")
        assert backlog.get_item("p", card["id"])["status"] == "review"


class TestPendingReport:
    def test_lists_other_pending_including_deferred(self, runtime) -> None:
        a = backlog.add_item("p", "ค้างอยู่")
        b = backlog.add_item("p", "พักไว้")
        backlog.defer("p", b["id"])
        c = backlog.add_item("p", "เสร็จแล้ว")
        backlog.mark_done("p", c["id"])
        started, _ = backlog.ensure_for_assign("p", "backend", "งานใหม่")
        report = backlog.pending_report("p", exclude_ids=(started["id"],))
        assert "งานค้างใน backlog 2 ใบ" in report
        assert a["id"] in report and b["id"] in report
        assert c["id"] not in report and started["id"] not in report

    def test_nothing_pending_is_empty(self, runtime) -> None:
        started, _ = backlog.ensure_for_assign("p", "backend", "งานเดียว")
        assert backlog.pending_report("p", exclude_ids=(started["id"],)) == ""

    def test_list_pending_filter(self, runtime) -> None:
        b = backlog.add_item("p", "พักไว้")
        backlog.defer("p", b["id"])
        assert [it["id"] for it in backlog.list_items("p", status="pending")] == [b["id"]]
        assert backlog.list_items("p", status="open") == []


class TestStartLeadWork:
    def test_title_creates_doing_card(self, runtime) -> None:
        item, is_new = backlog.start_lead_work("p", title="แก้เอง")
        assert is_new and item["status"] == "doing"
        assert backlog.has_active_item("p")

    def test_needs_id_or_title(self, runtime) -> None:
        with pytest.raises(ValueError):
            backlog.start_lead_work("p")


class _BacklogOrch:
    """Real backlog_for_assign/backlog_command on a minimal host."""

    def __init__(self) -> None:
        from agent_takkub.orchestrator import Orchestrator

        self.backlog_for_assign = Orchestrator.backlog_for_assign.__get__(self)
        self.backlog_command = Orchestrator.backlog_command.__get__(self)

    @staticmethod
    def _resolve_project(project):
        return project or "default"


class TestNoPendingNag:
    def test_new_work_with_pending_does_not_nag(self, runtime) -> None:
        orch = _BacklogOrch()
        backlog.add_item("p", "งานค้างเก่า")
        ok, note, item_id = orch.backlog_for_assign("p", "backend", "งานใหม่")
        assert ok and item_id
        assert "สร้างใบให้อัตโนมัติ" in note
        assert "งานค้าง" not in note and "แจ้ง user" not in note

    def test_bad_backlog_id_fails_the_assign(self, runtime) -> None:
        orch = _BacklogOrch()
        ok, note, _ = orch.backlog_for_assign("p", "backend", "x", "missing")
        assert not ok and "ไม่พบ" in note

    def test_backlog_start_verb_does_not_nag(self, runtime) -> None:
        orch = _BacklogOrch()
        old = backlog.add_item("p", "ค้าง")
        ok, msg, payload = orch.backlog_command("start", {"title": "Lead ทำเอง"}, project="p")
        assert ok and payload["id"]
        assert old["id"] not in msg

    def test_pending_verb_still_lists_on_demand(self, runtime) -> None:
        orch = _BacklogOrch()
        old = backlog.add_item("p", "ค้าง")
        ok, msg, _ = orch.backlog_command("pending", {}, project="p")
        assert ok and old["id"] in msg


class TestLeadEditGate:
    def _edit(self, tmp_path, project="p"):
        from agent_takkub import pane_guard

        src = tmp_path / "proj" / "app.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("x = 1\n", encoding="utf-8")
        return pane_guard.evaluate_lead_direct_edit(
            "Edit",
            {"file_path": str(src), "old_string": "x = 1", "new_string": "x = 2"},
            cwd=str(src.parent),
            project=project,
            scope="tiny",
            state_file=tmp_path / "lead_edits.json",
        )

    def _roots(self, monkeypatch, tmp_path):
        from agent_takkub import lead_context

        monkeypatch.setattr(
            lead_context, "_allowed_project_roots", lambda _p: [(tmp_path / "proj").resolve()]
        )

    def test_denied_without_a_doing_card(self, runtime, tmp_path, monkeypatch) -> None:
        self._roots(monkeypatch, tmp_path)
        verdict = self._edit(tmp_path)
        assert not verdict.allowed
        assert verdict.rule == "lead_direct_edit:no_backlog_card"
        assert "takkub backlog start" in verdict.reason

    def test_allowed_once_a_card_is_doing(self, runtime, tmp_path, monkeypatch) -> None:
        self._roots(monkeypatch, tmp_path)
        backlog.start_lead_work("p", title="แก้เอง")
        verdict = self._edit(tmp_path)
        assert verdict.rule != "lead_direct_edit:no_backlog_card"

    def test_solo_lead_preset_is_not_exempt(self, runtime, tmp_path, monkeypatch) -> None:
        from agent_takkub import team_preset

        self._roots(monkeypatch, tmp_path)
        monkeypatch.setattr(team_preset, "lead_may_implement", lambda _p: True)
        assert self._edit(tmp_path).rule == "lead_direct_edit:no_backlog_card"
        backlog.start_lead_work("p", title="แก้เอง")
        assert self._edit(tmp_path).allowed

    def test_notes_and_outside_root_files_stay_exempt(self, runtime, tmp_path, monkeypatch) -> None:
        from agent_takkub import pane_guard

        self._roots(monkeypatch, tmp_path)
        note = pane_guard.evaluate_lead_direct_edit(
            "Write", {"file_path": str(tmp_path / "proj" / "plan.md"), "content": "x"}, project="p"
        )
        assert note.allowed
        scratch = tmp_path / "scratch" / "task.py"
        outside = pane_guard.evaluate_lead_direct_edit(
            "Write", {"file_path": str(scratch), "content": "x"}, cwd=str(tmp_path), project="p"
        )
        assert outside.rule != "lead_direct_edit:no_backlog_card"
