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
        self._pane_state: dict = {}
        self.restore_parked_pane_result: dict | None = None
        self.restore_parked_pane_calls: list[tuple[str, str, str]] = []

    def spawn(self, role, cwd=None, project=None):
        self.spawn_calls.append((role, cwd, project))
        return True, "ok"

    def _ps(self, key):
        from agent_takkub.orchestrator import PaneState

        return self._pane_state.setdefault(key, PaneState())

    def _save_pending_done_notices(self, project: str) -> None:
        pass

    def _send_when_ready(self, role: str, task: str, project: str | None = None) -> None:
        self.send_when_ready_calls.append((role, task))

    def _restore_parked_pane(self, project: str, role: str, last_task: str) -> dict | None:
        """#495: stand-in for AutoResumeMixin's real method. Defaults to
        "no marker found" (None) so every pre-#495 test in this file keeps
        exercising the plain resend path unmodified; tests for the new
        behaviour set `restore_parked_pane_result` first."""
        self.restore_parked_pane_calls.append((project, role, last_task))
        return self.restore_parked_pane_result


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

    def test_park_restore_skip_resend_uses_its_own_notice(
        self,
        isolated_session_file: pathlib.Path,
        ledger_open_roles: set[tuple[str, str]],
    ) -> None:
        """#495: when `_restore_parked_pane` says the pane is still
        rate-limited (skip_resend=True), restore_teammates must NOT
        `_send_when_ready` the task immediately — the mixin already armed a
        delayed wake — and must surface *its* notice instead of the generic
        "re-sent automatically" text."""
        ledger_open_roles.add(("p", "backend"))
        now = dt.datetime.now().isoformat(timespec="seconds")
        snap = {
            "saved_at": now,
            "projects": {"p": [{"role": "backend", "cwd": "/x", "last_task": "do X"}]},
        }
        isolated_session_file.write_text(json.dumps(snap), encoding="utf-8")
        fake = _FakeOrchestrator()
        fake.restore_parked_pane_result = {
            "skip_resend": True,
            "notice": "🌙 still parked, wake re-armed",
        }
        assert _run_restore(fake) == 1
        assert fake.send_when_ready_calls == []
        assert fake.restore_parked_pane_calls == [("p", "backend", "do X")]
        body = fake._pending_done_notices["p"][0]["body"]
        assert body == "🌙 still parked, wake re-armed"

    def test_park_restore_elapsed_still_resends_with_its_notice(
        self,
        isolated_session_file: pathlib.Path,
        ledger_open_roles: set[tuple[str, str]],
    ) -> None:
        """Counterpart: skip_resend=False (quota already reset while cockpit
        was down, or the marker couldn't be trusted) must still resend the
        task as normal, but with the mixin's explanatory notice instead of
        the generic restore text."""
        ledger_open_roles.add(("p", "backend"))
        now = dt.datetime.now().isoformat(timespec="seconds")
        snap = {
            "saved_at": now,
            "projects": {"p": [{"role": "backend", "cwd": "/x", "last_task": "do X"}]},
        }
        isolated_session_file.write_text(json.dumps(snap), encoding="utf-8")
        fake = _FakeOrchestrator()
        fake.restore_parked_pane_result = {
            "skip_resend": False,
            "notice": "🌙 quota reset already, resending",
        }
        assert _run_restore(fake) == 1
        assert fake.send_when_ready_calls == [("backend", "do X")]
        body = fake._pending_done_notices["p"][0]["body"]
        assert body == "🌙 quota reset already, resending"

    def test_no_park_marker_keeps_generic_restore_notice(
        self,
        isolated_session_file: pathlib.Path,
        ledger_open_roles: set[tuple[str, str]],
    ) -> None:
        """`_restore_parked_pane` returning None (the common case — no park
        marker at all) must fall through to the pre-#495 generic notice."""
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


