# Core V2 Phase 4 — Version / Compatibility / Migration engine (#309)

Worktree: `wt/backend-2-1787126395`, base `feat/v2-core` (Phase 1+2 already committed).

## Files created

```
src/agent_takkub/provider_probe.py                        # NEW — pure-leaf, extracted from doctor.py
src/agent_takkub/core/versioning/__init__.py               # NEW
src/agent_takkub/core/versioning/store.py                  # NEW — version.json read/write (atomic)
src/agent_takkub/core/versioning/compatibility.py          # NEW — CompatibilityMatrix
src/agent_takkub/core/versioning/detector.py                # NEW — ProviderVersionDetector
src/agent_takkub/core/versioning/probe.py                   # NEW — live-store schema-drift probe
src/agent_takkub/core/migration/__init__.py                 # NEW
src/agent_takkub/core/migration/journal.py                  # NEW — MigrationJournal (jsonl)
src/agent_takkub/core/migration/backup.py                   # NEW — BackupManager (copy-never-move)
src/agent_takkub/core/migration/report.py                   # NEW — StepReport
src/agent_takkub/core/migration/steps.py                    # NEW — VersionMarkerStep (proof step)
src/agent_takkub/core/migration/engine.py                   # NEW — MigrationEngine
tests/test_core_versioning.py                                # NEW — 31 tests
tests/test_core_migration.py                                 # NEW — 16 tests
tests/test_doctor_core_version.py                            # NEW — 3 tests
tests/test_cli_migrate.py                                    # NEW — 5 tests
docs/v2/phase4-report.md                                     # NEW — this file
```

## Files modified

