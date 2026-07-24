"""H1 (cross-platform audit 2026-07-10): non-claude panes (codex/gemini/
shell) must get the same env defaults claude gets — `_apply_color_term`
(mac monochrome fix), `_apply_non_interactive_env` (issue #52, both-OS
npx/git y/N hang), `_apply_mcp_timeout`. These used to be applied only in
`spawn_engine.py`'s claude branch, *after* the shell/codex/gemini branches
had already early-returned — so a codex/gemini/shell pane spawned with none
of them.

Unlike `test_spawn_codex_argv.py` (which mocks `_build_pane_env` out
entirely to isolate argv), these tests deliberately let the real
`_build_pane_env()` run so the fix is exercised end-to-end: the allowlist
filter drops COLORTERM/npm_config_yes/GIT_TERMINAL_PROMPT from the host env,
then the three `_apply_*` helpers (now called from inside `_build_pane_env()`
itself) put the per-pane defaults back.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import Orchestrator

TEST_PROJECT = "h1envtest"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


def _make_orchestrator(qapp, monkeypatch):
    monkeypatch.setattr(Orchestrator, "_resolve_project", staticmethod(lambda p: p or TEST_PROJECT))
    o = Orchestrator()
    o._idle_watchdog.stop()
    return o


def _make_pane(role: str):
    pane = MagicMock()
    pane.role = MagicMock()
    pane.role.name = role
    pane.session = None
    pane.state = "empty"
    pane._transcript_path = None
    return pane


def _spawn_and_capture_env(qapp, monkeypatch, role: str, *extra_patches) -> dict[str, str]:
    orch = _make_orchestrator(qapp, monkeypatch)
    pane = _make_pane(role)
    orch._panes_by_project[TEST_PROJECT] = {role: pane}

    pty_spawn_calls: list[dict] = []

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(orch, "_is_spawn_blocked", return_value=False))
        stack.enter_context(patch.object(orch, "_final_gate_clear", return_value=True))
        mock_pty_cls = stack.enter_context(patch("agent_takkub.orchestrator.PtySession"))
        stack.enter_context(patch("agent_takkub.orchestrator.QTimer.singleShot"))
        stack.enter_context(patch("agent_takkub.orchestrator.inject_user_profile_env"))
        for p in extra_patches:
            stack.enter_context(p)

        mock_pty = MagicMock()
        mock_pty.spawn.side_effect = lambda **kw: pty_spawn_calls.append(kw)
        mock_pty_cls.return_value = mock_pty
        pane.attach_session = MagicMock()

        ok, msg = orch.spawn(role, project=TEST_PROJECT)

    assert ok is True, msg
    assert pty_spawn_calls, "PtySession.spawn was not called"
    return pty_spawn_calls[0]["env"]


class TestNonClaudeBranchesGetH1EnvDefaults:
    def test_codex_pane_gets_color_and_non_interactive_env(self, qapp, monkeypatch):
        from agent_takkub.provider_config import CODEX

        monkeypatch.setenv("COLORTERM", "24bit")  # host value must NOT leak through
        env = _spawn_and_capture_env(
            qapp,
            monkeypatch,
            "codex",
            patch(
                "agent_takkub.provider_config.effective_provider_for",
                return_value=CODEX,
            ),
            patch("agent_takkub.codex_helper.find_codex_executable", return_value="codex"),
            patch("agent_takkub.codex_agents_md.ensure_agents_md"),
            patch(
                "agent_takkub.mcp_bridge.subprocess.run",
                return_value=MagicMock(returncode=0, stdout="[]", stderr=""),
            ),
        )
        assert env["COLORTERM"] == "truecolor"
        assert env["TERM"] == "xterm-256color"
        assert env["npm_config_yes"] == "true"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["MCP_TOOL_TIMEOUT"] == "180000"

    def test_gemini_pane_gets_color_and_non_interactive_env(self, qapp, monkeypatch):
        from agent_takkub.provider_config import GEMINI

        monkeypatch.setenv("COLORTERM", "24bit")
        env = _spawn_and_capture_env(
            qapp,
            monkeypatch,
            "gemini",
            patch(
                "agent_takkub.provider_config.effective_provider_for",
                return_value=GEMINI,
            ),
            patch("agent_takkub.gemini_helper.find_agy_executable", return_value="agy"),
            patch("agent_takkub.codex_agents_md.ensure_agents_md"),
        )
        assert env["COLORTERM"] == "truecolor"
        assert env["TERM"] == "xterm-256color"
        assert env["npm_config_yes"] == "true"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["MCP_TOOL_TIMEOUT"] == "180000"

    def test_shell_pane_gets_color_and_non_interactive_env(self, qapp, monkeypatch):
        import shutil as _shutil_mod

        monkeypatch.setenv("COLORTERM", "24bit")
        env = _spawn_and_capture_env(
            qapp,
            monkeypatch,
            "shell",
            patch.object(_shutil_mod, "which", return_value="C:/Windows/System32/pwsh.exe"),
        )
        assert env["COLORTERM"] == "truecolor"
        assert env["TERM"] == "xterm-256color"
        assert env["npm_config_yes"] == "true"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["MCP_TOOL_TIMEOUT"] == "180000"
