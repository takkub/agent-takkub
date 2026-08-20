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
import sys
import threading
from collections.abc import Sequence
from shutil import which


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

        # L1 (cross-platform audit 2026-07-10, repro'd on this machine): pass
        # argv as a LIST, never a pre-joined cmdline string. pywinpty's own
        # PtyProcess.spawn() only re-splits a *string* argv via
        # `shlex.split(argv, posix=False)` — which does NOT strip quote
        # characters — before doing its own `shutil.which(argv[0])`
        # existence check. If we pre-quote argv[0] ourselves (the old
        # `subprocess.list2cmdline(list(argv))` here), a spaced full path
        # like `"C:\Program Files\PowerShell\7\pwsh.EXE"` comes back out of
        # that re-split *still wearing its quote characters*, `which()` looks
        # for a file literally named with quotes in it, finds nothing, and
        # pywinpty raises `FileNotFoundError` before ConPTY ever starts.
        # Passing a list instead skips that string-reparsing branch
        # entirely — pywinpty takes `argv[0]` verbatim and quotes
        # `argv[1:]` itself via the same `list2cmdline` internally, so
        # quoting for the remaining args still happens, just exactly once.
        argv_list = list(argv)
        # Prefer ConPTY for lowest latency (sends ANSI directly instead of
        # scraping the screen buffer like WinPTY). Fall back if unavailable.
        try:
            proc = winpty.PtyProcess.spawn(
                argv_list,
                dimensions=(rows, cols),
                cwd=cwd,
                env=env,
                backend=winpty.Backend.ConPTY,
            )
        except Exception:
            proc = winpty.PtyProcess.spawn(argv_list, dimensions=(rows, cols), cwd=cwd, env=env)
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


class SpawnTargetCorrupt(OSError):
    """Raised when argv[0] resolves to a file whose header fails the
    pre-flight sanity check in :func:`_validate_spawn_target` — see that
    function's docstring for exactly what this does and doesn't catch.
    """


_POSIX_MAGIC_PREFIXES = (
    b"\x7fELF",  # ELF (Linux)
    b"#!",  # shebang script — exec'd via its interpreter, not loaded directly
    b"\xcf\xfa\xed\xfe",  # Mach-O 64-bit LE
    b"\xce\xfa\xed\xfe",  # Mach-O 32-bit LE
    b"\xfe\xed\xfa\xcf",  # Mach-O 64-bit BE
    b"\xfe\xed\xfa\xce",  # Mach-O 32-bit BE
    b"\xca\xfe\xba\xbe",  # Mach-O universal/fat binary
    b"\xbe\xba\xfe\xca",
)


