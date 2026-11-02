"""#965: prod froze 11.4 s at boot on a claude `--version` probe run on the Qt
main thread, then stalled ~1 s every 2 s re-globbing the claude store for a
Lead transcript that did not exist yet."""

from __future__ import annotations

import json
import sys

import pytest

from agent_takkub import pane_env
from agent_takkub.remote import notify


def test_version_comes_from_package_json_without_a_process(monkeypatch, tmp_path):
    pkg = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code"
    exe = pkg / "bin" / "claude.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"x")
    (pkg / "package.json").write_text(
        json.dumps({"name": "@anthropic-ai/claude-code", "version": "2.1.316"}), encoding="utf-8"
    )

    def no_process(*_a, **_kw):
        raise AssertionError("must not launch claude to read its version")

    monkeypatch.setattr(pane_env.subprocess, "run", no_process)
    monkeypatch.setattr(pane_env, "_CLAUDE_VERSION_CACHE", {})
    assert pane_env._claude_version(str(exe)) == "2.1.316"


def test_version_probe_is_cached_per_binary(monkeypatch, tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_bytes(b"x")
    calls = []

    def fake_run(*_a, **_kw):
        calls.append(1)
        return type("C", (), {"returncode": 0, "stdout": "2.1.300 (Claude Code)"})()

    monkeypatch.setattr(pane_env.subprocess, "run", fake_run)
    monkeypatch.setattr(pane_env, "_CLAUDE_VERSION_CACHE", {})
    assert pane_env._claude_version(str(exe)) == "2.1.300 (Claude Code)"
    assert pane_env._claude_version(str(exe)) == "2.1.300 (Claude Code)"
    assert len(calls) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="the pipe hang is Windows-specific")
def test_probe_returns_at_timeout_even_if_a_grandchild_keeps_stdout(tmp_path):
    import time

    bat = tmp_path / "slow.bat"
    bat.write_text(
        '@echo off\r\nstart "" /b cmd /c "ping -n 12 127.0.0.1 >nul"\r\n'
        "ping -n 12 127.0.0.1 >nul\r\n",
        encoding="utf-8",
    )
    t0 = time.monotonic()
    assert pane_env._claude_version_probe(str(bat)) is None
    assert time.monotonic() - t0 < 6


def test_missing_transcript_is_not_globbed_every_poll(monkeypatch, tmp_path):
    base = tmp_path / "claude-config"
    (base / "projects" / "some-dir").mkdir(parents=True)
    monkeypatch.setattr(notify, "config_dir_for", lambda _p: base)
    monkeypatch.setattr(notify, "_jsonl_glob_miss_at", {})
    monkeypatch.setattr(notify, "_jsonl_path_cache", {})
    globs = []
    real_glob = type(base).glob

    def counting_glob(self, pattern):
        globs.append(pattern)
        return real_glob(self, pattern)

    monkeypatch.setattr(type(base), "glob", counting_glob)
    for _ in range(5):
        assert notify._resolve_claude_jsonl_path("p", "uuid-1") is None
    assert len(globs) == 1


def test_legacy_cwd_dir_is_found_without_a_glob(monkeypatch, tmp_path):
    from agent_takkub import config

    base = tmp_path / "claude-config"
    cwd = tmp_path / "my_proj.web"
    cwd.mkdir()
    encoded = "".join(c if c.isalnum() else "-" for c in str(cwd.resolve()))
    transcript = base / "projects" / encoded / "uuid-2.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(notify, "config_dir_for", lambda _p: base)
    monkeypatch.setattr(config, "lead_cwd", lambda _p: str(cwd))
    monkeypatch.setattr(notify, "_jsonl_path_cache", {})
    monkeypatch.setattr(type(base), "glob", lambda *_a: pytest.fail("glob not needed"))
    assert notify._resolve_claude_jsonl_path("p", "uuid-2") == transcript
