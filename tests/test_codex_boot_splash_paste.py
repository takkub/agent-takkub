"""codex "Starting MCP servers (0/3)" splash → paste after a short settle.

Root-caused live 2026-08-27 on the dev cockpit: codex's own log DB shows every
injected MCP server initialised within ~2-3s of spawn, yet the TUI keeps
painting the boot splash (elapsed counter frozen at 0s) and never shows its
ready prompt until either any input reaches the pane or ~110s pass. The
composer behind the splash is live — a single keystroke flipped the pane to
"Working" with the text in the composer — so delivery may paste+submit once
``ProviderSpec.boot_splash_paste_after_s`` has elapsed instead of sitting on
the boot marker until the blind-paste fallback (90s) / boot-stall grace (110s).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator
from agent_takkub.provider_spec import PROVIDER_REGISTRY, boot_splash_paste_after_s_for


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _live_session(*, boot_line: str) -> MagicMock:
    s = MagicMock()
    s.is_alive = True
    s.write = MagicMock(return_value=True)
    s.is_at_trust_prompt.return_value = False
    s.is_blocked_on_tty_prompt.return_value = None
    s.is_blocked_on_permission_prompt.return_value = None
    s.shows_startup_marker.return_value = True
    s.shows_boot_phase_marker.return_value = True  # splash never clears on its own
    s.boot_phase_detail.return_value = boot_line
    s.is_at_ready_prompt.return_value = False
    s.shows_account_pending_marker.return_value = False
    s.seconds_since_output.return_value = 0.5  # the splash keeps redrawing
    return s


def _pane(session: MagicMock, provider: str) -> MagicMock:
    p = MagicMock()
    p.session = session
    p.model.provider_name = provider
    p._session_generation = 0
    return p


@pytest.fixture
def orch(qapp, monkeypatch) -> Orchestrator:
    o = Orchestrator.__new__(Orchestrator)
    QObject.__init__(o)
    o._panes_by_project = {}
    monkeypatch.setattr(o, "_resolve_project", lambda p=None: p or "P")
    monkeypatch.setattr(
        o, "_project_panes", lambda p=None: o._panes_by_project.get(o._resolve_project(p), {})
    )
    monkeypatch.setattr(o, "_run_boot_diagnostic_async", MagicMock())
    # No real Qt event loop: run each poll tick inline.
    monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))
    return o


def _written(session: MagicMock) -> list[str]:
    return [c.args[0] for c in session.write.call_args_list if c.args]


def _events(log: MagicMock) -> list[str]:
    return [c.args[0] for c in log.call_args_list if c.args]


MCP_SPLASH = (
    "• Starting MCP servers (0/3): chrome-devtools, graft, playwright (0s • esc to interrupt)"
)


class TestSpec:
    def test_codex_has_window_and_others_do_not(self) -> None:
        assert PROVIDER_REGISTRY["codex"].boot_splash_paste_after_s == 10.0
        for name, spec in PROVIDER_REGISTRY.items():
            if name != "codex":
                assert spec.boot_splash_paste_after_s == 0.0, name

    def test_env_override_and_unknown_provider(self, monkeypatch) -> None:
        monkeypatch.setenv("TAKKUB_BOOT_SPLASH_PASTE_AFTER_S_CODEX", "2.5")
        assert boot_splash_paste_after_s_for("codex") == 2.5
        monkeypatch.setenv("TAKKUB_BOOT_SPLASH_PASTE_AFTER_S_CODEX", "nope")
        assert boot_splash_paste_after_s_for("codex") == 10.0
        monkeypatch.setenv("TAKKUB_BOOT_SPLASH_PASTE_AFTER_S_CODEX", "-3")
        assert boot_splash_paste_after_s_for("codex") == 0.0
        assert boot_splash_paste_after_s_for("no-such-provider") == 0.0


class TestDelivery:
    def test_codex_pastes_onto_mcp_splash_after_window(self, orch, monkeypatch) -> None:
        monkeypatch.setenv("TAKKUB_BOOT_SPLASH_PASTE_AFTER_S_CODEX", "0.45")  # 3 polls
        codex = _pane(_live_session(boot_line=MCP_SPLASH), "codex")
        orch._panes_by_project["P"] = {
            "lead": _pane(_live_session(boot_line=""), "claude"),
            "qa": codex,
        }

        with patch("agent_takkub.lead_inbox._log_event") as log:
            orch._send_when_ready("qa", "run smoke", max_wait_ms=3_000, project="P")

        assert any("run smoke" in w for w in _written(codex.session)), "task must be pasted"
        assert "task_deliver_boot_splash_paste" in _events(log)
        splash = [
            c
            for c in log.call_args_list
            if c.args and c.args[0] == "task_deliver_boot_splash_paste"
        ]
        assert splash[0].kwargs["role"] == "qa"
        assert "Starting MCP servers" in splash[0].kwargs["boot_line"]
        # Pasted well before the ordinary ready-wait / blind-paste fallback.
        assert splash[0].kwargs["elapsed_sec"] < 1.0

    def test_window_measured_from_session_alive_not_before(self, orch, monkeypatch) -> None:
        monkeypatch.setenv("TAKKUB_BOOT_SPLASH_PASTE_AFTER_S_CODEX", "0.45")
        codex = _pane(_live_session(boot_line=MCP_SPLASH), "codex")
        # Session shows up only after 4 polls: the window must start THEN.
        codex.session.is_alive = False
        alive = [False] * 4 + [True] * 100
        type(codex.session).is_alive = property(lambda _s: alive.pop(0) if alive else True)
        orch._panes_by_project["P"] = {
            "lead": _pane(_live_session(boot_line=""), "claude"),
            "qa": codex,
        }
        with patch("agent_takkub.lead_inbox._log_event") as log:
            orch._send_when_ready("qa", "run smoke", max_wait_ms=3_000, project="P")
        splash = [
            c
            for c in log.call_args_list
            if c.args and c.args[0] == "task_deliver_boot_splash_paste"
        ]
        assert splash, "must still paste once the window elapses after the session came up"
        assert 0.4 <= splash[0].kwargs["elapsed_sec"] < 1.0

    def test_non_mcp_boot_screen_is_never_pasted_onto(self, orch, monkeypatch) -> None:
        """Only the MCP splash has a live composer behind it — a login/trust
        style boot screen must keep the ordinary wait (and its escalations)."""
        monkeypatch.setenv("TAKKUB_BOOT_SPLASH_PASTE_AFTER_S_CODEX", "0.15")
        codex = _pane(_live_session(boot_line="Loading… please wait"), "codex")
        # Let the splash clear after 6 polls so the poll loop terminates normally.
        codex.session.shows_boot_phase_marker.side_effect = [True] * 6 + [False] * 200
        codex.session.is_at_ready_prompt.side_effect = [False] * 6 + [True] * 200
        orch._panes_by_project["P"] = {
            "lead": _pane(_live_session(boot_line=""), "claude"),
            "qa": codex,
        }
        with patch("agent_takkub.lead_inbox._log_event") as log:
            orch._send_when_ready("qa", "run smoke", max_wait_ms=3_000, project="P")
        assert "task_deliver_boot_splash_paste" not in _events(log)
        assert any("run smoke" in w for w in _written(codex.session))  # ordinary path delivered

    def test_provider_without_window_keeps_waiting(self, orch, monkeypatch) -> None:
        claude = _pane(_live_session(boot_line=MCP_SPLASH), "claude")
        claude.session.shows_boot_phase_marker.side_effect = [True] * 6 + [False] * 200
        claude.session.is_at_ready_prompt.side_effect = [False] * 6 + [True] * 200
        orch._panes_by_project["P"] = {
            "lead": _pane(_live_session(boot_line=""), "claude"),
            "backend": claude,
        }
        with patch("agent_takkub.lead_inbox._log_event") as log:
            orch._send_when_ready("backend", "run smoke", max_wait_ms=3_000, project="P")
        assert "task_deliver_boot_splash_paste" not in _events(log)

    def test_modal_prompt_on_top_of_splash_blocks_the_paste(self, orch, monkeypatch) -> None:
        monkeypatch.setenv("TAKKUB_BOOT_SPLASH_PASTE_AFTER_S_CODEX", "0.15")
        codex = _pane(_live_session(boot_line=MCP_SPLASH), "codex")
        codex.session.is_at_trust_prompt.side_effect = [True] * 6 + [False] * 200
        codex.session.shows_boot_phase_marker.side_effect = [True] * 6 + [False] * 200
        codex.session.is_at_ready_prompt.side_effect = [False] * 6 + [True] * 200
        orch._panes_by_project["P"] = {
            "lead": _pane(_live_session(boot_line=""), "claude"),
            "qa": codex,
        }
        with patch("agent_takkub.lead_inbox._log_event") as log:
            orch._send_when_ready("qa", "run smoke", max_wait_ms=3_000, project="P")
        assert "task_deliver_boot_splash_paste" not in _events(log)
