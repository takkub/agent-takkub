"""Tests for `gemini_md.ensure_gemini_md` — plants the takkub
cheatsheet as `GEMINI.md` in the spawn cwd. Mirror of
test_codex_agents_md.py. Guards the two safety rules: never clobber
a user-authored GEMINI.md, refresh our marker-tagged file idempotently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub.gemini_md import TAKKUB_GEMINI_MARKER, ensure_gemini_md


class TestEnsureGeminiMd:
    def test_creates_when_missing(self, tmp_path: Path) -> None:
        ok, reason = ensure_gemini_md(tmp_path)
        assert ok is True
        assert reason == "written"
        target = tmp_path / "GEMINI.md"
        assert target.exists()
        first = target.read_text(encoding="utf-8").splitlines()[0]
        assert TAKKUB_GEMINI_MARKER in first

    def test_refreshes_when_marker_present(self, tmp_path: Path) -> None:
        target = tmp_path / "GEMINI.md"
        target.write_text(f"{TAKKUB_GEMINI_MARKER}\n\nold body\n", encoding="utf-8")
        ok, reason = ensure_gemini_md(tmp_path)
        assert ok is True
        assert reason == "written"
        body = target.read_text(encoding="utf-8")
        assert TAKKUB_GEMINI_MARKER in body
        assert "old body" not in body
        assert "takkub send" in body  # cheatsheet content present

    def test_skips_user_owned_file(self, tmp_path: Path) -> None:
        target = tmp_path / "GEMINI.md"
        original = "# Project GEMINI\n\nrules: be careful with rm.\n"
        target.write_text(original, encoding="utf-8")
        ok, reason = ensure_gemini_md(tmp_path)
        assert ok is False
        assert reason == "user-owned"
        assert target.read_text(encoding="utf-8") == original

    def test_accepts_str_path(self, tmp_path: Path) -> None:
        ok, reason = ensure_gemini_md(str(tmp_path))
        assert ok is True
        assert reason == "written"

    def test_returns_failure_when_target_unwritable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_write_text(self, *_a, **_kw):  # type: ignore[no-untyped-def]
            raise OSError("disk full")

        monkeypatch.setattr(Path, "write_text", fake_write_text)
        ok, reason = ensure_gemini_md(tmp_path)
        assert ok is False
        assert "write failed" in reason

    def test_handles_empty_existing_file(self, tmp_path: Path) -> None:
        target = tmp_path / "GEMINI.md"
        target.write_text("", encoding="utf-8")
        ok, reason = ensure_gemini_md(tmp_path)
        assert ok is False
        assert reason == "user-owned"

    def test_marker_is_distinct_from_codex(self) -> None:
        # Guard against accidental copy-paste: each planter must use
        # its own marker so the two files can coexist in a single cwd
        # without one clobbering the other on refresh.
        from agent_takkub.codex_agents_md import TAKKUB_MARKER as codex_marker

        assert TAKKUB_GEMINI_MARKER != codex_marker
        assert "GEMINI" in TAKKUB_GEMINI_MARKER
