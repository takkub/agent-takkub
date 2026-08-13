# Issues #170, #171, #172, #176 — spawn-queue-wedge cluster (wave A)

Date: 2026-08-13
Scope: `takkub assign` spawn arbiter wedge → silent FIFO drop → doctor blind spot →
1800s busy-ceiling silence.

## Finding: all 4 are duplicates of #139/#140/#141/#144, already fixed in v1.0.42 (2026-08-04)

Each of the 4 GitHub issues was migrated from a **local-only tracker** entry
(`local://issue/4`, `/5`, `/6`, `/10`, all created 2026-08-04 07:26–08:37) describing the
**wash-locker incident** of the morning of 2026-08-04. `CHANGELOG.md`'s `[1.0.42] - 2026-08-04`
entry shows the *same incident* (explicitly names "โปรเจค wash-locker เมื่อ 4 ส.ค.") was fixed
that same afternoon under a **different** issue-numbering scheme (#139, #140, #141, #144),
landed in commits `758357d` (fix(spawn): bound native spawn, unwedge FIFO queue, surface
busy-wait early) and `b4e6905` (fix(#141): takkub doctor --live sees spawn-queue wedges).

The tracker migration that created #170/#171/#172/#176 imported the morning bug reports
without checking they'd already been closed out by the afternoon commits on the same day —
this is a stale-duplicate-import artifact, not a live gap. Both commits are ancestors of
this branch's HEAD (`git merge-base --is-ancestor` confirmed against `758357d`/`b4e6905`),
and the running code (`__version__ = "1.0.57"`) is 15 releases past the 1.0.42 fix.

| GH issue | Local origin | Fixed by | Mechanism |
|---|---|---|---|
| **#170** spawn queue wedged, `spawn_in_progress` stuck forever | `local://issue/4` | **#139** | Escape hatch: `spawn_engine.SpawnEngineMixin._check_spawn_queue_stuck()`, ridden on the idle watchdog's 5s tick (`orchestrator.py:3597`). If the queue head has waited ≥ `SPAWN_QUEUE_STUCK_SEC` (120s, `spawn_engine.py:115`), forces `_spawn_in_progress = False` and calls `_drain_spawn_queue()` — unwedges the arbiter without a manual `takkub restart`. Also: the native ConPTY call itself is now wrapped in `spawn_pty_bounded()`/`PtySpawnTimeout` (30s default) so the call that used to block 56.9 minutes (`spawn_native_ms=3,412,178` in the original incident) can no longer hold `_spawn_in_progress` open indefinitely. |
| **#172** silent FIFO drop — queued assign never gets a Lead notice | `local://issue/6` | **#140** | `spawn_engine._check_spawn_queue_stuck()` calls `self._warn_lead_spawn_stuck(role_name, project, age_sec)` (`lead_inbox.py:968`) before draining — a blocking `[spawn-stuck]` notice to Lead (bypasses the digest queue, same tier as `[spawn-failed]`), naming the stuck role and confirming a retry is already underway. Logged as `spawn_stuck_warned`. |
| **#171** `takkub doctor` reports all-clear while the queue is wedged | `local://issue/5` | **#141** | `spawn_queue_health.SpawnQueueHealthMonitor` (new module) polls `orch._spawn_queue`/`orch._spawn_in_progress` every 2s and tracks transition ages independently of queue depth (so it also catches `spawn_in_progress=True` with an otherwise-empty queue). `cli_server.py`'s `spawn-queue-status` RPC exposes a snapshot; `doctor.check_spawn_queue_live()` (`doctor.py:1691`) FAILs when `in_progress_age_s` or `oldest_queued_age_s` ≥ 60s, telling the Lead to `takkub restart`. Only runs on `takkub doctor --live` (opt-in TCP round-trip) — a plain `takkub doctor` stays pure-logic as designed; SKIPs (not FAILs) when the cockpit isn't running. |
| **#176** task delivery hits the 1800s busy ceiling silently | `local://issue/10` | **#144** | `lead_inbox.py`'s `_send_when_ready()` busy-wait branch (`BUSY_WAIT_CEILING_SEC`, default 1800s) now calls `_warn_lead_delivery_busy_wait()` (`lead_inbox.py:864`) **once, right as the extension begins** — not just when/if the ceiling eventually fires. If the ceiling does fire, the pre-existing `[delivery-unconfirmed]` notice (`_warn_lead_delivery_unconfirmed(busy_ceiling=True)`) still lands with ceiling-specific wording (distinguishes "pane busy the whole time" from "pane went empty/silent"). |

## Verification (this session)

Targeted tests, run from a fresh editable install (`.venv`) on this worktree's HEAD
(rebased onto `origin/release/2026-08-13`):

```
tests/test_spawn_queue_stuck.py
tests/test_spawn_queue_health.py
tests/test_spawn_gate.py
tests/test_spawn_task_delivery.py
tests/test_delivery_unconfirmed.py
tests/test_delivery_busy_wait_notice.py
                                          → 134 passed, 0 failed, 0 errors

tests/test_doctor.py
tests/test_launch_session.py
tests/test_pty_session_spawn_timeout.py
tests/test_single_instance_watchdog.py
                                          → 121 passed, 0 failed, 0 errors
```

No source changes were made — this wave is a verification + tracker-hygiene pass, not
an implementation pass. Considered and rejected one speculative extension (below).

## Considered and rejected: self-heal for `spawn_in_progress` stuck True with an *empty* queue

`_check_spawn_queue_stuck()` only re-evaluates the head of `_spawn_queue`; if
`_spawn_in_progress` were somehow stuck `True` with **nothing queued behind it**, the
watchdog has nothing to inspect and never force-clears it (doctor's live check *would*
still catch this, since `SpawnQueueHealthMonitor` tracks `_spawn_in_progress` age
independently of queue depth — a manual `takkub restart` recovers it).

Traced whether this is reachable: both call sites that set `_spawn_in_progress = True`
(`spawn_engine.py:1198`, `:2495`) are immediately wrapped in `try/finally: self.
_spawn_in_progress = False; self._drain_spawn_queue()`. A `finally` block runs on every
exception path in Python; the only way to skip it is the interpreter dying outright (hard
kill, segfault) — at which point the whole app is gone, not merely wedged, and no
in-process self-heal could run anyway (the Qt event loop itself is dead). The original
incident's actual symptom — multiple subsequent `takkub assign` calls queuing up FIFO
behind the stuck flag — is exactly the case `_check_spawn_queue_stuck` already handles,
since Lead issuing another assign is what populates the queue for it to inspect.
Reproducing "stuck forever with zero future assigns" would need a path that doesn't exist
in the current code. Adding an unreachable-scenario branch to this hot path was judged not
worth the risk of a new bug in code this safety-critical — left alone.

## Recommendation

Close #170, #171, #172, #176 as duplicates of #139, #140, #141, #144 respectively
(already fixed in v1.0.42, verified still intact at v1.0.57 by this audit). Do not
touch `lead_inbox.py::_reap_pending_done_notices`'s draft-hold branch (unbounded by
design, per #118 — see `docs/audit/2026-08-13-cockpit-issues-163-165.md`) — unrelated
to this cluster and explicitly out of scope.
