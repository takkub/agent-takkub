"""Fail-closed contract for the codex "session_override" MCP bridge
(follow-up to #100/#121): if `mcp_bridge._codex_resolved_mcp_names` can't
resolve codex's own inherited MCP set (CLI crash/timeout/malformed
output), `spawn_engine.spawn()` must refuse the spawn with `(False, msg)`
instead of letting `McpResolutionError` escape uncaught up the spawn path,
or silently spawning the pane with an unfiltered/under-denied MCP set.

Mirrors the fixture shape of `test_h1_nonclaude_env.py` /
`test_lead_provider_unlock.py`.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import Orchestrator

TEST_PROJECT = "mcpfailclosedtest"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


def _make_orchestrator(monkeypatch):
    monkeypatch.setattr(Orchestrator, "_resolve_project", staticmethod(lambda p: p or TEST_PROJECT))
    o = Orchestrator()
    o.shutdown_timers()
    return o


def _make_pane(role: str):
    pane = MagicMock()
    pane.role = MagicMock()
    pane.role.name = role
    pane.session = None
    pane.state = "empty"
    pane._transcript_path = None
    return pane


class TestCodexMcpResolutionFailure:
    def test_spawn_refuses_instead_of_raising_or_leaking(self, qapp, monkeypatch):
        from agent_takkub import mcp_bridge
        from agent_takkub.provider_config import CODEX

        orch = _make_orchestrator(monkeypatch)
        pane = _make_pane("backend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}

        pty_spawn_calls: list[dict] = []

        def _boom(*args):
            raise mcp_bridge.McpResolutionError("codex CLI timed out")

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(orch, "_is_spawn_blocked", return_value=False))
            stack.enter_context(patch.object(orch, "_final_gate_clear", return_value=True))
            stack.enter_context(
                patch("agent_takkub.spawn_engine._cwd_within_project", return_value=True)
            )
            mock_pty_cls = stack.enter_context(patch("agent_takkub.orchestrator.PtySession"))
            stack.enter_context(patch("agent_takkub.orchestrator.QTimer.singleShot"))
            stack.enter_context(patch("agent_takkub.orchestrator.inject_user_profile_env"))
            stack.enter_context(
                patch("agent_takkub.provider_config.effective_provider_for", return_value=CODEX)
            )
            stack.enter_context(
                patch("agent_takkub.codex_helper.find_codex_executable", return_value="codex")
            )
            stack.enter_context(patch("agent_takkub.codex_agents_md.ensure_agents_md"))
            # Role must have an explicit cockpit MCP policy (non-None) so the
            # session_override branch actually calls _codex_resolved_mcp_names
            # — a role with no policy at all takes the passthrough path and
            # never hits this failure mode.
            stack.enter_context(
                patch("agent_takkub.shared_dev_tools.role_mcp_allowlist", return_value=frozenset())
            )
            stack.enter_context(
                patch("agent_takkub.mcp_bridge._codex_resolved_mcp_names", side_effect=_boom)
            )
            # Force the version-gate branch that still resolves (see
            # mcp_bridge._CODEX_RESOLVE_SAFE_MIN_VERSION / docs/reviews/
            # 2026-08-05-graft-mcp-security.md H1) — a verified-safe codex
            # build skips _codex_resolved_mcp_names entirely and can no
            # longer hit this failure mode at all.
            stack.enter_context(
                patch("agent_takkub.mcp_bridge._codex_cli_version", return_value=None)
            )

            mock_pty = MagicMock()
            mock_pty.spawn.side_effect = lambda **kw: pty_spawn_calls.append(kw)
            mock_pty_cls.return_value = mock_pty
            pane.attach_session = MagicMock()

            ok, msg = orch.spawn("backend", project=TEST_PROJECT)

        assert ok is False
        assert "backend" in msg
        assert not pty_spawn_calls, "pane must not spawn when the MCP deny-list can't be resolved"

    def test_verified_safe_codex_version_spawns_despite_resolve_failure(self, qapp, monkeypatch):
        """H1 (docs/reviews/2026-08-05-graft-mcp-security.md): a codex-cli
        within the closed `_CODEX_RESOLVE_SAFE_MIN_VERSION`..
        `_CODEX_RESOLVE_SAFE_MAX_VERSION` window never even calls the
        resolve subprocess (`-c mcp_servers={}` alone is deny-by-default,
        verified against real 0.144.1/0.145.0/0.146.0 binaries) — so a
        resolve failure that would hard-abort on an old/unknown/too-new
        codex build (#352) must NOT block the spawn here."""
        from agent_takkub import mcp_bridge
        from agent_takkub.provider_config import CODEX

        orch = _make_orchestrator(monkeypatch)
        pane = _make_pane("backend")
        orch._panes_by_project[TEST_PROJECT] = {"backend": pane}

        pty_spawn_calls: list[dict] = []

        def _boom(*args):
            raise mcp_bridge.McpResolutionError("codex CLI timed out")

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(orch, "_is_spawn_blocked", return_value=False))
            stack.enter_context(patch.object(orch, "_final_gate_clear", return_value=True))
            stack.enter_context(
                patch("agent_takkub.spawn_engine._cwd_within_project", return_value=True)
            )
            mock_pty_cls = stack.enter_context(patch("agent_takkub.orchestrator.PtySession"))
            stack.enter_context(patch("agent_takkub.orchestrator.QTimer.singleShot"))
            stack.enter_context(patch("agent_takkub.orchestrator.inject_user_profile_env"))
            stack.enter_context(
                patch("agent_takkub.provider_config.effective_provider_for", return_value=CODEX)
            )
            stack.enter_context(
                patch("agent_takkub.codex_helper.find_codex_executable", return_value="codex")
            )
            stack.enter_context(patch("agent_takkub.codex_agents_md.ensure_agents_md"))
            stack.enter_context(
                patch("agent_takkub.shared_dev_tools.role_mcp_allowlist", return_value=frozenset())
            )
            stack.enter_context(
                patch("agent_takkub.mcp_bridge._codex_resolved_mcp_names", side_effect=_boom)
            )
            stack.enter_context(
                patch(
                    "agent_takkub.mcp_bridge._codex_cli_version",
                    return_value=mcp_bridge._CODEX_RESOLVE_SAFE_MIN_VERSION,
                )
            )

            mock_pty = MagicMock()
            mock_pty.spawn.side_effect = lambda **kw: pty_spawn_calls.append(kw)
            mock_pty_cls.return_value = mock_pty
            pane.attach_session = MagicMock()

            ok, msg = orch.spawn("backend", project=TEST_PROJECT)

        assert ok is True, msg
        assert pty_spawn_calls, "pane must still spawn — the resolve step was never needed"
