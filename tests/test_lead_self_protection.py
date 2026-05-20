"""Tests for Lead self-protection (defense-in-depth, 2 layers).

Layer 1 — orchestrator.py:
  - done() rejects from_role == "lead" before any pane lookup
  - close() skips session.terminate() / set_state("empty") when role is lead

Layer 2 — cli.py:
  - _enforce_role_gate("done") returns error when TAKKUB_ROLE=lead
  - _enforce_role_gate("done") returns None (allow) for teammate roles
  - _enforce_role_gate("done") returns None (allow) when TAKKUB_ROLE is unset
  - existing LEAD_ONLY_COMMANDS gate still works (regression guard)
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import cli, config
from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator

# ─────────────────────────────────────────────────────────────
# Shared fixtures
# ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def minimal_project_json(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    pj = tmp_path / "projects.json"
    pj.write_text(
        json.dumps(
            {
                "active": "proj",
                "projects": {
                    "proj": {
                        "paths": {
                            "api": str(tmp_path / "proj" / "api"),
                            "web": str(tmp_path / "proj" / "web"),
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "PROJECTS_JSON", pj)
    cockpit = tmp_path / "cockpit"
    monkeypatch.setattr(config, "REPO_ROOT", cockpit)
    monkeypatch.setattr(orch_mod, "REPO_ROOT", cockpit)
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", runtime)
    return pj


# ─────────────────────────────────────────────────────────────
# Layer 1 — orchestrator.done()
# ─────────────────────────────────────────────────────────────


class TestOrchestratorDoneRejectsLead:
    """done("lead") must short-circuit before touching any pane or emitting signals."""

    def test_returns_false_with_cannot_message(
        self,
        qapp: QCoreApplication,
        minimal_project_json: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            Orchestrator,
            "_resolve_project",
            staticmethod(lambda project: project or "proj"),
        )
        orch = Orchestrator()
        orch._idle_watchdog.stop()

        ok, msg = orch.done("lead", note="I am lead trying to close myself")
        assert ok is False
        assert "lead cannot" in msg

    def test_no_pane_lookup_occurs(
        self,
        qapp: QCoreApplication,
        minimal_project_json: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Even with no panes registered, done("lead") must not raise."""
        monkeypatch.setattr(
            Orchestrator,
            "_resolve_project",
            staticmethod(lambda project: project or "proj"),
        )
        orch = Orchestrator()
        orch._idle_watchdog.stop()
        orch._panes_by_project.clear()

        ok, msg = orch.done("lead")
        assert ok is False
        assert "lead cannot" in msg

    def test_teammate_done_still_works(
        self,
        qapp: QCoreApplication,
        minimal_project_json: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: pathlib.Path,
    ) -> None:
        """Regression: backend.done() must still reach the success path."""
        monkeypatch.setattr(
            Orchestrator,
            "_resolve_project",
            staticmethod(lambda project: project or "proj"),
        )
        orch = Orchestrator()
        orch._idle_watchdog.stop()

        # Suppress side-effects (QTimer, file I/O)
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda *_: None))
        monkeypatch.setattr(orch, "_save_decision_note", lambda *_a, **_kw: None)
        monkeypatch.setattr(orch, "_write_hot_md", lambda: None)

        backend_pane = MagicMock()
        backend_pane.session = None
        lead_pane = MagicMock()
        lead_pane.session = None
        orch._panes_by_project["proj"] = {"backend": backend_pane, "lead": lead_pane}

        ok, msg = orch.done("backend", note="done", project="proj")
        assert ok is True
        assert "backend" in msg


# ─────────────────────────────────────────────────────────────
# Layer 1 — orchestrator.close()
# ─────────────────────────────────────────────────────────────


