"""#318: the persistent role/provider model pin rides ANTHROPIC_DEFAULT_MODEL
(pane_env.apply_default_model), not --model — --model always wins over a model
the user saved with /model, on every spawn AND on a crash-respawn's --resume
(same argv), so a teammate could never keep a model its own session picked.
ANTHROPIC_DEFAULT_MODEL only applies when nothing else already claimed the
model slot (docs.claude.com/en/model-config, "Set a default model for new
sessions"), so a saved /model choice sticks.

A deliberate one-off override (--model on `takkub assign`, or the operator's
TAKKUB_TEAMMATE_MODEL env force) is a different concern — genuinely narrower
than a persistent default — and keeps going through the --model flag.
"""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import config, provider_models, role_models
from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator
from agent_takkub.orchestrator_text import _exit_key
from agent_takkub.provider_config import CLAUDE

_PROJECT = "default"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def tmp_env(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    cockpit = tmp_path / "cockpit"
    cockpit.mkdir(parents=True, exist_ok=True)
    (cockpit / "CLAUDE.md").write_text("# Lead\n", encoding="utf-8")
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(config, "REPO_ROOT", cockpit)
    monkeypatch.setattr(orch_mod, "REPO_ROOT", cockpit)
    monkeypatch.setattr(orch_mod, "find_claude_executable", lambda: "claude")
    monkeypatch.setattr(role_models, "_PATH", tmp_path / "role-models.json")
    monkeypatch.setattr(provider_models, "_PATH", tmp_path / "provider-models.json")
    monkeypatch.delenv("TAKKUB_TEAMMATE_MODEL", raising=False)
    return tmp_path


@pytest.fixture
def orch(qapp: QCoreApplication, tmp_env: pathlib.Path) -> Orchestrator:
    o = Orchestrator()
    o.shutdown_timers()
    return o


def _spawn_capture(orch: Orchestrator, role_name: str, cwd: str = "/proj"):
    fake_pane = MagicMock()
    fake_pane.session = None
    fake_pane.state = "empty"
    fake_pane.attach_session = MagicMock()
    fake_pane._transcript_path = None
    orch._panes_by_project.setdefault(_PROJECT, {})[role_name] = fake_pane

    captured: list[tuple[list[str], dict[str, str]]] = []
    fake_session = MagicMock()
    fake_session.processExited = MagicMock()
    fake_session.processExited.connect = MagicMock()

    with patch.object(orch_mod.PtySession, "__new__", return_value=fake_session):
        with patch.object(
            fake_session,
            "spawn",
            side_effect=lambda argv, cwd, env, **kwargs: captured.append((list(argv), dict(env))),
        ):
            orch.spawn(role_name, cwd=cwd, project=_PROJECT)

    return captured[0] if captured else ([], {})


class TestTeammateDefaultModelPin:
    def test_tier_default_goes_to_anthropic_default_model_not_argv(
        self, orch: Orchestrator
    ) -> None:
        # No role/provider pin configured — backend's tier default applies.
        argv, env = _spawn_capture(orch, "backend")
        assert "--model" not in argv
        assert env.get("ANTHROPIC_DEFAULT_MODEL") == "claude-sonnet-5"

    def test_role_pin_goes_to_anthropic_default_model_not_argv(self, orch: Orchestrator) -> None:
        role_models.set_model("backend", CLAUDE, "claude-opus-5")
        argv, env = _spawn_capture(orch, "backend")
        assert "--model" not in argv
        assert env.get("ANTHROPIC_DEFAULT_MODEL") == "claude-opus-5"

    def test_provider_level_pin_goes_to_anthropic_default_model(self, orch: Orchestrator) -> None:
        provider_models.set_model(CLAUDE, "claude-haiku-4-5")
        argv, env = _spawn_capture(orch, "backend")
        assert "--model" not in argv
        assert env.get("ANTHROPIC_DEFAULT_MODEL") == "claude-haiku-4-5"

    def test_role_pin_wins_over_provider_pin(self, orch: Orchestrator) -> None:
        provider_models.set_model(CLAUDE, "claude-haiku-4-5")
        role_models.set_model("backend", CLAUDE, "claude-opus-5")
        _argv, env = _spawn_capture(orch, "backend")
        assert env.get("ANTHROPIC_DEFAULT_MODEL") == "claude-opus-5"

    def test_explicit_assign_override_still_uses_argv_model_flag(self, orch: Orchestrator) -> None:
        # A deliberate one-off --model on this assign must beat every pin and
        # must NOT also set ANTHROPIC_DEFAULT_MODEL (single mechanism per spawn).
        role_models.set_model("backend", CLAUDE, "claude-opus-5")
        orch._ps(_exit_key(_PROJECT, "backend")).model_override = "claude-haiku-4-5"
        argv, env = _spawn_capture(orch, "backend")
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "claude-haiku-4-5"
        assert "ANTHROPIC_DEFAULT_MODEL" not in env

    def test_teammate_model_env_force_still_uses_argv_model_flag(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TAKKUB_TEAMMATE_MODEL", "claude-haiku-4-5")
        argv, env = _spawn_capture(orch, "backend")
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "claude-haiku-4-5"
        assert "ANTHROPIC_DEFAULT_MODEL" not in env


class TestLeadDefaultModelPin:
    def test_lead_with_no_pin_gets_neither_flag_nor_env(self, orch: Orchestrator) -> None:
        # Lead has no tier default (rides the user's own default) and Max
        # plan_tier means _lead_model_override() returns None too.
        argv, env = _spawn_capture(orch, "lead")
        assert "--model" not in argv
        assert "ANTHROPIC_DEFAULT_MODEL" not in env

    def test_lead_role_pin_goes_to_anthropic_default_model_not_argv(
        self, orch: Orchestrator
    ) -> None:
        role_models.set_model("lead", CLAUDE, "claude-opus-5")
        argv, env = _spawn_capture(orch, "lead")
        assert "--model" not in argv
        assert env.get("ANTHROPIC_DEFAULT_MODEL") == "claude-opus-5"
