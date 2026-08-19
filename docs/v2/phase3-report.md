# Core V2 — Phase 3 Report: Provider Adapter + Account + Router

> epic #309 · branch `wt/backend-1787126355` (base `feat/v2-core`, on top of Phase 1+2 commits
> `c12a87e`/`ffbddeb`) · 2026-08-19

Implements plan §2 Phase 2 row ("Provider adapter + Account + Router") — labelled "Phase 3" in
the assigned task, matching epic #309's own phase numbering.

---

## 1. Files created

```text
src/agent_takkub/core/providers/__init__.py
src/agent_takkub/core/providers/errors.py
src/agent_takkub/core/providers/claude_adapter.py
src/agent_takkub/core/providers/cli_adapter.py
src/agent_takkub/core/providers/registry.py

src/agent_takkub/core/accounts/__init__.py
src/agent_takkub/core/accounts/registry.py
src/agent_takkub/core/accounts/legacy_reader.py
src/agent_takkub/core/accounts/switch_log.py
src/agent_takkub/core/accounts/selector.py

src/agent_takkub/core/routing/__init__.py
src/agent_takkub/core/routing/policy.py
src/agent_takkub/core/routing/router.py
src/agent_takkub/core/routing/flag.py
src/agent_takkub/core/routing/facade.py

src/agent_takkub/core/contracts/routing_policy.py   # new contract, same pattern as
                                                      # account_selector.py/provider_adapter.py

tests/test_core_providers.py
tests/test_core_accounts.py
tests/test_core_routing.py
tests/test_provider_spec_v2_fields.py

docs/v2/phase3-report.md   # this file
```

## 2. Files modified

| File | Change |
|---|---|
| `src/agent_takkub/core/models/account.py` | `SelectionStrategy` +`MANUAL`, +`COOLDOWN_FAILOVER` (additive; existing 3 members untouched) |
| `src/agent_takkub/provider_spec.py` | `ProviderSpec` +§16 field group: `transport`/`auth_kinds`/`adapter_id`/`compat_range`, all with safe defaults (`"cli"`/`()`/`""`/`""`) — no `PROVIDER_REGISTRY` entry's existing fields touched |
| `src/agent_takkub/spawn_engine.py` | **The one façade call** (plan §2: "จุดเชื่อมเดียว"), `spawn()` ~line 1463: `effective_provider_for(...)` → `core.routing.effective_provider_for_v2(...)`. `TAKKUB_V2_ROUTER` unset/off ⇒ byte-identical (facade re-imports and calls `provider_config.effective_provider_for` directly, same as before) |
| `tests/test_core_contracts.py` | added `routing_policy` to the Protocol-conformance sweep (a fake satisfying `RoutingPolicy`) |
| `tests/test_core_jsonl_store.py` | added `core.providers`/`core.accounts`/`core.routing`/`core.contracts.routing_policy` to the no-PyQt6-leak subprocess probe |

## 3. Scope decision — adapters are structural, not process-owning (read this first)

**`ClaudeCliAdapter`/`CliProviderAdapter`'s `spawn()`/`send()`/`is_ready()`/`terminate()` raise a
documented `ProviderAdapterNotWired`, not real spawn_engine calls.**

