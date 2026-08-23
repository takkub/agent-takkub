"""`obsidian_dedup` — persistent cross-restart dedup index (#365 phase 8
improvement 2): same upsert-log shape `core.accounts.registry` already
uses, keyed by `knowledge_id`."""

from __future__ import annotations

from agent_takkub import obsidian_dedup
from agent_takkub.core.storage.jsonl_store import JsonlStore
from agent_takkub.obsidian_dedup import DedupIndex


def _index(tmp_path) -> DedupIndex:
    return DedupIndex(JsonlStore(tmp_path / "obsidian_dedup.jsonl"))


_RECORD = dict(
    path="/vault/01-Projects/demo.md",
    project_id="demo",
    source="distill",
    kind="fact",
    content_hash="abc123",
    created_at="2026-08-23T12:00:00",
    updated_at="2026-08-23T12:00:00",
)


class TestDedupIndex:
    def test_lookup_missing_returns_none(self, tmp_path):
        assert _index(tmp_path).lookup("nope") is None

    def test_record_then_lookup_round_trips(self, tmp_path):
        idx = _index(tmp_path)
        idx.record_write("kid1", **_RECORD)
        found = idx.lookup("kid1")
        assert found is not None
        assert found["path"] == _RECORD["path"]
        assert found["project_id"] == "demo"

    def test_repeat_write_updates_not_duplicates(self, tmp_path):
        """A second `record_write` for the same knowledge_id must be an
        UPDATE (latest record wins), never a second live entry — the
        exact 'เขียนซ้ำ = update ไม่ใช่ไฟล์ใหม่' contract from the phase-8
        task."""
        idx = _index(tmp_path)
        idx.record_write("kid1", **_RECORD)
        updated = dict(_RECORD, updated_at="2026-08-24T09:00:00")
        idx.record_write("kid1", **updated)
        assert idx.count() == 1
        assert idx.lookup("kid1")["updated_at"] == "2026-08-24T09:00:00"

    def test_count_excludes_tombstones(self, tmp_path):
        idx = _index(tmp_path)
        idx.record_write("kid1", **_RECORD)
        idx._store.append({"id": "kid1", "deleted": True})
        assert idx.count() == 0
        assert idx.lookup("kid1") is None

    def test_all_records_latest_per_id_only(self, tmp_path):
        idx = _index(tmp_path)
        idx.record_write("kid1", **_RECORD)
        idx.record_write("kid2", **dict(_RECORD, path="/vault/01-Projects/other.md"))
        records = idx.all_records()
        assert {r["id"] for r in records} == {"kid1", "kid2"}
        assert len(records) == 2

    def test_survives_across_separate_index_instances(self, tmp_path):
        """Persistent across restart: a NEW `DedupIndex` instance pointed
        at the same store must see records a prior instance wrote."""
        store_path = tmp_path / "obsidian_dedup.jsonl"
        DedupIndex(JsonlStore(store_path)).record_write("kid1", **_RECORD)
        reopened = DedupIndex(JsonlStore(store_path))
        assert reopened.lookup("kid1") is not None


class TestModuleLevelWrappers:
    """Module-level convenience functions resolve through the isolated
    RUNTIME_DIR `core_home()` fallback (autouse `_isolate_runtime`
    fixture) — no leakage into the real machine's dedup index."""

    def test_record_and_lookup_round_trip(self):
        obsidian_dedup.record_write("kid-wrapper", **_RECORD)
        found = obsidian_dedup.lookup("kid-wrapper")
        assert found is not None
        assert found["project_id"] == "demo"
        assert obsidian_dedup.count() >= 1
