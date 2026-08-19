"""FileSecretBackend: round-trip, atomic write, missing file."""

from __future__ import annotations

from agent_takkub.core.secrets.backends import BackendStatus
from agent_takkub.core.secrets.backends.file_backend import FileSecretBackend


def test_status_missing_when_file_absent(tmp_path):
    backend = FileSecretBackend(tmp_path / "does-not-exist.json")
    assert backend.status("default") == BackendStatus.MISSING
    assert backend.get("default") is None


def test_set_then_get_round_trips(tmp_path):
    backend = FileSecretBackend(tmp_path / "creds.json")
    backend.set("default", '{"access_token": "abc"}')
    assert backend.status("default") == BackendStatus.FOUND
    assert backend.get("default") == '{"access_token": "abc"}'


def test_set_creates_parent_directories(tmp_path):
    backend = FileSecretBackend(tmp_path / "nested" / "dir" / "creds.json")
    backend.set("default", "hello")
    assert backend.get("default") == "hello"


def test_set_overwrites_existing_value(tmp_path):
    backend = FileSecretBackend(tmp_path / "creds.json")
    backend.set("default", "first")
    backend.set("default", "second")
    assert backend.get("default") == "second"


def test_set_is_atomic_via_os_replace(tmp_path, monkeypatch):
    path = tmp_path / "creds.json"
    backend = FileSecretBackend(path)

    calls = []
    real_replace = __import__("os").replace

    def spy_replace(src, dst):
        calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr("agent_takkub.core.secrets.backends.file_backend.os.replace", spy_replace)
    backend.set("default", "value")
    assert len(calls) == 1
    tmp_leftover = path.with_name(path.name + ".tmp")
    assert not tmp_leftover.exists()


def test_delete_removes_file(tmp_path):
    path = tmp_path / "creds.json"
    backend = FileSecretBackend(path)
    backend.set("default", "value")
    assert path.is_file()
    backend.delete("default")
    assert not path.exists()
    assert backend.status("default") == BackendStatus.MISSING


def test_delete_on_missing_file_does_not_raise(tmp_path):
    backend = FileSecretBackend(tmp_path / "does-not-exist.json")
    backend.delete("default")  # must not raise


def test_get_on_unreadable_directory_path_returns_none(tmp_path):
    # A directory at the credential path can never be read as a file — this
    # backend must report it as "no secret", not crash.
    dir_path = tmp_path / "creds.json"
    dir_path.mkdir()
    backend = FileSecretBackend(dir_path)
    assert backend.get("default") is None
