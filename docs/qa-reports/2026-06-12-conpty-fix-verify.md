# QA Verification — ConPTY boot crash fix (Tier 1 + Tier 2)

**Date:** 2026-06-12  
**Branch:** `fix/conpty-boot-spawn-crash`  
**VERDICT: PASS**

---

## Full suite

```
1984 passed, 2 skipped
```

Baseline was 1971 passed, 2 skipped.  13 new tests added (see below).  No regressions.

---

## Targeted smoke results

### 1. Tier 1 `_spawn_lead_when_quiet` streak logic

| Scenario | Result |
|---|---|
| `is_in_send_blocked=True` mid-streak → reset to 0, reschedule | PASS |
| N=3 consecutive clear turns → `orch.spawn(LEAD)` called exactly once | PASS |
| blocked mid-streak (clear, block, 3×clear) → resets then fires once | PASS |
| `modal_pred()=True` → streak reset | PASS (new) |
| `applicationState != ApplicationActive` → streak reset | PASS (new) |
| `isVisible()=False` → streak reset | PASS (new) |

**Key assertion confirmed:** Each turn is a *separate QTimer callback* (one event-loop turn). The streak counter increments only across real turns — `_boot_quiet_count < _BOOT_LEAD_QUIET_N` schedules another singleShot without spawning. No premature spawn possible.

### 2. Tier 2 final re-sample gate — all 4 branches

| Branch | TOCTOU blocked → no native spawn | `_spawn_in_progress` reset | token revoked |
|---|---|---|---|
| claude (backend) | PASS | PASS | PASS |
| shell | PASS | PASS (new) | — |
| gemini | PASS | PASS (new) | — |
| codex | PASS | PASS (new) | — |

**clear/clear/blocked sequence:** `is_in_send_stable(3)` returns False when any of the 3 synchronous samples is blocked — confirmed by new `TestIsInSendStable` direct unit tests (all 5 cases).

### 3. No-leak on TOCTOU redefer

| Artifact | Behavior |
|---|---|
| `_spawn_in_progress` | Reset to False via `finally` block — verified on all 4 branches |
| `_pane_tokens` | Revoked on deferred path — confirmed for claude + shell |
| `_spawn_deferred` | Role re-added for retry — confirmed on all branches |
| `_spawn_in_progress` when `pane_tok=None` | No crash — confirmed (new) |
| Unknown/already-revoked token | `dict.pop` with default — no crash (new) |

No leaked pane tokens, sessions, UUIDs, or transcripts observed. The `_toctou_redefer` path does not create a new `PtySession` — it exits before that point — so no orphaned session objects.

### 4. Normal spawn path regression

Existing `TestSpawnGateDefer.test_no_guard_spawn_proceeds` and `test_gate_clear_gate_pred_false` confirm gate-clear path still spawns normally. Full 1984-pass suite provides broader regression coverage.

### 5. Telemetry events

`_log_event` calls are present in source at:
- `boot_lead_gate_blocked` — `_spawn_lead_when_quiet` L1147
- `boot_lead_spawn_ready` — `_spawn_lead_when_quiet` L1161
- `spawn_toctou_redeferred` — `_toctou_redefer` L1348
- `spawn_native_ms` — all 4 spawn branches (shell L1534, gemini L1632, codex L1733, claude L2166)

Not tested via live `events.log` file (no GUI runtime in CI). Source inspection confirms placement is correct. Parse with `jq 'select(.event == "spawn_toctou_redeferred")' runtime/events.log` post-deploy.

### 6. `_on_codex_exit` spawn_ts guard

| Scenario | Result |
|---|---|
| Stale exit (pane.session != old_session) → early return, spawn_ts unchanged | PASS |
| Current exit (pane.session == current_session) → spawn_ts cleared to None | PASS |

Guard order confirmed: stale check runs *before* `codex_spawn_ts = None` assignment (line 2261 vs 2265).

---

## Edge cases found and covered (new tests — gap-fill)

| Class | Test | Gap addressed |
|---|---|---|
| `TestIsInSendStable` | all-clear → True | `is_in_send_stable` had zero direct tests |
| `TestIsInSendStable` | all-blocked → False | |
| `TestIsInSendStable` | clear/clear/blocked → False | Key TOCTOU case tested at unit level |
| `TestIsInSendStable` | n=1 blocked → False | Edge: single-sample gate |
| `TestIsInSendStable` | n=1 clear → True | |
| `TestTier1NonInsendConditions` | modal blocked → reset | Only insend was tested before |
| `TestTier1NonInsendConditions` | app_inactive → reset | |
| `TestTier1NonInsendConditions` | window_not_visible → reset | |
| `TestTier2InProgressResetNonClaude` | shell TOCTOU → in_progress=False | Only claude branch was tested |
| `TestTier2InProgressResetNonClaude` | gemini TOCTOU → in_progress=False | |
| `TestTier2InProgressResetNonClaude` | codex TOCTOU → in_progress=False | |
| `TestToctouRedeferEdge` | pane_tok=None → no crash | Defensive path not exercised |
| `TestToctouRedeferEdge` | unknown token → no crash | dict.pop fallback not exercised |

---

## Gaps not covered (acceptable risk)

| Gap | Risk | Rationale |
|---|---|---|
| Live `events.log` content verification | Low | Requires GUI runtime. Source review confirms placement. |
| Telemetry `spawn_native_ms` fires post-native-call | Low | Only testable with real PtySession.spawn; all mock paths exit before it |
| Residual race: new SendMessage arrives *during* native ConPTY setup | By design | Acknowledged in impl notes. Tier 1+2 reduces exposure; cannot eliminate. Escalate to Tier 3 if post-release `0x8001010d` persists. |
| `TOCTOU_RESAMPLE_N` constant value drift | Low | Constant is 3; `is_in_send_stable` accepts `n` — any future change picked up by direct unit tests |

---

## Summary

The Tier 1 debounce streak and Tier 2 TOCTOU re-sample gate are implemented correctly and are well-tested. All 4 spawn branches handle the TOCTOU defer path cleanly with no token, flag, or session leaks. The `_on_codex_exit` stale-guard order fix is correct. 13 new tests cover previously untested edge cases (direct `is_in_send_stable` unit tests, non-insend Tier 1 conditions, non-claude TOCTOU `_spawn_in_progress` reset). No regressions in the 1984-test suite.
