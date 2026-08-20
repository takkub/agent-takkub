"""Per-provider model persistence, CLI surface, and spawn argv integration."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import provider_models, role_models
from agent_takkub.orchestrator import Orchestrator

TEST_PROJECT = "providermodeltest"


@pytest.fixture(autouse=True)
def isolated_models(monkeypatch, tmp_path) -> Path:
    path = tmp_path / "provider-models.json"
    monkeypatch.setattr(provider_models, "_PATH", path)
    monkeypatch.setattr(role_models, "_PATH", tmp_path / "role-models.json")
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


def _capture_generic_argv(
    qapp,
    monkeypatch,
    tmp_path,
    provider: str,
    *,
    model_override: str | None = None,
    effort_override: str | None = None,
    role: str | None = None,
    gemini_project_id: str | None = None,
) -> list[str]:
    from agent_takkub import pane_tools_policy as ptp
    from agent_takkub import shared_dev_tools as sdt

    orchestrator = _make_orchestrator(qapp, monkeypatch)
    spawn_role = role or provider
    pane = _make_pane(spawn_role)
    orchestrator._panes_by_project[TEST_PROJECT] = {spawn_role: pane}
    ps = orchestrator._ps(f"{TEST_PROJECT}::{spawn_role}")
    ps.model_override = model_override
    ps.effort_override = effort_override
    # Canonical names: cursor ships `cursor-agent` (the bare `agent` alias is
    # only the fallback in discovery, since it collides too easily).
    binary = {
        "codex": "codex",
        "cursor": "cursor-agent",
        "gemini": "agy",
        "kimi": "kimi",
        "opencode": "opencode",
    }[provider]
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
        patch("agent_takkub.spawn_engine.sys.platform", "win32"),
        patch("agent_takkub.codex_helper.find_codex_executable", return_value=binary),
        patch("agent_takkub.gemini_helper.find_agy_executable", return_value=binary),
        patch(
            "agent_takkub.gemini_helper.resolve_agy_project_id",
            return_value=gemini_project_id,
        ),
        patch("shutil.which", side_effect=lambda name: binary if name == binary else None),
        patch("agent_takkub.codex_agents_md.ensure_agents_md"),
        patch("agent_takkub.orchestrator.inject_user_profile_env"),
    ):
        mock_pty = MagicMock()
        mock_pty.spawn.side_effect = lambda **kwargs: spawn_calls.append(kwargs)
        mock_pty_cls.return_value = mock_pty
        pane.attach_session = MagicMock()

        ok, message = orchestrator.spawn(spawn_role, project=TEST_PROJECT)

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

    def test_assign_override_wins_over_role_and_provider_models(
        self, qapp, monkeypatch, tmp_path
    ) -> None:
        role_models.set_model("cursor", "cursor", "role-model")
        provider_models.set_model("cursor", "provider-model")

        argv = _capture_generic_argv(
            qapp,
            monkeypatch,
            tmp_path,
            "cursor",
            model_override="assign-model",
        )

        assert argv == ["cursor-agent", "--force", "--model", "assign-model"]

    def test_role_model_still_wins_over_provider_model(self, qapp, monkeypatch, tmp_path) -> None:
        role_models.set_model("cursor", "cursor", "role-model")
        provider_models.set_model("cursor", "provider-model")

        argv = _capture_generic_argv(qapp, monkeypatch, tmp_path, "cursor")

        assert argv == ["cursor-agent", "--force", "--model", "role-model"]


class TestProviderEffortSpecs:
    def test_codex_keeps_config_backed_effort_surface(self) -> None:
        from agent_takkub.provider_spec import codex_spec

        assert codex_spec.effort_flag == "-c"
        assert codex_spec.effort_config_key == "model_reasoning_effort"

    def test_unsupported_providers_remain_explicit(self) -> None:
        from agent_takkub.provider_spec import cursor_spec, kimi_spec, opencode_spec

        assert opencode_spec.effort_flag is None
        assert kimi_spec.effort_flag is None
        assert cursor_spec.effort_flag is None

    def test_gemini_gains_effort_flag(self) -> None:
        # #323 follow-up: agy's #125 silent-model-swap regression is fixed
        # upstream (agy 1.1.10+) — see gemini_spec's own comment.
        from agent_takkub.provider_spec import gemini_spec

        assert gemini_spec.effort_flag == "--effort"
        assert gemini_spec.effort_levels == ("low", "medium", "high")


_GEMINI_SCOPE_CASES = pytest.mark.parametrize(
    "gemini_project_id,expected_scope_tail",
    [
        pytest.param(None, ["--new-project"], id="no-match"),
        pytest.param(
            "17fdc03a-8cb3-446e-a833-4aaffc55f6bb",
            ["--project", "17fdc03a-8cb3-446e-a833-4aaffc55f6bb"],
            id="matched",
        ),
    ],
)


class TestGenericProviderSpawnEffort:
    # #323 follow-up: agy's #125 silent-model-swap regression (mismatched
    # --model/--effort silently discarding the explicit --model) is fixed
    # upstream in agy 1.1.10+ — re-verified live against the installed 1.1.15
    # binary (docs/reviews/2026-08-20-323-agy-effort-restored.md). gemini now
    # goes through the same generic effort path claude/codex already use, so
    # these two tests assert --effort IS present instead of absent. The model
    # override below (gemini-3.1-pro-high) intentionally MATCHES the "high"
    # tier effort — Takkub does not itself cross-validate an explicit --model
    # against the resolved effort before spawn (see gemini_spec's comment on
    # the residual conflict case), so a mismatched pair would still reach
    # argv here and fail only when agy itself parses it.
    @_GEMINI_SCOPE_CASES
    def test_gemini_model_override_now_gets_matching_effort(
        self, qapp, monkeypatch, tmp_path, gemini_project_id, expected_scope_tail
    ) -> None:
        monkeypatch.setenv("TAKKUB_TEAMMATE_EFFORT", "high")

        argv = _capture_generic_argv(
            qapp,
            monkeypatch,
            tmp_path,
            "gemini",
            model_override="gemini-3.1-pro-high",
            role="backend",
            gemini_project_id=gemini_project_id,
        )

        # #132: project scoping always appends --project/--new-project after
        # the effort flag — pin the resolver (via gemini_project_id) so this
        # stays deterministic instead of reading the real ~/.gemini registry.
        assert argv == [
            "agy",
            "--dangerously-skip-permissions",
            "--model",
            "gemini-3.1-pro-high",
            "--effort",
            "high",
            *expected_scope_tail,
        ]

    @_GEMINI_SCOPE_CASES
    def test_gemini_without_model_override_now_gets_tier_effort(
        self, qapp, monkeypatch, tmp_path, gemini_project_id, expected_scope_tail
    ) -> None:
        monkeypatch.setenv("TAKKUB_TEAMMATE_EFFORT", "high")

        argv = _capture_generic_argv(
            qapp,
            monkeypatch,
            tmp_path,
            "gemini",
            role="backend",
            gemini_project_id=gemini_project_id,
        )

        assert argv == [
            "agy",
            "--dangerously-skip-permissions",
            "--effort",
            "high",
            *expected_scope_tail,
        ]

    def test_codex_uses_session_config_override(self, qapp, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("TAKKUB_TEAMMATE_EFFORT", "low")

        with patch(
            "agent_takkub.mcp_bridge.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="[]", stderr=""),
        ):
            argv = _capture_generic_argv(qapp, monkeypatch, tmp_path, "codex")

        effort_idx = argv.index("model_reasoning_effort=low")
        assert argv[effort_idx - 1 : effort_idx + 1] == [
            "-c",
            "model_reasoning_effort=low",
        ]

    def test_explicit_empty_env_disables_effort(self, qapp, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("TAKKUB_TEAMMATE_EFFORT", "")

        argv = _capture_generic_argv(qapp, monkeypatch, tmp_path, "gemini")

        assert "--effort" not in argv

    def test_unsupported_provider_gets_no_effort_arg(self, qapp, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("TAKKUB_TEAMMATE_EFFORT", "high")

        argv = _capture_generic_argv(qapp, monkeypatch, tmp_path, "opencode")

        assert argv == ["opencode", "--auto"]

    def test_assign_effort_override_wins_over_env_and_tier(
        self, qapp, monkeypatch, tmp_path
    ) -> None:
        # Issue #323: `takkub assign --effort low` on a role whose tier
        # default and TAKKUB_TEAMMATE_EFFORT both say "high" — the per-assign
        # value must reach codex's actual spawn argv.
        monkeypatch.setenv("TAKKUB_TEAMMATE_EFFORT", "high")

        with patch(
            "agent_takkub.mcp_bridge.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="[]", stderr=""),
        ):
            argv = _capture_generic_argv(
                qapp, monkeypatch, tmp_path, "codex", effort_override="low"
            )

        effort_idx = argv.index("model_reasoning_effort=low")
        assert argv[effort_idx - 1 : effort_idx + 1] == ["-c", "model_reasoning_effort=low"]

    def test_assign_effort_override_ignored_for_unsupported_provider(
        self, qapp, monkeypatch, tmp_path
    ) -> None:
        # opencode has no effort_flag at all (#103 gap) — a per-assign
        # override must degrade silently, same as every other precedence
        # layer, not invent a CLI flag the provider doesn't accept.
        argv = _capture_generic_argv(
            qapp, monkeypatch, tmp_path, "opencode", role="backend", effort_override="high"
        )

        assert "--effort" not in argv

    def test_assign_effort_override_reaches_gemini_argv(self, qapp, monkeypatch, tmp_path) -> None:
        # #323 follow-up: gemini/agy now has a real effort_flag (#125's
        # silent-model-swap regression is fixed upstream, agy 1.1.10+) — a
        # per-assign override must win over env/tier same as codex/claude.
        monkeypatch.setenv("TAKKUB_TEAMMATE_EFFORT", "medium")

        argv = _capture_generic_argv(
            qapp,
            monkeypatch,
            tmp_path,
            "gemini",
            role="backend",
            effort_override="low",
            gemini_project_id="17fdc03a-8cb3-446e-a833-4aaffc55f6bb",
        )

        effort_idx = argv.index("--effort")
        assert argv[effort_idx : effort_idx + 2] == ["--effort", "low"]


def _capture_claude_argv(
    qapp,
    monkeypatch,
    tmp_path,
    *,
    model_override: str | None = None,
    effort_override: str | None = None,
) -> list[str]:
    from agent_takkub.provider_config import CLAUDE

    orchestrator = _make_orchestrator(qapp, monkeypatch)
    pane = _make_pane("backend")
    orchestrator._panes_by_project[TEST_PROJECT] = {"backend": pane}
    ps = orchestrator._ps(f"{TEST_PROJECT}::backend")
    ps.model_override = model_override
    ps.effort_override = effort_override
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


def _capture_claude_spawn(
    qapp,
    monkeypatch,
    tmp_path,
    *,
    model_override: str | None = None,
) -> dict:
    """Like `_capture_claude_argv` but returns the full spawn() kwargs (argv
    AND env) — #318 needs both since a persistent model pin now rides
    ANTHROPIC_DEFAULT_MODEL in env instead of the --model argv flag."""
    from agent_takkub.provider_config import CLAUDE

    orchestrator = _make_orchestrator(qapp, monkeypatch)
    pane = _make_pane("backend")
    orchestrator._panes_by_project[TEST_PROJECT] = {"backend": pane}
    orchestrator._ps(f"{TEST_PROJECT}::backend").model_override = model_override
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
    return spawn_calls[0]


def _model_arg(argv: list[str]) -> str | None:
    if "--model" not in argv:
        return None
    return argv[argv.index("--model") + 1]


class TestClaudeTeammateModelPrecedence:
    def test_claude_keeps_role_tier_effort(self, qapp, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("TAKKUB_TEAMMATE_EFFORT", raising=False)

        argv = _capture_claude_argv(qapp, monkeypatch, tmp_path)

        effort_idx = argv.index("--effort")
        assert argv[effort_idx : effort_idx + 2] == ["--effort", "high"]

    def test_assign_override_wins_over_role_provider_and_env(
        self, qapp, monkeypatch, tmp_path
    ) -> None:
        role_models.set_model("backend", "claude", "claude-role")
        provider_models.set_model("claude", "claude-provider")
        monkeypatch.setenv("TAKKUB_TEAMMATE_MODEL", "claude-env")

        spawn_kwargs = _capture_claude_spawn(
            qapp,
            monkeypatch,
            tmp_path,
            model_override="claude-assign",
        )

        assert _model_arg(spawn_kwargs["argv"]) == "claude-assign"
        # Single mechanism per spawn (#318): an explicit override never also
        # sets the ANTHROPIC_DEFAULT_MODEL pin.
        assert "ANTHROPIC_DEFAULT_MODEL" not in spawn_kwargs["env"]

    def test_config_wins_over_tier_when_env_unset(self, qapp, monkeypatch, tmp_path) -> None:
        # #318: a persistent pin (no explicit override, no TAKKUB_TEAMMATE_MODEL
        # force) rides ANTHROPIC_DEFAULT_MODEL, not --model — see
        # docs/audit/2026-08-20-issue-318-*.md for why.
        monkeypatch.delenv("TAKKUB_TEAMMATE_MODEL", raising=False)
        provider_models.set_model("claude", "claude-custom")

        spawn_kwargs = _capture_claude_spawn(qapp, monkeypatch, tmp_path)
        assert _model_arg(spawn_kwargs["argv"]) is None
        assert spawn_kwargs["env"].get("ANTHROPIC_DEFAULT_MODEL") == "claude-custom"

    def test_explicit_empty_env_keeps_no_model_behavior(self, qapp, monkeypatch, tmp_path) -> None:
        provider_models.set_model("claude", "claude-custom")
        monkeypatch.setenv("TAKKUB_TEAMMATE_MODEL", "")

        # An explicitly empty TAKKUB_TEAMMATE_MODEL means "use the Claude CLI
        # default" — it must short-circuit the whole precedence chain, so
        # neither --model nor ANTHROPIC_DEFAULT_MODEL should be set.
        spawn_kwargs = _capture_claude_spawn(qapp, monkeypatch, tmp_path)
        assert _model_arg(spawn_kwargs["argv"]) is None
        assert "ANTHROPIC_DEFAULT_MODEL" not in spawn_kwargs["env"]

    def test_nonempty_env_wins_over_config(self, qapp, monkeypatch, tmp_path) -> None:
        # TAKKUB_TEAMMATE_MODEL is an explicit operator force, same argv-flag
        # tier as an assign-level model_override (#318) — not the persistent
        # ANTHROPIC_DEFAULT_MODEL pin.
        provider_models.set_model("claude", "claude-config")
        monkeypatch.setenv("TAKKUB_TEAMMATE_MODEL", "claude-env")

        spawn_kwargs = _capture_claude_spawn(qapp, monkeypatch, tmp_path)
        assert _model_arg(spawn_kwargs["argv"]) == "claude-env"
        assert "ANTHROPIC_DEFAULT_MODEL" not in spawn_kwargs["env"]

    def test_assign_effort_override_wins_over_role_tier_default(
        self, qapp, monkeypatch, tmp_path
    ) -> None:
        # backend's tier default effort is "high" (test_claude_keeps_role_tier_effort
        # above) — a per-assign --effort must win over it (issue #323).
        monkeypatch.delenv("TAKKUB_TEAMMATE_EFFORT", raising=False)

        argv = _capture_claude_argv(qapp, monkeypatch, tmp_path, effort_override="low")

        effort_idx = argv.index("--effort")
        assert argv[effort_idx : effort_idx + 2] == ["--effort", "low"]


class TestRunningPaneModelOverride:
    def test_override_warns_lead_and_does_not_change_live_pane(self, qapp, monkeypatch) -> None:
        from agent_takkub.provider_config import CLAUDE

        orchestrator = _make_orchestrator(qapp, monkeypatch)
        pane = _make_pane("backend")
        pane.session = MagicMock()
        pane.session.is_alive = True
        orchestrator._panes_by_project[TEST_PROJECT] = {"backend": pane}
        state = orchestrator._ps(f"{TEST_PROJECT}::backend")
        state.model_override = "model-used-at-spawn"

        with (
            patch(
                "agent_takkub.provider_config.effective_provider_for",
                return_value=CLAUDE,
            ),
            patch("agent_takkub.orchestrator._task_handoff_pointer", return_value=("task", None)),
            patch("agent_takkub.task_ledger.create_assignment", return_value=None),
            patch.object(orchestrator, "_notify_lead") as notify,
            patch.object(orchestrator, "_send_when_ready"),
        ):
            ok, message = orchestrator.assign(
                "backend",
                cwd=None,
                task="scan",
                project=TEST_PROJECT,
                model="claude-haiku-4-5",
            )

        assert ok is True, message
        assert state.model_override == "model-used-at-spawn"
        warning = notify.call_args.args[1]
        assert "ไม่มีผล" in warning
        assert "close" in warning


class TestRunningPaneEffortOverride:
    def test_override_warns_lead_and_does_not_change_live_pane(self, qapp, monkeypatch) -> None:
        # Issue #323 — mirrors TestRunningPaneModelOverride: CLI argv can't
        # change after process start, so --effort on an already-running pane
        # must warn and leave the pane's spawned effort untouched.
        from agent_takkub.provider_config import CLAUDE

        orchestrator = _make_orchestrator(qapp, monkeypatch)
        pane = _make_pane("backend")
        pane.session = MagicMock()
        pane.session.is_alive = True
        orchestrator._panes_by_project[TEST_PROJECT] = {"backend": pane}
        state = orchestrator._ps(f"{TEST_PROJECT}::backend")
        state.effort_override = "effort-used-at-spawn"

        with (
            patch(
                "agent_takkub.provider_config.effective_provider_for",
                return_value=CLAUDE,
            ),
            patch("agent_takkub.orchestrator._task_handoff_pointer", return_value=("task", None)),
            patch("agent_takkub.task_ledger.create_assignment", return_value=None),
            patch.object(orchestrator, "_notify_lead") as notify,
            patch.object(orchestrator, "_send_when_ready"),
        ):
            ok, message = orchestrator.assign(
                "backend",
                cwd=None,
                task="scan",
                project=TEST_PROJECT,
                effort="low",
            )

        assert ok is True, message
        assert state.effort_override == "effort-used-at-spawn"
        warning = notify.call_args.args[1]
        assert "ไม่มีผล" in warning
        assert "close" in warning


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
