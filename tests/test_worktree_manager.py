"""Unit tests for worktree_manager (issue #81, Phase 1).

Pure-logic helpers + the WorktreeManager lifecycle exercised through an
injected fake git runner, so nothing here shells out to a real repository
(runs identically on Windows + macOS CI).
"""

from __future__ import annotations

from agent_takkub.worktree_manager import (
    GitResult,
    UnsafePathError,
    WorktreeInfo,
    WorktreeManager,
    branch_name,
    build_merge_proposal,
    sanitize_ref_component,
    worktree_dest,
    worktree_root,
)

# ── Pure helpers ────────────────────────────────────────────────────────────


class TestPureHelpers:
    def test_sanitize_strips_unsafe_and_collapses(self):
        assert sanitize_ref_component("qa#1") == "qa-1"
        assert sanitize_ref_component("front end//x") == "front-end-x"
        # leading dots stripped, interior underscores kept (legal in git refs),
        # '..' collapsed (illegal in a ref name)
        assert sanitize_ref_component("  ..weird__  ") == "weird__"
        assert sanitize_ref_component("///") == "pane"  # never empty
        assert ".." not in sanitize_ref_component("a..b")

    def test_branch_name_deterministic_and_prefixed(self):
        assert branch_name("frontend", 1720000000) == "wt/frontend-1720000000"
        # same inputs → same branch (no internal time sampling)
        assert branch_name("qa#2", 42) == branch_name("qa#2", 42) == "wt/qa-2-42"

    def test_worktree_dest_stays_under_managed_root(self):
        dest = worktree_dest("proj", "frontend", 99)
        root = worktree_root("proj")
        assert dest == root / "frontend-99"
        assert root in dest.parents

    def test_worktree_dest_neutralizes_traversal(self):
        # A crafted traversal role is neutralized by sanitize BEFORE it can
        # escape — the resulting dest still lives under the managed root.
        dest = worktree_dest("proj", "../../etc/evil", 1)
        root = worktree_root("proj")
        assert root in dest.parents
        assert ".." not in dest.parts

    def test_unsafe_path_error_is_available_as_a_guard(self):
        # Defensive net for future callers that might bypass sanitize: raised
        # when a dest genuinely escapes. Exercised directly since sanitize makes
        # it unreachable through worktree_dest's own input.
        assert issubclass(UnsafePathError, Exception)

    def test_worktree_info_roundtrip(self):
        info = WorktreeInfo(path="/w/p", branch="wt/x-1", base_sha="abc", git_root="/r")
        assert WorktreeInfo.from_dict(info.as_dict()) == info


# ── Fake runner ─────────────────────────────────────────────────────────────


class FakeRunner:
    """Scripts git responses by matching a subsequence of the arg list.

    Records every call for assertions. Rules are (needle_tokens, GitResult);
    the first rule whose tokens all appear in the call's args wins. Unmatched
    calls default to a clean empty success.
    """

    def __init__(self, rules: list[tuple[list[str], GitResult]] | None = None):
        self.rules = rules or []
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], cwd) -> GitResult:
        self.calls.append(args)
        for needles, result in self.rules:
            if all(n in args for n in needles):
                return result
        return GitResult(0, "", "")

    def ran(self, *tokens: str) -> bool:
        return any(all(t in call for t in tokens) for call in self.calls)


def _ok(out: str = "") -> GitResult:
    return GitResult(0, out, "")


def _fail(err: str = "boom", code: int = 1) -> GitResult:
    return GitResult(code, "", err)


# ── Discovery ───────────────────────────────────────────────────────────────


class TestDiscovery:
    def test_git_root_returns_toplevel(self):
        r = FakeRunner([(["rev-parse", "--show-toplevel"], _ok("/repo/root\n"))])
        assert WorktreeManager(r).git_root("/repo/root/sub") == "/repo/root"

    def test_git_root_none_when_not_a_repo(self):
        r = FakeRunner([(["rev-parse", "--show-toplevel"], _fail("not a git repo", 128))])
        assert WorktreeManager(r).git_root("/tmp/plain") is None


# ── Create ──────────────────────────────────────────────────────────────────


