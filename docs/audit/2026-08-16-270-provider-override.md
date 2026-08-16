# #270 — Lead has no escape hatch when a role's provider is boot-stalled/broken

## Symptom (real incident, saas_admin, 2026-08-16)

A codex pane stalled at boot ("Starting MCP servers (1/2): context7 …") and
never reached a ready prompt, for both `backend` and `frontend` (both
mapped to codex). Lead tried to force the role onto claude instead:

```
takkub assign --role backend --model claude-opus-5 ...
```

and got:

```
err: --model 'claude-opus-5' looks like a claude model id, but role
'backend' -> provider 'codex' (Codex); use a codex model id instead of a
claude one (role->provider mapping: 'backend' -> 'codex')
```

`--help` claimed `--model` "takes precedence over role/provider defaults" —
untrue; it only ever picks a model *within* the role's already-resolved
provider. There was no way to reroute a role to a different CLI for a
single assign short of hand-editing `role-providers.json`, which needs a
**cockpit restart** to take effect (`provider_config.py` module docstring)
— useless mid-incident. Lead was stuck with no working escape hatch while a
known-good provider (claude) sat idle in the same session.

Secondary complaint (`takkub list` showing `backend working` while the pane
was actually boot-stalled) is tracked separately — this issue is scoped to
"Lead has no way out."

## Fix: `--provider` per-assign override (issue #270 option 1)

Added `takkub assign --role <role> --provider <name> ...` — forces THAT
ONE assign's fresh spawn onto a different registered CLI than the role's
configured provider. Reuses infrastructure that already existed for the
exact same substitution, just automated: `PaneState.provider_override`
(set today by the no-content/auth-failure watchdogs in
`lead_inbox._recover_broken_pane`, consumed by `spawn_engine.spawn()`'s
`_ps_initial.provider_override or effective_provider_for(...)`). This
change lets Lead trigger the same substitution manually and immediately,
instead of waiting for (or never getting) an automatic recovery.

### Why not just fix the `--help` text and leave it at that?

Both are required by the task, but a corrected help text alone still
leaves Lead with **no working command** — the actual capability was
missing, not just misdocumented. `--provider` closes that gap; the
`--help` text for `--model` was ALSO corrected (see below) since it no
longer accidentally becomes true just because `--provider` now exists —
`--model` still never changes provider by itself.

### Plumbing

- `provider_config.assign_provider_override_error(provider)` — new
  validator: unknown provider name, or a known one that's currently
  disabled/not installed (`_provider_available`), is rejected with a
  reason (that override would just fail the same way the stuck role
  already fails). Deliberately does **not** special-case forced-identity
  roles (`codex`/`gemini`/…) — the exact same pane-scoped substitution
  already happens automatically for them via `effective_provider_for`'s
  own degrade-on-unavailable fallback; this just lets Lead trigger it
  manually and earlier.
- `provider_config.assign_model_override_error/_warning` gained an
  optional `provider_override` kwarg — when the same assign also carries a
  validated `--provider`, `--model` is validated against THAT provider
  (what will actually spawn), not the role's static config/availability
  resolution. `--model claude-opus-5 --provider claude` on a codex-mapped
  role now succeeds instead of being wrongly blocked as cross-provider.
  The hard-block error message also grew a hint — `· add --provider
  <family> to the same assign to force this` — only shown when the caller
  hadn't already supplied `--provider` (task requirement: an error that
  says what's wrong must also say the fix).
- `cli.py`: new `sa.add_argument("--provider", ...)`; `cmd_assign`
  validates it (client-side, mirrors the existing `--model` checks),
  rejects it under `--mode subagent` (subagents always inherit the
  parent's provider/model context — same reasoning as the existing
  `--model` rejection there), and forwards it in all three dispatch
  shapes (plan, shard fan-out, plain assign). `--model`'s help text was
  rewritten to stop claiming provider precedence it never had.
- `cli_server.py`: the existing synchronous pre-ack validation block
  (issue #26 — validate before the "task queued" ack, not after) now also
  validates `--provider` and threads the validated value into the
  `--model` check's `provider_override`. Forwarded into the deferred
  `self._orch.assign(...)` call.
- `orchestrator.py` `assign()`: new `provider: str | None = None`
  parameter. Subagent-mode guard mirrors `--model`'s. Validated the same
  way as the CLI/server layers (defense in depth — `assign()` is also
  reachable from the resource-governor's queued-retry replay and the
  fan-out queue's `_drain_fanout_queue`, both of which now carry `provider`
  through unchanged, exactly like `model` already does).
- `orchestrator._assign_dispatch()`: the `pane_is_running` check (and the
  `PaneState` fetch it needs) was hoisted earlier in the function — it now
  runs BEFORE the `effective_provider` computation instead of after, so an
  explicit `--provider` can be folded into `effective_provider` in time for
  the codex task-rewrite decision (`_rewrite_task_for_codex`) and the
  `PROVIDER_REGISTRY[...].system_prompt_flag` lookup that stages the
  one-shot spawn payload — both must reflect the CLI that will actually
  run. Mirrors `model_override`'s existing "ignored + Lead-notified when
  pane already running, else set on `PaneState` so it survives spawn
  gate/FIFO retries and crash auto-respawn" contract, one-to-one, just for
  `provider_override` instead of `model_override`. A plain re-assign
  without `--provider` still clears any earlier override (same contract as
  `--model`) — a genuinely degraded pane's next `--provider`-less assign
  falls through to `effective_provider_for()`'s own resolution, same as
  before this change.

### Side effect (intentional, not a regression)

Before this change, `_assign_dispatch`'s early `effective_provider` (used
for the codex task-rewrite decision) was computed purely from static
config (`effective_provider_for`), ignoring any live pane-scoped degrade
already recorded on `PaneState.provider_override`. After hoisting the
check, that local variable now reads `ps_assign.provider_override or
effective_provider_for(...)` — the same resolution `spawn()` itself
already uses. A pane previously auto-degraded to claude no longer gets a
stale "rewrite for codex" hint on its next plain re-assign.

