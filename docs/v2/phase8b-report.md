# Core V2 Phase 8b — Storage V2 migration (#309)

Worktree: `wt/backend-2-1787130627`, base `feat/v2-core` (Phase 1–8a already merged).
Layout source: blueprint `02_STORAGE_AND_FOLDER_STRUCTURE.md` §1–15 + `13_MIGRATION_MAPPING_FROM_V1.md`
(`~/Desktop/AGENT_TAKKUB_V2_MASTER_BLUEPRINT.md`), trimmed to the task's own explicit tree.

## Files created

```
src/agent_takkub/core/storage/layout.py            # NEW — StorageLayoutV2/ProjectLayoutV2 + LEGACY_MAPPING
src/agent_takkub/core/migration/registry_copy_step.py  # NEW — generic RegistryCopyStep (ladder steps 1/3/5)
src/agent_takkub/core/migration/steps_v1.py         # NEW — all 7 ladder step classes/factories
tests/test_core_storage_layout.py                    # NEW — 12 tests
tests/test_core_migration_v1_steps.py                 # NEW — 17 tests (x2 param = 34 runs)
docs/v2/phase8b-report.md                             # NEW — this file
```

## Files modified

```
src/agent_takkub/core/storage/paths.py   # core_home()/conversation_dir() gain a layout-aware
                                          # legacy fallback (plan §3.4) — inert until a future
                                          # step relocates Core's own internal store (see Gaps)
src/agent_takkub/core/migration/engine.py  # MigrationEngine()'s default ladder: version-marker
                                          # + the 7 new steps, in plan §5.3 risk order
src/agent_takkub/doctor.py               # + check_storage_layout_state() (opt-in, --storage-layout)
src/agent_takkub/cli.py                  # + `takkub doctor --storage-layout` flag/wiring
src/agent_takkub/disk_usage.py           # + runtime_layout_triage() awareness (read-only, no
                                          # new prune category), folded into disk_report()
tests/test_core_migration.py             # default-step-count test updated: 1 -> 8 steps
tests/test_cli_migrate.py                # isolated DATA_HOME/SETTINGS_HOME (autouse fixture);
                                          # step-count assertions updated: 1 -> 8 steps
tests/test_disk_usage.py                 # + 1 test for runtime_layout_triage()
docs/architecture/depgraph.json          # regenerated (tools/gen_import_graph.py)
```

`config.py` untouched — no constant added or changed (the layout builder reads
`config.DATA_HOME`/`config.SETTINGS_HOME` live, per plan §3.4's own rule that
`core/storage/paths.py` must never declare a new home).

## 8b-1 — `core/storage/layout.py`

`StorageLayoutV2` is one frozen dataclass covering the task's exact tree:
`config/ providers/ accounts/ models/ capabilities/ agents/
projects/<id>/{project.json,worktrees,artifacts,conversations,brain,checkpoints,logs,state}
brain/{global,operational} state/{providers,accounts,sessions,tasks,issues,registry}
runtime/ cache/ secrets/ system/{version.json,schemas,migrations,backups}`.
`storage_layout_v2(data_home=None)` is pure path arithmetic — it never creates
a directory (verified by `test_storage_layout_v2_is_pure_path_arithmetic`).

**Root placement decision**: the physical root is `DATA_HOME / "v2"`, not bare
top-level names under `DATA_HOME`. Blueprint uses `projects/`, `runtime/`,
`cache/` at the root — but V1 already owns those *exact* names with a
*different* shape. Reusing them unnamespaced would let a migration step
silently interleave V1 and V2 files in one directory, defeating copy-never-
move. Nesting under `v2/` keeps every step's target physically disjoint from
every V1 source, by construction, with zero risk of a step accidentally
touching a V1 file it doesn't have write intent over.

`LEGACY_MAPPING`: 21 rows, one per V1 artifact from `13_MIGRATION_MAPPING_FROM_V1.md`
+ audit §4, each carrying a `LegacySemantics` tag (`READ_ONLY_REGISTRY` /
`ROLE_AGENT` / `CAPABILITY` / `PROJECT` / `STATE` / `CREDENTIAL` / `UNKNOWN`)
and a `ladder_step` (1–7, matching plan §5.3; `0` = deliberately unscheduled).
Every row's V1 path was confirmed by grepping the actual source constant
(`provider_models.py:_PATH`, `custom_roles.py:CUSTOM_ROLES_FILE`, etc.) — none
guessed, per migration rule #1 "Do not move files blindly". Two rows
(`graft-graphs/`, `RUNTIME_DIR/tunnel` + `/browser-profiles`) are tagged
`UNKNOWN`/step `0` on purpose — the blueprint itself only says "subsystem-
specific path หรือ plugin storage" for graft, and the runtime cache dirs are
explicitly out of scope per 8b-3's own "ยังไม่ลบอัตโนมัติ" instruction — an
honest gap, not a silently dropped file (test
`test_legacy_mapping_entries_have_semantics_and_are_not_blindly_unknown`
asserts every *scheduled* row has real semantics).