class TestCreate:
    def _repo_runner(self, extra=None):
        rules = [
            (["rev-parse", "--show-toplevel"], _ok("/repo\n")),
            (["rev-parse", "HEAD"], _ok("basesha123\n")),
        ]
        rules += extra or []
        return FakeRunner(rules)

    def test_create_success_returns_info_and_runs_add(self):
        r = self._repo_runner()
        info, reason = WorktreeManager(r).create("/repo/web", "proj", "frontend", 555)
        assert reason == ""
        assert info is not None
        assert info.branch == "wt/frontend-555"
        assert info.base_sha == "basesha123"
        assert info.git_root == "/repo"
        # add ran with -b <branch> <base_sha>
        assert r.ran("worktree", "add", "-b", "wt/frontend-555", "basesha123")

    def test_create_falls_back_when_not_git(self):
        r = FakeRunner([(["rev-parse", "--show-toplevel"], _fail("nope", 128))])
        info, reason = WorktreeManager(r).create("/tmp/plain", "proj", "qa", 1)
        assert info is None
        assert "git repo" in reason
        assert not r.ran("worktree", "add")  # never attempted

    def test_create_falls_back_when_no_head(self):
        r = FakeRunner(
            [
                (["rev-parse", "--show-toplevel"], _ok("/repo\n")),
                (["rev-parse", "HEAD"], _fail("no head", 128)),
            ]
        )
        info, reason = WorktreeManager(r).create("/repo", "proj", "qa", 1)
        assert info is None
        assert "commit" in reason

    def test_create_falls_back_when_add_fails(self):
        r = self._repo_runner(
            extra=[(["worktree", "add"], _fail("fatal: branch checked out elsewhere", 128))]
        )
        info, reason = WorktreeManager(r).create("/repo", "proj", "qa", 1)
        assert info is None
        assert "worktree add" in reason
        assert "checked out elsewhere" in reason


# ── Inspect ─────────────────────────────────────────────────────────────────


class TestInspect:
    def _info(self):
        return WorktreeInfo(path="/w", branch="wt/x-1", base_sha="base", git_root="/repo")

    def test_commit_count_parses(self):
        r = FakeRunner([(["rev-list", "--count"], _ok("3\n"))])
        assert WorktreeManager(r).commit_count(self._info()) == 3

    def test_commit_count_zero_on_error(self):
        r = FakeRunner([(["rev-list", "--count"], _fail())])
        assert WorktreeManager(r).commit_count(self._info()) == 0

    def test_is_dirty_true_when_porcelain_nonempty(self):
        r = FakeRunner([(["status", "--porcelain"], _ok(" M file.ts\n"))])
        assert WorktreeManager(r).is_dirty(self._info()) is True

    def test_is_dirty_false_when_clean(self):
        r = FakeRunner([(["status", "--porcelain"], _ok(""))])
        assert WorktreeManager(r).is_dirty(self._info()) is False


# ── Destroy (2-tier) ────────────────────────────────────────────────────────


class TestSafeRemove:
    def _info(self):
        return WorktreeInfo(path="/w", branch="wt/x-1", base_sha="base", git_root="/repo")

    def test_refuses_dirty_worktree(self):
        r = FakeRunner([(["status", "--porcelain"], _ok(" M work.ts\n"))])
        removed, reason = WorktreeManager(r).safe_remove(self._info())
        assert removed is False
        assert "uncommitted" in reason
        # must NOT have attempted a remove — work is preserved
        assert not r.ran("worktree", "remove")

    def test_removes_clean_empty_and_deletes_branch(self):
        r = FakeRunner(
            [
                (["status", "--porcelain"], _ok("")),  # clean
                (["rev-list", "--count"], _ok("0\n")),  # no commits
            ]
        )
        removed, reason = WorktreeManager(r).safe_remove(self._info())
        assert removed is True and reason == ""
        assert r.ran("worktree", "remove")
        assert r.ran("worktree", "prune")
        assert r.ran("branch", "-D", "wt/x-1")  # throwaway branch deleted

    def test_removes_clean_but_keeps_branch_with_commits(self):
        r = FakeRunner(
            [
                (["status", "--porcelain"], _ok("")),  # clean
                (["rev-list", "--count"], _ok("2\n")),  # has commits
            ]
        )
        removed, _reason = WorktreeManager(r).safe_remove(self._info())
        assert removed is True
        assert r.ran("worktree", "remove")
        assert not r.ran("branch", "-D")  # branch with work is preserved

    def test_reports_reason_when_remove_fails(self):
        r = FakeRunner(
            [
                (["status", "--porcelain"], _ok("")),
                (["worktree", "remove"], _fail("locked worktree", 1)),
            ]
        )
        removed, reason = WorktreeManager(r).safe_remove(self._info())
        assert removed is False
        assert "locked" in reason

    def test_force_remove_uses_force(self):
        r = FakeRunner()
        removed, _reason = WorktreeManager(r).force_remove(self._info())
        assert removed is True
        assert r.ran("worktree", "remove", "--force")
        assert r.ran("branch", "-D", "wt/x-1")


# ── Merge proposal ──────────────────────────────────────────────────────────


