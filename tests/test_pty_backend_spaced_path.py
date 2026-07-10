"""L1 (cross-platform audit 2026-07-10) — a full binary path containing a
space (e.g. `C:\\Program Files\\PowerShell\\7\\pwsh.EXE`) must spawn
correctly through the Windows ConPTY backend.

Repro'd live on this machine before the fix: `_WinptyBackend.spawn` used to
pre-join argv into a single cmdline string via `subprocess.list2cmdline`,
quoting a spaced argv[0]. pywinpty's own `PtyProcess.spawn()` re-splits a
*string* argv via `shlex.split(argv, posix=False)`, which does NOT strip
quote characters — so the quoted path came back out still wearing its
quotes, `shutil.which()` couldn't find a file literally named with quotes
in it, and pywinpty raised `FileNotFoundError` before ConPTY ever started.
Passing argv as a *list* skips that re-split entirely (pywinpty takes
argv[0] verbatim), which is what these tests pin.
"""

from __future__ import annotations

import sys
from typing import ClassVar

import pytest

from agent_takkub import _pty_backend as backend

_SPACED_PATH = r"C:\Program Files\PowerShell\7\pwsh.EXE"


class _FakeProc:
    pid = 4242


class _FakePtyProcess:
    calls: ClassVar[list[dict]] = []

    @classmethod
    def spawn(cls, argv, dimensions=None, cwd=None, env=None, backend=None):
        cls.calls.append({"argv": argv, "cwd": cwd, "backend": backend})
        return _FakeProc()


class _FakeBackendEnum:
    ConPTY = object()


class _FakeWinpty:
    PtyProcess = _FakePtyProcess
    Backend = _FakeBackendEnum


@pytest.fixture
def fake_winpty(monkeypatch: pytest.MonkeyPatch):
    _FakePtyProcess.calls = []
    monkeypatch.setitem(sys.modules, "winpty", _FakeWinpty)
    monkeypatch.setattr(backend.sys, "platform", "win32")
    return _FakePtyProcess


class TestWinptyBackendPassesArgvAsList:
    def test_spawn_pty_passes_a_list_not_a_joined_string(self, fake_winpty) -> None:
        argv = [_SPACED_PATH, "-NoLogo", "-NoProfile"]
        backend.spawn_pty(argv, cwd=None)

        assert len(fake_winpty.calls) == 1
        sent_argv = fake_winpty.calls[0]["argv"]
        assert isinstance(sent_argv, list)
        # argv[0] must be the raw, unquoted path — not wrapped in "..." by a
        # pre-join step (that's exactly what broke pywinpty's own re-split).
        assert sent_argv[0] == _SPACED_PATH
        assert '"' not in sent_argv[0]
        assert sent_argv == argv

    def test_winpty_backend_spawn_directly(self, fake_winpty) -> None:
        result = backend._WinptyBackend.spawn(
            [_SPACED_PATH, "-NoLogo"], cwd=None, env=None, rows=24, cols=80
        )
        assert result.pid == 4242
        assert fake_winpty.calls[0]["argv"] == [_SPACED_PATH, "-NoLogo"]

    def test_falls_back_to_non_conpty_backend_on_exception_still_as_list(
        self, monkeypatch: pytest.MonkeyPatch, fake_winpty
    ) -> None:
        call_backends: list = []

        class _FlakyPtyProcess:
            calls_seen = 0

            @classmethod
            def spawn(cls, argv, dimensions=None, cwd=None, env=None, backend=None):
                call_backends.append(backend)
                cls.calls_seen += 1
                if cls.calls_seen == 1:
                    raise RuntimeError("ConPTY unavailable")
                return _FakeProc()

        class _FlakyWinpty:
            PtyProcess = _FlakyPtyProcess
            Backend = _FakeBackendEnum

        monkeypatch.setitem(sys.modules, "winpty", _FlakyWinpty)
        result = backend._WinptyBackend.spawn([_SPACED_PATH], cwd=None, env=None, rows=24, cols=80)
        assert result.pid == 4242
        assert len(call_backends) == 2
        assert call_backends[0] is _FakeBackendEnum.ConPTY
        assert call_backends[1] is None  # fallback call omits backend= entirely
