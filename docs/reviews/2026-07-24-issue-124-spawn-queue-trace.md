# Issue #124 — one-shot spawn delivery queue trace

Date: 2026-07-24

## Result

The observed `initial_delivery: "pointer"` events were not caused by the CLI
stagger timer, spawn gate, or FIFO drain losing `PaneState`. The affected
`backend` roles were configured to use Codex, whose provider specification has
no confirmed file-backed system-prompt flag. Pointer delivery is the intended
fallback for that provider.

The queue path is nevertheless hardened and covered by an integration test:
an accepted payload remains `pending` on its `PaneState` through deferred retry
and FIFO drain until native launch calls `_finish_spawn_initial_task()`.

## Trace before changes

1. `CliServer._dispatch()` receives `assign`, reserves a stagger slot with
   `_next_spawn_delay_ms()`, replies immediately, then schedules
   `Orchestrator.assign()` with `QTimer.singleShot(delay, ...)`.
2. `Orchestrator.assign()` optionally creates a worktree, then enters
   `_assign_dispatch()`.
3. `_assign_dispatch()` resolves the effective provider and materializes the
   pointer file. It stages `spawn_initial_task`, its fallback pointer, and
   state `"requested"` only when that provider exposes `system_prompt_flag`.
4. `spawn()` changes `"requested"` to `"pending"` before every gate/FIFO early
   return. Gate retry and FIFO entries carry only pane identity/launch
   parameters; the payload remains on the per-pane `PaneState`.
5. On the real Claude launch, `_prepare_spawn_system_prompt()` reads the pending
   payload and builds the pane-scoped prompt file. After `session.spawn()`,
   `_finish_spawn_initial_task(preloaded=True)` logs
   `spawn_initial_task_preloaded`, clears the payload, and sends only the tiny
   trigger. Prompt preparation/provider failure calls the same finisher with
   `preloaded=False`, preserving pointer fallback.

There was no source path between steps 4 and 5 that cleared a pending payload.
The only pending clears were native-launch success/fallback finalization or
native spawn failure.

## Runtime evidence

`runtime/events.log` at 12:21:50–12:22:11 shows the three reported backend
assignments:

- each has `spawn_native_ms` and `spawn` before its `assign` event;
- none has `spawn_deferred_gate`, `spawn_queued_fifo`, or `spawn_queue_drain`;
- all log `initial_delivery: "pointer"`.

The project-local provider mapping at the time was:

```json
{
  "backend": "codex",
  "mobile": "gemini",
  "reviewer": "codex",
  "maintainer": "gemini"
}
```

`PROVIDER_REGISTRY["codex"].system_prompt_flag` is `None`, while Claude owns
`"--append-system-prompt-file"`. Therefore `_assign_dispatch()` correctly did
not stage one-shot payloads for those backend panes.

The same log provides a counterexample to the queue hypothesis: the QA
assignment at 12:35:02, using Claude through the same asynchronous CLI dispatch,
logged both `spawn_initial_task_preloaded` and
`initial_delivery: "delivered"`.

## Changes

- `_retry_deferred_spawn()` and `_drain_spawn_queue()` now explicitly normalize
  any retained `"requested"`/`"pending"` task payload to `"pending"` without
  copying it out of `PaneState`.
- `assign` and `assign_plan` events now include `effective_provider` and
  `initial_delivery_reason`. In particular, pointer-by-capability is
  `"provider-unsupported (by design)"`, while an accepted preload that must
  degrade is `"fallback-after-fail"`. The dedicated pointer-fallback event
  carries the latter reason too.
- The integration test queues three real `Orchestrator.assign()` calls behind
  the spawn arbiter, releases the FIFO, processes actual zero-delay Qt timers,
  and requires one `spawn_initial_task_preloaded` event per pane.

Pointer delivery remains the fallback for running panes, unsupported providers,
prompt-file preparation failure, or a provider change before launch.
