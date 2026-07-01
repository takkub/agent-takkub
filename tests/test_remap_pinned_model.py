"""Proxy-mode model-pin translation (_remap_pinned_model).

Regression guard for the "teammate panes พังทุกตัว / 404 No active credentials
for provider: anthropic" bug: a Claude auth profile that remaps the tier aliases
to non-Anthropic ids (a gateway serving only ``ocg/*``) was defeated by the
concrete ``--model claude-sonnet-5`` pin, because ``ANTHROPIC_DEFAULT_*_MODEL``
only rewrites the bare tier alias — a concrete id is sent verbatim and 404s on
the proxy. _remap_pinned_model translates the pin through the same remap so the
pinned teammate/Lead-fallback models reach the proxy as ``ocg/*`` too.
"""

from __future__ import annotations

from agent_takkub.spawn_engine import _remap_pinned_model

# A profile's env after apply_claude_auth_overrides, proxy remap present.
PROXY_ENV = {
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "ocg/glm-5.2",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "ocg/deepseek-v4-pro",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "ocg/deepseek-v4-pro",
}


def test_sonnet_pin_translated_to_remap():
    assert _remap_pinned_model("claude-sonnet-5", PROXY_ENV) == "ocg/deepseek-v4-pro"


def test_haiku_fallback_pin_translated():
    # The fallback that used to 404 the pane one tier down.
    assert _remap_pinned_model("claude-haiku-4-5", PROXY_ENV) == "ocg/deepseek-v4-pro"


def test_opus_pin_translated_and_uses_its_own_tier():
    # Gate roles (reviewer/critic) keep tier separation: opus → glm, not deepseek.
    assert _remap_pinned_model("claude-opus-4-8", PROXY_ENV) == "ocg/glm-5.2"


def test_opus_1m_variant_classified_by_tier_name():
    assert _remap_pinned_model("claude-opus-4-8[1m]", PROXY_ENV) == "ocg/glm-5.2"


def test_no_remap_env_keeps_pin_unchanged():
    # Normal, non-proxy install: behavior must be identical to before.
    assert _remap_pinned_model("claude-sonnet-5", {}) == "claude-sonnet-5"


def test_partial_remap_only_translates_defined_tiers():
    env = {"ANTHROPIC_DEFAULT_SONNET_MODEL": "ocg/deepseek-v4-pro"}
    assert _remap_pinned_model("claude-sonnet-5", env) == "ocg/deepseek-v4-pro"
    # Opus tier has no remap → pin passes through untouched.
    assert _remap_pinned_model("claude-opus-4-8", env) == "claude-opus-4-8"


def test_already_proxy_native_id_passes_through():
    # User set TAKKUB_TEAMMATE_MODEL="ocg/deepseek-v4-pro" directly.
    assert _remap_pinned_model("ocg/deepseek-v4-pro", PROXY_ENV) == "ocg/deepseek-v4-pro"


def test_empty_pin_returns_empty():
    # "" means "no --model flag" — must stay empty, never grow a value.
    assert _remap_pinned_model("", PROXY_ENV) == ""


def test_blank_remap_value_falls_back_to_pin():
    env = {"ANTHROPIC_DEFAULT_SONNET_MODEL": "   "}
    assert _remap_pinned_model("claude-sonnet-5", env) == "claude-sonnet-5"