def _looks_like_valid_executable(path: str) -> bool:
    """Best-effort magic-header sanity check on a *resolved* spawn target.

    Returns False ONLY when the file is a recognisable native-binary format
    for this platform (a Windows ``.exe``, or a POSIX file the OS would try
    to exec directly) whose header is missing/truncated/corrupt — the exact
    condition proven (issue #313, reproduced directly against this repo's
    pywinpty dependency: `docs/audit/2026-08-20-issue-313-spawn-deadlock.md`)
    to send the native pty constructor into a call that can hold the whole
    interpreter's GIL hostage for hours on Windows (a modal "Unsupported
    16-Bit Application" hard-error dialog for a mid-write PE, e.g. an npm
    self-update racing a spawn). Any other outcome (file missing/unreadable,
    a shim/script the OS hands off to an interpreter, or a header that
    parses fine) returns True — this exists to catch one narrow, *proven*
    failure mode, not to become a general launchability oracle that could
    false-positive on a binary format it doesn't recognise.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except OSError:
        return True  # can't read it — not the failure mode this guards against

    if sys.platform == "win32":
        if not path.lower().endswith(".exe"):
            return True  # .cmd/.bat/.ps1 shims aren't PE — nothing to check
        if len(head) < 64 or head[:2] != b"MZ":
            return False
        e_lfanew = int.from_bytes(head[60:64], "little")
        try:
            with open(path, "rb") as f:
                f.seek(e_lfanew)
                pe_sig = f.read(4)
        except OSError:
            return True
        return pe_sig == b"PE\x00\x00"

    # POSIX: only distrust a file the loader would actually exec directly —
    # an executable-bit-set regular file whose header matches none of the
    # recognised formats is the POSIX analogue of a truncated Windows PE.
    if not head or head.startswith(_POSIX_MAGIC_PREFIXES):
        return True
    try:
        executable_bit = bool(os.stat(path).st_mode & 0o111)
    except OSError:
        return True
    return not executable_bit


def _validate_spawn_target(argv0: str, env: dict | None) -> None:
    """Resolve argv[0] via PATH (same lookup pywinpty/ptyprocess do
    internally) and raise :class:`SpawnTargetCorrupt` if it fails the header
    check above. A no-op (never raises) when the target can't be resolved at
    all — the backend's own FileNotFoundError already covers that case with
    a proper error path, so this stays purely additive.
    """
    resolved = which(argv0, path=(env or os.environ).get("PATH"))
    if resolved is not None and not _looks_like_valid_executable(resolved):
        raise SpawnTargetCorrupt(
            f"spawn target failed pre-flight header check, refusing the "
            f"native constructor call (#313): {resolved!r}"
        )


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
    if argv:
        _validate_spawn_target(argv[0], env)
    if sys.platform == "win32":
        return _WinptyBackend.spawn(argv, cwd, env, rows, cols)
    return _PosixBackend.spawn(argv, cwd, env, rows, cols)


class PtySpawnTimeout(Exception):
    """Raised when the native PTY spawn call doesn't return within the bound.

    Both backends' underlying constructor (pywinpty on Windows, ptyprocess on
    macOS/Linux) is a blocking native call with no timeout of its own — a
    wedged one once blocked the Qt main thread (and the spawn-in-progress FIFO
    arbiter behind it, spawn_engine.py's ``_spawn_in_progress``) solid for
    47+ minutes with nothing able to recover (issue #139). Python cannot
    interrupt a blocking native call, so the worker thread that made it keeps
    running in the background after this is raised — see
    ``spawn_pty_bounded``, which tears down whatever process that worker
    eventually produces instead of leaking it to a caller that already gave up.
    """


def spawn_pty_bounded(
    argv: Sequence[str],
    cwd: str | None,
    env: dict | None,
    rows: int,
    cols: int,
    timeout_sec: float,
):
    """Run :func:`spawn_pty` on a worker thread, bounded by ``timeout_sec``.

    Returns the same wrapper ``spawn_pty`` would on success. Raises
    ``PtySpawnTimeout`` if the worker hasn't finished by the deadline —
    trading "block forever" for "fail fast" (#139). The worker thread itself
    is left running (daemon, so it never blocks interpreter exit); a `lock`-
    guarded handoff decides, whichever side finishes first, whether the
    caller still wants the result:
      - worker finishes first → hands the live process to the caller normally.
      - deadline hits first → caller raises and moves on; when/if the worker
        later completes, it finds itself abandoned and force-terminates the
        process it just spawned rather than leaving an unmanaged handle.
    """
    lock = threading.Lock()
    state: dict = {"proc": None, "error": None, "abandoned": False}

    def _run() -> None:
        try:
            proc = spawn_pty(argv, cwd=cwd, env=env, rows=rows, cols=cols)
        except BaseException as exc:  # must reach the caller's thread, not just Exception
            with lock:
                if not state["abandoned"]:
                    state["error"] = exc
            return
        with lock:
            if not state["abandoned"]:
                state["proc"] = proc
                return
        # The caller already gave up on us — nothing left to hand this
        # process to, so kill it instead of leaking a rogue PTY (#139).
        try:
            proc.terminate(force=True)
        except Exception:
            pass

    worker = threading.Thread(target=_run, name="pty-spawn", daemon=True)
    worker.start()
    worker.join(timeout_sec)
    with lock:
        if state["proc"] is not None:
            return state["proc"]
        if state["error"] is not None:
            raise state["error"]
        state["abandoned"] = True
    raise PtySpawnTimeout(
        f"native pty spawn exceeded {timeout_sec:g}s (argv[0]={argv[0] if argv else None!r})"
    )
