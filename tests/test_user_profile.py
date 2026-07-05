"""Tests for user_profile — per-project Claude account selection."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_takkub import user_profile as up


@pytest.fixture(autouse=True)
def isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect all file I/O to a temp dir so tests don't touch ~/.takkub."""
    monkeypatch.setattr(up, "_BASE_DIR", tmp_path)
    monkeypatch.setattr(up, "_REGISTRY_PATH", tmp_path / "user-profiles.json")
    monkeypatch.setattr(up, "_DEFAULT_CONFIG_DIR", tmp_path / "dot-claude")


# ─────────────────────────── list_profiles ────────────────────────────────


class TestListProfiles:
    def test_default_always_first(self) -> None:
        profiles = up.list_profiles()
        assert profiles[0]["name"] == "default"

    def test_default_config_dir_is_dot_claude(self) -> None:
        profiles = up.list_profiles()
        assert profiles[0]["config_dir"] == str(up._DEFAULT_CONFIG_DIR)

    def test_includes_registered_profiles(self) -> None:
        up.add_profile("work", "/home/user/.claude-work")
        names = [p["name"] for p in up.list_profiles()]
        assert "default" in names
        assert "work" in names

    def test_empty_registry_returns_only_default(self) -> None:
        assert len(up.list_profiles()) == 1


# ─────────────────────────── add_profile ──────────────────────────────────


class TestAddProfile:
    def test_add_and_retrieve(self) -> None:
        up.add_profile("personal", "/home/user/.claude-personal")
        names = [p["name"] for p in up.list_profiles()]
        assert "personal" in names

    def test_config_dir_stored_correctly(self) -> None:
        up.add_profile("work", "/some/config/dir")
        profiles = up.list_profiles()
        entry = next(p for p in profiles if p["name"] == "work")
        assert entry["config_dir"] == "/some/config/dir"

    def test_duplicate_name_raises(self) -> None:
        up.add_profile("work", "/a")
        with pytest.raises(ValueError, match="already exists"):
            up.add_profile("work", "/b")

    def test_default_name_reserved(self) -> None:
        with pytest.raises(ValueError, match="reserved"):
            up.add_profile("default", "/x")

    def test_invalid_name_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid profile name"):
            up.add_profile("bad name!", "/x")

    def test_empty_name_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid profile name"):
            up.add_profile("", "/x")

    def test_empty_config_dir_raises(self) -> None:
        with pytest.raises(ValueError, match="config_dir must not be empty"):
            up.add_profile("ok", "")


# ─────────────────────────── remove_profile ───────────────────────────────


class TestRemoveProfile:
    def test_remove_existing(self) -> None:
        up.add_profile("work", "/a")
        up.remove_profile("work")
        names = [p["name"] for p in up.list_profiles()]
        assert "work" not in names

    def test_remove_nonexistent_is_silent(self) -> None:
        up.remove_profile("nonexistent")  # should not raise

    def test_remove_default_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot remove"):
            up.remove_profile("default")


# ─────────────────────────── profile_for ──────────────────────────────────


class TestProfileFor:
    def test_no_selection_returns_default(self) -> None:
        assert up.profile_for("myproject") == "default"

    def test_returns_selected_profile(self) -> None:
        up.add_profile("work", "/a")
        up.set_profile("myproject", "work")
        assert up.profile_for("myproject") == "work"

    def test_corrupt_file_returns_default(self, tmp_path: Path) -> None:
        slug = up._project_slug("myproject")
        p = tmp_path / "projects" / slug / "user-profile.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("not-json", encoding="utf-8")
        assert up.profile_for("myproject") == "default"

    def test_missing_file_returns_default(self) -> None:
        assert up.profile_for("newproject") == "default"

    def test_removed_profile_falls_back_to_default(self) -> None:
        up.add_profile("work", "/a")
        up.set_profile("myproject", "work")
        up.remove_profile("work")
        assert up.profile_for("myproject") == "default"


# ─────────────────────────── set_profile ──────────────────────────────────


class TestSetProfile:
    def test_set_registered_profile(self) -> None:
        up.add_profile("work", "/a")
        up.set_profile("myproject", "work")
        assert up.profile_for("myproject") == "work"

    def test_set_default_is_allowed(self) -> None:
        up.add_profile("work", "/a")
        up.set_profile("myproject", "work")
        up.set_profile("myproject", "default")
        assert up.profile_for("myproject") == "default"

    def test_set_unknown_profile_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown profile"):
            up.set_profile("myproject", "ghost")

    def test_set_creates_file(self, tmp_path: Path) -> None:
        up.add_profile("work", "/a")
        up.set_profile("myproject", "work")
        slug = up._project_slug("myproject")
        assert (tmp_path / "projects" / slug / "user-profile.json").exists()


