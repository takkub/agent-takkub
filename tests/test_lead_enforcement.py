"""How a restricted Lead's policy is held (#842) after the Codex Lead OS
sandbox was removed (2026-10-14)."""

from __future__ import annotations

from types import SimpleNamespace

from agent_takkub import lead_enforcement
from agent_takkub.orchestrator import Orchestrator


def _fake(provider: str, mode: str = "none") -> SimpleNamespace:
    pane = SimpleNamespace(
        session=SimpleNamespace(is_alive=True), model=SimpleNamespace(provider_name=provider)
    )
    return SimpleNamespace(
        _resolve_project=lambda project: project,
        _panes_by_project={"project": {"lead": pane}},
        _pane_state={"project::lead": SimpleNamespace(lead_enforcement_mode=mode)},
    )


def test_live_claude_or_codex_lead_may_tighten_without_restart() -> None:
    """Claude's guard reads the preset live; a Codex Lead has no spawn-time
    boundary any more, so a restart would change nothing for either."""
    for provider in ("claude", "codex"):
        assert Orchestrator._lead_restriction_transition_block(_fake(provider), "project") == ""


def test_live_lead_without_a_restricted_mode_still_blocks_tightening() -> None:
    reason = Orchestrator._lead_restriction_transition_block(_fake("gemini"), "project")
    assert "close the active unrestricted Lead" in reason


def test_codex_lead_is_a_supported_restricted_lead_labelled_prompt_only() -> None:
    """Removing the sandbox must not make a restricted Codex Lead
    unavailable — readiness reports what actually holds it: the policy text."""
    assert "codex" in lead_enforcement.RESTRICTED_LEAD_PROVIDERS
    mode = lead_enforcement.MODE_CODEX_PROMPT
    assert lead_enforcement.enforcement_gap("codex", mode, True) == ""
    assert lead_enforcement.gate_label("codex", mode, True) == "prompt_only"
    assert "not enforced" in lead_enforcement.enforcement_gap("codex", "none", True)
