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
from agent_takkub.core.routing import Router, StaticRoutingPolicy, effective_provider_for_v2
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
