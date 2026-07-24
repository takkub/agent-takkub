"""Fresh-spawn task preload and provider fallback coverage."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import Orchestrator, _exit_key
from agent_takkub.provider_spec import PROVIDER_REGISTRY
from agent_takkub.spawn_engine import (
    _CURRENT_TASK_BEGIN,
    _CURRENT_TASK_END,
    _CURRENT_TASK_TRIGGER,
    _prepare_spawn_system_prompt,
)

TEST_PROJECT = "spawn-task-test"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture
def orch(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda project: project or TEST_PROJECT),
    )
    instance = Orchestrator()
    instance._idle_watchdog.stop()
    return instance


def _pane(*, alive: bool = False) -> MagicMock:
    pane = MagicMock()
    pane.state = "working" if alive else "empty"
    pane.session = MagicMock() if alive else None
    if pane.session is not None:
        pane.session.is_alive = True
    pane._transcript_path = None
    return pane


def _spawn_claude_assign(
    orch: Orchestrator,
    tmp_path: Path,
    task: str,
    *,
    prepare_result: str | None | object = ...,
) -> tuple[list[dict], MagicMock, Path]:
    from agent_takkub.provider_config import CLAUDE

    pane = _pane()
    orch._panes_by_project[TEST_PROJECT] = {"backend": pane}
    staging = tmp_path / "role"
    staging.mkdir()
    role_file = staging / "CLAUDE.md"
    role_file.write_text("# Backend role\n", encoding="utf-8")
    spawn_calls: list[dict] = []

    stack = [
        patch.object(orch, "_is_spawn_blocked", return_value=False),
        patch.object(orch, "_final_gate_clear", return_value=True),
        patch.object(orch, "_send_when_ready"),
        patch("agent_takkub.orchestrator.PtySession"),
        patch("agent_takkub.orchestrator.QTimer.singleShot"),
        patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
        patch("agent_takkub.orchestrator.find_claude_executable", return_value="claude"),
        patch("agent_takkub.provider_config.effective_provider_for", return_value=CLAUDE),
        patch("agent_takkub.spawn_engine.agent_role_dir", return_value=staging),
        patch("agent_takkub.spawn_engine._cwd_within_project", return_value=True),
        patch("agent_takkub.spawn_engine._default_plugin_dirs", return_value=[]),
        patch("agent_takkub.spawn_engine.inject_user_profile_env"),
        patch("agent_takkub.spawn_engine.apply_claude_auth_overrides"),
        patch("agent_takkub.mcp_bridge.mcp_argv_for_provider", return_value=[]),
        patch("agent_takkub.hook_wiring.ensure_hook_settings_file", return_value="hooks.json"),
        patch("agent_takkub.task_ledger.create_assignment", return_value=None),
    ]
    if prepare_result is not ...:
        stack.append(
            patch(
                "agent_takkub.spawn_engine._prepare_spawn_system_prompt",
                return_value=prepare_result,
            )
        )

    entered = [ctx.__enter__() for ctx in stack]
    try:
        mock_send = entered[2]
        mock_pty_cls = entered[3]
        mock_pty = MagicMock()
        mock_pty.spawn.side_effect = lambda **kwargs: spawn_calls.append(kwargs)
        mock_pty_cls.return_value = mock_pty
        pane.attach_session = MagicMock()

        ok, message = orch.assign(
            "backend",
            cwd=str(tmp_path),
            task=task,
            project=TEST_PROJECT,
        )
        assert ok is True, message
    finally:
        for ctx in reversed(stack):
            ctx.__exit__(None, None, None)

    return spawn_calls, mock_send, role_file


def test_prompt_helper_marks_current_task_and_strips_it_on_next_spawn(tmp_path: Path) -> None:
    role_file = tmp_path / "CLAUDE.md"
    prompt_file = tmp_path / "CLAUDE.spawn-pane.md"
    role_file.write_text("# Stable role rules\n", encoding="utf-8")

    assert _prepare_spawn_system_prompt(
        str(role_file),
        "CURRENT TASK",
        output_file=str(prompt_file),
    ) == str(prompt_file)
    with_task = prompt_file.read_text(encoding="utf-8")
    assert _CURRENT_TASK_BEGIN in with_task
    assert _CURRENT_TASK_END in with_task
    assert "Current task for this spawn (one-shot)" in with_task
    assert "CURRENT TASK" in with_task

    assert _prepare_spawn_system_prompt(
        str(role_file),
        None,
        output_file=str(prompt_file),
    ) == str(prompt_file)
    regenerated = prompt_file.read_text(encoding="utf-8")
    assert regenerated == "# Stable role rules\n"
    assert "CURRENT TASK" not in regenerated
    assert _CURRENT_TASK_BEGIN not in regenerated
    assert role_file.read_text(encoding="utf-8") == "# Stable role rules\n"


def test_fresh_claude_assign_preloads_task_and_sends_only_tiny_trigger(
    orch: Orchestrator,
    tmp_path: Path,
) -> None:
    task = "[ROLE: backend]\n" + ("implement safely\n" * 80)
    spawn_calls, mock_send, role_file = _spawn_claude_assign(orch, tmp_path, task)

    assert len(spawn_calls) == 1
    argv = spawn_calls[0]["argv"]
    flag_index = argv.index("--append-system-prompt-file")
    spawn_prompt_file = Path(argv[flag_index + 1])
    assert spawn_prompt_file != role_file
    assert spawn_prompt_file.parent == role_file.parent
    prompt = spawn_prompt_file.read_text(encoding="utf-8")
    assert task in prompt
    assert "Current task for this spawn (one-shot)" in prompt
    assert task not in role_file.read_text(encoding="utf-8")
    mock_send.assert_called_once_with(
        "backend",
        _CURRENT_TASK_TRIGGER,
        project=TEST_PROJECT,
    )
    assert task not in mock_send.call_args.args[1]
    assert "file-read tool" not in mock_send.call_args.args[1]

    state = orch._pane_state[_exit_key(TEST_PROJECT, "backend")]
    assert state.spawn_initial_task_state == "delivered"
    assert state.spawn_initial_task is None


def test_fresh_claude_prompt_write_failure_falls_back_once_to_pointer(
    orch: Orchestrator,
    tmp_path: Path,
) -> None:
    task = "[ROLE: backend]\n" + ("fallback\n" * 80)
    _calls, mock_send, _role_file = _spawn_claude_assign(
        orch,
        tmp_path,
        task,
        prepare_result=None,
    )

    mock_send.assert_called_once()
    sent = mock_send.call_args.args[1]
    assert sent.startswith("[ROLE: backend]")
    assert "file-read tool" in sent
    assert orch._pane_state[_exit_key(TEST_PROJECT, "backend")].spawn_initial_task_state == (
        "fallback"
    )


def test_running_claude_pane_keeps_mid_session_pointer_flow(
    orch: Orchestrator,
    tmp_path: Path,
) -> None:
    from agent_takkub.provider_config import CLAUDE

    orch._panes_by_project[TEST_PROJECT] = {"backend": _pane(alive=True)}
    task = "[ROLE: backend]\n" + ("mid-session\n" * 80)

    with (
        patch("agent_takkub.provider_config.effective_provider_for", return_value=CLAUDE),
        patch.object(orch, "_send_when_ready") as mock_send,
        patch("agent_takkub.task_ledger.create_assignment", return_value=None),
    ):
        ok, message = orch.assign(
            "backend",
            cwd=str(tmp_path),
            task=task,
            project=TEST_PROJECT,
        )

    assert ok is True, message
    mock_send.assert_called_once()
    sent = mock_send.call_args.args[1]
    assert sent != task
    assert "file-read tool" in sent
    assert orch._pane_state[_exit_key(TEST_PROJECT, "backend")].spawn_initial_task_state == ""


def test_deferred_fresh_claude_spawn_retains_preload_without_early_pointer(
    orch: Orchestrator,
    tmp_path: Path,
) -> None:
    from agent_takkub.provider_config import CLAUDE

    orch._panes_by_project[TEST_PROJECT] = {"backend": _pane()}
    task = "[ROLE: backend]\n" + ("deferred\n" * 80)

    with (
        patch("agent_takkub.provider_config.effective_provider_for", return_value=CLAUDE),
        patch("agent_takkub.spawn_engine._cwd_within_project", return_value=True),
        patch.object(orch, "_is_spawn_blocked", return_value=True),
        patch.object(orch, "_send_when_ready") as mock_send,
        patch("agent_takkub.spawn_engine.QTimer.singleShot"),
        patch("agent_takkub.task_ledger.create_assignment", return_value=None),
    ):
        ok, message = orch.assign(
            "backend",
            cwd=str(tmp_path),
            task=task,
            project=TEST_PROJECT,
        )

    assert ok is True, message
    mock_send.assert_not_called()
    state = orch._pane_state[_exit_key(TEST_PROJECT, "backend")]
    assert state.spawn_initial_task_state == "pending"
    assert state.spawn_initial_task == task
    assert state.spawn_initial_task_fallback is not None


def test_only_claude_has_confirmed_file_backed_system_prompt_capability() -> None:
    assert PROVIDER_REGISTRY["claude"].system_prompt_flag == "--append-system-prompt-file"
    for provider in ("codex", "gemini", "opencode", "kimi", "cursor"):
        assert PROVIDER_REGISTRY[provider].system_prompt_flag is None
