"""Tests for pane_env.inject_provider_no_autoupdate_env (#313 Part 1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import agent_takkub.pane_env as pane_env


class TestKnownProviders:
    def test_claude_sets_disable_autoupdater(self) -> None:
        env: dict[str, str] = {}
        pane_env.inject_provider_no_autoupdate_env(env, "claude")
        assert env == {"DISABLE_AUTOUPDATER": "1"}

    def test_gemini_sets_agy_env(self) -> None:
        env: dict[str, str] = {}
        pane_env.inject_provider_no_autoupdate_env(env, "gemini")
        assert env == {"AGY_CLI_DISABLE_AUTO_UPDATE": "true"}

    def test_kimi_sets_both_aliases(self) -> None:
        env: dict[str, str] = {}
        pane_env.inject_provider_no_autoupdate_env(env, "kimi")
        assert env == {"KIMI_CLI_NO_AUTO_UPDATE": "1", "KIMI_CODE_NO_AUTO_UPDATE": "1"}

    def test_setdefault_never_overrides_existing_value(self) -> None:
        env = {"DISABLE_AUTOUPDATER": "0"}
        pane_env.inject_provider_no_autoupdate_env(env, "claude")
        assert env["DISABLE_AUTOUPDATER"] == "0"

    def test_unknown_provider_gap_is_a_noop(self) -> None:
        env: dict[str, str] = {}
        pane_env.inject_provider_no_autoupdate_env(env, "codex")
        assert env == {}
        pane_env.inject_provider_no_autoupdate_env(env, "cursor")
        assert env == {}

    def test_gaps_table_documents_codex_and_cursor(self) -> None:
        assert "codex" in pane_env.NO_AUTOUPDATE_KNOB_GAPS
        assert "cursor" in pane_env.NO_AUTOUPDATE_KNOB_GAPS


class TestOpencodeConfigFile:
    def test_no_isolated_home_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Dev checkout: config.provider_home_env('opencode') returns {} by
        design — must never fall back to touching the user's real
        ~/.config/opencode/opencode.json."""
        from agent_takkub import config

        monkeypatch.setattr(config, "provider_home_env", lambda name: {})
        env: dict[str, str] = {}
        pane_env.inject_provider_no_autoupdate_env(env, "opencode")
        assert env == {}

    def test_writes_autoupdate_false_when_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agent_takkub import config

        monkeypatch.setattr(
            config, "provider_home_env", lambda name: {"XDG_CONFIG_HOME": str(tmp_path)}
        )
        pane_env.inject_provider_no_autoupdate_env({}, "opencode")
        cfg = tmp_path / "opencode" / "opencode.json"
        assert json.loads(cfg.read_text(encoding="utf-8")) == {"autoupdate": False}

    def test_merges_without_clobbering_existing_keys(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agent_takkub import config

        monkeypatch.setattr(
            config, "provider_home_env", lambda name: {"XDG_CONFIG_HOME": str(tmp_path)}
        )
        cfg_dir = tmp_path / "opencode"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "opencode.json").write_text(
            json.dumps({"$schema": "https://opencode.ai/config.json", "theme": "dark"}),
            encoding="utf-8",
        )
        pane_env.inject_provider_no_autoupdate_env({}, "opencode")
        data = json.loads((cfg_dir / "opencode.json").read_text(encoding="utf-8"))
        assert data == {
            "$schema": "https://opencode.ai/config.json",
            "theme": "dark",
            "autoupdate": False,
        }

    def test_idempotent_second_call_skips_write(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agent_takkub import config

        monkeypatch.setattr(
            config, "provider_home_env", lambda name: {"XDG_CONFIG_HOME": str(tmp_path)}
        )
        pane_env.inject_provider_no_autoupdate_env({}, "opencode")
        cfg = tmp_path / "opencode" / "opencode.json"
        first_mtime = cfg.stat().st_mtime_ns
        pane_env.inject_provider_no_autoupdate_env({}, "opencode")
        assert cfg.stat().st_mtime_ns == first_mtime

    def test_corrupt_json_recovers_instead_of_raising(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agent_takkub import config

        monkeypatch.setattr(
            config, "provider_home_env", lambda name: {"XDG_CONFIG_HOME": str(tmp_path)}
        )
        cfg_dir = tmp_path / "opencode"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "opencode.json").write_text("{not json", encoding="utf-8")
        pane_env.inject_provider_no_autoupdate_env({}, "opencode")  # must not raise
        data = json.loads((cfg_dir / "opencode.json").read_text(encoding="utf-8"))
        assert data == {"autoupdate": False}
