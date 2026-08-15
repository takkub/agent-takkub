"""Targeted tests for the pure #241 helpers in orchestrator_text.py:
`_notice_fingerprint` (dedup key for "Lead already read this via `takkub
inbox`") and `_truncate_at_word_boundary` (word-safe headline condensing)."""

from __future__ import annotations

from agent_takkub.orchestrator_text import _notice_fingerprint, _truncate_at_word_boundary


class TestNoticeFingerprint:
    def test_same_body_same_fingerprint(self) -> None:
        assert _notice_fingerprint("[backend done] x") == _notice_fingerprint("[backend done] x")

    def test_different_body_different_fingerprint(self) -> None:
        assert _notice_fingerprint("[backend done] x") != _notice_fingerprint("[backend done] y")

    def test_surrounding_whitespace_does_not_affect_fingerprint(self) -> None:
        assert _notice_fingerprint("  [backend done] x  \n") == _notice_fingerprint(
            "[backend done] x"
        )


class TestTruncateAtWordBoundary:
    def test_short_text_untouched(self) -> None:
        assert _truncate_at_word_boundary("short", 200) == "short"

    def test_cuts_at_preceding_space_not_mid_word(self) -> None:
        text = "the quick brown fox jumps over the lazy dog and keeps running"
        truncated = _truncate_at_word_boundary(text, 30)

        assert truncated.endswith("…")
        # never ends mid-word — the char right before "…" is not a letter
        # split awkwardly from the next one in the original text.
        body = truncated[:-1].rstrip()
        assert text.startswith(body)
        assert not text[len(body) : len(body) + 1].isalpha() or text[len(body)] == " "

    def test_single_long_token_falls_back_to_hard_cut(self) -> None:
        text = "x" * 500
        truncated = _truncate_at_word_boundary(text, 200)

        assert truncated == ("x" * 200) + "…"

    def test_never_exceeds_budget_plus_ellipsis(self) -> None:
        text = "word " * 100
        truncated = _truncate_at_word_boundary(text, 50)

        assert len(truncated) <= 51
