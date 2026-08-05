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

from agent_takkub import disk_usage, graft_store
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

    def test_orphan_not_in_worktree_list_still_reports_git_state(self, tmp_path):
        """#132: `git worktree list` doesn't know this path, but the checkout
        itself still responds to git directly — dirty/branch/ahead must be
        filled in rather than the classifier bailing out blind."""
        wt = tmp_path / "worktrees" / "proj" / "backend-11"
        wt.mkdir(parents=True)
        (wt / ".git").write_text("gitdir: /repo/.git/worktrees/backend-11\n")
        # `git worktree list` lists a DIFFERENT worktree — this one is absent.
        porcelain = _porcelain([("/repo/other", "abc123", "wt/other-1")])

        def runner(args, cwd):
            if "rev-parse" in args and "--show-toplevel" in args:
                return GitResult(0, "/repo\n", "")
            if "worktree" in args and "list" in args and "--porcelain" in args:
                return GitResult(0, porcelain, "")
            if "rev-parse" in args and "--abbrev-ref" in args:
                return GitResult(0, "wt/backend-11-1\n", "")
            if "rev-list" in args and "--count" in args:
                return GitResult(0, "2\n", "")
            if "status" in args and "--porcelain" in args:
                return GitResult(0, "M x\n", "")
            return GitResult(0, "", "")

        mgr = WorktreeManager(runner=runner)
        info = disk_usage.classify_worktree(wt, mgr)
        assert info["orphan"] is True
        assert info["branch"] == "wt/backend-11-1"
        assert info["dirty"] is True
        assert info["ahead"] == 2

    def test_orphan_unreadable_git_leaves_state_none(self, tmp_path):
        wt = tmp_path / "worktrees" / "proj" / "backend-12"
        wt.mkdir(parents=True)
        mgr = WorktreeManager(
            runner=lambda a, c: (_ for _ in ()).throw(AssertionError("git called"))
        )
        info = disk_usage.classify_worktree(wt, mgr)
        assert info["dirty"] is None
        assert info["branch"] is None
        assert info["ahead"] is None


class TestOrphanPruneBucket:
    def test_empty_dir_is_safe(self, tmp_path):
        wt = tmp_path / "wt-empty"
        wt.mkdir()
        assert disk_usage._orphan_prune_bucket(wt, {}) == disk_usage.SAFE

    def test_node_modules_only_is_safe(self, tmp_path):
        wt = tmp_path / "wt-nm"
        (wt / "node_modules" / "pkg").mkdir(parents=True)
        (wt / "node_modules" / "pkg" / "index.js").write_text("x")
        assert disk_usage._orphan_prune_bucket(wt, {}) == disk_usage.SAFE

    def test_nested_node_modules_only_is_safe(self, tmp_path):
        wt = tmp_path / "wt-nm-nested"
        (wt / "apps" / "web" / "node_modules" / "pkg").mkdir(parents=True)
        (wt / "apps" / "web" / "node_modules" / "pkg" / "index.js").write_text("x")
        assert disk_usage._orphan_prune_bucket(wt, {}) == disk_usage.SAFE

    def test_source_file_forces_review(self, tmp_path):
        wt = tmp_path / "wt-src"
        wt.mkdir()
        (wt / "app.py").write_text("print('hi')")
        assert disk_usage._orphan_prune_bucket(wt, {}) == disk_usage.REVIEW

    def test_source_file_alongside_node_modules_forces_review(self, tmp_path):
        wt = tmp_path / "wt-mixed"
        (wt / "node_modules" / "pkg").mkdir(parents=True)
        (wt / "src").mkdir()
        (wt / "src" / "index.ts").write_text("export {}")
        assert disk_usage._orphan_prune_bucket(wt, {}) == disk_usage.REVIEW

    def test_dirty_forces_review_even_when_content_looks_safe(self, tmp_path):
        wt = tmp_path / "wt-dirty"
        wt.mkdir()
        assert disk_usage._orphan_prune_bucket(wt, {"dirty": True}) == disk_usage.REVIEW

    def test_unmerged_commits_force_review_even_when_content_looks_safe(self, tmp_path):
        wt = tmp_path / "wt-ahead"
        wt.mkdir()
        assert disk_usage._orphan_prune_bucket(wt, {"ahead": 1}) == disk_usage.REVIEW


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
        result = disk_usage.prune(
            categories=["orphan-worktrees"], dry_run=False, data_home=tmp_path
        )
        assert not wt.exists()
        assert result["categories"][0]["removed_count"] == 1

    def test_orphan_worktree_with_source_file_is_never_touched_by_safe_category(self, tmp_path):
        """#132: a source file (not node_modules) is the exact shape that must
        never be auto-deleted, even with --yes at the default (safe) level."""
        wt = tmp_path / "worktrees" / "proj" / "frontend-11"
        wt.mkdir(parents=True)
        (wt / "leftover.py").write_text("print('important')")
        result = disk_usage.prune(
            categories=["orphan-worktrees"], dry_run=False, data_home=tmp_path
        )
        assert wt.exists()
        assert (wt / "leftover.py").read_text() == "print('important')"
        assert result["categories"][0]["target_count"] == 0
        assert result["categories"][0]["removed_count"] == 0

    def test_orphan_worktree_with_source_file_needs_review_category_and_yes(self, tmp_path):
        wt = tmp_path / "worktrees" / "proj" / "frontend-12"
        wt.mkdir(parents=True)
        (wt / "leftover.py").write_text("print('important')")
        result = disk_usage.prune(
            categories=["orphan-worktrees-review"],
            level="review",
            dry_run=False,
            data_home=tmp_path,
        )
        assert not wt.exists()
        assert result["categories"][0]["removed_count"] == 1
        assert result["categories"][0]["targets"] == [str(wt)]

    def test_orphan_worktree_with_node_modules_only_deleted_by_safe_category(self, tmp_path):
        wt = tmp_path / "worktrees" / "proj" / "frontend-13"
        (wt / "node_modules" / "pkg").mkdir(parents=True)
        (wt / "node_modules" / "pkg" / "index.js").write_text("x")
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


