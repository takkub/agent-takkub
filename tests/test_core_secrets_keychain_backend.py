"""KeychainBackend: mocked `security` subprocess (runs on any OS, incl.
Windows) — never touches a real macOS Keychain."""

from __future__ import annotations

import subprocess

import pytest

from agent_takkub.core.secrets.backends import BackendStatus, SecretUnavailableError
from agent_takkub.core.secrets.backends import keychain_backend as kb


def _fake_run(stdout: str, returncode: int = 0):
    def _run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

    return _run


def test_unavailable_off_darwin(monkeypatch):
    monkeypatch.setattr(kb.sys, "platform", "win32")
    backend = kb.KeychainBackend("Claude Code-credentials")
    assert backend.status("default") == BackendStatus.UNAVAILABLE
    assert backend.get("default") is None


def test_found_on_darwin_with_entry(monkeypatch):
    monkeypatch.setattr(kb.sys, "platform", "darwin")
    monkeypatch.setattr(kb.subprocess, "run", _fake_run('{"accessToken": "tok"}\n'))
    backend = kb.KeychainBackend("Claude Code-credentials")
    assert backend.get("default") == '{"accessToken": "tok"}'
    assert backend.status("default") == BackendStatus.FOUND


def test_missing_on_darwin_without_entry(monkeypatch):
    monkeypatch.setattr(kb.sys, "platform", "darwin")
    monkeypatch.setattr(kb.subprocess, "run", _fake_run("", returncode=44))
    backend = kb.KeychainBackend("Claude Code-credentials")
    assert backend.get("default") is None
    assert backend.status("default") == BackendStatus.MISSING


def test_get_swallows_subprocess_errors(monkeypatch):
    monkeypatch.setattr(kb.sys, "platform", "darwin")

    def _raise(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=5)

    monkeypatch.setattr(kb.subprocess, "run", _raise)
    backend = kb.KeychainBackend("Claude Code-credentials")
    assert backend.get("default") is None


def test_set_is_refused_read_only(monkeypatch):
    monkeypatch.setattr(kb.sys, "platform", "darwin")
    backend = kb.KeychainBackend("Claude Code-credentials")
    with pytest.raises(SecretUnavailableError):
        backend.set("default", "value")


def test_delete_is_refused_read_only(monkeypatch):
    monkeypatch.setattr(kb.sys, "platform", "darwin")
    backend = kb.KeychainBackend("Claude Code-credentials")
    with pytest.raises(SecretUnavailableError):
        backend.delete("default")
