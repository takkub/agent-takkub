"""2.1.17 — pane reuse after `done`, #664 assign-into-idle-pane, and the
provider substitute RING (no fixed favourite; spawn-failure hop).

Field facts these lock in (prod 2026-09-18): every teammate pane was closed
2.5 s after `done` and re-booted for the role's next task (24 fresh boots a
day, ~35k tokens each); a pane that finished with `progress` read busy
forever so new assigns queued behind it until `close` dropped them (#664);
and every degrade landed on claude because the picker walked a priority
list from its head.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import orchestrator as orch_mod
from agent_takkub import provider_config
from agent_takkub.orchestrator import Orchestrator, _exit_key

TEST_PROJECT = "reuseproj"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda project: project or TEST_PROJECT),
    )
    o = Orchestrator()
    o.shutdown_timers()
    return o


def _pane(state: str, cwd: str, *, at_prompt: bool, background: bool = False) -> MagicMock:
    pane = MagicMock()
    pane.state = state
    pane.session = MagicMock()
    pane.session.is_alive = True
    pane.session.is_at_ready_prompt_cached.return_value = at_prompt
    pane.session.has_background_work.return_value = background
    pane._session_cwd = cwd
    pane._transcript_path = None
    pane.set_state.side_effect = lambda s, **kw: setattr(pane, "state", s)
    return pane


# ── #664: assign into a pane that is really idle ────────────────────────────


class TestAssignIntoIdlePane:
    def test_working_pane_parked_at_prompt_gets_the_task_not_a_queue_slot(
        self, orch, tmp_path
    ) -> None:
        key = _exit_key(TEST_PROJECT, "devops")
        pane = _pane("working", str(tmp_path), at_prompt=True)
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["devops"] = pane
        ps = orch._ps(key)
        ps.last_assigned_task = "old task reported via progress"
        ps.task_delivered = True
        with (
            patch.object(orch, "spawn", return_value=(True, "devops already running")),
            patch.object(orch, "_send_when_ready") as send,
            patch.object(orch, "_notify_lead"),
        ):
            ok, msg = orch._assign_dispatch(
                "devops", str(tmp_path), "new task", project=TEST_PROJECT
            )
        assert ok, msg
        assert "queued after current task" not in msg
        assert not getattr(orch, "_pending_assignments", {}).get(key)
        send.assert_called_once()
        assert "new task" in send.call_args.args[1]
        assert "new task" in (orch._ps(key).last_assigned_task or "")

    def test_busy_pane_still_queues_and_lead_is_told_once(self, orch, tmp_path) -> None:
        key = _exit_key(TEST_PROJECT, "devops")
        pane = _pane("working", str(tmp_path), at_prompt=False)
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["devops"] = pane
        ps = orch._ps(key)
        ps.last_assigned_task = "still running"
        ps.task_delivered = True
        with patch.object(orch, "_notify_lead") as notify:
            ok, msg = orch._assign_dispatch(
                "devops", str(tmp_path), "queued one", project=TEST_PROJECT
            )
        assert ok and "queued" in msg
        assert [i["task"] for i in orch._pending_assignments[key]] == ["queued one"]
        kinds = [c.kwargs.get("kind") for c in notify.call_args_list]
        assert kinds.count("queued-assignment") == 1
        assert "takkub close --role devops" in notify.call_args.args[1]

    def test_background_work_at_prompt_is_not_idle(self, orch, tmp_path) -> None:
        pane = _pane("working", str(tmp_path), at_prompt=True, background=True)
        assert orch._pane_idle_at_prompt(pane) is False
        assert orch._pane_idle_for_reassign(pane) is False

    def test_done_pane_is_idle_for_reassign_even_without_prompt_read(self, orch, tmp_path) -> None:
        pane = _pane("done", str(tmp_path), at_prompt=False)
        assert orch._pane_idle_for_reassign(pane) is True


# ── pane reuse: done() keeps the pane ───────────────────────────────────────


class TestDoneKeepsPane:
    def _finish(self, orch, monkeypatch, tmp_path) -> tuple[list, str]:
        key = _exit_key(TEST_PROJECT, "backend")
        pane = _pane("working", str(tmp_path), at_prompt=True)
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane
        ps = orch._ps(key)
        ps.last_assigned_task = "first task"
        ps.task_id = "first-id"
        ps.task_delivered = True
        scheduled: list[tuple[int, object]] = []
        monkeypatch.setattr(
            "agent_takkub.orchestrator.QTimer.singleShot",
            lambda ms, cb: scheduled.append((ms, cb)),
        )
        assert orch.done("backend", note="first finished", project=TEST_PROJECT)[0]
        return scheduled, key

    def test_keep_mode_keeps_pane_alive_and_stamps_kept_since(self, orch, monkeypatch, tmp_path):
        # (#683) keep-alive is now the opt-out mode (TAKKUB_CLOSE_ON_DONE=0).
        monkeypatch.setattr(orch_mod, "CLOSE_ON_DONE", False)
        scheduled, key = self._finish(orch, monkeypatch, tmp_path)
        assert not any(ms == 2_500 for ms, _cb in scheduled)
        assert orch._ps(key).done_kept_since > 0
        assert orch._panes_by_project[TEST_PROJECT]["backend"].state == "done"

    def test_default_close_mode_schedules_auto_close(self, orch, monkeypatch, tmp_path):
        # (#683) close-on-done is the default: the 2.5 s close timer fires.
        monkeypatch.setattr(orch_mod, "CLOSE_ON_DONE", True)
        scheduled, key = self._finish(orch, monkeypatch, tmp_path)
        assert any(ms == 2_500 for ms, _cb in scheduled)
        assert orch._ps(key).done_kept_since == 0.0

    def test_close_on_done_defaults_on_when_env_unset(self, monkeypatch):
        # (#683 owner requirement) the shipped default must be close, not
        # keep — an opt-in close would never get used (#641 lesson).
        import os as _os

        monkeypatch.delenv("TAKKUB_CLOSE_ON_DONE", raising=False)
        assert _os.environ.get("TAKKUB_CLOSE_ON_DONE", "1").strip() == "1"
        from agent_takkub import agent_pane as ap_mod

        assert ap_mod._close_on_done_env() is True


class TestCloseArmsResumeWindow683:
    """#683: the done-driven close (preserve_resume=True) must leave behind
    exactly what spawn()'s auto-resume check needs — the popped PaneState's
    session uuid reseeded under the same key, plus a fresh `_recent_exits`
    stamp. A manual/user close (preserve_resume=False) must NOT, so the
    user's next assign cold-boots a genuinely fresh session."""

    UUID = "11111111-2222-3333-4444-555555555555"

    def test_preserve_resume_reseeds_uuid_and_stamps_recent_exit(self, orch, tmp_path):
        key = _exit_key(TEST_PROJECT, "backend")
        pane = _pane("done", str(tmp_path), at_prompt=True)
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane
        ps = orch._ps(key)
        ps.session_uuid = self.UUID
        ps.session_uuid_cwd = str(tmp_path)

        ok, _msg = orch.close("backend", project=TEST_PROJECT, preserve_resume=True)

        assert ok
        seeded = orch._pane_state.get(key)
        assert seeded is not None
        assert seeded.session_uuid == self.UUID
        assert seeded.session_uuid_cwd == str(tmp_path)
        exit_rec = orch._recent_exits.get(key)
        assert exit_rec is not None
        assert exit_rec["cwd"] == str(tmp_path)
        assert time.time() - exit_rec["ts"] < 5

    def test_manual_close_discards_uuid(self, orch, tmp_path):
        # A user/manual close (no preserve_resume) keeps the pre-#683
        # contract: uuid dies with the pop, next assign is fresh.
        key = _exit_key(TEST_PROJECT, "reviewer")
        pane = _pane("done", str(tmp_path), at_prompt=True)
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["reviewer"] = pane
        ps = orch._ps(key)
        ps.session_uuid = self.UUID
        ps.session_uuid_cwd = str(tmp_path)

        ok, _msg = orch.close("reviewer", project=TEST_PROJECT)

        assert ok
        assert key not in orch._recent_exits
        seeded = orch._pane_state.get(key)
        assert seeded is None or seeded.session_uuid is None

    def test_preserve_resume_without_uuid_leaves_no_resume_state(self, orch, tmp_path):
        key = _exit_key(TEST_PROJECT, "qa")
        pane = _pane("done", str(tmp_path), at_prompt=True)
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["qa"] = pane
        orch._ps(key)  # no uuid ever recorded

        ok, _msg = orch.close("qa", project=TEST_PROJECT, preserve_resume=True)

        assert ok
        assert key not in orch._recent_exits
        seeded = orch._pane_state.get(key)
        assert seeded is None or seeded.session_uuid is None


