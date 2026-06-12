# ConPTY 0x8001010d Tier 1+2 cross-check

Date: 2026-06-12  
Scope: quiet boot defer in `MainWindow._boot()` and final spawn-gate
re-sampling in `Orchestrator.spawn()`  
Constraints: keep ConPTY, main-thread spawn, and existing ANSI streaming

## Verdict

Tier 1+2 is the best proportionate mitigation under the stated constraints,
provided Tier 2 performs its final check immediately adjacent to every
`session.spawn()` call. It should materially reduce the observed boot/spawn
crash, but it cannot prove that `RPC_E_CANTCALLOUT_ININPUTSYNCCALL` is
impossible.

The remaining race is intrinsic to calling opaque native ConPTY setup on the
Qt/OLE GUI thread. A clear `InSendMessageEx` result describes the current
thread state only. It is not a lock, reservation, or promise covering the
subsequent native call.

Recommended release decision:

1. Ship Tier 1+2 with counters/logging for deferrals, final-gate failures,
   native spawn duration, and HRESULT/crash recurrence.
2. Do not add `CoRegisterMessageFilter` as part of this fix.
3. Defer the helper-process design until post-release telemetry shows whether
   Tier 1+2 leaves a meaningful crash/freeze rate, unless zero UI-process crash
   is already a hard product requirement.

## 1. What consecutive `InSendMessageEx` samples achieve

Microsoft documents `InSendMessageEx(NULL)` as a test of whether the current
window procedure is processing a message sent from another thread. The
blocked-sender test used by `spawn_gate.py` is correct:

```text
(flags & (ISMEX_REPLIED | ISMEX_SEND)) == ISMEX_SEND
```

### Samples in one callback

Reading `InSendMessageEx` N times synchronously in one Python callback adds
almost no temporal protection. No Qt/Windows message dispatch normally occurs
between those reads, so N clear samples mostly confirm the same instant N
times. This is useful only as a cheap defensive re-read against an unusual
state transition or test instrumentation; it is not a real quiet window.

For Tier 1, "N consecutive clear samples" should mean N separate event-loop
turns, or clear observations spanning a minimum elapsed quiet period. Reset
the streak whenever any of these is true:

- `InSendMessageEx` reports a blocked sender.
- Qt has an active modal or popup widget.
- the application/window is not active and exposed.

Activation/exposure is a boot-readiness proxy, not a safety proof. A practical
policy is to require active/exposed state and clear gate observations over
roughly 150-250 ms before the initial Lead spawn.

### Final samples immediately before native spawn

Tier 2 is still valuable even if its consecutive reads happen in one callback.
Its purpose is different: remove the large preparation interval between the
existing gate around `orchestrator.py:1394` and the native calls at roughly
1466, 1553, 1643, and 2065.

The final check must be in a shared helper/wrapper called directly before each
`session.spawn()`, after argv/env/transcript/session construction. Once it
passes, call native spawn in the same callback without a timer, queued signal,
`processEvents()`, or other explicit message-pump yield. If blocked, abandon
that prepared attempt cleanly and re-enter the existing deferred path.

### Residual worst-case race

Ordinary posted Qt events cannot run between the final Python check and the
next Python call while the GUI thread remains in the same callback. Likewise,
a cross-thread `SendMessage` does not generally preempt arbitrary code on the
target thread.

The unresolved case is re-entrancy after entering opaque native code:

1. The final sample is clear.
2. `session.spawn()` enters pywinpty/ConPTY setup.
3. Native setup, COM/OLE called by it, or a dependency pumps/dispatches Windows
   messages or invokes a callback/hook.
4. A system, WebEngine, IME, accessibility, focus, activation, or other
   cross-thread sent message is dispatched on the GUI thread.
5. Code reached under that input-synchronous dispatch attempts an outgoing
   COM/RPC call and receives `0x8001010d`, or the native stack wedges.

There is also a smaller pre-entry possibility if any operation is inserted
between the final check and native call that itself pumps messages or invokes
re-entrant user/native code. This is why the checked section must contain
almost nothing.

No finite number of pre-call samples closes these races. It reduces exposure
to entering ConPTY while the thread is *already* in a forbidden context.

## 2. Alternative Win32 mechanisms

### `CoRegisterMessageFilter` / `IMessageFilter`

This is not a better replacement for the gate.

`IMessageFilter` handles COM concurrency while an STA is waiting on or
receiving COM calls. `RetryRejectedCall` is for calls rejected by a busy
callee, represented by errors such as `RPC_E_SERVERCALL_RETRYLATER` or
`RPC_E_SERVERCALL_REJECTED`. The target failure is different:
`RPC_E_CANTCALLOUT_ININPUTSYNCCALL` says the outgoing call is illegal because
the caller is currently dispatching an input-synchronous call. Retrying policy
does not make that call legal at that point.

