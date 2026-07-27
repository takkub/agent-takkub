"""Tests for provider project scoping (#132).

agy dumps every conversation without an explicit `--project <id>` into a
shared `default-cli-project` pseudo-project, mixing every takkub project's
Lead/teammate conversation history together. `ProviderSpec.project_scope_*`
(provider_spec.py) lets spawn_engine.py resolve an existing agy project id
from the spawn cwd and pass `--project <id>` — or `--new-project` when no
existing agy project covers this folder — without hardcoding "gemini"
anywhere in spawn_engine.py itself.

These tests pin down:
  - gemini spawn argv gets `--project <id>` when the resolver matches
  - gemini spawn argv gets `--new-project` when the resolver finds nothing
  - codex/opencode spawn argv is untouched (project_scope_flag is None there)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import Orchestrator

TEST_PROJECT = "projectscopetest"


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
    o._idle_watchdog.stop()
    return o


def _make_pane(role: str):
    pane = MagicMock()
    pane.role = MagicMock()
    pane.role.name = role
    pane.session = None
    pane.state = "empty"
    pane._transcript_path = None
    return pane


def _spawn_and_capture_argv(qapp, monkeypatch, role: str, *extra_patches) -> list[str]:
    import contextlib

    orch = _make_orchestrator(qapp, monkeypatch)
    pane = _make_pane(role)
    orch._panes_by_project[TEST_PROJECT] = {role: pane}

    pty_spawn_calls: list[dict] = []

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(orch, "_is_spawn_blocked", return_value=False))
        stack.enter_context(patch.object(orch, "_final_gate_clear", return_value=True))
        mock_pty_cls = stack.enter_context(patch("agent_takkub.orchestrator.PtySession"))
        stack.enter_context(patch("agent_takkub.orchestrator.QTimer.singleShot"))
        stack.enter_context(patch("agent_takkub.orchestrator._build_pane_env", return_value={}))
        stack.enter_context(patch("agent_takkub.orchestrator.inject_user_profile_env"))
        stack.enter_context(patch("agent_takkub.codex_agents_md.ensure_agents_md"))
        for p in extra_patches:
            stack.enter_context(p)

        mock_pty = MagicMock()
        mock_pty.spawn.side_effect = lambda **kw: pty_spawn_calls.append(kw)
        mock_pty_cls.return_value = mock_pty
        pane.attach_session = MagicMock()

        ok, msg = orch.spawn(role, project=TEST_PROJECT)

    assert ok is True, msg
    assert pty_spawn_calls, "PtySession.spawn was not called"
    return pty_spawn_calls[0]["argv"]


class TestGeminiArgvGetsProjectScope:
    def test_resolved_id_appends_project_flag(self, qapp, monkeypatch):
        from agent_takkub.provider_config import GEMINI

        argv = _spawn_and_capture_argv(
            qapp,
            monkeypatch,
            "gemini",
            patch("agent_takkub.provider_config.effective_provider_for", return_value=GEMINI),
            patch("agent_takkub.gemini_helper.find_agy_executable", return_value="agy"),
            patch(
                "agent_takkub.gemini_helper.resolve_agy_project_id",
                return_value="17fdc03a-8cb3-446e-a833-4aaffc55f6bb",
            ),
        )
        assert "--project" in argv
        idx = argv.index("--project")
        assert argv[idx + 1] == "17fdc03a-8cb3-446e-a833-4aaffc55f6bb"
        assert "--new-project" not in argv

    def test_no_match_falls_back_to_new_project_flag(self, qapp, monkeypatch):
        from agent_takkub.provider_config import GEMINI

        argv = _spawn_and_capture_argv(
            qapp,
            monkeypatch,
            "gemini",
            patch("agent_takkub.provider_config.effective_provider_for", return_value=GEMINI),
            patch("agent_takkub.gemini_helper.find_agy_executable", return_value="agy"),
            patch("agent_takkub.gemini_helper.resolve_agy_project_id", return_value=None),
        )
        assert "--new-project" in argv
        assert "--project" not in argv

    def test_resolver_exception_degrades_to_new_project_flag(self, qapp, monkeypatch):
        # A resolver crash (e.g. unreadable/corrupt registry) must not break
        # the whole gemini spawn — degrade to --new-project like "no match".
        from agent_takkub.provider_config import GEMINI

        def _boom(_cwd):
            raise OSError("disk error")

        argv = _spawn_and_capture_argv(
            qapp,
            monkeypatch,
            "gemini",
            patch("agent_takkub.provider_config.effective_provider_for", return_value=GEMINI),
            patch("agent_takkub.gemini_helper.find_agy_executable", return_value="agy"),
            patch("agent_takkub.gemini_helper.resolve_agy_project_id", side_effect=_boom),
        )
        assert "--new-project" in argv
        assert "--project" not in argv


class TestOtherProvidersUnaffected:
    """project_scope_flag is opt-in per ProviderSpec — codex/opencode must
    never see --project/--new-project injected into their argv."""

    def test_codex_argv_has_no_project_scope_flags(self, qapp, monkeypatch, tmp_path):
        from agent_takkub import pane_tools_policy as ptp
        from agent_takkub import shared_dev_tools as sdt
        from agent_takkub.provider_config import CODEX

        monkeypatch.setattr(sdt, "SHARED_MCP_FILE", tmp_path / "shared-mcp.json")
        monkeypatch.setattr(ptp, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")

        argv = _spawn_and_capture_argv(
            qapp,
            monkeypatch,
            "codex",
            patch("agent_takkub.provider_config.effective_provider_for", return_value=CODEX),
            patch("agent_takkub.codex_helper.find_codex_executable", return_value="codex"),
            patch("agent_takkub.mcp_bridge._codex_resolved_mcp_names", return_value=[]),
        )
        assert "--project" not in argv
        assert "--new-project" not in argv

    def test_opencode_argv_has_no_project_scope_flags(self, qapp, monkeypatch, tmp_path):
        from agent_takkub import pane_tools_policy as ptp
        from agent_takkub import shared_dev_tools as sdt
        from agent_takkub.provider_config import OPENCODE

        monkeypatch.setattr(sdt, "SHARED_MCP_FILE", tmp_path / "shared-mcp.json")
        monkeypatch.setattr(ptp, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")

        argv = _spawn_and_capture_argv(
            qapp,
            monkeypatch,
            "opencode",
            patch("agent_takkub.provider_config.effective_provider_for", return_value=OPENCODE),
            # ProviderSpec is frozen — can't patch custom_discovery_fn on the
            # instance; pin shutil.which instead (see test_opencode_provider.py).
            patch(
                "shutil.which",
                side_effect=lambda n: "opencode" if str(n).startswith("opencode") else None,
            ),
        )
        assert "--project" not in argv
        assert "--new-project" not in argv
