# #201 fix — backoff (from #195) starved freed slots

## Root cause

`resource_governor.py::dispatch_waiting()` (issue #195) skips a queue head
whose `next_retry_at` is in the future, without calling `request_slot`, so
denied items retry on a `1s/2s/5s/15s` backoff schedule instead of flooding a
`resource_gate_block` line every tick.

The backoff was **purely time-based**. It had no notion of "capacity just
changed" — it only ever asked "has enough wall-clock time passed since I was
last denied?". `release_slot()` frees a slot without touching any queued
item's `next_retry_at`, so a slot freed a millisecond after a denial sat idle
until that item's own backoff timer caught up — up to 15s worst case.

`tools/performance_reliability_stress.py::scenario_a` (STR-A) enqueues 30
heavy tasks across 10 projects (global cap 4, per-project cap 2), then does
`while active: release_slot(token); dispatch_waiting()` in a tight loop. Real
wall-clock time between iterations is sub-millisecond, so after the first
round only 5 of 30 tasks ever got admitted before the loop drained `active`
— the other 25 stayed queued behind a 1s backoff that never had a chance to
elapse relative to the loop's own speed.

## Before

```
FAILED tests/test_performance_stress_harness.py::test_deterministic_stress_harness_a_through_i
STR-A metrics: {'admitted': 5, 'max_heavy': 4, 'max_per_project': 2, 'final_active': 0, 'final_queued': 25}
```
(expected: `admitted=30, final_queued=0`)

## Fix

Added a `_capacity_epoch` counter on `ResourceGovernor`, bumped whenever a
slot is actually freed (`release_slot`) or limits change (`update_limits`).
Each `QueuedTask` now records the epoch value at the moment its backoff was
set (`retry_epoch`). `dispatch_waiting()`'s skip condition becomes:

```python
if item.next_retry_at > now and item.retry_epoch == epoch:
    continue
```

So a queue head is only held back by the time-based backoff while capacity
has genuinely not changed since that backoff was computed. Any release (or
limit update) invalidates every backed-off item and lets them retry
immediately on the very next `dispatch_waiting()` call — no waiting on the
clock for an event that already happened.

Also added an optional `now: float | None = None` keyword to
`dispatch_waiting()` per the issue's ask, so callers (tests, or a future
stress harness variant) can fast-forward past a backoff deterministically
without a real sleep — not required for STR-A to pass (the epoch fix alone
is sufficient and deterministic), but useful for constructing narrower unit
tests.

`ponytail:` the epoch is a single global counter, not scoped per
project/resource-class — coarser than strictly necessary, so an unrelated
release can trigger one futile retry+log for a queue head whose actual
blocking condition didn't change. Bounded by the real rate of release
events (not wall-clock ticks), so it does not reproduce the original #195
flood. Upgrade path if this proves noisy in a busy cockpit: track a
per-resource-class (and/or per-project) epoch instead of one global counter.

## After

```
tests\test_resource_governor.py ...........                              [ 91%]
tests\test_performance_stress_harness.py .                               [100%]
============================= 12 passed in 2.20s ==============================
```

STR-A metrics: `admitted=30, max_heavy<=4, max_per_project<=2, final_active=0, final_queued=0` — assertion strictness unchanged from the original scenario (no expected values were relaxed).

## Regression coverage (#195 goal preserved)

`test_gate_block_backoff_reduces_retry_frequency` (pre-existing, unmodified)
already pins the "no flood while capacity is static" requirement: a single
blocked item across 10 one-second ticks with zero `release_slot()` calls
still produces exactly 4 `resource_gate_block` lines (1s/2s/5s/15s cadence),
because the capacity epoch never advances when nothing is released.

Two new tests added to `tests/test_resource_governor.py`:
- `test_freed_slot_admits_backed_off_item_without_waiting_for_backoff_issue_201`
  — reproduces the #201 scenario in miniature (denial sets backoff, release
  happens at the same clock tick, must admit immediately). Fails against the
  pre-fix code (`len(admitted) == 0`).
- `test_dispatch_waiting_accepts_injectable_now` — confirms the new `now=`
  kwarg works for deterministic fast-forwarding.

## Files changed

- `src/agent_takkub/resource_governor.py` — capacity epoch, `retry_epoch`
  field, `dispatch_waiting(now=...)` kwarg.
- `tests/test_resource_governor.py` — 2 new regression tests.

## Verification run (this worktree)

```
.venv\Scripts\python.exe -m pytest tests/test_performance_stress_harness.py tests/test_resource_governor.py -v
```
12 passed. Full suite deferred to qa's batch gate per project convention.

Note: this worktree had no `.venv` provisioned yet — created via
`uv sync --frozen --extra dev` before running tests (needed because
`test_deterministic_stress_harness_a_through_i` subprocess-execs
`<worktree_root>/.venv/Scripts/python.exe` directly).
