# Performance & Reliability v2 — implementation report

Date: 2026-08-14

## Outcome

The supplied master-fix requirements are implemented as an integrated reliability layer. Existing
command shapes remain compatible while automated delivery, resource admission, PTY I/O, rendering,
process teardown, completion notices, settings, and runtime health are bounded or lifecycle-managed
and observable.

The compact Token Meter now follows the provider of the active Lead pane. A Codex Lead displays
`Codex` even when Claude has higher utilization or is the first provider returned. The popup is
screen-clamped, right-aligned, flips above near the bottom edge, and was captured at 100%, 125%,
150%, and 200% scaling plus a narrow window.

## Root causes addressed

- Automated task submission lacked a single delivery identity across retries and session restarts.
- A slow native PTY could accumulate stale writes without a strict capacity or final validator.
- Heavy work across projects had no shared CPU/RAM/class admission policy.
- Per-read parsing, transcript flush, ready classification, and rendering multiplied CPU work across
  many panes.
- Descendant cleanup relied on best-effort PID traversal without OS ownership on Windows.
- Completion events and Lead retries lacked durable, restart-safe idempotency.
- Runtime operators could not tune limits or see resource/writer/stall state inside the cockpit.
- Token Meter chose a provider by usage ordering rather than actual Lead ownership.

## Architecture and files

| Area | Main implementation |
|---|---|
| Delivery identity, TTL, generation, notice dedupe | `src/agent_takkub/task_delivery.py`, `lead_inbox.py`, `orchestrator.py` |
| Resource governor and durable presets | `resource_governor.py`, `performance_settings.py`, `settings_window.py`, `user_actions.py` |
| Bounded priority PTY writer and batching | `pty_session.py` |
| Adaptive rendering | `agent_pane.py` |
| Windows process ownership | `job_object_manager.py`, pane/spawn integration |
| Health UI and live diagnostics | `status_header.py`, `doctor.py`, `cli_server.py`, `app.py` |
| Lead-aware Token Meter and safe popup geometry | `limit_panel.py`, `usage_meter.py`, `main_window.py` |
| Deterministic stress and UI evidence | `tools/performance_reliability_stress.py`, `tools/capture_performance_ui.py` |

Performance settings use an atomic schema-v1 file with Safe, Balanced, and Maximum presets. The
default is machine-aware Balanced. Invalid/corrupt files fall back safely. Environment `TAKKUB_*`
values retain higher precedence for operational rollback. Live application updates governor limits
without losing active tokens or waiting work and updates hidden-pane cadence.

## Verification results

- Full pytest suite: exit 0 on three consecutive isolated runs:
  `full-suite-run-2.log` (282.2 s), `full-suite-run-3.log` (573.0 s), and
  `full-suite-run-4.log` (418.3 s).
- The formerly environment-sensitive CLI instance-banner test is included and passes; its home and
  discovery state are now isolated without stopping the operator's live cockpit.
- Final focused gate: 118 tests passed, including Settings, Token Meter, popup geometry, stress A–I,
  and two real Windows process-tree shutdown modes.
- Stress A–I: 10/10 cycles passed (five fixed-seed and five varied-seed runs).
- Latest varied-seed highlights:
  - 30/30 resource tasks admitted, max heavy 4, final active/queued 0/0.
  - UI heartbeat p95 10.751 ms, max 10.790 ms, predeclared SLA 250 ms.
  - Slow writer max depth 6 with cap 8, 94 full-policy activations, stale native writes 0.
  - One-second submit stall: payload writes 1, Enter retries 3.
  - Session replacement: old Task A writes 0.
  - Completion dedupe: 11 attempts, 1 delivered, 10 prevented, persisted reload passed.
  - Ten-project startup: max parallel native spawn 1, all 10 completed.
- Real Windows Job Object: root, child, and grandchild all terminated for pane close and application
  shutdown; PID snapshots are stored in the artifact directory.
- UI evidence: 20 screenshots across four scales cover right edge, bottom edge, narrow window,
  Performance Settings, and Health Status.
- Ruff check and format check passed; import-linter kept 24 contracts with 0 broken.
- sdist and wheel built. A fresh wheel-install smoke printed version `1.0.59`, preset `balanced`, and
  machine-aware heavy limit `4`.

Package hashes from the verified build:

- wheel SHA-256: `BD89838F41F76A3226EFA7C064F33E4B006C07423179B518E8FCEF387A93215C`
- sdist SHA-256: `B02CC7D424168701AF856CE6E20865ACCD87ED8B3D63BB9D5FB31AD19238203D`

## Production isolation

Verification used an isolated application home/runtime, `TAKKUB_ALLOW_MULTI=1`, offscreen Qt, and
disabled warmup/native-browser startup. It never connected to, restarted, killed, or reused the
running prod cockpit. Editing source files does not mutate code already loaded into that process.
Prod adopts this candidate only when the operator later installs/restarts it.

## Evidence

- Traceability: `docs/performance-reliability-v2-traceability.md`
- Adversarial audit: `docs/performance-reliability-v2-adversarial-audit.md`
- Operational guide: `docs/performance-reliability.md`
- Runtime bundle: `$TAKKUB_ARTIFACTS_DIR/final-evidence/`

## Remaining qualification

This evidence supports high confidence on the tested Windows/Python/Qt configuration, not a literal
promise of zero bugs for every future user. A real Linux/macOS process-group run is still required
on those target OSes. Also, the supplied ZIP includes specifications only, not a pre-fix source
snapshot, so an exact same-hardware historic before/after benchmark cannot be produced honestly.

## Rollback

No project data schema was changed. Stop/restart into the previous package to roll back code. For a
less aggressive candidate, select Safe or lower limits in Performance settings. Environment values
override the saved preset. Deleting the performance settings file restores machine-aware Balanced.
Deleting `runtime/notice-dedupe.json` is safe but may allow one previously seen completion notice to
be accepted again. Do not disable generation, TTL, writer bounds, or Job Object ownership merely to
increase throughput; those are correctness barriers.

The build still emits pre-existing setuptools metadata deprecation/package-discovery warnings. Both
artifacts are valid; cleaning those warnings is outside this reliability change.
