"""Tests for `provider_config` — the per-role CLI provider mapping
that decides whether `takkub assign --role <X>` spawns a claude or
codex pane. Hard rules:
  - `codex` role is always codex.
  - `gemini` role is always gemini.
  - Everything else (including `lead` since issue #101's degraded-mode
    unlock): user override via JSON, default claude.
"""

from __future__ import annotations

import json
from dataclasses import replace
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
    def test_lead_defaults_to_claude(self) -> None:
        # No override on disk → default stays claude (issue #101: unlock is
        # opt-in, not a default change).
        assert provider_config.provider_for("lead") == "claude"

    def test_lead_is_overridable(self, redirect_config_path: Path) -> None:
        # Issue #101 degraded-mode unlock: lead is no longer in
        # _FORCED_PROVIDER, so a user override now takes effect like any
        # other role.
        redirect_config_path.write_text('{"lead": "codex"}', encoding="utf-8")
        assert provider_config.provider_for("lead") == "codex"

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

    def test_shard_suffix_uses_base_role_provider(self, redirect_config_path: Path) -> None:
        redirect_config_path.write_text('{"qa": "gemini"}', encoding="utf-8")
        assert provider_config.provider_for("codex#2") == "codex"
        assert provider_config.provider_for("gemini#3") == "gemini"
        assert provider_config.provider_for("qa#2") == provider_config.provider_for("qa")
        assert provider_config.provider_for("frontend") == "claude"


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

    def test_shard_suffix_uses_base_role_effective_provider(
        self, monkeypatch: pytest.MonkeyPatch, redirect_config_path: Path
    ) -> None:
        redirect_config_path.write_text('{"qa": "gemini"}', encoding="utf-8")
        monkeypatch.setattr(provider_config, "_provider_available", lambda p: True)
        assert provider_config.effective_provider_for("codex#2") == "codex"
        assert provider_config.effective_provider_for("gemini#3") == "gemini"
        assert provider_config.effective_provider_for(
            "qa#2"
        ) == provider_config.effective_provider_for("qa")
        assert provider_config.effective_provider_for("frontend") == "claude"

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
                "lead": "codex",  # #101: no longer forced → real override, kept
                "codex": "codex",  # forced → dropped
                "gemini": "gemini",  # forced → dropped
            }
        )
        assert provider_config.load_providers() == {
            "backend": "codex",
            "qa": "gemini",
            "lead": "codex",
        }

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


