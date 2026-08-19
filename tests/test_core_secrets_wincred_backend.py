"""WinCredBackend: mocked ctypes boundary (`_cred_read_bytes`) — never
touches the real Windows Credential Manager."""

from __future__ import annotations

import pytest

from agent_takkub.core.secrets.backends import BackendStatus, SecretUnavailableError
from agent_takkub.core.secrets.backends import wincred_backend as wc


def test_unavailable_off_windows(monkeypatch):
    monkeypatch.setattr(wc.sys, "platform", "darwin")
    backend = wc.WinCredBackend("takkub/claude")
    assert backend.status("default") == BackendStatus.UNAVAILABLE
    assert backend.get("default") is None


def test_get_missing_credential_returns_none(monkeypatch):
    monkeypatch.setattr(wc.sys, "platform", "win32")
    monkeypatch.setattr(wc, "_cred_read_bytes", lambda target_name: None)
    backend = wc.WinCredBackend("takkub/claude")
    assert backend.get("default") is None
    assert backend.status("default") == BackendStatus.MISSING


def test_get_decodes_utf16le_blob(monkeypatch):
    monkeypatch.setattr(wc.sys, "platform", "win32")
    value = "super-secret-token"
    blob = value.encode("utf-16-le")
    seen_targets = []

    def _fake_read(target_name: str) -> bytes | None:
        seen_targets.append(target_name)
        return blob if target_name == "takkub/claude/default" else None

    monkeypatch.setattr(wc, "_cred_read_bytes", _fake_read)
    backend = wc.WinCredBackend("takkub/claude")
    assert backend.get("default") == value
    assert backend.status("default") == BackendStatus.FOUND
    assert seen_targets == ["takkub/claude/default", "takkub/claude/default"]


def test_advapi32_is_none_off_windows(monkeypatch):
    monkeypatch.setattr(wc.sys, "platform", "darwin")
    assert wc._advapi32() is None


def test_cred_read_bytes_short_circuits_when_advapi32_unavailable(monkeypatch):
    monkeypatch.setattr(wc, "_advapi32", lambda: None)
    assert wc._cred_read_bytes("takkub/claude/default") is None


def test_set_not_implemented_yet(monkeypatch):
    monkeypatch.setattr(wc.sys, "platform", "win32")
    backend = wc.WinCredBackend("takkub/claude")
    with pytest.raises(SecretUnavailableError):
        backend.set("default", "value")


def test_delete_not_implemented_yet(monkeypatch):
    monkeypatch.setattr(wc.sys, "platform", "win32")
    backend = wc.WinCredBackend("takkub/claude")
    with pytest.raises(SecretUnavailableError):
        backend.delete("default")
