"""Tests for the #308 stuck-tool watchdog.

Real incident: an agy (gemini) pane called a shell tool and its screen
showed "Running command..." permanently for ~13 minutes, while the
provider's own idle footer ("? for shortcuts") stayed visible right below
it — so `is_at_ready_prompt()` and the pre-existing content-hash stuck
watchdog (`_check_stuck_panes`) both read the pane as normal the whole
time. This file covers the pieces built to catch that case anyway:

  - `provider_spec.tool_running_markers_for` / `ProviderSpec.tool_running_markers`
  - `PtySession.tool_running_marker` — the real screen-scrape, independent
    of ready-prompt classification
  - `Orchestrator._check_stuck_tool_panes` — the watchdog state machine:
    escalate once -> one-shot Esc -> grace period -> recovered nudge OR
    close+respawn recommendation (never auto-respawns)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent_takkub.orchestrator import (
    LEAD,
    TOOL_STUCK_ESC_GRACE_S,
    TOOL_STUCK_TIMEOUT_SEC,
    Orchestrator,
    PaneState,
)
from agent_takkub.provider_spec import tool_running_markers_for
from agent_takkub.pty_session import PtySession


class TestProviderSpecMarkers:
    def test_gemini_marker_is_confirmed_incident_text(self) -> None:
        assert "running command" in tool_running_markers_for("gemini")

    def test_unknown_provider_has_no_markers(self) -> None:
        assert tool_running_markers_for("not-a-real-provider") == ()

    def test_every_registered_provider_returns_a_tuple(self) -> None:
        for name in ("claude", "codex", "gemini", "opencode", "kimi", "cursor"):
            assert isinstance(tool_running_markers_for(name), tuple)


class TestPtySessionToolRunningMarker:
    def test_matches_confirmed_gemini_marker(self) -> None:
        s = PtySession(cols=80, rows=24)
        s._feed_and_log(b"Running command...\n? for shortcuts")
        assert s.tool_running_marker("gemini") == "running command"

    def test_case_insensitive(self) -> None:
        s = PtySession(cols=80, rows=24)
        s._feed_and_log(b"RUNNING COMMAND...")
        assert s.tool_running_marker("gemini") == "running command"

    def test_no_marker_when_idle(self) -> None:
        s = PtySession(cols=80, rows=24)
        s._feed_and_log(b"? for shortcuts")
        assert s.tool_running_marker("gemini") is None

    def test_independent_of_idle_footer_coexisting_on_screen(self) -> None:
        """The #308 shape: idle footer visible right below the stuck line.
        `tool_running_marker` must still fire regardless of what
        `is_at_ready_prompt()` says about the same screen."""
        s = PtySession(cols=80, rows=24)
        s._feed_and_log(b"Running command...\r\n? for shortcuts")
        assert s.tool_running_marker("gemini") == "running command"
        assert s.is_at_ready_prompt() is True  # the false-idle read #308 exposed

    def test_unknown_provider_never_matches(self) -> None:
        s = PtySession(cols=80, rows=24)
        s._feed_and_log(b"Running command...")
        assert s.tool_running_marker("not-a-real-provider") is None


class _FakeSession:
    def __init__(self, marker: str | None, seconds_since_output: float) -> None:
        self.is_alive = True
        self._marker = marker
        self._seconds_since_output = seconds_since_output
        self.written: list[str] = []

    def tool_running_marker(self, provider: str) -> str | None:
        return self._marker

    def seconds_since_output(self) -> float:
        return self._seconds_since_output

    def write(self, data) -> bool:
        self.written.append(data)
        return True


class _FakePane:
    def __init__(self, state: str = "working", session: object | None = None) -> None:
        self.state = state
        self.session = session


class _FakeOrch:
    def __init__(self) -> None:
        self._panes_by_project: dict[str, dict] = {}
        self._pane_state: dict[str, PaneState] = {}
        self.notify_calls: list[tuple[str, str, str | None]] = []

    def _ps(self, key: str) -> PaneState:
        try:
            return self._pane_state[key]
        except KeyError:
            ps = PaneState()
            self._pane_state[key] = ps
            return ps

    def _notify_lead(self, project, notice, from_role=None, note="") -> None:
        self.notify_calls.append((project, notice, from_role))


def _check(fake: _FakeOrch, now: float) -> None:
    Orchestrator._check_stuck_tool_panes(fake, now)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _stub_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    # _check_stuck_tool_panes resolves the provider via a lazy
    # `from .provider_config import effective_provider_for` inside the
    # method body — patching the module attribute takes effect on the next
    # call regardless, same lazy-import pattern provider_spec.py documents.
    monkeypatch.setattr(
        "agent_takkub.provider_config.effective_provider_for",
        lambda role, project=None: "gemini",
    )


class TestCheckStuckToolPanes:
    def test_lead_is_exempt(self) -> None:
        fake = _FakeOrch()
        session = _FakeSession("running command", TOOL_STUCK_TIMEOUT_SEC)
        fake._panes_by_project["p"] = {LEAD.name: _FakePane(session=session)}
        _check(fake, 1_000_000.0)
        assert session.written == []
        assert fake.notify_calls == []

    def test_non_working_state_skipped(self) -> None:
        fake = _FakeOrch()
        session = _FakeSession("running command", TOOL_STUCK_TIMEOUT_SEC)
        fake._panes_by_project["p"] = {"backend": _FakePane(state="done", session=session)}
        _check(fake, 1_000_000.0)
        assert session.written == []

    def test_marker_present_but_under_timeout_is_not_stuck(self) -> None:
        fake = _FakeOrch()
        session = _FakeSession("running command", TOOL_STUCK_TIMEOUT_SEC - 1)
        fake._panes_by_project["p"] = {"gemini": _FakePane(session=session)}
        _check(fake, 1_000_000.0)
        assert session.written == []
        assert fake.notify_calls == []

    def test_no_marker_is_not_stuck(self) -> None:
        fake = _FakeOrch()
        session = _FakeSession(None, TOOL_STUCK_TIMEOUT_SEC + 100)
        fake._panes_by_project["p"] = {"gemini": _FakePane(session=session)}
        _check(fake, 1_000_000.0)
        assert session.written == []
        assert fake.notify_calls == []

    def test_first_detection_notifies_lead_and_sends_esc_once(self) -> None:
        fake = _FakeOrch()
        session = _FakeSession("running command", TOOL_STUCK_TIMEOUT_SEC)
        fake._panes_by_project["p"] = {"gemini": _FakePane(session=session)}
        now = 1_000_000.0
        _check(fake, now)

        assert session.written == ["\x1b"]
        assert len(fake.notify_calls) == 1
        assert "gemini" in fake.notify_calls[0][1]
        ps = fake._pane_state["p::gemini"]
        assert ps.tool_stuck_escalated is True
        assert ps.tool_stuck_esc_sent_ts == now

    def test_still_stuck_within_grace_sends_no_second_esc_or_notice(self) -> None:
        fake = _FakeOrch()
        session = _FakeSession("running command", TOOL_STUCK_TIMEOUT_SEC)
        fake._panes_by_project["p"] = {"gemini": _FakePane(session=session)}
        now = 1_000_000.0
        _check(fake, now)
        _check(fake, now + 1)  # still within TOOL_STUCK_ESC_GRACE_S

        assert session.written == ["\x1b"]  # not sent again
        assert len(fake.notify_calls) == 1  # no repeat notice yet

    def test_recovered_within_grace_sends_nudge_and_clears_state(self) -> None:
        fake = _FakeOrch()
        session = _FakeSession("running command", TOOL_STUCK_TIMEOUT_SEC)
        fake._panes_by_project["p"] = {"gemini": _FakePane(session=session)}
        now = 1_000_000.0
        _check(fake, now)  # escalates, sends Esc

        # Esc "worked": marker clears, screen changes (seconds_since_output resets)
        session._marker = None
        session._seconds_since_output = 0.0
        _check(fake, now + 2)

        assert session.written[-1] != "\x1b"  # a nudge, not another Esc
        assert len(fake.notify_calls) == 2  # stuck notice + recovered notice
        assert "หลุดจาก" in fake.notify_calls[-1][1] or "recover" in fake.notify_calls[-1][1].lower()
        ps = fake._pane_state["p::gemini"]
        assert ps.tool_stuck_escalated is False
        assert ps.tool_stuck_esc_sent_ts == 0.0
        assert ps.tool_stuck_close_recommended is False

    def test_still_stuck_past_grace_recommends_close_once(self) -> None:
        fake = _FakeOrch()
        session = _FakeSession("running command", TOOL_STUCK_TIMEOUT_SEC)
        fake._panes_by_project["p"] = {"gemini": _FakePane(session=session)}
        now = 1_000_000.0
        _check(fake, now)  # escalates, sends Esc
        session._seconds_since_output = TOOL_STUCK_TIMEOUT_SEC + TOOL_STUCK_ESC_GRACE_S
        _check(fake, now + TOOL_STUCK_ESC_GRACE_S)  # grace elapsed, still stuck

        assert len(fake.notify_calls) == 2
        assert "close" in fake.notify_calls[-1][1].lower()
        ps = fake._pane_state["p::gemini"]
        assert ps.tool_stuck_close_recommended is True

        # A further tick still stuck must NOT repeat the close recommendation.
        _check(fake, now + TOOL_STUCK_ESC_GRACE_S + 5)
        assert len(fake.notify_calls) == 2

    def test_dead_session_skipped(self) -> None:
        fake = _FakeOrch()
        session = _FakeSession("running command", TOOL_STUCK_TIMEOUT_SEC)
        session.is_alive = False
        fake._panes_by_project["p"] = {"gemini": _FakePane(session=session)}
        _check(fake, 1_000_000.0)
        assert session.written == []

    def test_magicmock_session_never_misfires(self) -> None:
        """A loosely-mocked session (no explicit stub for the new methods)
        must not be misread as stuck — MagicMock's auto-attributes are
        truthy by default, which is exactly the trap `_check_stuck_panes`'s
        own isinstance guard already exists to avoid."""
        fake = _FakeOrch()
        session = MagicMock()
        session.is_alive = True
        fake._panes_by_project["p"] = {"gemini": _FakePane(session=session)}
        _check(fake, 1_000_000.0)
        session.write.assert_not_called()
        assert fake.notify_calls == []
