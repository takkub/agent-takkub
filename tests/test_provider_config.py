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
    """Point the global `config_path()` at a per-test temp file, and the
    per-project root at the temp dir, so the real path-resolution logic runs
    (rather than stubbing config_path away — which would hide the project arg)."""
    fake = tmp_path / "role-providers.json"
    monkeypatch.setattr(provider_config, "_CONFIG_PATH", fake)
    monkeypatch.setattr(provider_config, "_BASE_DIR", tmp_path)
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


class TestEffectiveProviderFor:
    """`effective_provider_for` degrades an unavailable codex/gemini role to
    claude (toggled off OR CLI not installed) while `provider_for` keeps
    reporting the static identity."""

    def test_claude_role_unaffected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # claude is always available — no availability probe needed.
        assert provider_config.effective_provider_for("frontend") == "claude"
        assert provider_config.effective_provider_for("lead") == "claude"

    def test_codex_available_stays_codex(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(provider_config, "_provider_available", lambda p: True)
        assert provider_config.effective_provider_for("codex") == "codex"

    def test_codex_unavailable_degrades_to_claude(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(provider_config, "_provider_available", lambda p: False)
        # role identity (provider_for) is still codex...
        assert provider_config.provider_for("codex") == "codex"
        # ...but the effective engine is claude (the substitute).
        assert provider_config.effective_provider_for("codex") == "claude"

    def test_gemini_unavailable_degrades_to_claude(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(provider_config, "_provider_available", lambda p: False)
        assert provider_config.effective_provider_for("gemini") == "claude"

    def test_remapped_role_also_degrades(
        self, monkeypatch: pytest.MonkeyPatch, redirect_config_path: Path
    ) -> None:
        # A user-remapped role (backend→codex) substitutes too when codex is off.
        redirect_config_path.write_text('{"backend": "codex"}', encoding="utf-8")
        monkeypatch.setattr(provider_config, "_provider_available", lambda p: False)
        assert provider_config.effective_provider_for("backend") == "claude"

    def test_disabled_toggle_makes_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # _provider_available consults provider_state.is_disabled.
        import agent_takkub.provider_state as ps

        monkeypatch.setattr(ps, "is_disabled", lambda prov: prov == "codex")
        assert provider_config._provider_available("codex") is False
        # gemini not disabled here — availability then depends on the CLI probe.

    def test_not_installed_makes_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import agent_takkub.codex_helper as ch
        import agent_takkub.provider_state as ps

        monkeypatch.setattr(ps, "is_disabled", lambda prov: False)
        monkeypatch.setattr(ch, "find_codex_executable", lambda: None)
        assert provider_config._provider_available("codex") is False


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
        redirect_config_path.write_text('{"backend": "gemini", "qa": "codex"}', encoding="utf-8")
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


class TestRoleProviderMap:
    def test_maps_each_role_to_its_cli(self, redirect_config_path: Path) -> None:
        redirect_config_path.write_text('{"backend": "codex"}', encoding="utf-8")
        m = provider_config.role_provider_map(["frontend", "backend", "codex", "gemini"])
        assert m == {
            "frontend": "claude",  # default
            "backend": "codex",  # override
            "codex": "codex",  # forced identity
            "gemini": "gemini",  # forced identity
        }


class TestSaveRoleOverrides:
    def test_drops_claude_defaults_and_forced_roles(self, redirect_config_path: Path) -> None:
        provider_config.save_role_overrides(
            {
                "frontend": "claude",  # default → dropped
                "backend": "codex",  # real override → kept
                "qa": "gemini",  # real override → kept
                "lead": "codex",  # forced → dropped
                "codex": "codex",  # forced → dropped
                "gemini": "gemini",  # forced → dropped
            }
        )
        assert provider_config.load_providers() == {"backend": "codex", "qa": "gemini"}

    def test_drops_invalid_providers(self, redirect_config_path: Path) -> None:
        provider_config.save_role_overrides({"backend": "codex", "ml": "openrouter"})
        assert provider_config.load_providers() == {"backend": "codex"}

    def test_empty_or_none_writes_empty(self, redirect_config_path: Path) -> None:
        provider_config.save_role_overrides({})
        assert provider_config.load_providers() == {}
        provider_config.save_role_overrides(None)  # type: ignore[arg-type]
        assert provider_config.load_providers() == {}

    def test_replaces_existing_file(self, redirect_config_path: Path) -> None:
        provider_config.save_providers({"backend": "codex", "qa": "gemini"})
        # New save with only backend → qa override must be gone (full replace).
        provider_config.save_role_overrides({"backend": "gemini"})
        assert provider_config.load_providers() == {"backend": "gemini"}

    def test_scope_preserves_overrides_outside_scope(self, redirect_config_path: Path) -> None:
        """Codex High #1 — a page that only renders controls for a subset of
        roles (e.g. Settings' Providers & Roles view, which excludes custom
        roles) must not delete overrides for roles it never showed."""
        provider_config.save_providers({"custom-role": "codex", "backend": "codex"})
        # Only "backend" is in scope this call — "custom-role" is untouched
        # on disk and must survive even though it's absent from `mapping`.
        provider_config.save_role_overrides({"backend": "gemini"}, scope=["backend", "qa"])
        assert provider_config.load_providers() == {
            "custom-role": "codex",
            "backend": "gemini",
        }

    def test_scope_still_drops_claude_default_within_scope(
        self, redirect_config_path: Path
    ) -> None:
        provider_config.save_providers({"backend": "codex"})
        provider_config.save_role_overrides({"backend": "claude"}, scope=["backend"])
        assert provider_config.load_providers() == {}


class TestPerProject:
    def test_projects_keep_independent_mappings(self, redirect_config_path: Path) -> None:
        provider_config.save_role_overrides({"backend": "codex"}, project="proj-a")
        provider_config.save_role_overrides({"backend": "gemini"}, project="proj-b")
        assert provider_config.provider_for("backend", project="proj-a") == "codex"
        assert provider_config.provider_for("backend", project="proj-b") == "gemini"

    def test_unsaved_project_inherits_global(self, redirect_config_path: Path) -> None:
        # Global override present; a project with no file inherits it.
        provider_config.save_role_overrides({"backend": "codex"})
        assert provider_config.provider_for("backend", project="fresh") == "codex"

    def test_per_project_does_not_leak_to_global(self, redirect_config_path: Path) -> None:
        provider_config.save_role_overrides({"backend": "codex"}, project="proj-a")
        # Global stays default (claude) — the per-project save didn't touch it.
        assert provider_config.provider_for("backend") == "claude"
