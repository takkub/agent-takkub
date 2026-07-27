"""Unit tests for disk_usage (`takkub disk` / `takkub prune`).

Every test operates strictly under `tmp_path` — never DATA_HOME / the real
`~/.agent-takkub` — per project policy: this feature must never delete a
real user's files while being developed or tested.
"""

from __future__ import annotations

import os
import stat
import sys
import time

import pytest

from agent_takkub import disk_usage
from agent_takkub.worktree_manager import GitResult, WorktreeManager


def _make_runner(git_root, porcelain, ahead=None, dirty=None):
    """Scripted git runner: rev-parse/worktree-list/rev-list/status only."""
    ahead = ahead or {}
    dirty = dirty or set()

    def runner(args, cwd):
        if "rev-parse" in args and "--show-toplevel" in args:
            return GitResult(0, git_root + "\n", "")
        if "worktree" in args and "list" in args and "--porcelain" in args:
            return GitResult(0, porcelain, "")
        if "rev-list" in args and "--count" in args:
            branch = args[-1].split("..", 1)[1]
            return GitResult(0, f"{ahead.get(branch, 0)}\n", "")
        if "status" in args and "--porcelain" in args:
            idx = args.index("-C")
            path = args[idx + 1]
            return GitResult(0, "M x\n" if path in dirty else "", "")
        return GitResult(0, "", "")

    return runner


def _porcelain(entries):
    """entries: list of (path, sha, branch) -> `git worktree list --porcelain` text."""
    lines = []
    for path, sha, branch in entries:
        lines += [f"worktree {path}", f"HEAD {sha}", f"branch refs/heads/{branch}", ""]
    return "\n".join(lines)


class TestOrphanDetection:
    def test_no_git_pointer_is_orphan(self, tmp_path):
        wt = tmp_path / "worktrees" / "proj" / "backend-1"
        wt.mkdir(parents=True)
        # runner would raise if called at all — proves the .git-missing check
        # short-circuits before any subprocess call.
        mgr = WorktreeManager(
            runner=lambda a, c: (_ for _ in ()).throw(AssertionError("git called"))
        )
        info = disk_usage.classify_worktree(wt, mgr)
        assert info["orphan"] is True
        assert info["registered"] is False

    def test_git_root_unresolvable_is_orphan(self, tmp_path):
        wt = tmp_path / "worktrees" / "proj" / "backend-2"
        wt.mkdir(parents=True)
        (wt / ".git").write_text("gitdir: /nowhere/.git/worktrees/backend-2\n")
        mgr = WorktreeManager(runner=lambda a, c: GitResult(128, "", "fatal: not a git repo"))
        info = disk_usage.classify_worktree(wt, mgr)
        assert info["orphan"] is True

    def test_not_listed_in_worktree_list_is_orphan(self, tmp_path):
        wt = tmp_path / "worktrees" / "proj" / "backend-3"
        wt.mkdir(parents=True)
        (wt / ".git").write_text("gitdir: /repo/.git/worktrees/backend-3\n")
        porcelain = _porcelain([("/repo/other", "abc123", "wt/other-1")])
        mgr = WorktreeManager(runner=_make_runner("/repo", porcelain))
        info = disk_usage.classify_worktree(wt, mgr)
        assert info["orphan"] is True

    def test_registered_worktree_reports_dirty_and_ahead(self, tmp_path):
        wt = tmp_path / "worktrees" / "proj" / "backend-4"
        wt.mkdir(parents=True)
        (wt / ".git").write_text("gitdir: /repo/.git/worktrees/backend-4\n")
        porcelain = _porcelain([(str(wt), "abc123", "wt/backend-4-1")])
        mgr = WorktreeManager(
            runner=_make_runner("/repo", porcelain, ahead={"wt/backend-4-1": 3}, dirty={str(wt)})
        )
        info = disk_usage.classify_worktree(wt, mgr)
        assert info["orphan"] is False
        assert info["registered"] is True
        assert info["ahead"] == 3
        assert info["dirty"] is True


