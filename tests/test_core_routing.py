"""core.routing — Router/StaticRoutingPolicy/facade (docs/v2/
V2_IMPLEMENTATION_PLAN.md §2 Phase 2 "Router", epic #309 Phase 3).

Central proof required by the plan: flag OFF must be byte-identical to
calling `provider_config.effective_provider_for` directly, for every role
`provider_config.effective_provider_for` itself is exercised against
(mirrors `tests/test_provider_config.py`'s own scenarios rather than
re-inventing new ones, so this test can never validate against a fixture
that has quietly drifted from the real function)."""

from __future__ import annotations

import pytest

import agent_takkub.provider_config as provider_config
from agent_takkub import provider_models, role_models
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


# ── effective_model_for / effective_model_for_v2 (model pin resolver).
# #504 cut half: role_models.py/provider_models.py read/write their V2
# target directly now, so there is exactly one store left — no V1-vs-V2
# shadow-read, no model_pin_v2_drift telemetry, no TAKKUB_V2_AUTHORITY gate.
# Router.effective_model_for is now a thin pass-through to
# role_models.model_for(role, provider) or provider_models.model_for
# (provider); these tests exercise that precedence directly through the
# real stores (isolation is automatic — conftest.py's autouse
# `_isolate_runtime`). ------------------------------------------------


@pytest.mark.parametrize(
    "role_entries,provider_entries,role,provider,expected",
    [
        # role pin saved FOR the spawning provider wins over the provider pin.
        (
            [("backend", "claude", "claude-opus-5")],
            [("claude", "claude-sonnet-5")],
            "backend",
            "claude",
            "claude-opus-5",
        ),
        # role pin exists but was saved for a DIFFERENT provider than the one
        # spawning — must be ignored (not leaked cross-provider), falls to
        # the provider-level pin instead.
        (
            [("backend", "codex", "gpt-5.6")],
            [("claude", "claude-sonnet-5")],
            "backend",
            "claude",
            "claude-sonnet-5",
        ),
        # role has no pin at all → provider-level pin.
        ([], [("claude", "claude-sonnet-5")], "backend", "claude", "claude-sonnet-5"),
        # neither role nor provider has a pin → None.
        ([], [], "backend", "claude", None),
        # multi-provider: role pinned for codex, spawning provider IS codex.
        ([("qa", "codex", "gpt-5.6-terra")], [], "qa", "codex", "gpt-5.6-terra"),
        # multi-provider: provider-level pin for a non-claude CLI.
        ([], [("opencode", "gpt-5.6-codex")], "backend", "opencode", "gpt-5.6-codex"),
    ],
    ids=[
        "role-pin-matches-provider",
        "role-pin-different-provider-ignored",
        "no-role-pin-falls-to-provider",
        "no-pin-at-all-is-none",
        "multi-provider-role-pin-codex",
        "multi-provider-provider-pin-opencode",
    ],
)
def test_effective_model_for_role_provider_precedence(
    role_entries, provider_entries, role, provider, expected
):
    for r, p, model in role_entries:
        role_models.set_model(r, p, model)
    for p, model in provider_entries:
        provider_models.set_model(p, model)

    assert Router().effective_model_for(role, provider) == expected


def test_effective_model_for_effort_only_role_entry_falls_to_provider_pin():
    # An effort-only role entry (no model) has nothing for model_for() to
    # return — falls to the provider-level pin, same as no entry at all.
    role_models.set_effort("backend", "claude", "high")
    provider_models.set_model("claude", "claude-sonnet-5")

    assert Router().effective_model_for("backend", "claude") == "claude-sonnet-5"


def test_effective_model_for_v2_matches_direct_call():
    role_models.set_model("backend", "claude", "claude-opus-5")
    provider_models.set_model("claude", "claude-sonnet-5")

    assert effective_model_for_v2("backend", "claude") == "claude-opus-5"
    assert effective_model_for_v2("backend", "claude") == Router().effective_model_for(
        "backend", "claude"
    )


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
