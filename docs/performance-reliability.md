# Performance & Reliability v2

Agent-Takkub applies resource admission before heavy work, binds automated
terminal delivery to a concrete pane session, and bounds every PTY writer
queue. The goal is stable multi-project throughput: work waits visibly when
the host is saturated instead of starting until CPU, RAM, terminal queues, or
child-process count become failure multipliers.

## Runtime flow

```text
assign / pipeline
      |
      v
ResourceGovernor -- CPU/RAM hysteresis + global/project/class limits
      |
      v
DeliveryManager -- delivery_id + task_id + TTL + session generation
      |
      v
pane/session single-flight
      |
      v
bounded priority PTY writer -- control > user > task > background
      |
      v
Claude / Codex / Gemini process tree -- Windows Job Object + PID-tree fallback
```

PTY output takes a separate path. The reader batches raw bytes before feeding
pyte and the Qt render signal. Transcript writes remain authoritative but flush
on a time/size boundary instead of on every native read. Visible panes render
at interactive cadence; hidden panes retain output while rendering less often.

## Safety invariants

- A pane session has at most one delivery in `WRITING`, `SUBMITTING`, or
  `UNCERTAIN`.
- A queued write whose session generation no longer matches is dropped before
  the native PTY write.
- Expired automated messages never reach the PTY.
- Automated production paths retry Enter only. They do not automatically paste
  the full task/notice body a second time when evidence is ambiguous.
- PTY writer queues are bounded and reserve capacity for control input.
- Completion notices derive a durable idempotency ID from project, role, task,
  and completion generation.
- Closing a pane releases its resource token, cancels waiting work, invalidates
  delivery state, closes its Windows Job Object, and retains the existing
  PID-scoped tree-kill fallback.
- ConPTY construction remains serialized/staggered by the existing spawn
  arbiter. Resource admission controls work after/beside that arbiter; it does
  not make native spawn fully parallel.

## Resource classes and fairness

`LIGHT` and `NORMAL` traffic is admitted without consuming a heavy slot.
`HEAVY`, `BROWSER`, `BUILD`, `TEST`, and `PACKAGE_INSTALL` work uses global,
per-project, and class-specific limits. Waiting work is scheduled round-robin
between projects and FIFO within each project. Closing a pane/project cancels
its waiting entries.

CPU sampling uses `psutil.cpu_percent(interval=None)` and never sleeps on the Qt
thread. Admission pauses at the CPU/RAM pause thresholds and resumes only after
the lower/higher resume thresholds are reached, preventing gate oscillation.
Already-running work is not killed merely because load rises.

## Configuration

The cockpit Performance page persists a schema-v1 preset and its expanded values atomically.
Balanced is the machine-aware default; Safe reduces pressure when prod is already running, while
Maximum raises throughput without removing CPU/RAM guards. Environment variables remain supported
and take precedence, so operations can override or roll back settings individually:

| Variable | Default |
|---|---:|
| `TAKKUB_MAX_HEAVY_GLOBAL` | machine-aware (2/4/6) |
| `TAKKUB_MAX_HEAVY_PER_PROJECT` | machine-aware (1/2/3) |
| `TAKKUB_MAX_BROWSER_GLOBAL` | machine-aware (1/2/3) |
| `TAKKUB_MAX_BUILD_GLOBAL` | 2 |
| `TAKKUB_MAX_TEST_GLOBAL` | 2 |
| `TAKKUB_MAX_PACKAGE_INSTALL_GLOBAL` | 1 |
| `TAKKUB_CPU_PAUSE_PERCENT` | 85 |
| `TAKKUB_CPU_RESUME_PERCENT` | 65 |
| `TAKKUB_MIN_AVAILABLE_RAM_PERCENT` | 20 |
| `TAKKUB_RESUME_RAM_PERCENT` | 25 |
| `TAKKUB_TASK_DELIVERY_TTL_SEC` | 30 |
| `TAKKUB_PTY_WRITER_QUEUE_MAX` | 128 |
| `TAKKUB_PTY_CONTROL_RESERVE` | 8 |
| `TAKKUB_PTY_BATCH_MS` | 50 |
| `TAKKUB_PTY_BATCH_BYTES` | 65536 |
| `TAKKUB_TRANSCRIPT_FLUSH_MS` | 200 |
| `TAKKUB_TRANSCRIPT_FLUSH_BYTES` | 65536 |
| `TAKKUB_VISIBLE_RENDER_MS` | 16 |
| `TAKKUB_HIDDEN_RENDER_MS` | 300 |

## Diagnostics

Run:

```powershell
takkub doctor --live
```

The performance finding shows CPU, free RAM, active heavy jobs, work waiting
for resources, maximum pane writer-queue depth, and stale writes dropped. The
existing spawn-queue and remote-mirror live findings remain separate so a
native spawn wedge is not confused with intentional resource waiting.

Structured events include resource gate allow/block/acquire/release, delivery
creation/write/submit/accepted/uncertain/expired/cancelled, writer queue full or
stale drops, and completion-notice dedupe.

## Migration and rollback

No persisted project schema changes are required. New durable state consists of the global
performance settings file and `runtime/notice-dedupe.json`. A missing, partial, or corrupt
performance file falls back to machine-aware Balanced; deleting it is a safe reset. Deleting the
dedupe file is safe but permits an already-seen completion notice to be accepted once more. Task
delivery and governor runtime state is in-memory and is rebuilt on restart.

To reduce throttling, use Performance Settings or raise the relevant environment override. Saved
settings apply live to the running candidate; environment changes require a restart. Avoid disabling
session-generation, TTL, queue bounds, or Job Object ownership: those are correctness barriers rather
than tuning knobs.
Code rollback consists of reverting the Reliability v2 modules/integration;
existing task handoff files, transcripts, spawn arbitration, and tree-kill
fallbacks remain backward compatible.
