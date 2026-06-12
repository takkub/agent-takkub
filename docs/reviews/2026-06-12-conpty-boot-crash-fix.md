# ConPTY boot crash fix — Tier 1 + Tier 2 + residual

Date: 2026-06-12  
Branch: fix/conpty-boot-spawn-crash  
Files changed: `spawn_gate.py`, `orchestrator.py`, `main_window.py`, `tests/test_spawn_gate.py`

## Problem

`runtime/boot.log` recorded 36 instances of `RPC_E_CANTCALLOUT_ININPUTSYNCCALL`
(Windows HRESULT `0x8001010d`).  Trigger #1: `main_window.py _boot` →
`orch.spawn(LEAD)` → `pty_session.spawn` → `winpty.PtyProcess.spawn(ConPTY)`.
The ConPTY COM call-out fails because the Qt main thread is inside an
input-synchronous `SendMessage` context (window-activation storm after
`w.show()`).

The existing 3-layer spawn gate (`spawn_gate.py`) checks `InSendMessageEx`
once at entry to `Orchestrator.spawn()`.  The large argv/env/transcript setup
interval between that check and the actual native call creates a TOCTOU
exposure.

## Fix summary

### Tier 1 — quiet-boot defer (`main_window.py`)

Replace `QTimer.singleShot(0, self._boot)` → `spawn(LEAD)` with an
event-driven debounce.

**Old flow**: `singleShot(0, _boot)` → `spawn(LEAD)` immediately — first idle
event fires during activation storm.

**New flow**:
1. `_boot()` does all non-spawn setup immediately (CLI listen, label, RTK
   button, preset/tab timers).
2. Schedules `QTimer.singleShot(150, _spawn_lead_when_quiet)` for first-paint
   approximation.
3. `_spawn_lead_when_quiet()` polls from separate `QTimer.singleShot(50, ...)` 
   callbacks — one event-loop turn per poll.  Each turn checks:
   - `is_in_send_blocked()` clear
   - Qt modal/popup gate clear (via `_spawn_gate_pred`)
   - `QApplication.applicationState() == ApplicationActive`
   - `self.isVisible()` (window exposed)
4. Streak counter `_boot_quiet_count` resets to 0 on any failure.
5. After `_BOOT_LEAD_QUIET_N = 3` consecutive clear turns (~150–250 ms of quiet
   at 50 ms intervals), calls `orch.spawn(LEAD.name)`.

Telemetry: `boot_lead_gate_blocked` event logged on each blocked turn with
`insend_clear`, `modal_clear`, `app_active`, `window_ready` flags.
`boot_lead_spawn_ready` logged when streak completes.

**Key insight from codex cross-check**: "N consecutive clear samples in one
callback" is a no-op — no dispatch occurs between synchronous reads.  Streak
must span separate event-loop turns.  Debounce reset on `InSendMessageEx /
modal / inactive / not-visible` is the correct implementation.

### Tier 2 — final re-sample gate (`orchestrator.py`)

Added `_final_gate_clear()` and `_toctou_redefer()` to `Orchestrator`.

**Before each `session.spawn()` call** (4 sites: shell ~1504, gemini ~1606,
codex ~1698, claude ~2130), immediately after all argv/env/transcript setup
and `_spawn_in_progress = True`:

```python
if not self._final_gate_clear():
    self._toctou_redefer(role_name, cwd, project, project_ns,
                         _from_auto_respawn, _shard_total, pane_tok=<tok>)
    return True, f"{role_name} spawn deferred (final re-sample blocked)"
# ← native ConPTY call here, no yield between check and call
```

`_final_gate_clear()` calls `spawn_gate.is_in_send_stable(_TOCTOU_RESAMPLE_N=3)`
— 3 synchronous reads in one callback.  Not a temporal quiet period; purpose is
to remove the large setup interval between the early gate check and the native
call.

`_toctou_redefer()` on failure:
- Revokes pane token from `_pane_tokens` (no token leak)
- Re-adds role to `_spawn_deferred`
- Schedules `_retry_deferred_spawn` at 50 ms
- `_spawn_in_progress` reset by `finally` block (no flag leak)
- Logs `spawn_toctou_redeferred` event

Each `session.spawn()` success also logs `spawn_native_ms` with the native call
duration in milliseconds — telemetry for escalation decisions.

**No yield between final check and native call**: the `return True, ...` path
exits the function; the caller's event loop does not run until after the `spawn`
function returns.  No `processEvents`, `QTimer`, or `await` is inserted.

### Fix-loop round 2 — `pane_tok` UnboundLocalError + QObject session leak

Two issues found by post-merge review and fixed in the same branch:

**HIGH — `pane_tok` unbound on Lead re-defer path** (`orchestrator.py`)

`pane_tok` was only assigned in the `else` (non-Lead) branch (~line 1909) but
passed unconditionally to `_toctou_redefer(pane_tok=pane_tok)` at the Tier 2
re-defer site for the claude branch.  For `role_name == LEAD.name` the variable
was never bound, so a Tier 2 final-gate block on the Lead path raised
`UnboundLocalError`, was caught by the outer `except Exception`, and returned
`ok=False` — preventing the Lead from ever booting on the exact rare path the
fix exists to protect.

