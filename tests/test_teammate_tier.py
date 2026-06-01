"""Per-role model tier map (_teammate_tier).

The cockpit owner runs on Claude Max, so model choice trades latency for
quality, not dollars. These tests lock the intent: gate roles get the biggest
tier, correctness-sensitive impl gets high effort on Sonnet, and everything
else stays on the snappy default — with a sane fallback one tier down.
"""

from __future__ import annotations

from agent_takkub.orchestrator import (
    _DEFAULT_TEAMMATE_TIER,
    _ROLE_MODEL_TIERS,
    _teammate_tier,
)


def test_gate_roles_run_opus_high_with_sonnet_fallback():
    """reviewer/critic gate what ships — max quality, degrade only to Sonnet."""
    for role in ("reviewer", "critic"):
        model, effort, fallback = _teammate_tier(role)
        assert model == "claude-opus-4-8"
        assert effort == "high"
        assert fallback == "claude-sonnet-4-6"


def test_correctness_roles_run_sonnet_high():
    """backend/devops touch schemas, auth, and irreversible infra → high effort."""
    for role in ("backend", "devops"):
        model, effort, fallback = _teammate_tier(role)
        assert model == "claude-sonnet-4-6"
        assert effort == "high"
        assert fallback == "claude-haiku-4-5"


def test_execution_roles_use_default_tier():
    """frontend/mobile/qa are high-frequency execution → snappy Sonnet medium."""
    for role in ("frontend", "mobile", "qa"):
        assert _teammate_tier(role) == _DEFAULT_TEAMMATE_TIER


def test_unknown_role_falls_back_to_default_tier():
    assert _teammate_tier("some-custom-role") == _DEFAULT_TEAMMATE_TIER


def test_default_tier_is_sonnet_medium_haiku():
    assert _DEFAULT_TEAMMATE_TIER == ("claude-sonnet-4-6", "medium", "claude-haiku-4-5")


def test_only_intended_roles_are_overridden():
    """Guard against accidental tier creep — keep the override set explicit.
    codex/gemini use Opus/high so Claude substitutes have the same quality as
    reviewer/critic when the real binary is unavailable."""
    assert set(_ROLE_MODEL_TIERS) == {"reviewer", "critic", "backend", "devops", "codex", "gemini"}


def test_codex_gemini_substitutes_use_opus_high():
    """codex/gemini roles map to Opus/high so a Claude substitute gets the same
    model quality as reviewer/critic (not the default Sonnet/medium)."""
    for role in ("codex", "gemini"):
        model, effort, fallback = _teammate_tier(role)
        assert model == "claude-opus-4-8"
        assert effort == "high"
        assert fallback == "claude-sonnet-4-6"
