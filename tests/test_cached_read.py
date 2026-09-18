"""#386: stat-validated read cache for the small config/state files the Qt
main thread polls on a timer — unchanged files must cost one `stat`, and a
changed file must never serve a stale parse."""

from __future__ import annotations

import json
import os
import time

import pytest

from agent_takkub import cached_read


@pytest.fixture(autouse=True)
def _exact_stat(monkeypatch: pytest.MonkeyPatch):
    """The #386 contracts below are about the stat-validated cache being
    EXACT; #658's stat-skip TTL (tested separately in `TestStatTtl`) is
    switched off here so a rewrite/delete is observed on the very next read."""
    monkeypatch.setattr(cached_read, "_STAT_TTL_S", 0.0)
    cached_read.invalidate()


def _old(path, seconds=10) -> None:
    """Push mtime into the past so the recent-write guard does not force a re-read."""
    t = time.time() - seconds
    os.utime(path, (t, t))


def test_missing_file_returns_missing_and_caches_nothing(tmp_path) -> None:
    p = tmp_path / "nope.json"
    assert cached_read.read_cached(p, json.loads, missing={}) == {}
    assert cached_read.read_cached(p, json.loads, missing=None) is None


def test_unchanged_file_is_parsed_once(tmp_path) -> None:
    p = tmp_path / "a.json"
    p.write_text('{"x": 1}', encoding="utf-8")
    _old(p)
    calls = []

    def parse(text):
        calls.append(text)
        return json.loads(text)

    assert cached_read.read_cached(p, parse) == {"x": 1}
    assert cached_read.read_cached(p, parse) == {"x": 1}
    assert cached_read.read_cached(p, parse) == {"x": 1}
    assert len(calls) == 1


def test_changed_file_is_reread(tmp_path) -> None:
    p = tmp_path / "a.json"
    p.write_text('{"x": 1}', encoding="utf-8")
    _old(p, 20)
    assert cached_read.read_cached(p, json.loads) == {"x": 1}
    p.write_text('{"x": 2}', encoding="utf-8")
    _old(p, 10)  # different mtime, same size → signature moved
    assert cached_read.read_cached(p, json.loads) == {"x": 2}


def test_recently_written_file_is_always_reread(tmp_path) -> None:
    """A rewrite inside the filesystem's mtime resolution with the same byte
    length must not be served stale — anything modified in the last
    `_RECENT_WRITE_S` bypasses the cache."""
    p = tmp_path / "a.json"
    p.write_text('{"x": 1}', encoding="utf-8")
    assert cached_read.read_cached(p, json.loads) == {"x": 1}
    p.write_text('{"x": 2}', encoding="utf-8")
    assert cached_read.read_cached(p, json.loads) == {"x": 2}


def test_deleted_file_drops_entry(tmp_path) -> None:
    p = tmp_path / "a.json"
    p.write_text('{"x": 1}', encoding="utf-8")
    _old(p)
    assert cached_read.read_cached(p, json.loads) == {"x": 1}
    p.unlink()
    assert cached_read.read_cached(p, json.loads, missing="gone") == "gone"


def test_parse_error_propagates_and_is_not_cached(tmp_path) -> None:
    p = tmp_path / "a.json"
    p.write_text("{bad", encoding="utf-8")
    _old(p)
    try:
        cached_read.read_cached(p, json.loads)
    except json.JSONDecodeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected JSONDecodeError")
    p.write_text('{"x": 1}', encoding="utf-8")
    assert cached_read.read_cached(p, json.loads) == {"x": 1}


def test_invalidate(tmp_path) -> None:
    p = tmp_path / "a.json"
    p.write_text('{"x": 1}', encoding="utf-8")
    _old(p)
    calls = []

    def parse(text):
        calls.append(1)
        return json.loads(text)

    cached_read.read_cached(p, parse)
    cached_read.invalidate(p)
    cached_read.read_cached(p, parse)
    cached_read.invalidate()
    cached_read.read_cached(p, parse)
    assert len(calls) == 3


class TestStatTtl:
    """#658: a file whose last stat showed no recent write is served for
    `_STAT_TTL_S` without touching the filesystem — the main thread re-reads
    the same few config files many times per tick, and on a wedged disk even
    the stat blocked ~1 s."""

    def test_old_file_served_without_stat_inside_ttl(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(cached_read, "_STAT_TTL_S", 3.0)
        p = tmp_path / "a.json"
        p.write_text('{"x": 1}', encoding="utf-8")
        _old(p)
        assert cached_read.read_cached(p, json.loads) == {"x": 1}
        stats = []
        real_stat = os.stat

        def counting_stat(path, *a, **kw):
            stats.append(path)
            return real_stat(path, *a, **kw)

        monkeypatch.setattr(cached_read.os, "stat", counting_stat)
        assert cached_read.read_cached(p, json.loads) == {"x": 1}
        assert cached_read.read_cached(p, json.loads) == {"x": 1}
        assert stats == []

    def test_recently_written_file_never_skips_the_stat(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(cached_read, "_STAT_TTL_S", 3.0)
        p = tmp_path / "a.json"
        p.write_text('{"x": 1}', encoding="utf-8")  # mtime = now → "recent"
        assert cached_read.read_cached(p, json.loads) == {"x": 1}
        p.write_text('{"x": 2}', encoding="utf-8")
        assert cached_read.read_cached(p, json.loads) == {"x": 2}

    def test_invalidate_makes_an_in_process_write_visible_at_once(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(cached_read, "_STAT_TTL_S", 3.0)
        p = tmp_path / "a.json"
        p.write_text('{"x": 1}', encoding="utf-8")
        _old(p)
        assert cached_read.read_cached(p, json.loads) == {"x": 1}
        p.write_text('{"x": 2}', encoding="utf-8")
        _old(p, 5)
        # inside the TTL, without invalidate the stale parse would be served
        cached_read.invalidate(p)
        assert cached_read.read_cached(p, json.loads) == {"x": 2}
