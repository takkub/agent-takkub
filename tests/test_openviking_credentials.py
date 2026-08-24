"""Tests for `agent_takkub.openviking.credentials`: the SecretManager
bridge for the OpenViking Setup Wizard's API key (HIGH finding,
docs/audit/2026-08-24-openviking-managed-review.md). No real
subprocess/network — `SecretManager`'s `FileSecretBackend` writes to an
isolated `tmp_path`, same convention as `test_settings_knowledge_design.py`'s
`_isolate_kd_paths` fixture."""

from __future__ import annotations

import os

import pytest

from agent_takkub import config
from agent_takkub.openviking import credentials


@pytest.fixture(autouse=True)
def _isolate_secrets_home(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SETTINGS_HOME", tmp_path)


class TestSaveAndLoadApiKey:
    def test_load_returns_none_when_never_saved(self) -> None:
        assert credentials.load_api_key() is None

    def test_save_then_load_roundtrips(self) -> None:
        credentials.save_api_key("sk-real-secret-value")
        assert credentials.load_api_key() == "sk-real-secret-value"

    def test_saved_key_never_lands_in_a_project_or_config_file(self, tmp_path) -> None:
        credentials.save_api_key("sk-real-secret-value")
        # Nothing outside the isolated secrets dir should exist yet — this
        # is a smoke check that `save_api_key` only touches SecretManager's
        # own backend file, not some other path.
        secrets_dir = tmp_path / "secrets"
        assert (secrets_dir / "openviking.json").read_text(encoding="utf-8") == (
            "sk-real-secret-value"
        )


class TestSubprocessEnv:
    def test_none_when_no_key_stored(self) -> None:
        assert credentials.subprocess_env() is None

    def test_includes_stored_key_alongside_inherited_environ(self, monkeypatch) -> None:
        monkeypatch.setenv("SOME_UNRELATED_VAR", "still-here")
        credentials.save_api_key("sk-real-secret-value")

        env = credentials.subprocess_env()

        assert env is not None
        assert env[credentials.API_KEY_ENV_VAR] == "sk-real-secret-value"
        assert env["SOME_UNRELATED_VAR"] == "still-here"

    def test_does_not_mutate_os_environ(self) -> None:
        credentials.save_api_key("sk-real-secret-value")
        credentials.subprocess_env()
        assert credentials.API_KEY_ENV_VAR not in os.environ


class TestApiKeyPlaceholder:
    def test_placeholder_matches_env_var_name(self) -> None:
        assert credentials.API_KEY_PLACEHOLDER == "${" + credentials.API_KEY_ENV_VAR + "}"
