"""Characterization test (epic #309 2.0.0 Wave A, per Lead's review of
`docs/v2/wave-a-claude-argv-extract-report.md`): proves
`core.providers.claude_plan.assemble_claude_argv` reproduces, byte for
byte, the argv `spawn_engine.py`'s LIVE claude branch actually builds
today — not a hand-transcribed approximation of it.

Unlike `test_core_providers_claude_plan.py`'s golden-order test (which
hand-derives an expected list by reading the source), this test spawns a
REAL `Orchestrator.spawn()` for a claude teammate pane (mocked `PtySession`
only — same harness shape as `test_spawn_codex_argv.py` /
`test_spawn_v2_account_env.py`), captures the argv the live branch actually
passed to `session.spawn()`, and asserts it equals `assemble_claude_argv()`
called with the same caller-resolved pieces. Every resolution input
(model/effort/fallback via `TAKKUB_TEAMMATE_*` env, MCP argv, the hook
`--settings` path, the session-id uuid) is pinned via monkeypatch/env so
both sides start from the identical resolved values — this test is not
allowed to pass by both sides independently guessing the same thing.

If a future edit changes the live branch's argv order without updating
`claude_plan.py` (the drift risk `claude_plan.py`'s own module docstring
names, and the risk Lead's review specifically asked to be covered), this
test fails — it does not require `spawn_engine.py` to actually call
`assemble_claude_argv` to catch that drift.
"""

from __future__ import annotations

import uuid as _uuid_mod
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.core.providers.claude_plan import assemble_claude_argv
from agent_takkub.orchestrator import Orchestrator

TEST_PROJECT = "claudeargvplantest"
FAKE_CWD = "/tmp/takkub-test-claude-argv-plan-cwd"
FIXED_UUID = _uuid_mod.UUID("00000000-0000-4000-8000-000000000042")
FAKE_MCP_ARGV = ["--mcp-config", "/tmp/fake-mcp-config.json", "--strict-mcp-config"]


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
    o.shutdown_timers()
    return o


# `find_claude_executable`/`_build_transcript_path` are resolved inside
# spawn_engine.py via `_from_orch(name)` (a `getattr(orchestrator_module,
# name)` lookup at call time), so patching them on `agent_takkub.orchestrator`
# is correct. `agent_role_dir`/`apply_claude_auth_overrides`/
# `_default_plugin_dirs` are plain module-level imports INTO spawn_engine.py
# itself (`from .x import y`) — patching them on `orchestrator` would be a
# silent no-op (confirmed the hard way: an earlier draft of this test patched
# `agent_takkub.orchestrator.agent_role_dir`, which never took effect, and
# the real branch synthesized a real, timestamped prompt file into the
# expected argv instead of leaving that piece empty) — they must be patched
# on `agent_takkub.spawn_engine` where the call site actually resolves them.
_PATCHES: list[tuple[str, object]] = [
    ("agent_takkub.orchestrator.find_claude_executable", "fake-claude"),
    ("agent_takkub.orchestrator._build_transcript_path", None),
    ("agent_takkub.spawn_engine._default_plugin_dirs", []),
    ("agent_takkub.spawn_engine.apply_claude_auth_overrides", None),
    # Nonexistent staging dir -> role_md_path.exists() is False -> role_md_file
    # stays None -> _prepare_spawn_system_prompt(None, ...) returns None too
    # (spawn_engine.py:261 `if not role_md_file: return None`) -> the branch's
    # system_prompt_argv piece is empty for this run.
    ("agent_takkub.spawn_engine.agent_role_dir", "/tmp/nonexistent-staging-claude-argv-plan"),
    ("agent_takkub.hook_wiring.ensure_hook_settings_file", "/tmp/fake-hook-settings.json"),
    ("agent_takkub.mcp_bridge.mcp_argv_for_provider", FAKE_MCP_ARGV),
    ("agent_takkub.mcp_bridge.describe_mcp_handshake", {}),
    ("agent_takkub.role_models.effort_for", ""),
]


def _spawn_claude_and_capture_argv(qapp, monkeypatch, role_name: str = "backend") -> list[str]:
    import pathlib

    monkeypatch.setenv("TAKKUB_TEAMMATE_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("TAKKUB_TEAMMATE_EFFORT", "high")
    monkeypatch.setenv("TAKKUB_TEAMMATE_FALLBACK", "claude-haiku-4-5")
    monkeypatch.delenv("TAKKUB_TEAMMATE_DISALLOWED_TOOLS", raising=False)
    monkeypatch.delenv("TAKKUB_ALLOW_TASK", raising=False)
    monkeypatch.delenv("TAKKUB_EXTRA_PLUGINS", raising=False)
    monkeypatch.delenv("TAKKUB_SETTING_SOURCES", raising=False)

    project = "default"  # exempt from the cwd-within-project check
    orch = _make_orchestrator(qapp, monkeypatch)

    mock_session = MagicMock()
    mock_session.processExited = MagicMock()
    mock_session.is_alive = True
    captured: dict[str, list[str]] = {}

    def _capture_spawn(argv, cwd, env, transcript_path=None):
        captured["argv"] = list(argv)

    mock_session.spawn = _capture_spawn

    pane = MagicMock()
    pane.session = None
    orch._panes_by_project.setdefault(project, {})[role_name] = pane

    from contextlib import ExitStack

    with ExitStack() as stack:
        for target, val in _PATCHES:
            if target.endswith("_build_transcript_path"):
                stack.enter_context(patch(target, return_value=pathlib.Path("/tmp/t.log")))
            elif target.endswith("agent_role_dir"):
                stack.enter_context(patch(target, return_value=pathlib.Path(val)))
            else:
                stack.enter_context(patch(target, return_value=val))
        stack.enter_context(
            patch("agent_takkub.orchestrator.PtySession", return_value=mock_session)
        )
        stack.enter_context(patch.object(orch, "_auto_trust"))
        stack.enter_context(patch("agent_takkub.spawn_engine._uuid.uuid4", return_value=FIXED_UUID))
        ok, msg = orch.spawn(role_name, cwd=FAKE_CWD, project=project)

    assert ok, f"spawn({role_name!r}) unexpectedly failed: {msg}"
    assert "argv" in captured, "PtySession.spawn was not called"
    return captured["argv"]


def test_assemble_claude_argv_reproduces_live_branch_argv(qapp, monkeypatch):
    real_argv = _spawn_claude_and_capture_argv(qapp, monkeypatch)

    reassembled = assemble_claude_argv(
        "fake-claude",
        setting_sources="project,local",
        settings_argv=["--settings", "/tmp/fake-hook-settings.json"],
        model_argv=["--model", "claude-sonnet-5"],
        effort_argv=["--effort", "high"],
        fallback_argv=["--fallback-model", "claude-haiku-4-5"],
        disallowed_tools_argv=["--disallowedTools", "Task"],
        plugin_dir_argv=[],
        system_prompt_argv=[],
        mcp_argv=FAKE_MCP_ARGV,
        denied_tools_argv=["--disallowed-tools", "Task,AskUserQuestion"],
        resume_argv=["--session-id", str(FIXED_UUID)],
    )

    assert real_argv == reassembled, (
        f"assemble_claude_argv() drifted from the live claude branch's real argv.\n"
        f"live:        {real_argv}\n"
        f"reassembled: {reassembled}"
    )