class TestOrchestratorCloseSkipsLeadTerminate:
    """close("lead") must not call session.terminate() or set_state("empty")
    when Lead has an active session."""

    def test_terminate_not_called_for_lead(
        self,
        qapp: QCoreApplication,
        minimal_project_json: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            Orchestrator,
            "_resolve_project",
            staticmethod(lambda project: project or "proj"),
        )
        orch = Orchestrator()
        orch._idle_watchdog.stop()

        mock_session = MagicMock()
        lead_pane = MagicMock()
        lead_pane.session = mock_session

        orch._panes_by_project["proj"] = {"lead": lead_pane}

        ok, msg = orch.close("lead", project="proj")

        assert ok is True
        assert "protected" in msg
        mock_session.terminate.assert_not_called()
        lead_pane.set_state.assert_not_called()

    def test_lead_pane_remains_in_registry_after_close(
        self,
        qapp: QCoreApplication,
        minimal_project_json: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            Orchestrator,
            "_resolve_project",
            staticmethod(lambda project: project or "proj"),
        )
        orch = Orchestrator()
        orch._idle_watchdog.stop()

        mock_session = MagicMock()
        lead_pane = MagicMock()
        lead_pane.session = mock_session

        orch._panes_by_project["proj"] = {"lead": lead_pane}

        orch.close("lead", project="proj")

        assert "lead" in orch._panes_by_project["proj"]

    def test_teammate_close_still_terminates(
        self,
        qapp: QCoreApplication,
        minimal_project_json: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: backend.close() must still terminate as before."""
        monkeypatch.setattr(
            Orchestrator,
            "_resolve_project",
            staticmethod(lambda project: project or "proj"),
        )
        orch = Orchestrator()
        orch._idle_watchdog.stop()

        # Suppress paneClosed / statusChanged Qt signals
        monkeypatch.setattr(orch, "paneClosed", MagicMock())
        monkeypatch.setattr(orch, "statusChanged", MagicMock())

        mock_session = MagicMock()
        backend_pane = MagicMock()
        backend_pane.session = mock_session

        orch._panes_by_project["proj"] = {"backend": backend_pane}

        ok, _ = orch.close("backend", project="proj")

        assert ok is True
        mock_session.terminate.assert_called_once()
        backend_pane.set_state.assert_called_once_with("empty", note=None)


# ─────────────────────────────────────────────────────────────
# Layer 2 — CLI _enforce_role_gate
# ─────────────────────────────────────────────────────────────


class TestCliGateLeadDone:
    """_enforce_role_gate("done") from Lead must return an error string."""

    def test_blocks_lead_from_done(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        result = cli._enforce_role_gate("done")
        assert result is not None
        assert "lead cannot" in result
        assert "done" in result

    def test_allows_teammate_done(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "backend")
        assert cli._enforce_role_gate("done") is None

    def test_allows_teammate_done_qa(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "qa")
        assert cli._enforce_role_gate("done") is None

    def test_allows_manual_done_no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        assert cli._enforce_role_gate("done") is None

    def test_lead_done_exits_one(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Full CLI path: takkub done from Lead pane must exit 1 before sending."""
        sent: list = []
        monkeypatch.setattr(cli, "_request", lambda p: sent.append(p) or {"ok": True, "msg": "x"})
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        rc = cli.main(["done", "some note"])
        assert rc == 1
        assert sent == []  # never reached the socket
        err = capsys.readouterr().err
        assert "lead cannot" in err


# ─────────────────────────────────────────────────────────────
# Layer 1 — orchestrator.close() with force=True
# ─────────────────────────────────────────────────────────────


class TestOrchestratorCloseForce:
    """close("lead", force=True) must terminate the Lead session."""

    def _make_orch(
        self,
        qapp: QCoreApplication,
        minimal_project_json: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> Orchestrator:
        monkeypatch.setattr(
            Orchestrator,
            "_resolve_project",
            staticmethod(lambda project: project or "proj"),
        )
        orch = Orchestrator()
        orch._idle_watchdog.stop()
        monkeypatch.setattr(orch, "paneClosed", MagicMock())
        monkeypatch.setattr(orch, "statusChanged", MagicMock())
        return orch

    def test_close_with_force_terminates_lead(
        self,
        qapp: QCoreApplication,
        minimal_project_json: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch = self._make_orch(qapp, minimal_project_json, monkeypatch)

        mock_session = MagicMock()
        lead_pane = MagicMock()
        lead_pane.session = mock_session
        orch._panes_by_project["proj"] = {"lead": lead_pane}

        ok, msg = orch.close("lead", project="proj", force=True)

        assert ok is True
        assert "protected" not in msg
        mock_session.terminate.assert_called_once()
        lead_pane.set_state.assert_called_once_with("empty", note=None)

    def test_close_force_does_not_emit_paneClosed_for_lead(
        self,
        qapp: QCoreApplication,
        minimal_project_json: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """paneClosed must not fire for Lead — tab close path handles UI removal."""
        orch = self._make_orch(qapp, minimal_project_json, monkeypatch)

        mock_session = MagicMock()
        lead_pane = MagicMock()
        lead_pane.session = mock_session
        orch._panes_by_project["proj"] = {"lead": lead_pane}

        orch.close("lead", project="proj", force=True)

        orch.paneClosed.emit.assert_not_called()

    def test_tab_close_path_uses_force_to_kill_lead(
        self,
        qapp: QCoreApplication,
        minimal_project_json: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression guard: simulates _on_tab_close_requested calling close with force=True."""
        orch = self._make_orch(qapp, minimal_project_json, monkeypatch)

        mock_session = MagicMock()
        lead_pane = MagicMock()
        lead_pane.session = mock_session
        orch._panes_by_project["proj"] = {"lead": lead_pane}

        # This is the exact call _on_tab_close_requested makes
        ok, _ = orch.close("lead", project="proj", force=True)

        assert ok is True
        mock_session.terminate.assert_called_once()


# ─────────────────────────────────────────────────────────────
# Layer 1 — orchestrator.unregister_pane() Lead protection
# ─────────────────────────────────────────────────────────────


class TestOrchestratorUnregisterPane:
    """unregister_pane() must refuse Lead by default; accept Lead with force=True."""

    def _make_orch(
        self,
        qapp: QCoreApplication,
        minimal_project_json: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> Orchestrator:
        monkeypatch.setattr(
            Orchestrator,
            "_resolve_project",
            staticmethod(lambda project: project or "proj"),
        )
        orch = Orchestrator()
        orch._idle_watchdog.stop()
        monkeypatch.setattr(orch, "statusChanged", MagicMock())
        return orch

    def test_unregister_pane_refuses_lead_by_default(
        self,
        qapp: QCoreApplication,
        minimal_project_json: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch = self._make_orch(qapp, minimal_project_json, monkeypatch)

        mock_session = MagicMock()
        lead_pane = MagicMock()
        lead_pane.session = mock_session
        orch._panes_by_project["proj"] = {"lead": lead_pane}

        orch.unregister_pane("lead", project="proj")

        mock_session.terminate.assert_not_called()
        assert "lead" in orch._panes_by_project["proj"]

    def test_unregister_pane_with_force_kills_lead(
        self,
        qapp: QCoreApplication,
        minimal_project_json: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch = self._make_orch(qapp, minimal_project_json, monkeypatch)

        mock_session = MagicMock()
        lead_pane = MagicMock()
        lead_pane.session = mock_session
        orch._panes_by_project["proj"] = {"lead": lead_pane}

        orch.unregister_pane("lead", project="proj", force=True)

        mock_session.terminate.assert_called_once()
        assert "lead" not in orch._panes_by_project["proj"]


class TestCliGateLeadOnlyCommandsRegression:
    """Existing LEAD_ONLY_COMMANDS gate must still work after the refactor."""

    def test_teammate_cannot_assign(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        sent: list = []
        monkeypatch.setattr(cli, "_request", lambda p: sent.append(p) or {"ok": True, "msg": "x"})
        monkeypatch.setenv("TAKKUB_ROLE", "devops")
        rc = cli.main(["assign", "--role", "devops", "--cwd", "/x", "self-assign attempt"])
        assert rc == 1
        assert sent == []
        err = capsys.readouterr().err
        assert "only lead" in err and "devops" in err

    def test_lead_can_assign(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent: list = []
        monkeypatch.setattr(cli, "_request", lambda p: sent.append(p) or {"ok": True, "msg": "x"})
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        rc = cli.main(["assign", "--role", "backend", "do work"])
        assert rc == 0
        assert sent[-1]["cmd"] == "assign"


# ─────────────────────────────────────────────────────────────
# B2 — close_all_teammates skips Lead even when Lead is in registry
# ─────────────────────────────────────────────────────────────


class TestCloseAllTeammatesSkipsLead:
    """close_all_teammates() must never touch the Lead pane."""

    def test_close_all_teammates_skips_lead_even_with_lead_in_registry(
        self,
        qapp: QCoreApplication,
        minimal_project_json: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            Orchestrator,
            "_resolve_project",
            staticmethod(lambda project: project or "proj"),
        )
        orch = Orchestrator()
        orch._idle_watchdog.stop()
        monkeypatch.setattr(orch, "paneClosed", MagicMock())
        monkeypatch.setattr(orch, "statusChanged", MagicMock())

        lead_session = MagicMock()
        lead_pane = MagicMock()
        lead_pane.session = lead_session

        frontend_session = MagicMock()
        frontend_pane = MagicMock()
        frontend_pane.session = frontend_session

        backend_session = MagicMock()
        backend_pane = MagicMock()
        backend_pane.session = backend_session

        orch._panes_by_project["proj"] = {
            "lead": lead_pane,
            "frontend": frontend_pane,
            "backend": backend_pane,
        }

        orch.close_all_teammates(project="proj")

        # Lead must be untouched
        lead_session.terminate.assert_not_called()
        assert "lead" in orch._panes_by_project["proj"]

        # Both teammates must be terminated
        frontend_session.terminate.assert_called_once()
        backend_session.terminate.assert_called_once()


# ─────────────────────────────────────────────────────────────
# W3 — restart-lead path: force=True kills session, slot stays for respawn
# ─────────────────────────────────────────────────────────────


class TestRestartLeadPath:
    """_restart_lead_for_active_project calls close(force=True) then spawn.
    Verify the close step terminates the session, and the slot is left in a
    state that allows spawn() to respawn (session is None / not alive)."""

    def test_restart_lead_path_force_kills_session(
        self,
        qapp: QCoreApplication,
        minimal_project_json: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            Orchestrator,
            "_resolve_project",
            staticmethod(lambda project: project or "proj"),
        )
        orch = Orchestrator()
        orch._idle_watchdog.stop()
        monkeypatch.setattr(orch, "paneClosed", MagicMock())
        monkeypatch.setattr(orch, "statusChanged", MagicMock())

        lead_session = MagicMock()
        lead_session.is_alive = True
        lead_pane = MagicMock()
        lead_pane.session = lead_session

        orch._panes_by_project["proj"] = {"lead": lead_pane}

        # Simulate the call sequence _restart_lead_for_active_project performs
        ok, _msg = orch.close("lead", project="proj", force=True, reason="restart_lead")

        assert ok is True
        lead_session.terminate.assert_called_once()

        # After force close, pane slot still exists in registry (not popped),
        # so spawn() can find the pane and attach a new session.
        assert "lead" in orch._panes_by_project["proj"]


# ─────────────────────────────────────────────────────────────
# W4 — close() emits distinct reason fields for tab_close vs restart_lead
# ─────────────────────────────────────────────────────────────


class TestCloseEmitsDistinctReasons:
    """close() must log a 'reason' field that distinguishes tab_close from restart_lead."""

    def _make_orch(
        self,
        qapp: QCoreApplication,
        minimal_project_json: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> Orchestrator:
        monkeypatch.setattr(
            Orchestrator,
            "_resolve_project",
            staticmethod(lambda project: project or "proj"),
        )
        orch = Orchestrator()
        orch._idle_watchdog.stop()
        monkeypatch.setattr(orch, "paneClosed", MagicMock())
        monkeypatch.setattr(orch, "statusChanged", MagicMock())
        return orch

    def test_close_emits_distinct_reasons(
        self,
        qapp: QCoreApplication,
        minimal_project_json: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch = self._make_orch(qapp, minimal_project_json, monkeypatch)

        logged: list[dict] = []

        def _capture_log(event: str, **details: object) -> None:
            logged.append({"event": event, **details})

        monkeypatch.setattr(orch_mod, "_log_event", _capture_log)

        lead_session_1 = MagicMock()
        lead_pane_1 = MagicMock()
        lead_pane_1.session = lead_session_1
        orch._panes_by_project["proj"] = {"lead": lead_pane_1}

        # tab_close path
        orch.close("lead", project="proj", force=True, reason="tab_close")

        # reset for second call
        lead_session_2 = MagicMock()
        lead_pane_2 = MagicMock()
        lead_pane_2.session = lead_session_2
        orch._panes_by_project["proj"] = {"lead": lead_pane_2}

        # restart_lead path
        orch.close("lead", project="proj", force=True, reason="restart_lead")

        reasons = [e.get("reason") for e in logged if e.get("event") == "close"]
        assert "tab_close" in reasons
        assert "restart_lead" in reasons
