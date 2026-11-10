"""#994: a devops pane ran a deploy script in the background and ended its
turn "waiting for the background notification"; the ssh inside hung, the
shell never exited, and the pane sat silent for 30 min — it HAS background
work, so the idle-at-prompt watchdog never fired. Now: one Lead notice after
BG_WAIT_NOTICE_S at the prompt with only background work and a frozen
screen. A notice only, never a restart (#968)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator


def _pane(at_prompt: bool = True, background: bool = True, state: str = "working"):
    session = SimpleNamespace(
        is_at_ready_prompt_cached=lambda: at_prompt, has_background_work=lambda: background
    )
    return SimpleNamespace(session=session, state=state)


def _ps(changed: float = 0.0):
    return SimpleNamespace(
        bg_wait_since_ts=0.0, bg_wait_notified=False, last_content_change_ts=changed
    )


def test_one_notice_after_the_quiet_window(monkeypatch) -> None:
    monkeypatch.setattr(orch_mod, "BG_WAIT_NOTICE_S", 600.0)
    fake = SimpleNamespace(_notify_lead=MagicMock())
    ps, pane = _ps(changed=1000.0), _pane()
    Orchestrator._track_background_wait(fake, "proj", "devops", pane, ps, 1000.0)
    Orchestrator._track_background_wait(fake, "proj", "devops", pane, ps, 1500.0)
    fake._notify_lead.assert_not_called()
    Orchestrator._track_background_wait(fake, "proj", "devops", pane, ps, 1601.0)
    Orchestrator._track_background_wait(fake, "proj", "devops", pane, ps, 2500.0)
    assert fake._notify_lead.call_count == 1
    assert "[bg-wait] devops" in fake._notify_lead.call_args.args[1]


def test_screen_activity_or_no_background_work_resets_the_clock(monkeypatch) -> None:
    monkeypatch.setattr(orch_mod, "BG_WAIT_NOTICE_S", 600.0)
    fake = SimpleNamespace(_notify_lead=MagicMock())
    ps = _ps(changed=1000.0)
    Orchestrator._track_background_wait(fake, "proj", "devops", _pane(), ps, 1000.0)
    ps.last_content_change_ts = 1550.0  # the job printed something
    Orchestrator._track_background_wait(fake, "proj", "devops", _pane(), ps, 1700.0)
    fake._notify_lead.assert_not_called()
    Orchestrator._track_background_wait(fake, "proj", "devops", _pane(background=False), ps, 9000.0)
    assert ps.bg_wait_since_ts == 0.0
    Orchestrator._track_background_wait(fake, "proj", "devops", _pane(state="done"), ps, 9999.0)
    fake._notify_lead.assert_not_called()