`layout_state(data_home=None)` returns `"v1"` (no V2 root) / `"v2"` (V2 root
exists, every V1 marker gone) / `"mixed"` (both present) — existence checks
only, no I/O beyond `Path.is_dir()`/`Path.exists()`, safe for a doctor check.

### `paths.py`'s legacy fallback

Per plan §3.4 ("paths.py เดิม resolve ผ่าน layout + legacy fallback"),
`core_home()` and `conversation_dir()` now consult the V2 layout first and
fall back to their original computation when the V2 location doesn't exist
yet:

- `core_home()` → `StorageLayoutV2.system` if it exists, else the original
  `RUNTIME_DIR / "core"`.
- `conversation_dir(project_id, ...)` → `StorageLayoutV2.project(project_id).root
  / "conversations" / ...` once *that project's* V2 subtree exists, else the
  original `core_home()/conversations/<project_id>/...`.

**Decision, stated explicitly**: no ladder step in this phase relocates
Core's own already-established internal storage (version.json, migration
journal/backups, accounts registry, secrets store, conversation directories
Phase 1–8a already write under the old `core_home()`) — the ladder steps
1–7 all migrate *V1 config files*, not Core V2's *own* storage. So today,
nothing ever creates `StorageLayoutV2.system` or a project's V2 subtree, and
both fallback branches always take the "else" arm — Phase 1–8a callers see
byte-identical behaviour, proven by the full `tests/test_core_*.py` sweep
passing unchanged (see Tests). The mechanism exists and is exercised by
`core_home`/`conversation_dir`'s own logic; a future phase that decides to
relocate Core's internal store only needs to add one more step that
populates `system/`, and both functions start resolving there automatically.

## 8b-2 — Migration steps (ladder, plan §5.3)

Every step is copy-never-move: `apply()` only ever writes under
`StorageLayoutV2.root`, and always backs up its own prior V2 target via
`BackupManager` before overwriting — `rollback()` restores that backup, or
deletes the file if there was none (first-time write). No step ever mutates
or deletes anything at a V1 path.

| # | step_id | Shape | V1 sources | V2 target |
|---|---|---|---|---|
| 1 | `readonly-registries` | `RegistryCopyStep` (generic) | provider-models, role-models, disabled-providers, exec-mode, rtk-enabled | `models/registry.json`, `models/aliases.json`, `providers/registry.json`, `config/execution.json`, `config/features/rtk.json` |
| 2 | `role-agent` | `RoleAgentMigrationStep` (fan-out) | custom-roles.json + role .md files, role-providers.json (global + every known project) | `agents/custom/registry.json` + `agents/custom/<role>.md`, `config/routing.json` (`{"global":..,"projects":{slug:..}}`) |
| 3 | `capability` | `RegistryCopyStep` (generic) | pane-tools.json, skill-policy.json | `capabilities/mcp/permissions.json`, `capabilities/skills/registry.json` |
| 4 | `project` | `ProjectMigrationStep` (fan-out) | projects.json | `projects/registry.json` + `projects/<id>/project.json` (each carrying a `worktrees_owned` ownership pointer list — the real checkouts are never copied, `worktree_manager.py` stays the sole owner) |
| 5 | `state` | `RegistryCopyStep` (generic) | .takkub_issues.json, auto_issue_dedup.json, autoresume.json, takkub-remote-sessions.json | `state/issues/local.json`, `state/issues/dedup.json`, `state/sessions/autoresume.json`, `state/sessions/remote.json` |
| 6 | `credential-reference` | `CredentialReferenceStep` (reference only) | claude/codex/opencode config-dir *presence*, never contents | `providers/<p>/provider.json` + `accounts/<p>/account.json`, each `{"config_dir", "config_dir_exists", "secret_ref": "secret://<p>/default"}` — **no credential byte is ever read into memory or written anywhere** |
| 7 | `runtime-triage` | `RuntimeTriageStep` | `RUNTIME_DIR/{tasks,sessions,role-memory,knowledge}` (audit D6 "state ถาวร") | `state/tasks`, `state/sessions`, `state/registry/role-memory`, `state/registry/knowledge`. `RUNTIME_DIR/{tunnel,browser-profiles}` are classified but never copied/deleted — 8b-3's own "ยังไม่ลบอัตโนมัติ" instruction |

