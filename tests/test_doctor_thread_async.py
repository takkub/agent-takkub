"""Regression: the 🩺 Doctor button must run its checks OFF the Qt main thread.

`doctor.run_all_checks()` chains ~9 subprocess probes plus a `git fetch` (up to
~8 s on a slow network). It used to run directly in `_on_doctor_clicked`, which
blocked the Qt event loop for the whole duration and froze the cockpit — the
field "cockpit ดับ" was the user force-killing a UI wedged on that fetch
(boot.log main-thread stack: `_on_doctor_clicked → run_all_checks →
check_version → fetch_remote`). The work now happens in `_DoctorThread`.

These tests drive `_DoctorThread.run()` directly (same thread, no `start()`) so
the `ready` signal is delivered synchronously — enough to assert the off-thread
worker's contract without a live event loop. The session-scoped QApplication
(conftest) backs the signal machinery.
"""

from __future__ import annotations

from agent_takkub import doctor, user_actions


def test_doctor_thread_emits_findings(monkeypatch):
    sentinel = ["finding-a", "finding-b"]
    monkeypatch.setattr(doctor, "run_all_checks", lambda: sentinel)

    th = user_actions._DoctorThread()
    got: list = []
    th.ready.connect(got.append)
    th.run()  # synchronous — exercises the worker body without spawning a thread

    assert got == [sentinel]


def test_doctor_thread_runs_fixes_before_recheck(monkeypatch):
    calls: list = []
    monkeypatch.setattr(doctor, "run_auto_fixes", lambda findings: calls.append(("fix", findings)))
    monkeypatch.setattr(doctor, "run_all_checks", lambda: calls.append(("check",)) or [])

    th = user_actions._DoctorThread(apply_fixes_to=["dirty"])
    th.ready.connect(lambda _f: None)
    th.run()

    # Fixes apply first, then a fresh re-check — order matters.
    assert calls == [("fix", ["dirty"]), ("check",)]


def test_doctor_thread_surfaces_errors_not_blank(monkeypatch):
    def _boom():
        raise RuntimeError("git exploded")

    monkeypatch.setattr(doctor, "run_all_checks", _boom)

    th = user_actions._DoctorThread()
    got: list = []
    th.ready.connect(got.append)
    th.run()

    # Must NOT propagate (a raise inside the Qt slot would abort the process),
    # but must NOT blank the report either — a silent [] renders as a healthy
    # env, the opposite of the truth. Surface the crash as a single FAIL finding.
    assert len(got) == 1
    findings = got[0]
    assert len(findings) == 1
    assert findings[0].status == doctor.Status.FAIL
    assert "git exploded" in findings[0].detail


def test_no_fixes_when_apply_fixes_none(monkeypatch):
    calls: list = []
    monkeypatch.setattr(doctor, "run_auto_fixes", lambda findings: calls.append("SHOULD-NOT-RUN"))
    monkeypatch.setattr(doctor, "run_all_checks", lambda: [])

    th = user_actions._DoctorThread(apply_fixes_to=None)
    th.ready.connect(lambda _f: None)
    th.run()

    assert calls == []  # plain Doctor open never auto-fixes
