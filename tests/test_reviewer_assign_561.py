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
from unittest.mock import patch

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
        # When preset config sets checker to "qa", can_spawn("reviewer") must succeed
        with patch(
            "agent_takkub.team_preset.current",
            return_value={"preset": "duo", "roles": {}, "checker": "qa"},
        ):
            ok, _ = team_preset.can_spawn("reviewer", "test-proj")
            assert ok is True

        # When preset config sets checker to "reviewer", can_spawn("qa") must succeed
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
