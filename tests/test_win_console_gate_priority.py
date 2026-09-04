"""`gate_popen_kwargs` — QA-gate child processes run at reduced OS scheduling
priority so a full gate no longer pegs the whole machine to 100% CPU/RAM
(#487). Platform is monkeypatched so both branches run on every OS in CI,
not just the one each branch targets natively.
"""

from __future__ import annotations

import subprocess

import pytest

from agent_takkub import _win_console


def test_windows_adds_below_normal_priority_and_no_window(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_win_console.sys, "platform", "win32")
    # getattr with a 0 fallback, not a bare attribute access: this test runs
    # on macos-latest/ubuntu-latest CI legs too (platform is monkeypatched,
    # not the real OS), and BELOW_NORMAL_PRIORITY_CLASS only exists as an
    # attribute on a Windows-built `subprocess` module.
    below_normal = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)

    kwargs = _win_console.gate_popen_kwargs()

    assert "preexec_fn" not in kwargs
    flags = kwargs["creationflags"]
    assert flags & _win_console.SUBPROCESS_NO_WINDOW == _win_console.SUBPROCESS_NO_WINDOW
    assert flags & below_normal == below_normal


def test_posix_uses_os_nice_via_preexec_fn(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_win_console.sys, "platform", "linux")

    kwargs = _win_console.gate_popen_kwargs()

    assert "creationflags" not in kwargs
    assert callable(kwargs["preexec_fn"])


def test_posix_preexec_fn_actually_calls_os_nice(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_win_console.sys, "platform", "darwin")
    calls: list[int] = []
    # raising=False: `os.nice` doesn't exist as a real attribute on a
    # Windows-built `os` module, and this test runs on the windows-latest CI
    # leg too (only `sys.platform` is monkeypatched above, not the real OS).
    monkeypatch.setattr("os.nice", lambda inc: calls.append(inc), raising=False)

    _win_console.gate_popen_kwargs()["preexec_fn"]()

    assert calls == [10]
