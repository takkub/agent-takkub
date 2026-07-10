"""Tests for option-B session UUID fix (resume-bleed prevention).

Each claude pane now gets an isolated session:
  * fresh spawn → --session-id <lowercase-v4-uuid>
  * respawn within RESUME_WINDOW_SEC (same cwd) → --resume <prior-uuid>
  * respawn outside window OR different cwd → new --session-id
  * close() / done() clears UUID so next spawn starts fresh
  * --continue never appears in any argv (regression guard)
"""

from __future__ import annotations

import pathlib
import re
import sys
import time
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import config
from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import RESUME_WINDOW_SEC, Orchestrator, PaneState, _exit_key

# project="default" bypasses the CWD-within-project validation in spawn()
_PROJECT = "default"

# Lowercase UUID v4 pattern
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


# ─────────────────────────────────────────────────────────────
# Module-scoped Qt application (shared, cannot re-create)
# ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


# ─────────────────────────────────────────────────────────────
# Per-test filesystem + Orchestrator fixtures
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_env(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Minimal filesystem setup required by spawn()."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    cockpit = tmp_path / "cockpit"
    cockpit.mkdir(parents=True, exist_ok=True)
    (cockpit / "CLAUDE.md").write_text("# Lead\n", encoding="utf-8")
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(config, "REPO_ROOT", cockpit)
    monkeypatch.setattr(orch_mod, "REPO_ROOT", cockpit)
    monkeypatch.setattr(orch_mod, "find_claude_executable", lambda: "claude")
    return tmp_path


@pytest.fixture
def orch(qapp: QCoreApplication, tmp_env: pathlib.Path) -> Orchestrator:
    o = Orchestrator()
    o._idle_watchdog.stop()
    return o


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────


def _spawn_capture(
    orch: Orchestrator,
    role_name: str,
    cwd: str = "/proj",
    project: str = _PROJECT,
) -> list[str]:
    """Run orch.spawn() with a fake PtySession and return the captured argv."""
    fake_pane = MagicMock()
    fake_pane.session = None
    fake_pane.state = "empty"
    fake_pane.attach_session = MagicMock()
    fake_pane._transcript_path = None
    orch._panes_by_project.setdefault(project, {})[role_name] = fake_pane

    captured: list[list[str]] = []
    fake_session = MagicMock()
    fake_session.processExited = MagicMock()
    fake_session.processExited.connect = MagicMock()

    with patch.object(orch_mod.PtySession, "__new__", return_value=fake_session):
        with patch.object(
            fake_session,
            "spawn",
            side_effect=lambda argv, cwd, env, **kwargs: captured.append(list(argv)),
        ):
            orch.spawn(role_name, cwd=cwd, project=project)

    return captured[0] if captured else []


def _simulate_exit(
    orch: Orchestrator,
    role_name: str,
    cwd: str,
    project: str = _PROJECT,
) -> None:
    """Stamp _recent_exits as _on_session_exit would, without auto-respawn."""
    orch._recent_exits[_exit_key(project, role_name)] = {"cwd": cwd, "ts": time.time()}
    pane = orch._panes_by_project.get(project, {}).get(role_name)
    if pane is not None:
        pane.state = "empty"


# ─────────────────────────────────────────────────────────────
# 1. First spawn: --session-id <uuid>, no --resume
# ─────────────────────────────────────────────────────────────


class TestFirstSpawnUsesSessionId:
    def test_session_id_in_argv_not_resume(self, orch: Orchestrator) -> None:
        argv = _spawn_capture(orch, "backend")
        assert "--session-id" in argv
        assert "--resume" not in argv
        assert "--continue" not in argv

    def test_uuid_is_lowercase_v4(self, orch: Orchestrator) -> None:
        argv = _spawn_capture(orch, "backend")
        idx = argv.index("--session-id")
        uuid_val = argv[idx + 1]
        assert _UUID_RE.match(uuid_val), f"Not a lowercase UUIDv4: {uuid_val!r}"

    def test_uuid_stored_in_session_uuids(self, orch: Orchestrator) -> None:
        argv = _spawn_capture(orch, "backend")
        idx = argv.index("--session-id")
        uuid_val = argv[idx + 1]
        key = _exit_key(_PROJECT, "backend")
        assert (orch._pane_state.get(key) or PaneState()).session_uuid == uuid_val


# ─────────────────────────────────────────────────────────────
# 2. Respawn within window: --resume <uuid>, no --session-id
# ─────────────────────────────────────────────────────────────


class TestRespawnWithinWindowUsesResume:
    def test_resume_with_prior_uuid(self, orch: Orchestrator) -> None:
        cwd = "/proj"
        argv1 = _spawn_capture(orch, "frontend", cwd=cwd)
        idx1 = argv1.index("--session-id")
        uuid1 = argv1[idx1 + 1]

        _simulate_exit(orch, "frontend", cwd=cwd)

        argv2 = _spawn_capture(orch, "frontend", cwd=cwd)
        assert "--resume" in argv2
        assert argv2[argv2.index("--resume") + 1] == uuid1
        assert "--session-id" not in argv2
        assert "--continue" not in argv2

    def test_uuid_unchanged_after_resume(self, orch: Orchestrator) -> None:
        cwd = "/proj"
        argv1 = _spawn_capture(orch, "qa", cwd=cwd)
        uuid1 = argv1[argv1.index("--session-id") + 1]

        _simulate_exit(orch, "qa", cwd=cwd)
        _spawn_capture(orch, "qa", cwd=cwd)

        key = _exit_key(_PROJECT, "qa")
        assert orch._pane_state[key].session_uuid == uuid1


# ─────────────────────────────────────────────────────────────
# 3. Respawn after window expired: fresh --session-id
# ─────────────────────────────────────────────────────────────


class TestRespawnAfterWindowUsesNewSessionId:
    def test_new_uuid_after_window_expiry(self, orch: Orchestrator) -> None:
        cwd = "/proj"
        argv1 = _spawn_capture(orch, "devops", cwd=cwd)
        uuid1 = argv1[argv1.index("--session-id") + 1]

        # Simulate exit with an expired timestamp
        key = _exit_key(_PROJECT, "devops")
        orch._recent_exits[key] = {"cwd": cwd, "ts": time.time() - (RESUME_WINDOW_SEC + 60)}
        pane = orch._panes_by_project.get(_PROJECT, {}).get("devops")
        if pane is not None:
            pane.state = "empty"

        argv2 = _spawn_capture(orch, "devops", cwd=cwd)
        assert "--session-id" in argv2
        assert "--resume" not in argv2
        assert "--continue" not in argv2
        uuid2 = argv2[argv2.index("--session-id") + 1]
        assert uuid2 != uuid1, "Expected a fresh UUID after window expiry"


# ─────────────────────────────────────────────────────────────
# 3b. L5: superficial cwd spelling differences must still resume
# ─────────────────────────────────────────────────────────────


class TestRespawnCwdNormalization:
    """L5 (cross-platform audit 2026-07-10): the 5-min auto-resume cwd check
    must survive superficial spelling differences of the *same* directory
    (trailing slash, mixed separators, and on Windows, case) — a raw string
    compare would otherwise treat a respawn into the same real directory as
    a different cwd and start a fresh session instead of resuming."""

    def test_trailing_slash_still_resumes(self, orch: Orchestrator) -> None:
        argv1 = _spawn_capture(orch, "backend", cwd="/proj")
        uuid1 = argv1[argv1.index("--session-id") + 1]

        _simulate_exit(orch, "backend", cwd="/proj")

        argv2 = _spawn_capture(orch, "backend", cwd="/proj/")
        assert "--resume" in argv2
        assert argv2[argv2.index("--resume") + 1] == uuid1
        assert "--session-id" not in argv2

    @pytest.mark.skipif(
        sys.platform != "win32", reason="case-insensitive filesystem is Windows-specific"
    )
    def test_case_difference_still_resumes_on_windows(self, orch: Orchestrator) -> None:
        argv1 = _spawn_capture(orch, "frontend", cwd="C:/Proj")
        uuid1 = argv1[argv1.index("--session-id") + 1]

        _simulate_exit(orch, "frontend", cwd="C:/Proj")

        argv2 = _spawn_capture(orch, "frontend", cwd="C:/PROJ")
        assert "--resume" in argv2
        assert argv2[argv2.index("--resume") + 1] == uuid1
        assert "--session-id" not in argv2


# ─────────────────────────────────────────────────────────────
# 4. Respawn with different cwd: fresh --session-id
# ─────────────────────────────────────────────────────────────


class TestRespawnDifferentCwdUsesNewSessionId:
    def test_new_uuid_when_cwd_changes(self, orch: Orchestrator) -> None:
        argv1 = _spawn_capture(orch, "mobile", cwd="/proj/a")
        uuid1 = argv1[argv1.index("--session-id") + 1]

        _simulate_exit(orch, "mobile", cwd="/proj/a")

        argv2 = _spawn_capture(orch, "mobile", cwd="/proj/b")
        assert "--session-id" in argv2
        assert "--resume" not in argv2
        uuid2 = argv2[argv2.index("--session-id") + 1]
        assert uuid2 != uuid1

    def test_cwd_updated_in_session_uuids(self, orch: Orchestrator) -> None:
        _spawn_capture(orch, "reviewer", cwd="/proj/a")
        _simulate_exit(orch, "reviewer", cwd="/proj/a")
        _spawn_capture(orch, "reviewer", cwd="/proj/b")
        key = _exit_key(_PROJECT, "reviewer")
        assert orch._pane_state[key].session_uuid_cwd == "/proj/b"


# ─────────────────────────────────────────────────────────────
# 5. Manual close() clears UUID
# ─────────────────────────────────────────────────────────────


class TestManualCloseClears:
    def test_close_pops_uuid(self, orch: Orchestrator) -> None:
        _spawn_capture(orch, "designer")
        key = _exit_key(_PROJECT, "designer")
        assert (
            orch._pane_state.get(key) is not None and orch._pane_state[key].session_uuid is not None
        )
        # close() clears all per-pane state even when session is None (pane not alive)
        orch.close("designer", project=_PROJECT)
        assert orch._pane_state.get(key) is None

    def test_post_close_spawn_gets_fresh_session_id(self, orch: Orchestrator) -> None:
        _spawn_capture(orch, "designer")
        uuid1 = orch._pane_state[_exit_key(_PROJECT, "designer")].session_uuid

        orch.close("designer", project=_PROJECT)

        argv_new = _spawn_capture(orch, "designer")
        assert "--session-id" in argv_new
        assert "--resume" not in argv_new
        uuid2 = argv_new[argv_new.index("--session-id") + 1]
        assert uuid2 != uuid1


# ─────────────────────────────────────────────────────────────
# 6. done() clears UUID
# ─────────────────────────────────────────────────────────────


class TestDoneClears:
    def test_done_pops_uuid(self, orch: Orchestrator) -> None:
        _spawn_capture(orch, "backend")
        key = _exit_key(_PROJECT, "backend")
        assert (
            orch._pane_state.get(key) is not None and orch._pane_state[key].session_uuid is not None
        )
        # done() clears all per-pane state regardless of session state
        orch.done("backend", note="done", project=_PROJECT)
        assert orch._pane_state.get(key) is None

    def test_post_done_spawn_gets_fresh_session_id(self, orch: Orchestrator) -> None:
        _spawn_capture(orch, "qa")
        uuid1 = orch._pane_state[_exit_key(_PROJECT, "qa")].session_uuid

        orch.done("qa", note="done", project=_PROJECT)

        argv_new = _spawn_capture(orch, "qa")
        uuid2 = argv_new[argv_new.index("--session-id") + 1]
        assert uuid2 != uuid1


# ─────────────────────────────────────────────────────────────
# 7. Two roles in same cwd: isolated UUIDs, different keys
# ─────────────────────────────────────────────────────────────


class TestTwoRolesIsolatedInSameCwd:
    def test_different_uuids_and_keys(self, orch: Orchestrator) -> None:
        cwd = "/shared"
        _spawn_capture(orch, "backend", cwd=cwd)
        _spawn_capture(orch, "frontend", cwd=cwd)

        key_back = _exit_key(_PROJECT, "backend")
        key_front = _exit_key(_PROJECT, "frontend")
        assert key_back != key_front
        assert (
            orch._pane_state.get(key_back) is not None
            and orch._pane_state[key_back].session_uuid is not None
        )
        assert (
            orch._pane_state.get(key_front) is not None
            and orch._pane_state[key_front].session_uuid is not None
        )
        uuid_back = orch._pane_state[key_back].session_uuid
        uuid_front = orch._pane_state[key_front].session_uuid
        assert uuid_back != uuid_front


# ─────────────────────────────────────────────────────────────
# 8. --continue must never appear in any argv (regression guard)
# ─────────────────────────────────────────────────────────────


class TestContinueFlagNeverAppears:
    def test_fresh_spawn(self, orch: Orchestrator) -> None:
        assert "--continue" not in _spawn_capture(orch, "backend", cwd="/proj")

    def test_respawn_within_window(self, orch: Orchestrator) -> None:
        cwd = "/proj"
        _spawn_capture(orch, "mobile", cwd=cwd)
        _simulate_exit(orch, "mobile", cwd=cwd)
        assert "--continue" not in _spawn_capture(orch, "mobile", cwd=cwd)

    def test_respawn_after_expired_window(self, orch: Orchestrator) -> None:
        cwd = "/proj"
        _spawn_capture(orch, "devops", cwd=cwd)
        key = _exit_key(_PROJECT, "devops")
        orch._recent_exits[key] = {"cwd": cwd, "ts": time.time() - (RESUME_WINDOW_SEC + 60)}
        pane = orch._panes_by_project.get(_PROJECT, {}).get("devops")
        if pane is not None:
            pane.state = "empty"
        assert "--continue" not in _spawn_capture(orch, "devops", cwd=cwd)

    def test_respawn_different_cwd(self, orch: Orchestrator) -> None:
        _spawn_capture(orch, "reviewer", cwd="/proj/a")
        _simulate_exit(orch, "reviewer", cwd="/proj/a")
        assert "--continue" not in _spawn_capture(orch, "reviewer", cwd="/proj/b")
