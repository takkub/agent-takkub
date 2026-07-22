"""Cursor CLI provider registration through the generic #103 spawn path."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import Orchestrator
from agent_takkub.provider_install import installable_providers
from agent_takkub.provider_spec import PROVIDER_REGISTRY, cursor_spec

TEST_PROJECT = "cursortest"


class TestCursorSpec:
    def test_registered_with_display_name(self) -> None:
        assert PROVIDER_REGISTRY["cursor"] is cursor_spec
        assert cursor_spec.display_name == "Cursor"

    def test_binary_names_match_official_cli(self) -> None:
        # Canonical `cursor-agent` first; the bare `agent` alias stays as a
        # fallback but must not win discovery (it collides with unrelated
        # programs of that name).
        assert cursor_spec.binary_names == [
            "cursor-agent",
            "cursor-agent.exe",
            "agent",
            "agent.exe",
        ]

    def test_manual_install_only(self) -> None:
        assert cursor_spec.install_command is None
        assert "cursor" not in installable_providers()

    def test_cross_platform_manual_install_commands_are_documented(self) -> None:
        instructions = cursor_spec.install_instructions
        assert "irm 'https://cursor.com/install?win32=true' | iex" in instructions
        assert "curl https://cursor.com/install -fsS | bash" in instructions

    def test_login_uses_browser_oauth(self) -> None:
        assert "`agent`" in cursor_spec.post_install_note
        assert "browser OAuth" in cursor_spec.post_install_note

    def test_documented_autonomy_flag_is_wired(self) -> None:
        # `-f/--force` ("Force allow commands unless explicitly denied") is the
        # documented full-autonomy flag; without it every shell command stops on
        # a y/n prompt and the pane can't run unattended.
        assert cursor_spec.autonomy_flags == {"default": ["--force"]}

    def test_plants_agents_md_so_teammates_learn_takkub_done(self) -> None:
        # cursor.com/docs/cli/using confirms AGENTS.md/CLAUDE.md are read as rules.
        assert cursor_spec.context_strategy == "agents_md_file"
        assert cursor_spec.cheatsheet_filename == "AGENTS.md"

    def test_uncalibrated_tui_and_resume_remain_explicit_gaps(self) -> None:
        assert cursor_spec.ready_rules == ()
        assert cursor_spec.supports_resume is False

    def test_forced_role(self) -> None:
        from agent_takkub import provider_config

        assert provider_config.CURSOR == "cursor"
        assert provider_config.provider_for("cursor") == "cursor"
        assert "cursor" in provider_config.FORCED_ROLES


class TestCursorManualInstallSurfaces:
    def test_provider_list_describes_manual_install(self, capsys) -> None:
        from agent_takkub import cli

        with patch("agent_takkub.provider_install._discover", return_value=None):
            result = cli.cmd_provider(SimpleNamespace(provider_cmd="list"))

        output = capsys.readouterr().out
        assert result["ok"] is True
        assert "cursor" in output
        assert "not installed  (manual — see takkub doctor)" in output

    def test_doctor_skips_missing_cursor_with_manual_instructions(self) -> None:
        from agent_takkub import doctor

        with (
            patch("agent_takkub.codex_helper.find_codex_executable", return_value=None),
            patch("agent_takkub.gemini_helper.find_agy_executable", return_value=None),
            patch("shutil.which", return_value=None),
        ):
            findings = doctor.check_providers()

        cursor = next(f for f in findings if f.category == "providers" and f.name == "cursor")
        assert cursor.status is doctor.Status.SKIP
        assert cursor.auto_fix is None
        assert "install?win32=true" in cursor.fix_hint
        assert "curl https://cursor.com/install" in cursor.fix_hint


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


def _make_orchestrator(qapp, monkeypatch):
    monkeypatch.setattr(Orchestrator, "_resolve_project", staticmethod(lambda p: p or TEST_PROJECT))
    orchestrator = Orchestrator()
    orchestrator._idle_watchdog.stop()
    return orchestrator


def _make_pane():
    pane = MagicMock()
    pane.role = MagicMock()
    pane.role.name = "cursor"
    pane.session = None
    pane.state = "empty"
    pane._transcript_path = None
    return pane


def test_empty_autonomy_mapping_resolves_to_no_flags() -> None:
    """A spec with no autonomy flags must resolve to an empty list, not raise —
    the generic argv builder splats this result directly."""
    import sys as _sys
    from dataclasses import replace

    bare = replace(cursor_spec, autonomy_flags={})
    assert bare.autonomy_flags.get(_sys.platform, bare.autonomy_flags.get("default", [])) == []


class TestCursorSpawnThroughGenericBranch:
    def test_spawn_argv_carries_the_documented_autonomy_flag(
        self, qapp, monkeypatch, tmp_path
    ) -> None:
        """The generic branch must pass cursor's `--force`, or every shell
        command in the pane stops on a y/n prompt."""
        from agent_takkub import pane_tools_policy as ptp
        from agent_takkub import shared_dev_tools as sdt
        from agent_takkub.provider_config import CURSOR

        orchestrator = _make_orchestrator(qapp, monkeypatch)
        pane = _make_pane()
        orchestrator._panes_by_project[TEST_PROJECT] = {"cursor": pane}
        monkeypatch.setattr(sdt, "SHARED_MCP_FILE", tmp_path / "shared-mcp.json")
        monkeypatch.setattr(ptp, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")
        spawn_calls: list[dict] = []

        with (
            patch.object(orchestrator, "_is_spawn_blocked", return_value=False),
            patch.object(orchestrator, "_final_gate_clear", return_value=True),
            patch("agent_takkub.orchestrator.PtySession") as mock_pty_cls,
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
            patch("agent_takkub.provider_config.effective_provider_for", return_value=CURSOR),
            patch("shutil.which", side_effect=lambda name: "agent" if name == "agent" else None),
            patch("agent_takkub.orchestrator.inject_user_profile_env"),
        ):
            mock_pty = MagicMock()
            mock_pty.spawn.side_effect = lambda **kwargs: spawn_calls.append(kwargs)
            mock_pty_cls.return_value = mock_pty
            pane.attach_session = MagicMock()

            ok, message = orchestrator.spawn("cursor", project=TEST_PROJECT)

        assert ok is True, message
        assert spawn_calls
        assert spawn_calls[0]["argv"] == ["agent", "--force"]
