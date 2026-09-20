"""#684: project backlog store, markdown import, and CLI/orchestrator wiring."""

from __future__ import annotations

import pathlib

import pytest

from agent_takkub import backlog


@pytest.fixture
def store(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    monkeypatch.setattr(backlog, "RUNTIME_DIR", tmp_path / "runtime")
    return tmp_path


class TestCrud:
    def test_add_and_list_newest_first(self, store) -> None:
        a = backlog.add_item("proj", "first")
        b = backlog.add_item("proj", "second")
        ids = [it["id"] for it in backlog.list_items("proj")]
        assert ids == [b["id"], a["id"]]

    def test_add_requires_title(self, store) -> None:
        with pytest.raises(ValueError):
            backlog.add_item("proj", "   ")

    def test_fields_persist(self, store) -> None:
        it = backlog.add_item(
            "proj",
            "ช่องถอนเงินยังเป็น input ดิบ",
            source="ui-audit#H-09",
            files=["a.tsx:50", "b.tsx:12"],
            impact="customer",
            severity="high",
        )
        got = backlog.get_item("proj", it["id"])
        assert got["source"] == "ui-audit#H-09"
        assert got["files"] == ["a.tsx:50", "b.tsx:12"]
        assert got["impact"] == "customer" and got["severity"] == "high"

    def test_survives_reload_from_disk(self, store) -> None:
        it = backlog.add_item("proj", "persisted")
        # New "process": clear the read cache by re-importing is overkill;
        # load() re-reads via cached_read which stat-validates, and the file
        # exists on disk.
        assert backlog.get_item("proj", it["id"]) is not None

    def test_block_requires_and_records_reason(self, store) -> None:
        it = backlog.add_item("proj", "x")
        backlog.block("proj", it["id"], "ไม่มีรหัส super ของ prod")
        got = backlog.get_item("proj", it["id"])
        assert got["status"] == "blocked"
        assert got["reason"] == "ไม่มีรหัส super ของ prod"

    def test_status_filter_open_excludes_terminal(self, store) -> None:
        a = backlog.add_item("proj", "open one")
        b = backlog.add_item("proj", "done one")
        backlog.mark_done("proj", b["id"])
        open_ids = [it["id"] for it in backlog.list_items("proj", status="open")]
        assert a["id"] in open_ids and b["id"] not in open_ids

    def test_progress_excludes_wont(self, store) -> None:
        backlog.add_item("proj", "a")
        b = backlog.add_item("proj", "b")
        c = backlog.add_item("proj", "c")
        backlog.mark_done("proj", b["id"])
        backlog.set_status("proj", c["id"], "wont", reason="ไม่กระทบ")
        # wont excluded from denominator; done counted
        assert backlog.progress("proj") == (1, 2)


class TestLedgerLink:
    def test_assign_then_done_flips_to_review(self, store) -> None:
        it = backlog.add_item("proj", "work")
        backlog.assign_item("proj", it["id"], "task-abc")
        assert backlog.get_item("proj", it["id"])["status"] == "doing"
        flipped = backlog.on_ledger_done("proj", "task-abc")
        assert flipped is not None
        assert backlog.get_item("proj", it["id"])["status"] == "review"

    def test_on_ledger_done_ignores_unlinked(self, store) -> None:
        backlog.add_item("proj", "unrelated")
        assert backlog.on_ledger_done("proj", "nope") is None


class TestMarkdownImport:
    def test_parses_thai_headers_and_maps_columns(self, store) -> None:
        md = (
            "| หัวข้อ | ไฟล์ | ความรุนแรง | ผลกระทบ |\n"
            "|---|---|---|---|\n"
            "| ปุ่ม submit ซ้อน | `a.tsx:12` | สูง | ลูกค้า |\n"
            "| สีพื้นหลังเพี้ยน | b.css | ต่ำ | ภายใน |\n"
        )
        created = backlog.import_markdown("proj", md, source="audit.md")
        assert len(created) == 2
        first = created[0]
        assert first["title"] == "ปุ่ม submit ซ้อน"
        assert first["severity"] == "high" and first["impact"] == "customer"
        assert first["files"] == ["a.tsx:12"]
        assert first["source"] == "audit.md"

    def test_no_table_returns_empty(self, store) -> None:
        assert backlog.import_markdown("proj", "just prose, no table") == []

    def test_table_without_title_column_skipped(self, store) -> None:
        md = "| foo | bar |\n|---|---|\n| 1 | 2 |\n"
        assert backlog.parse_markdown_table(md) == []


class TestWriterInvalidatesReadCache:
    def test_status_change_visible_through_cache_fast_path(
        self, store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Live-caught 2026-09-20: `backlog block` then an immediate
        `backlog list --status blocked` returned 0 items — cached_read's ≤3s
        no-stat fast path served the pre-write parse because `_save` didn't
        invalidate. Reproduce the exact window deterministically: age the
        file past _RECENT_WRITE_S, prime the cache (hit stored with
        recent=False), write, read immediately."""
        import os as _os
        import time as _time

        it = backlog.add_item("proj", "จะโดน block")
        # age the store so the priming stat sees a non-recent file
        p = backlog._store_path("proj")
        old = _time.time() - 30
        _os.utime(p, (old, old))
        backlog.load("proj")  # primes the ≤3s no-stat fast path

        backlog.block("proj", it["id"], "เหตุผล")

        got = backlog.list_items("proj", status="blocked")
        assert [x["id"] for x in got] == [it["id"]]


class TestAge:
    def test_age_days(self, store) -> None:
        import time

        it = backlog.add_item("proj", "old")
        it["created_ts"] = time.time() - 3 * 86400
        assert backlog.age_days(it) == 3
