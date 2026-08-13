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

from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator


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

        pane.session.write.assert_called_once_with("\r")

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
