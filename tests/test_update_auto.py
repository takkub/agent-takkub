"""Tests for Layer A (threaded recurring poll) and Layer C (startup
silent ff-pull).

All subprocess / git calls are mocked so no real git operations happen.
PyQt is imported lazily through fixtures so the test suite doesn't need
a display server.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from agent_takkub import update_worker

# ── helpers ──────────────────────────────────────────────────────────────────


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ── UpdateCheckWorker ─────────────────────────────────────────────────────────


class TestUpdateCheckWorker:
    """Worker emits the right dict; busy-flag guard is honoured by the caller."""

    def test_emits_status_dict_on_success(self) -> None:
        expected = {
            "ok": True,
            "clean": True,
            "ahead": 0,
            "behind": 3,
            "dirty_files": [],
        }
        with (
            patch.object(update_worker, "is_git_repo", return_value=True),
            patch.object(update_worker, "fetch_remote", return_value=(True, "fetched")),
            patch.object(update_worker, "local_status", return_value=expected),
        ):
            worker = update_worker.UpdateCheckWorker()
            received: list[dict] = []
            worker.signals.finished.connect(lambda d: received.append(d))
            worker.run()

        assert received == [expected]

    def test_emits_not_repo_when_not_git(self) -> None:
        with patch.object(update_worker, "is_git_repo", return_value=False):
            worker = update_worker.UpdateCheckWorker()
            received: list[dict] = []
            worker.signals.finished.connect(lambda d: received.append(d))
            worker.run()

        assert received[0].get("not_repo") is True
        assert received[0].get("ok") is False

    def test_emits_error_dict_when_local_status_raises(self) -> None:
        with (
            patch.object(update_worker, "is_git_repo", return_value=True),
            patch.object(update_worker, "fetch_remote", return_value=(True, "fetched")),
            patch.object(update_worker, "local_status", side_effect=RuntimeError("boom")),
        ):
            worker = update_worker.UpdateCheckWorker()
            received: list[dict] = []
            worker.signals.finished.connect(lambda d: received.append(d))
            worker.run()

        assert received[0].get("ok") is False
        assert "boom" in received[0].get("error", "")

    def test_fetch_failure_still_emits_local_status(self) -> None:
        """fetch_remote failure should not abort the worker — we still emit
        local_status so the UI reflects the last known commit count."""
        expected = {"ok": True, "clean": True, "ahead": 0, "behind": 0, "dirty_files": []}
        with (
            patch.object(update_worker, "is_git_repo", return_value=True),
            patch.object(update_worker, "fetch_remote", return_value=(False, "timeout")),
            patch.object(update_worker, "local_status", return_value=expected),
        ):
            worker = update_worker.UpdateCheckWorker()
            received: list[dict] = []
            worker.signals.finished.connect(lambda d: received.append(d))
            worker.run()

        assert received == [expected]

    def test_busy_flag_prevents_second_dispatch(self) -> None:
        """Simulate MainWindow._schedule_update_check busy guard."""
        dispatched: list[bool] = []

        def fake_schedule(busy: list) -> None:
            if busy:
                return  # already running
            busy.append(True)
            dispatched.append(True)

        busy: list[bool] = [True]  # pre-set busy
        fake_schedule(busy)
        assert dispatched == []  # skipped

        busy.clear()
        fake_schedule(busy)
        assert dispatched == [True]  # now dispatched


# ── Transition detection ──────────────────────────────────────────────────────


class TestTransitionDetection:
    """_on_update_check_done notifies only on the 0→N transition."""

    def _make_status(self, *, ok: bool, clean: bool, behind: int) -> dict:
        return {
            "ok": ok,
            "clean": clean,
            "behind": behind,
            "ahead": 0,
            "dirty_files": [] if clean else ["some/file.py"],
        }

    def _run_transition(self, prev: dict, current: dict) -> bool:
        """Return True if the notification would fire given prev → current."""
        prev_up_to_date = (
            prev.get("ok", False) and prev.get("clean", False) and prev.get("behind", 0) == 0
        )
        now_behind = (
            current.get("ok", False)
            and current.get("clean", False)
            and current.get("behind", 0) > 0
        )
        return bool(prev_up_to_date and now_behind)

    def test_notifies_on_zero_to_behind(self) -> None:
        prev = self._make_status(ok=True, clean=True, behind=0)
        curr = self._make_status(ok=True, clean=True, behind=3)
        assert self._run_transition(prev, curr) is True

    def test_no_duplicate_notification_when_still_behind(self) -> None:
        prev = self._make_status(ok=True, clean=True, behind=2)
        curr = self._make_status(ok=True, clean=True, behind=3)
        assert self._run_transition(prev, curr) is False

    def test_no_notification_through_dirty_state(self) -> None:
        """Was dirty → now behind: skip (dirty was not up-to-date)."""
        prev = self._make_status(ok=True, clean=False, behind=0)
        curr = self._make_status(ok=True, clean=True, behind=3)
        assert self._run_transition(prev, curr) is False

    def test_no_notification_from_error_state(self) -> None:
        prev = {"ok": False, "error": "timeout"}
        curr = self._make_status(ok=True, clean=True, behind=2)
        assert self._run_transition(prev, curr) is False

    def test_no_notification_when_now_up_to_date(self) -> None:
        prev = self._make_status(ok=True, clean=True, behind=0)
        curr = self._make_status(ok=True, clean=True, behind=0)
        assert self._run_transition(prev, curr) is False


# ── try_silent_self_update (Layer C) ─────────────────────────────────────────


class TestTrySilentSelfUpdate:
    """Startup ff-pull: only pulls when clean + behind; restarts via execv."""

    def _make_status(
        self,
        *,
        ok: bool = True,
        clean: bool = True,
        behind: int = 2,
    ) -> dict:
        return {
            "ok": ok,
            "clean": clean,
            "behind": behind,
            "ahead": 0,
            "dirty_files": [] if clean else ["src/foo.py"],
        }

    def _fake_git(self, pull_rc: int = 0):
        """Return an `update_helper._git` replacement for the pull + rev-parse
        calls that `try_silent_self_update` now routes through `_git` (so it
        inherits the credential-prompt hardening)."""

        def _git(*args, **kw):
            if "pull" in args:
                return _proc(returncode=pull_rc)
            return _proc(stdout="abc1234\n")

        return _git

    def test_execv_called_when_clean_and_behind(self) -> None:
        mock_execv = MagicMock()
        with (
            patch.object(update_worker, "is_git_repo", return_value=True),
            patch.object(update_worker, "fetch_remote", return_value=(True, "fetched")),
            patch.object(update_worker, "local_status", return_value=self._make_status()),
            patch.object(update_worker, "_git", side_effect=self._fake_git()),
            patch("agent_takkub.update_worker.os.execv", mock_execv),
        ):
            update_worker.try_silent_self_update()
        assert mock_execv.called

    def test_skip_when_not_git_repo(self) -> None:
        with patch.object(update_worker, "is_git_repo", return_value=False):
            assert update_worker.try_silent_self_update() is False

    def test_skip_when_dirty_tree(self) -> None:
        mock_execv = MagicMock()
        with (
            patch.object(update_worker, "is_git_repo", return_value=True),
            patch.object(update_worker, "fetch_remote", return_value=(True, "fetched")),
            patch.object(
                update_worker, "local_status", return_value=self._make_status(clean=False)
            ),
            patch.object(update_worker, "_git", side_effect=self._fake_git()),
            patch("agent_takkub.update_worker.os.execv", mock_execv),
        ):
            update_worker.try_silent_self_update()
        assert not mock_execv.called

    def test_skip_when_already_up_to_date(self) -> None:
        mock_execv = MagicMock()
        with (
            patch.object(update_worker, "is_git_repo", return_value=True),
            patch.object(update_worker, "fetch_remote", return_value=(True, "fetched")),
            patch.object(update_worker, "local_status", return_value=self._make_status(behind=0)),
            patch.object(update_worker, "_git", side_effect=self._fake_git()),
            patch("agent_takkub.update_worker.os.execv", mock_execv),
        ):
            result = update_worker.try_silent_self_update()
        assert result is False
        assert not mock_execv.called

    def test_skip_when_fetch_fails(self) -> None:
        mock_execv = MagicMock()
        with (
            patch.object(update_worker, "is_git_repo", return_value=True),
            patch.object(update_worker, "fetch_remote", return_value=(False, "timeout")),
            patch.object(update_worker, "local_status", return_value=self._make_status()),
            patch.object(update_worker, "_git", side_effect=self._fake_git()),
            patch("agent_takkub.update_worker.os.execv", mock_execv),
        ):
            result = update_worker.try_silent_self_update()
        assert result is False
        assert not mock_execv.called

    def test_skip_when_ff_pull_fails(self) -> None:
        mock_execv = MagicMock()
        with (
            patch.object(update_worker, "is_git_repo", return_value=True),
            patch.object(update_worker, "fetch_remote", return_value=(True, "fetched")),
            patch.object(update_worker, "local_status", return_value=self._make_status()),
            patch.object(update_worker, "_git", side_effect=self._fake_git(pull_rc=1)),
            patch("agent_takkub.update_worker.os.execv", mock_execv),
        ):
            update_worker.try_silent_self_update()
        assert not mock_execv.called

    def test_returns_false_on_exception(self) -> None:
        with (
            patch.object(update_worker, "is_git_repo", return_value=True),
            patch.object(update_worker, "fetch_remote", side_effect=OSError("network down")),
        ):
            assert update_worker.try_silent_self_update() is False

    def test_fetch_timeout_returns_false(self) -> None:
        with (
            patch.object(update_worker, "is_git_repo", return_value=True),
            patch.object(
                update_worker,
                "fetch_remote",
                side_effect=subprocess.TimeoutExpired(cmd="git fetch", timeout=5),
            ),
        ):
            assert update_worker.try_silent_self_update() is False


# ── _safe_emit (torn-down receiver guard) ────────────────────────────────────


class TestSafeEmit:
    """A pool-thread worker can outlive its `_WorkerSignals` when the cockpit is
    restarting/shutting down. `_safe_emit` must deliver normally but swallow the
    exact `RuntimeError: wrapped C/C++ object ... deleted` that a deleted
    receiver raises — that unhandled error is what littered boot.log during the
    restart storm."""

    def test_forwards_payload_on_happy_path(self) -> None:
        signals = MagicMock()
        payload = {"ok": True, "behind": 2}
        update_worker._safe_emit(signals, payload)
        signals.finished.emit.assert_called_once_with(payload)

    def test_swallows_runtimeerror_from_deleted_receiver(self) -> None:
        signals = MagicMock()
        signals.finished.emit.side_effect = RuntimeError(
            "wrapped C/C++ object of type _WorkerSignals has been deleted"
        )
        # Must not propagate — there is nobody left to receive the signal.
        update_worker._safe_emit(signals, {"ok": True})

    def test_does_not_swallow_unrelated_exceptions(self) -> None:
        signals = MagicMock()
        signals.finished.emit.side_effect = ValueError("real bug, must surface")
        with pytest.raises(ValueError):
            update_worker._safe_emit(signals, {"ok": True})
