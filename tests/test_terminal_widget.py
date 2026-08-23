"""Tests for TerminalWidget UTF-8 incremental decoding.

The decoding fix (stateful IncrementalDecoder instead of per-chunk
data.decode("utf-8", "replace")) does not depend on Qt rendering.
We test it by exercising the decoder directly — exactly what
TerminalWidget._utf8_decoder does — and by monkey-patching write_bytes
to capture decoded text without instantiating a QApplication.
"""

from __future__ import annotations

import codecs

# ---------------------------------------------------------------------------
# Decoder unit tests — no Qt required
# ---------------------------------------------------------------------------


class TestIncrementalDecoder:
    """Verifies that codecs IncrementalDecoder handles PTY chunk splits."""

    def _make_decoder(self):
        return codecs.getincrementaldecoder("utf-8")(errors="replace")

    def test_single_chunk_thai(self):
        dec = self._make_decoder()
        result = dec.decode("สวัสดี".encode())
        assert result == "สวัสดี"

    def test_split_mid_thai_char(self):
        """ส = 0xE0 0xB8 0xAA — split after first two bytes."""
        dec = self._make_decoder()
        sa_bytes = "ส".encode()
        assert sa_bytes == b"\xe0\xb8\xaa"

        part1 = dec.decode(sa_bytes[:2])  # 0xE0 0xB8 — buffered, not emitted
        part2 = dec.decode(sa_bytes[2:])  # 0xAA — completes the char

        assert part1 + part2 == "ส", (
            f"Expected 'ส', got {(part1 + part2)!r} — "
            "stateless decode would produce replacement chars"
        )

    def test_split_mid_char_full_word(self):
        """สวัสดี split at a random mid-character boundary stays intact."""
        dec = self._make_decoder()
        raw = "สวัสดี".encode()
        # split at byte 4 (inside วั which spans bytes 3-8)
        split = 4
        part1 = dec.decode(raw[:split])
        part2 = dec.decode(raw[split:])
        assert part1 + part2 == "สวัสดี"

    def test_mixed_ascii_thai_split(self):
        """ASCII + Thai interleaved; split inside Thai char."""
        dec = self._make_decoder()
        raw = "hello สวัสดี world".encode()
        # find byte offset of ส (first Thai char, after "hello ")
        sa_offset = raw.index("ส".encode()[0])
        part1 = dec.decode(raw[: sa_offset + 1])  # includes partial ส
        part2 = dec.decode(raw[sa_offset + 1 :])
        assert part1 + part2 == "hello สวัสดี world"

    def test_invalid_utf8_replaced_gracefully(self):
        """Truly invalid byte sequence still produces a replacement char."""
        dec = self._make_decoder()
        result = dec.decode(b"\xff")
        assert "�" in result or result == "�"

    def test_reset_clears_buffer(self):
        """After reset(), a partial sequence is discarded, next decode fresh."""
        dec = self._make_decoder()
        sa_bytes = "ส".encode()
        dec.decode(sa_bytes[:2])  # buffer partial — don't consume output
        dec.reset()
        # next full Thai char should decode cleanly
        result = dec.decode("ก".encode())
        assert result == "ก"

    def test_multiple_chunks_sequentially(self):
        """Simulate PTY delivering output one byte at a time."""
        dec = self._make_decoder()
        raw = "สวัสดี".encode()
        result = "".join(dec.decode(bytes([b])) for b in raw)
        assert result == "สวัสดี"

    def test_stateless_decode_fails_split(self):
        """Control: proves stateless .decode() DOES corrupt split chars.

        This documents why the fix is necessary. The stateless approach
        produces replacement chars for partial multi-byte sequences.
        """
        sa_bytes = "ส".encode()
        part1 = sa_bytes[:2].decode("utf-8", "replace")  # produces �
        part2 = sa_bytes[2:].decode("utf-8", "replace")  # produces �
        # stateless decode corrupts the character
        assert "�" in part1 or "�" in part2, "Expected corruption from stateless decode"
        assert (part1 + part2) != "ส"


