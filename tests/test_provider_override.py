"""Tests for `PaneState.provider_override` (#248/#247 round 2) — the
no-content watchdog's per-pane "give up on this provider, use claude
instead" degrade signal, wired into `spawn()`'s provider resolution and its
clear-on-fresh-spawn lifecycle (spawn_engine.py ~1429-1495)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import Orchestrator, _exit_key

TEST_PROJECT = "provider-override-test"


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
    instance.shutdown_timers()
    return instance


def _pane() -> MagicMock:
    pane = MagicMock()
    pane.state = "empty"
    pane.session = None
    pane._transcript_path = None
    return pane


def _spawn_backend(
    orch: Orchestrator,
    tmp_path: Path,
    *,
    from_auto_respawn: bool = False,
    effective_provider_return: str = "gemini",
) -> tuple[MagicMock, tuple[bool, str]]:
    """Drives a real `spawn()` call with every claude-branch side effect
    mocked out (mirrors test_spawn_task_delivery.py's `_spawn_claude_assign`
    fixture but calls `spawn()` directly — no task-preload machinery) while
    leaving `effective_provider_for`'s answer mockable per-call, so a test
    can prove `provider_override` wins over (or defers to) it.

    `find_claude_executable` is claude-branch-only plumbing — whether it was
    called is the discriminator for which branch actually ran. The non-claude
    branch is deliberately left UNMOCKED beyond that: on a dev machine
    without gemini's `agy` CLI installed it fails its own binary discovery
    and returns `(False, ...)`, which is fine for tests that only need to
    prove the claude branch was (or wasn't) skipped — they don't assert
    `ok` for that side."""
    pane = _pane()
    orch._panes_by_project[TEST_PROJECT] = {"backend": pane}
    staging = tmp_path / "role"
    staging.mkdir(exist_ok=True)
    (staging / "CLAUDE.md").write_text("# Backend role\n", encoding="utf-8")

    stack = [
        patch.object(orch, "_is_spawn_blocked", return_value=False),
        patch.object(orch, "_final_gate_clear", return_value=True),
        patch("agent_takkub.orchestrator.PtySession"),
        patch("agent_takkub.orchestrator.QTimer.singleShot"),
        patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
        patch(
            "agent_takkub.orchestrator.find_claude_executable",
            return_value="claude",
        ),
        patch(
            "agent_takkub.provider_config.effective_provider_for",
            return_value=effective_provider_return,
        ),
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
        mock_pty_cls = entered[2]
        mock_find_claude = entered[5]
        mock_pty = MagicMock()
        mock_pty.spawn.side_effect = lambda **kwargs: None
        mock_pty_cls.return_value = mock_pty
        pane.attach_session = MagicMock()

        result = orch.spawn(
            "backend",
            cwd=str(tmp_path),
            project=TEST_PROJECT,
            _from_auto_respawn=from_auto_respawn,
        )
    finally:
        for ctx in reversed(stack):
            ctx.__exit__(None, None, None)

    return mock_find_claude, result


def test_provider_override_wins_over_effective_provider_for_this_spawn(
    orch: Orchestrator, tmp_path: Path
) -> None:
    ps = orch._ps(_exit_key(TEST_PROJECT, "backend"))
    ps.provider_override = "claude"

    mock_find_claude, (ok, message) = _spawn_backend(
        orch, tmp_path, effective_provider_return="gemini"
    )

    # Claude branch ran (find_claude_executable is claude-only plumbing),
    # proving `effective_provider_for`'s "gemini" answer was overridden.
    mock_find_claude.assert_called_once()
    assert ok is True, message


def test_no_override_uses_effective_provider_for_normally(
    orch: Orchestrator, tmp_path: Path
) -> None:
    # No override set — effective_provider_for's answer ("gemini") must be
    # honored, taking the non-claude branch instead of claude's.
    mock_find_claude, _result = _spawn_backend(orch, tmp_path, effective_provider_return="gemini")

    mock_find_claude.assert_not_called()


def test_auto_respawn_spawn_does_not_clear_provider_override(
    orch: Orchestrator, tmp_path: Path
) -> None:
    """The no-content watchdog's own degrade respawn passes
    `_from_auto_respawn=True` — it must NOT wipe the override it just set,
    or the degrade would be undone the instant it takes effect."""
    ps = orch._ps(_exit_key(TEST_PROJECT, "backend"))
    ps.provider_override = "claude"
    ps.no_content_recover_attempts = 1

    _spawn_backend(orch, tmp_path, from_auto_respawn=True, effective_provider_return="gemini")

    ps_after = orch._pane_state[_exit_key(TEST_PROJECT, "backend")]
    assert ps_after.provider_override == "claude"
    assert ps_after.no_content_recover_attempts == 1


def test_fresh_manual_spawn_clears_provider_override_for_next_time(
    orch: Orchestrator, tmp_path: Path
) -> None:
    """A deliberate (non-auto-respawn) spawn still uses the override for
    THIS launch (it was already read before the clear runs) but resets it
    for the NEXT spawn — a role revived after a watchdog degrade eventually
    gets a clean shot at its real provider again."""
    ps = orch._ps(_exit_key(TEST_PROJECT, "backend"))
    ps.provider_override = "claude"
    ps.no_content_recover_attempts = 1

    mock_find_claude, (ok, message) = _spawn_backend(
        orch, tmp_path, from_auto_respawn=False, effective_provider_return="gemini"
    )

    # This spawn still honored the override (pre-clear value).
    mock_find_claude.assert_called_once()
    assert ok is True, message
    # But the override + attempts counter are cleared for the next spawn.
    ps_after = orch._pane_state[_exit_key(TEST_PROJECT, "backend")]
    assert ps_after.provider_override is None
    assert ps_after.no_content_recover_attempts == 0