`RegistryCopyStep` wraps each V1 JSON blob verbatim
(`{"schema", "migrated_from", "migrated_at", "data": <v1 json>}`) rather than
reshaping field-by-field — full-fidelity passthrough, so "unknown field
ห้ามทิ้งเงียบ" (plan §5.1) holds trivially: nothing is restructured, so
nothing can be silently dropped. A later phase that wants the real V2 domain
shape (e.g. an actual `ModelRegistry`) reads `data` out of the wrapper, same
pattern `core.storage.legacy_reader` already established.

A collision the generic step had to guard against: two mappings in the same
step can legitimately target the same *basename* in different V2 subdirs
(`models/registry.json` and `providers/registry.json` both end in
`registry.json`), but `BackupManager.latest_backup(step_id, name)` only keys
by basename within one `step_id` folder. `RegistryCopyStep` backs up under a
composite key (`f"{step_id}__{mapping.name}"`), not the bare `step_id`, so
one mapping's backup slot can never shadow another's.

`RoleAgentMigrationStep`/`ProjectMigrationStep`/`CredentialReferenceStep`/
`RuntimeTriageStep` all take explicit `data_home`/`settings_home` (and, where
relevant, `custom_agents_dir`/`runtime_dir`/`refs_override`) constructor
parameters, defaulting to the live `config.DATA_HOME`/`config.SETTINGS_HOME`
— mirrors `disk_usage.py`'s own `data_home` parameter convention, so a test
points a step at `tmp_path` instead of monkeypatching module globals.

`MigrationEngine()`'s default ladder is now `version-marker` (Phase 4,
unchanged, kept first) followed by the 7 steps above in risk order, all
sharing one `MigrationJournal`/`BackupManager` pair — `takkub migrate
rollback` walks the whole ladder in reverse from a single source of truth.

## 8b-3 — `runtime/` triage

