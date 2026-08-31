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
    o.shutdown_timers()
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

    def test_rtk_hook_present_when_available(
        self, tmp_env: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rtk_helper, "rtk_binary_available", lambda: True)
        path = hook_wiring.ensure_hook_settings_file()
        assert rtk_helper.RTK_HOOK_COMMAND in self._pre_cmds(path)
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        assert hook_wiring.HOOK_COMMAND in [
            h["command"] for grp in data["hooks"]["Stop"] for h in grp["hooks"]
        ]
        assert "SessionStart" in data["hooks"]

    def test_no_rtk_hook_when_binary_missing(
        self, tmp_env: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Binary gone: never inject a `rtk hook claude` command
        # that would make every Bash tool call in the pane fail.
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


class TestConciseOutputStyle:
    """#318: the Concise output style rides `--settings` (a per-invocation
    session override, docs/en/settings precedence tier 2) instead of any
    shared settings file, and is opt-in per role via TAKKUB_CONCISE_ROLES —
    default pilot is `qa` only, Lead is always excluded."""

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TAKKUB_CONCISE_ROLES", raising=False)

    def test_default_pilot_is_qa_only(self) -> None:
        assert hook_wiring.role_wants_concise("qa", is_lead=False) is True
        assert hook_wiring.role_wants_concise("backend", is_lead=False) is False

    def test_lead_never_concise_even_if_named_in_allowlist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TAKKUB_CONCISE_ROLES", "*")
        assert hook_wiring.role_wants_concise("lead", is_lead=True) is False

    def test_wildcard_env_enables_every_teammate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_CONCISE_ROLES", "*")
        assert hook_wiring.role_wants_concise("backend", is_lead=False) is True
        assert hook_wiring.role_wants_concise("devops", is_lead=False) is True

    def test_empty_env_disables_the_default_pilot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_CONCISE_ROLES", "")
        assert hook_wiring.role_wants_concise("qa", is_lead=False) is False

    def test_custom_roster_replaces_default_pilot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_CONCISE_ROLES", "backend,devops")
        assert hook_wiring.role_wants_concise("backend", is_lead=False) is True
        assert hook_wiring.role_wants_concise("qa", is_lead=False) is False

    def test_concise_file_carries_output_style_key(self, tmp_env: pathlib.Path) -> None:
        path = hook_wiring.ensure_hook_settings_file(concise=True)
        assert path.endswith("hook-settings-concise.json")
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        assert data["outputStyle"] == "Concise"

    def test_non_concise_file_has_no_output_style_key(self, tmp_env: pathlib.Path) -> None:
        path = hook_wiring.ensure_hook_settings_file(concise=False)
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        assert "outputStyle" not in data

    def test_concise_and_plain_files_are_separate_paths(self, tmp_env: pathlib.Path) -> None:
        plain = hook_wiring.ensure_hook_settings_file(concise=False)
        concise = hook_wiring.ensure_hook_settings_file(concise=True)
        assert plain != concise
        assert pathlib.Path(plain).exists()
        assert pathlib.Path(concise).exists()


class TestRemoteControlStartup:
    """#458: Claude Code 2.1.251 defaults Remote Control to on for every pane
    unless `remoteControlAtStartup` is stamped explicitly. Default roster is
    Lead only — the opposite default from concise's opt-in pilot."""

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TAKKUB_REMOTE_CONTROL_ROLES", raising=False)

    def test_default_is_lead_only(self) -> None:
        assert hook_wiring.role_wants_remote_control("lead", is_lead=True) is True
        assert hook_wiring.role_wants_remote_control("backend", is_lead=False) is False

    def test_wildcard_env_enables_every_role(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_REMOTE_CONTROL_ROLES", "*")
        assert hook_wiring.role_wants_remote_control("backend", is_lead=False) is True
        assert hook_wiring.role_wants_remote_control("qa", is_lead=False) is True

    def test_empty_env_disables_even_lead(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_REMOTE_CONTROL_ROLES", "")
        assert hook_wiring.role_wants_remote_control("lead", is_lead=True) is False

    def test_custom_roster_replaces_default_and_matches_by_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TAKKUB_REMOTE_CONTROL_ROLES", "backend,devops")
        assert hook_wiring.role_wants_remote_control("backend", is_lead=False) is True
        assert hook_wiring.role_wants_remote_control("lead", is_lead=True) is False

    def test_settings_file_always_carries_explicit_bool(self, tmp_env: pathlib.Path) -> None:
        on = json.loads(
            pathlib.Path(hook_wiring.ensure_hook_settings_file(remote_control=True)).read_text(
                encoding="utf-8"
            )
        )
        off = json.loads(
            pathlib.Path(hook_wiring.ensure_hook_settings_file(remote_control=False)).read_text(
                encoding="utf-8"
            )
        )
        assert on["remoteControlAtStartup"] is True
        assert off["remoteControlAtStartup"] is False

    def test_default_call_keeps_original_bare_filename(self, tmp_env: pathlib.Path) -> None:
        # doctor.check_hook_wiring() calls ensure_hook_settings_file() with no
        # args and must keep resolving to the same path it always has.
        path = hook_wiring.ensure_hook_settings_file()
        assert pathlib.Path(path).name == "hook-settings.json"

    def test_norc_file_is_a_separate_path_from_default(self, tmp_env: pathlib.Path) -> None:
        default_path = hook_wiring.ensure_hook_settings_file()
        norc_path = hook_wiring.ensure_hook_settings_file(remote_control=False)
        assert default_path != norc_path
        assert pathlib.Path(norc_path).name == "hook-settings-norc.json"
        assert pathlib.Path(default_path).exists()
        assert pathlib.Path(norc_path).exists()

    def test_concise_and_norc_combine_into_their_own_file(self, tmp_env: pathlib.Path) -> None:
        path = hook_wiring.ensure_hook_settings_file(concise=True, remote_control=False)
        assert pathlib.Path(path).name == "hook-settings-concise-norc.json"


class TestClaudeSpawnArgvIncludesSettings:
    def test_teammate_spawn_gets_settings_flag(self, orch: Orchestrator) -> None:
        argv = _spawn_capture(orch, "backend")
        assert "--settings" in argv
        settings_path = argv[argv.index("--settings") + 1]
        # backend is a non-Lead role: concise stays off by default (#318)
        # but so does remote control now (#458) -> the "-norc" file.
        assert settings_path.endswith("hook-settings-norc.json")
        assert pathlib.Path(settings_path).exists()
        data = json.loads(pathlib.Path(settings_path).read_text(encoding="utf-8"))
        assert data["remoteControlAtStartup"] is False

    def test_lead_spawn_gets_settings_flag(self, orch: Orchestrator) -> None:
        argv = _spawn_capture(orch, "lead")
        assert "--settings" in argv
        settings_path = argv[argv.index("--settings") + 1]
        data = json.loads(pathlib.Path(settings_path).read_text(encoding="utf-8"))
        assert data["remoteControlAtStartup"] is True

    def test_shell_pane_does_not_get_settings_flag(self, orch: Orchestrator) -> None:
        # shell is a plain terminal pane — never runs claude, so it must not
        # get claude-only flags at all.
        argv = _spawn_capture(orch, "shell")
        assert "--settings" not in argv

    def test_default_pilot_role_spawn_gets_concise_settings_file(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TAKKUB_CONCISE_ROLES", raising=False)
        monkeypatch.delenv("TAKKUB_REMOTE_CONTROL_ROLES", raising=False)
        argv = _spawn_capture(orch, "qa")
        settings_path = argv[argv.index("--settings") + 1]
        # qa is concise (default pilot) and non-Lead (rc off by default).
        assert settings_path.endswith("hook-settings-concise-norc.json")

    def test_lead_spawn_never_gets_concise_settings_file(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TAKKUB_CONCISE_ROLES", "*")
        argv = _spawn_capture(orch, "lead")
        settings_path = argv[argv.index("--settings") + 1]
        assert settings_path.endswith("hook-settings.json")
        assert not settings_path.endswith("hook-settings-concise.json")
