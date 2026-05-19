"""Tests for `gemini_helper` - the Gemini CLI wrapper behind
`takkub gemini "<prompt>"`. Mocks shutil.which + subprocess.run so
no real `gemini` calls leak from CI; the goal is to pin the argv
construction (gemini uses `-p` flag, NOT a subcommand) + the
error-surfacing contract.
"""

from __future__ import annotations

import subprocess

import pytest

from agent_takkub import gemini_helper


def _proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["gemini"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestFindGeminiExecutable:
    def test_returns_path_when_on_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper.shutil, "which", lambda name: "/usr/local/bin/gemini")
        assert gemini_helper.find_gemini_executable() == "/usr/local/bin/gemini"

    def test_returns_none_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper.shutil, "which", lambda name: None)
        assert gemini_helper.find_gemini_executable() is None


class TestGeminiExec:
    def test_returns_install_hint_when_binary_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gemini_helper, "find_gemini_executable", lambda: None)
        ok, msg = gemini_helper.gemini_exec("hi")
        assert ok is False
        assert "npm install -g @google/gemini-cli" in msg

    def test_rejects_empty_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_gemini_executable", lambda: "/x/gemini")
        ok, msg = gemini_helper.gemini_exec("   ")
        assert ok is False
        assert "empty prompt" in msg

    def test_builds_argv_without_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Gemini's headless flag is `-p <prompt>` - NOT a subcommand
        # like codex's `exec`. Pinning this is the whole point of the
        # test: a future refactor that reuses codex's argv shape would
        # send `gemini exec "..."` which would fail with "unknown
        # command".
        monkeypatch.setattr(gemini_helper, "find_gemini_executable", lambda: "/x/gemini")
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return _proc(0, stdout="ok")

        monkeypatch.setattr(gemini_helper.subprocess, "run", fake_run)
        ok, msg = gemini_helper.gemini_exec("hello world")
        assert ok is True
        assert msg == "ok"
        assert seen["argv"] == ["/x/gemini", "-p", "hello world"]

    def test_builds_argv_with_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `-m <model>` goes before `-p <prompt>` (both are flags so
        # ordering is technically irrelevant to yargs, but we pin a
        # stable shape for diffability).
        monkeypatch.setattr(gemini_helper, "find_gemini_executable", lambda: "/x/gemini")
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return _proc(0, stdout="out")

        monkeypatch.setattr(gemini_helper.subprocess, "run", fake_run)
        ok, _ = gemini_helper.gemini_exec("review", model="gemini-2.5-pro")
        assert ok is True
        assert seen["argv"] == ["/x/gemini", "-m", "gemini-2.5-pro", "-p", "review"]

    def test_propagates_cwd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_gemini_executable", lambda: "/x/gemini")
        seen = {}

        def fake_run(argv, **kwargs):
            seen["kwargs"] = kwargs
            return _proc(0)

        monkeypatch.setattr(gemini_helper.subprocess, "run", fake_run)
        gemini_helper.gemini_exec("hi", cwd="C:/projects/foo")
        assert seen["kwargs"]["cwd"] == "C:/projects/foo"

    def test_returns_false_on_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_gemini_executable", lambda: "/x/gemini")

        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="gemini", timeout=120.0)

        monkeypatch.setattr(gemini_helper.subprocess, "run", fake_run)
        ok, msg = gemini_helper.gemini_exec("hi")
        assert ok is False
        assert "timed out" in msg

    def test_returns_false_when_binary_disappears(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_gemini_executable", lambda: "/x/gemini")

        def fake_run(*args, **kwargs):
            raise FileNotFoundError("gemini")

        monkeypatch.setattr(gemini_helper.subprocess, "run", fake_run)
        ok, msg = gemini_helper.gemini_exec("hi")
        assert ok is False
        assert "disappeared" in msg

    def test_surfaces_stderr_on_nonzero_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_gemini_executable", lambda: "/x/gemini")
        monkeypatch.setattr(
            gemini_helper.subprocess,
            "run",
            lambda *a, **k: _proc(1, stderr="ERROR: auth expired. Re-run `gemini`."),
        )
        ok, msg = gemini_helper.gemini_exec("hi")
        assert ok is False
        assert "auth expired" in msg

    def test_falls_back_to_stdout_when_stderr_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_gemini_executable", lambda: "/x/gemini")
        monkeypatch.setattr(
            gemini_helper.subprocess,
            "run",
            lambda *a, **k: _proc(2, stdout="rate-limited", stderr=""),
        )
        ok, msg = gemini_helper.gemini_exec("hi")
        assert ok is False
        assert "rate-limited" in msg

    def test_trims_trailing_whitespace_from_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_gemini_executable", lambda: "/x/gemini")
        monkeypatch.setattr(
            gemini_helper.subprocess,
            "run",
            lambda *a, **k: _proc(0, stdout="answer text\n\n  "),
        )
        ok, msg = gemini_helper.gemini_exec("hi")
        assert ok is True
        assert msg == "answer text"
