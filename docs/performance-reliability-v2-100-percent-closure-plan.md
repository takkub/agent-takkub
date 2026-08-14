# Performance & Reliability v2 — 100% closure plan

Date: 2026-08-14

## What “100%” means

This plan treats 100% as **requirements closure with reproducible evidence**, not a claim that
software can never fail. Completion may be reported only when every row in
`FINAL_ACCEPTANCE_CHECKLIST.md` has an implementation reference, a repeatable verification,
an evidence artifact, and a passing result. No P0/P1 item may be waived silently.

## Current position

The core delivery, resource-governor, bounded PTY writer, batching, adaptive rendering,
Job Object integration, live doctor telemetry, documentation, unit tests, regression suite,
and package build are implemented. Three evidence groups remain incomplete:

1. Performance Settings UI and in-cockpit Health Status UI.
2. The master plan's stress scenarios A–I and before/after benchmark evidence.
3. Real process-tree termination evidence using root → child → grandchild processes on Windows
   (plus process-group coverage on Linux/macOS where supported).

## Gate 1 — Traceability matrix

Create a checked-in matrix with one row per item from all four supplied documents:

| Field | Required content |
|---|---|
| Requirement ID | Stable ID, e.g. `DEL-01`, `RES-04`, `STR-07` |
| Source | File and section/checklist line |
| Implementation | File and symbol |
| Automated test | Exact node ID |
| Real-system test | Script/scenario where needed |
| Pass criterion | Numeric or exact invariant |
| Evidence | Artifact path and run ID |
| Result | PASS/FAIL/BLOCKED only |

Any row without all applicable fields is not closed.

## Gate 2 — Finish product-visible gaps

### Performance Settings UI

Implement persisted settings for:

- Safe / Balanced / Maximum presets, default Balanced.
- Global and per-project heavy limits.
- Browser/build/test/package-install limits.
- CPU pause/resume and RAM pause/resume thresholds.
- Hidden-pane render cadence.

Tests must prove persistence, preset expansion, invalid-value handling, live application of new
values, and rollback to defaults.

### Health Status UI

Add a cockpit-visible status surface showing:

- system load, CPU, available/total RAM, and process count;
- active heavy/browser/build/test work versus limits;
- resource wait count and spawn queue depth;
- per-pane writer depth, stale drops, and queue-full count;
- duplicate notifications prevented and main-thread stall metrics;
- an explicit overload banner such as `Resource protection active`.

`takkub doctor --live` remains the CLI diagnostic, but does not substitute for the requested UI.

### Token Meter UI

Treat the multi-provider Token Meter as part of the final UI gate. Its detail popup must:

- right-align to the meter instead of opening beyond the screen's right edge;
- remain inside the active screen's available geometry, including multi-monitor layouts;
- use a readable detail width and flip above the anchor when there is insufficient room below;
- keep all provider names, quota/reset text, loading/error states, and Thai text legible;
- remain usable at 100%, 125%, 150%, and 200% display scaling and at the minimum supported window size;
- close on outside click and never cover the meter in a way that prevents reopening it.

Capture screenshots for right-edge, bottom-edge, scaled-display, and narrow-window cases. Geometry
unit tests are necessary but do not replace the visual QA screenshots.

### State and telemetry completeness

Verify `WAITING_RESOURCE`, `SPAWNED_IDLE`, and `RUNNING` are externally visible and transitions
are logged. Main-thread stall events must include workload context: active panes, output rate,
writer depth, spawn state, and active heavy tasks.

## Gate 3 — Deterministic stress harness

Add a non-interactive harness under `tests/stress/` or `tools/` that emits JSON plus human-readable
summaries into `$TAKKUB_ARTIFACTS_DIR`. Every run records git SHA/diff state, OS, CPU/RAM,
configuration, seed, start/end time, and pass/fail criteria.

Required scenarios:

| ID | Scenario | Exact pass criterion |
|---|---|---|
| STR-A | 10 projects × 3 panes | Limits never exceeded; all admitted work eventually runs; no slot leak |
| STR-B | Multi-pane output flood | UI heartbeat stays within agreed SLA; hidden rendering throttles; writer depth never exceeds cap |
| STR-C | Synthetic CPU saturation | New heavy work enters wait; existing work continues; queued work auto-starts after hysteresis recovery |
| STR-D | Simulated/controlled low RAM | Heavy dispatch pauses; no crash, duplicate, or stale delivery; resumes only above resume threshold |
| STR-E | Qt event loop stalled 1–2 seconds during submit | Full payload write count remains exactly one |
| STR-F | Slow/blocked PTY writer | Queue remains ≤ configured max; expired/cancelled/old-generation writes never reach native PTY |
| STR-G | Session dies with Task A queued | Task A write count in replacement session is zero |
| STR-H | Same done event triggered 10 times | Lead receives exactly one completion notice, including after dedupe-store reload |
| STR-I | 8–10 projects start simultaneously | ConPTY spawn remains serialized/staggered; no collision; governor limits hold; UI remains responsive |

