# Performance & Reliability v2 — traceability matrix

Date: 2026-08-14

This matrix maps every row in the supplied `FINAL_ACCEPTANCE_CHECKLIST.md` to
implementation and reproducible evidence. `PASS` means the invariant was exercised on the
current Windows candidate. It does not mean that arbitrary future hardware, providers, or OS
versions can never expose a defect.

Evidence aliases:

- `FULL-2..4`: `$TAKKUB_ARTIFACTS_DIR/verification/full-suite-run-{2,3,4}.log`
- `STRESS`: `$TAKKUB_ARTIFACTS_DIR/verification/stress-10-cycles.log` and the matching
  `$TAKKUB_ARTIFACTS_DIR/performance-reliability/<run-id>/stress-results.json`
- `PROC`: `$TAKKUB_ARTIFACTS_DIR/process-tree-{pane_close,application_shutdown}-*.json`
- `UI`: `$TAKKUB_ARTIFACTS_DIR/ui-evidence/scale-{1_0,1_25,1_5,2_0}/*.png`
- `STATIC`: `$TAKKUB_ARTIFACTS_DIR/verification/{ruff-check-final,ruff-format-final,import-contracts}.log`

## P0 delivery safety

| ID | Requirement | Implementation | Automated/real verification | Criterion | Evidence | Result |
|---|---|---|---|---|---|---|
| DEL-01 | Unique `delivery_id` | `task_delivery.DeliveryManager.create` | `test_task_delivery_v2.py::test_single_flight_per_pane_session` | IDs differ per delivery | FULL-2..4 | PASS |
| DEL-02 | Project, pane, task, generation bound | `task_delivery.TaskDelivery` | `test_task_delivery_v2.py::test_old_generation_and_expired_delivery_never_write` | All identity fields retained and validated | FULL-2..4 | PASS |
| DEL-03 | One active submit/uncertain per pane/session | `DeliveryManager._active_by_session` | `test_single_flight_per_pane_session` | Second active delivery rejected | FULL-2..4 | PASS |
| DEL-04 | Old generation cannot write | `DeliveryManager.validate_native_write`; `PtyWriter` validator | `test_old_generation_and_expired_delivery_never_write`; `test_stale_generation_and_ttl_are_checked_at_native_write` | Native write count 0 | FULL-2..4, STRESS G | PASS |
| DEL-05 | Expired message cannot write | Delivery TTL and writer expiry check | Same tests as DEL-04 | Native write count 0 | FULL-2..4, STRESS F | PASS |
| DEL-06 | No automated full-payload repaste | orchestration delivery calls use `allow_repaste=False`; Enter retry remains | `test_retry_enter_never_changes_payload_or_creates_delivery` | Payload write count 1 | FULL-2..4, STRESS E | PASS |
| DEL-07 | Main-thread stall cannot repaste | delivery identity plus Enter-only retry | `test_deterministic_stress_harness_a_through_i` / STR-E | 1 payload after 1 s stall | STRESS | PASS |
| DEL-08 | Lead/done idempotency IDs | `task_delivery.NoticeDeduper.notice_id` | `test_notice_dedupe_is_durable_and_ttl_bounded` | Stable ID survives reload | FULL-2..4, STRESS H | PASS |
| DEL-09 | Duplicate completion produces one notice | `NoticeDeduper.accept` integrated in `lead_inbox` | same test; STR-H | 11 attempts, 1 delivered | STRESS | PASS |

## P0 resource protection

| ID | Requirement | Implementation | Automated/real verification | Criterion | Evidence | Result |
|---|---|---|---|---|---|---|
| RES-01 | Global governor | `resource_governor.ResourceGovernor` owned by `Orchestrator` | `test_global_per_project_and_class_limits_release_cleanly` | Single shared active-token registry | FULL-2..4 | PASS |
| RES-02 | Non-blocking CPU sample | `psutil.cpu_percent(interval=None)` | `test_cpu_and_memory_hysteresis_is_non_blocking` | No sleep/blocking interval | FULL-2..4 | PASS |
| RES-03 | RAM thresholds | `GovernorLimits.min_available_ram_percent/resume_ram_percent` | same test; STR-D | Pause low, resume above threshold | STRESS | PASS |
| RES-04 | Hysteresis | `ResourceGovernor.sample` overload latch | same test; STR-C/D | Does not resume inside hysteresis band | STRESS | PASS |
| RES-05 | Global heavy limit | `GovernorLimits.max_heavy_global` | `test_global_per_project_and_class_limits_release_cleanly`; STR-A | Active heavy never exceeds limit | STRESS | PASS |
| RES-06 | Per-project heavy limit | `max_heavy_per_project` | same test; STR-A | Project active never exceeds limit | STRESS | PASS |
| RES-07 | Browser/build/test limits | class-specific `_class_limit` | same test; settings tests | Each class respects configured cap | FULL-2..4 | PASS |
| RES-08 | Fair scheduling | project round-robin, project FIFO | `test_waiting_queue_is_round_robin_by_project` | Interleaved project admission | FULL-2..4 | PASS |
| RES-09 | Waiting cancellation | `cancel_waiting` on pane/project close | `test_cancel_waiting_on_pane_close` | Matching entries removed | FULL-2..4 | PASS |
| RES-10 | Slots released | close/error integration plus idempotent `release_slot` | `test_global_per_project_and_class_limits_release_cleanly`; launch failure/close regressions | Final active count 0 | FULL-2..4, STRESS A | PASS |