class TestBootSweepConservative:
    """`prune_orphan_worktrees_boot` — the auto-sweep that runs unattended at
    cockpit startup. #132: it must never delete a checkout that still holds
    something a user could lose."""

    def test_leaves_orphan_with_source_files_untouched(self, tmp_path):
        wt = tmp_path / "worktrees" / "proj" / "frontend-boot-1"
        wt.mkdir(parents=True)
        (wt / "App.tsx").write_text("export default function App() {}")
        removed = disk_usage.prune_orphan_worktrees_boot(data_home=tmp_path)
        assert removed == 0
        assert wt.exists()
        assert (wt / "App.tsx").exists()

    def test_removes_orphan_that_is_only_node_modules(self, tmp_path):
        wt = tmp_path / "worktrees" / "proj" / "frontend-boot-2"
        (wt / "node_modules" / "pkg").mkdir(parents=True)
        (wt / "node_modules" / "pkg" / "index.js").write_text("x")
        removed = disk_usage.prune_orphan_worktrees_boot(data_home=tmp_path)
        assert removed == 1
        assert not wt.exists()

    def test_removes_orphan_that_is_completely_empty(self, tmp_path):
        wt = tmp_path / "worktrees" / "proj" / "frontend-boot-3"
        wt.mkdir(parents=True)
        removed = disk_usage.prune_orphan_worktrees_boot(data_home=tmp_path)
        assert removed == 1
        assert not wt.exists()

    def test_leaves_orphan_with_unmerged_branch_commits_untouched(self, tmp_path, monkeypatch):
        """#132: even an otherwise-empty checkout must survive the boot sweep
        when its branch still carries commits nobody merged — deleting the
        checkout would strand the pane's work behind a branch name nobody
        knows to look for."""
        wt = tmp_path / "worktrees" / "proj" / "backend-boot-4"
        wt.mkdir(parents=True)
        (wt / ".git").write_text("gitdir: /repo/.git/worktrees/backend-boot-4\n")
        # `git worktree list` doesn't know this path anymore (already
        # unregistered), but the checkout itself still answers git directly
        # and its branch has 2 commits nothing has merged yet.
        porcelain = _porcelain([("/repo/other", "abc123", "wt/other-1")])

        def runner(args, cwd):
            if "rev-parse" in args and "--show-toplevel" in args:
                return GitResult(0, "/repo\n", "")
            if "worktree" in args and "list" in args and "--porcelain" in args:
                return GitResult(0, porcelain, "")
            if "rev-parse" in args and "--abbrev-ref" in args:
                return GitResult(0, "wt/backend-boot-4-1\n", "")
            if "rev-list" in args and "--count" in args:
                return GitResult(0, "2\n", "")
            if "status" in args and "--porcelain" in args:
                return GitResult(0, "", "")
            return GitResult(0, "", "")

        monkeypatch.setattr(disk_usage, "WorktreeManager", lambda: WorktreeManager(runner=runner))
        removed = disk_usage.prune_orphan_worktrees_boot(data_home=tmp_path)
        assert removed == 0
        assert wt.exists()


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


