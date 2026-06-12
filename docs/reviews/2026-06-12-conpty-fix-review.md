# ConPTY boot/spawn crash fix (Tier 1+2) — code review

Date: 2026-06-12
Reviewer: reviewer
Branch: `fix/conpty-boot-spawn-crash`
Scope reviewed: uncommitted working-tree diff of `spawn_gate.py`,
`orchestrator.py`, `main_window.py`, `tests/test_spawn_gate.py`
Cross-check source: `docs/reviews/2026-06-12-conpty-fix-codex-crosscheck.md`
Impl note: `docs/reviews/2026-06-12-conpty-boot-crash-fix.md`

## VERDICT: **FAIL** (1 HIGH blocker on the Lead path) — fix then re-verify

Design is sound and 4 of 5 codex conditions are correctly met. But the Tier 2
re-defer path raises `UnboundLocalError` **on the Lead/claude branch** — i.e.
exactly the pane and exactly the rare TOCTOU case the whole fix targets. In that
case the safety net throws instead of cleanly re-deferring, and the Lead never
boots (no retry). Untested (test uses a non-Lead role). Trivial to fix.

---

## Checklist verdict (codex conditions)

| # | Condition | Result |
|---|---|---|
| 1 | Final-gate = shared helper, called adjacent to every `session.spawn()`, no yield between check and native call | **PASS** |
| 2 | Re-defer doesn't leak token / session / UUID / transcript / `_spawn_in_progress`; no busy-loop | **PARTIAL** — `_spawn_in_progress`/token OK; session QObject leaks; Lead path throws |
| 3 | Tier 1 streak N=3 spans event-loop turns + correct resets; no fixed `singleShot` | **PASS** |
| 4 | `_on_codex_exit` stale guard before `codex_spawn_ts` reset | **PASS** |
| 5 | No `CoRegisterMessageFilter` / COM-apartment / manual pump added | **PASS** |

---

## HIGH — `pane_tok` UnboundLocalError on Lead final-gate re-defer (BLOCKER)

`orchestrator.py:2157-2163` (claude branch):

```python
if not self._final_gate_clear():
    self._toctou_redefer(
        role_name, cwd, project, project_ns,
        _from_auto_respawn, _shard_total,
        pane_tok=pane_tok,          # ← unbound when role_name == LEAD
    )
    return True, f"{role_name} spawn deferred (final re-sample blocked)"
```

`pane_tok` is assigned **only in the `else:` (non-Lead) branch** at
`orchestrator.py:1909` — for `role_name == LEAD.name` (line 1901-1902 takes the
`if` branch) `pane_tok` is never bound. The `except`/token-revoke logic at
2228-2231 already guards this with `if role_name != LEAD.name`, confirming the
name is intentionally Lead-absent — but the **re-defer call passes it
unconditionally**.

**Trace (Windows only — `is_in_send_blocked()` is always False off-Windows, so
`_final_gate_clear()` is always True there):**
1. Tier 1 `_spawn_lead_when_quiet` reaches its 3-turn streak → `orch.spawn(LEAD)`.
2. Claude branch, `_spawn_in_progress = True`, calls `_final_gate_clear()`.
3. Final gate reports blocked (the TOCTOU case Tier 2 exists for).
4. Re-defer references `pane_tok` → `UnboundLocalError` → caught by
   `except Exception as e` at 2227 → returns `(False, "failed to spawn claude:
   cannot access local variable 'pane_tok' …")`.
5. `_spawn_lead_when_quiet` sees `ok=False` → shows "⚠ Lead spawn failed" for
   30 s and **never retries**. Cockpit comes up with no Lead.

So the one path the fix is built to protect degrades from "clean re-defer +
50 ms retry" to "hard fail, no Lead, cryptic error." Low probability (Tier 1
already waited for quiet) but severe outcome on the primary pane, and the
re-defer's clean-up (re-add to `_spawn_deferred`, schedule retry) never runs.

**Why untested:** `TestTier2FinalGate._make_claude_pane` defaults to
`role="backend"` (test:413, 425, 448) → `pane_tok` is bound, so the claude-branch
re-defer test passes while masking the Lead-only failure.
`TestTier1QuietBootStreak.test_n_consecutive_clears_fires_spawn` mocks
`mw.orch.spawn` (test:694), so it never executes the real Lead re-defer either.

**Fix (trivial):** bind `pane_tok = None` once at the top of the claude section
(before line 1901), letting the `else` overwrite it for non-Lead. The `else`
block already does the registration; `_toctou_redefer(pane_tok=None)` then pops
nothing (`dict.pop(None, None)`), which is correct since Lead has no pane token.
Add a `role="lead"`-equivalent Tier 2 test that drives the real `spawn()` with a
blocked final gate and asserts a clean re-defer (not `ok=False`).

