"""Tests for `_auto_trust`'s configurable poll window (issue #186).

`_auto_trust` used a hardcoded 30s watch window regardless of provider. That
is fine for claude/codex, whose trust modal renders essentially immediately
at spawn, but agy's own cold-boot allowance (`ready_wait_ms`) is up to 90s —
under multi-role fan-out contention the modal can render after 30s, leaving
it stuck with no one left polling to answer it. `_auto_trust(max_ms=...)`
lets a caller pass a longer window; `_launch_session` forwards it from the
provider's own `ready_wait_ms` (covered separately in test_launch_session.py).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator, _exit_key


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _never_trusted_pane() -> MagicMock:
    p = MagicMock()
    p.session = MagicMock()
    p.session.is_alive = True
    p.session.is_at_trust_prompt.return_value = False
    p.session.trust_prompt_selects_no.return_value = False
    p.session.write = MagicMock()
    return p


@pytest.fixture
def orch(qapp, monkeypatch) -> Orchestrator:
    o = Orchestrator.__new__(Orchestrator)
    QObject.__init__(o)
    o._panes_by_project = {}
    monkeypatch.setattr(o, "_resolve_project", lambda p=None: p or "P")
    monkeypatch.setattr(
        o, "_project_panes", lambda p=None: o._panes_by_project.get(o._resolve_project(p), {})
    )
    return o


def _drive_timer_queue(calls: list[tuple]) -> int:
    """Run every (ms, fn) the poller schedules, popping newly-scheduled
    calls onto the same queue as they arrive, until it stops rescheduling.
    Returns how many `_check` ticks fired."""
    n = 0
    while calls:
        _ms, fn = calls.pop(0)
        fn()
        n += 1
    return n


class TestAutoTrustPollWindow:
    def test_default_window_matches_historical_30s(self, orch, monkeypatch) -> None:
        pane = _never_trusted_pane()
        orch._panes_by_project["P"] = {"backend": pane}
        calls: list[tuple] = []
        monkeypatch.setattr(
            orch_mod.QTimer, "singleShot", staticmethod(lambda ms, fn: calls.append((ms, fn)))
        )

        orch._auto_trust("backend", project="P")
        ticks = _drive_timer_queue(calls)

        # 500ms cadence, giving up once elapsed >= 30_000ms → 60 ticks.
        assert ticks == 60
        pane.session.write.assert_not_called()  # never saw the trust prompt

    def test_custom_max_ms_extends_the_window(self, orch, monkeypatch) -> None:
        """A cold-boot provider's ready_wait_ms (e.g. agy's 90_000) must
        extend the watch window proportionally, not stay pinned at 30s."""
        pane = _never_trusted_pane()
        orch._panes_by_project["P"] = {"gemini": pane}
        calls: list[tuple] = []
        monkeypatch.setattr(
            orch_mod.QTimer, "singleShot", staticmethod(lambda ms, fn: calls.append((ms, fn)))
        )

        orch._auto_trust("gemini", project="P", max_ms=2_000)
        ticks = _drive_timer_queue(calls)

        assert ticks == 4  # 2_000 / 500

    def test_late_appearing_prompt_within_extended_window_still_gets_answered(
        self, orch, monkeypatch
    ) -> None:
        """The exact #186 scenario: the trust modal doesn't render until
        after the historical 30s window would have expired, but IS still
        inside a longer, provider-appropriate window — it must get a bare
        Enter, not be missed."""
        pane = _never_trusted_pane()
        # Trust prompt only appears from the 45th poll onward (~22.5s in) —
        # inside a 90s/500ms=180-tick window but past the old 60-tick one.
        calls_seen = {"n": 0}

        def _is_at_trust_prompt() -> bool:
            calls_seen["n"] += 1
            return calls_seen["n"] >= 45

        pane.session.is_at_trust_prompt.side_effect = _is_at_trust_prompt
        orch._panes_by_project["P"] = {"gemini": pane}
        calls: list[tuple] = []
        monkeypatch.setattr(
            orch_mod.QTimer, "singleShot", staticmethod(lambda ms, fn: calls.append((ms, fn)))
        )

        orch._auto_trust("gemini", project="P", max_ms=90_000)
        _drive_timer_queue(calls)

        # At least one Enter. Not exactly one: #330 made the poller keep
        # watching and re-press while the modal is STILL up, and this mock's
        # prompt never clears. One press followed by silence IS the bug.
        assert pane.session.write.call_args_list, "the late modal must get an Enter"
        assert all(c.args == ("\r",) for c in pane.session.write.call_args_list)

    def test_late_appearing_prompt_past_default_30s_window_is_missed(
        self, orch, monkeypatch
    ) -> None:
        """Same timing as above but with the OLD/default 30s window — proves
        the gap this fix closes: a modal appearing after ~22.5s already
        outlives a 60-tick/30s watcher in the general case (here it happens
        to still land in-window at tick 45 > 60? no — use a later tick to
        land outside 60 ticks) — pin a clearly-outside-default appearance."""
        pane = _never_trusted_pane()
        calls_seen = {"n": 0}

        def _is_at_trust_prompt() -> bool:
            calls_seen["n"] += 1
            return calls_seen["n"] >= 75  # ~37.5s in — past the 60-tick/30s default

        pane.session.is_at_trust_prompt.side_effect = _is_at_trust_prompt
        orch._panes_by_project["P"] = {"gemini": pane}
        calls: list[tuple] = []
        monkeypatch.setattr(
            orch_mod.QTimer, "singleShot", staticmethod(lambda ms, fn: calls.append((ms, fn)))
        )

        orch._auto_trust("gemini", project="P")  # default max_ms=30_000
        _drive_timer_queue(calls)

        pane.session.write.assert_not_called()  # watcher gave up before the modal ever showed


class TestAutoTrustKeepsWatchingUntilTheModalIsGone:
    """#330 — `_auto_trust` pressed Enter once and then `return`ed, ending the
    poll. A keypress that landed mid-render was simply lost, with nobody left
    watching: on a 3-pane fan-out two panes cleared and the third sat on the
    modal until a human noticed. Success is now defined as SEEING the modal
    disappear, not as having pressed a key."""

    def _pane_with_prompt(self, clears_after: int | None) -> MagicMock:
        """Trust modal is up from the first poll; it clears once
        `is_at_trust_prompt` has been asked `clears_after` times (None = it
        never clears, the stuck pane)."""
        p = MagicMock()
        p.session = MagicMock()
        p.session.is_alive = True
        p.session.write = MagicMock()
        asked = {"n": 0}

        def _at_prompt() -> bool:
            asked["n"] += 1
            return clears_after is None or asked["n"] < clears_after

        p.session.is_at_trust_prompt.side_effect = _at_prompt
        return p

    def test_a_swallowed_enter_is_retried(self, orch, monkeypatch) -> None:
        pane = self._pane_with_prompt(clears_after=None)
        orch._panes_by_project["P"] = {"frontend": pane}
        calls: list[tuple] = []
        monkeypatch.setattr(
            orch_mod.QTimer, "singleShot", staticmethod(lambda ms, fn: calls.append((ms, fn)))
        )

        with patch("agent_takkub.spawn_engine._log_event"):
            orch._auto_trust("frontend", project="P", max_ms=30_000)
            _drive_timer_queue(calls)

        assert pane.session.write.call_count > 1, "one press then silence is the #330 bug"

    def test_retries_are_capped(self, orch, monkeypatch) -> None:
        """`is_at_trust_prompt()` is a screen-scrape. A future TUI whose
        ordinary screen matched it must not take an Enter every 500ms forever."""
        from agent_takkub import spawn_engine

        pane = self._pane_with_prompt(clears_after=None)
        orch._panes_by_project["P"] = {"frontend": pane}
        calls: list[tuple] = []
        monkeypatch.setattr(
            orch_mod.QTimer, "singleShot", staticmethod(lambda ms, fn: calls.append((ms, fn)))
        )

        with patch("agent_takkub.spawn_engine._log_event"):
            orch._auto_trust("frontend", project="P", max_ms=120_000)
            _drive_timer_queue(calls)

        assert pane.session.write.call_count == spawn_engine._AUTO_TRUST_MAX_PRESSES

    def test_retries_are_spaced_out_not_hammered(self, orch, monkeypatch) -> None:
        """A second Enter 500ms after the first would arrive before the modal
        had a chance to act on the first one."""
        from agent_takkub import spawn_engine

        pane = self._pane_with_prompt(clears_after=None)
        orch._panes_by_project["P"] = {"frontend": pane}
        calls: list[tuple] = []
        monkeypatch.setattr(
            orch_mod.QTimer, "singleShot", staticmethod(lambda ms, fn: calls.append((ms, fn)))
        )

        # Window just big enough for two presses at the retry interval.
        window = spawn_engine._AUTO_TRUST_RETRY_EVERY_MS + 500
        with patch("agent_takkub.spawn_engine._log_event"):
            orch._auto_trust("frontend", project="P", max_ms=window)
            _drive_timer_queue(calls)

        assert pane.session.write.call_count == 2

    def test_polling_stops_as_soon_as_the_modal_clears(self, orch, monkeypatch) -> None:
        pane = self._pane_with_prompt(clears_after=2)
        orch._panes_by_project["P"] = {"frontend": pane}
        calls: list[tuple] = []
        monkeypatch.setattr(
            orch_mod.QTimer, "singleShot", staticmethod(lambda ms, fn: calls.append((ms, fn)))
        )

        with patch("agent_takkub.spawn_engine._log_event") as log_event:
            orch._auto_trust("frontend", project="P", max_ms=30_000)
            ticks = _drive_timer_queue(calls)

        assert ticks == 2, "must stop the moment the modal is gone, not keep polling to max_ms"
        assert any(c.args and c.args[0] == "auto_trust_cleared" for c in log_event.mock_calls)


class TestAutoTrustGiveUpIsAnnounced:
    """#330 item 2: the Lead's last word was "cockpit is auto-answering"
    (#186). When auto-answer gives up, nothing used to retract that — the pane
    read as `working` while it had never received a task at all."""

    def _stuck_pane(self) -> MagicMock:
        p = MagicMock()
        p.session = MagicMock()
        p.session.is_alive = True
        p.session.is_at_trust_prompt.return_value = True
        p.session.write = MagicMock()
        return p

    def _lead(self) -> MagicMock:
        p = MagicMock()
        p.session = MagicMock()
        p.session.is_alive = True
        p.session.write = MagicMock()
        return p

    def test_lead_is_told_the_pane_never_got_a_task(self, orch, monkeypatch) -> None:
        lead = self._lead()
        orch._panes_by_project["P"] = {"lead": lead, "frontend#3": self._stuck_pane()}
        calls: list[tuple] = []
        monkeypatch.setattr(
            orch_mod.QTimer, "singleShot", staticmethod(lambda ms, fn: calls.append((ms, fn)))
        )

        with patch("agent_takkub.spawn_engine._log_event"):
            orch._auto_trust("frontend#3", project="P", max_ms=2_000)
            _drive_timer_queue(calls)

        written = " ".join(
            c.args[0]
            for c in lead.session.write.call_args_list
            if c.args and isinstance(c.args[0], str)
        )
        assert "trust-prompt-stuck" in written
        assert "frontend#3" in written

    def test_no_warning_when_the_modal_cleared(self, orch, monkeypatch) -> None:
        lead = self._lead()
        pane = MagicMock()
        pane.session = MagicMock()
        pane.session.is_alive = True
        pane.session.write = MagicMock()
        asked = {"n": 0}

        def _at_prompt() -> bool:
            asked["n"] += 1
            return asked["n"] < 2

        pane.session.is_at_trust_prompt.side_effect = _at_prompt
        orch._panes_by_project["P"] = {"lead": lead, "frontend": pane}
        calls: list[tuple] = []
        monkeypatch.setattr(
            orch_mod.QTimer, "singleShot", staticmethod(lambda ms, fn: calls.append((ms, fn)))
        )

        with patch("agent_takkub.spawn_engine._log_event"):
            orch._auto_trust("frontend", project="P", max_ms=30_000)
            _drive_timer_queue(calls)

        assert not [c for c in lead.session.write.call_args_list if c.args]

    def test_a_pane_that_never_showed_the_modal_is_not_reported_stuck(
        self, orch, monkeypatch
    ) -> None:
        """Timing out having never seen a modal is the ordinary case for every
        provider that shows none — it must stay silent."""
        lead = self._lead()
        orch._panes_by_project["P"] = {"lead": lead, "backend": _never_trusted_pane()}
        calls: list[tuple] = []
        monkeypatch.setattr(
            orch_mod.QTimer, "singleShot", staticmethod(lambda ms, fn: calls.append((ms, fn)))
        )

        with patch("agent_takkub.spawn_engine._log_event"):
            orch._auto_trust("backend", project="P", max_ms=2_000)
            _drive_timer_queue(calls)

        assert not [c for c in lead.session.write.call_args_list if c.args]


class TestAutoTrustCursorOnNo:
    """Claude parks the cursor on "No, exit" when the folder pre-approves
    permissions via .claude/settings.local.json (freshly-imported project).
    A bare Enter there exits the CLI — the poller must arrow down first."""

    def test_arrows_down_before_enter_when_no_is_selected(self, orch, monkeypatch) -> None:
        pane = _never_trusted_pane()
        pane.session.is_at_trust_prompt.return_value = True
        selects_no = [True]
        pane.session.trust_prompt_selects_no.side_effect = lambda: selects_no[0]

        def _write(seq: str) -> None:
            if seq == "\x1b[B":
                selects_no[0] = False  # redraw now highlights "Yes"
            elif seq == "\r":
                pane.session.is_at_trust_prompt.return_value = False  # accepted

        pane.session.write.side_effect = _write
        orch._panes_by_project["P"] = {"lead": pane}
        calls: list[tuple] = []
        monkeypatch.setattr(
            orch_mod.QTimer, "singleShot", staticmethod(lambda ms, fn: calls.append((ms, fn)))
        )

        orch._auto_trust("lead", project="P")
        _drive_timer_queue(calls)

        seqs = [c.args[0] for c in pane.session.write.call_args_list]
        assert seqs == ["\x1b[B", "\r"]

    def test_enter_directly_when_yes_is_selected(self, orch, monkeypatch) -> None:
        pane = _never_trusted_pane()
        pane.session.is_at_trust_prompt.return_value = True
        pane.session.trust_prompt_selects_no.return_value = False
        pane.session.write.side_effect = lambda seq: setattr(
            pane.session.is_at_trust_prompt, "return_value", False
        )
        orch._panes_by_project["P"] = {"lead": pane}
        calls: list[tuple] = []
        monkeypatch.setattr(
            orch_mod.QTimer, "singleShot", staticmethod(lambda ms, fn: calls.append((ms, fn)))
        )

        orch._auto_trust("lead", project="P")
        _drive_timer_queue(calls)

        assert [c.args[0] for c in pane.session.write.call_args_list] == ["\r"]


class TestTrustPromptLateClear:
    """#330 item 2: once `_auto_trust` gives up, something must keep watching
    for the modal to clear later and resume the task that never got pasted —
    otherwise the pane sits forever with the Lead's last word being "never
    received a task", even after it reaches a genuinely idle ready prompt."""

    SAMPLE_TASK = "[ROLE: backend] implement /auth/logout\n\ntakkub done when done"

    def _still_stuck_pane(self) -> MagicMock:
        p = MagicMock()
        p.session = MagicMock()
        p.session.is_alive = True
        p.session.is_at_ready_prompt.return_value = False
        p.session.is_at_trust_prompt.return_value = True
        return p

    def test_never_started_for_lead(self, orch, monkeypatch) -> None:
        orch._panes_by_project["P"] = {"lead": self._still_stuck_pane()}
        calls: list[tuple] = []
        monkeypatch.setattr(
            orch_mod.QTimer, "singleShot", staticmethod(lambda ms, fn: calls.append((ms, fn)))
        )

        orch._watch_for_late_trust_clear("lead", project="P")

        assert calls == []

    def test_late_clear_resumes_the_cached_task(self, orch, monkeypatch) -> None:
        pane = MagicMock()
        pane.session = MagicMock()
        pane.session.is_alive = True
        polled = {"n": 0}

        def _ready() -> bool:
            polled["n"] += 1
            return polled["n"] >= 3  # clears on the 3rd late-clear poll

        pane.session.is_at_ready_prompt.side_effect = _ready
        pane.session.is_at_trust_prompt.return_value = False
        orch._panes_by_project["P"] = {"frontend": pane}
        orch._ps(_exit_key("P", "frontend")).last_assigned_task = self.SAMPLE_TASK
        calls: list[tuple] = []
        monkeypatch.setattr(
            orch_mod.QTimer, "singleShot", staticmethod(lambda ms, fn: calls.append((ms, fn)))
        )

        with (
            patch.object(orch, "_notify_lead") as mock_notify,
            patch.object(orch, "_send_when_ready") as mock_send,
            patch("agent_takkub.spawn_engine._log_event"),
        ):
            orch._watch_for_late_trust_clear("frontend", project="P")
            _drive_timer_queue(calls)

        mock_send.assert_called_once_with("frontend", self.SAMPLE_TASK, project="P")
        assert mock_notify.called

    def test_gives_up_silently_when_it_never_clears(self, orch, monkeypatch) -> None:
        pane = self._still_stuck_pane()
        orch._panes_by_project["P"] = {"frontend": pane}
        orch._ps(_exit_key("P", "frontend")).last_assigned_task = self.SAMPLE_TASK
        calls: list[tuple] = []
        monkeypatch.setattr(
            orch_mod.QTimer, "singleShot", staticmethod(lambda ms, fn: calls.append((ms, fn)))
        )

        with (
            patch.object(orch, "_notify_lead") as mock_notify,
            patch.object(orch, "_send_when_ready") as mock_send,
        ):
            orch._watch_for_late_trust_clear("frontend", project="P", max_ms=9_000)
            ticks = _drive_timer_queue(calls)

        assert ticks == 3  # 9_000 / 3_000
        mock_send.assert_not_called()
        mock_notify.assert_not_called()

    def test_stops_watching_once_the_pane_is_replaced(self, orch, monkeypatch) -> None:
        """A close()+respawn (or a fresh assign) replaces the AgentPane
        object entirely — a stale closure must not resume delivery against
        whatever now occupies that role slot."""
        original = self._still_stuck_pane()
        orch._panes_by_project["P"] = {"frontend": original}
        orch._ps(_exit_key("P", "frontend")).last_assigned_task = self.SAMPLE_TASK
        calls: list[tuple] = []
        monkeypatch.setattr(
            orch_mod.QTimer, "singleShot", staticmethod(lambda ms, fn: calls.append((ms, fn)))
        )

        with patch.object(orch, "_send_when_ready") as mock_send:
            orch._watch_for_late_trust_clear("frontend", project="P")
            # Swap in a fresh pane before the first scheduled tick runs.
            orch._panes_by_project["P"]["frontend"] = self._still_stuck_pane()
            _drive_timer_queue(calls)

        mock_send.assert_not_called()

    def test_resume_is_a_noop_without_a_cached_task(self, orch) -> None:
        orch._panes_by_project["P"] = {"frontend": MagicMock()}

        with (
            patch.object(orch, "_send_when_ready") as mock_send,
            patch("agent_takkub.spawn_engine._log_event"),
        ):
            orch._resume_blocked_delivery("frontend", "P")

        mock_send.assert_not_called()