Run deterministic fault-injection tests in normal CI. Run the real 30-pane/output/process tests in
a dedicated stress job so ordinary unit tests remain fast and reliable.

## Gate 4 — Real process ownership tests

On Windows, spawn a unique test tree:

```text
test root → child process → grandchild process
```

Assign the root to the pane Job Object, close the pane, and prove all three PIDs are gone within an
agreed deadline. Repeat for application shutdown. Capture before/after PID snapshots. Do not use
global kill-by-name commands.

On Linux/macOS, run the corresponding process-group test. Unit tests mocking `kernel32` remain
useful but cannot close this gate alone.

## Gate 5 — Before/after benchmark

Run the same harness against the pre-fix baseline in an isolated worktree and the final candidate,
using identical hardware, configuration, workload, and random seed. Record:

- CPU and RAM peak/average;
- process count peak;
- UI heartbeat p50/p95/p99/max and stall count;
- spawn wait and resource wait distributions;
- writer maximum depth and queue-full count;
- duplicate deliveries, stale native writes, and duplicate notices;
- orphan descendant count after pane/project/app close.

Correctness thresholds are absolute: duplicate delivery = 0, stale native writes = 0, orphan
descendants = 0, resource-limit violations = 0, and queue depth never exceeds its configured cap.
Agree the UI heartbeat SLA before the run; do not choose it after seeing results.

## Gate 6 — Isolated regression and flake proof

1. Run every existing test, including the CLI instance-banner test, with an isolated port,
   `AGENT_TAKKUB_HOME`, runtime directory, and no operator cockpit discovery.
2. Run the full suite at least three consecutive times with zero failures.
3. Run stress scenarios repeatedly with fixed and varied seeds; recommended minimum is 10 local/CI
   cycles and 30 overnight cycles for timing-sensitive scenarios.
4. Run Ruff, formatting, import contracts, package build, and wheel-install smoke test.
5. Smoke-test Claude, Codex, Gemini, shards, pipelines, browser roles, terminal Unicode/Thai,
   resize, detach/attach, project switching, Token Meter popup placement, and application shutdown.

The previously deselected CLI test must pass in isolation; an environment caveat is not acceptable
for final 100% closure.

## Gate 7 — Adversarial audit

Perform and record an explicit audit for:

- every automated PTY write path: delivery ID, generation, TTL, priority, and final validator;
- every queue: owner, maximum size, full policy, cancellation, and observability;
- every governor token acquisition: release on success, exception, cancel, close, and shutdown;
- every retry/timer callback: captured session generation and no blind body repaste;
- every completion notice path: stable idempotency ID and durable dedupe;
- every process-spawn path: ownership and scoped teardown fallback;
- Qt critical paths: no blocking psutil sample, sleep, process wait, disk flush, or native PTY write.

Use real text search in addition to code-navigation output. Ranked/empty graph results are not proof
that a bypass path does not exist.

## Gate 8 — Evidence bundle and sign-off

Store a final run directory under `$TAKKUB_ARTIFACTS_DIR` containing:

- machine/config manifest;
- full-suite and stress logs;
- JSON metrics and before/after comparison;
- process-tree before/after snapshots;
- Health UI screenshots;
- traceability matrix with every row PASS;
- final adversarial-review report;
- package artifacts and smoke-test log.

Update the implementation report with root causes, files changed, architectural changes, tests,
commands, results, metrics, remaining risks, and rollback notes. Then mark every item in the supplied
acceptance checklist with its evidence link. Only at that point is “100%” supportable.

## Recommended execution order

1. Build the traceability matrix and define the UI heartbeat SLA.
2. Implement Settings UI, Health UI, and missing telemetry/state exposure.
3. Add the deterministic stress harness and process-tree integration test.
4. Run isolated regressions and fix all failures/flakes.
5. Run baseline/candidate benchmarks and real-system stress cycles.
6. Complete the adversarial audit and evidence bundle.
7. Check every acceptance row and issue final sign-off.

Estimated effort is roughly 1–3 focused engineering days, depending mainly on real 30-pane stress
runtime, Windows process-tree behavior, and any flakes uncovered. A passing single run is not enough;
repeatability is part of the acceptance bar.
