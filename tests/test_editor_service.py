"""editor_service.py (#365 phase 3 — safe edit: conflict detect + atomic
save). Pure functions — tested directly, same convention as
project_file_index.py's list_dir_sync / editor_widget.py's
read_file_for_editor (production always calls these from a worker thread,
tests call them synchronously).
"""

from __future__ import annotations

import stat
import sys
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


class TestEncodingUnsupported:
    """BUG-005: strict UTF-8 decoding — a text-shaped file with invalid
    UTF-8 bytes must never be silently mangled via errors="replace"."""

    def test_invalid_utf8_is_flagged(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "latin1.txt"
        f.write_bytes(b"caf\xe9 con leche\n")  # "café" in Latin-1, not valid UTF-8

        state = read_for_edit(f, [root])

        assert state.encoding_unsupported is True
        assert state.binary is False
        assert state.too_large is False

    def test_invalid_utf8_still_hashed_and_newline_detected(self, tmp_path: Path) -> None:
        """sha256/newline must still come from the raw bytes even though
        decoding failed — a disk-change/conflict check still needs them."""
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "latin1.txt"
        raw = b"caf\xe9\r\ncon leche\r\n"
        f.write_bytes(raw)

        state = read_for_edit(f, [root])

        assert state.sha256 is not None
        assert state.newline == "\r\n"

    def test_valid_utf8_not_flagged(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "ok.txt"
        f.write_text("café con leche\n", encoding="utf-8")

        state = read_for_edit(f, [root])

        assert state.encoding_unsupported is False

    def test_utf8_bom_not_flagged(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "bom.txt"
        f.write_bytes(b"\xef\xbb\xbfhello\n")

        state = read_for_edit(f, [root])

        assert state.encoding_unsupported is False
        assert state.bom is True

    def test_binary_file_not_flagged_encoding_unsupported(self, tmp_path: Path) -> None:
        """binary is its own, older category (BUG-005 is about text-shaped
        files only) — must not double-flag."""
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "img.bin"
        f.write_bytes(b"\x89PNG\x00\x01\x02")

        state = read_for_edit(f, [root])

        assert state.encoding_unsupported is False

    def test_save_rejected_when_expected_is_encoding_unsupported(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "latin1.txt"
        raw = b"caf\xe9\n"
        f.write_bytes(raw)
        expected = read_for_edit(f, [root])
        assert expected.encoding_unsupported is True

        result = save_atomic(f, "new text\n", expected, [root])

        assert result.ok is False
        assert result.conflict is None
        assert result.error is not None
        assert f.read_bytes() == raw  # never overwritten


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


# ── save_atomic: POSIX file mode preservation (BUG-004) ─────────────────


class TestWriteAtomicModePreservation:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode has no meaning on Windows")
    def test_preserves_existing_mode_on_save(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "deploy.sh"
        f.write_text("#!/bin/sh\necho old\n", encoding="utf-8")
        f.chmod(0o755)
        expected = read_for_edit(f, [root])

        result = save_atomic(f, "#!/bin/sh\necho new\n", expected, [root])

        assert result.ok is True
        assert stat.S_IMODE(f.stat().st_mode) == 0o755

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode has no meaning on Windows")
    def test_preserves_narrow_mode_on_save(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "secret.env"
        f.write_text("OLD=1\n", encoding="utf-8")
        f.chmod(0o600)
        expected = read_for_edit(f, [root])

        save_atomic(f, "NEW=1\n", expected, [root])

        assert stat.S_IMODE(f.stat().st_mode) == 0o600

    def test_windows_never_calls_chmod(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Force the win32 branch regardless of the host OS running this
        test, and assert the chmod path is never exercised there.

        `_win_long_path` is stubbed to identity here too: on a real Windows
        host it's already covered on its own terms by
        test_worktree_manager.py/test_disk_usage.py, and on POSIX its real
        `\\?\` prefixing produces a string `open()` can't use — this test's
        target is only the mode-preservation gate (`sys.platform`), not the
        long-path helper, so it must not depend on which OS actually runs
        the suite.
        """
        import agent_takkub.editor_service as editor_service_module

        root = tmp_path / "proj"
        root.mkdir()
        f = root / "a.py"
        f.write_text("old\n", encoding="utf-8")
        expected = read_for_edit(f, [root])

        monkeypatch.setattr(editor_service_module.sys, "platform", "win32")
        monkeypatch.setattr(editor_service_module, "_win_long_path", lambda p: str(p))
        chmod_calls: list[tuple] = []
        monkeypatch.setattr(
            editor_service_module.os, "chmod", lambda *a, **kw: chmod_calls.append(a)
        )

        result = save_atomic(f, "new\n", expected, [root])

        assert result.ok is True
        assert chmod_calls == []

    def test_new_file_with_no_prior_mode_still_saves(self, tmp_path: Path) -> None:
        """No `expected` (brand-new file) means nothing to read a mode
        from — falls through to the process default umask, same as any
        normal file creation, on every platform."""
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "brand_new.py"

        result = save_atomic(f, "hello\n", None, [root])

        assert result.ok is True
        assert f.read_text(encoding="utf-8") == "hello\n"
