"""core.routing — Router/StaticRoutingPolicy/facade (docs/v2/
V2_IMPLEMENTATION_PLAN.md §2 Phase 2 "Router", epic #309 Phase 3).

Central proof required by the plan: flag OFF must be byte-identical to
calling `provider_config.effective_provider_for` directly, for every role
`provider_config.effective_provider_for` itself is exercised against
(mirrors `tests/test_provider_config.py`'s own scenarios rather than
re-inventing new ones, so this test can never validate against a fixture
that has quietly drifted from the real function)."""

from __future__ import annotations

import json

import pytest

import agent_takkub.provider_config as provider_config
from agent_takkub import provider_models, role_models
from agent_takkub.core.migration.steps_v1 import build_readonly_registries_step
from agent_takkub.core.routing import (
    Router,
    StaticRoutingPolicy,
    effective_model_for_v2,
    effective_provider_for_v2,
)
from agent_takkub.core.routing.flag import v2_router_enabled

# ── flag ──────────────────────────────────────────────────────────────────


def test_flag_on_by_default(monkeypatch):
    """Default flipped ON in 1.0.84 (epic #309)."""
    monkeypatch.delenv("TAKKUB_V2_ROUTER", raising=False)
    assert v2_router_enabled() is True


def test_flag_on_when_set_to_1(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_ROUTER", "1")
    assert v2_router_enabled() is True


def test_flag_off_for_any_other_value(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_ROUTER", "true")
    assert v2_router_enabled() is False


# ── StaticRoutingPolicy / Router: delegate verbatim ──────────────────────


def test_static_routing_policy_delegates_to_effective_provider_for(monkeypatch):
    monkeypatch.setattr(provider_config, "effective_provider_for", lambda role, project: "codex")
    assert StaticRoutingPolicy().resolve("backend", "proj") == "codex"


def test_router_default_policy_is_static(monkeypatch):
    monkeypatch.setattr(provider_config, "effective_provider_for", lambda role, project: "gemini")
    assert Router().effective_provider_for("qa", "proj") == "gemini"


def test_router_accepts_custom_policy():
    class _FakePolicy:
        def resolve(self, role, project=None):
            return "cursor"

    assert Router(_FakePolicy()).effective_provider_for("backend") == "cursor"


# ── facade: flag off = direct call, byte-identical ───────────────────────


@pytest.mark.parametrize(
    "role,project",
    [("lead", None), ("backend", "proj-a"), ("codex", None), ("gemini", "proj-b")],
)
def test_facade_flag_off_matches_direct_call(monkeypatch, role, project):
    monkeypatch.delenv("TAKKUB_V2_ROUTER", raising=False)
    direct = provider_config.effective_provider_for(role, project)
    via_facade = effective_provider_for_v2(role, project)
    assert via_facade == direct


def test_facade_flag_off_never_touches_router(monkeypatch):
    monkeypatch.delenv("TAKKUB_V2_ROUTER", raising=False)

    def boom(*a, **kw):
        raise AssertionError("Router must not be constructed when the flag is off")

    monkeypatch.setattr("agent_takkub.core.routing.facade.Router", boom)
    assert effective_provider_for_v2("backend", None) == provider_config.effective_provider_for(
        "backend", None
    )


# ── facade: flag on ───────────────────────────────────────────────────────


def test_facade_flag_on_resolves_via_router(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_ROUTER", "1")
    monkeypatch.setattr(provider_config, "effective_provider_for", lambda role, project: "opencode")
    assert effective_provider_for_v2("backend", "proj") == "opencode"


def test_facade_flag_on_fails_open_on_router_exception(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_ROUTER", "1")
    monkeypatch.setattr(provider_config, "effective_provider_for", lambda role, project: "claude")

    class _BoomRouter:
        def effective_provider_for(self, role, project=None):
            raise RuntimeError("router blew up")

    monkeypatch.setattr("agent_takkub.core.routing.facade.Router", lambda: _BoomRouter())
    # Must still return the direct answer instead of raising.
    assert effective_provider_for_v2("backend", "proj") == "claude"


# ── effective_model_for / effective_model_for_v2 (model pin resolver, epic #309 Wave C,
# plan §1.3) ─────────────────────────────────────────────────────────────


def _write_v1_model_state(settings_home, *, roles=None, providers=None):
    if roles is not None:
        (settings_home / "role-models.json").write_text(json.dumps(roles), encoding="utf-8")
    if providers is not None:
        (settings_home / "provider-models.json").write_text(json.dumps(providers), encoding="utf-8")


def _v1_direct_model_for(role, provider):
    """Same precedence spawn_engine's call sites used before Wave C wired
    them through the façade — the parity ground truth every test below
    compares `effective_model_for_v2`'s answer against."""
    return role_models.model_for(role, provider) or provider_models.model_for(provider)


@pytest.fixture
def v1_model_state(tmp_path, monkeypatch):
    """Point role_models/provider_models' own `_PATH` at a tmp settings dir
    — same isolation `test_role_models.py`/`test_spawn_default_model_env.py`
    use — so both the direct V1 calls AND the migration ladder step below
    read the exact same source files."""
    settings_home = tmp_path / "settings"
    settings_home.mkdir()
    monkeypatch.setattr(role_models, "_PATH", settings_home / "role-models.json")
    monkeypatch.setattr(provider_models, "_PATH", settings_home / "provider-models.json")
    return settings_home


def _migrate(data_home, settings_home):
    """Run the REAL ladder step 1 (RegistryCopyStep) — the actual mechanism
    that ships a user's V1 role/provider model pins into the V2 catalog —
    rather than hand-writing the wrapped JSON, so this test can never
    validate against a fixture shape that's quietly drifted from what
    `migrate apply` really produces."""
    report = build_readonly_registries_step(
        data_home=data_home, settings_home=settings_home
    ).apply()
    assert report.ok, report.detail


@pytest.fixture(autouse=True)
def _reset_drift_dedupe(monkeypatch):
    """`Router._log_drift`'s per-(role, provider) de-dupe set is module-level
    by design (see router.py) — reset it before every test in this module so
    an earlier test's drift can't silence a later test's assertion."""
    monkeypatch.setattr("agent_takkub.core.routing.router._DRIFT_LOGGED", set())


# -- not migrated: neither V2 target exists → Router falls back to V1 directly


def test_effective_model_for_falls_back_to_v1_when_not_migrated(v1_model_state, monkeypatch):
    _write_v1_model_state(
        v1_model_state,
        roles={"backend": {"provider": "claude", "model": "claude-opus-5"}},
        providers={"claude": "claude-sonnet-5"},
    )
    # No _migrate() call — models/registry.json + aliases.json don't exist.
    assert Router().effective_model_for("backend", "claude") == "claude-opus-5"


def test_effective_model_for_v2_falls_back_to_v1_when_not_migrated(v1_model_state, monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_ROUTER", "1")
    _write_v1_model_state(v1_model_state, roles={}, providers={"claude": "claude-sonnet-5"})
    assert effective_model_for_v2("backend", "claude") == "claude-sonnet-5"


# -- migrated: real V2 catalog, byte-identical to the V1 functions across the
# full role/provider precedence matrix (plan §1.3's core requirement) ------


@pytest.mark.parametrize(
    "roles,providers,role,provider",
    [
        # role pin saved FOR the spawning provider wins over the provider pin.
        (
            {"backend": {"provider": "claude", "model": "claude-opus-5"}},
            {"claude": "claude-sonnet-5"},
            "backend",
            "claude",
        ),
        # role pin exists but was saved for a DIFFERENT provider than the one
        # spawning — must be ignored (not leaked cross-provider), falls to
        # the provider-level pin instead. The exact hazard ModelProfile's
        # missing `provider` field would silently reintroduce if resolved
        # through it instead of the raw (provider, model) pin.
        (
            {"backend": {"provider": "codex", "model": "gpt-5.6"}},
            {"claude": "claude-sonnet-5"},
            "backend",
            "claude",
        ),
        # role has no pin at all → provider-level pin.
        ({}, {"claude": "claude-sonnet-5"}, "backend", "claude"),
        # neither role nor provider has a pin → None.
        ({}, {}, "backend", "claude"),
        # role entry is effort-only (no model) → skipped, falls to provider pin.
        (
            {"backend": {"provider": "claude", "effort": "high"}},
            {"claude": "claude-sonnet-5"},
            "backend",
            "claude",
        ),
        # multi-provider: role pinned for codex, spawning provider IS codex.
        ({"qa": {"provider": "codex", "model": "gpt-5.6-terra"}}, {}, "qa", "codex"),
        # multi-provider: provider-level pin for a non-claude CLI.
        ({}, {"opencode": "gpt-5.6-codex"}, "backend", "opencode"),
    ],
    ids=[
        "role-pin-matches-provider",
        "role-pin-different-provider-ignored",
        "no-role-pin-falls-to-provider",
        "no-pin-at-all-is-none",
        "effort-only-role-entry-falls-to-provider",
        "multi-provider-role-pin-codex",
        "multi-provider-provider-pin-opencode",
    ],
)
def test_effective_model_for_matches_v1_across_role_provider_matrix(
    v1_model_state, isolated_v2_data_home, roles, providers, role, provider
):
    _write_v1_model_state(v1_model_state, roles=roles, providers=providers)
    _migrate(isolated_v2_data_home, v1_model_state)

    direct = _v1_direct_model_for(role, provider)
    assert Router().effective_model_for(role, provider) == direct


def test_effective_model_for_v2_matches_v1_when_migrated_and_flag_on(
    v1_model_state, isolated_v2_data_home, monkeypatch
):
    monkeypatch.setenv("TAKKUB_V2_ROUTER", "1")
    _write_v1_model_state(
        v1_model_state,
        roles={"backend": {"provider": "claude", "model": "claude-opus-5"}},
        providers={"claude": "claude-sonnet-5"},
    )
    _migrate(isolated_v2_data_home, v1_model_state)

    assert effective_model_for_v2("backend", "claude") == "claude-opus-5"
    assert _v1_direct_model_for("backend", "claude") == "claude-opus-5"


def test_effective_model_for_v2_malformed_v2_json_fails_open_per_pin(
    v1_model_state, isolated_v2_data_home, monkeypatch
):
    """A corrupted V2 target (one file, not both — "not migrated" only
    triggers when NEITHER exists) must not crash: `read_json` itself fails
    open to "no data" for that one file. Since V1 is authoritative (Wave C
    shadow-read fix), the corruption can't change the *answer* either way —
    it only ever affects the shadow comparison used for drift telemetry."""
    monkeypatch.setenv("TAKKUB_V2_ROUTER", "1")
    _write_v1_model_state(
        v1_model_state,
        roles={"backend": {"provider": "claude", "model": "claude-opus-5"}},
        providers={"claude": "claude-sonnet-5"},
    )
    _migrate(isolated_v2_data_home, v1_model_state)
    (isolated_v2_data_home / "v2" / "models" / "aliases.json").write_text(
        "{not valid json", encoding="utf-8"
    )

    # V1's role pin answers regardless of the corrupted V2 aliases.json — never raises.
    assert effective_model_for_v2("backend", "claude") == "claude-opus-5"


# -- shadow-read: V1 authoritative even after migration, V2 drift is telemetry-only
# (Wave C fix, prod incident 2026-08-23 — Settings writes never sync back into V2) --


def test_effective_model_for_prefers_v1_over_stale_v2_after_migration(
    v1_model_state, isolated_v2_data_home
):
    """The steady-state prod hazard this fix closes: migrate once, then the
    user re-pins a model in Settings (V1-only write, `role_models.set_model`)
    — the next spawn must see the NEW pin, not the stale migrated V2 copy."""
    _write_v1_model_state(
        v1_model_state,
        roles={"backend": {"provider": "claude", "model": "claude-opus-5"}},
        providers={"claude": "claude-sonnet-5"},
    )
    _migrate(isolated_v2_data_home, v1_model_state)

    # User re-pins the role AFTER migration — only V1 sees this, exactly like
    # role_models.set_model() in production.
    _write_v1_model_state(
        v1_model_state, roles={"backend": {"provider": "claude", "model": "claude-fable-5"}}
    )

    assert Router().effective_model_for("backend", "claude") == "claude-fable-5"


def test_effective_model_for_provider_pin_drift_prefers_v1_too(
    v1_model_state, isolated_v2_data_home
):
    """Same hazard, provider-level pin instead of a role pin."""
    _write_v1_model_state(v1_model_state, roles={}, providers={"claude": "claude-sonnet-5"})
    _migrate(isolated_v2_data_home, v1_model_state)

    _write_v1_model_state(v1_model_state, providers={"claude": "claude-fable-5"})

    assert Router().effective_model_for("backend", "claude") == "claude-fable-5"


def test_effective_model_for_v1_pin_cleared_after_migration_returns_none(
    v1_model_state, isolated_v2_data_home
):
    """V1 drops the pin entirely (user clears it in Settings) while the
    stale V2 copy still carries the old value — V1's ``None`` must win, not
    V2's leftover value."""
    _write_v1_model_state(
        v1_model_state,
        roles={"backend": {"provider": "claude", "model": "claude-opus-5"}},
        providers={"claude": "claude-sonnet-5"},
    )
    _migrate(isolated_v2_data_home, v1_model_state)

    _write_v1_model_state(v1_model_state, roles={}, providers={})

    assert Router().effective_model_for("backend", "claude") is None


def test_effective_model_for_logs_drift_event_when_v1_and_v2_disagree(
    v1_model_state, isolated_v2_data_home, caplog
):
    _write_v1_model_state(
        v1_model_state,
        roles={"backend": {"provider": "claude", "model": "claude-opus-5"}},
        providers={"claude": "claude-sonnet-5"},
    )
    _migrate(isolated_v2_data_home, v1_model_state)
    _write_v1_model_state(
        v1_model_state, roles={"backend": {"provider": "claude", "model": "claude-fable-5"}}
    )

    with caplog.at_level("WARNING", logger="agent_takkub.core.routing.router"):
        Router().effective_model_for("backend", "claude")

    assert any("model_pin_v2_drift" in r.message for r in caplog.records)


def test_effective_model_for_no_drift_event_when_v1_matches_freshly_migrated_v2(
    v1_model_state, isolated_v2_data_home, caplog
):
    _write_v1_model_state(
        v1_model_state,
        roles={"backend": {"provider": "claude", "model": "claude-opus-5"}},
        providers={"claude": "claude-sonnet-5"},
    )
    _migrate(isolated_v2_data_home, v1_model_state)

    with caplog.at_level("WARNING", logger="agent_takkub.core.routing.router"):
        Router().effective_model_for("backend", "claude")

    assert not any("model_pin_v2_drift" in r.message for r in caplog.records)


def test_effective_model_for_drift_event_rate_limited_per_role_provider(
    v1_model_state, isolated_v2_data_home, caplog
):
    """Repeated spawns for the same (role, provider) log the drift once,
    not once per call — the module-level de-dupe set."""
    _write_v1_model_state(
        v1_model_state,
        roles={"backend": {"provider": "claude", "model": "claude-opus-5"}},
        providers={"claude": "claude-sonnet-5"},
    )
    _migrate(isolated_v2_data_home, v1_model_state)
    _write_v1_model_state(
        v1_model_state, roles={"backend": {"provider": "claude", "model": "claude-fable-5"}}
    )

    with caplog.at_level("WARNING", logger="agent_takkub.core.routing.router"):
        Router().effective_model_for("backend", "claude")
        Router().effective_model_for("backend", "claude")
        Router().effective_model_for("backend", "claude")

    drift_records = [r for r in caplog.records if "model_pin_v2_drift" in r.message]
    assert len(drift_records) == 1


# -- facade: flag off never touches Router for model resolution either -----


def test_facade_model_flag_off_never_touches_router(monkeypatch):
    monkeypatch.delenv("TAKKUB_V2_ROUTER", raising=False)

    def boom(*a, **kw):
        raise AssertionError("Router must not be constructed when the flag is off")

    monkeypatch.setattr("agent_takkub.core.routing.facade.Router", boom)
    monkeypatch.setattr(role_models, "model_for", lambda role, provider: None)
    monkeypatch.setattr(provider_models, "model_for", lambda provider: "claude-haiku-4-5")
    assert effective_model_for_v2("backend", "claude") == "claude-haiku-4-5"


def test_facade_model_flag_on_fails_open_on_router_exception(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_ROUTER", "1")
    monkeypatch.setattr(role_models, "model_for", lambda role, provider: None)
    monkeypatch.setattr(provider_models, "model_for", lambda provider: "claude-haiku-4-5")

    class _BoomRouter:
        def effective_model_for(self, role, provider, project=None):
            raise RuntimeError("router blew up")

    monkeypatch.setattr("agent_takkub.core.routing.facade.Router", lambda: _BoomRouter())
    assert effective_model_for_v2("backend", "claude") == "claude-haiku-4-5"
