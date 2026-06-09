# ConPTY COM crash cross-check

Date: 2026-06-09  
Scope: `pty_session.py`, orchestrator spawn paths, `app.py` watchdog  
Constraint: keep ConPTY and keep spawn on the Qt main thread

## Verdict

**`QApplication.activeModalWidget() is None` is a reasonable tactical
mitigation for the observed `QWizard.exec()` case, but it is not a sufficient
or principled fix for `RPC_E_CANTCALLOUT_ININPUTSYNCCALL`.**

The exception is about the current Windows thread dispatching an
input-synchronous `SendMessage`, not about whether Qt currently has a modal
widget. Those states overlap, but neither implies the other. The proposed gate
will remove an important, easily reproduced trigger while leaving non-modal
input-sync contexts exposed.

Recommended direction without off-thread spawn:

1. Put every spawn source behind one FIFO spawn arbiter.
2. Before the native `winpty.PtyProcess.spawn(..., Backend.ConPTY)` call, gate
   on all three:
   - `QApplication.activeModalWidget() is None`
   - `QApplication.activePopupWidget() is None`
   - Win32 `InSendMessageEx(NULL)` does not report a blocked `ISMEX_SEND`
3. After the gates first become clear, wait one posted event-loop turn (or a
   short 25-50 ms quiet period) and re-check before spawning.
4. Serialize spawns so only one ConPTY construction is in progress.

The Win32 check is the important correctness check. The Qt modal/popup checks
are conservative UX gates that avoid starting a GIL-holding native operation
inside nested UI interactions even when `InSendMessageEx` happens to be clear
at one instant.

## What the current paths actually do

- `PtySession.spawn()` constructs ConPTY synchronously on the Qt main thread at
  `pty_session.py:226` through pywinpty 3.0.3.
- `Orchestrator.spawn()` has four direct `session.spawn()` branches: shell,
  Gemini, Codex, and Claude. The gate must cover all branches, preferably once
  above this duplication rather than being copied four times.
- CLI `spawn`/`assign` is already acknowledged first and dispatched by
  `QTimer.singleShot`; pipeline roles and auto-respawn also use timers.
  A timer/queued callback changes the Python call stack but does **not**
  guarantee that the thread is outside a nested native or Qt event loop.
- `_send_when_ready()` does not spawn a PTY. It polls and writes the task/Enter
  after spawn. Its timers can run in Qt nested event loops, but it is not itself
  the ConPTY construction path.
- Boot Lead spawn is invoked synchronously from `MainWindow._boot()`.
  Presets are timer-staggered. Snapshot restore is not staggered internally:
  `restore_teammates()` loops and calls `spawn()` synchronously for every saved
  pane.
- The Python watchdog cannot rescue the known failure reliably because the
  native winpty call holds the GIL. The watchdog remains useful for unrelated
  main-thread stalls, but it is not a safety net for this crash.

## 1. Is modal-defer enough?

### It covers the main reported trigger

For a Qt `QWizard.exec()`, `QDialog.exec()`, and ordinary modal
`QMessageBox`, `activeModalWidget()` should remain non-null while the nested Qt
event loop is active. Retrying via `QTimer` until it becomes null should prevent
spawn from entering during those dialogs.

This is worth shipping as defense in depth because the application has many
`dlg.exec()` sites and timers for preset spawn, snapshot restore, pipeline
spawn, auto-respawn, and stuck recovery.

### It misses real input-sync contexts

1. **Qt popups and menus.** `QMenu.exec()` is represented by
   `activePopupWidget()`, not `activeModalWidget()`. The tab context menu is one
   concrete in-repo example.
2. **Drag/drop loops.** The terminal WebEngine view accepts drag/drop. OLE drag
   handling can run a nested native loop without a Qt modal widget.
3. **Window move/resize and system menus.** Windows enters native modal loops
   for sizing, moving, and some menu interactions. There need not be an active
   Qt modal widget.
4. **WebEngine/OLE/IME/accessibility/focus dispatch.** A cross-thread or
   cross-process `SendMessage` can enter a Qt/Chromium window procedure during
   an otherwise ordinary UI state. This is closest to the RCA's original
   WebEngine/input-dispatch hypothesis and has no modal-widget marker.
5. **Nested loops from code other than `QDialog.exec()`.** A local event loop or
   native API may dispatch the queued timer while `activeModalWidget()` is null.
6. **Race after the check.** A plain check followed immediately by spawn has a
   small check-to-call window. Since both happen on one GUI thread, ordinary
   posted events cannot interleave, but re-entrancy/native hooks inside work
   done between the check and ConPTY construction can still change context.
   Keep the checked section minimal and place it immediately before the native
   call.

