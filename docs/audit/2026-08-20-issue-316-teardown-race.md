# Issue #316 — `pty-teardown` RuntimeError: deleted `_WriterThread`

**Status:** fixed. **File:** `src/agent_takkub/pty_session.py`

## Root cause

`PtySession._teardown_resources()` snapshots `self._writer` / `self._reader`
(both `_WriterThread` / `_ReaderThread`, `QThread` subclasses constructed
with `parent=self`, i.e. children of the `PtySession` `QObject`), then runs
the actual `quit()` + `wait(2000)` join either inline or — the common case —
on a detached `pty-teardown` daemon thread so the Qt main thread never
blocks on it.

That join is a race against Qt's own object-tree teardown. When the app
(or the tab/pane owning this session) shuts down, Qt can delete the C++
side of a QObject's children as part of tearing down the parent chain —
independently of whatever the Python-level `_teardown()` closure is doing
on its own background thread. If that C++ delete lands on `_writer` (or
`_reader`) between the snapshot and the `.quit()`/`.wait()` call, PyQt6's
wrapper raises:

```
RuntimeError: wrapped C/C++ object of type _WriterThread has been deleted
```

Because this runs on a raw `threading.Thread` (not inside Qt's own
exception-safe slot dispatch), the RuntimeError was previously unhandled —
it killed the `pty-teardown` thread with an uncaught exception, which the
auto-capture system filed as issue #316 (`pty_session.py:1456`).

This is the same object-lifetime hazard as `PyQt6 raises RuntimeError ...
for attribute access on a QObject built via __new__ without __init__`
already called out in `terminate()`'s docstring a few lines above — same
family of problem, different code path (background-thread Qt-object access
during shutdown, not attribute access on a bare `__new__`'d object).

## Fix

Wrap the `quit()` + `wait()` pair for both `_writer` and `_reader` in
`try/except RuntimeError: pass` — if the C++ object is already gone there
is nothing left to stop, so swallowing is correct (matches the existing
broad `except Exception: pass` guards a few lines below for
`thread_obj._proc = None` and `_transcript.close()` in the same function).
No behavior change on the non-racing path; every other read of session
attributes in `_teardown_resources` was already `AttributeError`/
`RuntimeError`-guarded — only this inner `_teardown()` closure's Qt calls
were not.

Platform: the guard is plain Python `try/except`, not gated on
`sys.platform` — applies identically on Windows (ConPTY) and macOS
(`_pty_backend`).

## Related-but-separate: spawn-time `TerminalWidget`/`QTimer` "has been deleted"

`events.log` around the same incident (2026-08-20 08:51:50–52) also shows:

```
spawn_native_failed role=shell err="RuntimeError: wrapped C/C++ object of type TerminalWidget has been deleted"
spawn_native_failed role=shell err="RuntimeError: wrapped C/C++ object of type QTimer has been deleted"
```

Traced to `spawn_engine.py`'s `_spawn_common`-style path (`session.spawn()`
→ `pane.attach_session(...)` → downstream `TerminalWidget`/`QTimer` access),
**not** `pty_session.py`. Same underlying family (a widget/timer's C++ side
deleted mid-flight, here because the pane/tab was closing while a spawn was
still in progress) — but this path is **already caught**: it sits inside
`_spawn_common`'s outer `except Exception as e:` block (spawn_engine.py
~1526-1537), which logs `spawn_native_failed` with the exception and
returns `(False, "failed to spawn ...")` instead of propagating. No crash,
no uncaught-exception capture, cockpit continues. Confirmed no code change
needed here — noting as a follow-up only in case a future refactor moves
that spawn logic outside the existing try/except and reintroduces an
unguarded path.

## Tests

`tests/test_pty_teardown_deleted_qthread.py` (new, mocks only — no real Qt
event loop):
- `test_teardown_survives_writer_quit_on_deleted_qthread` — both writer and
  reader raise `RuntimeError` from `quit()`/`wait()`; `_teardown_resources`
  must return without propagating.
- `test_teardown_survives_reader_quit_on_deleted_qthread` — only the reader
  is "deleted"; the healthy writer's `quit()`/`wait()` must still be called.
- `test_teardown_still_joins_healthy_threads` — no deletion race at all;
  both threads must still be joined exactly as before (behavior-neutral on
  the non-racing path).

Run: `PYTHONPATH=src <venv>/Scripts/python.exe -m pytest
tests/test_pty_teardown_deleted_qthread.py -q`