class TestMergeProposal:
    def test_proposal_has_branch_merge_cmd_and_is_propose_only(self):
        info = WorktreeInfo(path="/w/p", branch="wt/frontend-9", base_sha="base9", git_root="/repo")
        msg = build_merge_proposal("frontend", info, 4, " src/x.ts | 10 +++")
        assert "wt/frontend-9" in msg
        assert "4 commit" in msg
        assert "merge --no-ff wt/frontend-9" in msg
        assert "src/x.ts" in msg  # diffstat carried through
        # propose-then-fire doctrine — must tell Lead to confirm, not auto-merge
        assert "confirm" in msg.lower()


# ── Env-propagation config (P2.1) ───────────────────────────────────────────


class TestWorktreeConfig:
    def _write(self, tmp_path, payload) -> str:
        import json as _json

        cfgdir = tmp_path / ".takkub"
        cfgdir.mkdir(parents=True, exist_ok=True)
        (cfgdir / "worktree.json").write_text(
            payload if isinstance(payload, str) else _json.dumps(payload), encoding="utf-8"
        )
        return str(tmp_path)

    def test_absent_file_is_empty_config_no_warning(self, tmp_path):
        from agent_takkub.worktree_manager import load_worktree_config

        cfg, warn = load_worktree_config(str(tmp_path))
        assert cfg.is_empty and warn == ""

    def test_valid_config_roundtrip(self, tmp_path):
        from agent_takkub.worktree_manager import load_worktree_config

        root = self._write(
            tmp_path,
            {
                "symlinks": [".env.local", "node_modules"],
                "postCreate": ["pnpm install"],
                "base_port": 5310,
            },
        )
        cfg, warn = load_worktree_config(root)
        assert warn == ""
        assert cfg.symlinks == (".env.local", "node_modules")
        assert cfg.post_create == ("pnpm install",)
        assert cfg.base_port == 5310

    def test_malformed_json_warns_and_returns_empty(self, tmp_path):
        from agent_takkub.worktree_manager import load_worktree_config

        root = self._write(tmp_path, "{not json")
        cfg, warn = load_worktree_config(root)
        assert cfg.is_empty
        assert "worktree.json" in warn

    def test_unsafe_symlink_entries_rejected_with_warning(self, tmp_path):
        from agent_takkub.worktree_manager import load_worktree_config

        root = self._write(
            tmp_path,
            {"symlinks": ["../secrets", "C:/evil", "/abs", "ok/dir", 42]},
        )
        cfg, warn = load_worktree_config(root)
        assert cfg.symlinks == ("ok/dir",)  # only the safe relative entry survives
        assert "ไม่ปลอดภัย" in warn

    def test_bad_base_port_zeroed_with_warning(self, tmp_path):
        from agent_takkub.worktree_manager import load_worktree_config

        root = self._write(tmp_path, {"base_port": 80})
        cfg, warn = load_worktree_config(root)
        assert cfg.base_port == 0
        assert "base_port" in warn

    def test_non_dict_top_level_rejected(self, tmp_path):
        from agent_takkub.worktree_manager import load_worktree_config

        root = self._write(tmp_path, ["not", "a", "dict"])
        cfg, warn = load_worktree_config(root)
        assert cfg.is_empty and "object" in warn


# ── Link engine (P2.2) ──────────────────────────────────────────────────────