### Native `QFileDialog` is a different edge

Qt documents that the native Windows file dialog runs a blocking modal event
loop that does not dispatch `QTimer`s. Therefore a timer-based deferred spawn
will normally wait until the native dialog closes even if
`activeModalWidget()` does not expose the native dialog as a QWidget.

That makes native `QFileDialog` less likely to bypass this specific timer gate,
not proof that `activeModalWidget()` is complete. A spawn already executing
before the dialog starts is unaffected, and other native loops do dispatch
messages differently.

## 2. Alternatives without moving spawn off-thread

### Best option: Win32 context gate plus a serialized queue

Use `InSendMessageEx(NULL)` on Windows immediately before ConPTY creation.
Microsoft's documented blocked-sender test is:

```text
(flags & (ISMEX_REPLIED | ISMEX_SEND)) == ISMEX_SEND
```

When true, do not call ConPTY; re-post the head request and return to the
current Windows dispatch. This checks the condition represented by
`RPC_E_CANTCALLOUT_ININPUTSYNCCALL` directly instead of using modal UI as a
proxy.

This should be combined with modal and popup checks, because "technically legal
right now" is still a poor time to block the main thread while a user is in a
menu or dialog.

Important design constraint: do not make existing `Orchestrator.spawn()` return
`(True, "queued")` unless all callers are updated for an explicit queued state.
Several paths assume `True` means the session exists now:

- `assign()` immediately starts `_send_when_ready()`.
- auto-respawn immediately reads `last_spawn_resumed` and decides whether to
  replay the task.
- pipeline code records `pipeline_run_id`, adds the role to `spawned_ok`, and
  may finalize the hop.
- snapshot restore increments its restored count and queues notices/tasks.

The least disruptive implementation is an arbiter above these workflows whose
callback invokes the existing synchronous `spawn()` only when safe. If the gate
must live inside `spawn()`, introduce a real `queued/spawning` lifecycle and
completion signal rather than pretending the process already exists.

### `QMetaObject.invokeMethod(..., QueuedConnection)`

Useful as a mechanism for posting into the arbiter, but **not a fix by itself**.
Qt guarantees queued delivery when the receiver's event loop processes the
event; a modal nested event loop is still an event loop and may process it.
`QTimer.singleShot(0)` already demonstrates the same limitation.

It becomes useful when the invoked slot performs the Win32/Qt gates, returns
without spawning when unsafe, and re-posts itself.

### `CoInitializeEx` mode changes

Not recommended.

- The Qt GUI thread is already an OLE/COM consumer and is normally STA-like.
  `OleInitialize` uses apartment threading.
- Reinitializing the same thread with an incompatible mode returns
  `RPC_E_CHANGED_MODE`; it does not convert an existing apartment.
- MTA does not make it valid to issue arbitrary COM/OLE operations from a
  Qt GUI thread while Qt/WebEngine expects its established apartment.
- Changing apartment mode early enough to take effect risks clipboard,
  drag/drop, file dialogs, WebEngine, accessibility, and other OLE behavior.

This is a process-wide integration experiment with a large blast radius, not a
targeted mitigation for one unsafe call site.

### `pythoncom.PumpWaitingMessages`

Not recommended here.

`PumpWaitingMessages` exists to provide a message pump for a COM thread. The Qt
main thread already has a message pump. Pumping again before/during spawn adds
re-entrancy and may dispatch more sent messages; it does not end the
input-synchronous call currently on the stack. During an input-synchronized COM
call, Microsoft explicitly warns against yielding control.

### pywinpty flags / timeout

The installed pywinpty 3.0.3 high-level `PtyProcess.spawn()` exposes only
`backend` in addition to argv/cwd/env/dimensions. The lower-level `PTY`
constructor has `timeout` and `agent_config`, but its documentation says these
options apply only to the WinPTY backend. There is no exposed ConPTY flag that
changes COM call-out behavior or makes construction asynchronous.

Reducing a timeout might reduce the duration of a bad wedge if the native path
honors it, but it does not make the call legal and is not documented for
ConPTY. Treat it as unsupported, not a fix.

### `ReplyMessage`

Win32 documents `ReplyMessage` as a way for a window procedure to release a
blocked `SendMessage` sender before yielding. It should not be injected from
application Python here: Qt owns the relevant window procedures and message
semantics. Calling it generically could acknowledge a message before Qt or
WebEngine has completed its contract. Detection with `InSendMessageEx` is safe;
altering Qt's dispatch is not.

## 3. Regression risks of modal defer

### Starvation and apparent "stuck spawn"

- A wizard/dialog/menu left open can defer auto-respawn indefinitely.
- A continuously refreshed retry timer can create needless wakeups and logs.
- If the modal closes and another opens immediately, retries may starve for a
  long time.

