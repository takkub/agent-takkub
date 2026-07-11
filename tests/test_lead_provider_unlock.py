"""Issue #101 — degraded-mode unlock: Lead can now be backed by codex/agy,
not just claude. These tests exercise the GEMINI/CODEX spawn branches'
`_is_lead` special-casing added to `spawn_engine.py`:

  - cwd resolves via `lead_cwd()` (project root), not `default_cwd_for_role()`
    (a teammate staging dir).
  - context is planted via `lead_context.render_lead_agents_md` (the SAME
    cockpit CLAUDE.md + BLOCKED_DIRS content claude gets, just delivered as
    an AGENTS.md file) — never the generic teammate cheatsheet
    (`codex_agents_md.ensure_agents_md`), which would tell Lead "you are a
    specialist, do the task yourself" — wrong for the orchestrator role.
  - env carries `TAKKUB_LEAD_TOKEN` (Lead-only takkub CLI auth) instead of a
    per-pane teammate token, and uses `_build_lead_env()` (git/gh passthrough)
    instead of `_build_pane_env()`.

Mirrors the fixture shape of `test_h1_nonclaude_env.py`.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import Orchestrator

TEST_PROJECT = "leadunlocktest"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


def _make_orchestrator(monkeypatch):
    monkeypatch.setattr(Orchestrator, "_resolve_project", staticmethod(lambda p: p or TEST_PROJECT))
    o = Orchestrator()
    o._idle_watchdog.stop()
    return o


def _make_pane():
    pane = MagicMock()
    pane.role = MagicMock()
    pane.role.name = "lead"
    pane.session = None
    pane.state = "empty"
    pane._transcript_path = None
    return pane


def _spawn_lead_and_capture(qapp, monkeypatch, tmp_path, effective_provider, *extra_patches):
    orch = _make_orchestrator(monkeypatch)
    orch._lead_token = "test-lead-token"
    pane = _make_pane()
    orch._panes_by_project[TEST_PROJECT] = {"lead": pane}

    pty_spawn_calls: list[dict] = []
    agents_md_calls: list[tuple] = []

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
            patch(
                "agent_takkub.provider_config.effective_provider_for",
                return_value=effective_provider,
            )
        )
        stack.enter_context(
            patch(
                "agent_takkub.spawn_engine.render_lead_agents_md",
                side_effect=lambda *a, **kw: agents_md_calls.append((a, kw)),
            )
        )
        # Teammate cheatsheet must NEVER be called for Lead.
        mock_ensure = stack.enter_context(patch("agent_takkub.codex_agents_md.ensure_agents_md"))
        for p in extra_patches:
            stack.enter_context(p)

        mock_pty = MagicMock()
        mock_pty.spawn.side_effect = lambda **kw: pty_spawn_calls.append(kw)
        mock_pty_cls.return_value = mock_pty
        pane.attach_session = MagicMock()

        ok, msg = orch.spawn("lead", cwd=str(tmp_path), project=TEST_PROJECT)

    assert ok is True, msg
    assert pty_spawn_calls, "PtySession.spawn was not called"
    return pty_spawn_calls[0], agents_md_calls, mock_ensure


class TestLeadThroughGeminiBranch:
    def test_lead_cwd_and_token(self, qapp, monkeypatch, tmp_path):
        call, agents_md_calls, mock_ensure = _spawn_lead_and_capture(
            qapp,
            monkeypatch,
            tmp_path,
            "gemini",
            patch("agent_takkub.gemini_helper.find_agy_executable", return_value="agy"),
        )
        assert call["cwd"] == str(tmp_path)
        assert call["env"]["TAKKUB_LEAD_TOKEN"] == "test-lead-token"
        assert "TAKKUB_PANE_TOKEN" not in call["env"]
        assert agents_md_calls, "render_lead_agents_md was not called for a gemini-backed Lead"
        assert agents_md_calls[0][0][:2] == (TEST_PROJECT, str(tmp_path))
        mock_ensure.assert_not_called()


class TestLeadThroughCodexBranch:
    def test_lead_cwd_and_token(self, qapp, monkeypatch, tmp_path):
        call, agents_md_calls, mock_ensure = _spawn_lead_and_capture(
            qapp,
            monkeypatch,
            tmp_path,
            "codex",
            patch("agent_takkub.codex_helper.find_codex_executable", return_value="codex"),
        )
        assert call["cwd"] == str(tmp_path)
        assert call["env"]["TAKKUB_LEAD_TOKEN"] == "test-lead-token"
        assert "TAKKUB_PANE_TOKEN" not in call["env"]
        assert agents_md_calls, "render_lead_agents_md was not called for a codex-backed Lead"
        assert agents_md_calls[0][0][:2] == (TEST_PROJECT, str(tmp_path))
        mock_ensure.assert_not_called()
