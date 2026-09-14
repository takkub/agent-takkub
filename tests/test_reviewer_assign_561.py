"""Regression tests for #561:
- Add --mode code|e2e|ui to takkub assign (cli.py -> cli_server -> orchestrator.assign)
- Default mode is code for reviewer; reject --mode on other roles (unless pane/subagent)
- Dispatch reviewer --mode e2e onto qa runtime path and --mode ui onto critic runtime path
- Keep qa/critic working as deprecated aliases with CLI warning
- team_preset can_spawn regression test through assign
- Codex diff comparison routed to reviewer on codex
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import cli, orchestrator, routing_planner, team_preset


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def mock_orch(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> orchestrator.Orchestrator:
    monkeypatch.setattr(
        orchestrator.Orchestrator,
        "_resolve_project",
        staticmethod(lambda project: project or "test-proj"),
    )
    orch = orchestrator.Orchestrator()
    orch.shutdown_timers()
    monkeypatch.setattr(orch, "_defer", lambda _delay, fn: fn())
    return orch


class TestReviewerCliAssign:
    def test_reviewer_mode_choices_in_cli_main(self, monkeypatch: pytest.MonkeyPatch):
        sent = []
        monkeypatch.setattr(cli, "_request", lambda req: sent.append(req) or {"ok": True})
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        monkeypatch.delenv("TAKKUB_PROJECT", raising=False)

        cli.main(["assign", "--role", "reviewer", "--mode", "e2e", "test task"])
        assert sent[-1]["mode"] == "e2e"

        cli.main(["assign", "--role", "reviewer", "--mode", "ui", "ui review"])
        assert sent[-1]["mode"] == "ui"

        cli.main(["assign", "--role", "reviewer", "--mode", "code", "code review"])
        assert sent[-1]["mode"] == "code"

    def test_non_reviewer_role_rejects_reviewer_modes(self):
        args = argparse.Namespace(
            role="backend",
            mode="e2e",
            task="do e2e",
            cwd=None,
            shards=1,
            plan=False,
        )
        res = cli.cmd_assign(args)
        assert res["ok"] is False
        assert "--mode e2e is only valid for --role reviewer" in res["msg"]

    def test_reviewer_defaults_to_code_mode(self):
        args = argparse.Namespace(
            role="reviewer",
            mode=None,
            task="review auth",
            cwd=None,
            shards=1,
            plan=False,
        )
        with patch("agent_takkub.cli._request", return_value={"ok": True}) as mock_req:
            res = cli.cmd_assign(args)
            assert res["ok"] is True
            payload = mock_req.call_args[0][0]
            assert payload["mode"] == "code"

    def test_qa_deprecation_warning_on_cli(self, capsys):
        args = argparse.Namespace(
            role="qa",
            mode=None,
            task="test login",
            cwd=None,
            shards=1,
            plan=False,
        )
        with patch("agent_takkub.cli._request", return_value={"ok": True}):
            cli.cmd_assign(args)
        captured = capsys.readouterr()
        assert (
            "warn: --role qa is deprecated (#513/#561); use --role reviewer --mode e2e instead"
            in captured.err
        )

    def test_critic_deprecation_warning_on_cli(self, capsys):
        args = argparse.Namespace(
            role="critic",
            mode=None,
            task="review ui",
            cwd=None,
            shards=1,
            plan=False,
        )
        with patch("agent_takkub.cli._request", return_value={"ok": True}):
            cli.cmd_assign(args)
        captured = capsys.readouterr()
        assert (
            "warn: --role critic is deprecated (#513/#561); use --role reviewer --mode ui instead"
            in captured.err
        )

    def test_browser_shard_warning_for_reviewer_e2e(self):
        # reviewer with mode=e2e triggers browser shard warning
        warn_e2e = cli._browser_shard_warning("reviewer", 2, mode="e2e")
        assert "shard เปิดเบราว์เซอร์อาจไม่ได้" in warn_e2e

        # reviewer with mode=code does NOT trigger warning
        warn_code = cli._browser_shard_warning("reviewer", 2, mode="code")
        assert warn_code == ""


class TestReviewerOrchestratorDispatch:
    def test_reviewer_e2e_dispatches_to_qa(self, mock_orch: orchestrator.Orchestrator):
        dispatched_roles = []
        with patch.object(
            mock_orch,
            "_assign_dispatch",
            side_effect=lambda role_name, *args, **kwargs: (
                dispatched_roles.append(role_name) or (True, "dispatched")
            ),
        ):
            ok, _ = mock_orch.assign(
                role_name="reviewer",
                cwd=None,
                task="test login flow",
                mode="e2e",
                project="test-proj",
            )
            assert ok is True
            assert dispatched_roles == ["qa"]

    def test_reviewer_ui_dispatches_to_critic(self, mock_orch: orchestrator.Orchestrator):
        dispatched_roles = []
        with patch.object(
            mock_orch,
            "_assign_dispatch",
            side_effect=lambda role_name, *args, **kwargs: (
                dispatched_roles.append(role_name) or (True, "dispatched")
            ),
        ):
            ok, _ = mock_orch.assign(
                role_name="reviewer",
                cwd=None,
                task="critique layout",
                mode="ui",
                project="test-proj",
            )
            assert ok is True
            assert dispatched_roles == ["critic"]

    def test_reviewer_code_dispatches_to_reviewer(self, mock_orch: orchestrator.Orchestrator):
        dispatched_roles = []
        with patch.object(
            mock_orch,
            "_assign_dispatch",
            side_effect=lambda role_name, *args, **kwargs: (
                dispatched_roles.append(role_name) or (True, "dispatched")
            ),
        ):
            ok, _ = mock_orch.assign(
                role_name="reviewer",
                cwd=None,
                task="review diff",
                mode="code",
                project="test-proj",
            )
            assert ok is True
            assert dispatched_roles == ["reviewer"]

    def test_reviewer_e2e_sharded_dispatches_to_qa_shard(
        self, mock_orch: orchestrator.Orchestrator
    ):
        dispatched_roles = []
        with patch.object(
            mock_orch,
            "_assign_dispatch",
            side_effect=lambda role_name, *args, **kwargs: (
                dispatched_roles.append(role_name) or (True, "dispatched")
            ),
        ):
            ok, _ = mock_orch.assign(
                role_name="reviewer#2",
                cwd=None,
                task="test login shard",
                mode="e2e",
                project="test-proj",
            )
            assert ok is True
            assert dispatched_roles == ["qa#2"]

    def test_reviewer_e2e_plan_matches_qa_plan(self, mock_orch: orchestrator.Orchestrator):
        dispatched_calls = []
        with patch.object(
            mock_orch,
            "_assign_dispatch",
            side_effect=lambda role_name, *args, **kwargs: (
                dispatched_calls.append((role_name, kwargs)) or (True, "dispatched")
            ),
        ):
            ok, _ = mock_orch.assign(
                role_name="reviewer",
                cwd="/web",
                task="e2e 5 หน้า",
                mode="e2e",
                plan=True,
                shard_total=3,
                project="test-proj",
            )
            assert ok is True
            assert len(dispatched_calls) == 1
            role, kw = dispatched_calls[0]
            assert role == "qa"
            assert kw.get("plan") is True
            assert kw.get("shard_total") == 3

    def test_invalid_mode_on_reviewer_rejected(self, mock_orch: orchestrator.Orchestrator):
        ok, msg = mock_orch.assign(
            role_name="reviewer",
            cwd=None,
            task="task",
            mode="invalid_mode",
            project="test-proj",
        )
        assert ok is False
        assert "mode for reviewer must be code, e2e, or ui" in msg

    def test_reviewer_mode_on_other_role_rejected(self, mock_orch: orchestrator.Orchestrator):
        ok, msg = mock_orch.assign(
            role_name="backend",
            cwd=None,
            task="task",
            mode="e2e",
            project="test-proj",
        )
        assert ok is False
        assert "--mode e2e is only valid for --role reviewer" in msg


class TestTeamPresetCanSpawnSeam:
    def test_can_spawn_canonical_alias(self):
        # checker="qa" satisfies can_spawn("qa") (identity) but must NOT
        # satisfy bare can_spawn("reviewer") (mode=code) — qa/e2e and
        # reviewer/code are different checking contracts even though #561
        # dispatches both through the `reviewer` CLI surface (asymmetric by
        # design — see the comment in team_preset.can_spawn).
        with patch(
            "agent_takkub.team_preset.current",
            return_value={"preset": "duo", "roles": {}, "checker": "qa"},
        ):
            ok, _ = team_preset.can_spawn("qa", "test-proj")
            assert ok is True
            ok, _ = team_preset.can_spawn("reviewer", "test-proj")
            assert ok is False

        # When preset config sets checker to "reviewer", can_spawn("qa") must succeed
        # (b7248a11: qa/e2e satisfies a canonical "reviewer" checker slot).
        with patch(
            "agent_takkub.team_preset.current",
            return_value={"preset": "duo", "roles": {}, "checker": "reviewer"},
        ):
            ok, _ = team_preset.can_spawn("qa", "test-proj")
            assert ok is True

        # critic is an exempt role (never governed)
        ok, _ = team_preset.can_spawn("critic", "test-proj")
        assert ok is True


class TestCodexDiffRouting:
    def test_thai_codex_diff(self):
        act = routing_planner.classify("ให้ codex เทียบ diff")
        assert act.kind == routing_planner.ActionKind.FIRE_ASSIGN
        assert act.role == "reviewer"
        assert act.mode == "code"
        assert act.provider == "codex"

    def test_thai_codex_review_diff(self):
        act = routing_planner.classify("ให้ codex ช่วย review diff")
        assert act.kind == routing_planner.ActionKind.FIRE_ASSIGN
        assert act.role == "reviewer"
        assert act.mode == "code"
        assert act.provider == "codex"

    def test_english_codex_compare_diff(self):
        act = routing_planner.classify("compare diff with codex")
        assert act.kind == routing_planner.ActionKind.FIRE_ASSIGN
        assert act.role == "reviewer"
        assert act.mode == "code"
        assert act.provider == "codex"

    def test_codex_diff_disabled_reviewer(self):
        act = routing_planner.classify(
            "ให้ codex เทียบ diff", context={"disabled_roles": {"reviewer"}}
        )
        assert act.kind == routing_planner.ActionKind.INFORMATIONAL
        assert "reviewer" in act.reason
        assert "ถูกปิด" in act.reason or "disabled" in act.reason


class TestQaCriticSpawnResolvesReviewerSettingsRole590:
    """#590 end-to-end regression: a `qa`/`critic` pane's actual spawn()
    call must resolve provider/model/effort against `reviewer`'s Settings
    entry (the only row the roster renders), not its own stale/invisible
    key — unless a custom preset's checker is explicitly `qa` itself."""

    @staticmethod
    def _spawn_role(
        orch: orchestrator.Orchestrator,
        tmp_path: Path,
        role: str,
        *,
        provider_side_effect,
    ) -> list[str]:
        """Drives a real `spawn()` call for *role* with every claude-branch
        side effect mocked out (mirrors test_provider_override.py's
        `_spawn_backend`), recording which role name each
        `effective_provider_for` call actually received."""
        pane = MagicMock()
        pane.state = "empty"
        pane.session = None
        pane._transcript_path = None
        orch._panes_by_project["test-proj"] = {role: pane}
        staging = tmp_path / "role"
        staging.mkdir(exist_ok=True)
        (staging / "CLAUDE.md").write_text("# role\n", encoding="utf-8")

        seen_roles: list[str] = []

        def _record(role_arg, project=None):
            seen_roles.append(role_arg)
            return provider_side_effect(role_arg)

        stack = [
            patch.object(orch, "_is_spawn_blocked", return_value=False),
            patch.object(orch, "_final_gate_clear", return_value=True),
            patch("agent_takkub.orchestrator.PtySession"),
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
            patch("agent_takkub.orchestrator.find_claude_executable", return_value="claude"),
            patch(
                "agent_takkub.provider_config.effective_provider_for",
                side_effect=_record,
            ),
            # #590 follow-up CI fix: a CI runner has neither codex nor the
            # Antigravity CLI (`agy`) installed, so the real binary-discovery
            # probe `spawn_engine.py` runs before argv-building
            # (`spec.custom_discovery_fn()`) returns None and spawn() fails
            # closed with "codex/agy binary not on PATH" before this test's
            # own assertions (which only care which ROLE's Settings row
            # `effective_provider_for` was resolved against, not whether a
            # real CLI is installed) ever run — a dev machine with codex/agy
            # on PATH masked this (#587 C3's same class of gap).
            patch("agent_takkub.codex_helper.find_codex_executable", return_value="codex"),
            patch("agent_takkub.gemini_helper.find_agy_executable", return_value="agy"),
            patch("agent_takkub.spawn_engine.agent_role_dir", return_value=staging),
            patch("agent_takkub.spawn_engine._cwd_within_project", return_value=True),
            patch("agent_takkub.spawn_engine._default_plugin_dirs", return_value=[]),
            patch("agent_takkub.spawn_engine.inject_user_profile_env"),
            patch("agent_takkub.spawn_engine.apply_claude_auth_overrides"),
            patch("agent_takkub.mcp_bridge.mcp_argv_for_provider", return_value=[]),
            patch(
                "agent_takkub.hook_wiring.ensure_hook_settings_file",
                return_value="hooks.json",
            ),
        ]
        entered = [ctx.__enter__() for ctx in stack]
        try:
            mock_pty = MagicMock()
            mock_pty.spawn.side_effect = lambda **kwargs: None
            entered[2].return_value = mock_pty
            pane.attach_session = MagicMock()

            ok, message = orch.spawn(role, cwd=str(tmp_path), project="test-proj")
            assert ok is True, message
        finally:
            for ctx in reversed(stack):
                ctx.__exit__(None, None, None)
        return seen_roles

    def test_qa_pane_resolves_against_reviewer_row_under_full_preset(
        self, mock_orch: orchestrator.Orchestrator, tmp_path: Path
    ) -> None:
        team_preset.set_current("full", "test-proj")

        seen = self._spawn_role(
            mock_orch,
            tmp_path,
            "qa",
            provider_side_effect=lambda role: "codex" if role == "reviewer" else "gemini",
        )

        assert "reviewer" in seen
        assert "qa" not in seen

    def test_critic_pane_resolves_against_reviewer_row(
        self, mock_orch: orchestrator.Orchestrator, tmp_path: Path
    ) -> None:
        team_preset.set_current("full", "test-proj")

        seen = self._spawn_role(
            mock_orch,
            tmp_path,
            "critic",
            provider_side_effect=lambda role: "codex" if role == "reviewer" else "gemini",
        )

        assert "reviewer" in seen
        assert "critic" not in seen

    def test_qa_pane_keeps_own_row_when_custom_checker_is_qa(
        self, mock_orch: orchestrator.Orchestrator, tmp_path: Path
    ) -> None:
        team_preset.set_current(
            "custom",
            "test-proj",
            custom={"roles": dict.fromkeys(team_preset.CORE_POSITION_ROLES, True), "checker": "qa"},
        )

        seen = self._spawn_role(
            mock_orch,
            tmp_path,
            "qa",
            provider_side_effect=lambda role: "codex" if role == "reviewer" else "gemini",
        )

        assert seen == ["qa"]

    def test_assign_result_line_and_event_report_reviewer_row_source(
        self, mock_orch: orchestrator.Orchestrator
    ) -> None:
        """#590 item D: `takkub assign --role reviewer --mode e2e` (qa's
        canonical form) must say which row actually backed the provider,
        and the `assign` log event must carry `provider_source`."""
        team_preset.set_current("full", "test-proj")
        events: list[tuple[str, dict]] = []

        def _fake_effective_provider_for(role, project=None):
            return "codex" if role == "reviewer" else "gemini"

        def _record_event(name, **fields):
            events.append((name, fields))

        with (
            patch.object(mock_orch, "spawn", return_value=(True, "spawned")),
            patch.object(mock_orch, "_send_when_ready"),
            patch(
                "agent_takkub.provider_config.effective_provider_for",
                side_effect=_fake_effective_provider_for,
            ),
            patch("agent_takkub.orchestrator._log_event", side_effect=_record_event),
        ):
            ok, msg = mock_orch.assign(
                role_name="reviewer",
                cwd="/web",
                task="test login flow",
                mode="e2e",
                project="test-proj",
            )

        assert ok is True
        assert "qa = reviewer --mode e2e" in msg
        assert "provider codex" in msg
        assert "ตามแถว Reviewer" in msg

        assign_events = [fields for name, fields in events if name == "assign"]
        assert len(assign_events) == 1
        assert assign_events[0]["provider_source"] == "reviewer_row"
        assert assign_events[0]["effective_provider"] == "codex"

    def test_assign_result_line_reports_own_row_when_checker_is_qa(
        self, mock_orch: orchestrator.Orchestrator
    ) -> None:
        team_preset.set_current(
            "custom",
            "test-proj",
            custom={"roles": dict.fromkeys(team_preset.CORE_POSITION_ROLES, True), "checker": "qa"},
        )
        events: list[tuple[str, dict]] = []

        def _fake_effective_provider_for(role, project=None):
            return "codex" if role == "reviewer" else "gemini"

        def _record_event(name, **fields):
            events.append((name, fields))

        with (
            patch.object(mock_orch, "spawn", return_value=(True, "spawned")),
            patch.object(mock_orch, "_send_when_ready"),
            patch(
                "agent_takkub.provider_config.effective_provider_for",
                side_effect=_fake_effective_provider_for,
            ),
            patch("agent_takkub.orchestrator._log_event", side_effect=_record_event),
        ):
            ok, msg = mock_orch.assign(
                role_name="qa",
                cwd="/web",
                task="test login flow",
                project="test-proj",
            )

        assert ok is True
        assert "provider gemini" in msg
        assert "ตามแถว QA" in msg
        assign_events = [fields for name, fields in events if name == "assign"]
        assert assign_events[0]["provider_source"] == "own_row"
