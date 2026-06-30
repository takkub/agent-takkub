"""Cross-platform PTY backend.

PtySession was written against ``pywinpty`` (ConPTY) and is Windows-only. This
module presents a single, minimal PtyProcess-like interface over two backends so
the rest of PtySession can stay backend-agnostic:

  - **Windows** → ``pywinpty`` (ConPTY backend, with a WinPTY fallback)
  - **macOS / Linux** → ``ptyprocess`` (the POSIX PTY layer that ``pexpect`` uses)

The two libraries are deliberately close (pywinpty's API was modelled on
ptyprocess/pexpect), so the wrapper only has to reconcile three differences:
  1. spawn takes an argv *list* (ptyprocess) vs a single cmdline *string* (winpty);
  2. ``read`` returns ``bytes`` (ptyprocess) vs ``str`` (winpty);
  3. ``write`` wants ``bytes`` (ptyprocess) vs ``str`` (winpty).

The wrapper normalises read→``bytes`` and accepts ``str`` *or* ``bytes`` on
``write``, so callers never have to care which backend is live. Everything else
(``isalive`` / ``terminate`` / ``setwinsize`` / ``pid`` / ``exitstatus``) already
has matching names on both libraries and is passed straight through.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence


class _BackendBase:
    def __init__(self, proc) -> None:
        self._proc = proc

    def isalive(self) -> bool:
        return self._proc.isalive()

    def terminate(self, force: bool = False) -> None:
        self._proc.terminate(force=force)

    def setwinsize(self, rows: int, cols: int) -> None:
        self._proc.setwinsize(rows, cols)

    @property
    def pid(self):
        return self._proc.pid

    @property
    def exitstatus(self):
        return self._proc.exitstatus


class _WinptyBackend(_BackendBase):
    """pywinpty (ConPTY) backend. ``read`` yields ``str``; normalise to bytes."""

    @classmethod
    def spawn(cls, argv: Sequence[str], cwd, env, rows: int, cols: int) -> _WinptyBackend:
        import winpty  # `pywinpty` package, imported module name is `winpty`

        cmd = subprocess.list2cmdline(list(argv))
        # Prefer ConPTY for lowest latency (sends ANSI directly instead of
        # scraping the screen buffer like WinPTY). Fall back if unavailable.
        try:
            proc = winpty.PtyProcess.spawn(
                cmd, dimensions=(rows, cols), cwd=cwd, env=env, backend=winpty.Backend.ConPTY
            )
        except Exception:
            proc = winpty.PtyProcess.spawn(cmd, dimensions=(rows, cols), cwd=cwd, env=env)
        return cls(proc)

    def read(self, size: int) -> bytes:
        data = self._proc.read(size)  # may raise EOFError — propagate to reader
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        return data

    def write(self, data: str | bytes):
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")  # pywinpty does its own encoding
        return self._proc.write(data)


class _PosixBackend(_BackendBase):
    """ptyprocess backend (macOS / Linux). ``read``/``write`` are already bytes."""

    @classmethod
    def spawn(cls, argv: Sequence[str], cwd, env, rows: int, cols: int) -> _PosixBackend:
        from ptyprocess import PtyProcess

        # ptyprocess execs argv[0] via PATH, so a bare `claude` (no `.exe`)
        # resolves correctly. env=None would break the child exec, so default
        # to the current environment.
        proc = PtyProcess.spawn(
            list(argv),
            cwd=cwd,
            env=env if env is not None else os.environ.copy(),
            dimensions=(rows, cols),
        )
        return cls(proc)

    def read(self, size: int) -> bytes:
        return self._proc.read(size)  # bytes; raises EOFError at EOF

    def write(self, data: str | bytes):
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        return self._proc.write(data)


def spawn_pty(
    argv: Sequence[str],
    cwd: str | None = None,
    env: dict | None = None,
    *,
    rows: int = 36,
    cols: int = 100,
):
    """Spawn ``argv`` in a PTY using the platform-appropriate backend and return
    a wrapper exposing read/write/isalive/terminate/setwinsize/pid/exitstatus."""
    # A non-existent cwd makes pywinpty's ConPTY backend hang *forever* — it
    # never returns and never raises, so the ConPTY→WinPTY except-fallback in
    # _WinptyBackend.spawn never fires either. Because PtySession.spawn() runs
    # synchronously on the Qt main thread, that freezes the whole cockpit GUI.
    # (Repro: delete a project's folder on disk, then reopen the cockpit — Lead
    # spawns into the now-missing lead_cwd and the window locks up.) ptyprocess
    # would raise on a bad cwd, but guard centrally so BOTH backends fail fast
    # and identically with a readable error that spawn()'s try/except turns into
    # a clean "spawn failed: working directory does not exist" message.
    if cwd is not None and not os.path.isdir(cwd):
        raise NotADirectoryError(f"working directory does not exist: {cwd!r}")
    if sys.platform == "win32":
        return _WinptyBackend.spawn(argv, cwd, env, rows, cols)
    return _PosixBackend.spawn(argv, cwd, env, rows, cols)
