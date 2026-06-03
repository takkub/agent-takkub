# QA Wave 1 Verification — 2026-06-03

**Suite:** 1600 passed, 2 skipped, 0 failed · ruff: all checks passed
**Scope:** Tier 1 #1-7, Tier 2 #8-9, Tier 3 #16-17 from `docs/reviews/2026-06-03-improvement-audit.md`

---

## Verdict: PASS (with 4 coverage gaps noted)

---

## Fix-by-fix status

| # | Finding | Code Location | Test Coverage | Status |
|---|---|---|---|---|
| #1 | `--shards` clamp 1–8 | `cli.py:145-156` `cmd_assign` | `TestShardClamp` (0/neg/9 rejected, 1/8 accepted) | ✅ PASS |
| #2 | ShardGroup generation guard | `orchestrator.py:809,831,1947-1953` | `TestShardGenerationGuard` (stale mismatch bails, matching fires) | ✅ PASS |
| #3 | done() late-complete notice | `orchestrator.py:2762-2784` | `TestShardLateComplete` (alive-lead write + absent-lead queue) | ✅ PASS |
| #4 | Shard crash + Lead down → group closes | `orchestrator.py:2135-2149` | `TestShardRespawnCappedLeadDown` (no lead, group closes + handoff queued) | ✅ PASS |
| #5 | Spawn-fail records into shard group | `orchestrator.py:1903-1920` | `TestShardSpawnFail` (creates group, fires handoff when last) | ✅ PASS |
| #6 | Watchdog snapshot+briefs before os._exit | `app.py:143-153` | os._exit tested; **snapshot/briefs call NOT asserted** → gap below | ✅ code correct / ⚠️ gap |
| #7 | `display_lines` → `hashlib.blake2b` | `orchestrator.py:3720-3723` | `test_lifecycle_recovery.py` uses `_EMPTY_FILTERED_HASH` constant (blake2b) | ✅ PASS |
| #8 | close() auto-chain last pane → handoff | `orchestrator.py:2516-2532` | `TestAutoChainCloseHandoff` (last pane fires, non-last doesn't, no flag skips) | ✅ PASS |
| #9 | Restore re-paste last_task + persist | `orchestrator.py:3295-3378` | `TestSnapshotAndRestore` (snapshot field, queue notice present); **re-paste call not asserted** → gap below | ✅ code correct / ⚠️ gap |
| #16 | Doctor button + dialog + Fix button | `main_window.py:656-665, 1828-1902` | `TestDoctorIntegration` (run_all_checks, format_report, auto_fix callable) | ✅ PASS |
| #17 | Provider chip 3-state (amber=not_installed) | `main_window.py:198-269` | `TestProviderChipStyle/State/Tooltip` (disabled/not_installed/available × codex/gemini) | ✅ PASS |

---

## Coverage gaps (not regressions — code is correct, tests don't guard future drift)

### Gap 1 — #6: watchdog snapshot+briefs call unasserted
`test_single_instance_watchdog.py:TestWatchdogThreadBehaviour.test_watchdog_calls_os_exit_when_heartbeat_stale`
confirms `os._exit(1)` fires but does not mock/spy `write_session_snapshot` or `write_resume_briefs`
to assert they were called before exit.
**Risk:** future edit could move the try/except after `os._exit` and test wouldn't catch it.
**Suggested test:** add `window.orch.write_session_snapshot = MagicMock()` + `window.orch.write_resume_briefs = MagicMock()` to that test and assert both were called once before `exit_called` is set.

### Gap 2 — #9: `_send_when_ready` task re-paste call unasserted
The restore test in `tests/test_orchestrator_shard.py` (TestSnapshotAndRestore.test_restore_teammates_queues_notice_with_task) patches `_send_when_ready`
(via `patch.object`) but only checks the pending_done_notices queue, not that `_send_when_ready`
was called with `"add /login form"`.
**Risk:** if the re-paste path is accidentally removed, test still passes.
**Suggested assertion:** `orch._send_when_ready.assert_called_once_with("frontend", "add /login form")` (after fixing the monkeypatch to use `patch.object` return value).

### Gap 3 — Cross-project shard isolation
No test spawns shards for 2 different projects simultaneously and confirms `_shard_groups` keys
don't bleed. `_shard_groups` key is `"{project_ns}::{base_role}"` (correct), but untested.

### Gap 4 — All-shards-failed-via-respawn-cap aggregate
Audit doc listed this as a missing test. `TestShardSpawnFail` covers spawn-fail path.
Respawn-cap + all-failed path is partially covered by `TestShardRespawnCappedLeadDown`
(1 already-done + 1 cap = close). An all-crashed (both cap, none done) scenario is missing.

---

## Regression check

No regressions found. All 1600 existing tests pass. Fixes are narrowly scoped:
- #1-5 touch only the shard assign/respawn/close path
- #6 touches only the deadman-watchdog branch (os._exit path only)
- #7 changes hash function in the stuck-detect tick loop
- #8 adds a read + conditional call in `close()` before pop
- #9 adds fields to snapshot_state output + a _send_when_ready call in restore_teammates
- #16-17 add UI helpers + button wiring with no behavior change to existing orchestrator logic