---

## MEDIUM — abandoned `PtySession` QObject leaks on every blocked re-defer

All four branches construct the session **before** the final gate, parented to
the orchestrator:

- shell `orchestrator.py:1518` — `PtySession(cols=110, rows=36, parent=self)`
- claude `orchestrator.py:2150` — same, plus gemini/codex equivalents.

On a final-gate block the function `return`s without spawning and without
`session.deleteLater()`. Because the session has a **C++ Qt parent
(`parent=self`)**, the Python wrapper dropping its reference does **not** free
it — Qt keeps it alive for the orchestrator's lifetime. Each 50 ms retry that
re-blocks builds another orphan `PtySession` under the orchestrator. A gate that
stays blocked for a couple of seconds (the freeze scenario this very fix is
about) accrues ~20–40 orphan QObjects. Pre-spawn each is lightweight (no winpty
handle yet), so it's memory-creep, not a handle leak — but it is unbounded under
sustained blocking and contradicts the impl note's implicit "session is local,
GC'd" assumption.

**Fix:** in `_toctou_redefer` (or at each re-defer site) call
`session.deleteLater()` before returning, or construct the `PtySession`
**after** the final gate passes (move construction below the gate). The latter
also shrinks the check-to-native window further, which aligns with the Tier 2
goal.

---

## LOW — notes (non-blocking)

- **Stale `pane._transcript_path` on re-defer.** `pane._transcript_path = _t_path`
  is set before the gate (claude:2152, shell:1520). On re-defer the pane keeps a
  path string whose session never spawned; the retry overwrites it. Harmless if
  `_build_transcript_path` only builds a string (verify it doesn't pre-create the
  file). No action needed unless it touches disk.
- **Tier 1 quiet window slightly under codex's 150–250 ms guidance.** `N=3` ×
  `50 ms` ≈ 100–150 ms of post-initial quiet after the 150 ms first-paint wait.
  Total ≈ 300 ms wall, but the *contiguous-quiet* portion is ~100–150 ms. Within
  spirit; bump `_BOOT_LEAD_QUIET_N` to 4 if telemetry shows boot blocks slipping
  through. Not blocking.
- **Doc drift (cosmetic).** Impl note cites spawn sites "~1504/1606/1698/2130";
  actual final-gate sites are 1525/1623/1724/2157. Update for future readers.

---

## What is correct (verified, not just claimed)

- **Adjacency (item 1).** Only `_t0 = time.time()` sits between
  `_final_gate_clear()` and `session.spawn()` at all four sites — `time.time()`
  does not pump messages; no `QTimer`/`processEvents`/queued signal/`await`.
  Helper is shared (`_final_gate_clear` → `spawn_gate.is_in_send_stable`). ✓
- **`_spawn_in_progress` no-leak (item 2).** All four branches wrap the gate+spawn
  in `try: … finally: self._spawn_in_progress = False; self._drain_spawn_queue()`
  (shell 1522/1554-1556, claude 2154/2233-2235). The re-defer `return` is *inside*
  the `try`, so the flag is always reset — does not reproduce the H1 inline-reset
  leak from the 2026-06-12 review. ✓
- **Token revoke + re-schedule (item 2, non-Lead).** `_toctou_redefer` pops the
  pane token, re-adds `f"{project_ns}::{role_name}"` (key format matches the early
  gate at 1445 and `_retry_deferred_spawn` at 1256), and schedules a 50 ms retry —
  returns to the event loop, no busy-loop. `_retry_deferred_spawn` discards the
  key at 1259 before re-entry, so the early-gate dedup doesn't short-circuit the
  retry. ✓ (Lead path excepted — see HIGH.)
- **Tier 1 turn-spanning streak (item 3).** Each poll is its own
  `QTimer.singleShot(50, _spawn_lead_when_quiet)` callback = one event-loop turn;
  `_boot_quiet_count` resets to 0 on any of insend-blocked / modal / not-active /
  not-visible (main_window 1120-1168). Not "N reads in one tick." ✓
- **Codex exit guard (item 4).** Stale-session guard
  `if _pane_cdx.session is not session: return` now runs **before**
  `_ps_cx.codex_spawn_ts = None` (orchestrator 2253-2261), so a stale
  `processExited` no longer clobbers a live session's crash-diagnostic window.
  This also closes residual (b) from the round-2 review. ✓
- **No forbidden COM mechanisms (item 5).** No `CoRegisterMessageFilter`,
  `IMessageFilter`, apartment change, or manual message pump introduced. ✓

---

## Residual race acknowledged (by design, out of scope)