class TestReapDonePanes:
    def test_closes_kept_pane_after_ttl(self, orch, monkeypatch, tmp_path):
        monkeypatch.setattr(orch_mod, "DONE_PANE_TTL_S", 100.0)
        key = _exit_key(TEST_PROJECT, "qa")
        pane = _pane("done", str(tmp_path), at_prompt=True)
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["qa"] = pane
        now = time.time()
        orch._ps(key).done_kept_since = now - 101
        with patch.object(orch, "close", return_value=(True, "closed")) as close:
            orch._reap_done_panes(now)
        close.assert_called_once()
        assert close.call_args.args[0] == "qa"
        assert orch._ps(key).done_kept_since == 0.0

    def test_leaves_recent_queued_and_working_panes_alone(self, orch, monkeypatch, tmp_path):
        monkeypatch.setattr(orch_mod, "DONE_PANE_TTL_S", 100.0)
        now = time.time()
        panes = orch._panes_by_project.setdefault(TEST_PROJECT, {})
        panes["recent"] = _pane("done", str(tmp_path), at_prompt=True)
        orch._ps(_exit_key(TEST_PROJECT, "recent")).done_kept_since = now - 5
        panes["queued"] = _pane("done", str(tmp_path), at_prompt=True)
        orch._ps(_exit_key(TEST_PROJECT, "queued")).done_kept_since = now - 500
        orch._pending_assignments = {_exit_key(TEST_PROJECT, "queued"): [{"task": "x"}]}
        panes["busy"] = _pane("working", str(tmp_path), at_prompt=False)
        orch._ps(_exit_key(TEST_PROJECT, "busy")).done_kept_since = now - 500
        with patch.object(orch, "close") as close:
            orch._reap_done_panes(now)
        close.assert_not_called()


