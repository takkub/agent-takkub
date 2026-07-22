"""Per-provider model persistence, CLI surface, and spawn argv integration."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import provider_models
from agent_takkub.orchestrator import Orchestrator

TEST_PROJECT = "providermodeltest"


@pytest.fixture(autouse=True)
def isolated_models(monkeypatch, tmp_path) -> Path:
    path = tmp_path / "provider-models.json"
    monkeypatch.setattr(provider_models, "_PATH", path)
    return path


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


class TestProviderModelConfig:
    def test_round_trip_set_get_clear_and_all(self, isolated_models) -> None:
        assert provider_models.model_for("codex") is None

        provider_models.set_model("codex", "  gpt-6-codex  ")
        provider_models.set_model("kimi", "k2.5")

        assert provider_models.model_for("codex") == "gpt-6-codex"
        assert provider_models.all_models() == {
            "codex": "gpt-6-codex",
            "kimi": "k2.5",
        }

        provider_models.clear_model("codex")
        assert provider_models.model_for("codex") is None
        assert provider_models.all_models() == {"kimi": "k2.5"}

    def test_unknown_provider_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown provider"):
            provider_models.set_model("not-a-provider", "anything")

    def test_empty_model_clears_existing_value(self) -> None:
        provider_models.set_model("cursor", "composer-2")
        provider_models.set_model("cursor", "   ")

        assert provider_models.model_for("cursor") is None
        assert provider_models.all_models() == {}

    def test_load_drops_unknown_empty_and_non_string_entries(self, isolated_models) -> None:
        isolated_models.write_text(
            json.dumps(
                {
                    "kimi": "  k2.5  ",
                    "retired-provider": "old",
                    "cursor": "  ",
                    "codex": 123,
                }
            ),
            encoding="utf-8",
        )

        assert provider_models.all_models() == {"kimi": "k2.5"}

    def test_write_uses_atomic_tmp_replace(self, isolated_models, monkeypatch) -> None:
        original_replace = Path.replace
        replacements: list[tuple[Path, Path]] = []

        def tracked_replace(source: Path, target: Path) -> Path:
            replacements.append((source, target))
            return original_replace(source, target)

        monkeypatch.setattr(Path, "replace", tracked_replace)

        provider_models.set_model("gemini", "gemini-3-pro")

        assert replacements == [(isolated_models.with_suffix(".json.tmp"), isolated_models)]
        assert isolated_models.exists()
        assert not isolated_models.with_suffix(".json.tmp").exists()


def _make_orchestrator(qapp, monkeypatch) -> Orchestrator:
    monkeypatch.setattr(Orchestrator, "_resolve_project", staticmethod(lambda p: p or TEST_PROJECT))
    orchestrator = Orchestrator()
    orchestrator._idle_watchdog.stop()
    return orchestrator


def _make_pane(role: str) -> MagicMock:
    pane = MagicMock()
    pane.role = MagicMock()
    pane.role.name = role
    pane.session = None
    pane.state = "empty"
    pane._transcript_path = None
    return pane


def _capture_generic_argv(qapp, monkeypatch, tmp_path, provider: str) -> list[str]:
    from agent_takkub import pane_tools_policy as ptp
    from agent_takkub import shared_dev_tools as sdt

    orchestrator = _make_orchestrator(qapp, monkeypatch)
    pane = _make_pane(provider)
    orchestrator._panes_by_project[TEST_PROJECT] = {provider: pane}
    # Canonical names: cursor ships `cursor-agent` (the bare `agent` alias is
    # only the fallback in discovery, since it collides too easily).
    binary = {"cursor": "cursor-agent", "kimi": "kimi"}[provider]
    monkeypatch.setattr(sdt, "SHARED_MCP_FILE", tmp_path / "shared-mcp.json")
    monkeypatch.setattr(ptp, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")
    spawn_calls: list[dict] = []

    with (
        patch.object(orchestrator, "_is_spawn_blocked", return_value=False),
        patch.object(orchestrator, "_final_gate_clear", return_value=True),
        patch("agent_takkub.spawn_engine._cwd_within_project", return_value=True),
        patch("agent_takkub.orchestrator.PtySession") as mock_pty_cls,
        patch("agent_takkub.orchestrator.QTimer.singleShot"),
        patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
        patch("agent_takkub.provider_config.effective_provider_for", return_value=provider),
        patch("shutil.which", side_effect=lambda name: binary if name == binary else None),
        patch("agent_takkub.orchestrator.inject_user_profile_env"),
    ):
        mock_pty = MagicMock()
        mock_pty.spawn.side_effect = lambda **kwargs: spawn_calls.append(kwargs)
        mock_pty_cls.return_value = mock_pty
        pane.attach_session = MagicMock()

        ok, message = orchestrator.spawn(provider, project=TEST_PROJECT)

    assert ok is True, message
    assert spawn_calls
    return spawn_calls[0]["argv"]


class TestGenericProviderSpawnModels:
    def test_cursor_config_appends_model_flag(self, qapp, monkeypatch, tmp_path) -> None:
        provider_models.set_model("cursor", "composer-2")

        argv = _capture_generic_argv(qapp, monkeypatch, tmp_path, "cursor")

        assert argv == ["cursor-agent", "--force", "--model", "composer-2"]

    def test_kimi_without_config_has_no_model_flag(self, qapp, monkeypatch, tmp_path) -> None:
        argv = _capture_generic_argv(qapp, monkeypatch, tmp_path, "kimi")

        assert argv == ["kimi", "--yolo"]
        assert "--model" not in argv


def _capture_claude_argv(qapp, monkeypatch, tmp_path) -> list[str]:
    from agent_takkub.provider_config import CLAUDE

    orchestrator = _make_orchestrator(qapp, monkeypatch)
    pane = _make_pane("backend")
    orchestrator._panes_by_project[TEST_PROJECT] = {"backend": pane}
    spawn_calls: list[dict] = []

    with (
        patch.object(orchestrator, "_is_spawn_blocked", return_value=False),
        patch.object(orchestrator, "_final_gate_clear", return_value=True),
        patch("agent_takkub.spawn_engine._cwd_within_project", return_value=True),
        patch("agent_takkub.orchestrator.PtySession") as mock_pty_cls,
        patch("agent_takkub.orchestrator.QTimer.singleShot"),
        patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
        patch("agent_takkub.orchestrator._build_lead_env", return_value={}),
        patch("agent_takkub.orchestrator.agent_role_dir", return_value=tmp_path),
        patch("agent_takkub.orchestrator.find_claude_executable", return_value="claude"),
        patch("agent_takkub.provider_config.effective_provider_for", return_value=CLAUDE),
        patch("agent_takkub.orchestrator.inject_user_profile_env"),
        patch("agent_takkub.orchestrator.apply_claude_auth_overrides"),
        patch("agent_takkub.orchestrator._default_plugin_dirs", return_value=[]),
        patch("agent_takkub.hook_wiring.ensure_hook_settings_file", return_value="hooks.json"),
        patch("agent_takkub.mcp_bridge.mcp_argv_for_provider", return_value=[]),
    ):
        mock_pty = MagicMock()
        mock_pty.spawn.side_effect = lambda **kwargs: spawn_calls.append(kwargs)
        mock_pty_cls.return_value = mock_pty
        pane.attach_session = MagicMock()

        ok, message = orchestrator.spawn("backend", cwd=str(tmp_path), project=TEST_PROJECT)

    assert ok is True, message
    assert spawn_calls
    return spawn_calls[0]["argv"]


def _model_arg(argv: list[str]) -> str | None:
    if "--model" not in argv:
        return None
    return argv[argv.index("--model") + 1]


class TestClaudeTeammateModelPrecedence:
    def test_config_wins_over_tier_when_env_unset(self, qapp, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("TAKKUB_TEAMMATE_MODEL", raising=False)
        provider_models.set_model("claude", "claude-custom")

        assert _model_arg(_capture_claude_argv(qapp, monkeypatch, tmp_path)) == "claude-custom"

    def test_explicit_empty_env_keeps_no_model_behavior(self, qapp, monkeypatch, tmp_path) -> None:
        provider_models.set_model("claude", "claude-custom")
        monkeypatch.setenv("TAKKUB_TEAMMATE_MODEL", "")

        assert _model_arg(_capture_claude_argv(qapp, monkeypatch, tmp_path)) is None

    def test_nonempty_env_wins_over_config(self, qapp, monkeypatch, tmp_path) -> None:
        provider_models.set_model("claude", "claude-config")
        monkeypatch.setenv("TAKKUB_TEAMMATE_MODEL", "claude-env")

        assert _model_arg(_capture_claude_argv(qapp, monkeypatch, tmp_path)) == "claude-env"


class TestProviderModelCli:
    @staticmethod
    def _args(model=None, *, clear=False) -> SimpleNamespace:
        return SimpleNamespace(provider_cmd="model", name="kimi", model=model, clear=clear)

    def test_model_get_set_clear(self, capsys) -> None:
        from agent_takkub import cli

        result = cli.cmd_provider(self._args("  k2.5  "))
        assert result == {"ok": True, "msg": "kimi model: k2.5"}
        assert provider_models.model_for("kimi") == "k2.5"

        result = cli.cmd_provider(self._args())
        assert result == {"ok": True, "msg": "kimi model: k2.5"}

        result = cli.cmd_provider(self._args(clear=True))
        assert result == {"ok": True, "msg": "kimi model cleared (provider default)"}
        assert provider_models.model_for("kimi") is None
        assert "kimi model: k2.5" in capsys.readouterr().out

    def test_provider_list_appends_configured_model(self, capsys) -> None:
        from agent_takkub import cli

        provider_models.set_model("kimi", "k2.5")
        with patch("agent_takkub.provider_install._discover", return_value="kimi"):
            result = cli.cmd_provider(SimpleNamespace(provider_cmd="list"))

        assert result["ok"] is True
        output = capsys.readouterr().out
        assert "kimi" in output
        assert "· model: k2.5" in output
