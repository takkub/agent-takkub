"""Regression tests for issue #64: spawn_still_blocked log dedup.

Before the fix, _retry_deferred_spawn() logged 'spawn_still_blocked' on every
50ms retry, producing hundreds of identical entries per block episode. After the
fix it logs once on first block, then once every SPAWN_BLOCK_WARN_AFTER_S.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent_takkub.spawn_engine import SPAWN_BLOCK_WARN_AFTER_S, SpawnEngineMixin


class _FakeEngine:
    """Minimal SpawnEngineMixin-compatible stub for _retry_deferred_spawn tests."""

    def __init__(self, blocked: bool = True) -> None:
        self._spawn_deferred: set = set()
        self._blocked = blocked
        self.logged_events: list[tuple[str, dict]] = []

    def _is_spawn_blocked(self) -> bool:
        return self._blocked

    def _resolve_project(self, project):
        return project or "proj"

    def _preserve_pending_spawn_initial_task(self, role_name, project_ns):
        pass

    def _project_panes(self, project_ns):
        # Return an empty dict so pane is None → early return on "gone" path
        # is suppressed by returning a pane stub.
        return {"backend": _PaneStub()}

    def spawn(self, role, cwd=None, project=None, **_kw):
        pass


class _PaneStub:
    """AgentPane-minimal stub: alive session (doesn't trigger pane_gone early exit)."""

    def __init__(self) -> None:
        self.session = MagicMock()
        self.session.is_alive = False  # not alive → won't short-circuit as "already alive"


@pytest.fixture()
def _captured_log(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    """Capture _log_event calls from spawn_engine module."""
    captured: list[tuple[str, dict]] = []

    def _fake_log(event: str, **kwargs) -> None:
        captured.append((event, kwargs))

    monkeypatch.setattr("agent_takkub.spawn_engine._log_event", _fake_log)
    return captured


@pytest.fixture()
def _no_qtimer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swallow QTimer.singleShot so the retry loop doesn't recurse infinitely."""

    class _NullTimer:
        @staticmethod
        def singleShot(ms, fn):
            pass  # don't fire — test controls calls explicitly

    monkeypatch.setattr("agent_takkub.spawn_engine.QTimer", _NullTimer)


def _retry(engine: _FakeEngine, ts: float | None = None) -> None:
    """Call _retry_deferred_spawn on the fake engine via the real mixin method."""
    if ts is not None:
        with patch("agent_takkub.spawn_engine.time") as mock_time:
            mock_time.time.return_value = ts
            SpawnEngineMixin._retry_deferred_spawn(  # type: ignore[arg-type]
                engine, "backend", None, "proj", False, 0
            )
    else:
        SpawnEngineMixin._retry_deferred_spawn(  # type: ignore[arg-type]
            engine, "backend", None, "proj", False, 0
        )


class TestSpawnStillBlockedLogDedup:
    def test_first_block_logs_once(self, _captured_log: list, _no_qtimer: None) -> None:
        engine = _FakeEngine(blocked=True)
        _retry(engine, ts=1_000.0)
        blocked_logs = [e for e, _ in _captured_log if e == "spawn_still_blocked"]
        assert len(blocked_logs) == 1

    def test_second_call_within_warn_window_no_extra_log(
        self, _captured_log: list, _no_qtimer: None
    ) -> None:
        engine = _FakeEngine(blocked=True)
        _retry(engine, ts=1_000.0)
        # Second retry just 1s later — within SPAWN_BLOCK_WARN_AFTER_S
        _retry(engine, ts=1_001.0)
        blocked_logs = [e for e, _ in _captured_log if e == "spawn_still_blocked"]
        assert len(blocked_logs) == 1

    def test_log_again_after_warn_window(self, _captured_log: list, _no_qtimer: None) -> None:
        engine = _FakeEngine(blocked=True)
        t0 = 1_000.0
        _retry(engine, ts=t0)
        # Advance past SPAWN_BLOCK_WARN_AFTER_S
        _retry(engine, ts=t0 + SPAWN_BLOCK_WARN_AFTER_S + 0.1)
        blocked_logs = [e for e, _ in _captured_log if e == "spawn_still_blocked"]
        assert len(blocked_logs) == 2

    def test_new_episode_logs_again(self, _captured_log: list, _no_qtimer: None) -> None:
        engine = _FakeEngine(blocked=True)
        t0 = 1_000.0
        _retry(engine, ts=t0)

        # Simulate gate clearing: remove key from _spawn_blocked_first_ts
        bts = getattr(engine, "_spawn_blocked_first_ts", {})
        bts.pop("proj::backend", None)

        # New block episode — should log again
        _retry(engine, ts=t0 + 60.0)
        blocked_logs = [e for e, _ in _captured_log if e == "spawn_still_blocked"]
        assert len(blocked_logs) == 2

    def test_gate_clear_removes_episode_key(
        self, _captured_log: list, _no_qtimer: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When gate clears, _spawn_blocked_first_ts entry is removed."""
        engine = _FakeEngine(blocked=True)
        _retry(engine, ts=1_000.0)
        assert "proj::backend" in getattr(engine, "_spawn_blocked_first_ts", {})

        # Now gate is clear
        engine._blocked = False
        _retry(engine, ts=1_001.0)
        assert "proj::backend" not in getattr(engine, "_spawn_blocked_first_ts", {})