class TestNodeModulesScan:
    def test_finds_node_modules_at_multiple_depths(self, tmp_path):
        root = tmp_path / "wt"
        (root / "web" / "node_modules" / "leftpad").mkdir(parents=True)
        (root / "api" / "node_modules" / "expr").mkdir(parents=True)
        found = disk_usage.find_node_modules(root)
        rels = sorted(str(p.relative_to(root)) for p in found)
        assert rels == [os.path.join("api", "node_modules"), os.path.join("web", "node_modules")]

    def test_does_not_descend_into_nested_node_modules(self, tmp_path):
        root = tmp_path / "wt"
        nested = root / "web" / "node_modules" / "pkg" / "node_modules" / "inner"
        nested.mkdir(parents=True)
        found = disk_usage.find_node_modules(root)
        assert len(found) == 1
        assert found[0] == root / "web" / "node_modules"

    def test_scan_splits_orphan_vs_live_bytes(self, tmp_path):
        data_home = tmp_path
        orphan_wt = data_home / "worktrees" / "proj" / "frontend-1"
        (orphan_wt / "node_modules").mkdir(parents=True)
        (orphan_wt / "node_modules" / "f.txt").write_bytes(b"x" * 100)

        live_wt = data_home / "worktrees" / "proj" / "backend-1"
        (live_wt / "node_modules").mkdir(parents=True)
        (live_wt / "node_modules" / "f.txt").write_bytes(b"y" * 250)
        (live_wt / ".git").write_text("gitdir: /repo/.git/worktrees/backend-1\n")
        porcelain = _porcelain([(str(live_wt), "abc", "wt/backend-1-1")])
        mgr = WorktreeManager(runner=_make_runner("/repo", porcelain))

        result = disk_usage.scan_node_modules(data_home, mgr)
        assert result["orphan_bytes"] == 100
        assert result["orphan_count"] == 1
        assert result["live_bytes"] == 250
        assert result["live_count"] == 1

    def test_prune_node_modules_skips_live_by_default(self, tmp_path):
        data_home = tmp_path
        live_wt = data_home / "worktrees" / "proj" / "backend-5"
        (live_wt / "node_modules").mkdir(parents=True)
        (live_wt / "node_modules" / "f.txt").write_bytes(b"z" * 500)
        (live_wt / ".git").write_text("gitdir: /repo/.git/worktrees/backend-5\n")
        porcelain = _porcelain([(str(live_wt), "abc", "wt/backend-5-1")])
        mgr = WorktreeManager(runner=_make_runner("/repo", porcelain))

        outcome = disk_usage._prune_node_modules(
            data_home, dry_run=False, include_live=False, mgr=mgr
        )
        assert outcome.removed_count == 0
        assert (live_wt / "node_modules").exists()
        assert outcome.skipped  # explicit skip note, not silence

    def test_prune_node_modules_include_live_deletes(self, tmp_path):
        data_home = tmp_path
        live_wt = data_home / "worktrees" / "proj" / "backend-6"
        (live_wt / "node_modules").mkdir(parents=True)
        (live_wt / "node_modules" / "f.txt").write_bytes(b"z" * 500)
        (live_wt / ".git").write_text("gitdir: /repo/.git/worktrees/backend-6\n")
        porcelain = _porcelain([(str(live_wt), "abc", "wt/backend-6-1")])
        mgr = WorktreeManager(runner=_make_runner("/repo", porcelain))

        outcome = disk_usage._prune_node_modules(
            data_home, dry_run=False, include_live=True, mgr=mgr
        )
        assert outcome.removed_count == 1
        assert not (live_wt / "node_modules").exists()

    def test_orphan_node_modules_deleted_without_include_live(self, tmp_path):
        data_home = tmp_path
        orphan_wt = data_home / "worktrees" / "proj" / "frontend-7"
        (orphan_wt / "node_modules").mkdir(parents=True)
        (orphan_wt / "node_modules" / "f.txt").write_bytes(b"x" * 10)
        mgr = WorktreeManager()

        outcome = disk_usage._prune_node_modules(
            data_home, dry_run=False, include_live=False, mgr=mgr
        )
        assert outcome.removed_count == 1
        assert not (orphan_wt / "node_modules").exists()


