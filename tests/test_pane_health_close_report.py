"""#280 close-path proof — a pane that dies WITHOUT reporting must still hand
Lead what the watchdogs saw.

`done()` folding health into its report (tests/test_pane_health_reporting.py)
only covers panes that report. The whole reason the old design narrated live
was the other case: a pane that gets closed, crashes, or is torn down having
never called `done`. If nothing spoke for it at close, holding the
observations back would turn "noisy" into "silent", which is worse.

Uses a real `Orchestrator()` and a real `close()` — the same harness
tests/test_auto_chain.py drives close() with — rather than stubs, so this
exercises the actual teardown path.
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

PROJECT = "healthclose"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture(autouse=True)
def _stub_verify_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settle the submit-verify chain synchronously — these tests assert on
    what reaches Lead, not on QTimer timing (same reason test_auto_chain.py
    stubs it)."""

    def _fake_verified(*_args, **kwargs):
        on_settled = kwargs.get("on_settled")
        if on_settled is not None:
            on_settled()

    monkeypatch.setattr(orch_mod, "_delayed_enter_verified", _fake_verified)


@pytest.fixture
def project_json(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pj = tmp_path / "projects.json"
    pj.write_text(
        json.dumps(
            {
                "active": PROJECT,
                "projects": {PROJECT: {"paths": {"api": str(tmp_path / "api")}}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "PROJECTS_JSON", pj)
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", runtime)


def _orch(roles: list[str]) -> tuple[Orchestrator, dict[str, MagicMock]]:
    orch = Orchestrator()
    orch._idle_watchdog.stop()
    panes: dict[str, MagicMock] = {}
    for role in roles:
        pane = MagicMock()
        pane._session_cwd = "/tmp"
        pane._transcript_path = None
        pane.session = MagicMock()
        pane.session.is_alive = True
        pane.session.write = MagicMock()
        pane.set_state = MagicMock()
        pane.mark_expected_exit = MagicMock()
        panes[role] = pane
    orch._panes_by_project[PROJECT] = panes
    return orch, panes


def test_close_without_done_reports_what_was_observed(
    qapp: QCoreApplication, project_json: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TAKKUB_PANE_WATCH_NOTICES", raising=False)
    orch, _panes = _orch(["lead", "frontend"])
    orch._ps(f"{PROJECT}::frontend").assign_ts = 1.0
    notices: list[str] = []
    monkeypatch.setattr(orch, "_notify_lead", lambda ns, body, **kw: notices.append(body) or None)

    with patch("agent_takkub.lead_inbox._log_event"):
        orch._warn_lead_delivery_boot_stall("frontend", PROJECT, 110.0)
    assert notices == [], "nothing may reach Lead while the pane is still alive"

    orch.close("frontend", project=PROJECT, force=True)

    health = [n for n in notices if "[pane health]" in n]
    assert health, "a pane that never reported must still speak at close"
    assert "closed] ปิดโดยไม่มีรายงาน done" in health[0]
    assert "boot ช้า 110s" in health[0]


def test_close_of_a_clean_pane_says_nothing(
    qapp: QCoreApplication, project_json: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Report-at-close must not become its own source of noise."""
    orch, _panes = _orch(["lead", "frontend"])
    orch._ps(f"{PROJECT}::frontend").assign_ts = 1.0
    notices: list[str] = []
    monkeypatch.setattr(orch, "_notify_lead", lambda ns, body, **kw: notices.append(body) or None)

    orch.close("frontend", project=PROJECT, force=True)

    assert not [n for n in notices if "[pane health]" in n]


def test_health_is_not_reported_twice_after_done_then_autoclose(
    qapp: QCoreApplication, project_json: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """done() drains the health; the auto-close 2.5s later must not repeat it."""
    monkeypatch.delenv("TAKKUB_PANE_WATCH_NOTICES", raising=False)
    orch, _panes = _orch(["lead", "frontend"])
    notices: list[str] = []
    monkeypatch.setattr(orch, "_notify_lead", lambda ns, body, **kw: notices.append(body) or None)

    with patch("agent_takkub.lead_inbox._log_event"):
        orch._warn_lead_delivery_boot_stall("frontend", PROJECT, 110.0)
    orch.done("frontend", note="เสร็จ", project=PROJECT)
    orch.close("frontend", project=PROJECT, force=True)

    health = [n for n in notices if "[pane health]" in n]
    assert len(health) == 1, f"expected exactly one health report, got {len(health)}"
    assert health[0].startswith("[frontend done]")
