"""The Lead's --model decision (_lead_model_override).

Locks the Pro/Max contract: Max inherits the user default (no flag), Pro pins
a standard-context model so the 1M-context credit error can't hard-fail the
Lead pane. Env override and the disable-the-pin escape hatch are covered too.
"""

from __future__ import annotations

import pytest

from agent_takkub import orchestrator, plan_tier


@pytest.fixture
def tmp_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(plan_tier, "_PATH", tmp_path / "plan.json")
    # Default env: no override set.
    monkeypatch.delenv("TAKKUB_PRO_LEAD_MODEL", raising=False)
    return tmp_path


def test_max_inherits_user_default(tmp_plan):
    plan_tier.set_current(plan_tier.MAX)
    assert orchestrator._lead_model_override() is None


def test_pro_pins_standard_context_opus(tmp_plan):
    plan_tier.set_current(plan_tier.PRO)
    model = orchestrator._lead_model_override()
    assert model == plan_tier.PRO_LEAD_MODEL
    assert "[1m]" not in model


def test_pro_env_override_swaps_model(tmp_plan, monkeypatch):
    plan_tier.set_current(plan_tier.PRO)
    monkeypatch.setenv("TAKKUB_PRO_LEAD_MODEL", "claude-sonnet-5")
    assert orchestrator._lead_model_override() == "claude-sonnet-5"


def test_pro_empty_env_disables_pin(tmp_plan, monkeypatch):
    """Escape hatch: empty override means inherit the user default even on Pro."""
    plan_tier.set_current(plan_tier.PRO)
    monkeypatch.setenv("TAKKUB_PRO_LEAD_MODEL", "")
    assert orchestrator._lead_model_override() is None


def test_default_install_is_max(tmp_plan):
    # No plan.json written → behaves as Max → no model pin.
    assert orchestrator._lead_model_override() is None