# ─────────────────────────── config_dir_for ───────────────────────────────


class TestConfigDirFor:
    def test_default_returns_dot_claude(self) -> None:
        assert up.config_dir_for("myproject") == up._DEFAULT_CONFIG_DIR

    def test_non_default_returns_registered_dir(self) -> None:
        up.add_profile("work", "/custom/config")
        up.set_profile("myproject", "work")
        assert up.config_dir_for("myproject") == Path("/custom/config")

    def test_fallback_on_removed_profile(self) -> None:
        up.add_profile("work", "/custom/config")
        up.set_profile("myproject", "work")
        up.remove_profile("work")
        assert up.config_dir_for("myproject") == up._DEFAULT_CONFIG_DIR


# ─────────────────────────── pane_env injection ───────────────────────────


class TestInjectUserProfileEnv:
    """inject_user_profile_env sets CLAUDE_CONFIG_DIR only for non-default profiles."""

    def test_default_profile_does_not_set_var(self) -> None:
        from agent_takkub.pane_env import inject_user_profile_env

        env: dict[str, str] = {}
        inject_user_profile_env(env, "myproject")
        assert "CLAUDE_CONFIG_DIR" not in env

    def test_non_default_sets_var(self) -> None:
        from agent_takkub.pane_env import inject_user_profile_env

        up.add_profile("work", "/custom/config")
        up.set_profile("myproject", "work")
        env: dict[str, str] = {}
        inject_user_profile_env(env, "myproject")
        assert env["CLAUDE_CONFIG_DIR"] == str(Path("/custom/config"))

    def test_does_not_overwrite_other_env_keys(self) -> None:
        from agent_takkub.pane_env import inject_user_profile_env

        up.add_profile("work", "/c")
        up.set_profile("proj", "work")
        env = {"PATH": "/usr/bin", "TAKKUB_ROLE": "backend"}
        inject_user_profile_env(env, "proj")
        assert env["PATH"] == "/usr/bin"
        assert env["TAKKUB_ROLE"] == "backend"
        assert env["CLAUDE_CONFIG_DIR"] == str(Path("/c"))

    def test_silent_on_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import agent_takkub.user_profile as _up
        from agent_takkub import pane_env

        monkeypatch.setattr(
            _up, "profile_for", lambda _p: (_ for _ in ()).throw(RuntimeError("boom"))
        )  # type: ignore[arg-type]
        env: dict[str, str] = {}
        pane_env.inject_user_profile_env(env, "any")  # must not raise
        assert "CLAUDE_CONFIG_DIR" not in env

    def test_installed_instance_sets_var_even_for_default_profile(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Installed builds isolate their default profile under DATA_HOME, so
        even the 'default' profile must set CLAUDE_CONFIG_DIR — otherwise
        every pane falls through to the OS-wide ~/.claude instead of the
        prod-scoped profile (isolation plan, finding C5)."""
        import agent_takkub.config as config_mod
        from agent_takkub.pane_env import inject_user_profile_env

        monkeypatch.setattr(config_mod, "DATA_HOME", tmp_path / "agent-takkub-home")
        monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path / "venv-lib")

        env: dict[str, str] = {}
        inject_user_profile_env(env, "myproject")

        assert env["CLAUDE_CONFIG_DIR"] == str(up._DEFAULT_CONFIG_DIR)


class TestSharedSessionProfiles:
    """Shared-session profiles — switch the ACCOUNT, keep sessions/plugins.

    Uses real links (junction on win / symlink on posix) in tmp dirs via the
    worktree_manager link helpers.
    """

    @pytest.fixture()
    def homes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        main = tmp_path / "claude-main"
        (main / "projects" / "proj-a").mkdir(parents=True)
        (main / "projects" / "proj-a" / "s1.jsonl").write_text("main", encoding="utf-8")
        monkeypatch.setattr(up, "_DEFAULT_CONFIG_DIR", main)
        return main, tmp_path / "claude-second"

    def test_provision_links_shared_items(self, homes) -> None:
        main, second = homes
        linked = up.provision_shared_profile(second)
        assert set(linked) == set(up.SHARED_ITEMS)
        # writing through the link lands in the main home = shared for real
        via_link = second / "projects" / "proj-a" / "s2.jsonl"
        via_link.parent.mkdir(parents=True, exist_ok=True)
        via_link.write_text("x", encoding="utf-8")
        assert (main / "projects" / "proj-a" / "s2.jsonl").exists()

    def test_provision_never_clobbers_existing(self, homes) -> None:
        _main, second = homes
        (second / "projects").mkdir(parents=True)
        (second / "projects" / "own.txt").write_text("mine", encoding="utf-8")
        linked = up.provision_shared_profile(second)
        assert "projects" not in linked  # left alone
        assert (second / "projects" / "own.txt").read_text(encoding="utf-8") == "mine"

    def test_convert_merges_then_links_with_backup(self, homes) -> None:
        main, second = homes
        old = second / "projects" / "proj-b"
        old.mkdir(parents=True)
        (old / "old-session.jsonl").write_text("old", encoding="utf-8")
        # collision: same rel path in both — the MAIN copy must win
        dup = second / "projects" / "proj-a"
        dup.mkdir(parents=True)
        (dup / "s1.jsonl").write_text("second-copy", encoding="utf-8")

        results = up.convert_profile_to_shared(second)
        assert "merged 1 file(s) in" in results["projects"]
        # old data now visible from the MAIN home
        assert (main / "projects" / "proj-b" / "old-session.jsonl").exists()
        # collision: main's file untouched
        assert (main / "projects" / "proj-a" / "s1.jsonl").read_text(encoding="utf-8") == "main"
        # original kept as backup, live path is now a link
        assert (second / "projects.pre-share-backup").is_dir()
        from agent_takkub.worktree_manager import _is_link_point

        assert _is_link_point(second / "projects")
        # idempotent
        again = up.convert_profile_to_shared(second)
        assert again["projects"] == "already shared"

    def test_cleanup_removes_links_only(self, homes) -> None:
        main, second = homes
        up.provision_shared_profile(second)
        removed = up.cleanup_profile_links(second)
        assert set(removed) == set(up.SHARED_ITEMS)
        # link points gone, shared data in the main home untouched
        assert not (second / "projects").exists()
        assert (main / "projects" / "proj-a" / "s1.jsonl").exists()

    def test_add_profile_share_sessions(self, homes, tmp_path, monkeypatch) -> None:
        _main, second = homes
        monkeypatch.setattr(up, "_REGISTRY_PATH", tmp_path / "reg.json")
        linked = up.add_profile("work2", second, share_sessions=True)
        assert set(linked) == set(up.SHARED_ITEMS)
        assert any(p["name"] == "work2" for p in up.list_profiles())


# ─────────────────────── bootstrap_default_profile ────────────────────────
# First-boot clone of ~/.claude into an installed instance's isolated
# default profile (DATA_HOME/claude-config), minus .credentials.json.
# (isolation plan, finding C5)


class TestBootstrapDefaultProfile:
    def test_noop_for_dev_checkout(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import agent_takkub.config as config_mod

        monkeypatch.setattr(config_mod, "DATA_HOME", config_mod.REPO_ROOT)

        assert up.bootstrap_default_profile() is False

    def test_noop_when_dest_has_completion_marker(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A dest with the marker is a completed profile — never re-touched,
        even though the real ~/.claude on the test machine may exist."""
        import agent_takkub.config as config_mod

        monkeypatch.setattr(config_mod, "DATA_HOME", tmp_path / "agent-takkub-home")
        monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path / "venv-lib")
        dest = tmp_path / "dot-claude"
        dest.mkdir()
        (dest / up._BOOTSTRAP_MARKER).write_text("", encoding="utf-8")
        (dest / "own-file.txt").write_text("mine", encoding="utf-8")

        assert up.bootstrap_default_profile() is False
        assert (dest / "own-file.txt").read_text(encoding="utf-8") == "mine"

    def test_noop_when_dest_has_credentials(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A dest with credentials proves the user already logged in there
        (e.g. a profile predating the completion marker) — never re-touched."""
        import agent_takkub.config as config_mod

        monkeypatch.setattr(config_mod, "DATA_HOME", tmp_path / "agent-takkub-home")
        monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path / "venv-lib")
        dest = tmp_path / "dot-claude"
        dest.mkdir()
        (dest / ".credentials.json").write_text('{"token": "secret"}', encoding="utf-8")

        assert up.bootstrap_default_profile() is False
        assert (dest / ".credentials.json").exists()

    def test_torn_dest_without_marker_or_credentials_is_recloned(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A dest with neither the marker nor credentials has no real user
        data (never logged in) — discarded and re-cloned rather than left
        as a permanently stuck torn profile."""
        import agent_takkub.config as config_mod

        monkeypatch.setattr(config_mod, "DATA_HOME", tmp_path / "agent-takkub-home")
        monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path / "venv-lib")

        home = tmp_path / "real-home"
        src = home / ".claude"
        src.mkdir(parents=True)
        (src / "settings.json").write_text('{"fresh": true}', encoding="utf-8")
        monkeypatch.setattr(Path, "home", lambda: home)

        dest = tmp_path / "dot-claude"
        dest.mkdir()
        (dest / "stray-leftover.txt").write_text("torn junk", encoding="utf-8")

        assert up.bootstrap_default_profile() is True
        assert not (dest / "stray-leftover.txt").exists()
        assert (dest / "settings.json").read_text(encoding="utf-8") == '{"fresh": true}'
        assert (dest / up._BOOTSTRAP_MARKER).exists()

    def test_noop_when_no_source_claude_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import agent_takkub.config as config_mod

        monkeypatch.setattr(config_mod, "DATA_HOME", tmp_path / "agent-takkub-home")
        monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path / "venv-lib")
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "no-home-claude-here")

        assert up.bootstrap_default_profile() is False
        assert not (tmp_path / "dot-claude").exists()

    def test_clones_allowlisted_items_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Core clone is selective: config/agents/skills/plugins come along,
        and a bounded number of recent session transcripts per project (see
        TestCloneRecentSessions) — but credentials and unrecognized heavy
        caches never do."""
        import agent_takkub.config as config_mod

        monkeypatch.setattr(config_mod, "DATA_HOME", tmp_path / "agent-takkub-home")
        monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path / "venv-lib")

        home = tmp_path / "real-home"
        src = home / ".claude"
        (src / "projects" / "proj-a").mkdir(parents=True)
        (src / "projects" / "proj-a" / "s1.jsonl").write_text("hi", encoding="utf-8")
        (src / "agents").mkdir(parents=True)
        (src / "agents" / "backend.md").write_text("role", encoding="utf-8")
        (src / "settings.json").write_text("{}", encoding="utf-8")
        (src / ".credentials.json").write_text('{"token": "secret"}', encoding="utf-8")
        (src / "shell-snapshots").mkdir(parents=True)
        (src / "shell-snapshots" / "snap.sh").write_text("junk", encoding="utf-8")
        monkeypatch.setattr(Path, "home", lambda: home)

        dest = tmp_path / "dot-claude"  # fixture's _DEFAULT_CONFIG_DIR
        assert up.bootstrap_default_profile() is True

        assert (dest / "settings.json").exists()
        assert (dest / "agents" / "backend.md").read_text(encoding="utf-8") == "role"
        assert not (dest / ".credentials.json").exists()
        # recent sessions ARE cloned per project (bounded — see TestCloneRecentSessions)
        assert (dest / "projects" / "proj-a" / "s1.jsonl").read_text(encoding="utf-8") == "hi"
        assert not (dest / "shell-snapshots").exists()  # not allowlisted
        assert (dest / up._BOOTSTRAP_MARKER).exists()

    def test_stale_partial_from_killed_attempt_is_cleaned(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A `.partial` sibling left over from a killed prior clone is wiped
        before a fresh attempt, never merged/left half-written."""
        import agent_takkub.config as config_mod

        monkeypatch.setattr(config_mod, "DATA_HOME", tmp_path / "agent-takkub-home")
        monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path / "venv-lib")

        home = tmp_path / "real-home"
        src = home / ".claude"
        src.mkdir(parents=True)
        (src / "settings.json").write_text('{"fresh": true}', encoding="utf-8")
        monkeypatch.setattr(Path, "home", lambda: home)

        dest = tmp_path / "dot-claude"
        partial = tmp_path / "dot-claude.partial"
        partial.mkdir(parents=True)
        (partial / "half-written.txt").write_text("torn", encoding="utf-8")

        assert up.bootstrap_default_profile() is True
        assert not partial.exists()
        assert (dest / "settings.json").read_text(encoding="utf-8") == '{"fresh": true}'
        assert not (dest / "half-written.txt").exists()


