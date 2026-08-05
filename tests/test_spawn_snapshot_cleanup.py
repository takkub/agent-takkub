"""Cleanup of stale `CLAUDE.spawn-<hash>.md` pane-scoped snapshot files
(token-reduction task, 2026-08): these accumulate under
``runtime/agents/<role>/`` — one per (project, role_name) pane — and must be
swept once a pane's project/role combo is no longer live, without ever
touching a snapshot a currently-open pane is using.
"""

from __future__ import annotations

import hashlib
import pathlib
import time

from agent_takkub.spawn_engine import (
    _SPAWN_SNAPSHOT_RE,
    _active_spawn_scope_hashes,
    _prune_stale_spawn_snapshots,
)


def _hash(project: str, role: str) -> str:
    return hashlib.sha256(f"{project}\0{role}".encode()).hexdigest()[:16]


def _touch(path: pathlib.Path, *, mtime: float | None = None) -> None:
    path.write_text("x", encoding="utf-8")
    if mtime is not None:
        import os

        os.utime(path, (mtime, mtime))


class TestActiveSpawnScopeHashes:
    def test_empty_registry_yields_empty_set(self) -> None:
        assert _active_spawn_scope_hashes({}) == set()

    def test_computes_hash_per_project_role_pair(self) -> None:
        panes_by_project = {"proj_a": {"backend": object(), "qa#1": object()}}
        got = _active_spawn_scope_hashes(panes_by_project)
        assert got == {_hash("proj_a", "backend"), _hash("proj_a", "qa#1")}

    def test_covers_multiple_projects(self) -> None:
        panes_by_project = {
            "proj_a": {"backend": object()},
            "proj_b": {"backend": object()},
        }
        got = _active_spawn_scope_hashes(panes_by_project)
        assert got == {_hash("proj_a", "backend"), _hash("proj_b", "backend")}
        # same base role, different project → different hash
        assert len(got) == 2


class TestPruneStaleSpawnSnapshots:
    def test_never_deletes_active_pane_snapshot(self, tmp_path: pathlib.Path) -> None:
        h = _hash("proj_a", "backend")
        f = tmp_path / f"CLAUDE.spawn-{h}.md"
        _touch(f, mtime=time.time() - 999_999)  # ancient, but active
        _prune_stale_spawn_snapshots(tmp_path, {h}, keep_stale=0)
        assert f.exists()

    def test_deletes_stale_snapshot_beyond_keep_count(self, tmp_path: pathlib.Path) -> None:
        now = time.time()
        files = []
        for i in range(5):
            h = _hash("proj_a", f"qa#{i}")
            f = tmp_path / f"CLAUDE.spawn-{h}.md"
            _touch(f, mtime=now - i)  # #0 newest, #4 oldest
            files.append(f)
        _prune_stale_spawn_snapshots(tmp_path, set(), keep_stale=2)
        remaining = {p.name for p in tmp_path.glob("CLAUDE.spawn-*.md")}
        assert remaining == {files[0].name, files[1].name}  # 2 newest survive
        for f in files[2:]:
            assert not f.exists()

    def test_keep_stale_zero_deletes_all_inactive(self, tmp_path: pathlib.Path) -> None:
        h = _hash("proj_a", "backend")
        f = tmp_path / f"CLAUDE.spawn-{h}.md"
        _touch(f)
        _prune_stale_spawn_snapshots(tmp_path, set(), keep_stale=0)
        assert not f.exists()

    def test_mixed_active_and_stale(self, tmp_path: pathlib.Path) -> None:
        active_hash = _hash("proj_a", "backend")
        active_f = tmp_path / f"CLAUDE.spawn-{active_hash}.md"
        _touch(active_f)
        stale_files = []
        for i in range(3):
            h = _hash("proj_b", f"qa#{i}")
            f = tmp_path / f"CLAUDE.spawn-{h}.md"
            _touch(f, mtime=time.time() - i)
            stale_files.append(f)
        _prune_stale_spawn_snapshots(tmp_path, {active_hash}, keep_stale=1)
        assert active_f.exists()
        assert stale_files[0].exists()  # newest stale kept (keep_stale=1)
        assert not stale_files[1].exists()
        assert not stale_files[2].exists()

    def test_ignores_non_matching_filenames(self, tmp_path: pathlib.Path) -> None:
        other = tmp_path / "CLAUDE.md"
        _touch(other)
        weird = tmp_path / "CLAUDE.spawn-not-a-hash.md"
        _touch(weird)
        _prune_stale_spawn_snapshots(tmp_path, set(), keep_stale=0)
        assert other.exists()
        assert weird.exists()  # doesn't match _SPAWN_SNAPSHOT_RE → left alone

    def test_best_effort_swallows_unlink_error(self, tmp_path: pathlib.Path, monkeypatch) -> None:
        h = _hash("proj_a", "backend")
        f = tmp_path / f"CLAUDE.spawn-{h}.md"
        _touch(f)

        def boom(self):
            raise OSError("locked")

        monkeypatch.setattr(pathlib.Path, "unlink", boom)
        # Must not raise even though the delete fails.
        _prune_stale_spawn_snapshots(tmp_path, set(), keep_stale=0)

    def test_nonexistent_dir_is_a_noop(self, tmp_path: pathlib.Path) -> None:
        _prune_stale_spawn_snapshots(tmp_path / "does-not-exist", set())

    def test_snapshot_regex_matches_expected_shape(self) -> None:
        h = _hash("proj_a", "backend")
        assert _SPAWN_SNAPSHOT_RE.match(f"CLAUDE.spawn-{h}.md")
        assert not _SPAWN_SNAPSHOT_RE.match("CLAUDE.md")
        assert not _SPAWN_SNAPSHOT_RE.match(f"CLAUDE.spawn-{h}.tmp")