The intrinsic re-entrancy after entering opaque native ConPTY setup (codex §1.4)
remains — Tier 1+2 narrows exposure but cannot prove `0x8001010d` impossible.
The telemetry (`boot_lead_gate_blocked`, `spawn_toctou_redeferred`,
`spawn_native_ms`) is in place to drive a Tier 3 escalation decision, matching
codex's recommendation. Not a defect of this change.

## Required before merge

1. **HIGH** — bind `pane_tok = None` before the claude branch's Lead/non-Lead
   split so the Lead final-gate re-defer doesn't `UnboundLocalError`; add a Lead
   Tier 2 test driving the real `spawn()` with a blocked final gate.
2. **MEDIUM** — `deleteLater()` the abandoned `PtySession` on re-defer (or build
   it after the gate) to stop QObject creep under sustained blocking.

---

## RE-VERIFY (fix-loop round, 2026-06-12) — **VERDICT: PASS**

Both blockers fixed and verified against the working-tree code (not just the
impl note). No regression on previously-PASS items. Full suite green.

### HIGH #1 — `pane_tok` UnboundLocalError on Lead re-defer → **CLOSED**

- `pane_tok = None` is bound at `orchestrator.py:1906`, **before** the
  `if role_name == LEAD.name:` split at `1911`. Confirmed by reading the live
  code: the Lead branch (1911-1912) sets only `TAKKUB_LEAD_TOKEN` and leaves
  `pane_tok` as `None`; the `else` branch (1913-1929) overwrites it with the
  real `secrets.token_urlsafe(32)`. Every path through the claude branch now
  has `pane_tok` bound, so the re-defer call at `2170-2174`
  (`pane_tok=pane_tok`) can never raise `UnboundLocalError`.
- `_toctou_redefer` guards `if pane_tok is not None:` before
  `_pane_tokens.pop`, so `pane_tok=None` (Lead) is a clean no-op — correct,
  Lead has no pane token.
- **Regression test is genuine, not vacuous.** `TestToctouRedeferEdge::
  test_lead_toctou_blocked_clean_redefer` drives the **real** `orch.spawn(
  LEAD.name)` with `_final_gate_clear` patched to `False` (the exact Tier 2
  block path) and asserts `ok is True`, `"deferred"` in msg, `PtySession.spawn`
  NOT called, and the role key present in `_spawn_deferred`. Verified the test
  exercises the Lead path correctly by **temporarily neutralising the fix**
  (commenting out `pane_tok = None`) and re-running: the test fails with
  `AssertionError: ... cannot access local variable 'pane_tok' where it is not
  associated with a value` — i.e. it catches exactly this regression. Fix
  restored afterward; `git diff` confirms the working tree is back to the
  intended state (120 insertions, no temp marker).

### MEDIUM #2 — abandoned `PtySession` QObject leak on re-defer → **CLOSED**

All **4** Tier 2 re-defer sites now release the pre-spawn session before
returning (verified by reading the live code):

| Branch | Site | `setParent(None)` + `deleteLater()` |
|---|---|---|
| shell  | `1526-1527` | ✓ |
| gemini | `1626-1627` | ✓ |
| codex  | `1729-1730` | ✓ |
| claude | `2168-2169` | ✓ |

`setParent(None)` detaches the QObject from the orchestrator's C++ parent tree
(so the C++ side no longer keeps it alive) and `deleteLater()` schedules
deletion on the next event-loop turn — no orphan accumulation under sustained
blocking.

### No regression on previously-PASS items

- **Adjacency / no-yield (item 1).** At all 4 sites only `_t0 = time.time()`
  sits between `_final_gate_clear()` returning True and `session.spawn()`
  (e.g. claude `2176-2177`). No `processEvents`/`QTimer`/`await`/queued signal
  inserted. ✓
- **`_spawn_in_progress` finally-release (item 2).** The fix round did not touch
  the `finally` blocks (the only diff hit is a docstring line); all 4 resets
  remain at `1557 / 1658 / 1759 / 2246`. The re-defer `return` is **inside** the
  `try`, so `finally` still fires — no flag leak. ✓
- **Codex-exit guard (item 4).** Stale-session guard
  `if _pane_cdx.session is not session: return` still runs **before**
  `_ps_cx.codex_spawn_ts = None` — no clobber of a live session's
  crash-diagnostic window. ✓

### Test evidence

- `tests/test_spawn_gate.py` — 47 passed.
- Full suite — **1985 passed, 2 skipped** (2 skipped = QtWebEngine import opt-out).
- Neutralise-the-fix check — Lead regression test fails with the exact
  `UnboundLocalError` when `pane_tok = None` is removed (proves coverage).

**Cleared for merge** (subject to Lead's version-control decision). LOW notes
from the original review (stale `_transcript_path` on re-defer, Tier 1 quiet
window ~100-150 ms, doc line-number drift) remain non-blocking.