class TestLeadCapabilityGap:
    """Issue #101: Lead degradation must be visible, never silent. This is
    the function the Settings UI badge and the remote API responses consult
    to tell the user which claude-only features are gone."""

    def test_claude_lead_has_no_gap(self) -> None:
        assert provider_config.lead_capability_gap() is None

    def test_codex_lead_reports_provider_and_missing_features(
        self, redirect_config_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        redirect_config_path.write_text('{"lead": "codex"}', encoding="utf-8")
        monkeypatch.setattr(provider_config, "_provider_available", lambda p: True)
        gap = provider_config.lead_capability_gap()
        assert gap is not None
        provider, missing = gap
        assert provider == "codex"
        assert missing  # codex_spec has every supports_* flag False
        assert any("mirror" in m for m in missing)

    def test_unavailable_codex_lead_degrades_to_claude_no_gap(
        self, redirect_config_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same substitution rule as any other role: an unusable codex Lead
        # silently falls back to claude at spawn time, so there's no
        # capability gap to report — the *effective* engine is claude.
        redirect_config_path.write_text('{"lead": "codex"}', encoding="utf-8")
        monkeypatch.setattr(provider_config, "_provider_available", lambda p: False)
        assert provider_config.lead_capability_gap() is None


class TestLeadCapabilityGapForProvider:
    """Pure sibling of lead_capability_gap — no disk/role lookup, so the
    Settings UI can warn on an unsaved combo selection (#101 critic R3
    blocker #1)."""

    def test_claude_has_no_gap(self) -> None:
        assert provider_config.lead_capability_gap_for_provider("claude") == []

    def test_codex_reports_missing_features(self) -> None:
        missing = provider_config.lead_capability_gap_for_provider("codex")
        assert missing
        assert any("mirror" in m for m in missing)

    def test_unknown_provider_reports_every_label(self) -> None:
        missing = provider_config.lead_capability_gap_for_provider("nonexistent")
        assert missing == [label for _, label in provider_config._LEAD_CAPABILITY_LABELS]


class TestAssignModelOverrideValidation:
    def test_supported_effective_provider_accepts_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            provider_config, "effective_provider_for", lambda *_args, **_kwargs: "claude"
        )
        assert provider_config.assign_model_override_error("qa", "haiku") is None

    def test_provider_without_model_flag_returns_clear_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub.provider_spec import PROVIDER_REGISTRY

        monkeypatch.setattr(
            provider_config, "effective_provider_for", lambda *_args, **_kwargs: "cursor"
        )
        monkeypatch.setitem(
            PROVIDER_REGISTRY,
            "cursor",
            replace(PROVIDER_REGISTRY["cursor"], model_flag=None),
        )

        error = provider_config.assign_model_override_error("qa", "cheap-scan")

        assert error is not None
        assert "cursor" in error
        assert "model_flag" in error


class TestAssignEffortOverrideValidation:
    """Issue #323 — `--effort` validated against the provider that will
    actually spawn, mirroring `TestAssignModelOverrideValidation`."""

    def test_empty_effort_never_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(provider_config, "effective_provider_for", lambda *_a, **_kw: "claude")
        assert provider_config.assign_effort_override_error("qa", "") is None
        assert provider_config.assign_effort_override_error("qa", None) is None

    @pytest.mark.parametrize("effort", ["low", "medium", "high"])
    def test_supported_provider_accepts_valid_level(
        self, monkeypatch: pytest.MonkeyPatch, effort: str
    ) -> None:
        monkeypatch.setattr(provider_config, "effective_provider_for", lambda *_a, **_kw: "codex")
        assert provider_config.assign_effort_override_error("backend", effort) is None

    def test_claude_accepts_xhigh_and_max_beyond_the_cli_common_baseline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # claude_spec.effort_levels includes xhigh/max even though the
        # `takkub assign --effort` CLI flag only exposes low/medium/high —
        # a level string reaching this validator from another caller must
        # still be checked against the real provider list, not a narrower
        # CLI-only vocabulary.
        monkeypatch.setattr(provider_config, "effective_provider_for", lambda *_a, **_kw: "claude")
        assert provider_config.assign_effort_override_error("backend", "xhigh") is None

    def test_level_not_accepted_by_provider_is_blocked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # codex's effort_levels is exactly ("low", "medium", "high") — no xhigh.
        monkeypatch.setattr(provider_config, "effective_provider_for", lambda *_a, **_kw: "codex")
        error = provider_config.assign_effort_override_error("backend", "xhigh")
        assert error is not None
        assert "codex" in error
        assert "xhigh" in error

    def test_provider_without_effort_flag_degrades_silently(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # opencode/kimi/cursor have no CLI knob at all (#103 gap) — issue
        # #323's acceptance criteria requires this NOT be a hard error.
        monkeypatch.setattr(
            provider_config, "effective_provider_for", lambda *_a, **_kw: "opencode"
        )
        assert provider_config.assign_effort_override_error("backend", "low") is None

    def test_gemini_accepts_its_documented_effort_levels(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #323 follow-up: agy's #125 silent-model-swap regression is fixed
        # upstream (agy 1.1.10+) — gemini is validated like claude/codex now.
        monkeypatch.setattr(provider_config, "effective_provider_for", lambda *_a, **_kw: "gemini")
        assert provider_config.assign_effort_override_error("backend", "low") is None
        assert provider_config.assign_effort_override_error("backend", "high") is None

    def test_gemini_rejects_a_level_it_does_not_accept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # gemini_spec.effort_levels is exactly ("low", "medium", "high") — no xhigh.
        monkeypatch.setattr(provider_config, "effective_provider_for", lambda *_a, **_kw: "gemini")
        error = provider_config.assign_effort_override_error("backend", "xhigh")
        assert error is not None
        assert "gemini" in error
        assert "xhigh" in error

    def test_provider_override_wins_over_effective_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same "what will actually spawn" rule as assign_model_override_error
        # (issue #270) — a validated --provider on the same assign is what
        # gets checked, not the role's static config.
        monkeypatch.setattr(provider_config, "effective_provider_for", lambda *_a, **_kw: "gemini")
        error = provider_config.assign_effort_override_error(
            "backend", "xhigh", provider_override="codex"
        )
        assert error is not None
        assert "codex" in error


class TestAssignModelOverrideProviderMismatch:
    """Issue #127: a --model id that unambiguously belongs to a DIFFERENT
    provider's naming scheme (e.g. claude-* sent to a role that maps to
    gemini/agy) must be blocked at assign time instead of silently falling
    back to that provider's own default."""

    @pytest.mark.parametrize(
        "provider,model",
        [
            ("gemini", "claude-haiku-4-5"),
            ("gemini", "haiku"),
            ("claude", "gemini-3.1-pro"),
            ("claude", "gpt-5"),
            ("codex", "claude-sonnet-4-5"),
            ("codex", "gemini-3.1-pro-high"),
            ("kimi", "claude-opus-4"),
            ("gemini", "k2.5"),
        ],
    )
    def test_wrong_provider_model_is_blocked(
        self, monkeypatch: pytest.MonkeyPatch, provider: str, model: str
    ) -> None:
        monkeypatch.setattr(provider_config, "effective_provider_for", lambda *_a, **_kw: provider)
        error = provider_config.assign_model_override_error("qa", model)
        assert error is not None
        assert provider in error
        assert model in error

    @pytest.mark.parametrize(
        "provider,model",
        [
            ("claude", "claude-sonnet-4-5"),
            ("claude", "opus"),
            ("codex", "gpt-5"),
            ("codex", "o3-mini"),
            ("gemini", "gemini-3.1-pro"),
            ("kimi", "k2.5"),
            ("kimi", "kimi-k2"),
        ],
    )
    def test_own_provider_model_is_never_blocked(
        self, monkeypatch: pytest.MonkeyPatch, provider: str, model: str
    ) -> None:
        monkeypatch.setattr(provider_config, "effective_provider_for", lambda *_a, **_kw: provider)
        assert provider_config.assign_model_override_error("qa", model) is None

    @pytest.mark.parametrize("provider", ["opencode", "cursor"])
    def test_router_providers_never_blocked_even_for_other_providers_ids(
        self, monkeypatch: pytest.MonkeyPatch, provider: str
    ) -> None:
        # opencode/cursor front many backends by design — a claude-shaped id
        # can be a genuinely valid selection for them.
        monkeypatch.setattr(provider_config, "effective_provider_for", lambda *_a, **_kw: provider)
        assert provider_config.assign_model_override_error("qa", "claude-sonnet-4-5") is None


class TestAssignModelOverrideWarning:
    """Issue #127: an unrecognized-but-not-provably-wrong model id should
    warn, not block — new models ship faster than this table can track."""

    def test_unrecognized_model_for_known_provider_warns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(provider_config, "effective_provider_for", lambda *_a, **_kw: "claude")
        warning = provider_config.assign_model_override_warning("qa", "totally-new-model-9")
        assert warning is not None
        assert "claude" in warning
        assert "qa" in warning
        # The blocking check must NOT fire for the same input.
        assert provider_config.assign_model_override_error("qa", "totally-new-model-9") is None

    def test_own_provider_model_does_not_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(provider_config, "effective_provider_for", lambda *_a, **_kw: "claude")
        assert provider_config.assign_model_override_warning("qa", "claude-sonnet-4-5") is None

    def test_wrong_provider_model_does_not_also_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # It's already blocked by assign_model_override_error; the warning
        # path is for the softer "unknown, not provably wrong" case only.
        monkeypatch.setattr(provider_config, "effective_provider_for", lambda *_a, **_kw: "gemini")
        assert provider_config.assign_model_override_warning("qa", "claude-haiku-4-5") is None

    @pytest.mark.parametrize("provider", ["opencode", "cursor"])
    def test_router_providers_never_warn(
        self, monkeypatch: pytest.MonkeyPatch, provider: str
    ) -> None:
        monkeypatch.setattr(provider_config, "effective_provider_for", lambda *_a, **_kw: provider)
        assert provider_config.assign_model_override_warning("qa", "anything-goes-here") is None

    def test_empty_model_never_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(provider_config, "effective_provider_for", lambda *_a, **_kw: "claude")
        assert provider_config.assign_model_override_warning("qa", "") is None
        assert provider_config.assign_model_override_warning("qa", None) is None

    def test_provider_without_model_flag_never_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agent_takkub.provider_spec import PROVIDER_REGISTRY

        monkeypatch.setattr(provider_config, "effective_provider_for", lambda *_a, **_kw: "cursor")
        monkeypatch.setitem(
            PROVIDER_REGISTRY,
            "cursor",
            replace(PROVIDER_REGISTRY["cursor"], model_flag=None),
        )
        assert provider_config.assign_model_override_warning("qa", "cheap-scan") is None


class TestAssignProviderOverrideValidation:
    """Issue #270: `--provider` is a per-assign escape hatch for a role
    whose configured provider is stuck/broken — Lead didn't have one before,
    and hand-editing role-providers.json needs a cockpit restart to apply."""

    def test_known_available_provider_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(provider_config, "_provider_available", lambda p: True)
        assert provider_config.assign_provider_override_error("claude") is None

    def test_empty_provider_accepted(self) -> None:
        assert provider_config.assign_provider_override_error(None) is None
        assert provider_config.assign_provider_override_error("") is None

    def test_unknown_provider_name_is_rejected(self) -> None:
        error = provider_config.assign_provider_override_error("not-a-real-provider")
        assert error is not None
        assert "not-a-real-provider" in error

    def test_disabled_or_uninstalled_provider_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The override would just fail the same way the stuck role already
        # fails — reject it up front with a clear reason instead.
        monkeypatch.setattr(provider_config, "_provider_available", lambda p: False)
        error = provider_config.assign_provider_override_error("codex")
        assert error is not None
        assert "codex" in error

    def test_is_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(provider_config, "_provider_available", lambda p: True)
        assert provider_config.assign_provider_override_error("CLAUDE") is None


class TestAssignModelOverrideWithProviderOverride:
    """Issue #270: `--model` must validate against the OVERRIDDEN provider
    when `--provider` is given on the same assign, not the role's normal
    config/availability resolution — and the hard-block error should point
    at the `--provider` escape hatch when no override was given yet."""

    def test_model_validated_against_provider_override_not_role_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Role's normal resolution says codex, but --provider claude on this
        # same assign means claude is what will actually spawn — a
        # claude-shaped model id must be accepted, not blocked as
        # cross-provider.
        monkeypatch.setattr(provider_config, "effective_provider_for", lambda *_a, **_kw: "codex")
        error = provider_config.assign_model_override_error(
            "backend", "claude-opus-5", provider_override="claude"
        )
        assert error is None

    def test_model_still_blocked_against_the_override_itself(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(provider_config, "effective_provider_for", lambda *_a, **_kw: "codex")
        error = provider_config.assign_model_override_error(
            "backend", "gemini-3.1-pro", provider_override="claude"
        )
        assert error is not None
        assert "claude" in error

    def test_error_hints_at_provider_flag_when_no_override_given(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(provider_config, "effective_provider_for", lambda *_a, **_kw: "codex")
        error = provider_config.assign_model_override_error("backend", "claude-opus-5")
        assert error is not None
        assert "--provider claude" in error

    def test_error_omits_hint_when_override_already_given(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Already used --provider this call (just picked the wrong one) —
        # repeating the same hint would be noise, not help.
        monkeypatch.setattr(provider_config, "effective_provider_for", lambda *_a, **_kw: "codex")
        error = provider_config.assign_model_override_error(
            "backend", "gemini-3.1-pro", provider_override="claude"
        )
        assert error is not None
        assert "add --provider" not in error

    def test_warning_also_respects_provider_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(provider_config, "effective_provider_for", lambda *_a, **_kw: "codex")
        warning = provider_config.assign_model_override_warning(
            "backend", "totally-new-model-9", provider_override="claude"
        )
        assert warning is not None
        assert "claude" in warning
