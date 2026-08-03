"""Tests for `config._gui_binary_dirs` / `ensure_gui_path` / `find_npm` —
the consolidated cross-platform CLI-binary discovery used by a GUI-launched
process (no inherited shell PATH, e.g. macOS Finder/Dock/Launchpad) to find
npm/node. All filesystem + PATH access is mocked; no test touches this
process's real PATH or filesystem outside `tmp_path`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_takkub import config


@pytest.fixture(autouse=True)
def _reset_memoization(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts as if `ensure_gui_path()` has never run this process."""
    monkeypatch.setattr(config, "_gui_path_ensured", False)


def _make_exec(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho fake npm\n", encoding="utf-8")
    path.chmod(0o755)
    return path


# ─────────────────────────────────────────────────────────────────────
# _node_version_key — version-aware sort (S4)
# ─────────────────────────────────────────────────────────────────────


class TestNodeVersionKey:
    def test_orders_numerically_not_lexicographically(self) -> None:
        paths = [
            "/home/u/.nvm/versions/node/v9.5.0/bin",
            "/home/u/.nvm/versions/node/v10.2.0/bin",
            "/home/u/.nvm/versions/node/v20.1.0/bin",
        ]
        ordered = sorted(paths, key=config._node_version_key, reverse=True)
        assert ordered[0].endswith("v20.1.0/bin")
        assert ordered[-1].endswith("v9.5.0/bin")

    def test_unparseable_path_does_not_raise(self) -> None:
        assert config._node_version_key("/no/version/here/bin") == (0, 0, 0)


# ─────────────────────────────────────────────────────────────────────
# _gui_binary_dirs — per-platform directory list
# ─────────────────────────────────────────────────────────────────────


class TestGuiBinaryDirs:
    def test_darwin_includes_arm_and_intel_homebrew(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config.sys, "platform", "darwin")
        dirs = config._gui_binary_dirs()
        assert "/opt/homebrew/bin" in dirs  # Apple Silicon
        assert "/usr/local/bin" in dirs  # Intel

    def test_win32_includes_appdata_npm_and_program_files(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # NOTE: monkeypatching sys.platform simulates win32, but os.path is
        # still the *host's* real module — production joins these with
        # os.path.join too, so build the expected strings the same way
        # instead of hardcoding "\" (which only matches when tests happen to
        # run on a Windows host; mac/linux CI got "/" and failed here).
        monkeypatch.setattr(config.sys, "platform", "win32")
        appdata = r"C:\Users\demo\AppData\Roaming"
        program_files = r"C:\Program Files"
        monkeypatch.setenv("APPDATA", appdata)
        monkeypatch.setenv("ProgramFiles", program_files)
        dirs = config._gui_binary_dirs()
        assert os.path.join(appdata, "npm") in dirs
        assert any(d.endswith(os.path.join("Program Files", "nodejs")) for d in dirs)

    def test_linux_does_not_crash_and_returns_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config.sys, "platform", "linux")
        dirs = config._gui_binary_dirs()
        assert isinstance(dirs, list)
        assert "/usr/local/bin" in dirs
        assert "/usr/bin" in dirs


# ─────────────────────────────────────────────────────────────────────
# _expand_gui_binary_dirs — glob expansion + version-aware sort + isdir filter
# ─────────────────────────────────────────────────────────────────────


class TestExpandGuiBinaryDirs:
    def test_nvm_multi_major_picks_newest_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        nvm_root = tmp_path / "nvm" / "versions" / "node"
        for ver in ("v9.5.0", "v10.2.0", "v20.1.0"):
            (nvm_root / ver / "bin").mkdir(parents=True)
        monkeypatch.setattr(config, "_gui_binary_dirs", lambda: [str(nvm_root / "*" / "bin")])
        expanded = config._expand_gui_binary_dirs()
        assert expanded[0].replace("\\", "/").endswith("v20.1.0/bin")
        assert expanded[-1].replace("\\", "/").endswith("v9.5.0/bin")

    def test_drops_nonexistent_dirs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        real = tmp_path / "real"
        real.mkdir()
        monkeypatch.setattr(
            config, "_gui_binary_dirs", lambda: [str(real), str(tmp_path / "missing")]
        )
        assert config._expand_gui_binary_dirs() == [str(real)]


# ─────────────────────────────────────────────────────────────────────
# find_npm
# ─────────────────────────────────────────────────────────────────────


class TestFindNpm:
    def test_prefers_shutil_which(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            config.shutil, "which", lambda name: "/usr/bin/npm" if name == "npm" else None
        )
        assert config.find_npm() == "/usr/bin/npm"

    def test_falls_back_to_gui_dir_darwin_arm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config.sys, "platform", "darwin")
        monkeypatch.setattr(config.shutil, "which", lambda name: None)
        homebrew_arm = tmp_path / "opt" / "homebrew" / "bin"
        npm_bin = _make_exec(homebrew_arm / "npm")
        monkeypatch.setattr(config, "_gui_binary_dirs", lambda: [str(homebrew_arm)])
        monkeypatch.setenv("PATH", "/usr/bin")

        found = config.find_npm()

        assert found == str(npm_bin)
        assert str(homebrew_arm) in os.environ["PATH"].split(os.pathsep)

    def test_falls_back_to_gui_dir_darwin_intel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config.sys, "platform", "darwin")
        monkeypatch.setattr(config.shutil, "which", lambda name: None)
        homebrew_intel = tmp_path / "usr" / "local" / "bin"
        npm_bin = _make_exec(homebrew_intel / "npm")
        monkeypatch.setattr(config, "_gui_binary_dirs", lambda: [str(homebrew_intel)])
        monkeypatch.setenv("PATH", "/usr/bin")

        assert config.find_npm() == str(npm_bin)

    def test_falls_back_via_nvm_glob_picks_newest_major(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config.sys, "platform", "darwin")
        monkeypatch.setattr(config.shutil, "which", lambda name: None)
        nvm_root = tmp_path / "nvm" / "versions" / "node"
        old_npm = _make_exec(nvm_root / "v9.5.0" / "bin" / "npm")
        _make_exec(nvm_root / "v10.2.0" / "bin" / "npm")
        newest_npm = _make_exec(nvm_root / "v20.1.0" / "bin" / "npm")
        monkeypatch.setattr(config, "_gui_binary_dirs", lambda: [str(nvm_root / "*" / "bin")])
        monkeypatch.setenv("PATH", "/usr/bin")

        found = config.find_npm()

        assert found == str(newest_npm)
        assert found != str(old_npm)

    def test_win32_prefers_npm_cmd_over_npm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config.sys, "platform", "win32")
        monkeypatch.setattr(config.shutil, "which", lambda name: None)
        node_dir = tmp_path / "nodejs"
        node_dir.mkdir()
        (node_dir / "npm").write_text("plain npm shim", encoding="utf-8")
        cmd_path = node_dir / "npm.cmd"
        cmd_path.write_text("@echo off", encoding="utf-8")
        monkeypatch.setattr(config, "_gui_binary_dirs", lambda: [str(node_dir)])
        monkeypatch.setenv("PATH", r"C:\Windows")

        assert config.find_npm() == str(cmd_path)

    def test_win32_does_not_check_x_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plain (non-executable-bit) file must still be found on Windows —
        os.access(X_OK) is unreliable there so the code must short-circuit it."""
        monkeypatch.setattr(config.sys, "platform", "win32")
        monkeypatch.setattr(config.shutil, "which", lambda name: None)
        node_dir = tmp_path / "nodejs"
        node_dir.mkdir()
        npm_exe = node_dir / "npm.exe"
        npm_exe.write_bytes(b"not really a PE, just bytes")
        monkeypatch.setattr(config, "_gui_binary_dirs", lambda: [str(node_dir)])
        monkeypatch.setattr(
            config.os,
            "access",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("os.access must be short-circuited on win32")
            ),
        )
        monkeypatch.setenv("PATH", r"C:\Windows")

        assert config.find_npm() == str(npm_exe)

    def test_linux_finds_local_bin(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config.sys, "platform", "linux")
        monkeypatch.setattr(config.shutil, "which", lambda name: None)
        local_bin = tmp_path / ".local" / "bin"
        npm_bin = _make_exec(local_bin / "npm")
        monkeypatch.setattr(config, "_gui_binary_dirs", lambda: [str(local_bin)])
        monkeypatch.setenv("PATH", "/usr/bin")

        assert config.find_npm() == str(npm_bin)

    def test_not_found_returns_none_and_path_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config.shutil, "which", lambda name: None)
        monkeypatch.setattr(config, "_gui_binary_dirs", lambda: [str(tmp_path / "nowhere")])
        monkeypatch.setenv("PATH", "/usr/bin")

        assert config.find_npm() is None
        assert os.environ["PATH"] == "/usr/bin"


# ─────────────────────────────────────────────────────────────────────
# ensure_gui_path — memoized, best-effort, cross-platform
# ─────────────────────────────────────────────────────────────────────


class TestEnsureGuiPath:
    def test_prepends_missing_dirs_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        extra = tmp_path / "extra" / "bin"
        extra.mkdir(parents=True)
        monkeypatch.setattr(config, "_gui_binary_dirs", lambda: [str(extra)])
        monkeypatch.setenv("PATH", "/usr/bin")

        config.ensure_gui_path()

        assert str(extra) in os.environ["PATH"].split(os.pathsep)

    def test_idempotent_no_duplicate_and_no_rescan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        extra = tmp_path / "extra" / "bin"
        extra.mkdir(parents=True)
        calls = {"n": 0}
        real_expand = config._expand_gui_binary_dirs

        def _spy() -> list[str]:
            calls["n"] += 1
            return real_expand()

        monkeypatch.setattr(config, "_gui_binary_dirs", lambda: [str(extra)])
        monkeypatch.setattr(config, "_expand_gui_binary_dirs", _spy)
        monkeypatch.setenv("PATH", "/usr/bin")

        config.ensure_gui_path()
        config.ensure_gui_path()

        assert calls["n"] == 1, "second call must not re-scan the filesystem"
        assert os.environ["PATH"].split(os.pathsep).count(str(extra)) == 1

    def test_never_raises_even_if_scanning_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> list[str]:
            raise OSError("simulated glob failure")

        monkeypatch.setattr(config, "_gui_binary_dirs", _boom)

        config.ensure_gui_path()  # must not raise

    def test_runs_on_every_platform_without_crashing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Register PATH with monkeypatch *before* calling so its real value is
        # captured and restored on teardown, even though ensure_gui_path()
        # mutates os.environ directly (not through monkeypatch.setenv).
        monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
        for plat in ("darwin", "win32", "linux"):
            monkeypatch.setattr(config, "_gui_path_ensured", False)
            monkeypatch.setattr(config.sys, "platform", plat)
            config.ensure_gui_path()  # must not raise on any platform
