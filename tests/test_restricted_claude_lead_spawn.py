"""A restricted Claude Lead spawns like every Claude pane (2.2.17).

2.2.15 (#842) refused every non-Codex Lead under a restricted preset (`full`),
so a Claude Lead never came up on prod; 2.2.16 (#866) ran it under Claude
Code's dontAsk allowlist, which left the Lead unusable on other machines.
2.2.17 reverts to --dangerously-skip-permissions plus the cockpit's PreToolUse
guards. Every other spawn test patches `lead_is_restricted` to False, which is
why CI never saw either regression; these drive the real spawn path with the
restriction ON.
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


@pytest.mark.parametrize("restricted", [True, False])
def test_claude_lead_spawns_with_bypass_and_hooks(orch: Orchestrator, restricted: bool) -> None:
    ok, msg, argv = _spawn_lead(orch, restricted=restricted)

    assert ok, msg
    assert "--dangerously-skip-permissions" in argv
    assert "--permission-mode" not in argv
    settings = _settings(argv)
    assert "permissions" not in settings
    commands = [
        hook["command"] for entry in settings["hooks"]["PreToolUse"] for hook in entry["hooks"]
    ]
    assert "takkub _context-guard" in commands
    state = orch._pane_state[_exit_key(_PROJECT, "lead")]
    assert state.lead_enforcement_mode == "claude_hooks"


def test_restricted_claude_lead_keeps_agent_tool(orch: Orchestrator) -> None:
    ok, msg, argv = _spawn_lead(orch, restricted=True)

    assert ok, msg
    if "--disallowed-tools" in argv:
        assert "Agent" not in argv[argv.index("--disallowed-tools") + 1].split(",")
