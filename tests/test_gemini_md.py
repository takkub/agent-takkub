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

    def test_rejects_relative_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Drive-relative / relative paths must be refused — otherwise
        # `mkdir(parents=True)` creates junk dirs under the process cwd.
        monkeypatch.chdir(tmp_path)
        ok, reason = ensure_gemini_md("UsersmonchWebstormProjectsagent-takkub")
        assert ok is False
        assert "invalid spawn_cwd" in reason
        assert not (tmp_path / "UsersmonchWebstormProjectsagent-takkub").exists()

    def test_rejects_nonexistent_absolute_path(self, tmp_path: Path) -> None:
        ghost = tmp_path / "does-not-exist"
        ok, reason = ensure_gemini_md(ghost)
        assert ok is False
        assert "invalid spawn_cwd" in reason
        assert not ghost.exists()

    def test_marker_is_distinct_from_codex(self) -> None:
        # Guard against accidental copy-paste: each planter must use
        # its own marker so the two files can coexist in a single cwd
        # without one clobbering the other on refresh.
        from agent_takkub.codex_agents_md import TAKKUB_MARKER as codex_marker

        assert TAKKUB_GEMINI_MARKER != codex_marker
        assert "GEMINI" in TAKKUB_GEMINI_MARKER


class TestGeminiMdGitGuard:
    def test_template_has_version_control_section(self) -> None:
        from agent_takkub.gemini_md import GEMINI_MD

        assert "Version control" in GEMINI_MD

    def test_template_forbids_git_commit(self) -> None:
        from agent_takkub.gemini_md import GEMINI_MD

        assert "git commit" in GEMINI_MD
        assert "NEVER" in GEMINI_MD

    def test_template_forbids_git_push(self) -> None:
        from agent_takkub.gemini_md import GEMINI_MD

        assert "git push" in GEMINI_MD

    def test_template_no_longer_has_weak_commit_rule(self) -> None:
        from agent_takkub.gemini_md import GEMINI_MD

        assert "commit when explicitly asked" not in GEMINI_MD

    def test_template_has_override_rule_section(self) -> None:
        from agent_takkub.gemini_md import GEMINI_MD

        assert "Override rule" in GEMINI_MD

    def test_template_override_rule_clarifies_takkub_done_is_shell(self) -> None:
        from agent_takkub.gemini_md import GEMINI_MD

        assert "takkub done is a shell command" in GEMINI_MD.replace("`", "")
