"""Tests for image input — Level 1 (drag-drop) + Level 2 (Ctrl+V clipboard image).

All tests exercise the module-level helper functions extracted from
terminal_widget.py.  No QApplication or QWebEngineView is needed; the helpers
contain all the testable business logic.
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path

import pytest

from agent_takkub.terminal_widget import (
    _cleanup_clipboard_images,
    _format_drop_paths,
    _normalize_path,
    _save_clipboard_image,
)

# ---------------------------------------------------------------------------
# Path normalisation
# ---------------------------------------------------------------------------


class TestNormalizePath:
    def test_backslash_to_forward(self):
        assert _normalize_path("C:\\Users\\alice\\file.png") == "C:/Users/alice/file.png"

    def test_forward_slash_unchanged(self):
        assert _normalize_path("C:/Users/alice/file.png") == "C:/Users/alice/file.png"

    def test_mixed_slashes(self):
        result = _normalize_path("C:\\Users/alice\\file.png")
        assert "\\" not in result
        assert result == "C:/Users/alice/file.png"

    def test_empty_string(self):
        assert _normalize_path("") == ""

    def test_no_slashes(self):
        assert _normalize_path("filename.png") == "filename.png"

    def test_unc_path(self):
        result = _normalize_path("\\\\server\\share\\file.png")
        assert result == "//server/share/file.png"


# ---------------------------------------------------------------------------
# Drop path formatting
# ---------------------------------------------------------------------------


class TestFormatDropPaths:
    def test_single_path(self):
        assert _format_drop_paths(["C:\\foo\\bar.png"]) == "C:/foo/bar.png"

    def test_multiple_paths_space_separated(self):
        result = _format_drop_paths(["C:\\a\\b.png", "C:\\c\\d.txt"])
        assert result == "C:/a/b.png C:/c/d.txt"

    def test_empty_strings_filtered(self):
        result = _format_drop_paths(["C:\\a.png", "", "C:\\b.png"])
        assert result == "C:/a.png C:/b.png"

    def test_empty_list(self):
        assert _format_drop_paths([]) == ""

    def test_forward_slash_paths_unchanged(self):
        assert _format_drop_paths(["/home/user/image.png"]) == "/home/user/image.png"

    def test_image_and_non_image_mixed(self):
        """All file types appear; filtering by extension is not our job."""
        result = _format_drop_paths(["C:\\a.png", "C:\\b.txt", "C:\\c.jpg"])
        parts = result.split(" ")
        assert len(parts) == 3
        assert "C:/a.png" in parts
        assert "C:/b.txt" in parts
        assert "C:/c.jpg" in parts

    def test_three_files_correct_separator_count(self):
        result = _format_drop_paths(["a.png", "b.png", "c.png"])
        assert result.count(" ") == 2


# ---------------------------------------------------------------------------
# Drag-event URL-filtering logic (pure simulation — no QUrl needed)
# ---------------------------------------------------------------------------


class TestDropUrlFiltering:
    """Mirrors the eventFilter logic: only local-file URLs are included.

    We use plain dicts to simulate QUrl's isLocalFile/toLocalFile interface
    without touching Qt at all.
    """

    def _extract_local_paths(self, urls: list[dict]) -> list[str]:
        return [u["path"] for u in urls if u["is_local"] and u["path"]]

    def test_local_file_url_included(self):
        urls = [{"is_local": True, "path": "C:/foo/bar.png"}]
        result = _format_drop_paths(self._extract_local_paths(urls))
        assert result == "C:/foo/bar.png"

    def test_http_url_excluded(self):
        urls = [{"is_local": False, "path": ""}]
        result = _format_drop_paths(self._extract_local_paths(urls))
        assert result == ""

    def test_mixed_local_and_http(self):
        urls = [
            {"is_local": True, "path": "C:/a.png"},
            {"is_local": False, "path": ""},  # http/https URL
            {"is_local": True, "path": "C:/b.jpg"},
        ]
        result = _format_drop_paths(self._extract_local_paths(urls))
        assert "C:/a.png" in result
        assert "C:/b.jpg" in result
        assert result.count(" ") == 1

    def test_all_http_urls_yields_empty(self):
        urls = [{"is_local": False, "path": ""}, {"is_local": False, "path": ""}]
        result = _format_drop_paths(self._extract_local_paths(urls))
        assert result == ""

    def test_single_local_file(self):
        urls = [{"is_local": True, "path": "C:/screenshots/snap.png"}]
        result = _format_drop_paths(self._extract_local_paths(urls))
        assert result == "C:/screenshots/snap.png"


# ---------------------------------------------------------------------------
# Clipboard image save
# ---------------------------------------------------------------------------

# Minimal valid PNG bytes (1×1 white pixel, ~67 bytes)
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x11\x00\x01\xf7\xe0\xa4\xd2\x00\x00\x00\x00IEND\xaeB`\x82"
)


class TestSaveClipboardImage:
    def test_saves_correct_bytes(self, tmp_path):
        b64 = base64.b64encode(_TINY_PNG).decode()
        path = _save_clipboard_image(b64, tmp_path)
        assert path.exists()
        assert path.read_bytes() == _TINY_PNG

    def test_filename_starts_with_clipboard(self, tmp_path):
        b64 = base64.b64encode(b"data").decode()
        path = _save_clipboard_image(b64, tmp_path)
        assert path.name.startswith("clipboard-")

    def test_filename_ends_with_png(self, tmp_path):
        b64 = base64.b64encode(b"data").decode()
        path = _save_clipboard_image(b64, tmp_path)
        assert path.suffix == ".png"

    def test_timestamp_portion_length(self, tmp_path):
        b64 = base64.b64encode(b"data").decode()
        path = _save_clipboard_image(b64, tmp_path)
        # stem = "clipboard-YYYY-MM-DDTHH-MM-SS"
        ts_part = path.stem[len("clipboard-") :]
        assert len(ts_part) == 19, f"unexpected timestamp: {ts_part!r}"

    def test_creates_parent_dir_if_missing(self, tmp_path):
        nested = tmp_path / "runtime" / "deep"
        b64 = base64.b64encode(b"data").decode()
        path = _save_clipboard_image(b64, nested)
        assert path.exists()

    def test_invalid_base64_raises(self, tmp_path):
        import binascii

        with pytest.raises(binascii.Error):
            _save_clipboard_image("!!!not-base64!!!", tmp_path)

    def test_path_uses_forward_slashes_after_normalize(self, tmp_path):
        b64 = base64.b64encode(b"data").decode()
        path = _save_clipboard_image(b64, tmp_path)
        fwd = _normalize_path(str(path))
        assert "\\" not in fwd


# ---------------------------------------------------------------------------
# Clipboard image cleanup
# ---------------------------------------------------------------------------


def _make_clipboard_files(directory: Path, count: int) -> list[Path]:
    """Create `count` clipboard-*.png stubs with staggered mtimes (oldest first)."""
    files = []
    base_time = time.time() - count * 2
    for i in range(count):
        p = directory / f"clipboard-2026-05-20T12-{i // 60:02d}-{i % 60:02d}.png"
        p.write_bytes(b"x")
        t = base_time + i * 2  # earlier i → older mtime
        os.utime(str(p), (t, t))
        files.append(p)
    return files  # files[0] is oldest


class TestCleanupClipboardImages:
    def test_fewer_than_keep_leaves_all(self, tmp_path):
        _make_clipboard_files(tmp_path, 5)
        deleted = _cleanup_clipboard_images(tmp_path, keep=50)
        assert deleted == []
        assert len(list(tmp_path.glob("clipboard-*.png"))) == 5

    def test_exactly_keep_leaves_all(self, tmp_path):
        _make_clipboard_files(tmp_path, 50)
        deleted = _cleanup_clipboard_images(tmp_path, keep=50)
        assert deleted == []
        assert len(list(tmp_path.glob("clipboard-*.png"))) == 50

    def test_one_over_keep_deletes_oldest(self, tmp_path):
        files = _make_clipboard_files(tmp_path, 51)
        deleted = _cleanup_clipboard_images(tmp_path, keep=50)
        assert len(deleted) == 1
        assert deleted[0] == files[0]
        assert not files[0].exists()
        assert len(list(tmp_path.glob("clipboard-*.png"))) == 50

    def test_many_over_keep_deletes_correct_count(self, tmp_path):
        _make_clipboard_files(tmp_path, 60)
        deleted = _cleanup_clipboard_images(tmp_path, keep=50)
        assert len(deleted) == 10
        assert len(list(tmp_path.glob("clipboard-*.png"))) == 50

    def test_no_files_is_noop(self, tmp_path):
        deleted = _cleanup_clipboard_images(tmp_path, keep=50)
        assert deleted == []

    def test_ignores_non_clipboard_png_files(self, tmp_path):
        (tmp_path / "other-image.png").write_bytes(b"x")
        _make_clipboard_files(tmp_path, 3)
        deleted = _cleanup_clipboard_images(tmp_path, keep=50)
        assert deleted == []
        assert (tmp_path / "other-image.png").exists()

    def test_idempotent_second_call_deletes_nothing(self, tmp_path):
        _make_clipboard_files(tmp_path, 55)
        _cleanup_clipboard_images(tmp_path, keep=50)
        deleted2 = _cleanup_clipboard_images(tmp_path, keep=50)
        assert deleted2 == []

    def test_keeps_newest_not_oldest(self, tmp_path):
        files = _make_clipboard_files(tmp_path, 52)
        # files[0] and files[1] are the two oldest
        deleted = _cleanup_clipboard_images(tmp_path, keep=50)
        assert len(deleted) == 2
        assert files[0] in deleted
        assert files[1] in deleted
        # newest 50 survive
        for f in files[2:]:
            assert f.exists()

    def test_empty_runtime_dir_is_noop(self, tmp_path):
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        deleted = _cleanup_clipboard_images(runtime, keep=50)
        assert deleted == []

    def test_custom_keep_value(self, tmp_path):
        _make_clipboard_files(tmp_path, 10)
        deleted = _cleanup_clipboard_images(tmp_path, keep=3)
        assert len(deleted) == 7
        assert len(list(tmp_path.glob("clipboard-*.png"))) == 3
