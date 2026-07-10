"""Tests for `codex_helper` — the Codex CLI wrapper behind
`takkub codex "<prompt>"`. Mocks shutil.which + subprocess.run so
no real `codex` calls leak from CI; the goal is to pin the argv
construction + the error-surfacing contract.
"""

from __future__ import annotations

import subprocess

import pytest

from agent_takkub import codex_helper


def _proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["codex"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestFindCodexExecutable:
    def test_returns_path_when_on_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(codex_helper.shutil, "which", lambda name: "/usr/local/bin/codex")
        assert codex_helper.find_codex_executable() == "/usr/local/bin/codex"

    def test_returns_none_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(codex_helper.shutil, "which", lambda name: None)
        assert codex_helper.find_codex_executable() is None


class TestFindCodexExecutableAvoidsCmdShim:
    """L4 (cross-platform audit 2026-07-10): mirrors find_claude_executable's
    `.cmd`-avoidance — on Windows, invoking the npm `.cmd` shim through
    ConPTY flashes a visible cmd.exe console. Prefer the real vendored
    `codex.exe` (confirmed shipped by @openai/codex, nested under its
    platform-specific optional-dependency package) when it's found next to
    the shim; fall back to the `.cmd` path otherwise."""

    def test_windows_prefers_vendored_exe_when_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        npm_dir = tmp_path / "npm"
        native = (
            npm_dir
            / "node_modules"
            / "@openai"
            / "codex"
            / "node_modules"
            / "@openai"
            / "codex-win32-x64"
            / "vendor"
            / "x86_64-pc-windows-msvc"
            / "bin"
            / "codex.exe"
        )
        native.parent.mkdir(parents=True)
        native.write_text("")
        cmd_shim = npm_dir / "codex.CMD"
        cmd_shim.write_text("")

        monkeypatch.setattr(codex_helper.sys, "platform", "win32")
        monkeypatch.setattr(codex_helper.shutil, "which", lambda name: str(cmd_shim))

        assert codex_helper.find_codex_executable() == str(native)

    def test_windows_falls_back_to_cmd_shim_when_exe_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        npm_dir = tmp_path / "npm"
        npm_dir.mkdir()
        cmd_shim = npm_dir / "codex.CMD"
        cmd_shim.write_text("")

        monkeypatch.setattr(codex_helper.sys, "platform", "win32")
        monkeypatch.setattr(codex_helper.shutil, "which", lambda name: str(cmd_shim))

        assert codex_helper.find_codex_executable() == str(cmd_shim)

    def test_non_windows_never_probes_for_exe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # POSIX has no console-flash problem — shutil.which's result is
        # returned as-is, no extra filesystem probing.
        monkeypatch.setattr(codex_helper.sys, "platform", "darwin")
        monkeypatch.setattr(codex_helper.shutil, "which", lambda name: "/usr/local/bin/codex")
        assert codex_helper.find_codex_executable() == "/usr/local/bin/codex"


class TestCodexExec:
    def test_returns_install_hint_when_binary_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(codex_helper, "find_codex_executable", lambda: None)
        ok, msg = codex_helper.codex_exec("hi")
        assert ok is False
        assert "npm install -g @openai/codex" in msg
        assert "codex login" in msg

    def test_rejects_empty_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Prevent accidental empty calls (e.g. user runs `takkub codex ""`
        # by mistake) — codex itself would just hang waiting for stdin.
        monkeypatch.setattr(codex_helper, "find_codex_executable", lambda: "/x/codex")
        ok, msg = codex_helper.codex_exec("   ")
        assert ok is False
        assert "empty prompt" in msg

    def test_builds_argv_without_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(codex_helper, "find_codex_executable", lambda: "/x/codex")
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return _proc(0, stdout="ok")

        monkeypatch.setattr(codex_helper.subprocess, "run", fake_run)
        ok, msg = codex_helper.codex_exec("hello world")
        assert ok is True
        assert msg == "ok"
        assert seen["argv"] == ["/x/codex", "exec", "hello world"]

    def test_builds_argv_with_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # --model gets inserted BEFORE the prompt so clap treats the
        # prompt as the trailing positional, not as the model value.
        monkeypatch.setattr(codex_helper, "find_codex_executable", lambda: "/x/codex")
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return _proc(0, stdout="out")

        monkeypatch.setattr(codex_helper.subprocess, "run", fake_run)
        ok, _ = codex_helper.codex_exec("review", model="gpt-5-codex")
        assert ok is True
        assert seen["argv"] == ["/x/codex", "exec", "--model", "gpt-5-codex", "review"]

    def test_propagates_cwd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # cwd is critical: `takkub codex "review this"` inside a project
        # pane must run Codex against THAT project, not the cockpit's cwd.
        monkeypatch.setattr(codex_helper, "find_codex_executable", lambda: "/x/codex")
        seen = {}

        def fake_run(argv, **kwargs):
            seen["kwargs"] = kwargs
            return _proc(0)

        monkeypatch.setattr(codex_helper.subprocess, "run", fake_run)
        codex_helper.codex_exec("hi", cwd="C:/projects/foo")
        assert seen["kwargs"]["cwd"] == "C:/projects/foo"

    def test_returns_false_on_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(codex_helper, "find_codex_executable", lambda: "/x/codex")

        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="codex", timeout=120.0)

        monkeypatch.setattr(codex_helper.subprocess, "run", fake_run)
        ok, msg = codex_helper.codex_exec("hi")
        assert ok is False
        assert "timed out" in msg

    def test_returns_false_when_binary_disappears(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(codex_helper, "find_codex_executable", lambda: "/x/codex")

        def fake_run(*args, **kwargs):
            raise FileNotFoundError("codex")

        monkeypatch.setattr(codex_helper.subprocess, "run", fake_run)
        ok, msg = codex_helper.codex_exec("hi")
        assert ok is False
        assert "disappeared" in msg

    def test_surfaces_stderr_on_nonzero_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Auth expired, rate limit, etc. — Codex writes the diagnostic
        # to stderr. Pass it back so `takkub codex` users see the real
        # reason instead of a generic "codex failed".
        monkeypatch.setattr(codex_helper, "find_codex_executable", lambda: "/x/codex")
        monkeypatch.setattr(
            codex_helper.subprocess,
            "run",
            lambda *a, **k: _proc(1, stderr="ERROR: not logged in. Run `codex login`."),
        )
        ok, msg = codex_helper.codex_exec("hi")
        assert ok is False
        assert "not logged in" in msg

    def test_falls_back_to_stdout_when_stderr_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Some failure modes write to stdout (e.g. structured JSON
        # error). Surface whichever channel has content.
        monkeypatch.setattr(codex_helper, "find_codex_executable", lambda: "/x/codex")
        monkeypatch.setattr(
            codex_helper.subprocess,
            "run",
            lambda *a, **k: _proc(2, stdout="rate-limited", stderr=""),
        )
        ok, msg = codex_helper.codex_exec("hi")
        assert ok is False
        assert "rate-limited" in msg

    def test_trims_trailing_whitespace_from_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Codex CLI appends a trailing newline + occasionally a banner;
        # the helper strips so the caller doesn't have to.
        monkeypatch.setattr(codex_helper, "find_codex_executable", lambda: "/x/codex")
        monkeypatch.setattr(
            codex_helper.subprocess,
            "run",
            lambda *a, **k: _proc(0, stdout="answer text\n\n  "),
        )
        ok, msg = codex_helper.codex_exec("hi")
        assert ok is True
        assert msg == "answer text"