class TestPruneDryRun:
    def test_dry_run_orphan_worktree_does_not_delete(self, tmp_path):
        wt = tmp_path / "worktrees" / "proj" / "frontend-9"
        wt.mkdir(parents=True)
        (wt / "leftover.txt").write_text("hi")
        result = disk_usage.prune(categories=["orphan-worktrees"], dry_run=True, data_home=tmp_path)
        assert wt.exists()
        cat = result["categories"][0]
        assert cat["dry_run"] is True
        assert cat["target_count"] == 1
        assert cat["removed_count"] == 0
        assert result["total_freed_bytes"] == 0

    def test_yes_removes_orphan_worktree(self, tmp_path):
        wt = tmp_path / "worktrees" / "proj" / "frontend-10"
        wt.mkdir(parents=True)
        (wt / "leftover.txt").write_text("hi")
        result = disk_usage.prune(
            categories=["orphan-worktrees"], dry_run=False, data_home=tmp_path
        )
        assert not wt.exists()
        assert result["categories"][0]["removed_count"] == 1

    def test_partial_prune_does_not_crash_when_claude_config_is_outside_home(
        self, tmp_path, monkeypatch
    ):
        # Dev-checkout shape: default_claude_config_dir() (~/.claude) sits
        # completely outside DATA_HOME/`home` — `_prune_partial` must not
        # `_assert_under(home)` that path (it used to, and raised ValueError).
        outside_root = tmp_path / "outside_home" / "claude-config"
        outside_root.mkdir(parents=True)
        partial = outside_root.with_name(outside_root.name + ".partial")
        (partial / "leftover").mkdir(parents=True)
        os.utime(partial, (time.time() - 3600, time.time() - 3600))
        monkeypatch.setattr(disk_usage, "default_claude_config_dir", lambda: outside_root)

        home = tmp_path / "home"
        home.mkdir()
        result = disk_usage.prune(categories=["partial"], dry_run=False, data_home=home)
        assert result["ok"] is True
        assert not partial.exists()
        assert result["categories"][0]["removed_count"] == 1


class TestNeverLevelRefusal:
    def test_unknown_never_category_is_refused_with_reason(self, tmp_path):
        result = disk_usage.prune(categories=["venv"], dry_run=True, data_home=tmp_path)
        assert result["ok"] is False
        assert any(
            "venv" in r and ("never" in r or "ห้าม" in r or "ปฏิเสธ" in r) for r in result["refusals"]
        )

    def test_review_category_refused_at_safe_level(self, tmp_path):
        result = disk_usage.prune(
            categories=["chat-history"], level="safe", dry_run=True, data_home=tmp_path
        )
        assert result["ok"] is False
        assert any("chat-history" in r for r in result["refusals"])

    def test_review_category_allowed_at_review_level(self, tmp_path, monkeypatch):
        cfg_dir = tmp_path / "claude-config"
        (cfg_dir / "projects").mkdir(parents=True)
        monkeypatch.setattr(disk_usage, "default_claude_config_dir", lambda: cfg_dir)
        result = disk_usage.prune(
            categories=["chat-history"], level="review", dry_run=True, data_home=tmp_path
        )
        assert result["ok"] is True
        assert not result["refusals"]

    def test_default_categories_at_safe_level_never_include_review(self, tmp_path, monkeypatch):
        # shell-snapshots (a safe-level default category) reads
        # default_claude_config_dir() — pin it under tmp_path so this never
        # touches the real ~/.claude or ~/.agent-takkub/claude-config.
        monkeypatch.setattr(
            disk_usage, "default_claude_config_dir", lambda: tmp_path / "claude-config"
        )
        result = disk_usage.prune(dry_run=True, data_home=tmp_path)
        categories_touched = {c["category"] for c in result["categories"]}
        assert "chat-history" not in categories_touched
        assert "exports" not in categories_touched