class TestRestoreTeammatesWorktreeBookkeeping:
    """#410: without this, a cockpit restart between assign(isolation=
    "worktree") and done() strands the pane's merge-proposal identity in
    memory only — the restored pane looks exactly like a fresh one with no
    assignment on record, and done() reports "ตรวจไม่ได้" with no proposal
    despite the branch really carrying commits."""

    def test_restores_worktree_dict_from_snapshot(
        self, isolated_session_file: pathlib.Path
    ) -> None:
        now = dt.datetime.now().isoformat(timespec="seconds")
        wt = {
            "path": "/wt/backend-1",
            "branch": "wt/backend-1",
            "base_sha": "abc123",
            "git_root": "/repo",
            "links": [],
            "port": 0,
        }
        snap = {
            "saved_at": now,
            "projects": {"p": [{"role": "backend", "cwd": "/wt/backend-1", "worktree": wt}]},
        }
        isolated_session_file.write_text(json.dumps(snap), encoding="utf-8")
        fake = _FakeOrchestrator()
        assert _run_restore(fake) == 1
        assert fake._pane_state["p::backend"].worktree == wt

    def test_restores_shared_tree_baseline_from_snapshot(
        self, isolated_session_file: pathlib.Path
    ) -> None:
        now = dt.datetime.now().isoformat(timespec="seconds")
        snap = {
            "saved_at": now,
            "projects": {
                "p": [
                    {
                        "role": "backend",
                        "cwd": "/repo/api",
                        "assign_base_sha": "deadbeef",
                        "assign_git_root": "/repo",
                        # Stored (and read back) as JSON lists — restore
                        # must turn them back into tuples so a later
                        # comparison against a fresh `dirty_snapshot()`
                        # read (which returns tuples) isn't always "changed"
                        # just from a list-vs-tuple type mismatch.
                        "assign_dirty_snapshot": {"a.py": ["M ", 111, 22]},
                    }
                ]
            },
        }
        isolated_session_file.write_text(json.dumps(snap), encoding="utf-8")
        fake = _FakeOrchestrator()
        assert _run_restore(fake) == 1
        ps = fake._pane_state["p::backend"]
        assert ps.assign_base_sha == "deadbeef"
        assert ps.assign_git_root == "/repo"
        assert ps.assign_dirty_snapshot == {"a.py": ("M ", 111, 22)}
        assert isinstance(ps.assign_dirty_snapshot["a.py"], tuple)

    def test_restores_non_git_classification_from_snapshot(
        self, isolated_session_file: pathlib.Path
    ) -> None:
        now = dt.datetime.now().isoformat(timespec="seconds")
        snap = {
            "saved_at": now,
            "projects": {
                "p": [
                    {
                        "role": "backend",
                        "cwd": "/not/a/repo",
                        "assign_non_git": True,
                    }
                ]
            },
        }
        isolated_session_file.write_text(json.dumps(snap), encoding="utf-8")
        fake = _FakeOrchestrator()
        assert _run_restore(fake) == 1
        assert fake._pane_state["p::backend"].assign_non_git is True

    def test_older_snapshot_without_bookkeeping_creates_no_pane_state(
        self, isolated_session_file: pathlib.Path
    ) -> None:
        # A snapshot written before #410 has no worktree/assign_* keys at
        # all — must not fabricate a spurious PaneState entry for it.
        now = dt.datetime.now().isoformat(timespec="seconds")
        snap = {
            "saved_at": now,
            "projects": {"p": [{"role": "backend", "cwd": "/x", "state": "active"}]},
        }
        isolated_session_file.write_text(json.dumps(snap), encoding="utf-8")
        fake = _FakeOrchestrator()
        assert _run_restore(fake) == 1
        assert fake._pane_state == {}


class TestMaybeWritePeriodicSnapshot:
    """#532: `write_session_snapshot()` used to run ONLY from the two
    graceful shutdown/restart call sites — a hard kill of an unresponsive
    cockpit (e.g. a pane wedged with a runaway child-process tree) skipped
    both, so `last_assigned_task` never reached disk and the next boot's
    restore_teammates() respawned the pane with no task to re-paste."""

    def test_writes_snapshot_on_first_call(self) -> None:
        from types import SimpleNamespace

        from agent_takkub.orchestrator import Orchestrator

        calls: list[None] = []
        fake = SimpleNamespace(write_session_snapshot=lambda: calls.append(None))
        Orchestrator._maybe_write_periodic_snapshot(fake, now=1000.0)  # type: ignore[arg-type]
        assert len(calls) == 1
        assert fake._last_periodic_snapshot_ts == 1000.0

    def test_throttles_within_interval(self) -> None:
        from types import SimpleNamespace

        from agent_takkub.orchestrator import Orchestrator

        calls: list[None] = []
        fake = SimpleNamespace(
            write_session_snapshot=lambda: calls.append(None),
            _last_periodic_snapshot_ts=1000.0,
        )
        Orchestrator._maybe_write_periodic_snapshot(fake, now=1010.0)  # type: ignore[arg-type]
        assert calls == []
        assert fake._last_periodic_snapshot_ts == 1000.0

    def test_fires_again_once_interval_elapses(self) -> None:
        from types import SimpleNamespace

        from agent_takkub.orchestrator import _PERIODIC_SNAPSHOT_INTERVAL_S, Orchestrator

        calls: list[None] = []
        fake = SimpleNamespace(
            write_session_snapshot=lambda: calls.append(None),
            _last_periodic_snapshot_ts=1000.0,
        )
        later = 1000.0 + _PERIODIC_SNAPSHOT_INTERVAL_S
        Orchestrator._maybe_write_periodic_snapshot(fake, now=later)  # type: ignore[arg-type]
        assert len(calls) == 1
        assert fake._last_periodic_snapshot_ts == later

    def test_swallows_write_errors(self) -> None:
        """A disk hiccup here must never break the watchdog tick that calls
        this — same best-effort contract as write_session_snapshot itself."""
        from types import SimpleNamespace

        from agent_takkub.orchestrator import Orchestrator

        def _boom() -> None:
            raise OSError("disk full")

        fake = SimpleNamespace(write_session_snapshot=_boom)
        Orchestrator._maybe_write_periodic_snapshot(fake, now=1000.0)  # type: ignore[arg-type]
        assert fake._last_periodic_snapshot_ts == 1000.0

    def test_end_to_end_persists_last_assigned_task_without_graceful_shutdown(
        self, isolated_session_file: pathlib.Path
    ) -> None:
        """The scenario from #532: a `working` pane with an assigned task,
        never closed gracefully — periodic snapshot must still land it on
        disk so a later `restore_teammates()` can re-paste it."""
        from types import SimpleNamespace

        from agent_takkub.orchestrator import Orchestrator, PaneState

        pane = SimpleNamespace(
            _session_cwd="/wt/frontend-1", state="working", session=SimpleNamespace(is_alive=True)
        )
        fake = SimpleNamespace(
            _panes_by_project={"p": {"frontend": pane}},
            _pane_state={"p::frontend": PaneState(last_assigned_task="verify checkout UI")},
        )
        fake.snapshot_state = lambda: Orchestrator.snapshot_state(fake)
        fake.write_session_snapshot = lambda: Orchestrator.write_session_snapshot(fake)
        Orchestrator._maybe_write_periodic_snapshot(fake, now=1000.0)  # type: ignore[arg-type]
        assert isolated_session_file.is_file()
        saved = json.loads(isolated_session_file.read_text(encoding="utf-8"))
        assert saved["projects"]["p"][0]["last_task"] == "verify checkout UI"