Use one pending retry timer for the queue head, exponential or capped polling
(for example 25, 50, 100, then 200 ms), and visible structured logging such as
`spawn_deferred` / `spawn_defer_resumed`. Do not use a timeout that eventually
forces an unsafe spawn. A long deferral can surface a non-fatal status message.

### Duplicate requests

If deferral occurs after `paneRequested`, state clearing, UUID selection, or
pane creation, another assign/respawn can observe "pane exists but no live
session" and enqueue a duplicate. Queue by `(project, role)` and attach
generation/request IDs. Coalesce compatible duplicate spawn requests; preserve
the newest task metadata deliberately rather than accidentally.

### Ordering

Independent timers currently imply approximate time ordering, not one global
order. A central queue changes this to deterministic FIFO, which is preferable,
but priority must be explicit:

- Lead boot should normally precede teammate restore/presets.
- manual user/Lead assign may deserve priority over background restore.
- auto-respawn should not jump ahead of a requested pipeline role indefinitely.

Do not let separate CLI, pipeline, restore, and respawn retry loops compete;
that recreates ordering races outside the gate.

### Resume-window expiry

`RESUME_WINDOW_SEC` is five minutes and is evaluated when the actual
`spawn()` runs. A long modal can push auto-respawn beyond that window, causing a
fresh session rather than `--resume`. The subsequent task-replay behavior then
changes. This is safer than crashing, but should be logged and tested.

### Pipeline bookkeeping

The pipeline currently finalizes after the last scheduled `_spawn_one` callback
returns. If `_spawn_one` reports queued-success before the process exists, the
hop can be announced/finalized prematurely. Completion must mean actual
`session.spawn()` success or failure.

### Snapshot/boot restore

`restore_teammates()` currently performs multiple synchronous spawns in a loop.
It is both an ordering hotspot and a regression hotspot for an async modal
gate. Restored count/notices/task replay must follow actual completion, not
queue admission. A global arbiter also subsumes the current per-path staggering
and avoids back-to-back restore spawns.

### Cancellation and teardown

Queued requests must be cancelled when:

- the pane/project/tab is closed;
- the role is manually closed before spawn begins;
- the pipeline run is closed or advances;
- the application starts shutdown/restart;
- a newer generation for the same `(project, role)` supersedes the request.

Without cancellation, closing a modal could unexpectedly resurrect panes that
the user closed while the modal was open.

## Suggested acceptance tests

1. Open `ConfigWizard.exec()`, schedule manual assign, preset, pipeline spawn,
   and auto-respawn; verify none calls pywinpty until the wizard closes.
2. Repeat with `QMenu.exec()` and verify popup gating.
3. Mock `InSendMessageEx` as blocked with no active modal/popup; verify repeated
   retries and exactly one eventual native spawn.
4. Queue several roles from CLI, pipeline, and snapshot restore; verify actual
   spawn order, serialization, and no duplicate `(project, role)`.
5. Close a pane/project/pipeline while its request is deferred; verify it never
   spawns after the modal closes.
6. Defer auto-respawn beyond five minutes; verify fresh-session/replay behavior
   is explicit and logged.
7. Make native spawn fail after deferral; verify pipeline/restore/respawn
   bookkeeping sees failure only at actual completion.
8. Live Windows stress: hold menus, drag files over WebEngine panes, move/resize
   the main window, and generate parallel assigns while recording
   `InSendMessageEx` flags and spawn outcomes.

## Bottom line

Ship modal defer only if it is described as a narrow guard for known nested Qt
dialogs. For a robust main-thread ConPTY fix, use the actual Win32
input-synchronous-state detector and centralize/serialize all spawn requests.
`CoInitializeEx`, manual message pumping, a queued connection alone, and
pywinpty flags do not solve the underlying legality of the call.

## References

- Microsoft, `InSendMessageEx`:
  https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-insendmessageex
- Microsoft, COM call synchronization:
  https://learn.microsoft.com/en-us/windows/win32/com/call-synchronization
- Microsoft, messages and message deadlocks:
  https://learn.microsoft.com/en-us/windows/win32/winmsg/about-messages-and-message-queues
- Microsoft, `CoInitializeEx`:
  https://learn.microsoft.com/en-us/windows/win32/api/combaseapi/nf-combaseapi-coinitializeex
- Qt, `QApplication::activeModalWidget()` / `activePopupWidget()`:
  https://doc.qt.io/qt-6/qapplication.html
- Qt, `QFileDialog` Windows timer behavior:
  https://doc.qt.io/qt-6/qfiledialog.html
- Qt, queued connections:
  https://doc.qt.io/qt-6/threads-qobject.html

