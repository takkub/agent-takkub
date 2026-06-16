"""Tests for clickable-link path resolution in the terminal panes.

Covers `_resolve_open_path` — the pure helper that turns a path-looking
token clicked in a pane into an existing filesystem path (or None). The
Qt open handlers and the JS link provider are exercised by live e2e.
"""

from __future__ import annotations

from agent_takkub.terminal_widget import _resolve_open_path


class TestResolveOpenPath:
    def test_absolute_existing(self, tmp_path):
        f = tmp_path / "report.html"
        f.write_text("ok", encoding="utf-8")
        assert _resolve_open_path(str(f)) == f

    def test_absolute_missing_returns_none(self, tmp_path):
        assert _resolve_open_path(str(tmp_path / "nope.md")) is None

    def test_relative_resolves_against_cwd(self, tmp_path):
        (tmp_path / "docs").mkdir()
        f = tmp_path / "docs" / "design-review.md"
        f.write_text("x", encoding="utf-8")
        assert _resolve_open_path("docs/design-review.md", cwd=str(tmp_path)) == f

    def test_relative_resolves_against_extra_base(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        f = repo / "ARCHITECTURE.md"
        f.write_text("x", encoding="utf-8")
        # cwd has no such file; the extra base (repo root) does
        got = _resolve_open_path("ARCHITECTURE.md", cwd=str(tmp_path), extra_bases=(str(repo),))
        assert got == f

    def test_cwd_wins_over_extra_base(self, tmp_path):
        cwd = tmp_path / "proj"
        repo = tmp_path / "repo"
        cwd.mkdir()
        repo.mkdir()
        (cwd / "x.md").write_text("a", encoding="utf-8")
        (repo / "x.md").write_text("b", encoding="utf-8")
        assert _resolve_open_path("x.md", cwd=str(cwd), extra_bases=(str(repo),)) == cwd / "x.md"

    def test_trailing_sentence_punctuation_stripped(self, tmp_path):
        f = tmp_path / "notes.md"
        f.write_text("x", encoding="utf-8")
        # path printed mid-sentence: "see C:/.../notes.md."
        assert _resolve_open_path(str(f) + ".") == f
        assert _resolve_open_path(f"({f})") == f

    def test_surrounding_quotes_stripped(self, tmp_path):
        f = tmp_path / "a b.md"  # space in name → often quoted
        f.write_text("x", encoding="utf-8")
        assert _resolve_open_path(f'"{f}"') == f

    def test_empty_and_blank(self):
        assert _resolve_open_path("") is None
        assert _resolve_open_path("   ") is None
        assert _resolve_open_path(".") is None

    def test_relative_with_no_base_is_none(self, tmp_path):
        # nothing to resolve against → cannot confirm existence
        assert _resolve_open_path("docs/x.md") is None


class TestExecExtensionGuard:
    """M3#13: clicked paths to OS-executable files must be flagged so the open
    handler reveals-in-folder instead of running them."""

    def test_windows_executables_flagged(self):
        from pathlib import Path

        from agent_takkub.terminal_widget import _is_exec_path

        for ext in (".exe", ".bat", ".cmd", ".ps1", ".hta", ".lnk", ".msi", ".vbs", ".scr"):
            assert _is_exec_path(Path(f"payload{ext}")) is True, ext

    def test_extension_check_is_case_insensitive(self):
        from pathlib import Path

        from agent_takkub.terminal_widget import _is_exec_path

        assert _is_exec_path(Path("Payload.EXE")) is True
        assert _is_exec_path(Path("run.Bat")) is True

    def test_documents_and_source_not_flagged(self):
        from pathlib import Path

        from agent_takkub.terminal_widget import _is_exec_path

        for name in ("report.html", "notes.md", "main.py", "app.js", "diagram.png", "data.json"):
            assert _is_exec_path(Path(name)) is False, name


class TestPathConfinement:
    """M3#13: a clicked path must lie within the pane cwd or an allowed base;
    anything escaping the subtree (absolute elsewhere, or `../` traversal) is
    refused so a pane can't lure a click onto an arbitrary file."""

    def test_path_inside_cwd_allowed(self, tmp_path):
        from agent_takkub.terminal_widget import _within_allowed_bases

        f = tmp_path / "docs" / "x.md"
        f.parent.mkdir()
        f.write_text("x", encoding="utf-8")
        assert _within_allowed_bases(f, cwd=str(tmp_path), extra_bases=()) is True

    def test_path_inside_extra_base_allowed(self, tmp_path):
        from agent_takkub.terminal_widget import _within_allowed_bases

        repo = tmp_path / "repo"
        repo.mkdir()
        f = repo / "ARCHITECTURE.md"
        f.write_text("x", encoding="utf-8")
        assert _within_allowed_bases(f, cwd=None, extra_bases=(str(repo),)) is True

    def test_path_outside_all_bases_refused(self, tmp_path):
        from agent_takkub.terminal_widget import _within_allowed_bases

        cwd = tmp_path / "proj"
        outside = tmp_path / "elsewhere" / "secret.txt"
        cwd.mkdir()
        outside.parent.mkdir()
        outside.write_text("x", encoding="utf-8")
        assert _within_allowed_bases(outside, cwd=str(cwd), extra_bases=()) is False

    def test_dotdot_traversal_refused(self, tmp_path):
        from pathlib import Path

        from agent_takkub.terminal_widget import _within_allowed_bases

        cwd = tmp_path / "proj"
        cwd.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("x", encoding="utf-8")
        # cwd/../secret.txt escapes the cwd subtree once resolved
        escaped = Path(str(cwd)) / ".." / "secret.txt"
        assert _within_allowed_bases(escaped, cwd=str(cwd), extra_bases=()) is False

    def test_no_bases_refuses_everything(self, tmp_path):
        from agent_takkub.terminal_widget import _within_allowed_bases

        f = tmp_path / "x.md"
        f.write_text("x", encoding="utf-8")
        assert _within_allowed_bases(f, cwd=None, extra_bases=()) is False
