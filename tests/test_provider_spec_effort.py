"""effort_levels_for — single source of truth for which reasoning-effort
levels a provider/model combination accepts (#136 follow-up).

Consumed by both the spawn-time resolver (spawn_engine._resolve_teammate_effort)
and the Settings "Effort" dropdown (settings_window._fill_effort_combo) — this
file locks the contract both of those depend on.
"""

from __future__ import annotations

from agent_takkub import provider_spec


def test_claude_effort_levels() -> None:
    assert provider_spec.claude_spec.effort_levels == ("low", "medium", "high", "xhigh", "max")


def test_codex_effort_levels() -> None:
    assert provider_spec.codex_spec.effort_levels == ("low", "medium", "high")


def test_providers_without_effort_flag_have_no_levels() -> None:
    for spec in (
        provider_spec.gemini_spec,
        provider_spec.opencode_spec,
        provider_spec.kimi_spec,
        provider_spec.cursor_spec,
    ):
        assert spec.effort_flag is None
        assert spec.effort_levels == ()


def test_effort_levels_for_claude_default_model() -> None:
    assert provider_spec.effort_levels_for("claude", "claude-sonnet-5") == (
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    )


def test_effort_levels_for_no_model_still_supported() -> None:
    # No explicit model override (pane spawns with the CLI's own default) —
    # can't prove it's haiku, so don't suppress the whole provider.
    assert provider_spec.effort_levels_for("claude", "") != ()
    assert provider_spec.effort_levels_for("claude", None) != ()


def test_effort_levels_for_haiku_model_is_empty() -> None:
    assert provider_spec.effort_levels_for("claude", "claude-haiku-4-5") == ()
    assert provider_spec.effort_levels_for("claude", "haiku") == ()


def test_effort_levels_for_unsupported_provider_is_empty() -> None:
    assert provider_spec.effort_levels_for("gemini", "gemini-3.1-pro-high") == ()


def test_effort_levels_for_unknown_provider_is_empty() -> None:
    assert provider_spec.effort_levels_for("does-not-exist", "some-model") == ()
