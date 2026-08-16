# #274 — Performance Health said "cpu_high" at CPU 29%

## Symptom

Dialog showed a self-contradicting state:

```
System Load: OVERLOAD — new heavy work paused
CPU: 29.1% · Available RAM: 21.5% (8.5/39.6 GiB) · Resource queue: 2 (waiting: cpu_high)
status bar: SYS OVERLOAD · CPU 39% · RAM 21%
```

User: "อะไรคือสูง" — CPU was nowhere near any threshold, yet the UI blamed it.

## Root cause

`resource_governor.py::_denial_reason()` (pre-fix):

```python
if self._overloaded:
    if self._available_ram_percent < self.limits.min_available_ram_percent:  # 20%
        return "memory_low"
    return "cpu_high"          # unconditional fallback — never reads self._cpu_percent
```

`sample()`'s hysteresis (unchanged by this fix, lines ~290-300):

- **enters** overload when `cpu >= cpu_pause_percent (85)` **or** `ram < min_available_ram_percent (20)`
- **exits** only when `cpu <= cpu_resume_percent (65)` **and** `ram >= resume_ram_percent (25)` (both, not either)

In the reported case, RAM had tripped the latch earlier, then recovered to 21.5% — above the 20% *pause* line but still below the stricter 25% *resume* line. The first `if` was therefore false (21.5 is not `< 20`), so the function fell through to the unconditional `return "cpu_high"`, regardless of CPU's actual value (29%, nowhere near its own 85%/65% thresholds).

The bug was a **reporting** bug, not a hysteresis bug: the latch itself was doing the right thing (waiting for RAM to clear its resume threshold); only the human-readable reason string was wrong.

## Fix

`src/agent_takkub/resource_governor.py`

1. New `_overload_state_reason()`: checks each metric against **its own pause threshold** directly instead of RAM-then-fallback:
   ```python
   if self._cpu_percent >= self.limits.cpu_pause_percent:
       return "cpu_high"
   if self._available_ram_percent < self.limits.min_available_ram_percent:
       return "memory_low"
   return "waiting_resume"   # new third state
   ```
   `waiting_resume` covers the case proven above: the latch is held, but neither metric is over *its own* pause line right now — it's waiting on the (stricter) resume side of the hysteresis gap.
2. `_denial_reason()` now delegates to `_overload_state_reason()` instead of duplicating the buggy inline logic.
3. `snapshot()` gains `overload_reason` (`""` when not overloaded) — the system-wide reason, independent of any one queued task's resource class, so the UI can explain the OVERLOAD banner directly instead of inferring it from a per-task queue reason.

No pause/resume threshold values were changed (85/65/20/25) — this is a reporting fix only, per the issue's explicit constraint.

`src/agent_takkub/status_header.py` — `_show_performance_health_dialog()`:

- Labels the OVERLOAD line with the actual reason (`CPU above pause threshold` / `RAM below pause threshold` / `waiting to clear resume thresholds`) pulled from `overload_reason`.
- Adds a `Thresholds: CPU pause ≥85% / resume ≤65% · RAM pause <20% / resume ≥25%` line so the user can see the hysteresis band being applied, not just the raw OVERLOAD word.
- When overloaded, adds `Needed to resume: ...` listing exactly which metric(s) haven't cleared their resume line yet and by how much (e.g. `RAM ≥25% (now 21.5%)`).

## Tests

`tests/test_resource_governor.py` (new):
- `test_request_slot_reports_cpu_high_only_when_cpu_actually_over_pause` — CPU 90% → `cpu_high`
- `test_request_slot_reports_memory_low_when_ram_actually_under_pause` — RAM 15% → `memory_low`
- `test_request_slot_reports_waiting_resume_when_latched_but_neither_metric_over_pause` — reproduces the exact field values (CPU 29%, RAM 21.5%, latched via an earlier RAM dip) → `waiting_resume`, asserts it is **not** `cpu_high`
- `test_request_slot_unblocks_once_both_resume_thresholds_clear` — both metrics past their resume line → `overloaded` clears, reason `""`, slot admitted

`tests/test_performance_health_chip.py` (new):
- `test_health_dialog_does_not_blame_cpu_when_ram_is_the_actual_latch` — feeds the #274 field snapshot through `_show_performance_health_dialog()`, asserts the rendered text never says `cpu_high`/"CPU above pause threshold", does say "waiting to clear resume thresholds", includes the thresholds line, and includes `Needed to resume: RAM ≥25% (now 21.5%)`

Run (targeted, not full suite per test-tier policy):
```
PYTHONPATH=src python -m pytest tests/test_resource_governor.py tests/test_performance_health_chip.py tests/test_resource_queue_visibility.py tests/test_doctor_performance_live.py -q
```
Result: 32 passed.

## Scope not touched

- Pause/resume threshold *values* — unchanged, per issue constraint. If the thresholds themselves are judged wrong, that's a separate issue.
- `_marker_signals`/`classify_resource` and the queue/backoff machinery — untouched, unrelated to the reason string.
- Multi-provider: N/A (no provider-specific behavior in this code path). Cross-platform: `resource_governor.py` uses `psutil` only, no platform branching; fix is pure Python logic, verified via injected `sampler` callable (no real OS calls in tests) so it is platform-agnostic by construction.
