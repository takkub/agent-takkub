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


@pytest.fixture
def isolated_restart_reason_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> pathlib.Path:
    """Redirect the module-level _RESTART_REASON_FILE (#232) to a tmp path so
    tests don't touch/clear the real cockpit's marker under `runtime/`."""
    target = tmp_path / "restart-reason.json"
    monkeypatch.setattr(orch_mod, "_RESTART_REASON_FILE", target)
    return target


@pytest.fixture
def ledger_open_roles(monkeypatch: pytest.MonkeyPatch) -> set[tuple[str, str]]:
    """#230: `restore_teammates` cross-checks task_ledger.load_state(project)
    before re-sending `last_task`. Tests that exercise the resend path (not
    the ledger-check itself) opt a (project, role) pair into this set so
    `load_state` reports it as still-open, matching the real "pane genuinely
    still working" case rather than hitting whatever real ledger happens to
    exist on the machine running the tests."""
    open_pairs: set[tuple[str, str]] = set()

    def _fake_load_state(project: str) -> dict:
        return {
            "groups": [],
            "open": {role: {} for (proj, role) in open_pairs if proj == project},
        }

    monkeypatch.setattr("agent_takkub.task_ledger.load_state", _fake_load_state)
    return open_pairs


class _FakeOrchestrator:
    """Stand-in for Orchestrator that only carries the state
    `restore_teammates` reads. The real class needs Qt to construct,
    so we drive the unbound method directly through this stub."""

    def __init__(self) -> None:
        self._recent_exits: dict[str, dict] = {}
        self.spawn_calls: list[tuple[str, str | None, str]] = []
        self._pending_done_notices: dict = {}
        self.send_when_ready_calls: list[tuple[str, str]] = []

    def spawn(self, role, cwd=None, project=None):
        self.spawn_calls.append((role, cwd, project))
        return True, "ok"

    def _save_pending_done_notices(self, project: str) -> None:
        pass

    def _send_when_ready(self, role: str, task: str, project: str | None = None) -> None:
        self.send_when_ready_calls.append((role, task))


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

    def test_restore_notice_carries_npm_update_reason(
        self,
        isolated_session_file: pathlib.Path,
        isolated_restart_reason_file: pathlib.Path,
        ledger_open_roles: set[tuple[str, str]],
    ) -> None:
        """Issue #232: the Lead-facing restore notice should say WHY the
        cockpit restarted, not just that it did."""
        ledger_open_roles.add(("p", "backend"))
        now = dt.datetime.now().isoformat(timespec="seconds")
        snap = {
            "saved_at": now,
            "projects": {"p": [{"role": "backend", "cwd": "/x", "last_task": "do X"}]},
        }
        isolated_session_file.write_text(json.dumps(snap), encoding="utf-8")
        isolated_restart_reason_file.write_text(
            json.dumps({"reason": "npm_update", "version": "1.2.3"}), encoding="utf-8"
        )
        fake = _FakeOrchestrator()
        assert _run_restore(fake) == 1
        body = fake._pending_done_notices["p"][0]["body"]
        assert "restarted to apply update v1.2.3" in body
        assert "re-sent automatically" in body
        # Marker is single-use — a second boot with no fresh marker must not
        # keep repeating a stale reason.
        assert not isolated_restart_reason_file.exists()

    def test_restore_notice_has_no_reason_suffix_when_marker_absent(
        self,
        isolated_session_file: pathlib.Path,
        isolated_restart_reason_file: pathlib.Path,
        ledger_open_roles: set[tuple[str, str]],
    ) -> None:
        ledger_open_roles.add(("p", "backend"))
        now = dt.datetime.now().isoformat(timespec="seconds")
        snap = {
            "saved_at": now,
            "projects": {"p": [{"role": "backend", "cwd": "/x", "last_task": "do X"}]},
        }
        isolated_session_file.write_text(json.dumps(snap), encoding="utf-8")
        assert not isolated_restart_reason_file.exists()
        fake = _FakeOrchestrator()
        assert _run_restore(fake) == 1
        body = fake._pending_done_notices["p"][0]["body"]
        assert (
            body
            == "[cockpit restart] backend pane restored from last session and last task re-sent automatically."
        )

    def test_skips_resend_when_task_ledger_has_no_open_row(
        self,
        isolated_session_file: pathlib.Path,
        ledger_open_roles: set[tuple[str, str]],
    ) -> None:
        """#230: a pane can finish (done() pops the ledger's open[role] row)
        between the snapshot write and an abrupt restart. Re-sending its
        last_task in that case would silently re-run already-completed work.
        `ledger_open_roles` is intentionally left empty — the role is not
        marked as still-open in the ledger."""
        now = dt.datetime.now().isoformat(timespec="seconds")
        snap = {
            "saved_at": now,
            "projects": {"p": [{"role": "backend", "cwd": "/x", "last_task": "do X"}]},
        }
        isolated_session_file.write_text(json.dumps(snap), encoding="utf-8")
        fake = _FakeOrchestrator()
        assert _run_restore(fake) == 1
        assert fake.send_when_ready_calls == []
        body = fake._pending_done_notices["p"][0]["body"]
        assert "NOT re-sent automatically" in body
        assert "task ledger" in body

    def test_resends_when_task_ledger_shows_role_still_open(
        self,
        isolated_session_file: pathlib.Path,
        ledger_open_roles: set[tuple[str, str]],
    ) -> None:
        ledger_open_roles.add(("p", "backend"))
        now = dt.datetime.now().isoformat(timespec="seconds")
        snap = {
            "saved_at": now,
            "projects": {"p": [{"role": "backend", "cwd": "/x", "last_task": "do X"}]},
        }
        isolated_session_file.write_text(json.dumps(snap), encoding="utf-8")
        fake = _FakeOrchestrator()
        assert _run_restore(fake) == 1
        assert fake.send_when_ready_calls == [("backend", "do X")]
        body = fake._pending_done_notices["p"][0]["body"]
        assert "re-sent automatically" in body

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
