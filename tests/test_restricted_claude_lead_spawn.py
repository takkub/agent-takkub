"""2.2.16: a restricted Claude Lead spawns under Claude Code's dontAsk boundary.

2.2.15 (#842) refused every non-Codex Lead under a restricted preset (`full`),
so a Claude Lead never came up on prod. Every existing spawn test patched
`lead_is_restricted` to False, which is why CI never saw it. These tests drive
the real spawn path with the restriction ON.
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import config
from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator
from agent_takkub.orchestrator_text import _exit_key

_PROJECT = "default"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(qapp: QCoreApplication, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
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
    o = Orchestrator()
    o.shutdown_timers()
    return o


def _spawn_lead(orch: Orchestrator, *, restricted: bool, provider: str = "claude"):
    fake_pane = MagicMock()
    fake_pane.session = None
    fake_pane.state = "empty"
    fake_pane._transcript_path = None
    orch._panes_by_project.setdefault(_PROJECT, {})["lead"] = fake_pane

    captured: list[list[str]] = []
    fake_session = MagicMock()
    with (
        patch("agent_takkub.lead_enforcement.lead_is_restricted", return_value=restricted),
        patch("agent_takkub.lead_enforcement.verify_claude_version", return_value=None),
        patch.object(orch, "effective_provider_for", return_value=provider, create=True),
        patch.object(orch_mod.PtySession, "__new__", return_value=fake_session),
        patch.object(
            fake_session,
            "spawn",
            side_effect=lambda argv, cwd, env, **kwargs: captured.append(list(argv)),
        ),
    ):
        ok, msg = orch.spawn("lead", cwd=str(config.RUNTIME_DIR.parent), project=_PROJECT)
    return ok, msg, (captured[0] if captured else [])


def _settings(argv: list[str]) -> dict:
    path = argv[argv.index("--settings") + 1]
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def test_restricted_claude_lead_spawns_under_dontask(orch: Orchestrator) -> None:
    ok, msg, argv = _spawn_lead(orch, restricted=True)

    assert ok, msg
    assert "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    denied = argv[argv.index("--disallowed-tools") + 1].split(",")
    assert "Agent" in denied
    perms = _settings(argv)["permissions"]
    assert perms["defaultMode"] == "dontAsk"
    assert perms["disableBypassPermissionsMode"] == "disable"
    edit_rules = [rule for rule in perms["allow"] if rule.startswith("Edit(")]
    assert edit_rules
    # The takkub CLI writes the readiness ack itself; the model must not be
    # able to forge it with its own edit tools.
    assert not any("lead-readiness" in rule or "lead-tmp" in rule for rule in edit_rules)
    state = orch._pane_state[_exit_key(_PROJECT, "lead")]
    assert state.lead_enforcement_mode == "claude_permissions"


def test_restricted_claude_lead_settings_skip_rtk_rewrite(orch: Orchestrator) -> None:
    with patch("agent_takkub.rtk_helper.rtk_should_inject", return_value=True):
        ok, msg, argv = _spawn_lead(orch, restricted=True)

    assert ok, msg
    commands = [
        hook["command"]
        for entry in _settings(argv)["hooks"]["PreToolUse"]
        for hook in entry["hooks"]
    ]
    assert not any("rtk" in command for command in commands)


def test_unrestricted_claude_lead_keeps_bypass(orch: Orchestrator) -> None:
    ok, msg, argv = _spawn_lead(orch, restricted=False)

    assert ok, msg
    assert "--dangerously-skip-permissions" in argv
    assert "--permission-mode" not in argv
    assert "permissions" not in _settings(argv)
    state = orch._pane_state[_exit_key(_PROJECT, "lead")]
    assert state.lead_enforcement_mode == "none"


def test_restricted_claude_lead_fails_closed_on_old_cli(orch: Orchestrator) -> None:
    fake_pane = MagicMock()
    fake_pane.session = None
    orch._panes_by_project.setdefault(_PROJECT, {})["lead"] = fake_pane
    with (
        patch("agent_takkub.lead_enforcement.lead_is_restricted", return_value=True),
        patch(
            "agent_takkub.lead_enforcement.verify_claude_version",
            side_effect=RuntimeError("Claude Code 2.1.200 is older than 2.1.267"),
        ),
        patch.object(orch_mod.PtySession, "__new__") as new_session,
    ):
        ok, msg = orch.spawn("lead", cwd=str(config.RUNTIME_DIR.parent), project=_PROJECT)

    assert not ok
    assert "older than" in msg
    new_session.assert_not_called()