## Decision: boot-stall does NOT auto-degrade (issue #270 option 3)

The no-content watchdog (`NO_CONTENT_WATCHDOG_SEC`) and the auth-failure
detector (#269) both auto-degrade via `_recover_broken_pane` — those are
definitive dead ends (nothing ever rendered / the CLI itself says it can't
log in; retrying the same provider cannot help). Boot-stall (#254) is
different in kind: the CLI **is** rendering content (a boot-phase marker
like "Booting MCP server: …") continuously — it might still clear on its
own (a slow first cold-start MCP handshake is a real, recoverable case),
so it is not proof the provider itself is broken the way the other two
signals are.

**Decision: keep boot-stall Lead-driven, not auto-degraded.**
Auto-killing a pane that was about to succeed would (a) waste the boot
time already sunk, and (b) could silently substitute away a real
misconfiguration (a broken MCP server entry — see issue #273, filed the
same day, which found exactly this: an `http`-type shared MCP server being
forwarded to codex with no `command`/`transport`, producing a permanent,
not-slow, boot-stall) that Lead should see and fix, not have papered over.
A boot-stall that auto-degrades to claude every time would have hidden
#273's actual root cause instead of surfacing it.

What DID change: `_warn_lead_delivery_boot_stall`'s notice now names the
concrete escape hatch instead of a generic "close and reassign" (which,
without `--provider`, just stalls again on the SAME broken provider):

```
`takkub close --role <role>` แล้ว `takkub assign --role <role>
--provider claude ...` เพื่อบังคับ spawn ใหม่ด้วย claude แทน provider
เดิมที่ค้าง (#270 — assign ใหม่แบบไม่ระบุ --provider จะไปติด provider
เดิมซ้ำ)
```

## Tests (targeted, not full suite)

- `tests/test_provider_config.py` — `TestAssignProviderOverrideValidation`
  (unknown name / disabled / not-installed / case-insensitive / empty),
  `TestAssignModelOverrideWithProviderOverride` (model validated against
  the override, hint appears only when no override was given yet, warning
  path too).
- `tests/test_cli.py` — `--provider` forwarded (single + shard fan-out),
  rejected under `--mode subagent`, validation error blocks the request,
  `--model`'s validation call receives the resolved `--provider`.
- `tests/test_cli_server.py` — `--provider` forwarded to
  `orchestrator.assign`, rejected synchronously before the "task queued"
  ack (mirrors the existing `--model` pre-check test), `--model`'s
  pre-check receives the validated `--provider`. `_FakeOrch.assign` gained
  a `provider=None` parameter (was raising `TypeError` once the real
  dispatch call started always passing it).
- `tests/test_subagent_mode.py` — `--provider` rejected under
  `mode="subagent"` (mirrors the existing `--model` rejection test).

All of the above plus the pre-existing suites that exercise
`_assign_dispatch`/`_assign_with_worktree`/the resource governor's queued
retry/the fan-out queue/shard fan-out/plan fan-out/boot-stall notices/spawn
gate were re-run green after the `_assign_dispatch` reordering:
`test_fanout_queue.py`, `test_resource_governor.py`, `test_spawn_gate.py`,
`test_spawn_task_delivery.py`, `test_worktree_assign.py`,
`test_spawn_codex_argv.py`, `test_orchestrator_shard.py`,
`test_qa_plan_fanout.py`, `test_orchestrator_auto_respawn_replay.py`,
`test_orchestrator_stall.py`, `test_spawn_queue_stuck.py`,
`test_spawn_queue_health.py`, `test_delivery_boot_stall_notice.py`,
`test_lead_wait.py`.

## Multi-provider / cross-platform

`assign_provider_override_error` validates against
`provider_config.VALID_PROVIDERS` (derived from `PROVIDER_REGISTRY`, issue
#103) — any current or future registered provider works as an override
target automatically, no hand-maintained list. No platform-specific code
touched; the override just changes which `ProviderSpec` `effective_provider`
resolves to, same code path every provider already goes through.