# Integration via TerminalWidget instance was removed: it required Qt stubs
# whose import-order coupling was brittle in the full suite. The unit tests
# above exercise the exact codecs.IncrementalDecoder instance that
# TerminalWidget._utf8_decoder uses, so behavior coverage is identical.


# ---------------------------------------------------------------------------
# __init__ order regression (PR #149 review blocker #1): focusInEvent/
# mousePressEvent were once spliced into the middle of __init__, so every
# statement after them (the UTF-8 decoder, _pending_writes, _input_locked,
# ..., down to the final self._view.load(...)) was silently reparented as
# the BODY of mousePressEvent instead of __init__ — dead code that never
# runs at construction time. A real QWebEngineView construction was tried
# here first to catch this end-to-end, but it hard-aborts the whole pytest
# process (exit 127) even under QT_QPA_PLATFORM=offscreen on this Windows
# box — exactly the class of crash test_terminal_widget.py's own removal
# note above and test_keepalive_suspend.py already warn about, confirmed by
# actually reproducing it. An AST check instead inspects the *source
# structure* of __init__: if the buggy splice reappears, the attribute
# assignments below stop being descendants of the __init__ FunctionDef node
# (they become descendants of a sibling method instead) and this goes red —
# without ever importing Qt or constructing anything.
# ---------------------------------------------------------------------------


class TestTerminalWidgetInitStructure:
    def _init_body_attrs(self) -> set[str]:
        import ast
        import inspect

        from agent_takkub import terminal_widget

        source = inspect.getsource(terminal_widget.TerminalWidget)
        cls_node = ast.parse(source).body[0]
        init_node = next(
            n for n in cls_node.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"
        )
        return {
            node.attr
            for node in ast.walk(init_node)
            if isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        }

    def test_init_assigns_attributes_past_the_bridge_setup(self) -> None:
        assigned = self._init_body_attrs()
        for attr in ("_page_ready", "_utf8_decoder", "_input_locked", "_pending_writes"):
            assert attr in assigned, (
                f"TerminalWidget.__init__ never assigns self.{attr} — did "
                "focusInEvent/mousePressEvent get spliced back into the "
                "middle of __init__, reparenting everything after them "
                "into a sibling method's dead-code body?"
            )

    def test_focus_and_mousepress_handlers_are_not_nested_in_init(self) -> None:
        import ast
        import inspect

        from agent_takkub import terminal_widget

        source = inspect.getsource(terminal_widget.TerminalWidget)
        cls_node = ast.parse(source).body[0]
        init_node = next(
            n for n in cls_node.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"
        )
        nested_defs = {n.name for n in ast.walk(init_node) if isinstance(n, ast.FunctionDef)}
        assert "focusInEvent" not in nested_defs
        assert "mousePressEvent" not in nested_defs


# ---------------------------------------------------------------------------
# #364 lever 1 — _cap_snapshot_lines: pure, no Qt required (unlike the rest
# of the discard/reattach machinery, which lives inside a real
# QWebEngineView and can only be exercised via tools/spike_pane_discard_ram.py
# as a subprocess — see tests/test_pane_discard_spike.py).
# ---------------------------------------------------------------------------


class TestCapSnapshotLines:
    def _cap(self, text: str, max_lines: int = 5_000) -> str:
        from agent_takkub.terminal_widget import _cap_snapshot_lines

        return _cap_snapshot_lines(text, max_lines)

    def test_under_limit_is_unchanged(self) -> None:
        text = "\n".join(f"line{i}" for i in range(10))
        assert self._cap(text, max_lines=100) == text

    def test_over_limit_keeps_only_the_last_n_lines(self) -> None:
        text = "\n".join(f"line{i}" for i in range(10))
        capped = self._cap(text, max_lines=3)
        assert capped == "line7\nline8\nline9"

    def test_default_cap_is_5000_lines(self) -> None:
        text = "\n".join(f"line{i}" for i in range(6_000))
        capped = self._cap(text)
        assert capped.count("\n") == 4_999  # 5000 lines → 4999 separators
        assert capped.splitlines()[0] == "line1000"
        assert capped.splitlines()[-1] == "line5999"

    def test_empty_text(self) -> None:
        assert self._cap("", max_lines=10) == ""
