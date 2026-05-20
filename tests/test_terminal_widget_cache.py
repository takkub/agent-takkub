"""Tests for _FmtCache: LRU-capped cache utility in terminal_widget.

The old QPlainTextEdit pipeline had an unbounded `_fmt_cache` dict (ANSI attrs
→ QTextCharFormat) that grew forever on long cockpit sessions.  The xterm.js
rewrite removed that rendering path, but the LRU utility class lives on in
terminal_widget.py as a general-purpose in-process cache capped at 256 entries.

These tests exercise the cache without instantiating a QApplication.
"""

from __future__ import annotations

import pytest

from agent_takkub.terminal_widget import _FmtCache


class TestFmtCacheBasicStorage:
    def test_stores_and_retrieves_entry(self) -> None:
        cache: _FmtCache[str, int] = _FmtCache(maxsize=4)
        cache["bold"] = 1
        assert cache["bold"] == 1

    def test_missing_key_raises_key_error(self) -> None:
        cache: _FmtCache[str, int] = _FmtCache(maxsize=4)
        with pytest.raises(KeyError):
            _ = cache["nonexistent"]

    def test_contains(self) -> None:
        cache: _FmtCache[str, int] = _FmtCache(maxsize=4)
        cache["x"] = 99
        assert "x" in cache
        assert "y" not in cache

    def test_len(self) -> None:
        cache: _FmtCache[str, int] = _FmtCache(maxsize=8)
        assert len(cache) == 0
        cache["a"] = 1
        cache["b"] = 2
        assert len(cache) == 2


class TestFmtCacheEviction:
    def test_evicts_oldest_when_cap_exceeded(self) -> None:
        cache: _FmtCache[str, int] = _FmtCache(maxsize=3)
        cache["a"] = 1
        cache["b"] = 2
        cache["c"] = 3
        # Adding a 4th entry evicts the LRU (oldest-used) entry "a"
        cache["d"] = 4
        assert len(cache) == 3
        assert "a" not in cache
        assert "b" in cache
        assert "c" in cache
        assert "d" in cache

    def test_size_never_exceeds_maxsize(self) -> None:
        maxsize = 256
        cache: _FmtCache[str, int] = _FmtCache(maxsize=maxsize)
        for i in range(maxsize + 50):
            cache[f"key-{i}"] = i
        assert len(cache) <= maxsize

    def test_eviction_is_fifo_when_no_reads(self) -> None:
        cache: _FmtCache[str, int] = _FmtCache(maxsize=3)
        cache["first"] = 1
        cache["second"] = 2
        cache["third"] = 3
        cache["fourth"] = 4  # evicts "first"
        assert "first" not in cache


class TestFmtCacheLruBehavior:
    def test_read_promotes_entry_prevents_eviction(self) -> None:
        cache: _FmtCache[str, int] = _FmtCache(maxsize=3)
        cache["a"] = 1
        cache["b"] = 2
        cache["c"] = 3
        # Read "a" to make it recently used
        _ = cache["a"]
        # Adding "d" should evict "b" (now the least recently used), not "a"
        cache["d"] = 4
        assert "a" in cache, "recently read 'a' should NOT be evicted"
        assert "b" not in cache, "'b' (LRU) should be evicted"
        assert "c" in cache
        assert "d" in cache

    def test_write_to_existing_key_promotes_entry(self) -> None:
        cache: _FmtCache[str, int] = _FmtCache(maxsize=3)
        cache["a"] = 1
        cache["b"] = 2
        cache["c"] = 3
        # Update "a" — should promote it
        cache["a"] = 100
        cache["d"] = 4  # should evict "b", not "a"
        assert "a" in cache
        assert cache["a"] == 100
        assert "b" not in cache

    def test_repeated_hit_does_not_grow_cache(self) -> None:
        cache: _FmtCache[str, int] = _FmtCache(maxsize=4)
        cache["x"] = 1
        for _ in range(10):
            _ = cache["x"]
        assert len(cache) == 1


class TestFmtCacheDefaultMaxsize:
    def test_default_maxsize_is_256(self) -> None:
        cache: _FmtCache[str, int] = _FmtCache()
        assert cache.maxsize == 256

    def test_default_cache_caps_at_256(self) -> None:
        cache: _FmtCache[str, int] = _FmtCache()
        for i in range(300):
            cache[f"k{i}"] = i
        assert len(cache) == 256
