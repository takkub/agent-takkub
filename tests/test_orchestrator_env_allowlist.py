"""Tests for orchestrator._build_pane_env() — env allowlist for spawned panes."""

from __future__ import annotations

import os

import pytest

from agent_takkub.orchestrator import _build_lead_env, _build_pane_env


def test_build_pane_env_includes_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = _build_pane_env()
    assert "PATH" in env
    assert env["PATH"] == "/usr/bin:/bin"


def test_build_pane_env_includes_userprofile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USERPROFILE", "C:\\Users\\test")
    env = _build_pane_env()
    assert "USERPROFILE" in env


def test_build_pane_env_includes_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/testuser")
    env = _build_pane_env()
    assert "HOME" in env


def test_build_pane_env_excludes_anthropic_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-key")
    env = _build_pane_env()
    assert "ANTHROPIC_API_KEY" not in env


def test_build_pane_env_excludes_openai_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret")
    env = _build_pane_env()
    assert "OPENAI_API_KEY" not in env


def test_build_pane_env_excludes_gh_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghp_supersecret")
    env = _build_pane_env()
    assert "GH_TOKEN" not in env


def test_build_pane_env_excludes_aws_access_key_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA_FAKE_KEY")
    env = _build_pane_env()
    assert "AWS_ACCESS_KEY_ID" not in env


def test_build_pane_env_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    # On some systems env var names come through in lower case; allowlist should still pass them
    monkeypatch.setenv("path", "/usr/local/bin")
    env = _build_pane_env()
    # Either "path" or "PATH" should be in env (allowlist normalises via .upper())
    assert "path" in env or "PATH" in env


def test_build_pane_env_returns_plain_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _build_pane_env()
    assert isinstance(env, dict)
    # Must not be the os.environ object itself — mutation safety
    assert env is not os.environ


def test_build_pane_env_includes_comspec(monkeypatch: pytest.MonkeyPatch) -> None:
    # COMSPEC = path to cmd.exe; Node.js child_process needs it on Windows.
    # Missing COMSPEC → ENOENT crash in MCP servers (codex_apps) that shell out.
    monkeypatch.setenv("COMSPEC", "C:\\Windows\\system32\\cmd.exe")
    env = _build_pane_env()
    assert "COMSPEC" in env
    assert env["COMSPEC"] == "C:\\Windows\\system32\\cmd.exe"


def test_build_pane_env_includes_userdomain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USERDOMAIN", "WORKGROUP")
    env = _build_pane_env()
    assert "USERDOMAIN" in env


def test_build_pane_env_includes_sessionname(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSIONNAME", "Console")
    env = _build_pane_env()
    assert "SESSIONNAME" in env


def test_build_pane_env_forwards_port_file(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression: in multi-instance mode app.py sets TAKKUB_PORT_FILE in the
    # cockpit process env so panes dial *this* cockpit's cli_server. If the
    # allowlist drops it, the pane's `takkub` CLI falls back to a stale
    # runtime/port and gets connection-refused.
    monkeypatch.setenv("TAKKUB_PORT_FILE", "/tmp/agent-takkub-port.4242")
    env = _build_pane_env()
    assert env.get("TAKKUB_PORT_FILE") == "/tmp/agent-takkub-port.4242"


def test_build_lead_env_forwards_port_file(monkeypatch: pytest.MonkeyPatch) -> None:
    # Lead is a pane too — it must reach its own cockpit's cli_server for
    # every `takkub assign/list/status`, so the port file must survive the
    # Lead allowlist as well.
    monkeypatch.setenv("TAKKUB_PORT_FILE", "/tmp/agent-takkub-port.4242")
    env = _build_lead_env()
    assert env.get("TAKKUB_PORT_FILE") == "/tmp/agent-takkub-port.4242"
