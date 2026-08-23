"""editor_service.py (#365 phase 3 — safe edit: conflict detect + atomic
save). Pure functions — tested directly, same convention as
project_file_index.py's list_dir_sync / editor_widget.py's
read_file_for_editor (production always calls these from a worker thread,
tests call them synchronously).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from agent_takkub.editor_service import (
    MAX_EDIT_FILE_BYTES,
    read_for_edit,
    save_atomic,
    stat_snapshot,
)
from agent_takkub.project_file_index import PathEscapesRootsError

# ── read_for_edit / stat_snapshot ───────────────────────────────────────


class TestReadForEdit:
    def test_text_file_state(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "main.py"
        # write_bytes, not write_text — write_text's universal-newlines mode
        # would translate "\n" to os.linesep (i.e. "\r\n" on Windows) before
        # it ever reaches disk, defeating the point of this assertion.
        f.write_bytes(b"print(1)\n")

        state = read_for_edit(f, [root])

        assert state.binary is False
        assert state.too_large is False
        assert state.language == "py"
        assert state.sha256 is not None
        assert state.newline == "\n"
        assert state.bom is False
        assert state.size == f.stat().st_size

    def test_path_outside_roots_raises(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        outside = tmp_path / "outside.py"
        outside.write_text("x", encoding="utf-8")

        with pytest.raises(PathEscapesRootsError):
            read_for_edit(outside, [root])

    def test_binary_file_flagged_no_hash(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "img.bin"
        f.write_bytes(b"\x89PNG\x00\x01\x02")

        state = read_for_edit(f, [root])

        assert state.binary is True
        assert state.sha256 is None

    def test_oversized_file_flagged_no_hash(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "big.txt"
        f.write_text("a" * 100, encoding="utf-8")

        state = read_for_edit(f, [root], max_bytes=50)

        assert state.too_large is True
        assert state.sha256 is None
        assert state.size == 100

    def test_bom_and_crlf_detected(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "win.txt"
        f.write_bytes(b"\xef\xbb\xbfhello\r\nworld\r\n")

        state = read_for_edit(f, [root])

        assert state.bom is True
        assert state.newline == "\r\n"

    def test_unicode_thai_path(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "ไฟล์ทดสอบ ก.txt"
        f.write_text("สวัสดี\n", encoding="utf-8")

        state = read_for_edit(f, [root])

        assert state.binary is False
        assert state.path.name == "ไฟล์ทดสอบ ก.txt"


class TestStatSnapshot:
    def test_matches_read_for_edit_for_same_file(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "a.py"
        f.write_text("x", encoding="utf-8")

        via_read = read_for_edit(f, [root])
        via_stat = stat_snapshot(f.resolve())

        assert via_read.sha256 == via_stat.sha256
        assert via_read.mtime_ns == via_stat.mtime_ns


# ── save_atomic: conflict detection ─────────────────────────────────────


class TestSaveAtomicConflicts:
    def test_matching_expected_saves_cleanly(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "a.py"
        f.write_text("old\n", encoding="utf-8")
        expected = read_for_edit(f, [root])

        result = save_atomic(f, "new\n", expected, [root])

        assert result.ok is True
        assert result.conflict is None
        assert f.read_text(encoding="utf-8") == "new\n"
        assert result.state.sha256 != expected.sha256

    def test_disk_modified_since_load_is_conflict(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "a.py"
        f.write_text("old\n", encoding="utf-8")
        expected = read_for_edit(f, [root])

        # Simulate an external editor / agent touching the file after load.
        time.sleep(0.01)
        f.write_text("changed on disk\n", encoding="utf-8")

        result = save_atomic(f, "my new content\n", expected, [root])

        assert result.ok is False
        assert result.conflict is not None
        assert result.conflict.disk.sha256 != expected.sha256
        assert result.conflict.ours == expected
        # never overwrote the disk version
        assert f.read_text(encoding="utf-8") == "changed on disk\n"

    def test_disk_touched_but_content_identical_is_still_conflict(self, tmp_path: Path) -> None:
        """mtime_ns is part of the tracked version — a touch with unchanged
        bytes still trips the conflict rule (04_MONACO_EDITOR_SPEC.md: track
        mtime_ns + size + sha256, never overwrite silently if it moved)."""
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "a.py"
        f.write_text("same\n", encoding="utf-8")
        expected = read_for_edit(f, [root])

        time.sleep(0.01)
        f.write_text("same\n", encoding="utf-8")  # same bytes, new mtime

        result = save_atomic(f, "edited\n", expected, [root])

        assert result.ok is False
        assert result.conflict is not None

    def test_file_deleted_since_load_is_conflict(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "a.py"
        f.write_text("old\n", encoding="utf-8")
        expected = read_for_edit(f, [root])
        f.unlink()

        result = save_atomic(f, "new\n", expected, [root])

        assert result.ok is False
        assert result.conflict is not None
        assert result.conflict.disk is None
        assert not f.exists()

    def test_new_file_with_no_expected_saves(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "brand_new.py"

        result = save_atomic(f, "hello\n", None, [root])

        assert result.ok is True
        assert f.read_text(encoding="utf-8") == "hello\n"

    def test_new_file_intent_but_something_already_exists_is_conflict(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "surprise.py"
        f.write_text("already here\n", encoding="utf-8")

        result = save_atomic(f, "hello\n", None, [root])

        assert result.ok is False
        assert result.conflict is not None
        assert f.read_text(encoding="utf-8") == "already here\n"

    def test_path_outside_roots_errors_not_conflict(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        outside = tmp_path / "outside.py"
        outside.write_text("x", encoding="utf-8")

        result = save_atomic(outside, "y", None, [root])

        assert result.ok is False
        assert result.conflict is None
        assert result.error is not None


class TestSaveAtomicSizeCap:
    def test_at_cap_saves(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "a.txt"
        f.write_text("old\n", encoding="utf-8")
        expected = read_for_edit(f, [root])
        text = "a" * 50

        result = save_atomic(f, text, expected, [root], max_bytes=50)

        assert result.ok is True
        assert result.error is None
        assert f.read_text(encoding="utf-8") == text

    def test_over_cap_rejected_and_file_untouched(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "a.txt"
        f.write_text("old\n", encoding="utf-8")
        expected = read_for_edit(f, [root])

        result = save_atomic(f, "a" * 51, expected, [root], max_bytes=50)

        assert result.ok is False
        assert result.conflict is None
        assert result.error is not None
        assert f.read_text(encoding="utf-8") == "old\n"

    def test_over_cap_new_file_not_created(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "brand_new.txt"

        result = save_atomic(f, "a" * 51, None, [root], max_bytes=50)

        assert result.ok is False
        assert result.conflict is None
        assert result.error is not None
        assert not f.exists()

    def test_cap_measured_in_encoded_bytes_not_chars(self, tmp_path: Path) -> None:
        """3-byte-per-char Thai text: char count stays under the cap while
        the encoded byte count — what's actually being rejected — exceeds
        it, so a char-length check would wrongly let this through."""
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "a.txt"
        f.write_text("old\n", encoding="utf-8")
        expected = read_for_edit(f, [root])
        text = "ก" * 20
        assert len(text) < 50 <= len(text.encode("utf-8"))

        result = save_atomic(f, text, expected, [root], max_bytes=50)

        assert result.ok is False
        assert f.read_text(encoding="utf-8") == "old\n"


# ── save_atomic: atomicity / containment / encoding ─────────────────────


class TestSaveAtomicWriteBehavior:
    def test_write_is_atomic_no_leftover_temp_file(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "a.py"
        f.write_text("old\n", encoding="utf-8")
        expected = read_for_edit(f, [root])

        save_atomic(f, "new\n", expected, [root])

        leftovers = [p for p in root.iterdir() if "takkub-tmp" in p.name]
        assert leftovers == []

    def test_preserves_crlf_newline_on_save(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "win.txt"
        f.write_bytes(b"line1\r\nline2\r\n")
        expected = read_for_edit(f, [root])

        save_atomic(f, "line1\nline2\nline3\n", expected, [root])

        assert f.read_bytes() == b"line1\r\nline2\r\nline3\r\n"

    def test_preserves_bom_on_save(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "win.txt"
        f.write_bytes(b"\xef\xbb\xbfhello\n")
        expected = read_for_edit(f, [root])

        save_atomic(f, "hello world\n", expected, [root])

        assert f.read_bytes().startswith(b"\xef\xbb\xbf")

    def test_new_file_has_no_bom(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "fresh.txt"

        save_atomic(f, "hello\n", None, [root])

        assert not f.read_bytes().startswith(b"\xef\xbb\xbf")

    def test_binary_write_containment_symlink_escape(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        link = root / "escape"
        try:
            link.symlink_to(outside_dir)
        except OSError:
            pytest.skip("symlink creation not permitted in this environment")

        target = link / "evil.py"
        result = save_atomic(target, "pwned\n", None, [root])

        assert result.ok is False
        assert result.error is not None
        assert not (outside_dir / "evil.py").exists()

    def test_unicode_thai_path_round_trip(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "ไฟล์ ก.py"
        f.write_text("เก่า\n", encoding="utf-8")
        expected = read_for_edit(f, [root])

        result = save_atomic(f, "ใหม่\n", expected, [root])

        assert result.ok is True
        assert f.read_text(encoding="utf-8") == "ใหม่\n"


def test_max_edit_file_bytes_is_a_sane_positive_bound() -> None:
    assert 0 < MAX_EDIT_FILE_BYTES <= 10_000_000
