"""Tests for the throughput watchdog added to _check_stuck_panes (issue #35).

The watchdog flags panes that sustain > RUNAWAY_BYTES_S bytes/s for
RUNAWAY_DURATION_S seconds, injecting a warning into the Lead pane.

Strategy: pre-seed PaneState to simulate prior ticks rather than driving
multiple _check calls — this avoids rounding issues in the rate arithmetic.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent_takkub.orchestrator import (
    LEAD,
    RUNAWAY_BYTES_S,
    RUNAWAY_DURATION_S,
    RUNAWAY_WARN_COOLDOWN_S,
    Orchestrator,
    PaneState,
)


class _FakePane:
    def __init__(
        self,
        state: str = "working",
        last_out: float = 0.0,
        tp_total: int = 0,
        session_alive: bool = True,
        cwd: str = "/x",
    ) -> None:
        self.state = state
        self._last_output_ts = last_out
        self._tp_total_bytes = tp_total
        self._session_cwd = cwd
        if session_alive:
            sess = MagicMock()
            sess.is_alive = True
            sess.display_lines = MagicMock(return_value=["line"])
            self.session = sess
        else:
            self.session = None


class _FakeOrch:
    def __init__(self) -> None:
        self._panes_by_project: dict = {}
        self._pane_state: dict = {}
        self._idle_state: dict = {}
        self._recent_exits: dict = {}
        self.warn_calls: list[tuple[str, str, float]] = []
        self.recover_calls: list = []

    def _ps(self, key: str) -> PaneState:
        if key not in self._pane_state:
            self._pane_state[key] = PaneState()
        return self._pane_state[key]

    def close(self, role, project=None):
        return True, "ok"

    def spawn(self, role, cwd=None, project=None, **_kw):
        return True, "ok"

    def _send_when_ready(self, *a, **kw):
        pass

    def _auto_recover_stuck(self, role, project, pane, now):
        self.recover_calls.append((role, project, now))

    def _warn_lead_runaway_pane(self, role: str, project: str, rate: float) -> None:
        self.warn_calls.append((role, project, rate))

    def _check_shell_open_dialog(self, project_name, role, pane, key) -> None:
        pass  # no-op stub — #104 tripwire covered in test_stuck_recover.py


@pytest.fixture(autouse=True)
def _patch_qtimer(monkeypatch: pytest.MonkeyPatch):
    class _ShotCapture:
        @staticmethod
        def singleShot(ms, fn):
            fn()

    monkeypatch.setattr("agent_takkub.orchestrator.QTimer", _ShotCapture)


def _check(fake: _FakeOrch, now: float) -> None:
    Orchestrator._check_stuck_panes(fake, now)  # type: ignore[arg-type]


def _seed_throughput(
    fake: _FakeOrch,
    key: str,
    *,
    tp_last_total: int,
    tp_last_ts: float,
    tp_runaway_since: float | None = None,
    tp_warn_ts: float = 0.0,
    last_content_change_ts: float | None = None,
) -> PaneState:
    """Pre-populate PaneState so a single _check call exercises the watchdog."""
    ps = fake._ps(key)
    ps.tp_last_total = tp_last_total
    ps.tp_last_ts = tp_last_ts
    ps.tp_runaway_since = tp_runaway_since
    ps.tp_warn_ts = tp_warn_ts
    if last_content_change_ts is not None:
        ps.last_content_change_ts = last_content_change_ts
    return ps


class TestThroughputWatchdog:
    def test_no_warn_below_threshold(self) -> None:
        """Low throughput — no warning fired."""
        fake = _FakeOrch()
        now = 1_000_000.0
        # 10 KB/s — far below RUNAWAY_BYTES_S
        pane = _FakePane(last_out=now - 5, tp_total=100_000)
        fake._panes_by_project["p"] = {"backend": pane}

        _seed_throughput(
            fake, "p::backend", tp_last_total=0, tp_last_ts=now - 10, last_content_change_ts=now - 5
        )

        _check(fake, now)
        assert fake.warn_calls == []

    def test_no_warn_if_not_yet_sustained(self) -> None:
        """High rate but runaway_since < RUNAWAY_DURATION_S — no warn."""
        fake = _FakeOrch()
        now = 1_000_000.0
        # 1 MB/s, but runaway_since is only 5 s ago
        interval = 5.0
        pane = _FakePane(last_out=now - 1, tp_total=int(RUNAWAY_BYTES_S * 2 * interval))
        fake._panes_by_project["p"] = {"backend": pane}

        _seed_throughput(
            fake,
            "p::backend",
            tp_last_total=0,
            tp_last_ts=now - interval,
            tp_runaway_since=now - 5,  # only 5 s, < RUNAWAY_DURATION_S (60)
            last_content_change_ts=now - 3,
        )

        _check(fake, now)
        assert fake.warn_calls == []

    def test_warn_fires_after_sustained_overrate(self) -> None:
        """Sustained > RUNAWAY_BYTES_S for > RUNAWAY_DURATION_S → warn fires."""
        fake = _FakeOrch()
        now = 1_000_000.0
        interval = 10.0  # seconds since last tick
        # 2 MB/s sustained for > RUNAWAY_DURATION_S
        rate_2x = 2 * RUNAWAY_BYTES_S
        pane = _FakePane(
            last_out=now - 1,
            tp_total=int(rate_2x * interval),  # bytes since last tick
        )
        fake._panes_by_project["p"] = {"backend": pane}

        _seed_throughput(
            fake,
            "p::backend",
            tp_last_total=0,
            tp_last_ts=now - interval,
            # runaway started 62 s ago — past RUNAWAY_DURATION_S (60)
            tp_runaway_since=now - RUNAWAY_DURATION_S - 2,
            tp_warn_ts=0.0,
            last_content_change_ts=now - 3,
        )

        _check(fake, now)

        assert len(fake.warn_calls) == 1
        role, project, rate = fake.warn_calls[0]
        assert role == "backend"
        assert project == "p"
        assert rate > RUNAWAY_BYTES_S

    def test_warn_cooldown_suppresses_repeat(self) -> None:
        """Second warn within RUNAWAY_WARN_COOLDOWN_S is suppressed."""
        fake = _FakeOrch()
        now = 1_000_000.0
        interval = 10.0
        rate_2x = 2 * RUNAWAY_BYTES_S
        pane = _FakePane(last_out=now - 1, tp_total=int(rate_2x * interval))
        fake._panes_by_project["p"] = {"backend": pane}

        # First warn: tp_warn_ts=0 so it fires.
        _seed_throughput(
            fake,
            "p::backend",
            tp_last_total=0,
            tp_last_ts=now - interval,
            tp_runaway_since=now - RUNAWAY_DURATION_S - 2,
            tp_warn_ts=0.0,
            last_content_change_ts=now - 3,
        )
        _check(fake, now)
        assert len(fake.warn_calls) == 1

        # Second tick immediately after — within cooldown window.
        pane._tp_total_bytes += int(rate_2x * 2)
        _check(fake, now + 2)
        # No new warn because tp_warn_ts was just set.
        assert len(fake.warn_calls) == 1

    def test_warn_fires_again_after_cooldown(self) -> None:
        """Warn fires again once the cooldown period has elapsed."""
        fake = _FakeOrch()
        now = 1_000_000.0
        interval = 10.0
        rate_2x = 2 * RUNAWAY_BYTES_S
        pane = _FakePane(last_out=now - 1, tp_total=int(rate_2x * interval))
        fake._panes_by_project["p"] = {"backend": pane}

        # Pre-seed: warn was sent just over RUNAWAY_WARN_COOLDOWN_S ago.
        _seed_throughput(
            fake,
            "p::backend",
            tp_last_total=0,
            tp_last_ts=now - interval,
            tp_runaway_since=now - RUNAWAY_DURATION_S - 2,
            tp_warn_ts=now - RUNAWAY_WARN_COOLDOWN_S - 1,
            last_content_change_ts=now - 3,
        )

        _check(fake, now)
        assert len(fake.warn_calls) == 1

    def test_rate_clears_after_throughput_drops(self) -> None:
        """Once throughput drops below threshold, tp_runaway_since resets."""
        fake = _FakeOrch()
        now = 1_000_000.0
        interval = 10.0
        # 10 KB/s — well below threshold
        pane = _FakePane(last_out=now - 1, tp_total=10_000 * int(interval))
        fake._panes_by_project["p"] = {"backend": pane}

        _seed_throughput(
            fake,
            "p::backend",
            tp_last_total=0,
            tp_last_ts=now - interval,
            tp_runaway_since=now - 5,  # was flagged
            last_content_change_ts=now - 3,
        )

        _check(fake, now)

        key = "p::backend"
        assert fake._pane_state[key].tp_runaway_since is None

    def test_tp_snapshot_updated_each_tick(self) -> None:
        """tp_last_total and tp_last_ts are updated each time."""
        fake = _FakeOrch()
        now = 1_000_000.0
        pane = _FakePane(last_out=now - 5, tp_total=42)
        fake._panes_by_project["p"] = {"backend": pane}

        _seed_throughput(
            fake, "p::backend", tp_last_total=0, tp_last_ts=now - 10, last_content_change_ts=now - 5
        )

        _check(fake, now)

        key = "p::backend"
        ps = fake._pane_state[key]
        assert ps.tp_last_total == 42
        assert ps.tp_last_ts == now

    def test_lead_is_exempt(self) -> None:
        """Throughput watchdog must never fire for the Lead pane."""
        fake = _FakeOrch()
        now = 1_000_000.0
        # Even with absurd throughput, Lead should be skipped.
        pane = _FakePane(last_out=now - 5, tp_total=10**9)
        fake._panes_by_project["p"] = {LEAD.name: pane}

        _seed_throughput(
            fake,
            f"p::{LEAD.name}",
            tp_last_total=0,
            tp_last_ts=now - 10,
            tp_runaway_since=now - RUNAWAY_DURATION_S - 10,
        )

        _check(fake, now)
        assert fake.warn_calls == []
