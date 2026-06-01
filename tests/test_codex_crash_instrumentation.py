"""Codex early-crash instrumentation tests.

Coverage:
  1. _on_codex_exit with early exit → writes dump file + logs codex_early_crash event
  2. _on_codex_exit with late exit (> threshold) → no dump written
  3. _on_codex_exit always delegates to _on_session_exit
  4. _write_codex_crash_dump with display_lines failure → still writes dump (no exception)
  5. _codex_spawn_times populated on codex spawn, cleared on _on_codex_exit
  6. dump filename format: <ts>-<project>-<role>.log in runtime/codex_crash_dumps/
"""

from __future__ import annotations

import pathlib
import time
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import CODEX_EARLY_CRASH_WINDOW_SEC, Orchestrator, PaneState

TEST_PROJECT = "default"
FAKE_CWD = "/tmp/takkub-codex-crash-test"

_COMMON_PATCHES: list[tuple[str, object]] = [
    ("agent_takkub.orchestrator.find_claude_executable", "fake-claude"),
    ("agent_takkub.orchestrator._build_transcript_path", pathlib.Path("/tmp/t.log")),
    ("agent_takkub.orchestrator._default_plugin_dirs", []),
    ("agent_takkub.orchestrator.render_lead_settings", pathlib.Path("/tmp/lead.json")),
    ("agent_takkub.orchestrator._render_lead_context", "/tmp/lead-ctx.md"),
    ("agent_takkub.orchestrator.agent_role_dir", pathlib.Path("/tmp/nonexistent-staging-xyz")),
    ("agent_takkub.orchestrator.default_cwd_for_role", FAKE_CWD),
]


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda project: project or TEST_PROJECT),
    )
    o = Orchestrator()
    o._idle_watchdog.stop()
    return o


