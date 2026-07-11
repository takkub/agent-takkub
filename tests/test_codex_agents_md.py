"""Tests for `codex_agents_md.ensure_agents_md` — the helper that
plants the cockpit's Codex cheatsheet into the spawn cwd. Guards the
two safety rules: (a) never clobber a user-authored AGENTS.md, (b)
refresh our own marker-tagged file idempotently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub.codex_agents_md import TAKKUB_MARKER, ensure_agents_md


class TestEnsureAgentsMd:
    def test_creates_when_missing(self, tmp_path: Path) -> None:
        ok, reason = ensure_agents_md(tmp_path)
        assert ok is True
        assert reason == "written"
        target = tmp_path / "AGENTS.md"
        assert target.exists()
        first = target.read_text(encoding="utf-8").splitlines()[0]
        assert TAKKUB_MARKER in first

    def test_refreshes_when_marker_present(self, tmp_path: Path) -> None:
        # Pre-existing takkub-managed file with stale body.
        target = tmp_path / "AGENTS.md"
        target.write_text(f"{TAKKUB_MARKER}\n\nold body\n", encoding="utf-8")
        ok, reason = ensure_agents_md(tmp_path)
        assert ok is True
        assert reason == "written"
        body = target.read_text(encoding="utf-8")
        # Marker preserved, fresh content overwrote the old body
        assert TAKKUB_MARKER in body
        assert "old body" not in body
        assert "takkub send" in body  # part of the cheatsheet

    def test_skips_user_owned_file(self, tmp_path: Path) -> None:
        # User-written AGENTS.md with no marker — leave it alone.
        target = tmp_path / "AGENTS.md"
        original = "# Project AGENTS\n\nrules: be careful with rm.\n"
        target.write_text(original, encoding="utf-8")
        ok, reason = ensure_agents_md(tmp_path)
        assert ok is False
        assert reason == "user-owned"
        assert target.read_text(encoding="utf-8") == original

    def test_accepts_str_path(self, tmp_path: Path) -> None:
        ok, reason = ensure_agents_md(str(tmp_path))
        assert ok is True
        assert reason == "written"

    def test_returns_failure_when_target_unwritable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force write_text to raise so we exercise the OSError branch
        # without depending on filesystem-permission tricks (which are
        # flaky on Windows).
        def fake_write_text(self, *_a, **_kw):  # type: ignore[no-untyped-def]
            raise OSError("disk full")

        monkeypatch.setattr(Path, "write_text", fake_write_text)
        ok, reason = ensure_agents_md(tmp_path)
        assert ok is False
        assert "write failed" in reason

    def test_handles_empty_existing_file(self, tmp_path: Path) -> None:
        # Edge: empty AGENTS.md (no first line). Should be treated as
        # user-owned because the marker isn't present.
        target = tmp_path / "AGENTS.md"
        target.write_text("", encoding="utf-8")
        ok, reason = ensure_agents_md(tmp_path)
        assert ok is False
        assert reason == "user-owned"

    def test_rejects_relative_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Drive-relative / relative paths must be refused — otherwise
        # `mkdir(parents=True)` creates junk dirs under the process cwd.
        monkeypatch.chdir(tmp_path)
        ok, reason = ensure_agents_md("UsersaliceWebstormProjectsagent-takkub")
        assert ok is False
        assert "invalid spawn_cwd" in reason
        assert not (tmp_path / "UsersaliceWebstormProjectsagent-takkub").exists()

    def test_rejects_nonexistent_absolute_path(self, tmp_path: Path) -> None:
        ghost = tmp_path / "does-not-exist"
        ok, reason = ensure_agents_md(ghost)
        assert ok is False
        assert "invalid spawn_cwd" in reason
        assert not ghost.exists()


class TestCodexAgentsMdGitGuard:
    def test_template_has_version_control_section(self) -> None:
        from agent_takkub.codex_agents_md import CODEX_AGENTS_MD

        assert "Version control" in CODEX_AGENTS_MD

    def test_template_forbids_git_commit(self) -> None:
        from agent_takkub.codex_agents_md import CODEX_AGENTS_MD

        assert "git commit" in CODEX_AGENTS_MD
        assert "NEVER" in CODEX_AGENTS_MD

    def test_template_forbids_git_push(self) -> None:
        from agent_takkub.codex_agents_md import CODEX_AGENTS_MD

        assert "git push" in CODEX_AGENTS_MD

    def test_template_no_longer_has_weak_commit_rule(self) -> None:
        from agent_takkub.codex_agents_md import CODEX_AGENTS_MD

        assert "commit when explicitly asked" not in CODEX_AGENTS_MD


class TestEnsureAgentsMdExtra:
    """`extra` (#103 phase 4) bridges Skill Matrix content into AGENTS.md."""

    def test_appends_extra_after_base_cheatsheet(self, tmp_path: Path) -> None:
        ok, reason = ensure_agents_md(tmp_path, extra="\n\n## Skills\n- debug-mantra\n")
        assert ok is True
        assert reason == "written"
        body = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
        assert "takkub send" in body  # base cheatsheet still present
        assert "## Skills" in body
        assert "debug-mantra" in body
        # extra comes after the base content
        assert body.index("takkub send") < body.index("## Skills")

    def test_empty_extra_is_unchanged_from_before(self, tmp_path: Path) -> None:
        from agent_takkub.codex_agents_md import CODEX_AGENTS_MD

        ensure_agents_md(tmp_path)
        assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == CODEX_AGENTS_MD

    def test_refresh_replaces_stale_extra(self, tmp_path: Path) -> None:
        ensure_agents_md(tmp_path, extra="\n\n## Skills\n- old-skill\n")
        ensure_agents_md(tmp_path, extra="\n\n## Skills\n- new-skill\n")
        body = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
        assert "new-skill" in body
        assert "old-skill" not in body

    def test_user_owned_file_still_skipped_with_extra(self, tmp_path: Path) -> None:
        target = tmp_path / "AGENTS.md"
        original = "# Project AGENTS\n\nrules: be careful.\n"
        target.write_text(original, encoding="utf-8")
        ok, reason = ensure_agents_md(tmp_path, extra="\n\n## Skills\n- debug-mantra\n")
        assert ok is False
        assert reason == "user-owned"
        assert target.read_text(encoding="utf-8") == original


class TestGitExclude:
    """A2: a cockpit-planted AGENTS.md is added to `.git/info/exclude` so it
    never shows in the user's `git status` — without touching `.gitignore`."""

    def _exclude(self, repo: Path) -> Path:
        return repo / ".git" / "info" / "exclude"

    def test_adds_agents_md_to_exclude_in_a_repo(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        ok, _ = ensure_agents_md(tmp_path)
        assert ok
        lines = self._exclude(tmp_path).read_text(encoding="utf-8").splitlines()
        assert "AGENTS.md" in lines

    def test_no_exclude_when_not_a_repo(self, tmp_path: Path) -> None:
        ok, _ = ensure_agents_md(tmp_path)
        assert ok
        assert not (tmp_path / ".git").exists()

    def test_idempotent_no_duplicate_line(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        ensure_agents_md(tmp_path)
        ensure_agents_md(tmp_path)  # refresh
        lines = self._exclude(tmp_path).read_text(encoding="utf-8").splitlines()
        assert lines.count("AGENTS.md") == 1

    def test_preserves_existing_exclude_entries(self, tmp_path: Path) -> None:
        info = tmp_path / ".git" / "info"
        info.mkdir(parents=True)
        (info / "exclude").write_text("# git ls-files --others\n*.log\n", encoding="utf-8")
        ensure_agents_md(tmp_path)
        text = self._exclude(tmp_path).read_text(encoding="utf-8")
        assert "*.log" in text
        assert "AGENTS.md" in text.splitlines()

    def test_user_owned_file_not_excluded(self, tmp_path: Path) -> None:
        # A user-owned AGENTS.md is left alone AND not force-hidden from them.
        (tmp_path / ".git").mkdir()
        (tmp_path / "AGENTS.md").write_text("# mine\n", encoding="utf-8")
        ok, reason = ensure_agents_md(tmp_path)
        assert not ok and reason == "user-owned"
        assert not self._exclude(tmp_path).exists()

    def test_skips_git_file_worktree(self, tmp_path: Path) -> None:
        # A `.git` FILE (linked worktree/submodule) is skipped rather than
        # parsed — the marker already says "do not commit".
        (tmp_path / ".git").write_text("gitdir: /somewhere/.git/worktrees/x\n", encoding="utf-8")
        ok, _ = ensure_agents_md(tmp_path)
        assert ok  # still plants the file, just no exclude write


class TestCodexAgentsMdOverrideRule:
    """Guards the section that prevents codex from misreading
    Lead's `[ROLE: ... ห้าม spawn subagent]` prefix as forbidding the
    mandatory `takkub done` shell call (root cause of the "codex doesn't
    send takkub done" bug — see 2026-05-28 screenshots).
    """

    def test_template_has_override_rule_section(self) -> None:
        from agent_takkub.codex_agents_md import CODEX_AGENTS_MD

        assert "Override rule" in CODEX_AGENTS_MD

    def test_override_rule_clarifies_subagent_scope(self) -> None:
        from agent_takkub.codex_agents_md import CODEX_AGENTS_MD

        assert "AI subagents only" in CODEX_AGENTS_MD

    def test_override_rule_pins_done_as_shell_command(self) -> None:
        from agent_takkub.codex_agents_md import CODEX_AGENTS_MD

        assert "`takkub done` is a shell command, not a subagent" in CODEX_AGENTS_MD
