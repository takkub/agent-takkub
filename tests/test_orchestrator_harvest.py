"""Tests for scan_artifacts() — artifact scan filter, exclude dirs, mtime threshold.

What these tests pin down:
  - returns only files modified >= since_ts
  - excludes paths that contain any _HARVEST_EXCLUDE_DIRS component
  - skips symlinks and directories
  - caps result at `limit`
  - sorted by mtime descending
  - skips non-existent scan bases silently
  - mtime_rel string follows age bucket rules
  - circular dir symlinks do not cause infinite recursion (#16)
"""

from __future__ import annotations

import pathlib
import time

import pytest

from agent_takkub.orchestrator import _HARVEST_EXCLUDE_DIRS, scan_artifacts


def _write(path: pathlib.Path, content: str = "x") -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


class TestScanArtifactsFilter:
    def test_returns_files_modified_after_since(self, tmp_path: pathlib.Path) -> None:
        now = time.time()
        old = _write(tmp_path / "old.txt")
        new = _write(tmp_path / "new.txt")
        # backdate old file
        old_ts = now - 3600
        import os

        os.utime(old, (old_ts, old_ts))

        since = now - 60
        result = scan_artifacts([tmp_path], since)
        paths = [pathlib.Path(r["path"]) for r in result]
        assert new in paths
        assert old not in paths

    def test_excludes_excluded_dirs(self, tmp_path: pathlib.Path) -> None:
        good = _write(tmp_path / "src" / "module.py")
        for excluded in _HARVEST_EXCLUDE_DIRS:
            _write(tmp_path / excluded / "hidden.py")

        since = time.time() - 60
        result = scan_artifacts([tmp_path], since)
        paths = [pathlib.Path(r["path"]) for r in result]
        assert good in paths
        for excluded in _HARVEST_EXCLUDE_DIRS:
            hidden = tmp_path / excluded / "hidden.py"
            assert hidden not in paths, f"should exclude {excluded}/"

    def test_skips_directories_themselves(self, tmp_path: pathlib.Path) -> None:
        sub = tmp_path / "subdir"
        sub.mkdir()
        _write(sub / "file.py")
        since = time.time() - 60
        result = scan_artifacts([tmp_path], since)
        # only the file should appear, not the dir entry
        for r in result:
            assert not pathlib.Path(r["path"]).is_dir()

    def test_skips_symlinks(self, tmp_path: pathlib.Path) -> None:
        target = _write(tmp_path / "real.py")
        link = tmp_path / "link.py"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported")
        since = time.time() - 60
        result = scan_artifacts([tmp_path], since)
        paths = [r["path"] for r in result]
        assert str(link) not in paths

    def test_skips_nonexistent_paths(self, tmp_path: pathlib.Path) -> None:
        ghost = tmp_path / "does_not_exist"
        real = _write(tmp_path / "real.py")
        since = time.time() - 60
        result = scan_artifacts([ghost, tmp_path], since)
        paths = [pathlib.Path(r["path"]) for r in result]
        assert real in paths  # real base still scanned

    def test_caps_at_limit(self, tmp_path: pathlib.Path) -> None:
        for i in range(20):
            _write(tmp_path / f"file{i:02d}.py")
        since = time.time() - 60
        result = scan_artifacts([tmp_path], since, limit=5)
        assert len(result) <= 5

    def test_sorted_mtime_desc(self, tmp_path: pathlib.Path) -> None:
        import os

        base_ts = time.time() - 300
        files = []
        for i in range(5):
            p = _write(tmp_path / f"f{i}.py")
            ts = base_ts + i * 10
            os.utime(p, (ts, ts))
            files.append((ts, p))

        since = base_ts - 1
        result = scan_artifacts([tmp_path], since)
        mtimes = [r["mtime_ts"] for r in result]
        assert mtimes == sorted(mtimes, reverse=True)


class TestScanArtifactsMtimeRel:
    def test_mtime_rel_seconds(self, tmp_path: pathlib.Path) -> None:
        _write(tmp_path / "fresh.py")
        since = time.time() - 60
        result = scan_artifacts([tmp_path], since)
        assert result
        # recent file → "Ns ago"
        assert result[0]["mtime_rel"].endswith("s ago")

    def test_mtime_rel_minutes(self, tmp_path: pathlib.Path) -> None:
        import os

        p = _write(tmp_path / "old.py")
        ts = time.time() - 600  # 10 min ago
        os.utime(p, (ts, ts))
        since = ts - 1
        result = scan_artifacts([tmp_path], since)
        assert result
        assert result[0]["mtime_rel"].endswith("m ago")

    def test_mtime_rel_hours(self, tmp_path: pathlib.Path) -> None:
        import os

        p = _write(tmp_path / "older.py")
        ts = time.time() - 7200  # 2 h ago
        os.utime(p, (ts, ts))
        since = ts - 1
        result = scan_artifacts([tmp_path], since)
        assert result
        assert result[0]["mtime_rel"].endswith("h ago")


class TestScanArtifactsPayloadShape:
    def test_result_has_required_keys(self, tmp_path: pathlib.Path) -> None:
        _write(tmp_path / "x.py")
        since = time.time() - 60
        result = scan_artifacts([tmp_path], since)
        assert result
        for item in result:
            assert "path" in item
            assert "mtime_ts" in item
            assert "mtime_rel" in item


class TestScanArtifactsCircularSymlink:
    """#16 — circular dir symlinks must not cause infinite recursion."""

    @pytest.mark.skipif(
        not hasattr(pathlib.Path, "symlink_to"),
        reason="symlinks not available",
    )
    def test_circular_dir_symlink_does_not_hang(self, tmp_path: pathlib.Path) -> None:
        """scan_artifacts must return without hanging when a dir symlink points back."""
        real_file = _write(tmp_path / "src" / "module.py")
        loop_target = tmp_path / "loop"
        try:
            loop_target.symlink_to(tmp_path, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("cannot create directory symlink on this platform")

        since = time.time() - 60
        result = scan_artifacts([tmp_path], since)
        paths = [pathlib.Path(r["path"]) for r in result]
        # real file found; the circular symlink dir was not followed
        assert real_file in paths
        assert loop_target not in paths

    def test_normal_files_still_scanned_after_os_walk_switch(self, tmp_path: pathlib.Path) -> None:
        """os.walk-based scan still returns regular files correctly (regression guard)."""
        f1 = _write(tmp_path / "a" / "one.py")
        f2 = _write(tmp_path / "b" / "two.py")
        since = time.time() - 60
        result = scan_artifacts([tmp_path], since)
        paths = [pathlib.Path(r["path"]) for r in result]
        assert f1 in paths
        assert f2 in paths
