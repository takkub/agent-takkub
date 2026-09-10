"""#516 follow-up (idle-pane reuse): re-`assign`-ing a role whose pane is
still alive and sitting in "done" state (finished a previous task, not yet
auto-closed) must deliver the new task by pasting into the SAME running
session (`_send_when_ready`) — never by tearing the pane down and booting a
fresh claude process, which is the ~70k-boot-token cost this is meant to
avoid.

Traced (not guessed) via `spawn_engine.spawn()`: its very first check
(`pane.session is not None and pane.session.is_alive`) already short-circuits
as a no-op, and `orchestrator._assign_dispatch` already falls through to
`_send_when_ready` instead of the fresh-spawn `--append-system-prompt-file`
branch whenever that no-op fires. This file locks that existing behaviour in
with a regression test — see docs/audit/2026-09-07-boot-context.md §7 for why
no production code changed here (the ALREADY-EXITED-pane case is a separate,
intentionally different design — resume-bleed prevention — not this file's
concern).
"""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import config
from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator

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
    return tmp_path


@pytest.fixture
def orch(qapp: QCoreApplication, tmp_env: pathlib.Path) -> Orchestrator:
    o = Orchestrator()
    o.shutdown_timers()
    return o


class TestAliveDonePaneReusedByAssign:
    def test_assign_pastes_into_alive_done_pane_instead_of_fresh_spawn(
        self, orch: Orchestrator, tmp_env: pathlib.Path
    ) -> None:
        role = "backend"
        cwd = str(tmp_env / "workdir")
        pathlib.Path(cwd).mkdir(parents=True, exist_ok=True)

        fake_pane = MagicMock()
        alive_session = MagicMock()
        alive_session.is_alive = True
        fake_pane.session = alive_session
        fake_pane.state = "done"
        fake_pane._session_cwd = cwd
        orch._panes_by_project.setdefault(_PROJECT, {})[role] = fake_pane

        with patch.object(orch_mod.PtySession, "__new__") as mock_new:
            with patch.object(Orchestrator, "_send_when_ready") as mock_send:
                ok, _msg = orch.assign(role, cwd, "review the follow-up PR", project=_PROJECT)

        assert ok is True
        # No fresh claude process was ever constructed — the alive session
        # was reused, not torn down and rebooted.
        mock_new.assert_not_called()
        # The new task was delivered via the paste path into the SAME pane.
        mock_send.assert_called_once()
        sent_role = mock_send.call_args[0][0]
        sent_text = mock_send.call_args[0][1]
        assert sent_role == role
        assert "review the follow-up PR" in sent_text

    def test_working_pane_also_reused_not_just_done(
        self, orch: Orchestrator, tmp_env: pathlib.Path
    ) -> None:
        """Same guarantee for a pane still mid-task (`state="working"`) —
        the alive-session short-circuit in spawn() is state-agnostic, so a
        second assign before the first task's done() must reuse it too."""
        role = "frontend"
        cwd = str(tmp_env / "workdir2")
        pathlib.Path(cwd).mkdir(parents=True, exist_ok=True)

        fake_pane = MagicMock()
        alive_session = MagicMock()
        alive_session.is_alive = True
        fake_pane.session = alive_session
        fake_pane.state = "working"
        fake_pane._session_cwd = cwd
        orch._panes_by_project.setdefault(_PROJECT, {})[role] = fake_pane

        with patch.object(orch_mod.PtySession, "__new__") as mock_new:
            with patch.object(Orchestrator, "_send_when_ready") as mock_send:
                ok, _msg = orch.assign(role, cwd, "one more thing", project=_PROJECT)

        assert ok is True
        mock_new.assert_not_called()
        mock_send.assert_called_once()