class TestGraftGraphsScan:
    """`graft-graphs/` — external code-graph store (#146 follow-up). Orphan
    classification compares each store dir's own name (its `graph_key`)
    against every path currently configured in projects.json, so every test
    here isolates `config.PROJECTS_JSON` under tmp_path — never the real
    dev machine's project list.

    The store root itself is never DATA_HOME-relative (see graft_store.py's
    module docstring), so `scan_graft_graphs`'s `data_home` arg is a no-op —
    every test here monkeypatches `graft_store.GRAFT_STORE_ROOT` directly to
    an isolated tmp dir instead."""

    def _isolate_projects(self, monkeypatch, tmp_path, projects: dict) -> None:
        import json as _json

        from agent_takkub import config

        pj = tmp_path / "projects.json"
        pj.write_text(_json.dumps({"active": None, "projects": projects}), encoding="utf-8")
        monkeypatch.setattr(config, "PROJECTS_JSON", pj)

    def test_no_root_dir_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(graft_store, "GRAFT_STORE_ROOT", tmp_path / "graft-graphs")
        result = disk_usage.scan_graft_graphs(tmp_path)
        assert result == {
            "entries": [],
            "orphan_bytes": 0,
            "orphan_count": 0,
            "live_bytes": 0,
            "live_count": 0,
        }

    def test_live_store_matches_configured_project_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(graft_store, "GRAFT_STORE_ROOT", tmp_path / "graft-graphs")
        target = tmp_path / "some-project" / "api"
        target.mkdir(parents=True)
        self._isolate_projects(monkeypatch, tmp_path, {"proj": {"paths": {"api": str(target)}}})

        store = tmp_path / "graft-graphs" / graft_store.graph_key(target)
        (store / "graft").mkdir(parents=True)
        (store / "graft" / "index.json").write_bytes(b"x" * 40)

        result = disk_usage.scan_graft_graphs(tmp_path)
        assert result["live_count"] == 1
        assert result["live_bytes"] == 40
        assert result["orphan_count"] == 0

    def test_orphan_store_not_referenced_by_any_project(self, tmp_path, monkeypatch):
        monkeypatch.setattr(graft_store, "GRAFT_STORE_ROOT", tmp_path / "graft-graphs")
        self._isolate_projects(monkeypatch, tmp_path, {})

        store = tmp_path / "graft-graphs" / ("0" * 64)
        (store / "graft").mkdir(parents=True)
        (store / "graft" / "index.json").write_bytes(b"y" * 25)

        result = disk_usage.scan_graft_graphs(tmp_path)
        assert result["orphan_count"] == 1
        assert result["orphan_bytes"] == 25
        assert result["live_count"] == 0

    def test_entry_carries_manifest_source_when_present(self, tmp_path, monkeypatch):
        monkeypatch.setattr(graft_store, "GRAFT_STORE_ROOT", tmp_path / "graft-graphs")
        self._isolate_projects(monkeypatch, tmp_path, {})
        target = tmp_path / "orphaned-project"
        target.mkdir()
        graft_store.write_store_manifest(target)

        result = disk_usage.scan_graft_graphs(tmp_path)
        assert result["entries"][0]["source"] == str(target.resolve())
        assert result["entries"][0]["orphan"] is True  # not in projects.json paths


