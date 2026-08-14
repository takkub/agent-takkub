# Performance & Reliability v2 — adversarial audit

Date: 2026-08-14

The audit used direct source search in addition to tests. Raw inventories are stored in
`$TAKKUB_ARTIFACTS_DIR/verification/adversarial-search.txt` and `queue-inventory.txt`.

## Automated PTY delivery

The automated task path creates a delivery with project, pane, task, generation, TTL, and a unique
delivery ID. The pane/session single-flight map rejects a second active submit. Enqueued writer
messages carry generation, expiry, delivery ID, and priority. Immediately before the native PTY
write, the writer checks generation, TTL, cancellation, and the delivery validator again.

Production task paths disable automated body repaste. Recovery can send Enter again, but does not
create a new delivery or paste the payload again. STR-E deliberately stalls the event loop for one
second: three Enter attempts still produce exactly one payload write. STR-G invalidates a queued
generation and proves zero writes enter the replacement session.

Direct `session.write` calls remain for user input and cockpit control/status messages. They use the
same bounded native writer, but they do not pretend to be automated task deliveries. This separation
is intentional: user keystrokes and process-control input need writer priority rather than task TTL.

## Queue ownership and backpressure

| Queue/buffer | Owner | Bound/policy | Cancellation/stale policy | Observability |
|---|---|---|---|---|
| PTY native writer | `PtySession` | max 128 by default; control reserve 8 | generation/TTL/delivery validator at dequeue | depth, full, stale, output rate |
| PTY reader batch | reader worker | 64 KiB or 50 ms flush | session termination stops reader | batch metrics/stress result |
| Render coalescer | `AgentPane` | byte cap with immediate force flush; cadence timer | cleared/flushed on detach | heartbeat/render tests |
| Transcript buffer | `PtySession` | 64 KiB or 200 ms flush | final flush/close on terminate | transcript tests |
| Resource wait | `ResourceGovernor` | work-derived, round-robin by project | pane/project/task cancellation | waiting rows/count/age events |
| ConPTY spawn FIFO | `SpawnEngine` | serialized and naturally limited by requested pane starts | stuck-head escape hatch and close lifecycle | depth/oldest age/doctor |
| Fan-out persistence | `Orchestrator` | optional, default off, machine pane admission | drains one per released slot; durable file | queue depth/events |
| Lead inbox/digest | `lead_inbox` | deduped/digested control traffic | generation-checked timers and durable done dedupe | dedupe count/events |

The bounded-queue acceptance concern in the supplied master specification is the high-rate PTY
writer (`queue.Queue()` in the old design). That path is now strictly bounded and non-blocking. The
resource, spawn, fan-out, and Lead queues are low-rate orchestration queues and do not contain raw PTY
output. They remain workload-derived rather than having an arbitrary hard rejection cap; their
depths are now visible. A future hard cap would require an explicit product policy for rejecting or
persisting legitimate user assignments, not a silent `deque(maxlen=...)` that loses work.

## Governor tokens

Token acquisition is centralized. Normal close, failure, role completion, pane close, and project
close paths release idempotently; queued work is cancelled by pane/project identity. Live limit
updates retain already-owned tokens and the fair waiting queue. STR-A admits all 30 tasks while
respecting global/per-project limits and ends with active=0, queued=0.

## Timers and retries

Delivery timers capture pane/session generation or act only on the delivery ID. Old callbacks fail
validation. Digest timers use a monotonically increasing generation because `singleShot` cannot be
cancelled. Spawn retry and task-submit retry remain distinct. The production submit path retries
Enter only and cannot blindly reconstruct the body.

## Completion notices

Done notices derive a stable ID from project, role, task, and completion generation. `NoticeDeduper`
persists accepted IDs atomically, prunes by TTL, and exposes duplicates prevented. STR-H invokes the
same completion 10 extra times and reloads the store: one delivery, ten duplicates prevented.

## Process ownership

On Windows, each pane owns a Job Object configured with
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`; the pane process is assigned immediately after spawn. Pane
close closes the job before the existing PID-scoped tree fallback. The real integration test creates
root → child → grandchild and proves all three PIDs disappear for both pane close and application
shutdown. No global kill-by-name command was added.

Non-Windows uses the existing process-group behavior; `JobObjectManager` is a tested safe no-op.
This host cannot provide real Linux/macOS process evidence, so cross-platform release pipelines
should run the corresponding process-group integration test on each target OS.

## Qt critical paths

- CPU sampling uses `psutil.cpu_percent(interval=None)`; there is no sampling sleep.
- Queue-full handling returns/evicts immediately and never waits on Qt.
- Native PTY writes run in the writer worker.
- PTY parsing, transcript I/O, ready classification, and rendering are batched/throttled.
- The watchdog records active panes, output rate, writer depth, spawn state, and active heavy work.

STR-B recorded heartbeat p95 10.751 ms and max 10.790 ms against the predeclared 250 ms SLA in the
latest varied-seed run. STR-E's one-second event-loop stall is intentional fault injection and does
not duplicate delivery.

## Residual risks and decisions

1. No finite test campaign can prove absence of every defect for every user, provider version, OS,
   driver, and hardware combination.
2. Real Windows ownership is proven; Linux/macOS real process-group execution remains a release-CI
   requirement rather than a result from this Windows host.
3. The supplied ZIP has no pre-fix source snapshot. Candidate metrics are real, but an exact numeric
   historical before/after comparison would be fabricated and is therefore not reported.
4. Low-rate orchestration queues are observable and lifecycle-managed but not hard-rejection capped.
   If product requirements demand a global assignment backlog cap, its reject/persist UX must be
   defined first so accepted work is never silently discarded.
5. The currently running prod process has not been restarted or killed. It continues executing its
   already-loaded code; these changes take effect only after the operator installs/restarts later.
