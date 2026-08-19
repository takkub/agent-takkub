"""Prod state stays inside DATA_HOME for every provider that can (2026-08-19).

The pairing that matters is spawn-side vs read-side: `pane_env` exports the
isolated home into the pane, and `codex_helper`/`opencode_helper` must resolve
to the SAME directory. When those two disagree the Remote mirror reads a
location nothing writes to and the phone shows a blank chat with no error —
the failure this whole release is about.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_takkub import codex_helper, config, opencode_helper, pane_env, provider_bootstrap


@pytest.fixture
def installed_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make config look like an installed build with DATA_HOME under tmp."""
    data_home = tmp_path / "agent-takkub-home"
    data_home.mkdir()
    monkeypatch.setattr(config, "DATA_HOME", data_home)
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "repo")
    return data_home


class TestProviderHomeEnv:
    def test_codex_and_opencode_move_into_data_home(self, installed_home: Path):
        codex = config.provider_home_env("codex")
        assert codex == {"CODEX_HOME": str(installed_home / "codex-home")}

        opencode = config.provider_home_env("opencode")
        assert opencode == {
            "XDG_DATA_HOME": str(installed_home / "opencode-home" / "data"),
            "XDG_CONFIG_HOME": str(installed_home / "opencode-home" / "config"),
        }

    def test_dev_checkout_keeps_the_os_wide_homes(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(config, "DATA_HOME", config.REPO_ROOT)
        assert config.provider_home_env("codex") == {}
        assert config.provider_home_env("opencode") == {}

    def test_provider_without_a_knob_yields_nothing(self, installed_home: Path):
        # gemini/kimi/cursor have no directory env var — the gap is declared,
        # not silently papered over with an env var they would ignore.
        for provider in config.PROVIDER_ISOLATION_GAPS:
            assert config.provider_home_env(provider) == {}

    def test_env_injection_overrides_an_inherited_value(self, installed_home: Path):
        env = {"CODEX_HOME": "/somewhere/else"}
        pane_env.inject_provider_home_env(env, "codex")
        assert env["CODEX_HOME"] == str(installed_home / "codex-home")

    def test_env_injection_is_a_noop_for_claude(self, installed_home: Path):
        env: dict[str, str] = {}
        pane_env.inject_provider_home_env(env, "claude")
        assert env == {}


class TestReadSideAgreesWithSpawnSide:
    def test_codex_sessions_root_follows_the_isolated_home(
        self, installed_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # An inherited CODEX_HOME must NOT win: panes get the isolated value
        # assigned, so a reader honouring the inherited one would look in a
        # directory no pane ever writes to.
        monkeypatch.setenv("CODEX_HOME", str(installed_home / "not-this-one"))
        assert codex_helper.codex_home() == installed_home / "codex-home"
        assert codex_helper.codex_sessions_root() == installed_home / "codex-home" / "sessions"

    def test_codex_sessions_root_honours_env_on_a_dev_checkout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(config, "DATA_HOME", config.REPO_ROOT)
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "custom"))
        assert codex_helper.codex_sessions_root() == tmp_path / "custom" / "sessions"

    def test_opencode_db_prefers_the_isolated_data_home(
        self, installed_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("OPENCODE_DB_PATH", raising=False)
        isolated = installed_home / "opencode-home" / "data" / "opencode" / "opencode.db"
        isolated.parent.mkdir(parents=True)
        isolated.write_bytes(b"")
        assert opencode_helper.opencode_db_path() == isolated


class TestSeeding:
    def _write_rollout(self, root: Path, name: str, first_user: str) -> Path:
        path = root / "sessions" / "2026" / "08" / "18" / f"rollout-{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": first_user},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def test_codex_seed_copies_auth_and_lead_sessions_only(
        self, installed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        legacy = tmp_path / "dot-codex"
        legacy.mkdir()
        (legacy / "auth.json").write_text("{}", encoding="utf-8")
        (legacy / "config.toml").write_text("x = 1", encoding="utf-8")
        (legacy / "log").mkdir()  # bulk state: must NOT be copied
        (legacy / "log" / "big.log").write_text("noise", encoding="utf-8")
        self._write_rollout(legacy, "lead", "ทดสอบ")
        self._write_rollout(legacy, "mate", "[ROLE: backend] do the thing")
        monkeypatch.setattr(provider_bootstrap, "_legacy_codex_home", lambda: legacy)

        assert provider_bootstrap.ensure_provider_home("codex") is True

        dest = installed_home / "codex-home"
        assert (dest / "auth.json").is_file()
        assert (dest / "config.toml").is_file()
        assert not (dest / "log").exists()
        copied = sorted(p.name for p in dest.rglob("rollout-*.jsonl"))
        assert copied == ["rollout-lead.jsonl"]
        # Codex's date layout is preserved — remote/notify.py's day-directory
        # walk depends on it.
        assert (dest / "sessions" / "2026" / "08" / "18" / "rollout-lead.jsonl").is_file()

    def test_seed_runs_once_and_leaves_the_source_untouched(
        self, installed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        legacy = tmp_path / "dot-codex"
        legacy.mkdir()
        (legacy / "auth.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(provider_bootstrap, "_legacy_codex_home", lambda: legacy)

        assert provider_bootstrap.ensure_provider_home("codex") is True
        assert provider_bootstrap.ensure_provider_home("codex") is False
        assert (legacy / "auth.json").is_file()  # copy, never move

    def test_seed_is_a_noop_without_isolation(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(config, "DATA_HOME", config.REPO_ROOT)
        assert provider_bootstrap.ensure_provider_home("codex") is False
        assert provider_bootstrap.ensure_provider_home("gemini") is False
