"""Tests for token_meter.read_last_usage — the tail-read fast path that
replaced streaming the whole session JSONL on the Qt main thread every 5 s
(periodic UI hitch; see docs/cockpit-freeze-rca-2026-05-29.md). Correctness is
preserved by a full-scan fallback when the tail holds no assistant turn.
"""

from __future__ import annotations

import json
import pathlib

from agent_takkub.token_meter import (
    _TAIL_SCAN_BYTES,
    encode_path_for_claude,
    read_last_usage,
)


class TestEncodePathForClaude:
    """The token badge finds a pane's session JSONL by reproducing Claude's
    project-dir encoding. A mismatch = silent missing badge (the bug where
    projects with '_' in the path, e.g. line_websupport, never showed)."""

    def test_underscore_becomes_dash(self) -> None:
        # This is the regression: '_' MUST encode to '-' like Claude does.
        enc = encode_path_for_claude("C:/Users/monch/WebstormProjects/line_websupport/client")
        assert "line-websupport-client" in enc
        assert "_" not in enc

    def test_dot_becomes_dash(self) -> None:
        enc = encode_path_for_claude("C:/Users/monch/.claude-monitor/x")
        assert "." not in enc
        assert "-claude-monitor-x" in enc

    def test_separators_and_drive(self) -> None:
        enc = encode_path_for_claude("C:/Users/monch/WebstormProjects/agent-takkub")
        assert enc == "C--Users-monch-WebstormProjects-agent-takkub"

    def test_only_alnum_and_dash_remain(self) -> None:
        enc = encode_path_for_claude("C:/a_b.c/d e/f")
        assert set(enc) <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")


def _assistant(model: str, inp: int, cc: int, cr: int, out: int) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "model": model,
                "usage": {
                    "input_tokens": inp,
                    "cache_creation_input_tokens": cc,
                    "cache_read_input_tokens": cr,
                    "output_tokens": out,
                },
            },
        }
    )


def _user(text: str) -> str:
    return json.dumps({"type": "user", "message": {"content": text}})


class TestReadLastUsage:
    def test_returns_last_assistant_turn(self, tmp_path: pathlib.Path) -> None:
        f = tmp_path / "s.jsonl"
        f.write_text(
            _assistant("claude-a", 10, 0, 0, 5)
            + "\n"
            + _assistant("claude-b", 100, 20, 30, 7)
            + "\n",
            encoding="utf-8",
        )
        u = read_last_usage(f)
        assert u is not None
        assert u["model"] == "claude-b"
        assert u["prompt"] == 150  # 100 + 20 + 30
        assert u["total"] == 157
        assert u["output"] == 7

    def test_tail_fast_path_large_file(self, tmp_path: pathlib.Path) -> None:
        f = tmp_path / "big.jsonl"
        with open(f, "w", encoding="utf-8") as fh:
            # >512 KiB of filler so the tail window is exercised...
            filler = _user("x" * 500) + "\n"
            written = 0
            while written < _TAIL_SCAN_BYTES + 100_000:
                fh.write(filler)
                written += len(filler)
            # ...then the real last assistant turn at EOF.
            fh.write(_assistant("claude-final", 1000, 0, 0, 42) + "\n")
        assert f.stat().st_size > _TAIL_SCAN_BYTES
        u = read_last_usage(f)
        assert u is not None
        assert u["model"] == "claude-final"
        assert u["prompt"] == 1000
        assert u["output"] == 42

    def test_fallback_full_scan_when_no_assistant_in_tail(self, tmp_path: pathlib.Path) -> None:
        # Assistant turn at the very start, then >512 KiB of user lines after it
        # so the tail window contains NO assistant line — the full-scan fallback
        # must still find the early turn (correctness preserved).
        f = tmp_path / "front.jsonl"
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(_assistant("claude-early", 77, 0, 0, 3) + "\n")
            filler = _user("y" * 500) + "\n"
            written = 0
            while written < _TAIL_SCAN_BYTES + 100_000:
                fh.write(filler)
                written += len(filler)
        assert f.stat().st_size > _TAIL_SCAN_BYTES
        u = read_last_usage(f)
        assert u is not None
        assert u["model"] == "claude-early"
        assert u["prompt"] == 77

    def test_missing_file_returns_none(self, tmp_path: pathlib.Path) -> None:
        assert read_last_usage(tmp_path / "nope.jsonl") is None

    def test_no_assistant_turns_returns_none(self, tmp_path: pathlib.Path) -> None:
        f = tmp_path / "u.jsonl"
        f.write_text(_user("hi") + "\n" + _user("there") + "\n", encoding="utf-8")
        assert read_last_usage(f) is None