## P1 PTY queue and rendering

| ID | Requirement | Implementation | Automated/real verification | Criterion | Evidence | Result |
|---|---|---|---|---|---|---|
| PTY-01 | Writer queue bounded | `pty_session.BoundedPriorityWriter` | `test_writer_queue_is_bounded_and_reserves_control_capacity`; STR-F | Depth never exceeds cap | STRESS | PASS |
| PTY-02 | Control/user/task priorities | `WritePriority` and reserved control slots | `test_control_and_user_preempt_background_work` | Control/user drain first | FULL-2..4 | PASS |
| PTY-03 | Queue-full never blocks Qt | non-blocking enqueue/reject/evict | first queue test; STR-F | Immediate policy, no wait | STRESS | PASS |
| PTY-04 | Stale queued writes dropped | generation, TTL, cancellation validator immediately before native write | stale/cancelled writer tests; STR-F/G | Stale native writes 0 | STRESS | PASS |
| PTY-05 | Queue depth observable | `PtySession.writer_queue_depth`, performance status/doctor/Health UI | `test_performance_live_reports_governor_and_queue_metrics`; health chip test | Depth and full counters visible | FULL-2..4, UI | PASS |
| RND-01 | PTY parsing batched | reader 50 ms/64 KiB batch | `test_reader_batches_parser_and_render_delivery`; STR-B | Batches below byte cap | STRESS | PASS |
| RND-02 | UTF-8 incremental decode safe | terminal incremental decoder | `test_terminal_widget.py::TestIncrementalDecoder` | Split Thai/UTF-8 round trips | FULL-2..4 | PASS |
| RND-03 | Transcript writes buffered | timed/size buffered transcript | `test_pane_transcript.py` | Ordered bytes, flush/close safe | FULL-2..4 | PASS |
| RND-04 | Ready classification throttled | pane classification cadence/cache | full lifecycle/delivery regression suite | No per-byte classification regression | FULL-2..4 | PASS |
| RND-05 | Visible/hidden cadence separated | `AgentPane.apply_performance_settings` | settings tests; STR-B | 16 ms visible / 300 ms hidden defaults | STRESS | PASS |
| RND-06 | Hidden panes avoid render CPU | hidden render coalescing | STR-B | Hidden cadence is 300 ms | STRESS | PASS |
| RND-07 | Render buffer bounded | pane force-flush byte cap | `test_render_coalesce.py::TestRenderCoalescing::test_cap_triggers_immediate_flush` | Cap forces flush | FULL-2..4 | PASS |

## Process and spawn lifecycle

| ID | Requirement | Implementation | Automated/real verification | Criterion | Evidence | Result |
|---|---|---|---|---|---|---|
| PRC-01 | Windows Job Object manager | `job_object_manager.JobObjectManager` | `test_windows_job_assigns_process_and_closes_kill_on_close_handle` | Process assigned to owned job | FULL-2..4 | PASS |
| PRC-02 | Kill on job close | extended-limit flag set on handle | same test | `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` set | FULL-2..4 | PASS |
| PRC-03 | PID-tree fallback preserved | `AgentPane` close sequence | existing process cleanup regressions plus source audit | Job close precedes scoped fallback | FULL-2..4 | PASS |
| PRC-04 | Pane close kills descendants | Job Object close | `test_job_close_reaps_root_child_and_grandchild[pane_close]` | Root, child, grandchild gone by deadline | PROC | PASS |
| PRC-05 | App shutdown kills descendants | shared pane shutdown path | `test_job_close_reaps_root_child_and_grandchild[application_shutdown]` | All three PIDs gone | PROC | PASS |
| PRC-06 | No global kill-by-name introduced | scoped PID/job APIs only | adversarial `rg` audit | No new broad kill command | adversarial report | PASS |
| SPN-01 | Global spawn arbiter preserved | `spawn_engine._spawn_in_progress/_spawn_queue` | STR-I; spawn queue regressions | Max parallel ConPTY construction 1 | STRESS | PASS |
| SPN-02 | Spawn serialized/staggered | FIFO drain/timers | STR-I | 10 start/finish, max parallel 1 | STRESS | PASS |
| SPN-03 | Spawned state separate from heavy admission | `DeliveryState.SPAWNED_IDLE/WAITING_RESOURCE/RUNNING`; governor before assignment | stress/state telemetry tests | States separately observable | STRESS, UI | PASS |

