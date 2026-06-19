"""Tests for `gemini_helper` - the Antigravity CLI (`agy`) wrapper
behind `takkub gemini "<prompt>"`. Google retired the standalone Gemini
CLI on 2026-06-18, so the `gemini` role now runs `agy`. Mocks
shutil.which + subprocess.run so no real `agy` calls leak from CI; the
goal is to pin the argv construction (`agy -p`, NOT a subcommand) + the
error-surfacing contract.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_takkub import gemini_helper


def _proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["agy"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestFindAgyExecutable:
    def test_returns_path_when_on_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper.shutil, "which", lambda name: "/usr/local/bin/agy")
        assert gemini_helper.find_agy_executable() == "/usr/local/bin/agy"

    def test_probes_the_agy_binary_not_gemini(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Regression guard: the role is named "gemini" but the binary is
        # `agy`. A which("gemini") probe would always miss post-migration.
        seen = {}

        def fake_which(name):
            seen["name"] = name
            return "/x/agy"

        monkeypatch.setattr(gemini_helper.shutil, "which", fake_which)
        gemini_helper.find_agy_executable()
        assert seen["name"] == "agy"

    def test_returns_none_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper.shutil, "which", lambda name: None)
        monkeypatch.setattr(gemini_helper, "_default_agy_paths", lambda: [])
        assert gemini_helper.find_agy_executable() is None

    def test_falls_back_to_fixed_install_path_when_off_PATH(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        # Regression: the Antigravity Windows installer drops agy.exe under
        # %LOCALAPPDATA%\agy\bin but doesn't reliably add it to PATH. When
        # which() misses, we must still find the binary at its fixed
        # location — otherwise the cockpit falsely degrades gemini→claude.
        agy_exe = tmp_path / "agy" / "bin" / "agy.exe"
        agy_exe.parent.mkdir(parents=True)
        agy_exe.write_text("")
        monkeypatch.setattr(gemini_helper.shutil, "which", lambda name: None)
        monkeypatch.setattr(gemini_helper, "_default_agy_paths", lambda: [agy_exe])
        assert gemini_helper.find_agy_executable() == str(agy_exe)

    def test_path_wins_over_fixed_location(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A correctly-registered PATH entry takes priority over the fixed
        # fallback (no needless disk probing when PATH already resolves).
        monkeypatch.setattr(gemini_helper.shutil, "which", lambda name: "/on/path/agy")
        monkeypatch.setattr(
            gemini_helper,
            "_default_agy_paths",
            lambda: [Path("/should/not/be/used/agy.exe")],
        )
        assert gemini_helper.find_agy_executable() == "/on/path/agy"


class TestGeminiExec:
    def test_returns_install_hint_when_binary_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gemini_helper, "find_agy_executable", lambda: None)
        ok, msg = gemini_helper.gemini_exec("hi")
        assert ok is False
        assert "agy binary not on PATH" in msg
        assert "antigravity.google/download" in msg

    def test_rejects_empty_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_agy_executable", lambda: "/x/agy")
        ok, msg = gemini_helper.gemini_exec("   ")
        assert ok is False
        assert "empty prompt" in msg

    def test_builds_argv_without_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Antigravity's headless flag is `-p <prompt>` - NOT a subcommand
        # like codex's `exec`. Pinning this is the whole point of the
        # test: a future refactor that reuses codex's argv shape would
        # send `agy exec "..."` which would fail with "unknown command".
        monkeypatch.setattr(gemini_helper, "find_agy_executable", lambda: "/x/agy")
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return _proc(0, stdout="ok")

        monkeypatch.setattr(gemini_helper.subprocess, "run", fake_run)
        ok, msg = gemini_helper.gemini_exec("hello world")
        assert ok is True
        assert msg == "ok"
        # default timeout 120 → agy --print-timeout bounded to 115s (5s under us)
        assert seen["argv"] == ["/x/agy", "-p", "hello world", "--print-timeout", "115s"]

    def test_builds_argv_with_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `-m <model>` goes before `-p <prompt>` (both are flags so
        # ordering is technically irrelevant, but we pin a stable shape
        # for diffability).
        monkeypatch.setattr(gemini_helper, "find_agy_executable", lambda: "/x/agy")
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return _proc(0, stdout="out")

        monkeypatch.setattr(gemini_helper.subprocess, "run", fake_run)
        ok, _ = gemini_helper.gemini_exec("review", model="gemini-3.1-pro")
        assert ok is True
        assert seen["argv"] == [
            "/x/agy",
            "-m",
            "gemini-3.1-pro",
            "-p",
            "review",
            "--print-timeout",
            "115s",
        ]

    def test_empty_output_on_success_is_actionable_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # agy `-p` exits 0 but emits nothing when run non-interactively (no TTY).
        # We must NOT hand back a blank "success" — turn it into guidance toward
        # the interactive pane instead.
        monkeypatch.setattr(gemini_helper, "find_agy_executable", lambda: "/x/agy")
        monkeypatch.setattr(
            gemini_helper.subprocess, "run", lambda *a, **k: _proc(0, stdout="   \n")
        )
        ok, msg = gemini_helper.gemini_exec("hi")
        assert ok is False
        assert "no output" in msg
        assert "takkub assign --role gemini" in msg

    def test_print_timeout_scales_with_subprocess_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gemini_helper, "find_agy_executable", lambda: "/x/agy")
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return _proc(0, stdout="ok")

        monkeypatch.setattr(gemini_helper.subprocess, "run", fake_run)
        gemini_helper.gemini_exec("hi", timeout=30)
        assert "--print-timeout" in seen["argv"]
        assert seen["argv"][seen["argv"].index("--print-timeout") + 1] == "25s"

    def test_propagates_cwd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_agy_executable", lambda: "/x/agy")
        seen = {}

        def fake_run(argv, **kwargs):
            seen["kwargs"] = kwargs
            return _proc(0)

        monkeypatch.setattr(gemini_helper.subprocess, "run", fake_run)
        gemini_helper.gemini_exec("hi", cwd="C:/projects/foo")
        assert seen["kwargs"]["cwd"] == "C:/projects/foo"

    def test_returns_false_on_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_agy_executable", lambda: "/x/agy")

        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="agy", timeout=120.0)

        monkeypatch.setattr(gemini_helper.subprocess, "run", fake_run)
        ok, msg = gemini_helper.gemini_exec("hi")
        assert ok is False
        assert "timed out" in msg

    def test_returns_false_when_binary_disappears(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_agy_executable", lambda: "/x/agy")

        def fake_run(*args, **kwargs):
            raise FileNotFoundError("agy")

        monkeypatch.setattr(gemini_helper.subprocess, "run", fake_run)
        ok, msg = gemini_helper.gemini_exec("hi")
        assert ok is False
        assert "disappeared" in msg

    def test_surfaces_stderr_on_nonzero_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_agy_executable", lambda: "/x/agy")
        monkeypatch.setattr(
            gemini_helper.subprocess,
            "run",
            lambda *a, **k: _proc(1, stderr="ERROR: auth expired. Re-run `agy`."),
        )
        ok, msg = gemini_helper.gemini_exec("hi")
        assert ok is False
        assert "auth expired" in msg

    def test_falls_back_to_stdout_when_stderr_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_agy_executable", lambda: "/x/agy")
        monkeypatch.setattr(
            gemini_helper.subprocess,
            "run",
            lambda *a, **k: _proc(2, stdout="rate-limited", stderr=""),
        )
        ok, msg = gemini_helper.gemini_exec("hi")
        assert ok is False
        assert "rate-limited" in msg

    def test_trims_trailing_whitespace_from_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_agy_executable", lambda: "/x/agy")
        monkeypatch.setattr(
            gemini_helper.subprocess,
            "run",
            lambda *a, **k: _proc(0, stdout="answer text\n\n  "),
        )
        ok, msg = gemini_helper.gemini_exec("hi")
        assert ok is True
        assert msg == "answer text"
