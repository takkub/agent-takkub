"""_resolve_teammate_effort — precedence + provider/model gating for the
per-role reasoning-effort argument (#136 follow-up).

Precedence under test: role_models setting (Settings -> Providers & Roles)
> TAKKUB_TEAMMATE_EFFORT env escape hatch > the role's tier default
(orchestrator_text._teammate_tier). Whatever wins is then gated by
provider_spec.effort_levels_for so an unsupported provider or a
no-reasoning-effort model (claude-haiku-4-5) never gets an argv flag.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub import role_models
from agent_takkub.provider_spec import PROVIDER_REGISTRY
from agent_takkub.spawn_engine import _append_provider_effort, _resolve_teammate_effort

CLAUDE = PROVIDER_REGISTRY["claude"]
CODEX = PROVIDER_REGISTRY["codex"]
GEMINI = PROVIDER_REGISTRY["gemini"]


@pytest.fixture(autouse=True)
def redirect_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(role_models, "_PATH", tmp_path / "role-models.json")
    monkeypatch.delenv("TAKKUB_TEAMMATE_EFFORT", raising=False)
    yield tmp_path


def test_falls_back_to_tier_default_when_nothing_else_set() -> None:
    # backend's tier default effort is "high" (orchestrator_text._ROLE_MODEL_TIERS).
    assert _resolve_teammate_effort("backend", CLAUDE, "claude-sonnet-5") == "high"
    # frontend has no tier override -> the shared default tier ("medium").
    assert _resolve_teammate_effort("frontend", CLAUDE, "claude-sonnet-5") == "medium"


def test_env_wins_over_tier_default() -> None:
    import os

    os.environ["TAKKUB_TEAMMATE_EFFORT"] = "low"
    try:
        assert _resolve_teammate_effort("backend", CLAUDE, "claude-sonnet-5") == "low"
    finally:
        del os.environ["TAKKUB_TEAMMATE_EFFORT"]


def test_role_setting_wins_over_env_and_tier() -> None:
    import os

    role_models.set_effort("backend", "claude", "xhigh")
    os.environ["TAKKUB_TEAMMATE_EFFORT"] = "low"
    try:
        assert _resolve_teammate_effort("backend", CLAUDE, "claude-sonnet-5") == "xhigh"
    finally:
        del os.environ["TAKKUB_TEAMMATE_EFFORT"]


def test_role_setting_for_a_different_provider_does_not_apply() -> None:
    role_models.set_effort("backend", "codex", "high")
    # Role's saved effort is bound to codex; resolving for claude must fall
    # through to tier default instead of leaking codex's value.
    assert _resolve_teammate_effort("backend", CLAUDE, "claude-sonnet-5") == "high"
    # (coincidentally also "high" via tier default — prove the binding with a
    # provider whose tier default differs)
    role_models.set_effort("frontend", "codex", "xhigh")
    assert _resolve_teammate_effort("frontend", CLAUDE, "claude-sonnet-5") == "medium"


def test_unsupported_provider_resolves_empty_regardless_of_setting() -> None:
    role_models.set_effort("backend", "gemini", "high")
    assert _resolve_teammate_effort("backend", GEMINI, "gemini-3.1-pro-high") == ""


def test_haiku_model_resolves_empty_even_with_role_setting() -> None:
    role_models.set_effort("frontend", "claude", "high")
    assert _resolve_teammate_effort("frontend", CLAUDE, "claude-haiku-4-5") == ""
    assert _resolve_teammate_effort("frontend", CLAUDE, "haiku") == ""


def test_no_model_override_does_not_suppress_effort() -> None:
    # Empty model string = no explicit --model (CLI default account model),
    # not proof it's haiku — must not be gated away.
    assert _resolve_teammate_effort("backend", CLAUDE, "") == "high"


def test_append_provider_effort_claude_uses_plain_flag() -> None:
    argv: list[str] = []
    _append_provider_effort(argv, CLAUDE, "high")
    assert argv == ["--effort", "high"]


def test_append_provider_effort_codex_uses_config_key() -> None:
    argv: list[str] = []
    _append_provider_effort(argv, CODEX, "medium")
    assert argv == ["-c", "model_reasoning_effort=medium"]


def test_append_provider_effort_no_flag_for_unsupported_provider() -> None:
    argv: list[str] = []
    _append_provider_effort(argv, GEMINI, "high")
    assert argv == []


def test_append_provider_effort_empty_value_appends_nothing() -> None:
    argv: list[str] = []
    _append_provider_effort(argv, CLAUDE, "")
    assert argv == []
