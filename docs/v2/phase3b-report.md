# Core V2 — Phase 3b Report: Adapter spawn (inversion) + quota-aware per-account

> epic #309 · branch `wt/backend-1787130900` (base `feat/v2-core`, on top of Phase 1-8a +
> Phase 7c merge) · 2026-08-19

Closes the two gaps Phase 3's own report tracked before the router default flips on: (A)
adapters that answered "is this provider usable" but never built anything spawnable, and (B)
`QuotaAwareAccountSelector`'s documented "same figure for every account of one provider"
limitation.

---

## 1. Files created

```text
src/agent_takkub/core/models/spawn_plan.py       # SpawnPlan — pure dataclass
src/agent_takkub/core/providers/plan.py          # account_env_overrides / assemble_generic_argv /
                                                  # build_generic_spawn_plan — all pure
src/agent_takkub/core/accounts/facade.py         # resolve_account_for — the one façade call
                                                  # spawn_engine.py's two branches use

tests/test_core_providers_plan.py
tests/test_spawn_v2_account_env.py               # flag on/off parity, both branches, real Qt spawn

docs/v2/phase3b-report.md                        # this file
```

## 2. Files modified

| File | Change |
|---|---|
| `src/agent_takkub/core/models/account.py` | `ProviderAccount` +`config_dir: str \| None = None` (additive) — the isolated config/home dir this credential's provider CLI reads from |
| `src/agent_takkub/core/accounts/registry.py` | `_record_to_account` round-trips `config_dir` |
| `src/agent_takkub/core/accounts/legacy_reader.py` | `read_legacy_accounts` populates `config_dir` from `user_profile.list_profiles()`'s existing `config_dir` field |
| `src/agent_takkub/core/accounts/selector.py` | `QuotaAwareAccountSelector` +`account_usage_lookup` param (additive — `usage_lookup` kept, still provider-id-keyed) + `_default_account_usage_lookup` (per-`config_dir` via `provider_usage`) |
| `src/agent_takkub/core/accounts/__init__.py` | exports `resolve_account_for` |
| `src/agent_takkub/core/providers/claude_adapter.py` | +`build_plan()` — real (argv caller-supplied, env real) |
| `src/agent_takkub/core/providers/cli_adapter.py` | +`build_plan()` — real, full argv+env assembly |
| `src/agent_takkub/core/providers/__init__.py` | exports the three `plan.py` functions |
| `src/agent_takkub/provider_usage.py` | `fetch_codex_usage`/`fetch_provider_usage` +`config_dir` param; `ProviderUsageStore` gets a parallel account-scoped cache (`get`/`refresh_now` +`config_dir`) |
| `src/agent_takkub/spawn_engine.py` | +`_apply_v2_account_env_override()` helper; called from both the generic branch (after `inject_provider_home_env`) and the claude branch (after `inject_user_profile_env`), gated by `v2_router_enabled()`, fail-open |
| `tests/test_core_accounts.py` | +config_dir round-trip, +per-account quota-aware differentiation, +`resolve_account_for` pool-vs-legacy tests |
| `tests/test_provider_usage.py` | +`config_dir`-aware `fetch_codex_usage`/`fetch_provider_usage`/`ProviderUsageStore` tests |
| `tests/test_core_jsonl_store.py` | +`core.models.spawn_plan`, `core.providers.plan`, `core.accounts.facade` to the no-PyQt6-leak probe |

## 3. Scope decision — what "adapter spawn จริง" means here (read this first)

Phase 3's report (§3) closed off extracting the claude branch's ~800-line argv builder for
that phase — the same instruction ("ห้ามแก้ logic เดิม") stood in Phase 3b's task too, and the
branch is unchanged in size/shape since then. Re-opening a full extraction against a module
carrying the bulk of the suite's tests, in one task, was not a responsible risk to take. What
actually changed:

- **Generic non-claude branch is now genuinely adapter-driven for its pure parts.**
  `CliProviderAdapter.build_plan()` (→ `core.providers.plan.build_generic_spawn_plan` →
  `assemble_generic_argv` + `account_env_overrides`) is real, tested, order-verified against
  the branch's own argv assembly (`spawn_engine.py` ~1873-1977, read verbatim before writing
  the golden-order test in `test_core_providers_plan.py`). It is **not yet the branch's own
  code path** — spawn_engine.py's inline `provider_argv = [...]` / `.extend()` sequence is
  untouched, so flag-off behavior for argv is unaffected by construction, not just by a flag
  check. What IS wired live into the branch (both flag-gated and real) is the account→env half.
- **Claude branch gets the same account→env real wiring, nothing else.** `ClaudeCliAdapter.
  build_plan()` takes spawn_engine's already-built `argv` as-is and only adds the env override —
  this is the honest scope: the 800-line argv builder itself remains Phase 3's tracked gap,
  narrowed but not closed.