There are integration risks too:

- only one message filter may be registered per thread;
- it is STA-only;
- installing one could replace or interfere with a filter owned by Qt, OLE,
  WebEngine, or another library;
- a correct implementation and lifetime bridge from Python/ctypes is much
  larger and riskier than the scoped mitigation.

Do not add it for Tier 1+2. It could be investigated separately only for a
demonstrated rejected-call problem, not as the fix for `0x8001010d`.

### Stronger activation-complete detection

Windows does not expose a single "all activation-related sent messages are
finished and none can arrive" primitive. `QApplication.applicationState()`,
`QWindow::isExposed`, `isActiveWindow()`, `windowActivated`, `WM_ACTIVATE`,
`WM_SETFOCUS`, and `ShowWindow` completion describe milestones, not a future
message-free guarantee.

The best boot heuristic is event-driven readiness plus debounce:

1. Wait until the main window is shown/exposed and the application is active.
2. Start or restart a 150-250 ms quiet timer on relevant activation/state
   changes.
3. Across timer/event-loop turns, require the Qt modal/popup gate and
   `InSendMessageEx` to remain clear.
4. Let centralized Tier 2 make the final immediate decision.

This is stronger than an unconditional `singleShot(150)` because slow startup,
RDP, focus stealing, restore/minimize, or WebEngine initialization can outlast
a fixed delay. It still remains a heuristic by design.

`ReplyMessage`, manual message pumping, and changing COM apartment mode are not
appropriate. They alter dispatch/COM semantics owned by Qt/OLE and can create
new re-entrancy or correctness failures.

## 3. Is Tier 1+2 the best mitigation within constraints?

Yes.

Tier 1 removes the highest-risk startup timing: `singleShot(0, _boot)` currently
allows Lead spawn while show/activation/focus/WebEngine work is still settling.
Tier 2 checks the actual Win32 condition at the last controllable point and
avoids yielding after it. The existing modal/popup predicate and FIFO
serialization remain useful defense in depth.

Implementation conditions:

- Keep the early gate to avoid expensive preparation during a known blocked
  state, but do not mistake it for the final gate.
- Centralize the final gate so shell, Gemini, Codex, and Claude cannot diverge.
- Ensure a final-gate deferral does not leak pane tokens, session objects, UUID
  state, transcript state, or `_spawn_in_progress`.
- Do not busy-loop until clear. Return to the event loop and retry later.
- Test a state sequence such as clear/clear/blocked and verify native spawn is
  never called.
- Test all four provider branches, not only the Claude path.

One semantic nuance: if Tier 2 takes multiple samples without yielding, tests
should describe this as a final re-sample, not as proving an N-turn quiet
period.

## 4. Helper-process Tier 3

A sidecar that owns ConPTY creation and lifetime is the architectural boundary
that can protect the Qt process from both native construction crashes and
main-thread freezes. With asynchronous IPC, it removes ConPTY setup from the
GUI thread without changing the user-visible terminal stream.

It is nevertheless a substantial subsystem:

- framed bidirectional byte streaming and backpressure;
- resize, input, exit code, startup failure, cancellation, and teardown
  protocol;
- sidecar crash detection and process-tree cleanup;
- token/environment handling and transcript ownership;
- packaging, upgrades, diagnostics, and Windows-only integration tests.

Therefore it is worth designing now, but not necessarily implementing before
measuring Tier 1+2. Proceed immediately only if any remaining cockpit-process
crash/freeze is unacceptable or the current incident rate is already high
enough to justify the architectural cost. Otherwise ship Tier 1+2, collect
versioned telemetry for a defined observation window, and set an explicit
escalation threshold rather than leaving Tier 3 indefinite.

Suggested escalation signals include any confirmed recurrence of
`0x8001010d` on the Tier 1+2 build, repeated native spawn stalls, or a crash
rate above the product's reliability budget.

## References

- Microsoft, `InSendMessageEx`:
  https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-insendmessageex
- Microsoft, `IMessageFilter`:
  https://learn.microsoft.com/en-us/windows/win32/api/objidl/nn-objidl-imessagefilter
- Microsoft, `CoRegisterMessageFilter`:
  https://learn.microsoft.com/en-us/windows/win32/api/objbase/nf-objbase-coregistermessagefilter
- Microsoft, COM call synchronization:
  https://learn.microsoft.com/en-us/windows/win32/com/call-synchronization
- Microsoft, HRESULT `0x8001010d`:
  https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-erref/705fb797-2175-4a90-b5a3-3918024b10b8
