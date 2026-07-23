"""Tests for the Stop/Notification -> `takkub _hook` wiring (hook_wiring.py)
and its injection into every claude-backed pane's spawn argv.
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import config, hook_wiring, rtk_helper
from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator

_PROJECT = "default"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def tmp_env(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
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


def _spawn_capture(orch: Orchestrator, role_name: str, cwd: str = "/proj") -> list[str]:
    fake_pane = MagicMock()
    fake_pane.session = None
    fake_pane.state = "empty"
    fake_pane.attach_session = MagicMock()
    fake_pane._transcript_path = None
    orch._panes_by_project.setdefault(_PROJECT, {})[role_name] = fake_pane

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
            orch.spawn(role_name, cwd=cwd, project=_PROJECT)

    return captured[0] if captured else []


class TestHookSettingsFile:
    def test_writes_valid_json_wired_to_hook_command(self, tmp_env: pathlib.Path) -> None:
        path = hook_wiring.ensure_hook_settings_file()
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))

        stop_cmds = [h.get("command") for grp in data["hooks"]["Stop"] for h in grp["hooks"]]
        notif_groups = data["hooks"]["Notification"]
        notif_cmds = [h.get("command") for grp in notif_groups for h in grp["hooks"]]

        assert hook_wiring.HOOK_COMMAND in stop_cmds
        assert hook_wiring.HOOK_COMMAND in notif_cmds
        assert notif_groups[0]["matcher"] == "idle_prompt"

    def test_session_start_wired_to_session_report_command(self, tmp_env: pathlib.Path) -> None:
        path = hook_wiring.ensure_hook_settings_file()
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))

        start_cmds = [
            h.get("command") for grp in data["hooks"]["SessionStart"] for h in grp["hooks"]
        ]
        assert hook_wiring.SESSION_REPORT_COMMAND in start_cmds

    def test_resolves_runtime_dir_at_call_time(
        self, tmp_env: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guards against caching config.RUNTIME_DIR at import time — tests
        (and multi-project cockpit runs) monkeypatch config.RUNTIME_DIR, and
        a stale binding would silently write to the wrong directory."""
        other_runtime = tmp_env / "other_runtime"
        other_runtime.mkdir()
        monkeypatch.setattr(config, "RUNTIME_DIR", other_runtime)

        path = hook_wiring.ensure_hook_settings_file()

        assert pathlib.Path(path).parent == other_runtime

    def test_idempotent_no_rewrite_when_unchanged(self, tmp_env: pathlib.Path) -> None:
        path1 = hook_wiring.ensure_hook_settings_file()
        mtime1 = pathlib.Path(path1).stat().st_mtime_ns
        path2 = hook_wiring.ensure_hook_settings_file()
        mtime2 = pathlib.Path(path2).stat().st_mtime_ns

        assert path1 == path2
        assert mtime1 == mtime2  # second call must not touch the file


