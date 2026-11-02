"""#966: a claude pane in `--isolation worktree` (macOS, sandbox on) could not
create `<main>/.git/worktrees/<id>/index.lock`, so it could not stage or commit
on its wt/ branch. codex got the main `.git` via `--add-dir` in #947; claude
and agy now get the same grant."""

from __future__ import annotations

from pathlib import Path

from agent_takkub.core.providers.claude_plan import assemble_claude_argv
from agent_takkub.spawn_engine import _linked_worktree_common_dir


def _linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    main_git = tmp_path / "main" / ".git"
    gitdir = main_git / "worktrees" / "backend-1"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    return wt, main_git


def test_claude_argv_carries_add_dir_right_after_settings(tmp_path):
    wt, main_git = _linked_worktree(tmp_path)
    common = _linked_worktree_common_dir(str(wt))
    assert Path(common) == main_git.resolve()
    argv = assemble_claude_argv(
        "claude",
        setting_sources="user",
        settings_argv=["--settings", "s.json"],
        add_dir_argv=["--add-dir", common],
        model_argv=["--model", "m"],
    )
    i = argv.index("--add-dir")
    assert argv[i - 2 : i] == ["--settings", "s.json"]
    assert argv[i + 1 : i + 3] == [common, "--model"]


def test_claude_argv_unchanged_outside_a_worktree():
    argv = assemble_claude_argv("claude", setting_sources="user")
    assert "--add-dir" not in argv
