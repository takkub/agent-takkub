"""Tests for bm25_search — the ranked search backing `takkub search` (#152).

Pins: mixed ASCII/Thai tokenizer output, BM25 ranks a more-relevant doc
above a less-relevant one for Thai-only and mixed Thai/English queries,
role-memory archive lines join the corpus, and the grep fallback fires on
short queries / punctuation-only queries / indexing failures.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from agent_takkub import bm25_search
from agent_takkub.bm25_search import search, tokenize


def _write_jsonl(path: pathlib.Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def _user_rec(text: str, ts: str) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        "timestamp": ts,
    }


class TestTokenize:
    def test_ascii_words_lowercased(self) -> None:
        assert tokenize("Fix the Bug") == ["fix", "the", "bug"]

    def test_thai_run_becomes_trigrams(self) -> None:
        # 6-char Thai run -> sliding-window trigrams, 4 of them.
        toks = tokenize("สวัสดีครับ")
        assert len(toks) == len("สวัสดีครับ") - 2
        assert all(len(t) == 3 for t in toks)

    def test_short_thai_run_kept_whole(self) -> None:
        assert tokenize("คน") == ["คน"]

    def test_mixed_thai_english_order_preserved(self) -> None:
        toks = tokenize("fix บั๊ก now")
        assert toks[0] == "fix"
        assert toks[-1] == "now"
        assert any(len(t) <= 3 and t not in ("fix", "now") for t in toks)

    def test_empty_text(self) -> None:
        assert tokenize("") == []


class TestSearchRanking:
    def test_more_relevant_doc_ranks_first_english(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-foo"
        proj.mkdir(parents=True)
        _write_jsonl(
            proj / "s.jsonl",
            [
                _user_rec("bracketed paste bracketed paste terminal bug", "2026-05-17T10:00:00Z"),
                _user_rec("mentions paste once in passing", "2026-05-17T10:01:00Z"),
            ],
        )
        hits, used_bm25 = search("bracketed paste")
        assert used_bm25 is True
        assert len(hits) == 2
        assert "bracketed paste" in hits[0]["snippet"]
        assert hits[0]["score"] >= hits[1]["score"]

    def test_thai_only_query_ranks_matching_doc_first(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-foo"
        proj.mkdir(parents=True)
        _write_jsonl(
            proj / "s.jsonl",
            [
                _user_rec("แก้บั๊กเรื่องการค้นหาข้อมูลในระบบ", "2026-05-17T10:00:00Z"),
                _user_rec("วันนี้อากาศดีมากเลยครับ", "2026-05-17T10:01:00Z"),
            ],
        )
        hits, used_bm25 = search("ค้นหาข้อมูล")
        assert used_bm25 is True
        assert len(hits) >= 1
        assert "ค้นหา" in hits[0]["snippet"]

    def test_mixed_thai_english_query(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-foo"
        proj.mkdir(parents=True)
        _write_jsonl(
            proj / "s.jsonl",
            [
                _user_rec("แก้ endpoint search ให้ใช้ bm25", "2026-05-17T10:00:00Z"),
                _user_rec("ไม่เกี่ยวข้องเลย unrelated stuff", "2026-05-17T10:01:00Z"),
            ],
        )
        hits, used_bm25 = search("endpoint search bm25")
        assert used_bm25 is True
        assert hits[0]["score"] > 0
        assert "endpoint" in hits[0]["snippet"] or "bm25" in hits[0]["snippet"]

    def test_project_filter_narrows(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        base = tmp_path / ".claude" / "projects"
        keep = base / "C--Users-alice-agent-takkub"
        skip = base / "C--Users-alice-other"
        for d in (keep, skip):
            d.mkdir(parents=True)
        _write_jsonl(
            keep / "s.jsonl", [_user_rec("playwright setup notes", "2026-05-17T10:00:00Z")]
        )
        _write_jsonl(
            skip / "s.jsonl", [_user_rec("playwright setup notes", "2026-05-17T10:00:00Z")]
        )
        hits, used_bm25 = search("playwright setup", project_filter="agent-takkub")
        assert used_bm25 is True
        assert len(hits) == 1
        assert "agent-takkub" in hits[0]["project"]

    def test_limit_caps_results(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-foo"
        proj.mkdir(parents=True)
        recs = [_user_rec(f"hit number {i}", f"2026-05-17T10:{i:02d}:00Z") for i in range(10)]
        _write_jsonl(proj / "s.jsonl", recs)
        hits, used_bm25 = search("hit number", limit=3)
        assert used_bm25 is True
        assert len(hits) == 3

    def test_no_matches_returns_empty_bm25(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        hits, used_bm25 = search("nothing indexed anywhere")
        assert hits == []
        assert used_bm25 is True


class TestArchiveCorpus:
    def test_archive_lines_are_searchable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        role_mem = tmp_path / "role-memory"
        monkeypatch.setattr(bm25_search, "ROLE_MEMORY_DIR", role_mem)
        proj_dir = role_mem / "agent-takkub"
        proj_dir.mkdir(parents=True)
        (proj_dir / "backend-archive.md").write_text(
            "- old note about nothing\n"
            "- learned that the BM25 index skips empty archive lines correctly\n",
            encoding="utf-8",
        )
        hits, used_bm25 = search("BM25 index skips empty archive lines")
        assert used_bm25 is True
        assert len(hits) >= 1
        assert hits[0]["role"] == "archive"
        assert hits[0]["line"] == 2
        assert "backend-archive.md" in hits[0]["path"]

    def test_missing_archive_dir_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(bm25_search, "ROLE_MEMORY_DIR", tmp_path / "does-not-exist")
        hits, used_bm25 = search("anything at all here")
        assert hits == []
        assert used_bm25 is True


class TestFallback:
    def test_short_query_falls_back_to_grep(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-foo"
        proj.mkdir(parents=True)
        _write_jsonl(proj / "s.jsonl", [_user_rec("ci build fixed", "2026-05-17T10:00:00Z")])
        hits, used_bm25 = search("ci")
        assert used_bm25 is False
        assert len(hits) == 1
        assert "score" not in hits[0]

    def test_punctuation_only_query_falls_back_to_grep(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        hits, used_bm25 = search("!!!???")
        assert used_bm25 is False
        assert hits == []

    def test_indexing_failure_falls_back_to_grep(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-foo"
        proj.mkdir(parents=True)
        _write_jsonl(proj / "s.jsonl", [_user_rec("bracketed paste bug", "2026-05-17T10:00:00Z")])

        def _boom(*_a, **_k):
            raise RuntimeError("index blew up")

        monkeypatch.setattr(bm25_search, "_bm25_rank", _boom)
        hits, used_bm25 = search("bracketed paste")
        assert used_bm25 is False
        assert len(hits) == 1
        assert "score" not in hits[0]


class TestSnippet:
    def test_snippet_falls_back_to_head_when_no_literal_match(self) -> None:
        text = "z" * 300
        assert bm25_search._snippet(text, "notpresent") == text[:200]
