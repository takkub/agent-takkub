"""Tests for `agent_takkub.remote.session_store` — the on-disk password-
session store (#196). `AuthGate`'s own persistence behavior is covered by
`test_remote_auth.py::TestPasswordSessionPersistence`; this file tests the
storage primitives directly.
"""

from __future__ import annotations

import stat
import sys

from agent_takkub.remote import session_store
from agent_takkub.remote.config import RemoteConfig


class TestFingerprint:
    def test_same_config_fields_same_fingerprint(self):
        cfg1 = RemoteConfig(password_hash="h", secret_path="s", token="t")
        cfg2 = RemoteConfig(password_hash="h", secret_path="s", token="t")
        assert session_store.fingerprint(cfg1) == session_store.fingerprint(cfg2)

    def test_password_hash_change_changes_fingerprint(self):
        cfg1 = RemoteConfig(password_hash="h1", secret_path="s", token="t")
        cfg2 = RemoteConfig(password_hash="h2", secret_path="s", token="t")
        assert session_store.fingerprint(cfg1) != session_store.fingerprint(cfg2)

    def test_secret_path_change_changes_fingerprint(self):
        cfg1 = RemoteConfig(password_hash="h", secret_path="s1", token="t")
        cfg2 = RemoteConfig(password_hash="h", secret_path="s2", token="t")
        assert session_store.fingerprint(cfg1) != session_store.fingerprint(cfg2)

    def test_token_change_changes_fingerprint(self):
        cfg1 = RemoteConfig(password_hash="h", secret_path="s", token="t1")
        cfg2 = RemoteConfig(password_hash="h", secret_path="s", token="t2")
        assert session_store.fingerprint(cfg1) != session_store.fingerprint(cfg2)


class TestHashToken:
    def test_deterministic(self):
        assert session_store.hash_token("abc") == session_store.hash_token("abc")

    def test_different_tokens_different_hashes(self):
        assert session_store.hash_token("abc") != session_store.hash_token("xyz")

    def test_raw_token_not_recoverable_from_hash(self):
        assert "abc" not in session_store.hash_token("abc")


class TestLoadSave:
    def test_missing_file_returns_empty(self):
        assert session_store.load("fp") == {}

    def test_round_trips_matching_fingerprint(self):
        session_store.save("fp", {"h1": 123.0, "h2": 456.0})
        assert session_store.load("fp") == {"h1": 123.0, "h2": 456.0}

    def test_fingerprint_mismatch_returns_empty(self):
        session_store.save("fp-old", {"h1": 123.0})
        assert session_store.load("fp-new") == {}

    def test_corrupt_json_returns_empty(self):
        session_store.path().parent.mkdir(parents=True, exist_ok=True)
        session_store.path().write_text("not json{{{", encoding="utf-8")
        assert session_store.load("fp") == {}

    def test_non_dict_json_returns_empty(self):
        session_store.path().parent.mkdir(parents=True, exist_ok=True)
        session_store.path().write_text("[1, 2, 3]", encoding="utf-8")
        assert session_store.load("fp") == {}

    def test_malformed_sessions_field_returns_empty(self):
        session_store.path().parent.mkdir(parents=True, exist_ok=True)
        session_store.path().write_text(
            '{"fingerprint": "fp", "sessions": "not-a-dict"}', encoding="utf-8"
        )
        assert session_store.load("fp") == {}

    def test_save_overwrites_previous_contents(self):
        session_store.save("fp", {"h1": 1.0})
        session_store.save("fp", {"h2": 2.0})
        assert session_store.load("fp") == {"h2": 2.0}

    def test_save_creates_parent_dir(self, tmp_path, monkeypatch):
        nested = tmp_path / "nested" / "dir" / "sessions.json"
        monkeypatch.setattr(session_store, "_PATH", nested)
        session_store.save("fp", {"h1": 1.0})
        assert nested.exists()

    if sys.platform != "win32":

        def test_file_permissions_are_owner_only(self):
            session_store.save("fp", {"h1": 1.0})
            mode = stat.S_IMODE(session_store.path().stat().st_mode)
            assert mode == 0o600


class TestClear:
    def test_clear_removes_the_file(self):
        session_store.save("fp", {"h1": 1.0})
        assert session_store.path().exists()
        session_store.clear()
        assert not session_store.path().exists()

    def test_clear_missing_file_does_not_raise(self):
        session_store.clear()  # never saved anything — must be a no-op