class TestEarlyCrashDetection:
    def test_early_exit_writes_dump(self, orch: Orchestrator, tmp_path: pathlib.Path) -> None:
        """Exit within CODEX_EARLY_CRASH_WINDOW_SEC → dump file created."""
        ekey = f"{TEST_PROJECT}::codex"
        orch._ps(ekey).codex_spawn_ts = time.time() - 30  # 30s ago = early crash

        session = MagicMock()
        session.display_lines.return_value = ["Booting MCP server: codex_apps", ""]

        pane = MagicMock()
        pane.state = "exited"
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["codex"] = pane

        dump_dir = tmp_path / "codex_crash_dumps"

        with (
            patch("agent_takkub.orchestrator.RUNTIME_DIR", tmp_path),
            patch("agent_takkub.orchestrator.ensure_runtime"),
            patch("agent_takkub.orchestrator._log_event") as mock_log,
        ):
            orch._on_codex_exit(1, "codex", FAKE_CWD, TEST_PROJECT, session)

        dump_files = list(dump_dir.glob("*.log"))
        assert len(dump_files) == 1, f"expected 1 dump, got {dump_files}"

        # _log_event called with codex_early_crash
        crash_calls = [c for c in mock_log.call_args_list if c.args[0] == "codex_early_crash"]
        assert len(crash_calls) == 1
        assert crash_calls[0].kwargs["exit_code"] == 1
        assert crash_calls[0].kwargs["time_to_exit_s"] == pytest.approx(30.0, abs=2)

    def test_late_exit_no_dump(self, orch: Orchestrator, tmp_path: pathlib.Path) -> None:
        """Exit after threshold → no dump file created."""
        ekey = f"{TEST_PROJECT}::codex"
        # spawn time far in the past → time_to_exit >> threshold
        orch._ps(ekey).codex_spawn_ts = time.time() - (CODEX_EARLY_CRASH_WINDOW_SEC + 60)

        session = MagicMock()
        pane = MagicMock()
        pane.state = "exited"
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["codex"] = pane

        dump_dir = tmp_path / "codex_crash_dumps"

        with (
            patch("agent_takkub.orchestrator.RUNTIME_DIR", tmp_path),
            patch("agent_takkub.orchestrator.ensure_runtime"),
            patch("agent_takkub.orchestrator._log_event") as mock_log,
        ):
            orch._on_codex_exit(0, "codex", FAKE_CWD, TEST_PROJECT, session)

        assert not dump_dir.exists() or not list(dump_dir.glob("*.log"))
        crash_calls = [c for c in mock_log.call_args_list if c.args[0] == "codex_early_crash"]
        assert len(crash_calls) == 0

    def test_delegates_to_on_session_exit(self, orch: Orchestrator, tmp_path: pathlib.Path) -> None:
        """_on_codex_exit always calls _on_session_exit regardless of crash status."""
        ekey = f"{TEST_PROJECT}::codex"
        orch._ps(ekey).codex_spawn_ts = time.time() - 10  # early crash

        session = MagicMock()
        session.display_lines.return_value = []

        pane = MagicMock()
        pane.state = "active"  # not exited — auto-respawn won't fire
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["codex"] = pane

        with (
            patch("agent_takkub.orchestrator.RUNTIME_DIR", tmp_path),
            patch("agent_takkub.orchestrator.ensure_runtime"),
            patch("agent_takkub.orchestrator._log_event"),
            patch.object(orch, "_on_session_exit") as mock_exit,
        ):
            orch._on_codex_exit(1, "codex", FAKE_CWD, TEST_PROJECT, session)

        mock_exit.assert_called_once_with("codex", FAKE_CWD, TEST_PROJECT)

    def test_spawn_time_cleared_after_exit(
        self, orch: Orchestrator, tmp_path: pathlib.Path
    ) -> None:
        """_codex_spawn_times entry is popped during _on_codex_exit."""
        ekey = f"{TEST_PROJECT}::codex"
        orch._ps(ekey).codex_spawn_ts = time.time() - 5

        session = MagicMock()
        session.display_lines.return_value = []
        pane = MagicMock()
        pane.state = "active"
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["codex"] = pane

        with (
            patch("agent_takkub.orchestrator.RUNTIME_DIR", tmp_path),
            patch("agent_takkub.orchestrator.ensure_runtime"),
            patch("agent_takkub.orchestrator._log_event"),
        ):
            orch._on_codex_exit(0, "codex", FAKE_CWD, TEST_PROJECT, session)

        assert (orch._pane_state.get(ekey) or PaneState()).codex_spawn_ts is None

    def test_dump_survives_display_lines_exception(
        self, orch: Orchestrator, tmp_path: pathlib.Path
    ) -> None:
        """If session.display_lines() raises, the dump is still written."""
        ekey = f"{TEST_PROJECT}::codex"
        orch._ps(ekey).codex_spawn_ts = time.time() - 20

        session = MagicMock()
        session.display_lines.side_effect = RuntimeError("PTY gone")

        pane = MagicMock()
        pane.state = "exited"
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["codex"] = pane

        dump_dir = tmp_path / "codex_crash_dumps"

        with (
            patch("agent_takkub.orchestrator.RUNTIME_DIR", tmp_path),
            patch("agent_takkub.orchestrator.ensure_runtime"),
            patch("agent_takkub.orchestrator._log_event"),
        ):
            orch._on_codex_exit(1, "codex", FAKE_CWD, TEST_PROJECT, session)

        dump_files = list(dump_dir.glob("*.log"))
        assert len(dump_files) == 1
        content = dump_files[0].read_text(encoding="utf-8")
        assert "(unavailable)" in content

    def test_dump_filename_format(self, orch: Orchestrator, tmp_path: pathlib.Path) -> None:
        """Dump filename follows <ts>-<project>-<role>.log pattern."""
        ekey = f"{TEST_PROJECT}::codex"
        orch._ps(ekey).codex_spawn_ts = time.time() - 5

        session = MagicMock()
        session.display_lines.return_value = ["line1"]
        pane = MagicMock()
        pane.state = "exited"
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["codex"] = pane

        dump_dir = tmp_path / "codex_crash_dumps"

        with (
            patch("agent_takkub.orchestrator.RUNTIME_DIR", tmp_path),
            patch("agent_takkub.orchestrator.ensure_runtime"),
            patch("agent_takkub.orchestrator._log_event"),
        ):
            orch._on_codex_exit(2, "codex", FAKE_CWD, TEST_PROJECT, session)

        files = list(dump_dir.glob("*.log"))
        assert len(files) == 1
        name = files[0].name
        assert name.endswith(f"-{TEST_PROJECT}-codex.log")
        # timestamp portion: YYYYMMDDTHHmmss (15 chars + 1 hyphen prefix = 16)
        ts_part = name[: name.index(f"-{TEST_PROJECT}")]
        assert len(ts_part) == 15  # e.g. "20260521T103045"
        assert ts_part[8] == "T"
