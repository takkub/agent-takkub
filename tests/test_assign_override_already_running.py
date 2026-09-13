"""#587 C3: `assign --provider/--model/--effort` on a role whose pane is
already running used to warn "ไม่มีผล: pane เปิดอยู่แล้ว" every single time,
even when the requested value is IDENTICAL to what the pane is already
running (the common "queue a follow-up with the same flags" pattern) — pure
noise, on top of the existing #464 Lead-noise tally. The warning must fire
only when the requested value actually differs from what the pane is
currently running.

Harness mirrors test_idle_pane_reuse_assign.py: an alive pane already
registered in `_panes_by_project`, `_send_when_ready` mocked so no real PTY
write happens, and `PtySession.__new__` mocked so a fresh spawn (which must
never happen here — the pane is alive) would be caught by `assert_not_called`.
"""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import config, provider_config
from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator, _exit_key

_PROJECT = "default"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture(autouse=True)
def _providers_available(monkeypatch: pytest.MonkeyPatch) -> None:
    # These tests exercise `--provider codex`/`--provider gemini` overrides
    # purely for the "does the override match what's already running"
    # warning logic — they must not depend on whether codex/gemini CLIs
    # happen to be installed on the machine running the suite. A dev box
    # with both installed made this pass locally while a clean CI runner
    # (neither installed) failed `assign_provider_override_error`'s real
    # `_provider_available` check before ever reaching the code under test.
    # Mirrors the same monkeypatch in test_core_providers.py.
    monkeypatch.setattr(provider_config, "_provider_available", lambda p: True)


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


def _running_pane(orch: Orchestrator, role: str, cwd: str) -> None:
    fake_pane = MagicMock()
    alive_session = MagicMock()
    alive_session.is_alive = True
    fake_pane.session = alive_session
    fake_pane.state = "working"
    fake_pane._session_cwd = cwd
    orch._panes_by_project.setdefault(_PROJECT, {})[role] = fake_pane


def _assign(orch: Orchestrator, role: str, cwd: str, **kwargs) -> tuple[bool, str]:
    with (
        patch.object(orch_mod.PtySession, "__new__") as mock_new,
        patch.object(Orchestrator, "_send_when_ready"),
        patch.object(Orchestrator, "_notify_lead") as mock_notify,
    ):
        result = orch.assign(role, cwd, "follow-up task", project=_PROJECT, **kwargs)
        mock_new.assert_not_called()  # pane is alive — must never fresh-spawn
    return result, mock_notify


class TestOverrideWarningOnlyOnActualChange:
    def test_same_provider_as_already_running_does_not_warn(
        self, orch: Orchestrator, tmp_env: pathlib.Path
    ) -> None:
        role = "backend"
        cwd = str(tmp_env / "workdir")
        pathlib.Path(cwd).mkdir(parents=True, exist_ok=True)
        _running_pane(orch, role, cwd)
        orch._ps(_exit_key(_PROJECT, role)).provider_override = "codex"

        (ok, _msg), mock_notify = _assign(orch, role, cwd, provider="codex")

        assert ok is True
        ignored_calls = [
            c
            for c in mock_notify.call_args_list
            if c.kwargs.get("kind") == "assign-override-ignored"
        ]
        assert ignored_calls == []

    def test_different_provider_from_already_running_still_warns(
        self, orch: Orchestrator, tmp_env: pathlib.Path
    ) -> None:
        role = "backend"
        cwd = str(tmp_env / "workdir")
        pathlib.Path(cwd).mkdir(parents=True, exist_ok=True)
        _running_pane(orch, role, cwd)
        orch._ps(_exit_key(_PROJECT, role)).provider_override = "codex"

        (ok, _msg), mock_notify = _assign(orch, role, cwd, provider="gemini")

        assert ok is True
        ignored_calls = [
            c
            for c in mock_notify.call_args_list
            if c.kwargs.get("kind") == "assign-override-ignored"
        ]
        assert len(ignored_calls) == 1
        assert "gemini" in ignored_calls[0].args[1]

    def test_same_model_as_already_running_does_not_warn(
        self, orch: Orchestrator, tmp_env: pathlib.Path
    ) -> None:
        role = "backend"
        cwd = str(tmp_env / "workdir")
        pathlib.Path(cwd).mkdir(parents=True, exist_ok=True)
        _running_pane(orch, role, cwd)
        orch._ps(_exit_key(_PROJECT, role)).model_override = "opus"

        (ok, _msg), mock_notify = _assign(orch, role, cwd, model="opus")

        assert ok is True
        ignored_calls = [
            c
            for c in mock_notify.call_args_list
            if c.kwargs.get("kind") == "assign-override-ignored"
        ]
        assert ignored_calls == []

    def test_different_model_from_already_running_still_warns(
        self, orch: Orchestrator, tmp_env: pathlib.Path
    ) -> None:
        role = "backend"
        cwd = str(tmp_env / "workdir")
        pathlib.Path(cwd).mkdir(parents=True, exist_ok=True)
        _running_pane(orch, role, cwd)
        orch._ps(_exit_key(_PROJECT, role)).model_override = "opus"

        (ok, _msg), mock_notify = _assign(orch, role, cwd, model="sonnet")

        assert ok is True
        ignored_calls = [
            c
            for c in mock_notify.call_args_list
            if c.kwargs.get("kind") == "assign-override-ignored"
        ]
        assert len(ignored_calls) == 1
        assert "sonnet" in ignored_calls[0].args[1]

    def test_same_effort_as_already_running_does_not_warn(
        self, orch: Orchestrator, tmp_env: pathlib.Path
    ) -> None:
        role = "backend"
        cwd = str(tmp_env / "workdir")
        pathlib.Path(cwd).mkdir(parents=True, exist_ok=True)
        _running_pane(orch, role, cwd)
        orch._ps(_exit_key(_PROJECT, role)).effort_override = "high"

        (ok, _msg), mock_notify = _assign(orch, role, cwd, effort="high")

        assert ok is True
        ignored_calls = [
            c
            for c in mock_notify.call_args_list
            if c.kwargs.get("kind") == "assign-override-ignored"
        ]
        assert ignored_calls == []

    def test_different_effort_from_already_running_still_warns(
        self, orch: Orchestrator, tmp_env: pathlib.Path
    ) -> None:
        role = "backend"
        cwd = str(tmp_env / "workdir")
        pathlib.Path(cwd).mkdir(parents=True, exist_ok=True)
        _running_pane(orch, role, cwd)
        orch._ps(_exit_key(_PROJECT, role)).effort_override = "high"

        (ok, _msg), mock_notify = _assign(orch, role, cwd, effort="low")

        assert ok is True
        ignored_calls = [
            c
            for c in mock_notify.call_args_list
            if c.kwargs.get("kind") == "assign-override-ignored"
        ]
        assert len(ignored_calls) == 1
        assert "low" in ignored_calls[0].args[1]
