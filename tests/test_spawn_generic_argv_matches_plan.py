"""Characterization test (issue #347): proves
`core.providers.plan.assemble_generic_argv` reproduces, byte for byte, the
argv `spawn_engine.py`'s LIVE generic (non-claude) branch actually builds
today — not a hand-transcribed approximation of it.

Same shape as `test_spawn_claude_argv_matches_claude_plan.py` (Wave A):
spawns a REAL `Orchestrator.spawn()` for a codex pane (only `PtySession` is
mocked, to capture the argv instead of executing a real ConPTY launch),
captures the argv the live branch actually passed to `session.spawn()`, and
asserts it equals `assemble_generic_argv()` called with the same
caller-resolved pieces (model/mcp/resume pinned via monkeypatch so both
sides start from identical resolved values). Effort comes from codex's own
tier default (unpatched) — same as `test_spawn_codex_argv.py`'s existing
assertions.

This test does not require `spawn_engine.py` to call `assemble_generic_argv`
to be meaningful — it independently re-derives both sides and asserts
equality, so it fails the moment the two drift. Wave A's report (§9 Q2)
flagged this as the open gap on the generic side; this closes it before the
wiring step (§8/§9) touches the branch.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.core.providers.plan import assemble_generic_argv
from agent_takkub.orchestrator import Orchestrator

TEST_PROJECT = "genericargvplantest"
FAKE_MCP_ARGV = ["-c", 'mcp_servers.demo.command="node"']
RESUME_UUID = "fixed-resume-uuid-1"


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


def _make_codex_pane() -> MagicMock:
    pane = MagicMock()
    pane.role = MagicMock()
    pane.role.name = "codex"
    pane.session = None
    pane.state = "empty"
    pane._transcript_path = None
    return pane


def _spawn_codex_and_capture_argv(qapp, monkeypatch, tmp_path) -> list[str]:
    from agent_takkub import pane_tools_policy as ptp
    from agent_takkub import shared_dev_tools as sdt
    from agent_takkub.provider_config import CODEX

    orch = _make_orchestrator(qapp, monkeypatch)
    pane = _make_codex_pane()
    orch._panes_by_project[TEST_PROJECT] = {"codex": pane}

    # Isolate from the real dev machine's runtime/shared-mcp.json +
    # ~/.takkub/pane-tools.json (#100) — same isolation
    # test_spawn_codex_argv.py uses, so MCP injection here is deterministic
    # rather than machine-dependent.
    monkeypatch.setattr(sdt, "SHARED_MCP_FILE", tmp_path / "shared-mcp.json")
    monkeypatch.setattr(ptp, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")

    pty_spawn_calls: list[dict] = []

    with (
        patch.object(orch, "_is_spawn_blocked", return_value=False),
        patch.object(orch, "_final_gate_clear", return_value=True),
        patch("agent_takkub.orchestrator.PtySession") as mock_pty_cls,
        patch("agent_takkub.orchestrator.QTimer.singleShot"),
        patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
        patch("agent_takkub.spawn_engine.sys.platform", "win32"),
        patch("agent_takkub.provider_config.effective_provider_for", return_value=CODEX),
        patch("agent_takkub.codex_helper.find_codex_executable", return_value="codex"),
        patch("agent_takkub.codex_agents_md.ensure_agents_md"),
        patch("agent_takkub.orchestrator.inject_user_profile_env"),
        # Force a real --model flag into the argv (default test env has no
        # role/provider model configured, which would leave model_argv empty
        # and under-exercise the piece this test exists to guard).
        patch("agent_takkub.provider_models.model_for", return_value="gpt-5-codex"),
        patch("agent_takkub.mcp_bridge._codex_resolved_mcp_names", return_value=["demo"]),
        patch("agent_takkub.mcp_bridge.mcp_argv_for_provider", return_value=FAKE_MCP_ARGV),
        patch("agent_takkub.mcp_bridge.describe_mcp_handshake", return_value={}),
        # Resume: real cwd/JSONL validation is orthogonal to this test's
        # purpose (argv-order equivalence) — pin it to "valid" so
        # resume_argv is non-empty, same reasoning claude's characterization
        # test pins the session-id uuid.
        patch("agent_takkub.spawn_engine._resume_uuid_matches_provider_cwd", return_value=True),
    ):
        mock_pty = MagicMock()
        mock_pty.spawn.side_effect = lambda **kw: pty_spawn_calls.append(kw)
        mock_pty_cls.return_value = mock_pty
        pane.attach_session = MagicMock()

        ok, msg = orch.spawn("codex", project=TEST_PROJECT, resume_uuid=RESUME_UUID)

    assert ok is True, msg
    assert pty_spawn_calls, "PtySession.spawn was not called"
    return pty_spawn_calls[0]["argv"]


def test_assemble_generic_argv_reproduces_live_branch_argv(qapp, monkeypatch, tmp_path):
    real_argv = _spawn_codex_and_capture_argv(qapp, monkeypatch, tmp_path)

    reassembled = assemble_generic_argv(
        "codex",
        autonomy_argv=[
            "--dangerously-bypass-approvals-and-sandbox",
            "--disable",
            "apps",
        ],
        model_argv=["--model", "gpt-5-codex"],
        effort_argv=["-c", "model_reasoning_effort=high"],
        mcp_argv=FAKE_MCP_ARGV,
        project_scope_argv=[],
        resume_argv=["resume", RESUME_UUID],
    )

    assert real_argv == reassembled, (
        f"assemble_generic_argv() drifted from the live generic branch's real argv.\n"
        f"live:        {real_argv}\n"
        f"reassembled: {reassembled}"
    )