Fix: bind `pane_tok = None` once before the `if role_name == LEAD.name:` split
(line ~1897).  The non-Lead `else` branch overwrites it with the real token.
`_toctou_redefer(pane_tok=None)` skips the `_pane_tokens.pop` via the existing
`if pane_tok is not None:` guard — correct, because Lead has no pane token.

Regression test added: `TestToctouRedeferEdge.test_lead_toctou_blocked_clean_redefer`
— drives the real `orch.spawn(LEAD.name)` with `_final_gate_clear` patched to
`False`, asserts `ok is True`, "deferred" in msg, no `PtySession.spawn` call,
and role key present in `_spawn_deferred`.

**MEDIUM — abandoned `PtySession` QObject leaks on re-defer** (`orchestrator.py`)

All four spawn branches (`session = PtySession(parent=self)`) constructed the
`PtySession` before the final gate check.  On a gate block the function returned
without spawning and without releasing the session.  Because the Python wrapper
had a C++ Qt parent (`parent=self`), dropping the Python reference did not free
the underlying QObject — Qt kept it alive for the orchestrator's lifetime.
Sustained gate blocking could accumulate ~20–40 orphan pre-spawn QObjects.

Fix: at each of the 4 re-defer sites (shell, gemini, codex, claude) call
`session.setParent(None); session.deleteLater()` immediately before
`_toctou_redefer`.  `setParent(None)` detaches from the orchestrator tree;
`deleteLater()` schedules the C++ side for deletion on the next event-loop turn.

### Residual — `_on_codex_exit` spawn_ts clobber (`orchestrator.py`)

Bug: stale `processExited` could clear `codex_spawn_ts` of a newly-spawned
session, losing the crash-diagnostic window.

**Old order**:
1. Read `spawn_ts`
2. Clear `_ps_cx.codex_spawn_ts = None`  ← clobbers new session if stale
3. Stale-session guard

**New order**:
1. Read `spawn_ts`
2. Stale-session guard: `if pane.session is not session: return`  ← early exit
3. Clear `_ps_cx.codex_spawn_ts = None`  ← only runs for current session

### `spawn_gate.py` — `is_in_send_stable(n)`

New public function:
```python
def is_in_send_stable(n: int = 3) -> bool:
    return not any(is_in_send_blocked() for _ in range(n))
```

Used by `Orchestrator._final_gate_clear()`.

## Test results

```
1985 passed, 2 skipped
```

2 skipped = `TestDisableTranscriptOptOut` (QtWebEngineWidgets import).

New tests added to `tests/test_spawn_gate.py`:

| Class | Tests | Coverage |
|---|---|---|
| `TestTier2FinalGate` | 7 | all 4 branches (shell/gemini/codex/claude), token revoke, in_progress reset, clear/clear/blocked sequence |
| `TestTier1QuietBootStreak` | 3 | blocked resets streak; N clear turns → spawn; blocked mid-streak resets then fires |
| `TestCodexExitSpawnTsGuard` | 2 | stale exit no clobber; current exit clears normally |
| `TestToctouRedeferEdge` | 2 | pane_tok=None no crash; **Lead path blocked final gate → clean re-defer (fix-loop round 2)** |

## Telemetry events added

| Event | Location | Fields |
|---|---|---|
| `boot_lead_gate_blocked` | `_spawn_lead_when_quiet` | `insend_clear`, `modal_clear`, `app_active`, `window_ready` |
| `boot_lead_spawn_ready` | `_spawn_lead_when_quiet` | `quiet_turns` |
| `spawn_toctou_redeferred` | `_toctou_redefer` | `role`, `project` |
| `spawn_native_ms` | each spawn branch | `role`, `project`, `ms` |

Parse with `jq` on `runtime/events.log` to count deferral rates and monitor
native spawn duration post-release.

## What this does NOT fix

The residual race: a cross-process/thread `SendMessage` can arrive at any point
during the opaque native ConPTY setup code.  Tier 1+2 reduces exposure by not
entering ConPTY while already in a forbidden context; it cannot prevent a new
SendMessage from arriving during native setup.  If `0x8001010d` persists on
the Tier 1+2 build, escalate to Tier 3 (sidecar helper process).

**No `CoRegisterMessageFilter`**: that handles `RPC_E_SERVERCALL_RETRYLATER`,
not `RPC_E_CANTCALLOUT_ININPUTSYNCCALL`.  See cross-check docs.

## Tier 3 design note (not implemented)

A sidecar process that owns ConPTY creation and IPC-streams the PTY to the Qt
process would fully decouple the GUI thread from native COM construction.
Required components: framed byte-stream IPC, resize/exit/failure protocol,
sidecar crash detection, token/env handling.  Implement only if post-release
telemetry shows unacceptable recurrence of `0x8001010d`.