`steps_v1.RUNTIME_STATE_TARGETS` / `RUNTIME_CACHE_ENTRIES` are the single
classification table both `RuntimeTriageStep` (step 7, copies state dirs)
and `disk_usage.runtime_layout_triage()` (read-only awareness, folded into
`disk_report()`'s new `"runtime_triage"` key) read from — so the ladder step
and the disk-usage report can never drift apart on what counts as state vs.
cache. State dirs (`tasks`, `sessions`, `role-memory`, `knowledge`) get
copied on `apply()`; cache dirs (`tunnel`, `browser-profiles`) are only
counted/reported, never touched — no new `VALID_CATEGORIES` entry was added
to `disk_usage.py`, so `takkub prune`'s behavior is completely unchanged by
this phase, matching the task's explicit "ยังไม่ลบอัตโนมัติ".

`state/registry` has no dedicated task-list bucket for `role-memory`/
`knowledge` specifically (the task's own state list is
`{providers,accounts,sessions,tasks,issues,registry}`), so those two land
under the catch-all `state/registry/role-memory` and `state/registry/knowledge`
— documented in `LEGACY_MAPPING`, not a silent choice.

## 8b-4 — `doctor` + real dry-run

`takkub doctor --storage-layout` (new opt-in flag, same `--live`/`--core-
version` off-by-default pattern) reports layout state (v1/v2/mixed) and, on
`v1`, how many ladder steps remain. Plain `takkub doctor` is byte-identical
(the check is excluded from `run_all_checks()`'s default tuple, mirroring
`check_core_version_compat`).

### Real dry-run, this machine (dev checkout — not applied)

Resolved for this worktree at run time:

```
DATA_HOME     = C:\Users\monch\WebstormProjects\agent-takkub\worktrees\agent-takkub\backend-2-1787130627
SETTINGS_HOME = C:\Users\monch\.takkub                      (real, shared dev settings home)
```

`takkub migrate dry-run --json` (read-only, nothing written) against this
real `SETTINGS_HOME`:

| step | result |
|---|---|
| version-marker | app component would change (no `version.json` yet) |
| readonly-registries | 5/5 targets would change (all 5 V1 files exist on this dev machine) |
| role-agent | registry + routing would change; 0 custom-role `.md` files (none defined here) |
| capability | 2/2 targets would change (pane-tools.json, skill-policy.json both exist) |
| project | 0/0 — `projects.json` doesn't exist at this worktree's `DATA_HOME` (a worktree checkout, not the real cockpit's `~/.agent-takkub`) |
| state | 4/4 targets would change (all 4 V1 files exist) |
| credential-reference | 1 provider resolvable (`claude` — dev checkout, `codex`/`opencode` isolation is installed-build-only by design, `provider_home_env` returns `{}` here) |
| runtime-triage | 0/0 — no `tasks`/`sessions`/`role-memory`/`knowledge` dirs under this worktree's `RUNTIME_DIR` |

`takkub doctor --storage-layout --json`: `state: "v1"`, `7 ladder step(s) not
yet applied`. All 8 steps report `"ok": true`. **No `apply` was run** — read-
only per the task's explicit instruction.

## Tests

- `tests/test_core_storage_layout.py` — 12 tests: layout purity (no disk
  I/O), root-under-`v2/`-avoids-V1-collision, project sub-layout shape,
  `layout_state()` v1/v2/mixed, `LEGACY_MAPPING` covers all 7 ladder steps
  with real semantics.
- `tests/test_core_migration_v1_steps.py` — 17 tests × 2 params
  (`installed_merged` = `SETTINGS_HOME == DATA_HOME`, mirrors
  `~/.agent-takkub`; `dev_split` = separate, mirrors `~/.takkub`) = 34 runs:
  dry-run/apply/validate/rollback round-trip per step, unknown-field
  preservation, rollback-restores-prior-value, credential step never writing
  a credential byte anywhere under the V2 tree, worktree checkouts never
  copied, cache dirs never touched.
- `tests/test_core_migration.py` — updated default-step-count assertion
  (1 → 8, first step still `version-marker`).
- `tests/test_cli_migrate.py` — added an autouse `DATA_HOME`/`SETTINGS_HOME`
  isolation fixture (the widened default ladder now writes real files
  derived from those — without isolation `apply` would leave a stray `v2/`
  dir in the dev checkout and read this machine's real `~/.takkub`); updated
  step-count assertions.

**Targeted run** (every `tests/test_core_*.py` file, 27 files, ~450+ cases):
**all green**. `tests/test_doctor.py`, `test_doctor_core_version.py`,
`test_disk_usage.py`, `test_cli.py`: **all green** (unaffected by the new
opt-in flag/report key).

`ruff check src/ tests/`: all checks passed.
`lint-imports`: **28 contracts kept, 0 broken** — `core-is-bottom-layer`
holds (`core.storage.layout`/`core.migration.steps_v1`/
`registry_copy_step` import only `agent_takkub.config` + sibling `core.*`
modules, none reach PyQt6/orchestrator/cli/app/main_window/cli_server).
`docs/architecture/depgraph.json` regenerated via `tools/gen_import_graph.py`.

Full suite was **not** run (project rule: targeted mid-flight, full suite
once at the qa batch gate).

## Gaps / follow-ups

- **Core V2's own internal storage (`core_home()`'s existing contents) is
  not relocated by this phase.** The ladder migrates V1 config files only;
  `paths.py`'s new legacy-fallback mechanism is real and tested (via
  `core_home`/`conversation_dir`'s own logic) but currently always resolves
  to the pre-existing location since nothing populates `StorageLayoutV2.system`
  or a project's V2 subtree yet. A future phase adding that relocation step
  can reuse the exact fallback already wired here.
- **`state/registry`'s role-memory/knowledge bucket has no dedicated slot**
  in the task's own abbreviated state list — documented choice (catch-all
  subfolder), not silently improvised (see 8b-3 above).
- **`graft-graphs/`/`graft-staging/`** stay `LegacySemantics.UNKNOWN`,
  ladder step `0` — the blueprint itself doesn't commit to a location
  ("subsystem-specific path หรือ plugin storage"), and no source module
  currently reveals firmer semantics; flagged, not silently dropped.
- **Credential reference step (6) only resolves `claude`/`codex`/`opencode`**
  — `gemini`/`kimi`/`cursor` have no confirmed isolation knob in this
  codebase yet (same posture `provider_bootstrap.py`/`doctor.py` already
  take, tracked under #103).
- **`disk_usage.runtime_layout_triage()` is awareness-only** — no new
  `VALID_CATEGORIES` entry, so `takkub prune` cannot delete a runtime state
  or cache dir through this phase's work; that stays a deliberate future
  decision, per 8b-3's explicit scope limit.
