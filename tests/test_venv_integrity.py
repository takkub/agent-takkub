"""#341/#696: detection + repair of a clobbered cockpit interpreter file."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from agent_takkub import venv_integrity as vi

_WIN = sys.platform == "win32"


def _healthy_bytes() -> bytes:
    head = b"MZ" if _WIN else b"\x7fELF"
    return head.ljust(4 * 1024, b"\x00")


def _fake_venv(tmp_path: Path) -> tuple[Path, Path, Path]:
    """``(venv_root, venv_python, base_home)`` laid out like a real venv on
    this platform, with a healthy base interpreter to restore from."""
    home = tmp_path / "base"
    root = tmp_path / "venv"
    if _WIN:
        scripts = root / "Scripts"
        scripts.mkdir(parents=True)
        (home / "Lib" / "venv" / "scripts" / "nt").mkdir(parents=True)
        (home / "Lib" / "venv" / "scripts" / "nt" / "python.exe").write_bytes(_healthy_bytes())
        (home / "python.exe").write_bytes(_healthy_bytes())
        py = scripts / "python.exe"
        py.write_bytes(_healthy_bytes())
        (scripts / "pythonw.exe").write_bytes(_healthy_bytes())
    else:
        scripts = root / "bin"
        scripts.mkdir(parents=True)
        home.mkdir()
        ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
        (home / ver).write_bytes(_healthy_bytes())
        os.chmod(home / ver, 0o755)
        (home / "python3").symlink_to(home / ver)
        py = scripts / "python"
        py.symlink_to(home / ver)
        (scripts / "python3").symlink_to(home / ver)
    (root / "pyvenv.cfg").write_text(f"home = {home}\nversion = 3.11.8\n", encoding="utf-8")
    return root, py, home


class TestProblemDetection:
    def test_healthy_file_has_no_problem(self, tmp_path: Path) -> None:
        p = tmp_path / "python"
        p.write_bytes(_healthy_bytes())
        assert vi.python_executable_problem(p) is None

    def test_29_byte_text_is_too_small(self, tmp_path: Path) -> None:
        p = tmp_path / "python.exe"
        p.write_text("graphify-out\\.graphify_python", encoding="ascii")
        assert p.stat().st_size == 29
        assert vi.python_executable_problem(p) == "too-small"

    def test_wrong_header_is_bad_magic(self, tmp_path: Path) -> None:
        p = tmp_path / "python"
        p.write_bytes(b"NOPE".ljust(8 * 1024, b"\x00"))
        assert vi.python_executable_problem(p) == "bad-magic"

    def test_missing(self, tmp_path: Path) -> None:
        assert vi.python_executable_problem(tmp_path / "nope") == "missing"

    @pytest.mark.skipif(_WIN, reason="shebang wrappers are a POSIX venv shape")
    def test_shebang_wrapper_is_fine_on_posix(self, tmp_path: Path) -> None:
        p = tmp_path / "python"
        p.write_bytes(b"#!/bin/sh\n".ljust(2 * 1024, b"\n"))
        assert vi.python_executable_problem(p) is None

    def test_find_problems_lists_every_broken_interpreter_file(self, tmp_path: Path) -> None:
        _root, py, _home = _fake_venv(tmp_path)
        assert vi.find_problems(py) == []
        py.unlink()
        py.write_text("graphify-out\\.graphify_python", encoding="ascii")
        problems = vi.find_problems(py)
        assert [(p.name, why) for p, why in problems] == [(py.name, "too-small")]


class TestRepair:
    def test_repair_restores_from_base_and_keeps_evidence(self, tmp_path: Path) -> None:
        _root, py, _home = _fake_venv(tmp_path)
        py.unlink()
        py.write_text("graphify-out\\.graphify_python", encoding="ascii")

        ok, msg = vi.repair(py)

        assert ok, msg
        assert vi.python_executable_problem(py) is None
        backups = sorted(py.parent.glob(f"{py.name}.clobbered-*.bak"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == b"graphify-out\\.graphify_python"
        assert "restored" in msg

    def test_repair_is_a_noop_on_a_healthy_file(self, tmp_path: Path) -> None:
        _root, py, _home = _fake_venv(tmp_path)
        before = py.read_bytes() if not py.is_symlink() else os.readlink(py)
        ok, msg = vi.repair(py)
        assert ok and "already healthy" in msg
        after = py.read_bytes() if not py.is_symlink() else os.readlink(py)
        assert before == after
        assert not list(py.parent.glob("*.clobbered-*"))

    def test_repair_fails_loudly_without_a_healthy_source(self, tmp_path: Path) -> None:
        _root, py, home = _fake_venv(tmp_path)
        # Base interpreter gone too — nothing trustworthy to copy from.
        for f in home.rglob("*"):
            if f.is_file() or f.is_symlink():
                f.unlink()
        py.unlink()
        py.write_text("x" * 29, encoding="ascii")

        ok, msg = vi.repair(py)

        assert not ok
        assert "no healthy source" in msg
        assert "npm install -g agent-takkub --force" in msg

    def test_repair_hint_names_the_base_launcher(self, tmp_path: Path) -> None:
        _root, py, home = _fake_venv(tmp_path)
        hint = vi.repair_hint(py)
        assert str(home) in hint


class TestExecutableDirs:
    def test_venv_root_and_base_prefix_are_protected(self, tmp_path: Path) -> None:
        root, py, _home = _fake_venv(tmp_path)
        dirs = vi.cockpit_executable_dirs(executable=py, extra=[tmp_path / "shims"])
        assert root.resolve() in dirs
        assert Path(sys.base_prefix).resolve() in dirs
        assert (tmp_path / "shims").resolve() in dirs

    def test_is_inside_any_matches_descendants_only(self, tmp_path: Path) -> None:
        root, py, _home = _fake_venv(tmp_path)
        dirs = frozenset({root.resolve()})
        # Resolve the directory, not the file — on POSIX `bin/python` is a
        # symlink out of the venv (same contract as `interpreter_dir`).
        assert vi.is_inside_any(py.parent.resolve() / py.name, dirs) == root.resolve()
        assert vi.is_inside_any((root / "Lib" / "site-packages" / "x.pth").resolve(), dirs)
        assert vi.is_inside_any(tmp_path.resolve(), dirs) is None
        # A sibling that merely shares the prefix string is NOT inside.
        sibling = tmp_path / "venv2"
        sibling.mkdir()
        assert vi.is_inside_any(sibling.resolve(), dirs) is None
