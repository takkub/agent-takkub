"""#188 regression: app.py installs sys.excepthook at *import time*, so any
process that imports agent_takkub.app — including a pytest run that only
imports it transitively — routes its own unhandled exceptions into
auto_issue_capture.capture_cockpit_crash, which used to file a real GitHub
issue against the public takkub/agent-takkub repo (proven via
runtime/boot.log: pid=32640, pytest.console_main -> sys.stdout.flush() ->
OSError on a killed background pytest process).
"""

from __future__ import annotations

import sys

import agent_takkub.app as app_mod
from agent_takkub import auto_issue_capture as aic


class _SyncThread:
    """Runs target() inline instead of on a real thread, so a repro (if the
    guard were ever removed) would deterministically call issues.new_issue
    inside this test instead of racing a real background thread."""

    def __init__(self, target=None, args=(), name=None, daemon=None) -> None:
        self._target = target
        self._args = args

    def start(self) -> None:
        self._target(*self._args)


def _make_exc(cls: type[BaseException], msg: str = "boom"):
    try:
        raise cls(msg)
    except cls:
        return sys.exc_info()


def test_pytest_process_exception_never_files_an_issue(tmp_path, monkeypatch):
    """Fire a real exception through the *actual* sys.excepthook app.py
    installed (not a stand-in) and prove no GitHub issue gets filed and no
    background thread gets spawned. Re-installs the real hook first so this
    doesn't accidentally run against a stand-in left over from another test.
    """
    app_mod._install_exception_guard()
    assert sys.excepthook is not sys.__excepthook__, (
        "agent_takkub.app must install its own sys.excepthook"
    )

    # Defense in depth only — the guard under test must return before either
    # of these is ever touched; patched so a repro can't write real state.
    monkeypatch.setattr(aic, "_DEDUP_PATH", tmp_path / "auto_issue_dedup.json")
    new_issue_calls: list = []
    monkeypatch.setattr(
        aic.issues,
        "new_issue",
        lambda *a, **k: new_issue_calls.append((a, k)) or (1, "local://issue/1"),
    )
    spawn_calls: list = []

    def _tracking_spawn(*a, **k):
        spawn_calls.append((a, k))
        return _SyncThread(*a, **k)

    monkeypatch.setattr(aic, "_spawn", _tracking_spawn)

    et, ev, tb = _make_exc(OSError, "broken pipe")
    sys.excepthook(et, ev, tb)

    assert spawn_calls == [], (
        "#188: capture_cockpit_crash must no-op (never spawn a worker thread) "
        "for an exception raised inside a pytest process"
    )
    assert new_issue_calls == [], (
        "#188: a pytest-process exception must never file a real GitHub issue"
    )


def test_skip_env_zero_does_not_suppress(monkeypatch):
    """TAKKUB_SKIP_AUTO_ISSUE_CAPTURE must follow the same ""/"0" convention
    as its TAKKUB_SKIP_* siblings (TAKKUB_SKIP_ORPHAN_WORKTREE_PRUNE in
    disk_usage.py, TAKKUB_SKIP_GRAFT_BUILD in graft_autobuild.py,
    TAKKUB_SKIP_MCP_WARM in shared_dev_tools.py) — "=0" means the guard is
    OFF, not on. Strips the other three suppression signals (PYTEST_CURRENT_
    TEST, CI, "pytest" in sys.modules) so only the env var under test can
    suppress, then calls _auto_issue_suppressed() directly — this file has no
    autouse override of it, unlike test_auto_issue_capture.py.
    """
    monkeypatch.setenv("TAKKUB_SKIP_AUTO_ISSUE_CAPTURE", "0")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delitem(sys.modules, "pytest", raising=False)

    assert aic._auto_issue_suppressed() is False


def test_skip_env_nonzero_still_suppresses(monkeypatch):
    """Companion to the "=0" case above: any other value (the historical
    truthy-string convention, e.g. "1") must still suppress."""
    monkeypatch.setenv("TAKKUB_SKIP_AUTO_ISSUE_CAPTURE", "1")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delitem(sys.modules, "pytest", raising=False)

    assert aic._auto_issue_suppressed() is True


def test_set_macos_app_name_runs_safely(monkeypatch):
    # Safe on current platform (macOS executes ctypes, other platforms no-op)
    app_mod._set_macos_app_name("agent-takkub")

    # Non-darwin early exit
    monkeypatch.setattr(sys, "platform", "win32")
    app_mod._set_macos_app_name("agent-takkub")