class TestCwdSwitchOnReuse:
    def test_idle_pane_in_old_worktree_is_closed_and_respawned(self, orch, monkeypatch, tmp_path):
        monkeypatch.setattr(orch_mod, "CLOSE_ON_DONE", False)
        old = tmp_path / "wt-old"
        new = tmp_path / "wt-new"
        old.mkdir()
        new.mkdir()
        pane = _pane("done", str(old), at_prompt=True)
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["frontend"] = pane
        scheduled: list[tuple[int, object]] = []
        monkeypatch.setattr(
            "agent_takkub.orchestrator.QTimer.singleShot",
            lambda ms, cb: scheduled.append((ms, cb)),
        )
        with (
            patch.object(orch, "close", return_value=(True, "closed")) as close,
            patch.object(orch, "_notify_lead") as notify,
            patch.object(orch, "_send_when_ready") as send,
        ):
            ok, msg = orch._assign_dispatch(
                "frontend",
                str(new),
                "task in new wt",
                project=TEST_PROJECT,
                worktree={"path": str(new), "branch": "wt/frontend-2"},
            )
        assert ok and "respawning in" in msg
        close.assert_called_once()
        assert close.call_args.kwargs.get("keep_queue") is True
        assert any(ms == 2_000 for ms, _cb in scheduled)
        send.assert_not_called()
        assert any("wt-new" in c.args[1] for c in notify.call_args_list)

    def test_same_cwd_idle_pane_is_simply_reused(self, orch, monkeypatch, tmp_path):
        monkeypatch.setattr(orch_mod, "CLOSE_ON_DONE", False)
        pane = _pane("done", str(tmp_path), at_prompt=True)
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["frontend"] = pane
        with (
            patch.object(orch, "close") as close,
            patch.object(orch, "spawn", return_value=(True, "frontend already running")),
            patch.object(orch, "_send_when_ready") as send,
            patch.object(orch, "_notify_lead"),
        ):
            ok, _ = orch._assign_dispatch("frontend", str(tmp_path), "again", project=TEST_PROJECT)
        assert ok
        close.assert_not_called()
        send.assert_called_once()

    def test_busy_pane_in_other_cwd_refuses_instead_of_pasting_into_wrong_checkout(
        self, orch, tmp_path
    ):
        old = tmp_path / "a"
        new = tmp_path / "b"
        old.mkdir()
        new.mkdir()
        pane = _pane("working", str(old), at_prompt=False)
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane
        with patch.object(orch, "_send_when_ready") as send:
            ok, msg = orch._assign_dispatch(
                "backend",
                str(new),
                "x",
                project=TEST_PROJECT,
                worktree={"path": str(new), "branch": "wt/backend-2"},
            )
        assert ok is False
        assert "#162" in msg
        send.assert_not_called()

    def test_plain_reassign_with_other_cwd_still_pastes_into_running_pane(self, orch, tmp_path):
        old = tmp_path / "a"
        new = tmp_path / "b"
        old.mkdir()
        new.mkdir()
        pane = _pane("working", str(old), at_prompt=False)
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane
        with (
            patch.object(orch, "spawn", return_value=(True, "backend already running")),
            patch.object(orch, "_send_when_ready") as send,
            patch.object(orch, "_notify_lead"),
        ):
            ok, _ = orch._assign_dispatch("backend", str(new), "x", project=TEST_PROJECT)
        assert ok
        send.assert_called_once()

    def test_bare_mock_pane_is_not_idle(self, orch):
        # #162 guard tests register a bare MagicMock as "some live pane" —
        # unknown state/aliveness must never read as idle.
        assert orch._pane_idle_for_reassign(MagicMock()) is False

    def test_worktree_collision_guard_ignores_idle_pane(self, orch, tmp_path):
        pane = _pane("done", str(tmp_path), at_prompt=True)
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane
        assert orch._worktree_bare_role_collision("backend", TEST_PROJECT) is None
        pane.state = "working"
        pane.session.is_at_ready_prompt_cached.return_value = False
        assert orch._worktree_bare_role_collision("backend", TEST_PROJECT) is not None