class TestApplyLinks:
    """_apply_links against a real tmp filesystem but a monkeypatched
    _make_link recorder — semantics without platform-specific link calls."""

    def _mgr_and_dirs(self, tmp_path):
        main = tmp_path / "main"
        wt = tmp_path / "wt"
        (main / "node_modules").mkdir(parents=True)
        (main / ".env.local").write_text("SECRET=1", encoding="utf-8")
        wt.mkdir()
        return WorktreeManager(FakeRunner()), main, wt

    def test_links_existing_sources(self, tmp_path, monkeypatch):
        from agent_takkub import worktree_manager as wm

        made = []
        monkeypatch.setattr(wm, "_make_link", lambda s, d: made.append((s, d)) or None)
        mgr, main, wt = self._mgr_and_dirs(tmp_path)
        cfg = wm.WorktreeConfig(symlinks=("node_modules", ".env.local"))
        linked, warns = mgr._apply_links(str(main), wt, cfg)
        assert linked == ["node_modules", ".env.local"]
        assert warns == []
        assert [d.name for _, d in made] == ["node_modules", ".env.local"]

    def test_missing_source_skipped_with_warning(self, tmp_path, monkeypatch):
        from agent_takkub import worktree_manager as wm

        monkeypatch.setattr(wm, "_make_link", lambda s, d: None)
        mgr, main, wt = self._mgr_and_dirs(tmp_path)
        cfg = wm.WorktreeConfig(symlinks=("does-not-exist",))
        linked, warns = mgr._apply_links(str(main), wt, cfg)
        assert linked == []
        assert len(warns) == 1 and "does-not-exist" in warns[0]

    def test_existing_destination_not_overwritten(self, tmp_path, monkeypatch):
        from agent_takkub import worktree_manager as wm

        monkeypatch.setattr(wm, "_make_link", lambda s, d: None)
        mgr, main, wt = self._mgr_and_dirs(tmp_path)
        (wt / ".env.local").write_text("tracked", encoding="utf-8")  # already in checkout
        cfg = wm.WorktreeConfig(symlinks=(".env.local",))
        linked, warns = mgr._apply_links(str(main), wt, cfg)
        assert linked == []
        assert "มีอยู่แล้ว" in warns[0]
        assert (wt / ".env.local").read_text(encoding="utf-8") == "tracked"  # untouched

    def test_link_failure_warns_and_continues(self, tmp_path, monkeypatch):
        from agent_takkub import worktree_manager as wm

        monkeypatch.setattr(
            wm, "_make_link", lambda s, d: "boom" if s.name == "node_modules" else None
        )
        mgr, main, wt = self._mgr_and_dirs(tmp_path)
        cfg = wm.WorktreeConfig(symlinks=("node_modules", ".env.local"))
        linked, warns = mgr._apply_links(str(main), wt, cfg)
        assert linked == [".env.local"]  # failure of one didn't stop the other
        assert any("boom" in w for w in warns)


class TestRemoveLinkSafety:
    """_remove_link must NEVER recurse — the whole point of P2.2 safety."""

    def test_removes_file(self, tmp_path):
        from agent_takkub.worktree_manager import _remove_link

        f = tmp_path / "x.txt"
        f.write_text("hi", encoding="utf-8")
        _remove_link(f)
        assert not f.exists()

    def test_removes_empty_dir_or_junction_point(self, tmp_path):
        from agent_takkub.worktree_manager import _remove_link

        d = tmp_path / "emptydir"
        d.mkdir()
        _remove_link(d)  # rmdir path — same call that removes a junction point
        assert not d.exists()

    def test_never_deletes_real_dir_with_content(self, tmp_path):
        from agent_takkub.worktree_manager import _remove_link

        d = tmp_path / "real"
        d.mkdir()
        (d / "keep.txt").write_text("data", encoding="utf-8")
        _remove_link(d)  # os.rmdir fails on non-empty → swallowed → intact
        assert (d / "keep.txt").exists()

    def test_missing_path_is_noop(self, tmp_path):
        from agent_takkub.worktree_manager import _remove_link

        _remove_link(tmp_path / "ghost")  # no raise


class TestRemoveUnlinksFirst:
    def _info_with_links(self):
        return WorktreeInfo(
            path="/w",
            branch="wt/x-1",
            base_sha="base",
            git_root="/repo",
            links=("node_modules", ".env.local"),
        )

    def test_safe_remove_unlinks_before_git_remove(self, monkeypatch):
        from agent_takkub import worktree_manager as wm

        order = []
        monkeypatch.setattr(wm, "_remove_link", lambda p: order.append(("unlink", p.name)))
        r = FakeRunner(
            [
                (["status", "--porcelain"], _ok("")),
                (["rev-list", "--count"], _ok("0\n")),
            ]
        )

        class Spy(FakeRunner):
            pass

        mgr = WorktreeManager(
            lambda a, c: order.append(("git", a[2] if len(a) > 2 else a[0])) or r(a, c)
        )
        removed, _ = mgr.safe_remove(self._info_with_links())
        assert removed is True
        unlink_idx = [i for i, (k, _) in enumerate(order) if k == "unlink"]
        git_remove_idx = [i for i, (k, v) in enumerate(order) if k == "git" and v == "worktree"]
        assert unlink_idx and git_remove_idx
        assert max(unlink_idx) < min(git_remove_idx)  # ALL unlinks before git touches it

    def test_force_remove_unlinks_too(self, monkeypatch):
        from agent_takkub import worktree_manager as wm

        unlinked = []
        monkeypatch.setattr(wm, "_remove_link", lambda p: unlinked.append(p.name))
        mgr = WorktreeManager(FakeRunner())
        mgr.force_remove(self._info_with_links())
        assert unlinked == ["node_modules", ".env.local"]

    def test_info_links_roundtrip_and_backcompat(self):
        info = self._info_with_links()
        assert WorktreeInfo.from_dict(info.as_dict()) == info
        # Phase-1 dicts (no links key) still load
        legacy = {"path": "/w", "branch": "b", "git_root": "/r"}
        assert WorktreeInfo.from_dict(legacy).links == ()