# ─────────────────── recent-session cloning (per project) ─────────────────
# Part of bootstrap_default_profile's atomic .partial clone: each project
# subdir under ~/.claude/projects/ contributes its own most-recent N
# transcripts, so chatlog_scanner/resume aren't starting from nothing.


class TestCloneRecentSessions:
    def _touch_with_mtime(self, path: Path, content: str, mtime: float) -> None:
        path.write_text(content, encoding="utf-8")
        os.utime(path, (mtime, mtime))

    def test_takes_n_most_recent_per_project(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import agent_takkub.config as config_mod

        monkeypatch.setattr(config_mod, "DATA_HOME", tmp_path / "agent-takkub-home")
        monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path / "venv-lib")

        home = tmp_path / "real-home"
        proj_a = home / ".claude" / "projects" / "proj-a"
        proj_b = home / ".claude" / "projects" / "proj-b"
        proj_a.mkdir(parents=True)
        proj_b.mkdir(parents=True)
        for i in range(15):  # more than RECENT_SESSIONS_CLONE (10)
            self._touch_with_mtime(proj_a / f"s{i:02d}.jsonl", "a", mtime=1_700_000_000 + i)
        for i in range(12):
            self._touch_with_mtime(proj_b / f"s{i:02d}.jsonl", "b", mtime=1_700_000_000 + i)
        monkeypatch.setattr(Path, "home", lambda: home)

        assert up.bootstrap_default_profile() is True

        dest_a = tmp_path / "dot-claude" / "projects" / "proj-a"
        dest_b = tmp_path / "dot-claude" / "projects" / "proj-b"
        assert {p.name for p in dest_a.glob("*.jsonl")} == {
            f"s{i:02d}.jsonl" for i in range(5, 15)
        }  # the 10 most-recently-modified of proj-a's 15
        assert {p.name for p in dest_b.glob("*.jsonl")} == {
            f"s{i:02d}.jsonl" for i in range(2, 12)
        }  # the 10 most-recently-modified of proj-b's 12

    def test_oversized_session_file_is_skipped_and_reported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import agent_takkub.config as config_mod

        monkeypatch.setattr(config_mod, "DATA_HOME", tmp_path / "agent-takkub-home")
        monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path / "venv-lib")

        home = tmp_path / "real-home"
        proj = home / ".claude" / "projects" / "proj-a"
        proj.mkdir(parents=True)
        self._touch_with_mtime(proj / "small.jsonl", "ok", mtime=1_700_000_100)
        big = proj / "big.jsonl"
        with open(big, "wb") as f:
            f.seek(up._RECENT_SESSION_MAX_BYTES + 1)
            f.write(b"\0")
        os.utime(big, (1_700_000_200, 1_700_000_200))
        monkeypatch.setattr(Path, "home", lambda: home)

        events: list[tuple[str, dict]] = []
        assert (
            up.bootstrap_default_profile(log_event=lambda name, **kw: events.append((name, kw)))
            is True
        )

        dest = tmp_path / "dot-claude" / "projects" / "proj-a"
        assert (dest / "small.jsonl").exists()
        assert not (dest / "big.jsonl").exists()
        assert events == [("profile_recent_sessions_oversized_skipped", {"count": 1})]

    def test_non_jsonl_files_and_non_dirs_are_ignored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import agent_takkub.config as config_mod

        monkeypatch.setattr(config_mod, "DATA_HOME", tmp_path / "agent-takkub-home")
        monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path / "venv-lib")

        home = tmp_path / "real-home"
        projects = home / ".claude" / "projects"
        proj = projects / "proj-a"
        proj.mkdir(parents=True)
        self._touch_with_mtime(proj / "s1.jsonl", "hi", mtime=1_700_000_000)
        (proj / "notes.txt").write_text("not a session", encoding="utf-8")
        (projects / "stray-file.jsonl").write_text("not a project dir", encoding="utf-8")
        monkeypatch.setattr(Path, "home", lambda: home)

        assert up.bootstrap_default_profile() is True

        dest = tmp_path / "dot-claude" / "projects" / "proj-a"
        assert (dest / "s1.jsonl").exists()
        assert not (dest / "notes.txt").exists()
        assert not (tmp_path / "dot-claude" / "projects" / "stray-file.jsonl").exists()