Why: the task said "ห่อ (wrap) spawn_engine's 2 branches, ห้ามแก้ logic เดิม". The claude branch
(spawn_engine.py ~1990-2900) builds argv inline against live `PaneState`/env mutation and ends by
constructing a `PtySession` (PyQt6) directly — it is not spec-driven the way the generic
non-claude branch already is (`provider_spec.py`'s own docstring: claude_spec's fields are "NOT
wired into spawn_engine.py's claude argv builder ... faithful documentation ... for a future
phase", a precedent this phase follows rather than reverses). Two ways to make these adapters
call the real thing were both closed off:

1. **Extract** the ~800-line block into something `core` can import — explicitly out of scope
   ("ห้ามแก้ logic เดิม"), and risky against the module carrying the bulk of the suite's 7,033
   tests for a single Phase-3 task.
2. **Import `spawn_engine` from `core`** — transitively pulls in PyQt6 (`PtySession`/`QTimer`),
   tripping `core-is-bottom-layer` (verified: `lint-imports` 28/28 kept, see §6).

What IS real: `provider_id()`/`is_available()` call the exact functions spawn_engine.py itself
consults (`provider_config._provider_available`) — never re-derived, so they can never drift.
This differs by provider: the generic branch is genuinely spec-driven already (#103 Phase 1), so
`CliProviderAdapter.is_available()` is load-bearing logic reuse, not a stub; `ClaudeCliAdapter`'s
is a thin wrap over the same always-true claude check `provider_config` already hardcodes.

**Tracked gap** (not #103 — a Core V2 structural gap): a later phase must either (a) expose a
thin non-core façade the adapter can call into for the real spawn, or (b) invert control so
spawn_engine calls INTO the adapter. Until then, `core.providers` answers "is this provider
usable" correctly and is fully protocol-conformant, but does not itself spawn anything.

## 4. What's real and load-bearing

- **`core.providers`** — `adapter_for()` factory, real `is_available()`/`provider_id()` for all 6
  registered providers (see §3).
- **`core.accounts`** —
  - `AccountRegistry`/`AccountPoolRegistry`: JSONL-backed, latest-record-per-id wins (upsert log),
    `{"id":..., "deleted": true}` tombstone — same append-only pattern as Phase 1's
    `jsonl_store.py`.
  - `legacy_reader.read_legacy_accounts()`/`read_selected_account_id()`: reads
    `user_profile.list_profiles()`/`profile_for()` (unmodified) into `ProviderAccount` rows,
    `secret_ref` following `core.secrets.manager`'s `secret://provider/account-id` scheme. Proven
    against claude accounts (task's "แถวแรก (claude)"); converts whatever `list_profiles()`
    actually returns so a future non-claude profile is picked up for free, but that path is
    untested this phase (nothing populates one today).
  - `switch_log.log_switch()`/`read_switches()`: append-only event log, the "account switch = log
    event ไว้ก่อน (checkpoint มาขั้น 6)" scope.
  - Selectors (`core.contracts.account_selector.AccountSelector`), all filtered to
    `pool.account_ids` ∩ `status == ACTIVE`:
    - `ManualAccountSelector(account_id)` — pins one id.
    - `PriorityAccountSelector` — highest `priority`.
    - `StickyAccountSelector` — same account across calls until it drops out, then re-picks by
      priority.
    - `QuotaAwareAccountSelector` — lowest `provider_usage.ProviderUsage.utilization` (cached
      read via `get_store()`, never a live fetch); **known limitation**: utilization is
      per-PROVIDER today, not per-account (no adapter fetches usage scoped to one credential —
      would need per-account `config_dir` plumbed through `fetch_claude_usage`/
      `fetch_codex_usage`), so two accounts of the same provider in one pool see the same figure;
      `usage_lookup` is injectable for a future per-account fetch or a test.
    - `CooldownFailoverSelector` — priority pick with failover memory: a move away from an
      already-active pick (never the first pick for a pool) logs one `switch_log` event. This is
      the strategy that owns "account switch = log event".
    - `selector_for(strategy)` factory; raises on `MANUAL` (needs a caller-chosen id, no sane
      default).
- **`core.routing`** — `StaticRoutingPolicy.resolve()` delegates VERBATIM to
  `provider_config.effective_provider_for()` (no reimplementation, can't drift). `Router` wraps a
  policy (defaults to `StaticRoutingPolicy`). `effective_provider_for_v2()` (the façade) is
  `TAKKUB_V2_ROUTER`-gated and fail-open: any exception anywhere in the Router path falls back to
  the direct call, logged, never raised. Wired into spawn_engine.py's ONE call site.

## 5. Multi-provider

`CliProviderAdapter` is generic over all 5 non-claude `PROVIDER_REGISTRY` entries
(codex/gemini/opencode/kimi/cursor) plus `ClaudeCliAdapter` = all 6 — the adapter contract suite
(`test_core_providers.py`) is parametrized across every one. Account switching is proven for
claude (legacy reader) with codex's env-isolation mechanism (`CODEX_HOME` via
`inject_provider_home_env`) already REUSEd unmodified — Phase 3 does not touch that injection
path, so it stays multi-provider-ready by construction; actually wiring a live credential swap
through it is the tracked gap in §3.

## 6. Tests / lint

```text
PYTHONPATH=<worktree>/src python -m pytest \
  tests/test_core_providers.py tests/test_core_accounts.py tests/test_core_routing.py \
  tests/test_core_contracts.py tests/test_core_jsonl_store.py tests/test_provider_spec_v2_fields.py \
  tests/test_provider_override.py tests/test_provider_config.py \
  tests/test_spawn_gate.py tests/test_spawn_codex_argv.py tests/test_lead_provider_unlock.py \
  tests/test_opencode_provider.py tests/test_cursor_provider.py tests/test_h1_nonclaude_env.py \
  tests/test_provider_project_scope.py tests/test_provider_models.py tests/test_routing_planner.py
# 172 + 76 = all green, 0 failed
```

- `ruff check` on every created/modified file: clean.
- `lint-imports`: **28 kept, 0 broken** (261 files, 1629 dependencies) — `core-is-bottom-layer`
  holds for the 3 new `core/` subpackages.
- Full suite **not** run (targeted-tests-only policy — full suite is qa's batch-gate job).

## 7. Decisions made without asking

- `SelectionStrategy` gained `MANUAL`/`COOLDOWN_FAILOVER` members (Phase 1 only shipped
  priority/sticky/quota-aware) — additive, required by the task's 5-strategy list.
- `ProviderSpec`'s new §16 fields use plain `str`/`tuple[str, ...]` types (not a `core.models`
  enum import) to keep provider_spec.py's existing plain-type style and avoid a new
  engine→core coupling beyond what this phase already needed.
- `CooldownFailoverSelector` only logs on a MOVE away from an already-known pick, never on a
  pool's first-ever selection — an initial assignment isn't a failover.
- No new `AccountPool.strategy` default changed; `MANUAL` intentionally has no default instance
  from `selector_for()` (raises) since it needs a caller-chosen account id.
