"""`obsidian_metadata` — canonical note metadata (#365 phase 8)."""

from __future__ import annotations

from datetime import datetime

from agent_takkub.obsidian_metadata import (
    TRUST_AUTO,
    NoteMetadata,
    content_hash,
    make_knowledge_id,
)

_NOW = datetime(2026, 8, 23, 12, 0, 0)


class TestContentHash:
    def test_deterministic(self):
        assert content_hash("hello") == content_hash("hello")

    def test_differs_on_content_change(self):
        assert content_hash("hello") != content_hash("hello!")


class TestMakeKnowledgeId:
    def test_deterministic_same_inputs_same_id(self):
        a = make_knowledge_id("proj", "takkub-done", "session", "fixed the bug")
        b = make_knowledge_id("proj", "takkub-done", "session", "fixed the bug")
        assert a == b

    def test_differs_by_project_id(self):
        a = make_knowledge_id("proj-a", "takkub-done", "session", "same text")
        b = make_knowledge_id("proj-b", "takkub-done", "session", "same text")
        assert a != b

    def test_differs_by_kind(self):
        a = make_knowledge_id("proj", "distill", "fact", "same text")
        b = make_knowledge_id("proj", "distill", "moc", "same text")
        assert a != b

    def test_differs_by_content(self):
        a = make_knowledge_id("proj", "distill", "fact", "text one")
        b = make_knowledge_id("proj", "distill", "fact", "text two")
        assert a != b

    def test_short_and_hex(self):
        kid = make_knowledge_id("proj", "src", "kind", "text")
        assert len(kid) == 16
        int(kid, 16)  # raises ValueError if not hex


class TestNoteMetadata:
    def test_new_sets_created_equal_updated(self):
        meta = NoteMetadata.new(
            project_id="proj",
            source="takkub-done",
            kind="session",
            trust=TRUST_AUTO,
            text="body",
            now=_NOW,
        )
        assert meta.created_at == meta.updated_at == _NOW.isoformat(timespec="seconds")
        assert meta.project_id == "proj"
        assert meta.content_hash == content_hash("body")
        assert meta.knowledge_id == make_knowledge_id("proj", "takkub-done", "session", "body")

    def test_touched_keeps_identity_bumps_updated_at(self):
        meta = NoteMetadata.new(
            project_id="proj", source="s", kind="k", trust=TRUST_AUTO, text="body", now=_NOW
        )
        later = datetime(2026, 8, 23, 13, 0, 0)
        touched = meta.touched(now=later)
        assert touched.knowledge_id == meta.knowledge_id
        assert touched.created_at == meta.created_at
        assert touched.updated_at == later.isoformat(timespec="seconds")

    def test_frontmatter_lines_contain_all_canonical_fields(self):
        meta = NoteMetadata.new(
            project_id="proj", source="s", kind="k", trust=TRUST_AUTO, text="body", now=_NOW
        )
        lines = meta.frontmatter_lines()
        joined = "\n".join(lines)
        for field in (
            "knowledge_id",
            "project_id",
            "source",
            "kind",
            "trust",
            "content_hash",
            "created_at",
            "updated_at",
        ):
            assert f"{field}:" in joined
        # no stray frontmatter delimiters — caller wraps these lines itself
        assert not any(line.strip() == "---" for line in lines)