class TestPathSafety:
    def test_assert_under_rejects_escape(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        with pytest.raises(ValueError):
            disk_usage._assert_under(outside, home)

    def test_assert_under_allows_nested(self, tmp_path):
        home = tmp_path / "home"
        nested = home / "worktrees" / "x"
        nested.mkdir(parents=True)
        assert disk_usage._assert_under(nested, home) == nested.resolve()

    def test_crafted_escaping_worktree_path_is_never_deleted(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        escaped = tmp_path / "outside" / "evil"
        escaped.mkdir(parents=True)
        monkeypatch.setattr(disk_usage, "find_worktree_dirs", lambda h: [escaped])
        monkeypatch.setattr(
            disk_usage,
            "classify_worktree",
            lambda wt, mgr, **kw: {"path": str(wt), "orphan": True, "registered": False},
        )
        with pytest.raises(ValueError):
            disk_usage._prune_orphan_worktrees(home, dry_run=False, mgr=WorktreeManager())
        assert escaped.exists()  # never touched despite being reachable from a monkeypatched scan


class TestRobustRmtree:
    def test_removes_read_only_files(self, tmp_path):
        d = tmp_path / "ro"
        d.mkdir()
        f = d / "locked.txt"
        f.write_text("data")
        os.chmod(f, stat.S_IREAD)
        ok, leftover = disk_usage.robust_rmtree(d)
        assert ok is True
        assert leftover == []
        assert not d.exists()

    def test_reports_incomplete_removal_instead_of_claiming_success(self, tmp_path, monkeypatch):
        d = tmp_path / "stuck"
        d.mkdir()
        (d / "f.txt").write_text("x")

        def fake_rmtree(path, onerror=None):
            raise OSError("simulated stuck handle")

        monkeypatch.setattr(disk_usage.shutil, "rmtree", fake_rmtree)
        ok, leftover = disk_usage.robust_rmtree(d)
        assert ok is False
        assert leftover  # never silently reports success on a partial delete

    def test_win_long_path_prefixes_once(self, tmp_path):
        p = tmp_path / "x"
        s = disk_usage._win_long_path(p)
        assert s.startswith("\\\\?\\")
        assert s.count("\\\\?\\") == 1

    def test_robust_rmtree_uses_extended_prefix_on_win32(self, tmp_path, monkeypatch):
        d = tmp_path / "nested"
        d.mkdir()
        captured = {}

        def spy_rmtree(target, onerror=None):
            captured["target"] = target

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(disk_usage.shutil, "rmtree", spy_rmtree)
        disk_usage.robust_rmtree(d)
        assert captured["target"].startswith("\\\\?\\")


class TestDiskReport:
    def test_categories_are_json_serialisable_and_ranked(self, tmp_path, monkeypatch):
        # disk_report always scans claude-config regardless of `data_home` —
        # pin it under tmp_path so this never reads the real user's config.
        cfg_dir = tmp_path / "claude-config"
        (cfg_dir / "projects").mkdir(parents=True)
        (cfg_dir / "shell-snapshots").mkdir(parents=True)
        monkeypatch.setattr(disk_usage, "default_claude_config_dir", lambda: cfg_dir)
        (tmp_path / "venv").mkdir()
        report = disk_usage.disk_report(tmp_path)
        assert report["data_home"] == str(tmp_path.resolve())
        keys = {c["key"] for c in report["categories"]}
        assert {
            "venv",
            "chat-history",
            "shell-snapshots",
            "orphan-worktrees",
            "node-modules",
        } <= keys
        never = next(c for c in report["categories"] if c["key"] == "venv")
        assert never["level"] == "never"