# ── provider ring ───────────────────────────────────────────────────────────


class TestProviderRing:
    def test_walk_starts_after_the_hit_provider(self, monkeypatch):
        monkeypatch.setattr(provider_config, "_provider_available", lambda p: True)
        assert provider_config.pick_substitute_provider({"codex"}, after="codex") == "gemini"
        assert provider_config.pick_substitute_provider({"claude"}, after="claude") == "codex"
        assert provider_config.pick_substitute_provider({"gemini"}, after="gemini") == "kimi"

    def test_wraps_around_the_ring(self, monkeypatch):
        monkeypatch.setattr(provider_config, "_provider_available", lambda p: True)
        assert provider_config.pick_substitute_provider({"cursor"}, after="cursor") == "claude"

    def test_only_enabled_installed_providers_count(self, monkeypatch):
        monkeypatch.setattr(
            provider_config, "_provider_available", lambda p: p in ("codex", "opencode")
        )
        # claude is NOT installed on this machine — it must never be picked.
        assert provider_config.pick_substitute_provider({"codex"}, after="codex") == "opencode"
        assert provider_config.pick_substitute_provider({"opencode"}, after="opencode") == "codex"
        monkeypatch.setattr(provider_config, "_provider_available", lambda p: p == "codex")
        assert provider_config.pick_substitute_provider({"codex"}, after="codex") is None

    def test_quota_hit_candidates_are_skipped(self, monkeypatch):
        from agent_takkub import provider_state

        monkeypatch.setattr(provider_config, "_provider_available", lambda p: True)
        provider_state.set_quota_reset_at("gemini", time.time() + 3600)
        assert provider_config.pick_substitute_provider({"codex"}, after="codex") == "kimi"

    def test_mid_task_reroute_uses_the_ring_too(self, orch, monkeypatch):
        monkeypatch.setattr(provider_config, "_provider_available", lambda p: True)
        ps = orch._ps("proj::backend")
        assert orch._pick_reroute_provider("proj", "backend", ps, "codex") == "gemini"
        assert orch._pick_reroute_provider("proj", "backend", ps, "claude") == "codex"


