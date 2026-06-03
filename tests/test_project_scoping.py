"""Tests for Phase 1a multi-tab project scoping fixes.

Covers:
  Fix 1 – _allowed_project_roots / _cwd_within_project (+ spawn() guard)
  Fix 2 – _auto_trust uses correct project namespace
  Fix 3 – _render_lead_context(project) uses target project, not active
  Fix 4 – _recent_exits keyed by f"{project}::{role}"

All tests are written BEFORE implementation (TDD) and are expected to fail
until the orchestrator changes are applied.
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import config
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
def two_project_json(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """projects.json with two independent projects; active = proj_b."""
    pj = tmp_path / "projects.json"
    pj.write_text(
        json.dumps(
            {
                "active": "proj_b",
                "projects": {
                    "proj_a": {
                        "paths": {
                            "api": str(tmp_path / "proj_a" / "api"),
                            "web": str(tmp_path / "proj_a" / "web"),
                        }
                    },
                    "proj_b": {
                        "paths": {
                            "api": str(tmp_path / "proj_b" / "api"),
                            "web": str(tmp_path / "proj_b" / "web"),
                        }
                    },
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
# Fix 1 – _allowed_project_roots / _cwd_within_project
# ─────────────────────────────────────────────────────────────


class TestAllowedProjectRoots:
    def test_returns_resolved_paths_for_known_project(
        self, two_project_json: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        roots = orch_mod._allowed_project_roots("proj_a")
        expected_api = (tmp_path / "proj_a" / "api").resolve()
        expected_web = (tmp_path / "proj_a" / "web").resolve()
        assert expected_api in roots
        assert expected_web in roots

    def test_returns_empty_list_for_unknown_project(self, two_project_json: pathlib.Path) -> None:
        roots = orch_mod._allowed_project_roots("nonexistent")
        assert roots == []

    def test_returns_empty_list_for_default_project(self, two_project_json: pathlib.Path) -> None:
        roots = orch_mod._allowed_project_roots("default")
        assert roots == []


class TestCwdWithinProject:
    def test_accepts_exact_project_path(
        self, two_project_json: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        cwd = str(tmp_path / "proj_a" / "api")
        assert orch_mod._cwd_within_project(cwd, "proj_a", "backend") is True

    def test_accepts_subdir_of_project_path(
        self, two_project_json: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        cwd = str(tmp_path / "proj_a" / "api" / "src" / "handlers")
        assert orch_mod._cwd_within_project(cwd, "proj_a", "backend") is True

    def test_rejects_sibling_project_path(
        self, two_project_json: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        cwd = str(tmp_path / "proj_b" / "api")
        assert orch_mod._cwd_within_project(cwd, "proj_a", "backend") is False

    def test_rejects_completely_unrelated_path(
        self, two_project_json: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        cwd = str(tmp_path / "somewhere_else" / "foo")
        assert orch_mod._cwd_within_project(cwd, "proj_a", "backend") is False

    def test_accepts_cockpit_repo_root_for_lead(
        self, two_project_json: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        cockpit = tmp_path / "cockpit"
        assert orch_mod._cwd_within_project(str(cockpit), "proj_a", "lead") is True

    def test_accepts_cockpit_subdir_for_lead(
        self, two_project_json: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        cockpit_sub = tmp_path / "cockpit" / "runtime" / "sessions"
        assert orch_mod._cwd_within_project(str(cockpit_sub), "proj_a", "lead") is True

    def test_rejects_cockpit_repo_root_for_teammate(
        self, two_project_json: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        cockpit = tmp_path / "cockpit"
        assert orch_mod._cwd_within_project(str(cockpit), "proj_a", "backend") is False

    def test_rejects_path_for_default_namespace(
        self, two_project_json: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        # "default" project has no paths — only cockpit exception can pass
        unrelated = str(tmp_path / "proj_a" / "api")
        # cwd is NOT under the cockpit dir, so it must be rejected for "default"
        assert orch_mod._cwd_within_project(unrelated, "default", "backend") is False


# ─────────────────────────────────────────────────────────────
# Fix 1 – spawn() refuses explicit cwd outside project
# ─────────────────────────────────────────────────────────────


class _FakePane:
    """Minimal AgentPane stub: no active session, ready to spawn."""

    def __init__(self) -> None:
        self.session = None

    @property
    def state(self) -> str:
        return "empty"


class _SpawnOrch:
    """Fake Orchestrator carrying only the state spawn()'s cwd guard needs."""

    def __init__(self, project_ns: str) -> None:
        self._ns = project_ns
        self._panes_by_project: dict[str, dict[str, _FakePane]] = {}

    def _resolve_project(self, project: str | None = None) -> str:
        return project or self._ns

    def _project_panes(self, project: str | None = None) -> dict:
        ns = self._resolve_project(project)
        return self._panes_by_project.setdefault(ns, {})

    @property
    def panes(self) -> dict:
        return self._project_panes()


