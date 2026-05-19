"""Tests for `provider_config` — the per-role CLI provider mapping
that decides whether `takkub assign --role <X>` spawns a claude or
codex pane. Hard rules:
  - `lead` is always claude (cockpit plumbing demands it).
  - `codex` role is always codex.
  - Everything else: user override via JSON, default claude.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_takkub import provider_config


@pytest.fixture(autouse=True)
def redirect_config_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point every `config_path()` call at a per-test temp file."""
    fake = tmp_path / "role-providers.json"
    monkeypatch.setattr(provider_config, "config_path", lambda: fake)
    return fake


class TestProviderFor:
    def test_lead_is_always_claude(self, redirect_config_path: Path) -> None:
        # Even if user config says otherwise, Lead stays claude — too
        # much claude-specific plumbing depends on it.
        redirect_config_path.write_text('{"lead": "codex"}', encoding="utf-8")
        assert provider_config.provider_for("lead") == "claude"

    def test_codex_role_is_always_codex(self, redirect_config_path: Path) -> None:
        # User mapping a "codex" key to "claude" would be nonsensical;
        # the role's whole point is codex.
        redirect_config_path.write_text('{"codex": "claude"}', encoding="utf-8")
        assert provider_config.provider_for("codex") == "codex"

    def test_default_is_claude(self) -> None:
        # No config file yet → load_providers creates an empty one,
        # provider_for falls back to claude.
        assert provider_config.provider_for("frontend") == "claude"
        assert provider_config.provider_for("backend") == "claude"

    def test_user_override_routes_to_codex(self, redirect_config_path: Path) -> None:
        redirect_config_path.write_text('{"backend": "codex", "qa": "codex"}', encoding="utf-8")
        assert provider_config.provider_for("backend") == "codex"
        assert provider_config.provider_for("qa") == "codex"
        # Roles not in the map still default to claude
        assert provider_config.provider_for("frontend") == "claude"

    def test_case_and_whitespace_insensitive(self, redirect_config_path: Path) -> None:
        redirect_config_path.write_text('{"BACKEND": "CODEX"}', encoding="utf-8")
        assert provider_config.provider_for("backend") == "codex"
        assert provider_config.provider_for("  Backend  ") == "codex"

    def test_gemini_role_is_always_gemini(self, redirect_config_path: Path) -> None:
        # User mapping a "gemini" key to "claude" would be nonsensical;
        # the role's whole point is gemini.
        redirect_config_path.write_text('{"gemini": "claude"}', encoding="utf-8")
        assert provider_config.provider_for("gemini") == "gemini"

    def test_user_override_routes_to_gemini(self, redirect_config_path: Path) -> None:
        redirect_config_path.write_text('{"backend": "gemini", "qa": "gemini"}', encoding="utf-8")
        assert provider_config.provider_for("backend") == "gemini"
        assert provider_config.provider_for("qa") == "gemini"


class TestLoadProviders:
    def test_creates_empty_file_when_missing(self, redirect_config_path: Path) -> None:
        assert not redirect_config_path.exists()
        loaded = provider_config.load_providers()
        assert loaded == {}
        assert redirect_config_path.exists()
        # File should be valid JSON object
        assert json.loads(redirect_config_path.read_text(encoding="utf-8")) == {}

    def test_invalid_json_returns_empty(self, redirect_config_path: Path) -> None:
        redirect_config_path.write_text("{not valid json", encoding="utf-8")
        assert provider_config.load_providers() == {}

    def test_non_dict_top_level_returns_empty(self, redirect_config_path: Path) -> None:
        redirect_config_path.write_text('["backend", "codex"]', encoding="utf-8")
        assert provider_config.load_providers() == {}

    def test_drops_entries_with_unknown_provider(self, redirect_config_path: Path) -> None:
        # A typo or made-up provider shouldn't silently route a role
        # to nothing — drop it so we fall back to the claude default.
        redirect_config_path.write_text('{"backend": "codex", "qa": "ollama"}', encoding="utf-8")
        loaded = provider_config.load_providers()
        assert loaded == {"backend": "codex"}

    def test_accepts_gemini_provider(self, redirect_config_path: Path) -> None:
        # gemini joins claude/codex as a recognised provider — must
        # survive the sanitizer instead of being dropped.
        redirect_config_path.write_text(
            '{"backend": "gemini", "qa": "codex"}', encoding="utf-8"
        )
        loaded = provider_config.load_providers()
        assert loaded == {"backend": "gemini", "qa": "codex"}


class TestSaveProviders:
    def test_writes_and_round_trips(self, redirect_config_path: Path) -> None:
        provider_config.save_providers({"backend": "codex", "qa": "codex"})
        text = redirect_config_path.read_text(encoding="utf-8")
        # Pretty-printed JSON (indent=2) for hand-editing
        assert '"backend": "codex"' in text
        # Reload and confirm round-trip
        assert provider_config.load_providers() == {
            "backend": "codex",
            "qa": "codex",
        }

    def test_save_drops_invalid_providers(self, redirect_config_path: Path) -> None:
        # Even on save, sanitize so the file stays internally consistent
        # if the caller passes typos.
        provider_config.save_providers({"backend": "codex", "ml": "openrouter"})
        assert provider_config.load_providers() == {"backend": "codex"}
