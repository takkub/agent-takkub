"""tok-4: don't double-inject a project's CLAUDE.md.

Lead spawns with cwd = lead_cwd(project) (or an explicit --cwd). claude itself
auto-discovers CLAUDE.md from that cwd and every ancestor. So injecting the same
project CLAUDE.md into the system prompt too just duplicates ~750 tok of rules.

`_render_lead_context` must therefore SKIP the project-rules injection when the
file claude already auto-loads is the one we'd inject — but must STILL inject when
the file lives where claude won't auto-load it (a project subdir while Lead sits
at the parent, or an unrelated location).
"""

from __future__ import annotations

import pathlib

import pytest

from agent_takkub import lead_context as lc_mod
from agent_takkub.lead_context import _claude_autoloads, _render_lead_context

SECTION = "Project rules (auto-injected"  # marker emitted by the injection block


class TestClaudeAutoloads:
    """The pure predicate: does claude started in `cwd` auto-load `md_dir`'s
    CLAUDE.md? True for cwd itself + any ancestor; False for subdirs / siblings."""

    def test_same_dir(self, tmp_path: pathlib.Path) -> None:
        assert _claude_autoloads(tmp_path, tmp_path) is True

    def test_ancestor_is_autoloaded(self, tmp_path: pathlib.Path) -> None:
        child = tmp_path / "a" / "b"
        child.mkdir(parents=True)
        assert _claude_autoloads(child, tmp_path) is True  # md at an ancestor

    def test_subdir_is_not_autoloaded(self, tmp_path: pathlib.Path) -> None:
        sub = tmp_path / "web"
        sub.mkdir()
        assert _claude_autoloads(tmp_path, sub) is False  # md in a subdir of cwd

    def test_sibling_is_not_autoloaded(self, tmp_path: pathlib.Path) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        assert _claude_autoloads(a, b) is False


@pytest.fixture
def fake_project(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
    """A project whose single path has its own CLAUDE.md, wired into
    load_projects so _render_lead_context resolves it."""
    proj_root = tmp_path / "myproj"
    proj_root.mkdir()
    (proj_root / "CLAUDE.md").write_text("# myproj rules\n- use bun\n", encoding="utf-8")

    def fake_load() -> dict:
        return {"projects": {"myproj": {"paths": {"main": str(proj_root)}}}}

    monkeypatch.setattr(lc_mod, "load_projects", fake_load)
    return proj_root


class TestProjectRulesInjection:
    def test_skipped_when_claude_cwd_is_project_root(self, fake_project: pathlib.Path) -> None:
        # claude will auto-discover myproj/CLAUDE.md from its cwd → don't inject.
        out = _render_lead_context("myproj", claude_cwd=str(fake_project))
        assert out is not None
        assert SECTION not in pathlib.Path(out).read_text(encoding="utf-8")

    def test_skipped_when_claude_cwd_is_below_project_root(
        self, fake_project: pathlib.Path
    ) -> None:
        sub = fake_project / "packages" / "app"
        sub.mkdir(parents=True)
        # cwd below project root → claude walks UP to myproj/CLAUDE.md → skip.
        out = _render_lead_context("myproj", claude_cwd=str(sub))
        assert SECTION not in pathlib.Path(out).read_text(encoding="utf-8")

    def test_injected_when_claude_cwd_is_unrelated(
        self, fake_project: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        # claude won't auto-load myproj/CLAUDE.md from here → inject it so Lead
        # still sees the project rules at planning time.
        out = _render_lead_context("myproj", claude_cwd=str(elsewhere))
        assert SECTION in pathlib.Path(out).read_text(encoding="utf-8")
