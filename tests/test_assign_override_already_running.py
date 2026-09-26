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

from agent_takkub import config, gemini_helper, provider_config
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


def _idle_pane(orch: Orchestrator, role: str, cwd: str, state: str) -> MagicMock:
    fake_pane = MagicMock()
    alive_session = MagicMock()
    alive_session.is_alive = True
    fake_pane.session = alive_session
    fake_pane.state = state
    fake_pane._session_cwd = cwd
    orch._panes_by_project.setdefault(_PROJECT, {})[role] = fake_pane
    return fake_pane


class TestIdleProviderSwitch:
    """#603: `assign --provider` on a role whose pane is alive but IDLE (not
    mid-turn) used to just warn "ไม่มีผล" and leave the stale provider
    running — Lead had to `close` the pane by hand, then re-assign, risking
    #593/#594's races along the way. An idle pane (active/done/error, i.e.
    NOT the busy-queue path and NOT the still-mid-turn edge covered by
    TestOverrideWarningOnlyOnActualChange above) must instead close and
    respawn on the requested provider automatically."""

    @pytest.mark.parametrize("state", ["active", "done", "error"])
    def test_idle_pane_closes_and_schedules_respawn(
        self,
        orch: Orchestrator,
        tmp_env: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        state: str,
    ) -> None:
        monkeypatch.setattr(orch_mod, "CLOSE_ON_DONE", False)
        role = "backend"
        cwd = str(tmp_env / "workdir")
        pathlib.Path(cwd).mkdir(parents=True, exist_ok=True)
        _idle_pane(orch, role, cwd, state=state)
        orch._ps(_exit_key(_PROJECT, role)).provider_override = "codex"

        scheduled: list[tuple[int, object]] = []
        monkeypatch.setattr(
            orch_mod.QTimer, "singleShot", lambda ms, cb: scheduled.append((ms, cb))
        )
        with (
            patch.object(orch_mod.PtySession, "__new__"),
            patch.object(Orchestrator, "_send_when_ready"),
            patch.object(Orchestrator, "_notify_lead") as mock_notify,
            patch.object(Orchestrator, "close") as mock_close,
        ):
            ok, msg = orch.assign(role, cwd, "follow-up task", project=_PROJECT, provider="gemini")

        assert ok is True
        assert "gemini" in msg
        mock_close.assert_called_once()
        assert mock_close.call_args.kwargs.get("suppress_pipeline") is True
        assert mock_close.call_args.kwargs.get("suppress_auto_chain") is True
        switch_calls = [
            c
            for c in mock_notify.call_args_list
            if c.kwargs.get("kind") == "assign-provider-switch"
        ]
        assert len(switch_calls) == 1
        assert "gemini" in switch_calls[0].args[1]
        assert len(scheduled) == 1
        assert scheduled[0][0] == 2_000

    def test_idle_pane_respawn_lands_on_requested_provider(
        self, orch: Orchestrator, tmp_env: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deferred callback, once the old pane is actually gone, must
        really spawn a fresh pane on the requested provider (not just warn)."""
        # Unlike the other tests in this file (which only exercise the
        # override-warning logic and never reach a real spawn), this one
        # runs the deferred respawn all the way into spawn_engine's
        # non-claude branch, which resolves the CLI binary via
        # `ProviderSpec.custom_discovery_fn` directly (not via
        # `provider_config._provider_available`, already stubbed by the
        # autouse `_providers_available` fixture above) — on CI, where
        # `agy` isn't installed, that resolves to None and spawn() bails
        # out with `spec.install_instructions` before ever touching
        # `PtySession.__new__`. Stub discovery too so the fresh spawn
        # actually reaches PtySession.
        monkeypatch.setattr(gemini_helper, "find_agy_executable", lambda: "agy")
        role = "backend"
        cwd = str(tmp_env / "workdir")
        pathlib.Path(cwd).mkdir(parents=True, exist_ok=True)
        _idle_pane(orch, role, cwd, state="active")
        orch._ps(_exit_key(_PROJECT, role)).provider_override = "codex"

        scheduled: list[tuple[int, object]] = []
        monkeypatch.setattr(
            orch_mod.QTimer, "singleShot", lambda ms, cb: scheduled.append((ms, cb))
        )

        def _fake_close(role_name, **kwargs):
            # Mirrors what real close() eventually does once PTY teardown
            # finishes — the pane widget stays registered (spawn() reuses
            # it), only its session goes away.
            pane = orch._panes_by_project[_PROJECT][role_name]
            pane.session = None
            return True, "closed"

        # spawn() reads provider_override to pick the CLI then clears it
        # (its own one-shot "fresh-spawn-clear" contract, unrelated to
        # #603) — capture the value it actually saw rather than reading it
        # back afterwards.
        seen_provider: dict[str, str | None] = {}
        real_spawn = orch.spawn

        def _spy_spawn(role_name, *a, **kw):
            seen_provider["value"] = orch._ps(_exit_key(_PROJECT, role_name)).provider_override
            return real_spawn(role_name, *a, **kw)

        with (
            patch.object(orch_mod.PtySession, "__new__") as mock_new,
            patch.object(Orchestrator, "_send_when_ready") as mock_send,
            patch.object(Orchestrator, "_notify_lead"),
            patch.object(Orchestrator, "close", side_effect=_fake_close),
            patch.object(Orchestrator, "spawn", side_effect=_spy_spawn),
        ):
            ok, _msg = orch.assign(role, cwd, "follow-up task", project=_PROJECT, provider="gemini")
            assert ok is True
            assert len(scheduled) == 1
            mock_new.assert_not_called()  # not yet — only after the deferred respawn fires

            scheduled[0][1]()  # fire the deferred respawn

            mock_new.assert_called_once()  # fresh pane actually spawned
            mock_send.assert_called_once()

        assert seen_provider["value"] == "gemini"
