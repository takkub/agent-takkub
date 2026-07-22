"""OpenCode provider registration (#103 Phase 1) — the first provider added
purely through PROVIDER_REGISTRY + the generic non-claude spawn branch, with
no hand-written branch of its own.

Covers the registry surface (spec fields, ready-rule table integration,
forced role, togglability) and an end-to-end spawn through the generic
branch capturing the real argv, mirroring test_spawn_codex_argv.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import Orchestrator
from agent_takkub.provider_spec import (
    PROVIDER_REGISTRY,
    READY_HARD_BLOCKERS,
    READY_RULES,
    opencode_spec,
)

TEST_PROJECT = "opencodetest"


class TestOpencodeSpec:
    def test_registered(self) -> None:
        assert PROVIDER_REGISTRY["opencode"] is opencode_spec

    def test_valid_provider_everywhere(self) -> None:
        from agent_takkub.provider_config import OPENCODE, VALID_PROVIDERS

        assert OPENCODE in VALID_PROVIDERS

    def test_forced_role(self) -> None:
        """A role literally named `opencode` is always backed by opencode —
        same contract as the codex/gemini roles."""
        from agent_takkub import provider_config

        assert provider_config.provider_for("opencode") == "opencode"
        assert "opencode" in provider_config.FORCED_ROLES

    def test_togglable(self) -> None:
        from agent_takkub.provider_state import TOGGLABLE

        assert "opencode" in TOGGLABLE
        assert "claude" not in TOGGLABLE  # baseline is never togglable

    def test_ready_rule_in_global_table(self) -> None:
        assert (True, "ctrl+p commands") in READY_RULES

    def test_ready_marker_no_substring_collision(self) -> None:
        """opencode's marker must not be a substring of (or contain) any other
        provider's marker/blocker — position in the ordered concat then
        carries no precedence weight (see _READY_RULES_BY_PROVIDER comment)."""
        marker = "ctrl+p commands"
        others = [m for _, m in READY_RULES if m != marker] + list(READY_HARD_BLOCKERS)
        for other in others:
            assert marker not in other and other not in marker, (
                f"collision between {marker!r} and {other!r}"
            )

    def test_classify_ready_on_captured_footer(self) -> None:
        """The footer captured from a real opencode 1.18.3 ConPTY session
        (2026-07-17 calibration) must classify as ready."""
        from agent_takkub.pty_session import _classify_ready

        footer = "tab agents  ctrl+p commands"
        assert _classify_ready(footer) is True

    def test_busy_blockers_still_win(self) -> None:
        """Global hard blockers must override the idle footer if both render
        (e.g. scrollback shows the footer while an interrupt hint is live)."""
        from agent_takkub.pty_session import _classify_ready

        assert _classify_ready("esc to interrupt\ntab agents  ctrl+p commands") is False


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


def _make_orchestrator(qapp, monkeypatch):
    monkeypatch.setattr(Orchestrator, "_resolve_project", staticmethod(lambda p: p or TEST_PROJECT))
    o = Orchestrator()
    o._idle_watchdog.stop()
    return o


def _make_pane(role: str = "opencode"):
    pane = MagicMock()
    pane.role = MagicMock()
    pane.role.name = role
    pane.session = None
    pane.state = "empty"
    pane._transcript_path = None
    return pane


class TestOpencodeSpawnThroughGenericBranch:
    def _spawn_and_capture(self, qapp, monkeypatch, tmp_path):
        from agent_takkub import pane_tools_policy as ptp
        from agent_takkub import shared_dev_tools as sdt
        from agent_takkub.provider_config import OPENCODE

        orch = _make_orchestrator(qapp, monkeypatch)
        pane = _make_pane("opencode")
        orch._panes_by_project[TEST_PROJECT] = {"opencode": pane}

        monkeypatch.setattr(sdt, "SHARED_MCP_FILE", tmp_path / "shared-mcp.json")
        monkeypatch.setattr(ptp, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")

        pty_spawn_calls: list[dict] = []

        with (
            patch.object(orch, "_is_spawn_blocked", return_value=False),
            patch.object(orch, "_final_gate_clear", return_value=True),
            patch("agent_takkub.orchestrator.PtySession") as mock_pty_cls,
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
            patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
            patch(
                "agent_takkub.provider_config.effective_provider_for",
                return_value=OPENCODE,
            ),
            # ProviderSpec is frozen — can't patch custom_discovery_fn on the
            # instance. _discover_opencode lazily imports shutil, so patching
            # shutil.which pins the discovered binary name deterministically
            # (the real machine may or may not have opencode installed).
            patch(
                "shutil.which",
                side_effect=lambda n: "opencode" if str(n).startswith("opencode") else None,
            ),
            patch("agent_takkub.codex_agents_md.ensure_agents_md") as mock_agents_md,
            patch("agent_takkub.orchestrator.inject_user_profile_env"),
        ):
            mock_pty = MagicMock()
            mock_pty.spawn.side_effect = lambda **kw: pty_spawn_calls.append(kw)
            mock_pty_cls.return_value = mock_pty
            pane.attach_session = MagicMock()

            ok, msg = orch.spawn("opencode", project=TEST_PROJECT)

        assert ok is True, msg
        assert pty_spawn_calls, "PtySession.spawn was not called"
        return pty_spawn_calls[0], mock_agents_md

    def test_argv_is_binary_plus_auto(self, qapp, monkeypatch, tmp_path) -> None:
        spawn_kw, _ = self._spawn_and_capture(qapp, monkeypatch, tmp_path)
        assert spawn_kw["argv"] == ["opencode", "--auto"]

    def test_env_has_role_and_project(self, qapp, monkeypatch, tmp_path) -> None:
        spawn_kw, _ = self._spawn_and_capture(qapp, monkeypatch, tmp_path)
        env = spawn_kw["env"]
        assert env["TAKKUB_ROLE"] == "opencode"
        assert env["TAKKUB_PROJECT"] == TEST_PROJECT

    def test_agents_md_cheatsheet_planted(self, qapp, monkeypatch, tmp_path) -> None:
        """opencode reads AGENTS.md natively → the generic branch must plant
        the takkub cheatsheet exactly like it does for codex/gemini."""
        _, mock_agents_md = self._spawn_and_capture(qapp, monkeypatch, tmp_path)
        assert mock_agents_md.called