def test_spawn_refuses_cwd_outside_project_boundaries(
    two_project_json: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """spawn() must return (False, error) when explicit cwd falls outside project."""
    outside = str(tmp_path / "proj_b" / "api")
    assert not orch_mod._cwd_within_project(outside, "proj_a", "backend"), (
        "pre-condition: outside path must fail the guard"
    )


def test_spawn_allows_cwd_inside_project_boundary(
    two_project_json: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    inside = str(tmp_path / "proj_a" / "api")
    assert orch_mod._cwd_within_project(inside, "proj_a", "backend")


# ─────────────────────────────────────────────────────────────
# Fix 2 – _auto_trust uses correct project namespace
# ─────────────────────────────────────────────────────────────


class TestAutoTrustProjectNamespace:
    """_auto_trust(role, project='proj_a') must look up panes from proj_a,
    not from the active project (proj_b)."""

    def test_auto_trust_targets_specified_project_not_active(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            Orchestrator,
            "_resolve_project",
            staticmethod(lambda project: project or "proj_b"),
        )
        orch = Orchestrator()
        orch._idle_watchdog.stop()

        # Plant a pane only in proj_a; proj_b (active) has no backend pane.
        fake_pane = MagicMock()
        fake_pane.session = None  # no active session
        orch._panes_by_project["proj_a"] = {"backend": fake_pane}
        orch._panes_by_project["proj_b"] = {}

        timer_calls: list[tuple] = []
        monkeypatch.setattr(
            orch_mod.QTimer,
            "singleShot",
            staticmethod(lambda ms, cb: timer_calls.append((ms, cb))),
        )

        # Calling with project="proj_a" → pane found → timer started
        orch._auto_trust("backend", project="proj_a")
        assert timer_calls, "_auto_trust with correct project should start the trust-modal timer"

    def test_auto_trust_returns_early_when_pane_not_in_project(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            Orchestrator,
            "_resolve_project",
            staticmethod(lambda project: project or "proj_b"),
        )
        orch = Orchestrator()
        orch._idle_watchdog.stop()

        # proj_b (active) has no backend pane at all
        orch._panes_by_project["proj_b"] = {}

        timer_calls: list[tuple] = []
        monkeypatch.setattr(
            orch_mod.QTimer,
            "singleShot",
            staticmethod(lambda ms, cb: timer_calls.append((ms, cb))),
        )

        # Calling with no project → falls to active (proj_b) → pane None → early return
        orch._auto_trust("backend")
        assert not timer_calls, "_auto_trust with no matching pane should NOT start any timer"


# ─────────────────────────────────────────────────────────────
# Fix 3 – _render_lead_context uses target project, not active
# ─────────────────────────────────────────────────────────────


class TestRenderLeadContext:
    def _setup_cockpit(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cockpit = tmp_path / "cockpit"
        cockpit.mkdir(parents=True, exist_ok=True)
        (cockpit / "CLAUDE.md").write_text("# Lead Guide\n", encoding="utf-8")
        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(orch_mod, "REPO_ROOT", cockpit)
        monkeypatch.setattr(config, "REPO_ROOT", cockpit)
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", runtime)
        monkeypatch.setattr(config, "RUNTIME_DIR", runtime)

    def test_uses_target_project_paths_not_active(
        self,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Passing project='proj_a' → rendered file contains proj_a paths,
        NOT proj_b (which is the active project)."""
        self._setup_cockpit(tmp_path, monkeypatch)

        result = orch_mod._render_lead_context(project="proj_a")
        assert result is not None
        content = pathlib.Path(result).read_text(encoding="utf-8")

        proj_a_api = str(tmp_path / "proj_a" / "api")
        proj_b_api = str(tmp_path / "proj_b" / "api")
        assert proj_a_api in content, "proj_a path must appear in rendered context"
        assert proj_b_api not in content, "active proj_b path must NOT appear when target is proj_a"

    def test_falls_back_to_active_when_no_project_given(
        self,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No project arg → falls back to active project (proj_b)."""
        self._setup_cockpit(tmp_path, monkeypatch)

        result = orch_mod._render_lead_context()
        assert result is not None
        content = pathlib.Path(result).read_text(encoding="utf-8")

        proj_b_api = str(tmp_path / "proj_b" / "api")
        assert proj_b_api in content, "active proj_b path must appear when no project arg passed"


# ─────────────────────────────────────────────────────────────
# Fix 4 – _recent_exits keyed by "{project}::{role}"
# ─────────────────────────────────────────────────────────────


def test_exit_key_format() -> None:
    assert orch_mod._exit_key("proj_a", "backend") == "proj_a::backend"
    assert orch_mod._exit_key("line-websupport", "frontend") == "line-websupport::frontend"


class _ExitFake:
    """Fake for driving _on_session_exit / restore_teammates without Qt."""

    def __init__(self) -> None:
        self._recent_exits: dict[str, dict] = {}
        self._panes_by_project: dict = {}
        self._idle_state: dict = {}
        self._pane_state: dict = {}
        self._pending_done_notices: dict = {}

    def _ps(self, key: str):
        from agent_takkub.orchestrator import PaneState

        try:
            return self._pane_state[key]
        except KeyError:
            ps = PaneState()
            self._pane_state[key] = ps
            return ps

    def spawn(self, role: str, cwd: str | None = None, project: str | None = None):
        return True, "ok"

    def _save_pending_done_notices(self, project: str) -> None:
        pass

    def _send_when_ready(self, role: str, task: str, project: str | None = None) -> None:
        pass


class TestRecentExitsProjectScoping:
    def test_exit_recorded_with_project_scoped_key(self) -> None:
        fake = _ExitFake()
        Orchestrator._on_session_exit(fake, "backend", "/proj_a/api", "proj_a")  # type: ignore[arg-type]

        assert "proj_a::backend" in fake._recent_exits
        assert fake._recent_exits["proj_a::backend"]["cwd"] == "/proj_a/api"

    def test_bare_role_key_absent_after_exit(self) -> None:
        """After the fix, bare role key ("backend") must not appear."""
        fake = _ExitFake()
        Orchestrator._on_session_exit(fake, "backend", "/api", "proj_a")  # type: ignore[arg-type]

        assert "backend" not in fake._recent_exits, (
            "bare role key must not exist — _recent_exits must use project::role"
        )

    def test_exit_in_proj_a_does_not_overwrite_proj_b(self) -> None:
        """Exit in proj_a must not touch proj_b's exit record."""
        fake = _ExitFake()
        # Pre-seed proj_b's backend exit
        fake._recent_exits["proj_b::backend"] = {"cwd": "/proj_b/api", "ts": 9999.0}

        Orchestrator._on_session_exit(fake, "backend", "/proj_a/api", "proj_a")  # type: ignore[arg-type]

        # proj_a record written
        assert "proj_a::backend" in fake._recent_exits
        # proj_b record untouched
        assert fake._recent_exits["proj_b::backend"]["cwd"] == "/proj_b/api"
        assert fake._recent_exits["proj_b::backend"]["ts"] == 9999.0

    def test_restore_teammates_stamps_project_scoped_exit_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """restore_teammates must stamp _recent_exits with project::role key."""
        import datetime as dt

        session_file = tmp_path / "last-session.json"
        now = dt.datetime.now().isoformat(timespec="seconds")
        snap = {
            "saved_at": now,
            "projects": {
                "proj_a": [{"role": "backend", "cwd": "/proj_a/api", "state": "working"}],
                "proj_b": [{"role": "backend", "cwd": "/proj_b/api", "state": "active"}],
            },
        }
        session_file.write_text(json.dumps(snap), encoding="utf-8")
        monkeypatch.setattr(orch_mod, "_LAST_SESSION_FILE", session_file)

        class _Fake(_ExitFake):
            pass

        fake = _Fake()
        Orchestrator.restore_teammates(fake)  # type: ignore[arg-type]

        assert "proj_a::backend" in fake._recent_exits, "restore must stamp project-scoped key"
        assert "proj_b::backend" in fake._recent_exits
        # bare key must not exist
        assert "backend" not in fake._recent_exits
