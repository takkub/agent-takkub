# Handoff to Lead — Performance & Reliability v2

Date: 2026-08-14
Source request: `agent-takkub-performance-reliability-master-fix.zip`
Working tree: intentionally uncommitted; Lead owns review/commit/push.

## Outcome

Reliability v2 is implemented and verified on the current Windows host. The change adds delivery
identity/session guards, resource admission, a bounded priority PTY writer, batched terminal work,
adaptive rendering, Windows Job Object ownership, durable completion dedupe, Performance Settings,
cockpit Health UI, and a Lead-aware Token Meter.

The Token Meter now resolves the provider attached to the active Lead pane. When Lead is Codex the
compact meter says Codex, even if Claude utilization is higher or Claude appears first in provider
usage results. Popup geometry is clamped to the active screen, right-aligned, and flips above near
the bottom edge.

## Verification completed

- Full pytest suite passed three consecutive isolated runs:
  - `full-suite-run-2.log`: exit 0, 282.2 s
  - `full-suite-run-3.log`: exit 0, 573.0 s
  - `full-suite-run-4.log`: exit 0, 418.3 s
- Final focused gate: 118 tests passed.
- Deterministic stress scenarios A–I: 10/10 cycles passed, using fixed and varied seeds.
- Latest stress metrics:
  - 30/30 resource tasks completed; final active/queued = 0/0.
  - heartbeat p95 10.751 ms, max 10.790 ms; declared SLA 250 ms.
  - slow-writer max depth 6 with cap 8; stale native writes 0.
  - one-second submit stall produced exactly one payload write.
  - replacement session received zero writes from old Task A.
  - completion dedupe delivered 1 of 11 attempts and survived store reload.
  - ten-project startup kept maximum native-spawn concurrency at 1.
- Real Windows Job Object test passed for pane close and application shutdown; root, child, and
  grandchild PIDs were all gone by the deadline.
- UI evidence: 20 screenshots at 100%, 125%, 150%, and 200%, covering Token Meter right/bottom edge,
  narrow window, Performance Settings, and Health UI.
- Ruff check/format passed; import-linter kept 24 contracts with 0 broken.
- sdist/wheel build and isolated wheel import smoke passed for version 1.0.59.

## Main implementation areas

- `src/agent_takkub/task_delivery.py`: delivery IDs, TTL, generation validation, single-flight,
  durable notice dedupe.
- `src/agent_takkub/resource_governor.py`: machine-aware global/project/class limits, non-blocking
  CPU/RAM hysteresis, fair waiting and cancellation.
- `src/agent_takkub/performance_settings.py`: schema-v1 Safe/Balanced/Maximum settings with atomic
  persistence, validation, corrupt-file fallback, and environment precedence.
- `src/agent_takkub/pty_session.py`: bounded priority writer, reserved control capacity, final native
  validator, output/transcript batching and telemetry.
- `src/agent_takkub/job_object_manager.py`: Windows kill-on-job-close ownership.
- `src/agent_takkub/orchestrator.py`, `lead_inbox.py`, `spawn_engine.py`, `agent_pane.py`: integration,
  lifecycle cleanup, state exposure, stall context, dedupe, serialized spawn, adaptive rendering.
- `src/agent_takkub/settings_window.py`, `status_header.py`, `user_actions.py`: Performance Settings
  and live Health UI.
- `src/agent_takkub/limit_panel.py`, `usage_meter.py`, `main_window.py`: Lead-aware Token Meter and
  safe popup placement.
- `tools/performance_reliability_stress.py`: deterministic A–I stress harness.
- `tools/capture_performance_ui.py`: reproducible offscreen UI evidence.
- `tools/build_performance_evidence_bundle.py`: final bundle and SHA-256 manifest.

## Evidence and review entry points

- Final bundle:
  `runtime/exports/2026-08-14/agent-takkub/final-evidence/manifest.json`
- Implementation report:
  `docs/performance-reliability-v2-implementation-report.md`
- Full checklist traceability:
  `docs/performance-reliability-v2-traceability.md`
- Adversarial audit and residual risks:
  `docs/performance-reliability-v2-adversarial-audit.md`
- Operational guide:
  `docs/performance-reliability.md`
- Built wheel:
  `dist/agent_takkub-1.0.59-py3-none-any.whl`

## Production isolation

The operator had prod running throughout. Tests used an isolated application home/runtime,
`TAKKUB_ALLOW_MULTI=1`, offscreen Qt, and disabled warmup/native-browser startup. They did not stop,
restart, connect to, or reuse the prod cockpit. Source edits do not alter code already loaded by the
running prod process; adoption occurs only after a later install/restart.

## Review requests for Lead

1. Review the complete dirty diff; do not assume `git diff --stat` includes untracked new files.
2. Confirm task-delivery integration paths still use Enter-only recovery and the final writer
   validator.
3. Review the governor release/cancel paths and Job Object close ordering.
4. Inspect Token Meter screenshots and verify Lead-provider resolution against real panes.
5. Decide whether product policy needs a hard rejection cap for low-rate resource/spawn/fan-out/Lead
   orchestration backlogs. High-rate PTY/render/transcript paths are bounded, but these control queues
   are workload-derived and observable rather than silently dropping accepted work.
6. Review remaining qualifications before using the phrase “100%”: real Linux/macOS process-group
   execution has not run on this Windows host, and the supplied ZIP contains specifications only—not
   a pre-fix source snapshot for an exact historical before/after benchmark.
7. If accepted, commit/push under Lead ownership, wait for current prod jobs to finish, install the
   verified wheel, restart once, then smoke-check Token Meter and `takkub doctor --live`.

## Package hashes

- wheel SHA-256: `BD89838F41F76A3226EFA7C064F33E4B006C07423179B518E8FCEF387A93215C`
- sdist SHA-256: `B02CC7D424168701AF856CE6E20865ACCD87ED8B3D63BB9D5FB31AD19238203D`

## Honest release statement

The evidence supports high confidence for the tested Windows/Python/Qt configuration. It does not
support a literal promise that every current or future user will encounter zero bugs. Cross-platform
release CI and post-restart prod smoke remain the final operational checks.