```
src/agent_takkub/doctor.py           # _resolve_provider_bin/_run now thin re-exports of
                                      # provider_probe (behavior-neutral extraction, verbatim
                                      # logic moved, not rewritten); + new opt-in
                                      # check_core_version_compat()
src/agent_takkub/cli.py              # + `takkub migrate {inspect,plan,dry-run,apply,validate,
                                      #   rollback}` subcommand (additive); + `takkub doctor
                                      #   --core-version` opt-in flag
src/agent_takkub/core/models/version.py  # CompatibilityRule: additive fields
                                          # max_exclusive (default True, preserves old meaning)
                                          # and features (default ()), reused instead of a
                                          # parallel dataclass
```

## What each part does

**4a — `core/versioning/`**
- `store.py`: `version.json` under `core.storage.paths.core_home()`, atomic write
  (tmp + `os.replace`, same shape as `jsonl_store`). Reuses
  `core.models.version.ComponentVersion` as the record shape — `component` values
  `"app"`, `"storage_schema"`, `"adapter:<provider>"`.
- `compatibility.py`: `CompatibilityMatrix` over `CompatibilityRule` (min inclusive /
  max exclusive-or-inclusive + `features`). Only `claude` ships a real rule (mirrors
  `system_baseline.CORE_TOOLS["claude"]`); every other provider is `UNCALIBRATED` by
  design — no guessed thresholds for providers the project's own notes flag as
  "uncalibrated until login".
- `detector.py`: `ProviderVersionDetector` — uses `provider_spec.PROVIDER_REGISTRY` +
  the SAME binary-resolution logic `doctor.check_providers()` uses, via the new
  `provider_probe` module (see below). Not reimplemented.
- `probe.py`: live-store schema-drift probe. Reads the REAL on-disk store for
  claude (`~/.claude/projects/**/*.jsonl`), codex (`codex_helper.codex_sessions_root()`
  rollouts), opencode (`opencode_helper.opencode_db_path()` sqlite table names).
  gemini/kimi/cursor: no confirmed store location → `found=False`, never guessed.
  `detect_drift()` compares against a previously-recorded fingerprint (not a
  hardcoded "true" schema) — directly answers the codex-0.147 lesson in the plan:
  a wrong guess at "the" schema is exactly what let that drift go unnoticed.

**Extraction, not duplication**: `doctor._resolve_provider_bin`/`_run` moved
verbatim into `agent_takkub.provider_probe` (pure leaf: stdlib + `_win_console`
only). `doctor.py` keeps both names as one-line re-exports so its ~40 existing
call sites are untouched. `core.versioning.detector` imports the same
`provider_probe` module — one resolver, not two independently-drifting copies.

**4b — `core/migration/`**
- `journal.py` / `backup.py`: append-only jsonl journal + copy-never-move backups
  (mirrors `provider_bootstrap.ensure_provider_home`'s `.partial`+marker+`os.replace`
  precedent, but backups here are timestamped copies, not a single partial dir).
- `steps.py::VersionMarkerStep`: the Phase 4 proof-of-pipeline step — upserts
  `version.json`'s `"app"` component. Backs up the pre-existing file (if any)
  before writing; `rollback()` restores it, or deletes the file if there was
  nothing before. Exercises inspect → plan → dry_run → apply → validate →
  rollback end to end so a later, riskier plan §5.3 step can copy this shape.
- `engine.py::MigrationEngine`: `inspect/plan/dry_run` always run every step
  (read-only, a partial inventory is worse than a slow one); `apply/rollback`
  stop-the-line on the first failing step (plan §5.3's "เกณฑ์หยุด").
- CLI: `takkub migrate {inspect,plan,dry-run,apply,validate,rollback} [--json]`
  — additive subcommand in `cli.py`, no existing subcommand touched.

**4c — doctor**
- `check_core_version_compat()`: per-provider Schema/Adapter/Compat verdict, using
  4a's detector + matrix + probe. **Not** added to `run_all_checks()`'s default
  tuple — opt-in only via `takkub doctor --core-version`, mirroring the existing
  `--live` pattern (`check_spawn_queue_live` etc.), so a plain `takkub doctor` is
  byte-identical to before this landed (feature-flag rule in the task's shared
  constraints). Never returns `Status.FAIL` — an unregistered/unparseable
  provider is `INFO`, not a broken machine.

## Feature flags / connection points

| Connection point | Flag | Off-state behavior |
|---|---|---|
| `doctor.run_all_checks()` | `takkub doctor --core-version` | unchanged — new check excluded by default (tested: `check_core_version_compat` string absent from `run_all_checks` source) |
| `doctor._resolve_provider_bin` / `_run` | n/a (pure extraction) | verbatim logic moved to `provider_probe.py`; full `test_doctor.py` + `test_cli.py` suites still green |
| `cli.py` `migrate` subcommand | new, additive | no existing subcommand parser touched |
| `core.models.version.CompatibilityRule` | additive fields with defaults | any pre-existing construction keeps its old meaning |

## Tests

- `tests/test_core_versioning.py` — 31 tests (store round-trip/atomicity, compatibility
  matrix boundaries incl. max-exclusive vs inclusive, detector against fake
  registries/probes, live-store probe against synthetic claude/codex jsonl +
  a real sqlite opencode.db fixture, drift detection).
- `tests/test_core_migration.py` — 16 tests (journal apply/rollback bookkeeping,
  backup copy-never-move + restore + latest-slot lookup, VersionMarkerStep full
  lifecycle incl. rollback-restores-prior-value, MigrationEngine stop-the-line
  on both `apply()` and reverse-order `rollback()`).
- `tests/test_doctor_core_version.py` — 3 tests (never FAILs, covers every
  registered provider, confirmed absent from `run_all_checks()`).
- `tests/test_cli_migrate.py` — 5 tests (every subcommand via `cli.main()`,
  JSON + text output, full apply→validate→rollback cycle, missing-subcommand
  error).

**Targeted run** (`tests/test_core_versioning.py tests/test_core_migration.py
tests/test_doctor_core_version.py tests/test_cli_migrate.py
tests/test_core_contracts.py tests/test_core_jsonl_store.py tests/test_doctor.py
tests/test_doctor_version.py tests/test_cli.py`): **268 passed, 0 failed**
(55 new + 213 pre-existing, all green).

**`ruff check src/ tests/`**: all checks passed.

**`lint-imports`**: **28 contracts kept, 0 broken** (including `core-is-bottom-layer`
— `core.versioning`/`core.migration` import only `provider_probe`, `provider_spec`,
`system_baseline`, `config`, `codex_helper`, `opencode_helper` — all pure leaves,
none reach PyQt6/orchestrator/cli/app/agent_pane/terminal_widget).

Full suite was **not** run (project rule: targeted tests mid-flight, full suite
once at the qa batch gate).

## Gaps / follow-ups for #103 (multi-provider)

- `compatibility.py`'s `DEFAULT_MATRIX` only has a real rule for `claude`.
  codex/gemini/opencode/kimi/cursor stay `UNCALIBRATED` until each provider's
  actual min-compatible version is empirically confirmed (not guessed) — same
  posture the project already takes for provider auth checks.
- `probe.py` only has store resolvers for claude/codex/opencode. gemini (antigravity
  db layout, `gemini_helper.py`) and kimi/cursor have no confirmed store location
  in the existing codebase to resolve against yet — `probe_store()` returns
  `found=False` with an explicit note for these rather than guessing a path.
- `steps.py` ships exactly one step (`VersionMarkerStep`, no-op-safe). The plan
  §5.3 ladder (read-only registries → role/agent config → capability → project
  data → state → credentials) is intentionally NOT implemented in this phase —
  each later step should be released on its own, per the plan's own risk
  ordering, using `VersionMarkerStep` as the template for backup+journal+
  rollback wiring.
- `MigrationEngine` does not yet read `MigrationJournal.applied_step_ids()` to
  auto-select which steps `rollback()` should target when steps differ from
  the last `apply()` run (today it just walks `self._steps` in reverse) — fine
  for a single-step Phase 4, will need revisiting once multiple real steps
  exist and a partial-apply rollback must target only what actually ran.

## Decisions made without asking

- Chose **read-modify-write over jsonl-append** for `version.json` (a small,
  single JSON object with foreign keys per component) instead of using
  `core.storage.jsonl_store.JsonlStore` directly — jsonl fits event streams,
  not "the current state of N named slots"; same reasoning `provider_bootstrap.py`
  and `secrets/manager.py` already follow for their own single-document state.
- Extracted `doctor._resolve_provider_bin`/`_run` into `provider_probe.py`
  rather than importing `doctor.py` from `core` — importing `doctor` would
  create doctor→core→doctor once 4c's check called back into `core.versioning`;
  the extraction avoids the cycle and is a genuine reuse, not a rewrite.
- Gated the new doctor check behind `--core-version` (new flag, mirroring
  `--live`) rather than adding it to `run_all_checks()` unconditionally — the
  task's shared "feature flag / off = byte-identical" rule reads as the
  controlling constraint here even though "check ใหม่เท่านั้น" alone wouldn't
  have required it.