class TestPruneGraftGraphs:
    def test_dry_run_does_not_delete_orphan_store(self, tmp_path, monkeypatch):
        from agent_takkub import config

        monkeypatch.setattr(graft_store, "GRAFT_STORE_ROOT", tmp_path / "graft-graphs")
        pj = tmp_path / "projects.json"
        pj.write_text('{"active": null, "projects": {}}', encoding="utf-8")
        monkeypatch.setattr(config, "PROJECTS_JSON", pj)
        store = tmp_path / "graft-graphs" / ("1" * 64)
        (store / "graft").mkdir(parents=True)

        result = disk_usage.prune(categories=["graft-graphs"], dry_run=True, data_home=tmp_path)
        assert store.exists()
        assert result["categories"][0]["target_count"] == 1

    def test_yes_removes_orphan_store_only(self, tmp_path, monkeypatch):
        from agent_takkub import config

        monkeypatch.setattr(graft_store, "GRAFT_STORE_ROOT", tmp_path / "graft-graphs")
        live_target = tmp_path / "live-project"
        live_target.mkdir()
        pj = tmp_path / "projects.json"
        import json as _json

        pj.write_text(
            _json.dumps({"active": None, "projects": {"p": {"paths": {"a": str(live_target)}}}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "PROJECTS_JSON", pj)

        live_store = tmp_path / "graft-graphs" / graft_store.graph_key(live_target)
        (live_store / "graft").mkdir(parents=True)
        orphan_store = tmp_path / "graft-graphs" / ("2" * 64)
        (orphan_store / "graft").mkdir(parents=True)

        result = disk_usage.prune(categories=["graft-graphs"], dry_run=False, data_home=tmp_path)
        assert not orphan_store.exists()
        assert live_store.exists()
        assert result["categories"][0]["removed_count"] == 1

    def test_never_deletes_outside_store_root(self, tmp_path, monkeypatch):
        # `_prune_graft_graphs` reuses `_assert_under` like every other
        # category — a store dir reported outside `graft_store.GRAFT_STORE_ROOT`
        # (the real safety boundary now the store is never DATA_HOME-relative)
        # must refuse rather than delete, same guarantee TestPathSafety
        # asserts elsewhere.
        from agent_takkub import config

        monkeypatch.setattr(graft_store, "GRAFT_STORE_ROOT", tmp_path / "graft-graphs")
        pj = tmp_path / "projects.json"
        pj.write_text('{"active": null, "projects": {}}', encoding="utf-8")
        monkeypatch.setattr(config, "PROJECTS_JSON", pj)
        home = tmp_path / "home"
        home.mkdir()
        escaped = tmp_path / "outside" / "evil-store"  # not under GRAFT_STORE_ROOT
        monkeypatch.setattr(
            disk_usage,
            "scan_graft_graphs",
            lambda h: {
                "entries": [{"path": str(escaped), "orphan": True, "size_bytes": 1, "source": None}]
            },
        )
        with pytest.raises(ValueError):
            disk_usage._prune_graft_graphs(home, dry_run=False)


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
            "orphan-worktrees-review",
            "node-modules",
        } <= keys
        never = next(c for c in report["categories"] if c["key"] == "venv")
        assert never["level"] == "never"

    def test_orphan_with_source_files_counted_under_review_not_safe(self, tmp_path, monkeypatch):
        cfg_dir = tmp_path / "claude-config"
        (cfg_dir / "projects").mkdir(parents=True)
        (cfg_dir / "shell-snapshots").mkdir(parents=True)
        monkeypatch.setattr(disk_usage, "default_claude_config_dir", lambda: cfg_dir)
        wt = tmp_path / "worktrees" / "proj" / "frontend-report-1"
        wt.mkdir(parents=True)
        (wt / "leftover.py").write_text("print('important')")

        report = disk_usage.disk_report(tmp_path)
        safe_cat = next(c for c in report["categories"] if c["key"] == "orphan-worktrees")
        review_cat = next(c for c in report["categories"] if c["key"] == "orphan-worktrees-review")
        assert safe_cat["file_count"] == 0
        assert review_cat["file_count"] == 1
        assert review_cat["level"] == "review"
        rows = report["worktrees"]["orphan"]
        assert next(r for r in rows if r["path"] == str(wt))["prune_bucket"] == "review"
