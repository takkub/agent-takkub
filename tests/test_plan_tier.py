"""Unit tests for plan_tier — account plan (Pro/Max) state.

Pins the intent: default Max (no surprise downgrade for pre-existing
installs), Pro is the only tier that changes behaviour, and corrupt/garbage
state degrades to Max rather than crashing the cockpit on startup.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub import plan_tier


@pytest.fixture
def tmp_state_path(tmp_path, monkeypatch):
    """Redirect plan_tier to a tmp file so tests don't touch the real config."""
    path = tmp_path / "plan.json"
    monkeypatch.setattr(plan_tier, "_PATH", path)
    return path


def test_missing_file_defaults_to_max(tmp_state_path):
    assert plan_tier.current() == plan_tier.MAX
    assert plan_tier.is_pro() is False


def test_set_then_current_roundtrip(tmp_state_path):
    plan_tier.set_current(plan_tier.PRO)
    assert plan_tier.current() == plan_tier.PRO
    assert plan_tier.is_pro() is True
    plan_tier.set_current(plan_tier.MAX)
    assert plan_tier.current() == plan_tier.MAX
    assert plan_tier.is_pro() is False


def test_set_writes_atomically_via_tmp_file(tmp_state_path):
    plan_tier.set_current(plan_tier.PRO)
    assert tmp_state_path.exists()
    assert not tmp_state_path.with_suffix(tmp_state_path.suffix + ".tmp").exists()


def test_corrupt_json_defaults_to_max(tmp_state_path):
    tmp_state_path.write_text("{not valid json", encoding="utf-8")
    assert plan_tier.current() == plan_tier.MAX


def test_non_dict_json_defaults_to_max(tmp_state_path):
    tmp_state_path.write_text('["pro"]', encoding="utf-8")
    assert plan_tier.current() == plan_tier.MAX


def test_unknown_tier_in_file_defaults_to_max(tmp_state_path):
    tmp_state_path.write_text('{"tier": "enterprise"}', encoding="utf-8")
    assert plan_tier.current() == plan_tier.MAX


def test_set_current_normalizes_case_and_whitespace(tmp_state_path):
    plan_tier.set_current("  PRO  ")
    assert plan_tier.current() == plan_tier.PRO


def test_set_current_unknown_tier_raises(tmp_state_path):
    with pytest.raises(ValueError):
        plan_tier.set_current("enterprise")


def test_pro_lead_model_has_no_1m_suffix():
    # The whole point: the Pro pin must be a standard-context model.
    assert "[1m]" not in plan_tier.PRO_LEAD_MODEL


def test_default_path_is_under_home_takkub(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    import importlib

    import agent_takkub.plan_tier as pt_module

    importlib.reload(pt_module)
    assert pt_module._PATH == tmp_path / ".takkub" / "plan.json"
    importlib.reload(pt_module)