## Tests

| ID | Requirement | Verification | Criterion | Evidence | Result |
|---|---|---|---|---|---|
| TST-01 | Existing tests pass | Entire pytest suite | Exit 0, three consecutive runs | FULL-2..4 | PASS |
| TST-02 | Governor tests | `tests/test_resource_governor.py` | All pass | FULL-2..4 | PASS |
| TST-03 | Delivery tests | `tests/test_task_delivery_v2.py` | All pass | FULL-2..4 | PASS |
| TST-04 | Queue tests | `tests/test_pty_writer_queue_v2.py` | All pass | FULL-2..4 | PASS |
| TST-05 | Notice dedupe | durable dedupe test; STR-H | One delivered | STRESS | PASS |
| TST-06 | Session restart | old-generation tests; STR-G | Replacement receives Task A zero times | STRESS | PASS |
| TST-07 | Process tree | real root/child/grandchild parametrized test | All descendants gone | PROC | PASS |
| TST-08 | CPU saturation | STR-C | Wait then recover | STRESS | PASS |
| TST-09 | Memory pressure | STR-D | Wait then recover | STRESS | PASS |
| TST-10 | PTY stall | STR-F | Depth 6 ≤ cap 8; stale writes 0 | STRESS | PASS |
| TST-11 | Event-loop submit stall | STR-E | Payload exactly once | STRESS | PASS |
| TST-12 | Multi-project startup | STR-I | 10/10 finish, parallel spawn 1 | STRESS | PASS |
| TST-13 | Duplicate delivery zero | STR-E/G/H and delivery tests | Full payload duplicates 0 | STRESS | PASS |

## UX, observability, and final audit

| ID | Requirement | Implementation/verification | Criterion | Evidence | Result |
|---|---|---|---|---|---|
| OBS-01 | Waiting state visible | performance status plus Health UI queue count/state rows | `test_health_chip_surfaces_load_limits_queues_and_reliability_metrics` | Visible in UI and structured status | UI, FULL-2..4 | PASS |
| OBS-02 | `takkub doctor --live` enhanced | `doctor.check_performance_live` | two doctor performance tests | Metrics and overload warning | FULL-2..4 | PASS |
| OBS-03 | Resource/queue/delivery events | structured `_log_event` sinks | component tests and source audit | Acquire/release/full/drop/state events emitted | FULL-2..4 | PASS |
| OBS-04 | Stall workload context | `Orchestrator.record_main_thread_stall` | stress manifest and source audit | Pane/output/writer/spawn/heavy context attached | adversarial report | PASS |
| OBS-05 | Documentation updated | reliability guide, implementation report, matrix, audit | File review | All requested operational sections present | checked-in docs | PASS |
| AUD-01 | No duplicate delivery path | DeliveryManager final validator and Enter-only retry | adversarial search plus STR-E/H | Duplicate payload/notice 0 | STRESS, adversarial report | PASS |
| AUD-02 | No stale writer path | generation/TTL/cancel validator at native boundary | STR-F/G | Stale native writes 0 | STRESS | PASS |
| AUD-03 | No unbounded high-rate PTY queue | bounded priority writer; render/transcript byte/time caps | queue inventory | Automated PTY/render growth has an explicit cap/flush policy | queue inventory, adversarial report | PASS |
| AUD-04 | No slot leak | idempotent release/cancel paths | STR-A and governor tests | Final active and waiting 0 | STRESS | PASS |
| AUD-05 | No owned orphan path | Job Object plus scoped tree fallback | real process test | Orphan descendants 0 | PROC | PASS |
| AUD-06 | No critical Qt-thread blocking path | non-blocking sampling/enqueue; worker PTY writes; batched render | adversarial search; STR-B/E/F | Heartbeat max under 250 ms except deliberate injected stall | STRESS, adversarial report | PASS |

## Platform and baseline qualification

- Windows 10/11 behavior is covered by real Job Object and descendant tests on the current host.
- Non-Windows Job Object behavior is a tested safe no-op, while the existing POSIX process-group
  implementation remains in place. A real Linux/macOS run is still required for platform-specific
  release certification.
- The supplied ZIP contains specifications only, not a pre-fix source snapshot. Therefore an exact
  same-hardware before/after numeric benchmark cannot be reconstructed from that ZIP. Candidate
  correctness and latency metrics are reproducible; a historical baseline comparison is explicitly
  not claimed.