class TestRtkInjection:
    """A3: the rtk PreToolUse Bash hook is folded into the SAME central
    --settings file when rtk is enabled + on PATH, so it reaches panes
    without dirtying any project's .claude/settings.json."""

    @pytest.fixture(autouse=True)
    def _isolate_flag(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "SETTINGS_HOME", tmp_path / "settings-home")

    def _pre_cmds(self, path: str) -> list[str]:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        pre = data["hooks"].get("PreToolUse", [])
        return [h.get("command") for grp in pre for h in grp.get("hooks", [])]

    def test_no_rtk_hook_when_disabled(
        self, tmp_env: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rtk_helper, "rtk_binary_available", lambda: True)
        # not enabled → no rtk command in PreToolUse. The block itself still
        # exists because the pane_guard hook is unconditional (see
        # TestGuardInjection); only rtk's entry is absent.
        path = hook_wiring.ensure_hook_settings_file()
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        assert rtk_helper.RTK_HOOK_COMMAND not in self._pre_cmds(path)
        assert hook_wiring.HOOK_COMMAND in [
            h["command"] for grp in data["hooks"]["Stop"] for h in grp["hooks"]
        ]

    def test_rtk_hook_present_when_enabled_and_available(
        self, tmp_env: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rtk_helper, "rtk_binary_available", lambda: True)
        rtk_helper.set_rtk_enabled(True)
        path = hook_wiring.ensure_hook_settings_file()
        assert rtk_helper.RTK_HOOK_COMMAND in self._pre_cmds(path)
        # pane-state hooks still present alongside it
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        assert "SessionStart" in data["hooks"]

    def test_no_rtk_hook_when_binary_missing(
        self, tmp_env: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Enabled but binary gone: never inject a `rtk hook claude` command
        # that would make every Bash tool call in the pane fail.
        rtk_helper.set_rtk_enabled(True)
        monkeypatch.setattr(rtk_helper, "rtk_binary_available", lambda: False)
        path = hook_wiring.ensure_hook_settings_file()
        assert rtk_helper.RTK_HOOK_COMMAND not in self._pre_cmds(path)


class TestGuardInjection:
    """The pane_guard PreToolUse Bash hook is unconditional — it is what stops
    a teammate shelling around its MCP tool policy (`npx playwright`), so it
    must be present whether or not rtk is enabled, and must never displace
    rtk's own entry."""

    @pytest.fixture(autouse=True)
    def _isolate_flag(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "SETTINGS_HOME", tmp_path / "settings-home")

    def _pre_cmds(self, path: str) -> list[str]:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        pre = data["hooks"].get("PreToolUse", [])
        return [h.get("command") for grp in pre for h in grp.get("hooks", [])]

    def test_guard_present_when_rtk_disabled(
        self, tmp_env: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rtk_helper, "rtk_binary_available", lambda: False)
        path = hook_wiring.ensure_hook_settings_file()
        assert hook_wiring.GUARD_COMMAND in self._pre_cmds(path)

    def test_guard_and_rtk_coexist_guard_first(
        self, tmp_env: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rtk_helper, "rtk_binary_available", lambda: True)
        rtk_helper.set_rtk_enabled(True)
        cmds = self._pre_cmds(hook_wiring.ensure_hook_settings_file())
        assert hook_wiring.GUARD_COMMAND in cmds
        assert rtk_helper.RTK_HOOK_COMMAND in cmds
        # deny before rtk spends work rewriting a command that won't run
        assert cmds.index(hook_wiring.GUARD_COMMAND) < cmds.index(rtk_helper.RTK_HOOK_COMMAND)

    def test_guard_matcher_is_bash(self, tmp_env: pathlib.Path) -> None:
        data = json.loads(
            pathlib.Path(hook_wiring.ensure_hook_settings_file()).read_text(encoding="utf-8")
        )
        entry = next(
            grp
            for grp in data["hooks"]["PreToolUse"]
            if any(h.get("command") == hook_wiring.GUARD_COMMAND for h in grp.get("hooks", []))
        )
        assert entry["matcher"] == "Bash"


class TestClaudeSpawnArgvIncludesSettings:
    def test_teammate_spawn_gets_settings_flag(self, orch: Orchestrator) -> None:
        argv = _spawn_capture(orch, "backend")
        assert "--settings" in argv
        settings_path = argv[argv.index("--settings") + 1]
        assert settings_path.endswith("hook-settings.json")
        assert pathlib.Path(settings_path).exists()

    def test_lead_spawn_gets_settings_flag(self, orch: Orchestrator) -> None:
        argv = _spawn_capture(orch, "lead")
        assert "--settings" in argv

    def test_shell_pane_does_not_get_settings_flag(self, orch: Orchestrator) -> None:
        # shell is a plain terminal pane — never runs claude, so it must not
        # get claude-only flags at all.
        argv = _spawn_capture(orch, "shell")
        assert "--settings" not in argv
