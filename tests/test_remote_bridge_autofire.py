"""Tests for `/remote-control` auto-bridge on every Lead spawn (#4,
2026-07-09 core-upgrade plan).

Replaces the old two narrower trigger points (`_on_pane_resumed` on
session-resume, `_on_lead_input` on first Enter in a Lead pane) with a
single hook inside `SpawnEngineMixin.spawn()` —
`_maybe_fire_remote_bridge` — that fires after *every* successful Lead
spawn (fresh boot, tab open, respawn, crash recovery), deduped by
project+session-uuid so a brand-new session always fires once and a
resumed (same-uuid) session never fires twice.
"""

from __future__ import annotations

import pathlib
import time
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import config
from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import RESUME_WINDOW_SEC, Orchestrator, _exit_key
from agent_takkub.roles import LEAD
from agent_takkub.spawn_engine import _REMOTE_BRIDGE_MAX_WAIT_MS

_PROJECT = "default"


def _assert_bridge_call(call, project: str = _PROJECT) -> None:
    """A `_maybe_fire_remote_bridge` call to `inject_slash_command_when_ready`
    now carries the #107 slow-boot window + delivery/drop callbacks on top of
    the original (role, command, project) triple — assert the shape without
    hard-coding the exact positional/keyword split."""
    args, kwargs = call
    assert (LEAD.name, "/remote-control") == args
    assert kwargs["project"] == project
    assert kwargs["max_wait_ms"] == _REMOTE_BRIDGE_MAX_WAIT_MS
    assert callable(kwargs["on_delivered"])
    assert callable(kwargs["on_dropped"])


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
    o._idle_watchdog.stop()
    o.inject_slash_command_when_ready = MagicMock()  # type: ignore[method-assign]
    return o


def _spawn_capture(
    orch: Orchestrator,
    role_name: str,
    cwd: str = "/proj",
    project: str = _PROJECT,
) -> list[str]:
    """Run orch.spawn() with a fake PtySession and return the captured argv."""
    fake_pane = MagicMock()
    fake_pane.session = None
    fake_pane.state = "empty"
    fake_pane.attach_session = MagicMock()
    fake_pane._transcript_path = None
    orch._panes_by_project.setdefault(project, {})[role_name] = fake_pane

    captured: list[list[str]] = []
    fake_session = MagicMock()
    fake_session.processExited = MagicMock()
    fake_session.processExited.connect = MagicMock()

    with patch.object(orch_mod.PtySession, "__new__", return_value=fake_session):
        with patch.object(
            fake_session,
            "spawn",
            side_effect=lambda argv, cwd, env, **kwargs: captured.append(list(argv)),
        ):
            orch.spawn(role_name, cwd=cwd, project=project)

    return captured[0] if captured else []


def _simulate_exit(orch: Orchestrator, role_name: str, cwd: str, project: str = _PROJECT) -> None:
    orch._recent_exits[_exit_key(project, role_name)] = {"cwd": cwd, "ts": time.time()}
    pane = orch._panes_by_project.get(project, {}).get(role_name)
    if pane is not None:
        pane.state = "empty"


class TestFreshSpawnFiresOnce:
    def test_fresh_lead_spawn_fires_bridge(self, orch: Orchestrator) -> None:
        _spawn_capture(orch, LEAD.name)
        orch.inject_slash_command_when_ready.assert_called_once()
        _assert_bridge_call(orch.inject_slash_command_when_ready.call_args)


class TestRespawnNewUuidFiresAgain:
    def test_respawn_after_window_expiry_fires_again(self, orch: Orchestrator) -> None:
        cwd = "/proj"
        _spawn_capture(orch, LEAD.name, cwd=cwd)
        assert orch.inject_slash_command_when_ready.call_count == 1

        key = _exit_key(_PROJECT, LEAD.name)
        orch._recent_exits[key] = {"cwd": cwd, "ts": time.time() - (RESUME_WINDOW_SEC + 60)}
        pane = orch._panes_by_project.get(_PROJECT, {}).get(LEAD.name)
        if pane is not None:
            pane.state = "empty"

        _spawn_capture(orch, LEAD.name, cwd=cwd)
        assert orch.inject_slash_command_when_ready.call_count == 2


class TestResumePathDoesNotDoubleFire:
    def test_resume_within_window_keeps_same_uuid_no_refire(self, orch: Orchestrator) -> None:
        cwd = "/proj"
        _spawn_capture(orch, LEAD.name, cwd=cwd)
        assert orch.inject_slash_command_when_ready.call_count == 1

        # Exit + respawn within RESUME_WINDOW_SEC: picks up --resume <same uuid>.
        _simulate_exit(orch, LEAD.name, cwd=cwd)
        argv = _spawn_capture(orch, LEAD.name, cwd=cwd)
        assert "--resume" in argv

        # Same session (same uuid) — dedupe key unchanged, must not refire.
        assert orch.inject_slash_command_when_ready.call_count == 1


class TestTeammateSpawnDoesNotFire:
    def test_teammate_spawn_never_fires_bridge(self, orch: Orchestrator) -> None:
        _spawn_capture(orch, "backend")
        orch.inject_slash_command_when_ready.assert_not_called()
