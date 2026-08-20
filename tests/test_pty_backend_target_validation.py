"""Tests for the #313 pre-flight spawn-target validation.

`_looks_like_valid_executable` / `_validate_spawn_target` exist to stop
`spawn_pty()` from ever handing a corrupted/mid-write binary to the native
pty constructor — the exact condition proven (see
`docs/audit/2026-08-20-issue-313-spawn-deadlock.md`) to hang the whole
interpreter for hours on Windows (a modal hard-error dialog for a malformed
PE). That reproduction is real and dangerous to automate: spawning a corrupt
exe pops an actual OS dialog and can hang a CI runner solid. Nothing in this
file calls into either pty backend or spawns a real process — it only
exercises the pure, in-process file-header check and confirms the (mocked)
backends are never reached once that check fails.
"""

from __future__ import annotations

import os
import struct
import sys

import pytest

import agent_takkub._pty_backend as backend
from agent_takkub._pty_backend import (
    SpawnTargetCorrupt,
    _looks_like_valid_executable,
    _validate_spawn_target,
    spawn_pty,
)


def _write_pe(path, *, valid: bool) -> None:
    head = bytearray(64)
    head[0:2] = b"MZ"
    if valid:
        struct.pack_into("<I", head, 60, 64)  # e_lfanew -> right after the header
        path.write_bytes(bytes(head) + b"PE\x00\x00" + b"\x00" * 32)
    else:
        # e_lfanew left as 0 (garbage) — no valid PE signature will be found there.
        path.write_bytes(bytes(head))


class TestLooksLikeValidExecutableWindows:
    def test_valid_pe_header_passes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backend.sys, "platform", "win32")
        target = tmp_path / "good.exe"
        _write_pe(target, valid=True)
        assert _looks_like_valid_executable(str(target)) is True

    def test_real_python_exe_passes(self, monkeypatch):
        # Belt-and-suspenders sanity check against an actual on-disk PE.
        if sys.platform != "win32":
            pytest.skip("windows-only sanity check")
        monkeypatch.setattr(backend.sys, "platform", "win32")
        assert _looks_like_valid_executable(sys.executable) is True

    def test_truncated_mz_header_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backend.sys, "platform", "win32")
        target = tmp_path / "truncated.exe"
        target.write_bytes(b"MZ\x00\x00")  # far short of 64 bytes
        assert _looks_like_valid_executable(str(target)) is False

    def test_mz_header_with_garbage_pe_signature_fails(self, tmp_path, monkeypatch):
        """Mirrors the exact shape reproduced against the issue: a real MZ
        header (npm wrote that much) but no valid PE body yet (write cut
        off mid-flight)."""
        monkeypatch.setattr(backend.sys, "platform", "win32")
        target = tmp_path / "mid_write.exe"
        _write_pe(target, valid=False)
        assert _looks_like_valid_executable(str(target)) is False

    def test_non_exe_extension_is_not_checked(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backend.sys, "platform", "win32")
        target = tmp_path / "claude.cmd"
        target.write_text("@echo off\r\nnode claude.js %*\r\n", encoding="utf-8")
        assert _looks_like_valid_executable(str(target)) is True

    def test_missing_file_is_not_our_failure_mode(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backend.sys, "platform", "win32")
        assert _looks_like_valid_executable(str(tmp_path / "nope.exe")) is True


class TestLooksLikeValidExecutablePosix:
    def test_elf_header_passes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backend.sys, "platform", "linux")
        target = tmp_path / "good.bin"
        target.write_bytes(b"\x7fELF" + b"\x00" * 32)
        assert _looks_like_valid_executable(str(target)) is True

    def test_shebang_script_passes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backend.sys, "platform", "linux")
        target = tmp_path / "script.sh"
        target.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        assert _looks_like_valid_executable(str(target)) is True

    def test_non_executable_file_with_unrecognised_header_passes(self, tmp_path, monkeypatch):
        # No exec bit -> not the thing the loader would try to run directly.
        monkeypatch.setattr(backend.sys, "platform", "linux")
        target = tmp_path / "data.bin"
        target.write_bytes(b"not-an-elf-header")
        assert _looks_like_valid_executable(str(target)) is True

    def test_executable_bit_with_unrecognised_header_fails(self, tmp_path, monkeypatch):
        # Windows can't actually flip the exec bit via chmod, so fake the
        # stat result directly to exercise this branch's logic in isolation.
        monkeypatch.setattr(backend.sys, "platform", "linux")
        target = tmp_path / "corrupt-binary"
        target.write_bytes(b"not-an-elf-header")
        real_stat = os.stat

        class _FakeStat:
            st_mode = 0o100755

        def _fake_stat(path, *a, **kw):
            if os.fspath(path) == str(target):
                return _FakeStat()
            return real_stat(path, *a, **kw)

        monkeypatch.setattr(backend.os, "stat", _fake_stat)
        assert _looks_like_valid_executable(str(target)) is False


class TestValidateSpawnTarget:
    def test_unresolvable_argv0_is_a_noop(self, monkeypatch, tmp_path):
        monkeypatch.setattr(backend, "which", lambda *a, **kw: None)
        _validate_spawn_target("does-not-exist-anywhere", None)  # must not raise

    def test_corrupt_resolved_target_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backend.sys, "platform", "win32")
        target = tmp_path / "claude.exe"
        _write_pe(target, valid=False)
        monkeypatch.setattr(backend, "which", lambda *a, **kw: str(target))

        with pytest.raises(SpawnTargetCorrupt):
            _validate_spawn_target("claude", None)

    def test_valid_resolved_target_is_a_noop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backend.sys, "platform", "win32")
        target = tmp_path / "claude.exe"
        _write_pe(target, valid=True)
        monkeypatch.setattr(backend, "which", lambda *a, **kw: str(target))

        _validate_spawn_target("claude", None)  # must not raise


class TestSpawnPtyRefusesCorruptTargetBeforeNativeCall:
    """The whole point: a corrupt target must never reach either backend's
    native constructor. Both backends are stubbed to fail the test outright
    if called — proving the guard runs strictly first."""

    def _boom(self, *a, **kw):
        raise AssertionError("native backend must not be called for a corrupt target")

    def test_windows_backend_never_invoked(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backend.sys, "platform", "win32")
        target = tmp_path / "claude.exe"
        _write_pe(target, valid=False)
        monkeypatch.setattr(backend, "which", lambda *a, **kw: str(target))
        monkeypatch.setattr(backend._WinptyBackend, "spawn", self._boom)

        with pytest.raises(SpawnTargetCorrupt):
            spawn_pty(["claude"], cwd=str(tmp_path))

    def test_posix_backend_never_invoked(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backend.sys, "platform", "linux")
        target = tmp_path / "claude"
        target.write_bytes(b"not-an-elf-header")
        real_stat = os.stat

        class _FakeStat:
            st_mode = 0o100755

        def _fake_stat(path, *a, **kw):
            if os.fspath(path) == str(target):
                return _FakeStat()
            return real_stat(path, *a, **kw)

        monkeypatch.setattr(backend.os, "stat", _fake_stat)
        monkeypatch.setattr(backend, "which", lambda *a, **kw: str(target))
        monkeypatch.setattr(backend._PosixBackend, "spawn", self._boom)

        with pytest.raises(SpawnTargetCorrupt):
            spawn_pty(["claude"], cwd=str(tmp_path))