- **The real, live behavior change this phase ships**: `_apply_v2_account_env_override()`,
  called from both branches, `TAKKUB_V2_ROUTER`-gated, fail-open. It resolves a
  `ProviderAccount` via `core.accounts.resolve_account_for()` (real V2 pool if one is
  registered, else the legacy profile fallback) and lets `CLAUDE_CONFIG_DIR`/`CODEX_HOME`
  follow it. Proven end-to-end (real `Orchestrator.spawn()`, real Qt, mocked `PtySession`) in
  `tests/test_spawn_v2_account_env.py`:
  - flag off → env untouched even with a matching V2 pool registered (proves the gate itself,
    not just the facade's fallback)
  - flag on, no pool → env unchanged (legacy fallback resolves to the same account the old
    injectors already picked)
  - flag on, pool registered with two accounts of different `config_dir` → the
    higher-priority account's dir lands in `CODEX_HOME`/`CLAUDE_CONFIG_DIR`

**Tracked gap, narrowed**: a later phase that wants the claude branch's full argv itself
adapter-driven still has to do the ~800-line extraction Phase 3 declined and this phase also
declined, for the same reason — it needs its own dedicated task, not a slice of this one.

## 4. Quota-aware per-account — real, not just injectable

`provider_usage.ProviderUsageStore` gained a second cache, keyed by `(provider, config_dir)`,
parallel to (never replacing) the existing provider-keyed cache the background poll loop and
`/api/usage` still use unchanged. `fetch_codex_usage(config_dir=...)` scopes the app-server
probe via a `CODEX_HOME` env override on the subprocess (mirrors `fetch_claude_usage`'s
pre-existing `config_dir` param — codex is the other provider with a real isolation knob,
`config.PROVIDER_ISOLATION_GAPS` documents why gemini/kimi/cursor don't get one).

`QuotaAwareAccountSelector._default_account_usage_lookup` reads the per-`config_dir` cache when
an account carries one, triggers a non-blocking `refresh_now()` on a cache miss (returns `None`
for that call — same never-block contract the module has always had), and falls back to the
old provider-level lookup for an account with no `config_dir`. `usage_lookup` (the old
provider-id-keyed constructor param) still works exactly as before — every Phase 3 test using
it is untouched and green.

`test_quota_aware_selector_differentiates_same_provider_by_account` is the "account A ชน limit
→ เลือก B" proof the task named: two `claude` accounts, different `config_dir`, a fake store
returning 97%/12% respectively — the selector picks the cooler one.

## 5. Multi-provider

`account_env_overrides`' provider→env-var map (`claude`→`CLAUDE_CONFIG_DIR`,
`codex`→`CODEX_HOME`) matches exactly the two providers with a real isolation knob today
(`config.py`'s `_PROVIDER_HOME_SUBDIRS` / `user_profile`'s `CLAUDE_CONFIG_DIR`); every other
provider (gemini/opencode/kimi/cursor) resolves to `{}` — an explicit no-op, not a silent gap,
consistent with `config.PROVIDER_ISOLATION_GAPS`'s own documentation of which providers have no
knob yet. `CliProviderAdapter`/its `build_plan()` stay generic over all 5 non-claude
`PROVIDER_REGISTRY` entries, unchanged from Phase 3.

## 6. Tests / lint

```text
PYTHONPATH=<worktree>/src <shared-venv>/python -m pytest \
  tests/test_core_providers_plan.py tests/test_core_providers.py tests/test_core_accounts.py \
  tests/test_core_routing.py tests/test_core_contracts.py tests/test_core_jsonl_store.py \
  tests/test_provider_spec_v2_fields.py tests/test_provider_override.py tests/test_provider_config.py \
  tests/test_provider_usage.py tests/test_spawn_gate.py tests/test_spawn_codex_argv.py \
  tests/test_lead_provider_unlock.py tests/test_opencode_provider.py tests/test_cursor_provider.py \
  tests/test_h1_nonclaude_env.py tests/test_provider_project_scope.py tests/test_provider_models.py \
  tests/test_routing_planner.py tests/test_spawn_v2_account_env.py tests/test_orchestrator_claude_env_leak.py
# all green, 0 failed
```

- `ruff check` on every created/modified file: clean.
- `lint-imports`: **28 kept, 0 broken** — `core-is-bottom-layer` still holds (verified
  `agent_takkub.core.providers.plan` / `agent_takkub.core.accounts.facade` /
  `agent_takkub.core.models.spawn_plan` added to the no-PyQt6 subprocess probe).
- Full suite **not** run (targeted-tests-only policy — full suite is qa's batch-gate job).

## 7. Decisions made without asking

- `SpawnPlan.env`/`paste_timing`/`ready_rules` are plain `dict`s inside a `frozen=True` dataclass
  (same `field(default_factory=...)` pattern `AccountLimits` already uses) — frozen prevents
  reassigning the attribute, not mutating the dict; acceptable since nothing in this phase
  relies on `SpawnPlan` being deep-immutable.
- `resolve_account_for()`'s pool match prefers a `project_id`-scoped pool over a global
  (`project_id=None`) one for the same provider, and only ever inspects the first match of
  each — no pool ever populates today outside tests, so this ordering is a forward-looking
  default, not a proven-necessary one.
- `_apply_v2_account_env_override` lives as one module-level function in `spawn_engine.py`
  (not duplicated per branch) — both call sites pass a different `provider_id` (`spec.name` vs
  the already-bound `CLAUDE` constant) but share the same fail-open body, mirroring
  `core.routing.facade`'s single-function-two-call-sites shape from Phase 3.
- `paste_timing`/`ready_rules` on `SpawnPlan` are populated by nothing yet (empty dict default)
  — the task named them as fields the plan should carry, but nothing in either branch currently
  produces per-provider paste-timing/ready-rule data as a discrete value spawn_engine could hand
  over (paste timing is inline delay logic elsewhere in the file; ready-rule detection lives in
  provider_spec's ready markers, already consumed directly). Left as documented placeholders
  rather than invented content.
