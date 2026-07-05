"""Pane cwd fallback must use DATA_HOME, not REPO_ROOT (installed builds).

REPO_ROOT resolves into an empty/read-only venv ancestor in an installed
build (see docs/audit/2026-07-05-installed-build-audit-gemini.md, finding 4).
When a spawn has no explicit cwd and no configured project path, it must not
land inside that venv tree — DATA_HOME collapses to REPO_ROOT in a dev
checkout (no behavior change there) but diverges to the isolated
~/.agent-takkub home in an installed build.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import spawn_engine
from agent_takkub.orchestrator import Orchestrator

TEST_PROJECT = "no-such-project"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _make_orchestrator(qapp, monkeypatch):
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda p: p or TEST_PROJECT),
    )
    o = Orchestrator()
    o._idle_watchdog.stop()
    return o


def _make_pane(role: str = "shell"):
    pane = MagicMock()
    pane.role = MagicMock()
    pane.role.name = role
    pane.session = None
    pane.state = "empty"
    pane._transcript_path = None
    return pane


class TestShellSpawnCwdFallback:
    """Shell branch (goes through _launch_session) — no project configured,
    no explicit cwd → must fall back to spawn_engine.DATA_HOME."""

    def test_falls_back_to_data_home_not_repo_root(self, qapp, monkeypatch, tmp_path) -> None:
        import shutil as _shutil_mod

        fake_data_home = tmp_path / "agent-takkub-home"
        monkeypatch.setattr(spawn_engine, "DATA_HOME", fake_data_home)

        orch = _make_orchestrator(qapp, monkeypatch)
        pane = _make_pane("shell")
        orch._panes_by_project[TEST_PROJECT] = {"shell": pane}

        spawn_calls = []

        with (
            patch.object(orch, "_is_spawn_blocked", return_value=False),
            patch.object(orch, "_final_gate_clear", return_value=True),
            patch("agent_takkub.orchestrator.PtySession") as mock_pty_cls,
            patch.object(_shutil_mod, "which", return_value="C:/Windows/System32/pwsh.exe"),
            patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
        ):
            mock_pty = MagicMock()
            mock_pty.spawn.side_effect = lambda **kw: spawn_calls.append(kw)
            mock_pty_cls.return_value = mock_pty
            pane.attach_session = MagicMock()

            ok, _msg = orch.spawn("shell", project=TEST_PROJECT)

        assert ok is True
        assert spawn_calls, "PtySession.spawn was never called"
        assert spawn_calls[0]["cwd"] == str(fake_data_home)

    def test_dev_checkout_still_falls_back_to_repo_root_value(
        self, qapp, monkeypatch, tmp_path
    ) -> None:
        """DATA_HOME == REPO_ROOT in a dev checkout — same value, unchanged
        behavior, just resolved through DATA_HOME now."""
        import shutil as _shutil_mod

        from agent_takkub import config

        dev_root = tmp_path / "dev-checkout"
        monkeypatch.setattr(spawn_engine, "DATA_HOME", dev_root)
        monkeypatch.setattr(config, "REPO_ROOT", dev_root)
        monkeypatch.setattr(config, "DATA_HOME", dev_root)

        orch = _make_orchestrator(qapp, monkeypatch)
        pane = _make_pane("shell")
        orch._panes_by_project[TEST_PROJECT] = {"shell": pane}

        spawn_calls = []

        with (
            patch.object(orch, "_is_spawn_blocked", return_value=False),
            patch.object(orch, "_final_gate_clear", return_value=True),
            patch("agent_takkub.orchestrator.PtySession") as mock_pty_cls,
            patch.object(_shutil_mod, "which", return_value="C:/Windows/System32/pwsh.exe"),
            patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
        ):
            mock_pty = MagicMock()
            mock_pty.spawn.side_effect = lambda **kw: spawn_calls.append(kw)
            mock_pty_cls.return_value = mock_pty
            pane.attach_session = MagicMock()

            orch.spawn("shell", project=TEST_PROJECT)

        assert spawn_calls[0]["cwd"] == str(dev_root)


class TestRequiresCommitCwdFallback:
    """orchestrator.py:1518 — `requires_commit` uncommitted-check cwd fallback
    (used when a pane's session never recorded a cwd)."""

    def test_falls_back_to_data_home_when_no_session_cwd(self, qapp, monkeypatch, tmp_path) -> None:
        from agent_takkub import orchestrator as orch_mod

        fake_data_home = tmp_path / "agent-takkub-home"
        monkeypatch.setattr(orch_mod, "DATA_HOME", fake_data_home)

        orch = _make_orchestrator(qapp, monkeypatch)
        pane = MagicMock()
        pane.state = "working"
        pane.session = MagicMock()
        pane.session.is_alive = True
        pane.session.is_at_ready_prompt.return_value = True
        pane._session_cwd = None
        pane._transcript_path = None
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["frontend"] = pane
        orch._panes_by_project[TEST_PROJECT]["lead"] = MagicMock(session=MagicMock(is_alive=True))

        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready"),
        ):
            orch.assign(
                "frontend", cwd="/repo", task="do work", requires_commit=True, project=TEST_PROJECT
            )

        with patch.object(orch, "_check_uncommitted_async") as chk:
            orch.done("frontend", note="done", project=TEST_PROJECT)

        chk.assert_called_once_with(TEST_PROJECT, "frontend", str(fake_data_home))
