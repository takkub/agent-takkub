"""Tests for `Orchestrator.restore_teammates` and the snapshot helpers.

The full snapshot path needs a live PyQt6 + PtySession to exercise,
which is too heavyweight for a unit test. These tests focus on the
defensive branches in `restore_teammates` that must keep cockpit
boot safe: missing file, corrupt JSON, expired timestamp. The happy
path is covered by manual smoke-tests after the feature ships.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib

import pytest

from agent_takkub import orchestrator as orch_mod


@pytest.fixture
def isolated_session_file(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    """Redirect the module-level _LAST_SESSION_FILE to a tmp path so
    tests don't stomp the real cockpit snapshot under `runtime/`."""
    target = tmp_path / "last-session.json"
    monkeypatch.setattr(orch_mod, "_LAST_SESSION_FILE", target)
    return target


class _FakeOrchestrator:
    """Stand-in for Orchestrator that only carries the state
    `restore_teammates` reads. The real class needs Qt to construct,
    so we drive the unbound method directly through this stub."""

    def __init__(self) -> None:
        self._recent_exits: dict[str, dict] = {}
        self._session_uuids: dict[str, dict] = {}
        self.spawn_calls: list[tuple[str, str | None, str]] = []

    def spawn(self, role, cwd=None, project=None):
        self.spawn_calls.append((role, cwd, project))
        return True, "ok"


def _run_restore(fake: _FakeOrchestrator) -> int:
    """Call the unbound `restore_teammates` against the fake. Skips
    the Qt-heavy `Orchestrator.__init__`."""
    return orch_mod.Orchestrator.restore_teammates(fake)  # type: ignore[arg-type]


class TestRestoreTeammates:
    def test_returns_zero_when_file_missing(self, isolated_session_file: pathlib.Path) -> None:
        assert not isolated_session_file.exists()
        fake = _FakeOrchestrator()
        assert _run_restore(fake) == 0
        assert fake.spawn_calls == []

    def test_returns_zero_when_file_corrupt(self, isolated_session_file: pathlib.Path) -> None:
        isolated_session_file.write_text("{not valid", encoding="utf-8")
        fake = _FakeOrchestrator()
        assert _run_restore(fake) == 0
        assert fake.spawn_calls == []

    def test_returns_zero_when_timestamp_too_old(self, isolated_session_file: pathlib.Path) -> None:
        # `_LAST_SESSION_MAX_AGE_SEC` is one hour; offset by two hours so
        # the snapshot is decisively stale.
        old = dt.datetime.now() - dt.timedelta(hours=2)
        snap = {
            "saved_at": old.isoformat(timespec="seconds"),
            "projects": {"p": [{"role": "backend", "cwd": "/x", "state": "active"}]},
        }
        isolated_session_file.write_text(json.dumps(snap), encoding="utf-8")
        fake = _FakeOrchestrator()
        assert _run_restore(fake) == 0
        assert fake.spawn_calls == []

    def test_returns_zero_when_timestamp_missing(self, isolated_session_file: pathlib.Path) -> None:
        # A snapshot without `saved_at` can't have its age verified —
        # safer to skip than to assume "fresh".
        snap = {"projects": {"p": [{"role": "backend", "cwd": "/x", "state": "active"}]}}
        isolated_session_file.write_text(json.dumps(snap), encoding="utf-8")
        fake = _FakeOrchestrator()
        assert _run_restore(fake) == 0
        assert fake.spawn_calls == []

    def test_returns_zero_when_timestamp_unparseable(
        self, isolated_session_file: pathlib.Path
    ) -> None:
        snap = {
            "saved_at": "not-a-date",
            "projects": {"p": [{"role": "backend", "cwd": "/x", "state": "active"}]},
        }
        isolated_session_file.write_text(json.dumps(snap), encoding="utf-8")
        fake = _FakeOrchestrator()
        assert _run_restore(fake) == 0
        assert fake.spawn_calls == []

    def test_replays_fresh_snapshot_into_spawn_calls(
        self, isolated_session_file: pathlib.Path
    ) -> None:
        # A fresh snapshot with two teammates across two projects must
        # produce two spawn calls (project namespace preserved) and stamp
        # `_recent_exits` for crash-recovery bookkeeping.
        now = dt.datetime.now().isoformat(timespec="seconds")
        snap = {
            "saved_at": now,
            "projects": {
                "agent-takkub": [
                    {"role": "backend", "cwd": "C:/agent-takkub/api", "state": "working"}
                ],
                "line-websupport": [{"role": "frontend", "cwd": "C:/line/web", "state": "active"}],
            },
        }
        isolated_session_file.write_text(json.dumps(snap), encoding="utf-8")
        fake = _FakeOrchestrator()
        assert _run_restore(fake) == 2
        # spawn() called once per entry, project namespace propagated
        spawned = {(role, project) for role, _, project in fake.spawn_calls}
        assert spawned == {
            ("backend", "agent-takkub"),
            ("frontend", "line-websupport"),
        }
        # _recent_exits stamped for crash-recovery bookkeeping (project-scoped keys)
        assert "agent-takkub::backend" in fake._recent_exits
        assert "line-websupport::frontend" in fake._recent_exits

    def test_skips_entries_without_role(self, isolated_session_file: pathlib.Path) -> None:
        # Defensive: a malformed entry shouldn't blow up the whole restore.
        now = dt.datetime.now().isoformat(timespec="seconds")
        snap = {
            "saved_at": now,
            "projects": {
                "p": [
                    {"cwd": "/x", "state": "active"},  # no role
                    {"role": "backend", "cwd": "/x", "state": "active"},
                ]
            },
        }
        isolated_session_file.write_text(json.dumps(snap), encoding="utf-8")
        fake = _FakeOrchestrator()
        assert _run_restore(fake) == 1
        assert [c[0] for c in fake.spawn_calls] == ["backend"]