class TestSpawnFailureHop:
    def test_hop_picks_next_ring_provider_once_and_tells_lead(self, orch, monkeypatch):
        monkeypatch.setattr(provider_config, "_provider_available", lambda p: True)
        ps = orch._ps(_exit_key(TEST_PROJECT, "backend"))
        with patch.object(orch, "_notify_lead") as notify:
            first = orch._spawn_failure_provider_hop("backend", TEST_PROJECT, "codex", "boom", ps)
            second = orch._spawn_failure_provider_hop("backend", TEST_PROJECT, "gemini", "boom", ps)
        assert first == "gemini"
        assert second is None
        assert ps.spawn_provider_hops == 1
        assert notify.call_args.kwargs.get("kind") == "spawn-provider-hop"

    def test_lead_and_forced_identity_roles_never_hop(self, orch, monkeypatch):
        monkeypatch.setattr(provider_config, "_provider_available", lambda p: True)
        with patch.object(orch, "_notify_lead"):
            assert (
                orch._spawn_failure_provider_hop(
                    "lead", TEST_PROJECT, "claude", "x", orch._ps(_exit_key(TEST_PROJECT, "lead"))
                )
                is None
            )
            assert (
                orch._spawn_failure_provider_hop(
                    "codex", TEST_PROJECT, "codex", "x", orch._ps(_exit_key(TEST_PROJECT, "codex"))
                )
                is None
            )

    def test_assign_re_runs_on_the_substitute_when_spawn_fails(self, orch, monkeypatch, tmp_path):
        monkeypatch.setattr(provider_config, "_provider_available", lambda p: True)
        scheduled: list[tuple[int, object]] = []
        monkeypatch.setattr(
            "agent_takkub.orchestrator.QTimer.singleShot",
            lambda ms, cb: scheduled.append((ms, cb)),
        )
        key = _exit_key(TEST_PROJECT, "backend")
        with (
            patch.object(orch, "spawn", return_value=(False, "PtySpawnTimeout: native spawn hung")),
            patch.object(orch, "_notify_lead") as notify,
            patch.object(orch, "_warn_lead_spawn_failed") as warn,
        ):
            ok, msg = orch._assign_dispatch("backend", str(tmp_path), "task", project=TEST_PROJECT)
        assert ok and "retrying on codex" in msg
        assert orch._ps(key).spawn_provider_hops == 1
        warn.assert_not_called()
        assert any(c.kwargs.get("kind") == "spawn-provider-hop" for c in notify.call_args_list)
        assert any(ms == 1_500 for ms, _cb in scheduled)