class TestSnapshotStateWorktreeBookkeeping:
    """#410's other half: snapshot_state() must actually persist the
    bookkeeping restore_teammates() now knows how to restore."""

    @staticmethod
    def _pane(cwd: str, state: str = "working"):
        from types import SimpleNamespace

        return SimpleNamespace(
            _session_cwd=cwd, state=state, session=SimpleNamespace(is_alive=True)
        )

    def test_includes_worktree_dict_for_an_isolated_pane(self) -> None:
        from types import SimpleNamespace

        from agent_takkub.orchestrator import Orchestrator, PaneState

        wt = {
            "path": "/wt/backend-1",
            "branch": "wt/backend-1",
            "base_sha": "abc",
            "git_root": "/repo",
            "links": [],
            "port": 0,
        }
        fake = SimpleNamespace(
            _panes_by_project={"p": {"backend": self._pane("/wt/backend-1")}},
            _pane_state={"p::backend": PaneState(worktree=wt, last_assigned_task="fix X")},
        )
        snap = Orchestrator.snapshot_state(fake)  # type: ignore[arg-type]
        entry = snap["projects"]["p"][0]
        assert entry["worktree"] == wt
        assert entry["assign_base_sha"] is None
        assert entry["assign_git_root"] is None
        assert entry["assign_dirty_snapshot"] is None

    def test_includes_shared_tree_baseline_and_stays_json_serialisable(self) -> None:
        from types import SimpleNamespace

        from agent_takkub.orchestrator import Orchestrator, PaneState

        fake = SimpleNamespace(
            _panes_by_project={"p": {"backend": self._pane("/repo/api", state="active")}},
            _pane_state={
                "p::backend": PaneState(
                    assign_base_sha="deadbeef",
                    assign_git_root="/repo",
                    assign_dirty_snapshot={"a.py": ("M ", 111, 22)},
                )
            },
        )
        snap = Orchestrator.snapshot_state(fake)  # type: ignore[arg-type]
        entry = snap["projects"]["p"][0]
        assert entry["assign_base_sha"] == "deadbeef"
        assert entry["assign_git_root"] == "/repo"
        assert entry["assign_dirty_snapshot"] == {"a.py": ["M ", 111, 22]}
        json.dumps(snap)  # must round-trip through JSON (tuples → lists)

    def test_includes_non_git_classification(self) -> None:
        # #560: a non-git project's classification must survive a cockpit
        # restart too — otherwise the one done() report landing right after
        # a restart falls back to the old nested "ตรวจไม่ได้ (ตรวจไม่ได้ ...)"
        # caveat again.
        from types import SimpleNamespace

        from agent_takkub.orchestrator import Orchestrator, PaneState

        fake = SimpleNamespace(
            _panes_by_project={"p": {"backend": self._pane("/not/a/repo", state="active")}},
            _pane_state={"p::backend": PaneState(assign_non_git=True)},
        )
        snap = Orchestrator.snapshot_state(fake)  # type: ignore[arg-type]
        entry = snap["projects"]["p"][0]
        assert entry["assign_non_git"] is True
        json.dumps(snap)

    def test_no_pane_state_entry_omits_bookkeeping_without_crashing(self) -> None:
        from types import SimpleNamespace

        from agent_takkub.orchestrator import Orchestrator

        fake = SimpleNamespace(
            _panes_by_project={"p": {"backend": self._pane("/repo/api")}},
            _pane_state={},
        )
        snap = Orchestrator.snapshot_state(fake)  # type: ignore[arg-type]
        entry = snap["projects"]["p"][0]
        assert entry["worktree"] is None
        assert entry["assign_dirty_snapshot"] is None
