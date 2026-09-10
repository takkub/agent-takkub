# #504 acceptance review — 2.1.0 storage cutover

Date: 2026-09-10. Reviewer: reviewer, mode code. Reviewed **e67da930b1562ec933ca39740552e9745016c311**, including 18d7093d, 2ee7fd1f, a17b84ba and 1f26b97e. Source was not edited. The branch's version constants still say 2.0.8; the 2.1.0 version transition was additionally exercised with in-process version patches.

**Verdict: FAIL / do not release 2.1.0 or close #504.** Five BLOCKER findings below include reproducible loss of the only copy of data, removal of live provider homes, and disappearance of the project list after an otherwise successful migration. **#566 must be fixed before release**; fixture refactoring cost does not justify deferring a runtime reader whose source the migration removes.

The concern that `mixed` simply bypasses promotion is **not** present at this commit: `run_boot_stage()` calls `apply_pending()`, and the default ladder includes promotion before domain steps and archival last. The actual failure is that this path keeps the old independent-step failure semantics for steps that now destructively depend on each other.

## Evidence and limits

- Read the complete [#504 issue](https://github.com/takkub/agent-takkub/issues/504) with `gh issue view 504 --json title,body`, plus #566. References below are repository-relative `file:line` at the reviewed commit.
- Ran the actual source in this worktree by setting `PYTHONPATH=<worktree>/src`; bare Python otherwise imports the installed package. Existing tests: **142 passed in 3.96s** across `test_core_migration_promote_v1.py`, `test_auto_migrate_boot.py`, `test_core_migration.py`, `test_core_migration_v1_steps.py`, `test_core_storage_layout.py`, and `test_doctor_auto_migrate.py`.
- Ran an additional artifact-only harness against real step implementations and `run_boot_stage()`. It injects copy failures, supplies isolated config paths, and asserts the observed defects. Its successful exit means the defects reproduced, **not** that acceptance passed. No real user data or provider credentials were mutated.
- Evidence directory: `C:/Users/monch/WebstormProjects/agent-takkub/runtime/exports/2026-09-10/agent-takkub/`. Files: `504-review-repro.py`, `504-review-repro.jsonl`, `504-pytest.log`. Final fixture run: `504-review-uuaal2dm/`. The harness can be rerun with the worktree's `src` on `PYTHONPATH` and `TAKKUB_ARTIFACTS_DIR` set to a scratch artifact directory; it creates new fixtures without deleting existing ones.
- Tested tagged **v2.0.8** code in a separate interpreter, exported with `git archive` into artifacts. A simple seed → current-code upgrade → restore → old-code boot preserves the demo project and passes the old migration validation. This is a headless storage compatibility smoke, **not** a wheel downgrade installation or authenticated six-provider/UI end-to-end test.
- No physical second drive was used. Copy implementation was traced; relocated-archive testing copies the archive to a different fixture root and simulates the original location being unavailable. This reproduces the absolute-path defect without claiming a physical cross-volume test.
- `gh run list --commit e67da930b1562ec933ca39740552e9745016c311` returned **no runs**. CI results for v2.0.8/140eb631 are not results for the reviewed commit. Windows local tests passed; macOS and six authenticated providers remain unverified for this revision.

## Acceptance matrix

| Requirement | Verdict | Evidence / finding |
| --- | --- | --- |
| Old installed `mixed` machine automatically promotes | Partial | `auto_migrate_boot.py:311` → `:325` → `engine.py:86` and `:181`; harness reports `mixed → pending_applied → v2`, archive created. Version bump breaks first-boot validation: H6. |
| Old dev + prod: archive every V1 file, count/checksum, validate green | FAIL | Dev boot explicitly skips at `auto_migrate_boot.py:305`; shared V1 directories are excluded (H7). Per-copy SHA-256 exists, whole inventory/continued integrity does not (H9). |
| Restore complete V1 and use 2.0.x | FAIL overall | Basic tagged 2.0.8 headless round-trip passes. Repeated/relocated archives, target collisions, live Kimi home, and generic rollback fail: B3/B5/H1–H3. |
| Fresh DATA_HOME has only the new layout, no floating JSON/archive | FAIL | First boot writes `auto-migrate-state.json` at the root; second boot archives it (H8). Project writes recreate `projects.json` (B1). |
| All six providers work, homes untouched | FAIL / runtime verification incomplete | Default Claude/Codex/OpenCode homes excluded from archival, but named Claude homes are archived (B4); Kimi home deleted/moved by undo/restore (B3). Full authenticated provider tests were not run. |
| No imports of dual_write / v2_authority | PASS | Source grep and Python AST import scan find none, including `v1_only_write`. Historical comments remain (L1). Generic JSON `legacy_reader` usage is not itself a retired-authority import. |
| Midway failure restores whole prior state with journal guard | FAIL | B2/B3/B5; step outcome journal is not an item-level crash recovery record. |
| Cross-volume operations avoid raw shutil.move | PASS for implementation choice | `verify_copy.py:58` uses `copytree`/`copy2`, then SHA-256 checks. Restore portability still fails H2; this is not a physical multi-drive test. |
| Disk space checked before moving | FAIL | `mixed` bypasses gate; estimator ignores most new work (H4). |
| Dev nested-layout exception is consistent across readers | FAIL | Same-process dev readers agree, but spawned worktree and primary cockpit disagree silently (H5). Dev never auto-archives. |
| TAKKUB_V2_AUTHORITY=0 warns without crash | PASS in doctor | Direct helper and existing tests return WARN. Warning is opt-in `doctor --storage-layout`; no general boot warning was found. |
| doctor --storage-layout no longer reports mixed | FAIL | `layout.py:171`/`:207` retain mixed; `doctor.py:3423` still calls it expected. Dev always retains nested v2, and creating a project returns installed state to mixed. |
| Release docs and migration guide | Not shipped | This report includes drafts. Existing CHANGELOG has no 2.1.0 entry or restore-v1 downgrade warning. |

## BLOCKER

### B1 — Successful archive removes the only project registry the application reads

**Owner: backend#3** (#566), coordinated with backend#2.

- `src/agent_takkub/config.py:461`, `:574`, `:582`, `:595`: the runtime registry remains `DATA_HOME/projects.json`; missing source returns `{active: None, projects: {}}`, and save writes that same old path.
- `src/agent_takkub/core/migration/steps_v1.py:411`, `:475`: migration writes the new `projects/registry.json` envelope, but that is not read by `config.load_projects()`.
- `src/agent_takkub/core/migration/promote_v1.py:369`, `:425`, `:429`: `projects.json` is selected, copied into the archive, and removed.

**Repro (`mixed_boot_projects`):** seed one demo project and a nested `v2/models/registry.json`; boot with installed-style `SETTINGS_HOME == DATA_HOME`. Observed `pending_applied`, layout `v2`, all **11** validation reports green, one archive, but `config.load_projects()` returns an empty project map. Real project directories still exist, but the cockpit has lost access to its project index. Subsequent saves/boots repeat the same failure, now also creating more archives.

**Required fix:** migrate both runtime reads and writes to the registry in the same change, update fixtures, and add a regression that loads and edits an existing project through application APIs after boot and again after reboot. Merely leaving `projects.json` unarchived does not satisfy the agreed single-layout acceptance.

### B2 — Pending migration continues after failed promotion and deletes nonempty v2

**Owner: backend#2** (migration engine / boot transaction).

- `src/agent_takkub/core/migration/engine.py:149`, `:172`, `:181`: pending mode intentionally does not stop on failure and has no post-batch verification barrier.
- `src/agent_takkub/core/migration/promote_v1.py:434`: archive checks only `is_dir()`, creates an **empty** archive `v2/` marker, then calls `shutil.rmtree(legacy_root)` at `:436`. It never proves that promotion emptied the source.
- `src/agent_takkub/auto_migrate_boot.py:243`: failed steps are rolled back only after the entire pending pass. It rolls back the failed promote step, not the successful archive step; no promote manifest exists to reconstruct the lost tree.

**Repro (`pending_failure`):** seed `v2/state/only-copy.json` with unique data and a V1 project; inject `OSError` in the first promote copy. Boot returns `pending_rolled_back`, archive exists, but the unique file is absent from nested source, new target, **and every archive**. The only copy was deleted. The per-version guard does not repair this loss.

**Required fix:** treat promotion, validation and archive as a dependency group within the existing engine; stop dependent operations on failure, refuse to archive a nonempty `v2`, and roll back the transaction to the prior inventory. Crash recovery also needs durable per-item progress/preimages before source removals: the current manifest is written after removals (`promote_v1.py:251`, `:253`; archive `:429`, `:446`), and journal records are only step outcomes (`journal.py:41`). Process death in that gap bypasses the in-memory exception handler entirely.

### B3 — Merged-directory undo deletes live data; restore relocates entire provider homes

**Owner: backend#2**, coordinate provider regression coverage with backend#3.

- `src/agent_takkub/core/migration/verify_copy.py:60` merges into an existing destination with `dirs_exist_ok=True`.
- `src/agent_takkub/core/migration/promote_v1.py:249` appends only successfully verified entries. `_undo_moved_entries()` then deletes the **whole destination** at `:144`–`:147`, without saving its preexisting contents.
- Successful promotion records only top-level names (`:258`). Restore copies and removes the **whole** top-level directory at `:307`–`:312`.
- `src/agent_takkub/config.py:294`, `:347`: Kimi's real isolated home remains `DATA_HOME/providers/kimi/default`.

**Repro (`merge_undo`):** keep live `providers/kimi/default/auth.json`; seed nested `v2/providers/claude/provider.json` plus `v2/state/test.json`. Let provider copy succeed and inject failure on the next copy. Undo removes the entire top-level `providers`, including the original Kimi auth, while reporting the promote failure rolled back.

**Repro (`restore_provider`):** let promotion/archive succeed instead, then run archive rollback followed by promote rollback, as the CLI does. Both return `ok=True`, but Kimi auth now lives under `v2/providers/kimi/default`; the actual Kimi home is gone. This violates the never-touch requirement without any copy failure.

**Related partial-copy repro (`partial_copy`):** a copy that creates a destination file and then raises is not yet appended to `done`; the partial destination remains. Also, if directory removal deletes some children then fails while the source directory still exists, `_undo_moved_entries` treats the source as intact and discards the backup — a further loss path by inspection.

**Required fix:** record file-level ownership and original destination preimages; undo/restore only files introduced or overwritten by this transaction. Preserve provider-owned subtrees, including Kimi's. Do not remove merged destination directories wholesale.

### B4 — Broad archival selects live named-provider homes and still-active settings

**Owner: backend#2** (inventory), with backend#3 / accounts owner (remaining readers).

- `src/agent_takkub/core/migration/promote_v1.py:104` excludes only exact default names (`claude-config`, `codex-home`, `opencode-home`) and domain roots; `:369` archives everything else.
- `src/agent_takkub/accounts_adapter.py:362`, `:369`: a real named Claude account defaults to `DATA_HOME/claude-config-<name>`, which is **not** excluded.
- `src/agent_takkub/user_profile.py:36`, `:97`, `:118`: named accounts are still loaded from top-level `user-profiles.json`; this file is also archived without replacement of its reader.
- `src/agent_takkub/core/accounts/facade.py:71` still uses the user-profile bridge when no account pool resolves; archiving that registry is therefore a runtime behavior change, not harmless old metadata.
- Other active files include `core_v2_settings.py:55`/`:121` (scheduler policy/context strategy) and `config.py:512` (`project-skills` directory). These are not on the archive skip list either. They need a source-to-live-target inventory before blanket retirement.

**Repro (`named_profiles`):** seed a registered Claude account `team` pointing to `claude-config-team/auth.json`. `ArchiveV1LegacyStep.apply()` returns success, but profile names change from `[default, team]` to `[default]`, and the live account home no longer exists. Its bytes are archived, but the running provider/account configuration no longer points at them.

**Required fix:** protect actual configured provider homes (not just three literal basenames) and audit every archived entry for a migrated live reader/writer. Add default and named-account preservation tests. Do not claim every provider home is untouched from the default-Claude-only existing fixture.

### B5 — Generic engine rollback deletes the nested tree it just restored

**Owner: backend#2**.

- `src/agent_takkub/core/migration/engine.py:287` rolls back steps in reverse; promotion rollback recreates nested `v2/`.
- `src/agent_takkub/core/migration/engine.py:324` then unconditionally removes `DATA_HOME/v2`. Its comment claiming this cannot be the only copy is false after the new move-based promotion.

**Repro (`engine_rollback`):** seed `v2/models/only.json`, run the real promote/archive pair, then invoke `MigrationEngine([promote, archive], data_home=home, journal=journal).rollback()`. Every report is green and the unique file has **zero copies** anywhere in DATA_HOME afterward. This deliberately isolates the final cleanup defect from unrelated old step rollback behavior; the same unconditional cleanup runs in the default engine too.

**Required fix:** remove/rework the old disposable-mirror cleanup assumption and restrict rollback to state owned by this transaction. Cover automatic failure rollback and explicit `migrate rollback`, in addition to the separate `restore-v1` command.

## HIGH

### H1 — Latest-only restore cannot reconstruct an earlier full archive after later small archives

**Owner: backend#2.** `promote_v1.py:518`, `:562` selects only the most recent timestamped manifest; `:534` restores only its entries. There is no archive selector or generation/parent linkage in `cli.py:3158`.

**Repro (`latest_archive`):** first archive `projects.json` + `custom-roles.json`; create a new `auto-migrate-state.json`; archive again; restore. Two archives exist, restore returns success, but neither original data file is restored. Fresh boot's own state file and still-active top-level writers make later archives a real runtime condition, not just manual misuse. Retaining all archive directories is insufficient if the supported restore command cannot reconstruct the generation.

### H2 — Moved archives contain absolute source paths and missing members silently succeed

**Owner: backend#2.** `promote_v1.py:442` stores absolute archived paths; `:536` trusts these paths instead of resolving entries relative to the selected manifest. Missing entries are silently skipped at `:538`–`:539`.

**Repro (`relocated_archive`):** copy a populated `backups/` tree to a new DATA_HOME and make the original location unavailable. The new archive physically contains `projects.json`, but rollback reports `ok=True, restored=[]`. Moving the archive between drives has the same path-dependence. No cross-drive `shutil.move` is involved in this defect.

**Required fix:** use archive-relative paths with containment checking, fail closed on missing/corrupt members, and validate the selected generation before changing targets.

### H3 — Restore overwrites current files without preserving them and does not stop between failed halves

**Owner: backend#2.** `promote_v1.py:540` calls a merge/overwrite copy with no destination backup. Repro `restore_collision`: archive `projects.json='old'`, write current `'new-current'`, restore; outcome is `'old'` and no copy of `'new-current'` remains. This is not a lossless restoration of a populated installation.

Additionally `cli.py:3158` uses a two-element list, so `promote-v2-root` rollback still executes if `archive-v1-legacy` rollback reports false. `promote_v1.py:314` can return after partially moving earlier entries without reversing them. There is no transaction-wide disk/collision preflight. Preserve current state before restoring, and stop dependent restore steps on failure.

### H4 — Disk gate is bypassed on the actual upgrade path and underestimates the work

**Owner: backend#2.** `auto_migrate_boot.py:312` returns through pending mode before reaching `_disk_has_room()` at `:331`. Neither pending engine nor the move steps nor CLI apply/restore performs an equivalent preflight.

`auto_migrate_boot.py:130` estimates only `runtime/`, returning zero if that directory is absent. It omits nested V2 payloads, V1 files/directories selected for archival, existing destination backups, and a possibly separate archive volume.

**Repro (`disk_gate`):** a populated `v2/models` with no runtime accepts zero free bytes in `_disk_has_room`; booting the same mixed fixture with the disk check patched to false calls that check **zero times** and still migrates. Check every affected volume and actual copy set before either upgrade or restore.

### H5 — Dev worktree panes silently resolve the primary cockpit's wrong root

**Owner: backend#3** (pre-req path resolver), coordinate backend#2 layout.

- `core/storage/v2_target.py:47`–`:55`, `:77`: a worktree obtains primary DATA_HOME from `TAKKUB_PORT_FILE=<primary>/runtime/port`.
- `core/storage/layout.py:125` chooses nested layout only if the requested home equals **this process's** `config.REPO_ROOT`.
- `provider_models.py:19`, `role_models.py:49`, `provider_config.py:87` use that combined path for shared global settings.

**Repro (`dev_worktree`):** cockpit `DATA_HOME=REPO_ROOT=primary` resolves `primary/v2`. Child `DATA_HOME=REPO_ROOT=child`, with port file pointing to primary, resolves `primary` instead. Model/routing reads and writes diverge with no warning. Installed-primary processes work differently because their intended layout is top-level. Preserve the host's layout identity explicitly; comparing against the child's checkout cannot recover it.

Separately, plain dev boot is unconditionally skipped (`auto_migrate_boot.py:305`). Keeping nested storage can be a deliberate development exception, but the issue's dev acceptance must explicitly acknowledge it; it is not completion of the same physical migration in dev.

### H6 — A real version bump leaves the first upgraded boot's marker stale while reporting success

**Owner: backend#2.** `engine.py:87` runs `VersionMarkerStep` before promotion. `steps.py:66` resolves the marker via `core_home()` (`core/storage/paths.py:29`), which points to runtime/core before a top-level system exists. Promotion then copies the old `v2/system/version.json` into top-level system. Pending mode never re-validates the earlier marker step after that path switch (`engine.py:181`).

**Repro (`version_upgrade`):** seed `v2/system/version.json` with app=2.0.8; patch the app and marker step's running version to 2.1.0; boot. It returns `pending_applied`, then `MigrationEngine.validate()` immediately fails `version-marker`. A subsequent boot can repair it, but first-boot acceptance requires a green result. Existing tests use the same app version or omit a populated old nested system marker, masking this transition.

### H7 — Archive deliberately omits V1 files inside shared directory names

**Owner: backend#2.** `promote_v1.py:67`–`:81` explicitly documents the gap; `_V2_TOP_LEVEL_NAMES` excludes entire `agents/` and `projects/` from archival. Actual old sources are `steps_v1.py:185` (`projects/<slug>/role-providers.json`) and `:227` (`agents/<role>.md`).

**Repro (`shared_legacy`):** seed those two legacy files. Archive returns success, `validate()` is green, no archive is created, and both legacy files remain live. Therefore the archived inventory is **0 of these 2 files**, not a complete V1 count/checksum match. They are preserved, but not where the promised complete rollback snapshot says they should be. Archive individual legacy files under shared roots without moving the newly promoted domain wholesale.

### H8 — Fresh boot immediately recreates floating state; active writers defeat one-shot archival

**Owner: backend#2 / backend#3.** `auto_migrate_boot.py:359` saves state via `_state_path()` at `:93`; installed settings home is DATA_HOME (`config.py:216`). That write happens **after** archive and validation. On the next boot `ArchiveV1LegacyStep` selects the new root file again.

**Repro (`fresh_two_boots`):** first boot returns `applied` with top-level `auto-migrate-state.json`; second returns `pending_applied` and creates a V1 archive even though the machine never had V1 data. Existing fresh integration test (`tests/test_auto_migrate_boot.py:727`) checks absence of `v2/` and `backups/` on only the first boot; it does not assert that root JSON is absent.

Other still-active root writers are visible at `core_v2_settings.py:55`, `:140` and B1/B4. Moving those files away also removes live preferences or retry-state rather than retiring dead sources. Establish permanent new locations and test two boots plus normal settings/project edits. Do not merely suppress the doctor classification.

### H9 — Green migration validation can mean no target integrity check at all

**Owner: backend#2.** Once archive `_pending()` is false, `engine.py:267`–`:280` returns unconditional success for five domain steps. `apply_pending()` also skips already-applied domain steps at `:177` without validating any target. Archive validation itself checks only absence of candidates (`promote_v1.py:499`), not stored file counts, checksums or readability of the archive. Thus deleting/corrupting `models/registry.json` after archival can still produce green migration validation and no repair attempt.

`verify_copy.py:65` does compare every enumerated source file's SHA-256 during each successful copy. That is valuable, but `CopyVerification` counts are discarded by the callers, no original full V1 inventory is persisted, and the destination can contain unrelated extra files. A green result does not establish the issue's complete archive or a healthy readable new registry. Validate schemas/required targets and a persisted generation inventory independently of the retired live V1 source.

## MED

### M1 — Doctor still advertises mixed as expected and authority paths as v2/

**Owner: backend#3.** `doctor.py:3403` directly emits `layout_state()`. At `:3423` it says coexistence is expected until a future deprecation phase and not a problem. `layout.py:171` returns mixed for every nested dev layout; `:207` returns mixed when `projects.json` is recreated by its current writer. `doctor.py:3457` says all domains directly use `v2/` even on the new top-level installed layout.

The retired environment helper at `doctor.py:3445` works: `TAKKUB_V2_AUTHORITY=0` produced WARN, no exception, and AST scanning found no retired gate imports. Clarify doctor output for a deliberate dev exception, incomplete production promotion and actual storage roots. If the scope requires a warning at normal boot, wire one there: this helper is only included in opt-in storage-layout diagnostics.

## LOW

### L1 — Stale comments contradict the implemented migration order and remaining bridges

**Owner: backend#2 / backend#3.** Clean up with fixes, not as proof of behavior:

- `core/storage/layout.py:9` claims archive happens before names are reused; actual order is marker → promote → domain steps → archive (`engine.py:86`).
- `auto_migrate_boot.py:1` promises steady-state mixed and retains copy-never-move descriptions (`:65`, `:117`).
- `core/storage/layout.py:122` still mentions provider-config's dual-write gate; `steps_v1.py:198` names the removed `dual_write_routing` API.
- `core/storage/paths.py:20` says there is no internal-store migration yet, though `CoreInternalStoreStep` is in the ladder.
- `doctor.py:3391` describes every install as V1; `:3439` describes direct targets as `v2/`.
- `accounts_adapter.py:366` promises a future named-home move that the current blanket archive does not implement.

Historical retirement explanations in `v2_target.py` and router comments saying there is **no** old gate/drift telemetry are accurate and should not be counted as surviving functionality. `core/storage/legacy_reader.py` still supplies a generic JSON reader; the name alone does not prove a live V1 fallback. The actual profile bridge in B4 does still read unmigrated V1 data.

## Provider-home trace

| Provider | Actual source of home | Migration result / limitation |
| --- | --- | --- |
| Claude default | `config.py:233`, `:250` → DATA_HOME/claude-config; `pane_env.py:488` supplies selected config dir | Exact default basename excluded. Named/default-external profile selection is separate; named installed homes fail B4. |
| Codex | `config.py:276` → DATA_HOME/codex-home via CODEX_HOME | Exact default basename excluded; spawn injection `pane_env.py:585`, `spawn_engine.py:2332`. No authenticated run performed. |
| OpenCode | `config.py:282` → opencode-home/data and opencode-home/config via XDG pair | Parent excluded; same injection path. No authenticated run performed. |
| Kimi | `config.py:294` → providers/kimi/default via KIMI_SHARE_DIR | Archive skips providers, but promote merges into it and undo/restore deletes/moves the live home (B3). |
| Gemini | `config.py:300` records no supported home-isolation knob; existing agy default is outside DATA_HOME | Migration archive cannot reach that external default tree. This is code-path evidence, not a live Gemini authentication test. |
| Cursor | `config.py:309` records unverified isolation; `cursor_helper.py:110` reads existing CURSOR_HOME override | No new migration of that external home is implemented; cannot certify normal provider operation from this review alone. |

Archive excludes `venv`, `skills`, `psmodules`, `runtime`, and worktrees (`promote_v1.py:104`). That is not a blanket never-touch guarantee for **promotion**, which accepts every nested top-level entry (`:198`) and merges into matching destinations. Normal migration journal/version writes also remain under runtime/core by design. No archive expiry/auto-cleanup implementation referencing `v1-archive` was found; `restore-v1` leaves archive directories intact. B2/B5 still destroy data outside those retained archives.

## Remaining release gates

1. **Soak prerequisite not established.** #504 explicitly dates production 2.0.0 soak start to 2026-09-07 and requires at least one week with zero V1-only writes/model-pin drift on both dev and prod. Review date is 2026-09-10; that stated window has not elapsed (earliest 2026-09-14). `gh issue view 502` reports closed 2026-09-07T10:19:52Z, which is not a week of evidence. Lead must supply/finish the stated soak criteria before claiming acceptance.
2. **CI not established on this SHA.** `.github/workflows/ci.yml:24` defines Windows/macOS/Linux and `:104` defines Windows/macOS installed-mode jobs. No run was returned for e67da930. Do not reuse green runs for earlier 140eb631 as this change's evidence. Devops/Lead must run the fixed candidate through required CI.
3. **Runtime provider/restore acceptance remains incomplete.** Reviewer/e2e should cover authenticated claude/codex/gemini/opencode/kimi/cursor after migration, a real isolated old-wheel downgrade, multi-archive selection, physical cross-volume copy, destination collisions, process death, and checksummed inventories including named homes and shared legacy files. Current tests passing is insufficient in view of the reproduced defects.
4. **Version and documentation are pending.** `pyproject.toml:3` and `src/agent_takkub/__init__.py:3` remain 2.0.8. Treat the text below as a proposed entry for the fixed 2.1.0 release, not an announcement that this revision is safe.

## Draft CHANGELOG entry — publish only after fixes and acceptance

```markdown
## [2.1.0] - YYYY-MM-DD

### Changed

- Installed storage now uses one layout directly under DATA_HOME. On the first upgrade boot, the migration validates the new data and preserves the complete retired V1 layout in backups/v1-archive-<timestamp>/ before completing the transition. The former live v2/ directory is retired.
- Migrated domains read and write one location. The dual-write and V1 authority fallback paths are removed. TAKKUB_V2_AUTHORITY no longer changes storage behavior; doctor --storage-layout warns when the retired variable is set.
- V1 archives have no automatic expiry or cleanup. Provider homes, credentials, venv, skills, psmodules, and runtime remain in their supported locations.

### Upgrade and downgrade

- Back up DATA_HOME and close all cockpit instances and agent panes before upgrading. After first boot, run takkub migrate validate and takkub doctor --storage-layout; verify projects, accounts, and provider access before resuming work.
- **Downgrading below 2.1.0 requires restoring the V1 archive first.** Keep the 2.1.0 CLI installed, close the cockpit and agent panes, and run takkub migrate restore-v1 --json. Confirm successful restoration before installing 2.0.x. Do not start the older application against an unrestored 2.1.0 data directory.
- **For a 1.x installation, upgrade through a supported 2.0.x release first.** Boot 2.0.x, complete its migration and validation, and verify your data before upgrading to 2.1.0. This release guide does not promise a direct 1.x-to-2.1.0 jump.
- Source/dev checkouts retain the nested development layout to avoid collisions with tracked repository folders. Their storage paths must remain consistent between the primary cockpit and worktree panes; they do not perform the installed application's automatic physical promotion.
```

The draft's complete-archive/provider-safety statements are requirements for the fixed release. They are false for e67da930 and must not be published as current behavior. The dev exception should be explicitly approved as a scope clarification to #504 if it is retained.

## Draft migration guide — 1.x → 2.0.x → 2.1.0

1. Record the active DATA_HOME and take a complete backup outside that directory. Include existing archives and provider data; use the same intended home throughout the upgrade.
2. Close every cockpit instance and agent pane using that home. Install a supported 2.0.x release using the installation's existing package manager. Start it once, allow its migration to complete, then run `takkub migrate validate` and `takkub doctor --storage-layout`. On 2.0.x, mixed V1 plus the nested mirror is expected. Check the project list, named accounts, custom roles, model/provider settings and actual provider access.
3. Only after the validated 2.0.x state and the release prerequisites are established, close it and install the fixed 2.1.0 release. First installed boot performs the promotion/archive transaction. Do not enable the retired authority flag as a recovery method.
4. Run `takkub migrate validate` and `takkub doctor --storage-layout` again. Confirm the installed live layout has no nested v2 directory, the selected V1 archive has a complete verified inventory, and application readers see the original projects/accounts. Keep the archive indefinitely unless you deliberately choose to remove it.
5. If downgrade is needed, close all instances/panes, preserve a separate backup of the current 2.1.0 state, and use the **still-installed 2.1.0** `takkub migrate restore-v1 --json`. Any failure or incomplete restore must stop the downgrade. Install 2.0.x only after the intended archive generation is restored and verified, then boot and recheck its data/provider access. Do not assume generic `migrate rollback` is an equivalent downgrade operation.

The current CLI cannot safely satisfy steps 4–5 in the documented edge cases. The final guide must name the archive-generation selection mechanism and collision/relocation handling delivered by the fixes; do not invent flags that do not exist today. No direct 1.x-to-2.1.0 guarantee is made merely because a V1 ladder branch exists.

## Round 2 — 60abb771

Date: 2026-09-10. Reviewed main **60abb771**, containing backend#3 **8208bf98** and backend#2 **751b6b71**. This section supersedes the Round 1 verdicts for this candidate, without rewriting the historical evidence. Source was not edited. The branch still declares **2.0.8**; the version-transition harness patches the running version to 2.1.0 in isolation.

**Verdict: FAIL — not releasable.** The original repro suite now passes **14/15 acceptance assertions**, and the tagged 2.0.8 headless round-trip also passes. H5 remains HIGH. Additional fault injection proves a **BLOCKER in source-removal undo**: failure removing the second source deletes the only copy of the first source, in promote, archive and promotion rollback. Restore still accepts missing/corrupt archive members. Passing named tests does not close those failure paths.

### Round 2 evidence

- Reused the original artifact harness's fixtures and copy-failure injections, changed defect assertions to assert the correct state, and caught each case separately so one failure did not stop the remaining cases. H1 now invokes the actual `_cmd_migrate_restore_v1()` orchestration: the individual archive step still defaults to the latest generation, while the CLI now walks all generations. H3 checks the fixture's `BackupManager.root`, which is deliberately outside fixture DATA_HOME, for preserved current bytes.
- Original artifact retained: `runtime/exports/2026-09-10/agent-takkub/504-review-repro.py`. Runnable updated harness: `504-round2-repro.py`, generated by `504-round2-build.py`; output `504-round2-repro.jsonl`. Final fixture root: `504-review-czfrek_y/`. **Exit 1 means an acceptance failure**, unlike the Round 1 defect-asserting harness.
- Added artifact-only `504-round2-extra.py`, output `504-round2-extra.jsonl`, fixture root `504-review-44o8u3p1/`: 13 supplementary checks, **2 PASS / 11 FAIL**. These cover source-delete failures in three operations, missing/corrupt restores, nested provider homes, actual dev model read/write divergence, omitted disk inventory, required-target corruption, older-archive corruption, interruption/reboot/restore, project edit/reboot, and an AST retired-import scan.
- Ran source with `PYTHONPATH=<this worktree>/src`, never the installed package. Existing suites: **186 passed** across migration/promote/boot/layout/doctor/CLI/Core V2 settings, plus **173 passed** across project registry/provider models/role models/provider config/context strategy. Total **359 existing tests passed**. Logs: `504-round2-pytest.log`, `504-round2-readers-pytest.log`. Import contracts: **29 kept / 0 broken**, `504-round2-imports.log`.
- All artifacts are under `C:/Users/monch/WebstormProjects/agent-takkub/runtime/exports/2026-09-10/agent-takkub/`. Re-run either harness in PowerShell with `$env:PYTHONPATH=(Join-Path (Get-Location) 'src')` and the same `TAKKUB_ARTIFACTS_DIR`; each creates a new fixture directory. No real credentials or user data were touched.
- Re-read [#504](https://github.com/takkub/agent-takkub/issues/504) and [#568](https://github.com/takkub/agent-takkub/issues/568) through `gh issue view`. Read backend#2's supplied session note. `gh run list --commit 60abb771` returned **no runs**. Windows local results are not Windows/macOS candidate CI results. No authenticated provider run, physical second-volume test, real old-wheel downgrade or actual OS process kill was performed. The interruption test raises `KeyboardInterrupt` after a real source removal, bypassing the ordinary exception handler, then runs a real boot and restore.

### Original harness — every requested case

| Case | Round 2 verdict | Observed result |
| --- | --- | --- |
| `mixed_boot_projects` | PASS | `mixed → pending_applied → v2`; demo project present; 11 validation reports green. Supplementary save/edit/reboot preserves demo + second project without recreating `projects.json`. |
| `pending_failure` | PASS | Failed first promote copy stops the pass; unique nested source survives and no V1 archive is created. |
| `merge_undo` | PASS | Failed second copy preserves live Kimi auth and nested source. Delete-phase failure is separately BLOCKER R2-B1. |
| `restore_provider` | PASS | Archive + promotion rollback leave Kimi auth in its actual home, never under nested v2. |
| `partial_copy` | PASS | Partial destination is removed; original nested file survives. |
| `named_profiles` | PASS | Direct-child registered `claude-config-team` and `[default, team]` survive. Nested configured homes still fail R2-H2. |
| `engine_rollback` | PASS | Explicit two-step engine rollback leaves the unique nested model file intact. |
| `latest_archive` | PASS | Public CLI orchestration restores projects and custom roles from the older generation as well as the newer generation. |
| `relocated_archive` | PASS | Schema 2 archive restores projects from the new root while original archive location is unavailable. |
| `restore_collision` | PASS | Restored target has old bytes; preimage retains `new-current` in the fixture backup store. |
| `disk_gate` | PASS | Nested payload rejects zero free space; mixed boot calls the gate once and skips. Broader inventory remains incomplete, R2-H3. |
| `version_upgrade` | PASS | First simulated 2.1.0 pending boot validates all steps green. |
| `shared_legacy` | PASS | `agents/*.md` and `projects/*/role-providers.json` move into the archive individually. |
| `fresh_two_boots` | PASS | No root JSON after first boot, no V1 archive after second, state exists in `system/auto-migrate-state.json`. |
| `dev_worktree` | FAIL / HIGH | Primary resolves `primary/v2`; child with primary port file resolves `primary`. H8 does not remove this condition. |

Additional original `tagged_2_0_8_roundtrip`: **PASS**, with assertions on restoration, old project visibility and old validation. This is the same limited headless compatibility smoke described above.

### Acceptance matrix — every original row rechecked

| Requirement | Round 2 verdict | Evidence / remaining condition |
| --- | --- | --- |
| Old installed mixed machine automatically promotes | PASS, normal path | Real boot + registry edit/reboot pass; H6 version transition passes. Fault paths remain separate release blockers. |
| Old dev + prod: every V1 file archived, counts/checksums, green validation | FAIL | Installed normal path and two shared-file patterns pass. Dev deliberately skips boot migration; complete generation/target integrity still absent; provider-home inventory incomplete. |
| Restore complete V1 and use 2.0.x | FAIL | Happy-path tagged smoke, generation selection and relocation pass; missing/corrupt member handling and source-delete undo fail. Old-wheel validation still outstanding. |
| Fresh home has only new layout, no floating JSON/archive | PASS for two clean boots | H8 state location fixed. Normal project writes also remain V2. Existing live `user-profiles.json` and legacy settings fallbacks mean this does not certify that all account/settings workflows satisfy the broader single-layout scope. |
| All six providers work, homes untouched | FAIL | Default-home/Kimi/direct-child named-home fixtures pass. A registered nested home is archived. Six authenticated provider runs remain unverified. |
| No imports of dual_write / v2_authority | PASS | Real `rg` plus Python AST scan includes `v1_only_write`; no retired imports. Generic JSON helpers/historical comments are not retired-authority functionality. |
| Midway failure restores whole prior state with journal guard | FAIL / BLOCKER | Copy-failure cases improved; source-delete failures lose the only copy. Interruption followed by reboot also loses the complete promotion inventory. |
| Cross-volume operations avoid raw shutil.move | PASS for implementation | `copytree`/`copy2` plus SHA-256; no raw move in these helpers. No physical multi-volume execution claimed. |
| Disk checked before moving | FAIL overall | Actual mixed boot gate fixed; estimator excludes archive-only payload/preimages, and manual apply/restore lack a complete preflight. |
| Dev nested exception consistent across readers | FAIL / HIGH | H5 persists in real model reads/writes and now also Core V2 settings resolution. |
| TAKKUB_V2_AUTHORITY=0 warns without crash | PASS in doctor | Existing retirement tests pass; opt-in `doctor --storage-layout` helper warns. No general boot warning established. |
| doctor no longer reports mixed | FAIL literally / M1 messaging PASS | Installed migrated fixture reports v2; doctor now WARNs incomplete installed mixed state. Dev explicitly reports mixed with an explanatory OK message. Scope exception needs Lead/operator acceptance. |
| Release docs and migration guide | PASS as unreleased documentation | Finalized candidate entry in `CHANGELOG.md` and `docs/v2/2.1.0-migration-guide.md`; release hold explicit, real flags documented, restore-before-downgrade and 1.x-via-2.0.x mandatory. |

### R2-B1 — BLOCKER: deletion-phase undo destroys the only verified copy

**Owner: backend#2. Reopens the safety substance of B2/B3/H3; not fixed by destination preimages.**

`core/migration/promote_v1.py:176` `_two_phase_move()` copies every pair, then calls `_remove(src)` sequentially at `:204`. The source-removal loop is still inside the exception handler. If deletion of source 2 fails after source 1 was removed, callers invoke `_undo_moved_entries()` at `:235`. That helper ignores `_src` and removes the verified destinations; it restores only pre-existing **destination** preimages. New destinations have no preimage. Therefore source 1 and its verified target both disappear.

Supplementary `delete_phase_promote`, `delete_phase_archive`, and `delete_phase_rollback` each inject `OSError` on the second `_remove` after the first succeeds. All three leave **zero copies** of the first unique file across the fixture and its backup directory. Apply reports even say “rolled back”. This is ordinary handled I/O failure, not hypothetical process death; source file locks/permission changes and partial directory removal are enough to reach the flawed contract.

Required: preserve recoverable source inventory through every deletion, restore removed/partially removed sources before discarding verified destinations, and do not suppress failed undo while claiming recovery. Regression tests must inject source removal and undo failures, not just copy failures. Persist the transaction before destructive work; manifest writes at `:456` and `:760` currently occur only after source removals and are outside the helper's undo boundary.

### R2-H1 — HIGH: restore still accepts missing/corrupt archives

**Owner: backend#2. H2/H3/H9 only partially closed.**

`ArchiveV1LegacyStep.rollback()` (`promote_v1.py:851`) builds pairs from the selected manifest but skips a missing member at `:888`. It never checks the manifest's stored hashes before `_copy_only_transaction`. `copy_verified()` verifies equality to the **current** source bytes, so a corrupted archived file copies successfully. Both supplementary cases return all-green CLI reports: missing member restores nothing; corrupt member installs `corrupted`. Calling `archive.validate()` immediately before the command correctly reports false for these fixtures, but restore does not call it.

Schema 2 relative paths fix the original relocation repro, but path containment/schema validation is still absent. An explicit unknown timestamp returns the “no archive” success report (`:873`), and CLI then runs promotion rollback; unreadable generations are silently omitted by `list_v1_archives()`. Verify the requested generation and every member/hash before **any** target mutation, reject unknown selectors, and stop on incomplete inventory. Per-generation transactions also do not undo already-restored older generations if a later generation fails.

### R2-H2 — HIGH: registered nested provider homes are still archived

**Owner: backend#2, coordinate accounts owner/backend#3. B4 narrower repro fixed, full requirement not closed.**

`_named_account_home_names()` (`promote_v1.py:588`) protects only `Path(config_dir).parent == data_home` (`:613`). A valid registered `DATA_HOME/team-homes/claude` is therefore unprotected: `team-homes/` becomes an archive candidate and the live auth path disappears after a successful archive. Supplementary `nested_named_home` reproduces this. `user_profile.py` accepts configured paths and does not restrict them to immediate children. Protect the containing top-level subtree for configured descendants (and account for normalized paths); a malformed registry must not silently justify sweeping possible credential homes. Default/direct-child coverage is insufficient.

### R2-H3 — HIGH: disk preflight remains incomplete

**Owner: backend#2. H4 original repro fixed, full requirement not closed.**

`auto_migrate_boot.py:169` sums only runtime + nested V2 bytes and explicitly omits V1 archive candidates. `disk_archive_inventory` seeds an 8 KiB archive-only file, no runtime/nested V2; estimate is **0**, and **zero free bytes is accepted**. Preimages can also be much larger than the promoted subtree (e.g. merging into a populated providers directory). `_two_phase_move` and `_copy_only_transaction` have no per-volume disk check; CLI apply/restore bypass boot's gate. Count the actual pair set plus preimages for each affected volume. An “ordinary files are small” assumption cannot satisfy archive-complete safety.

### H5 decision — still HIGH; H8 broadens the affected readers

**Owner: backend#3 (shared resolver), coordinate backend#2 for layout identity.**

`v2_target.py:47` recovers primary DATA_HOME from the port file, then `layout.py:132` chooses nested versus flat by comparison with **the child's** `config.REPO_ROOT`. The primary's identity is lost. This does not involve `config.load_projects()` and was never dependent on the old H8 state-file location.

`dev_real_read_write` uses real `provider_models.set_model()`/`model_for()`: cockpit saves `primary-model`; child reads `None`; child saves `pane-model`; cockpit still reads `primary-model`. `provider_models.py:15`, `role_models.py:45`, and `provider_config.py:83` use this resolver. New `core_v2_settings.path()` (`:54`) also uses `effective_data_home(prefer_primary=True)`: primary settings go to `primary/v2/config/core-v2-settings.json`, child settings to `primary/config/core-v2-settings.json`. H8 fixes the installed root writer, **not** the shared dev-root mismatch. Preserve the host layout identity; test reads and writes through real APIs in both process contexts.

### #568 decision — neither gap is accepted as release-safe here

The sentence in [#568](https://github.com/takkub/agent-takkub/issues/568) saying neither gap blocks 2.1.0 “per reviewer's severity” is **not this reviewer's approval**. HIGH is a severity, not a release waiver. The current acceptance explicitly requires complete verified migration and whole-state rollback; both gaps still violate it.

1. **H9 — HIGH, integrity remains incomplete.** Archive manifests persist per-file SHA-256, and the latest archive's corruption test passes. But `ArchiveV1LegacyStep.validate()` checks only `_find_latest_manifest()` (`promote_v1.py:818`), while default restore consumes all generations. `older_archive_integrity` corrupts an older generation and validate stays green. `engine.py:337`/`:341` substitutes unconditional success for five domain validators after archival. `domain_integrity` corrupts `projects/registry.json`; all migration validation reports stay green while `config.load_projects()` returns an empty list. Full required-target schema/readability checks and a trustworthy generation inventory are needed; live mutable targets cannot simply be required to retain their original byte hashes forever.
2. **Crash/restart — HIGH, supported recovery is not guaranteed.** Preimages exist only for pre-existing destinations and the promotion manifest is committed after destructive source cleanup. The journal has step results, not durable per-item progress. `crash_restart_restore` interrupts after removal of the first promoted source, then runs boot and CLI restore. Boot reports `pending_applied`, restore is all green, but the first file remains only at the top level; it is never restored to its original nested path. The second promotion overwrites the manifest with the remaining candidates. This disproves the claim that an operator can always fix such a crash merely by running existing rollback/restore commands. It does not claim those bytes were deleted: their recorded restoration path was lost. Automatic resume could be deferred only if a tested durable manual recovery contract and an explicit acceptance change replace it; neither exists here.

R2-B1 is a separate, stronger **BLOCKER**: ordinary caught deletion errors actually lose bytes. Opening #568 does not address that defect or make this candidate releasable.

### Required code traces and closure ledger

| Finding / requested trace | Final Round 2 assessment |
| --- | --- |
| B1 / config registry | Original BLOCKER closed. `config.py:598` reads V2 envelope; `:633` writes it when present; real edit + reboot pass. Missing/corrupt target still falls back to V1/empty, covered by H9. |
| B2 / pending halt | Original first-copy deletion repro closed. `engine.py:240` stops after promote failure; archive `:699` independently refuses nonempty V2. Whole-transaction guarantee remains FAIL R2-B1/#568. |
| B3 / transaction + BackupManager | Copy-failure undo and sibling Kimi protection PASS. Helpers back up existing destination before mutation; `BackupManager.backup()` writes a timestamped copy. It is a destination preimage, not a durable source progress record. Source-delete undo FAIL R2-B1. |
| B4 / account inventory | Exact direct-child named account and live registry PASS. Nested configured home FAIL R2-H2. `user-profiles.json`/`project-skills` are intentionally still live and skipped. |
| B5 / `_remove_if_empty_dir` | Original BLOCKER closed. `engine.py:50` returns when it sees files; final rollback calls it at `:397`, retaining restored unique content. This is a pre-scan, not an atomic no-writer guarantee. |
| H1 / restore generation CLI | Original repro closed by `cli.py:3158`–`:3174`, oldest-first default plus selector/list. Selection alone does not establish complete/safe restore. |
| H2 / schema 2 relative paths | Relocated schema 2 archive PASS. Promote manifest records relative file ownership; archive manifest records relative members + hashes. Missing/corrupt/unchecked paths remain R2-H1. Schema 1 promote dirs are skipped when no per-file list exists (`:523`); no full old-manifest recovery guarantee. |
| H3 / preimages and sequencing | Collision preservation and stop-after-failed-archive PASS. `_copy_only_transaction` keeps archive source intact and tracks partial attempts. Promotion rollback still has R2-B1; restore has R2-H1. |
| H4 | Nested payload and mixed boot gate PASS; overall FAIL R2-H3. |
| H5 | FAIL / HIGH, real read/write repro above. |
| H6 | Original repro closed. `engine.py:244` re-applies version marker immediately after successful pending promotion. This evidence is for pending boot, not a claim that every separate apply/failed-marker path has the same barrier. |
| H7 | Two named shared legacy patterns now individually archived; original repro closed. Not an exhaustive live-source inventory. |
| H8 | Two clean boots PASS. Boot state is under `system/`; Core V2 settings are under **`config/`**, not system, with legacy read fallback. Dev settings remain affected by H5. |
| H9 | Latest archive checksum regression PASS; required domains/older generations FAIL, #568. |
| M1 | Messaging fixed: doctor distinguishes deliberate dev mixed from incomplete installed mixed and names actual layout. Literal removal of all mixed reporting not implemented. |
| L1 | PARTIAL / LOW. Several old comments fixed, but `_undo_moved_entries` claims source is never touched even though its caller deletes it; CLI apply help still says copy-never-move. Document safety from control flow, not these comments. |

### Release disposition and documentation

**Do not release 2.1.0 or close #504 at 60abb771.** backend#2 owns R2-B1 and the remaining restore/inventory/recovery defects; backend#3 owns H5. Lead received the new BLOCKER through `takkub send` during review.

After fixes, require the now-positive 15-case harness and supplementary failure cases to pass, cross-platform candidate CI, complete restored-inventory/old-wheel verification and authenticated provider coverage. The explicit one-week dev/prod soak beginning 2026-09-07 has not elapsed on this review date (earliest 2026-09-14); Lead must provide the required evidence. Lead/operator must also resolve the dev nested-layout and remaining live compatibility-file exceptions against the literal #504 acceptance. A green unit suite does not waive these gates.

The Round 1 embedded release drafts are historical. The finalized **unreleased** candidate text is now in [CHANGELOG.md](../../CHANGELOG.md) and the [2.1.0 migration guide](../v2/2.1.0-migration-guide.md). Both make the release hold explicit, require restore-v1 **before** installing an older version, route 1.x through 2.0.x, document real `--list`/`--archive` behavior and relocation/preimage limitations, and avoid promising that green validation or a successful restore exit proves completeness. No version bump or release publication was performed.

## Round 3 — 28a4e527

Date: 2026-09-10. **Verdict: FAIL — not releasable. Owner: backend#2.** This section supersedes the Round 2 assessment for this candidate while retaining its historical evidence. Current worktree HEAD is `28a4e527`. This is a documentation-only consolidation of existing evidence; no harness, tests, or additional investigation were run and no implementation was edited.

### Round 3 evidence

- Source of truth: `runtime/exports/2026-09-10/agent-takkub/504-round3-faults.jsonl` and case definitions in `504-round3-faults.py`, under the central project root `C:/Users/monch/WebstormProjects/agent-takkub/`. The JSONL records fixture root `504-review-00lezi5a` and source worktree `reviewer-1789035895`.
- Existing Round 3 fault results: **28 cases, 11 PASS / 17 FAIL**. The table below uses the JSONL verdict records, with observations from their associated records and the case code. Passing fault cases mean the expected rejection/recovery assertions passed, not that the injected operation succeeded.
- Previous reviewer's summary supplied by Lead: baseline `504-round2-repro.py` **16/16 PASS**, `504-round2-extra.py` **13/13 PASS** on this round. These are reported baseline results, not reruns in this documentation task. They close the corresponding narrow Round 2 repros, not the additional failures below.
- All `promote_v1.py:line` references below refer to `src/agent_takkub/core/migration/promote_v1.py` at current HEAD; other source references use the same `src/agent_takkub/` prefix. No new CI, authenticated-provider, physical cross-volume, old-wheel, or soak evidence is claimed.

### Fault harness — every recorded case

| Case | Round 3 verdict | Observed result |
| --- | --- | --- |
| `last_remove_promote` | PASS | Final removal fails; operation reports false and all three original source values survive. |
| `last_remove_archive` | PASS | Final removal fails; all three original archive sources survive. |
| `last_remove_rollback` | PASS | Final removal fails; all three rollback sources survive. |
| `manifest_write_promote` | FAIL | Manifest write fails but source is removed; apply and rollback report success, original nested file remains absent. |
| `manifest_write_promote_crash` | FAIL | Failed manifest write followed by interruption after removal; rollback reports success without reconstructing source. |
| `manifest_write_archive_crash` | FAIL | Failed archive manifest write followed by interruption after removal; restore reports false and original source is absent. |
| `source_restore_fails_again` | FAIL | Reconstruction error is hidden; manifest drops models ownership although only the flat copy survives. Retry and rollback succeed but original source remains absent. |
| `merged_live_home_after_retry` | FAIL / BLOCKER | Recovery copies live Kimi auth into nested source; retry claims ownership; successful rollback removes auth from the actual live home. |
| `partial_final_directory_remove` | FAIL | Failed removal already deleted `state/b.json`; source is absent, target still contains `b`, despite report claiming every source was restored. |
| `middle_generation_disjoint` | PASS | Second-generation copy fails; undo restores command-entry target value `CURRENT`. |
| `middle_generation_same_name` | FAIL | Second-generation failure leaves `generation-0` instead of `CURRENT`. |
| `third_generation_same_name` | FAIL | Third-generation failure leaves `generation-1` instead of `CURRENT`. |
| `middle_generation_nested_name` | FAIL | Undo leaves `projects/demo/role-providers.json` absent instead of restoring `CURRENT`. |
| `two_crashes_promote` | PASS | Two simulated interruptions, boot/reboot and restore recover all three original nested files. |
| `two_crashes_archive` | PASS | Two simulated interruptions, boot/reboot and restore recover all three original root files. |
| `windows_long_spaced_data_home` | PASS | 332-character home with spaces boots and restores nested value `LONG`. |
| `storage_root_empty` | PASS | Child reads `PRIMARY`, writes `CHILD` into primary nested root, creates no wrong root. |
| `storage_root_wrong` | FAIL | Nonempty override selects wrong existing root; read is null, primary stays `PRIMARY`, child writes elsewhere. |
| `storage_root_nonexistent` | FAIL | Nonempty override creates another root; read is null and primary stays `PRIMARY`. |
| `pane_overwrites_inherited_root` | PASS | Pane environment stamping replaces incorrect inherited value with the intended `data/v2` root. |
| `archive_missing_manifest_validation` | FAIL | Validate is green although generation listing marks it unreadable; restore correctly rejects it. |
| `promoted_member_inventory` | FAIL | Deleting arbitrary promoted `models/unique-extra.json` leaves every validation report green. |
| `domain_missing` | PASS | Missing required project registry makes validation fail. |
| `domain_required_key` | PASS | Registry `{}` makes validation fail. |
| `domain_null_data` | FAIL | Registry `{"data":null}` still validates green. |
| `disk_cli_apply` | FAIL | CLI apply succeeds and removes legacy source with zero recorded free-space checks under injected zero free space. |
| `disk_cli_restore-v1` | FAIL | CLI restore succeeds and restores legacy source with zero recorded free-space checks under injected zero free space. |
| `merge_unreadable_prior_manifest` | FAIL | Apply replaces corrupt prior ownership inventory; rollback succeeds but first nested source stays absent, bytes remain at flat target. |

### R3-B1 — BLOCKER: recovery contaminates promoted ownership and removes live Kimi auth

**Owner: backend#2.** `_restore_removed_source()` copies the entire merged destination directory back to the source (`promote_v1.py:236`, `:243`). That destination includes pre-existing `providers/kimi/default/auth.json`, which was never promoted. After a later source removal fails, this helper reconstructs `v2/providers` with Kimi included. Retry enumerates every nested source file into promoted ownership (`:634`, `:637`, `:648`). Rollback then builds source-removal pairs for those recorded relative files (`:735`, `:751`, `:754`; `_two_phase_move` removes at `:317`).

The recorded outcome is `copied_live_home_to_source=true`, `rollback_ok=true`, `live_home=null`, and `nested_home="live-secret"`. This is removal from the actual provider home, not total erasure of every copy. It still violates the never-touch-provider-home acceptance and can break provider access. Recover only the original source-owned members; destination-only siblings must never become retry ownership. Require this exact failure → retry → rollback regression before closure.

### R3-B2 — BLOCKER: failed durable record does not stop destructive removal

**Owner: backend#2.** `_two_phase_move()` invokes `on_before_remove` at `promote_v1.py:313`, swallows every callback exception at `:314`, then removes source at `:317`. Promotion supplies the manifest callback at `:648`; archive supplies it at `:1022`. A failed write therefore defeats the intended durable-before-delete barrier.

All three manifest-write fault cases fail. The ordinary promote case even reports successful apply and rollback while the original nested file is absent. The interruption cases remove source without a usable record and supported restore cannot reconstruct its original location. These observations establish lost recovery inventory; they do not establish that the copied target bytes were also erased. Required: successful durable recording must be a prerequisite to source deletion, with failure returned before destructive work and inventory retained for retry/recovery.

### R3-B3 — BLOCKER: multi-generation undo does not restore command-entry state

**Owner: backend#2.** CLI tracks only restored names and undoes prior generations in reverse order (`cli.py:3174`, `:3179`, `:3182`). `_undo_restored_names()` resolves each name through the latest backup (`promote_v1.py:1304`) and removes the current destination before copying that backup (`:1306`, `:1309`, `:1310`). It does not retain the exact preimage for each operation.

For overlapping names, the latest backup belongs to a later generation, so failed restore leaves `generation-0` or `generation-1`, not command-entry `CURRENT`. For a nested name, `BackupManager.backup()` stores only `source.name` (`core/migration/backup.py:40`), but `latest_backup()` looks up `slot / name` (`:68`); the nested relative path does not find that basename backup. Undo then deletes the target and restores nothing. The disjoint-name case passes, so that baseline cannot certify multi-generation atomicity. Required: carry exact per-operation backup references and restore the command-entry state, including nested paths, on any later-generation failure.

### R3-H1 — HIGH: source recovery failures and partial removals are misreported

**Owner: backend#2.** `_restore_removed_source()` suppresses `OSError` (`promote_v1.py:247`). Its caller still invokes the ownership-retraction callback (`:324`, `:327`; promotion binding at `:651`), discarding the record even if reconstruction failed. The recorded retry/rollback then cannot put the first file back. Separately, `removed.append()` occurs only after successful removal (`:318`); recovery loops only over that list (`:323`), omitting the source whose removal partly completed before raising. The failure report nevertheless claims every source has been restored (`:674`).

Do not retract ownership without verified reconstruction. Recover partially removed sources too, retain unresolved inventory, and report failed recovery accurately. Also reject an unreadable existing promote manifest: `_merge_manifest_entry()` currently converts read/parse failure into an empty inventory (`:595`, `:598`, `:599`) and writes the replacement (`:602`). The recorded corrupt-prior-manifest case loses the earlier restoration path while reporting apply/rollback success.

### R3-H2 — HIGH: #568 integrity remains open

**Owner: backend#2.** Missing archive manifests remain invisible to validate because `_find_all_manifests()` filters out generations without a manifest file (`promote_v1.py:1381`); archive validation consumes that filtered list (`:1181`). Listing/restore now detect the unreadable generation in the recorded case, but validation stays green. Promotion validation checks only whether nested work remains (`:696`, `:697`, `:698`), not completeness of the promoted member inventory. Deleting an arbitrary promoted member therefore still validates green. Required-domain checks reject a missing registry and `{}`, but accept `{"data":null}` in this run.

The prior baseline and the two repeated-interruption cases demonstrate improvements; they do not close #568 or waive R3-B2/R3-H1. Require complete generation presence, promoted-member existence, and required-domain shape validation without treating ordinary legitimate live edits as immutable archive hashes.

### R3-H3 — HIGH: remaining root-override and CLI disk gaps

**Owner: backend#2 for release coordination/disk; coordinate backend#3 for storage-root policy.** Baseline dev-root behavior, empty override, and pane stamping pass. The two nonempty override cases still redirect real provider-model reads/writes away from the cockpit. The harness asserts that even these overrides must resolve to the primary; whether an explicit override is intended to be authoritative requires a documented policy decision. These FAIL records must not be silently converted into a claim that the normal pane-stamping path failed.

CLI apply and restore-v1 both succeed under the zero-free-space injection without invoking the patched disk-usage check. The recorded failure is absence of CLI preflight, not a physical disk-full experiment. Boot-gate baseline success does not certify these manual entry points.

### Acceptance matrix — Round 3 re-score

| Requirement | Round 3 verdict | Evidence / remaining condition |
| --- | --- | --- |
| Automatic installed promotion; fresh two-boot layout | PASS in reported baseline | Baseline 16/16 and 13/13; no fresh execution here. |
| Complete archive/inventory and trustworthy validation | FAIL / HIGH | Missing manifest, arbitrary promoted member, and null domain data validate green; #568 remains open. |
| Complete restore and downgrade safety | FAIL / BLOCKER | Multi-generation undo loses command-entry state; missing recovery records prevent supported reconstruction. Old-wheel evidence not added. |
| Provider homes untouched; all six providers work | FAIL / BLOCKER | Live Kimi auth removed after recovery/retry/rollback; authenticated coverage not added. |
| Whole prior state restored on midway failure | FAIL / BLOCKER | Final-removal and repeated-interruption cases pass, but merged ownership, failed manifest writes, partial removal and second recovery failures remain. |
| Disk checked before mutation | FAIL | Both manual CLI entry points perform no observed check. Reported boot baseline does not close this. |
| Dev/worktree readers agree | PARTIAL | Reported baseline, empty override and pane stamping pass; wrong/nonexistent override cases fail and require explicit policy. |
| Cross-volume-safe implementation | Prior implementation PASS retained | No new physical multi-volume evidence; long/spaced Windows path case passes. |
| Retired imports, authority warning, doctor messaging | Prior scoped results retained | No new evidence here; Round 2 literal mixed-layout exception remains a scope decision. |
| Release/migration documentation | PASS as unreleased documentation | Existing release hold retained; this candidate must not be described as safe. |
| Candidate CI, provider/old-wheel acceptance and one-week soak | NOT ESTABLISHED by these artifacts | No additional evidence supplied; the stated 2026-09-07 soak cannot complete before 2026-09-14. |

### Release disposition

**FAIL — not releasable. Owner: backend#2.** Do not release 2.1.0 or close #504 at `28a4e527`. Baseline improvements are acknowledged, but live-provider removal, missing durable recovery records, and incorrect multi-generation undo remain release blockers. Fix and regress the recorded failures, resolve storage-root override semantics with backend#3, and supply the remaining acceptance evidence before rescoring. This task only appends the existing Round 3 findings; it does not rerun verification or change implementation.

## Round 4 — eda919ea

Date: 2026-09-10. Reviewer: reviewer, mode code (rerouted from codex to claude mid-round, #514). Reviewed **eda919eac4c4f8cdc8b641b060a4c0fa3f87a0f0** — the merge of backend#2's transaction-core rewrite (`26e0820a`, `TransferEntry` + `_copy_phase`/`_prune_phase`, follow-up `a2ad05a9`) and backend#3's storage-root validation (`10136473`, plus the merged `v2_target.py` change). Source was not edited.

**Verdict: FAIL — not releasable. Do not bump 2.1.0 and do not close #504 or #568 at this commit. Owner: backend#2** (R4-M2 storage-root semantics: coordinate backend#3).

The rewrite is a real structural improvement and it closes the three Round 3 BLOCKERs on their own repros: `merged_live_home_after_retry`, `manifest_write_*` and `middle_generation_*` all pass now, and every Round 1–3 harness is green. But the new core still loses the only copy of user data on two independent paths, and an ordinary process death during archival permanently disables `restore-v1` — the one recovery verb #504 promises. Gemini's B1 and B2 are not implemented as specified: there is no write-ahead ledger before the first copy, no `fsync` anywhere in the ledger writer, no per-entry `PENDING`/`VERIFIED` state, and the prune pass runs per step rather than after whole-ladder validation.

### Round 4 evidence

- Baselines rerun by this reviewer against this worktree's `src` (not reported second-hand): `504-round2-repro.py` **16/16 PASS**, `504-round2-extra.py` **13/13 PASS**, `504-round3-faults.py` **28/28 PASS**, all with `failures=[]`. Output: `504-round2-repro-r4rev.jsonl`, `504-round2-extra-r4rev.jsonl`, `504-round3-faults-r4rev.jsonl`. This confirms Lead's harness run on the same commit.
- Round 4 fault harness: `504-round4-faults.py` (56 cases, written by the codex reviewer pane before the reroute) with its recorded run `504-round4-faults.jsonl`. Rerun independently here into `504-round4-faults-claude-rerun.jsonl`: **24 PASS / 32 FAIL**, a byte-identical failure list to the original run. The harness was not modified.
- Repository test suite for the touched modules: **156 passed** (`test_core_migration_promote_v1.py`, `test_cli_migrate.py`, `test_auto_migrate_boot.py`, `test_core_migration.py`, `test_core_storage_v2_target.py`) with `PYTHONPATH=<worktree>/src`.
- All artifacts live under `C:/Users/monch/WebstormProjects/agent-takkub/runtime/exports/2026-09-10/agent-takkub/`. Fixture roots for the reruns: `r4/504-review-b7abea6g`, `r4/504-round4-8mkqv_z6`. Harness crash cases use real `os._exit(91)` worker subprocesses; this is process-death testing, **not** a physical power-cut or cross-volume test. No real user data or provider credentials were touched.
- `promote_v1.py:line` references are `src/agent_takkub/core/migration/promote_v1.py` at this commit. No CI, six-provider, old-wheel or soak evidence is claimed by this round.

### Trace against the Gemini B1–B4 design spec

| Spec item | Status | What the code actually does |
| --- | --- | --- |
| B1 — copy-only pass never touches source | **PARTIAL** | `_copy_phase` (`:358`) truly never mutates `src`, and `disk_full_middle_copy` confirms a mid-copy ENOSPC leaves both originals byte-intact. But a file created in the source **after** its inventory was computed is destroyed by the whole-directory prune (R4-B2). |
| B1 — prune only after all domains and generations validate | **NO** | `_prune_phase` runs inside each step's own `apply()` (`:822`, `:1174`). `barrier_domain_failure`, `barrier_domain_validation` and `barrier_old_generation` all show the nested `v2/` source already pruned while a later domain step reports `validate` false, and archival proceeding with a manifest-less generation on disk. |
| B1 — an un-prunable entry keeps its duplicate and marks cleanup pending | **NO** | On any prune failure the batch reconstructs `src` from `dest` (`:437`–`:441`) instead of retaining the duplicate. Behaviourally safe here (copies stay at least 1) but it is the reconstruct-shaped recovery the spec asked to remove. `prune_duplicate_only_contract` records the writes back into `v2/`. |
| B1 — no undo that deletes the target | **YES** | Undo only ever touches `dest` when a preimage exists (`_undo_copied_dest`, `:236`), and `ArchiveV1LegacyStep.rollback` is copy-only (`:1374`). |
| B2 — ledger written before the first file copy | **NO** | Observed event order for a two-entry promote is `copy:models, copy:state, ledger, remove:models, ledger, remove:state` (`wal_observed`). Every copy precedes the first ledger write; nothing on disk says a transaction is in flight. |
| B2 — flush/fsync before each state transition | **NO** | `write_json_atomic` (`registry_copy_step.py:41`) writes a temp file and `os.replace`s it with no `flush`/`fsync` on the file or its directory. The harness arms crash points inside `os.fsync` and the workers exit **0** instead of 91 — the hook is never reached because the writer never calls it (`crash_promote_fsync_before/after`, `crash_archive_fsync_before/after`). |
| B2 — per-entry PENDING/VERIFIED state plus checksums | **NO** | Promote ledger entries carry only `name`, `kind`, `paths` and `path` (`to_ledger`, `:284`); no state field, and `sha256` is passed only by the archive step. |
| B3 — command-level immutable snapshot for `restore-v1` | **PRESENT BUT UNSAFE** | `_begin_command_snapshot` and `_revert_to_command_snapshot` exist and fix the Round 3 shadowing (`middle_generation_entry_state` and the nested variant now pass), but both swallow every `OSError` — see R4-B1. |
| B4 — no swallowed errors in migration/restore/ledger paths | **NO** | Seven bare `except: pass` handlers remain at `promote_v1.py` lines **88, 232, 644, 663, 673, 1154, 1248**. Lines 644, 663 and 673 are the snapshot and revert paths and are the direct cause of R4-B1. Line 88 (`_log_event`'s own guard) is benign. |
| Never-touch enforced on every pass | **NO** | Three separate paths still write to or remove a live provider home — R4-H4 and R4-H5. |

### R4-B1 — BLOCKER: `restore-v1` destroys the live file when its own snapshot could not be taken

**Owner: backend#2.** `_begin_command_snapshot` copies each name into a snapshot directory and discards any `OSError` with a bare `pass` (`promote_v1.py:644`). Nothing checks afterwards whether a snapshot actually exists. When the command later aborts, `_revert_to_command_snapshot` deletes the current on-disk entry first (`:659`–`:664`) and only then tries to copy the saved preimage back, discarding that failure too (`:673`). A name whose snapshot silently failed is therefore deleted and never restored.

`snapshot_capture_then_integrity` reproduces the worst shape: three archive generations exist, the user then writes a unique value into `same.json`, the latest generation is corrupt, and the snapshot copy raises `ENOSPC`. The archive integrity check correctly refuses the restore — and the revert then erases the live file. Recorded result: `target: null`, `entry_copies: []`. **Zero copies of the user's value survive anywhere on disk**, on a command that reported only "refusing to restore". `snapshot_capture_middle` and `snapshot_revert_middle` end the same way, with the value surviving only inside internal `migration_backups` and `restore-v1-snapshots` scratch directories the user is never told about.

This is reachable without an actually full disk, because the preflight meant to prevent it under-measures by three orders of magnitude: `restore_size_observed` records `estimate=374` bytes against a live preimage of `262144` bytes, and the gate passes (`allowed: true`). Required: a failed snapshot capture must abort the command before anything is touched; the revert must verify the preimage exists before deleting the current entry; and both must report failure instead of `pass`.

### R4-B2 — BLOCKER: a source file written after copy-verification is deleted with no copy anywhere

**Owner: backend#2.** `TransferEntry.paths` is computed once before any copy (`:802`, `:1138`) and is the only inventory recovery ever reads — exactly the right fix for R3-B1. But the prune still removes the whole source **directory** (`_remove(entry.src)` at `:428`, `shutil.rmtree` at `:194`). Any file that appears in that directory between verification and prune is deleted although it was never copied and is in no inventory.

`late_source_conservation` writes `late.json` into the source right after `copy_verified` returns and records `copies: []` — zero surviving copies, while `apply()` reports `ok: true`. The trigger is not exotic: takkub runs many panes against one `DATA_HOME`, and boot-time migration is not exclusive of other writers. Required: prune per recorded relative path, never the directory as a unit, and refuse to remove a source directory that still holds an unrecorded file — the way `_rmdir_tree` (`:199`) already refuses for the legacy `v2/` root.

### R4-B3 — BLOCKER: one manifest-less archive generation disables `restore-v1` permanently

**Owner: backend#2.** A crash during the archive copy phase leaves `backups/v1-archive-<ts>/` on disk with content and no `manifest.json`, and nothing ever cleans it up or resumes it — a retry allocates a fresh timestamp (`:1128`). `list_v1_archives` then returns that generation with `unreadable: True` but a real `ts` (`:510`–`:521`), and `_cmd_migrate_restore_v1` walks every returned `ts` unconditionally (`cli.py:3168`). `_find_manifest_by_ts` returns `None` for it, `ArchiveV1LegacyStep.rollback` fails with "no v1-archive-<ts> generation found" (`:1340`), the command reverts to its snapshot and returns. Every future `restore-v1` takes the same path.

`crash_archive_tmp_before`, `crash_archive_tmp_after` and `crash_archive_replace_before` all record `restored: [null, null, null]` and `restore_ok: false` after three boot replays plus a supported restore, with the first replay reporting `rolled_back`. `barrier_old_generation` shows the same poisoning from a pre-existing empty generation directory. The bytes do survive under `backups/`, so this is loss of the recovery path rather than erasure — but "เอากลับได้เสมอ" is a core #504 acceptance requirement and an ordinary crash kills it. Required: skip or quarantine unreadable generations in the walk so they cannot fail the whole command, and detect-and-resume or discard an incomplete generation at boot. The `ts=""` entry returned on a listing `OSError` (`:497`) hits this identically.

### R4-H1 — HIGH: B2 durability is claimed but not implemented

**Owner: backend#2.** Covered in the trace table: no pre-copy ledger, no `fsync`, no per-entry state. `_prune_phase`'s docstring promises that "a durable ledger write naming it has itself already succeeded" (`:398`) and the commit subject says "WAL-safe", but `write_json_atomic` returns as soon as the page cache accepts the write. On a real power cut between the ledger write and `os.replace` reaching disk, the removal that follows is unrecorded — the exact R3-B2 shape the barrier was added to prevent. `wal_before_copy`, `wal_durable`, `wal_states` and the four `crash_*_fsync_*` cases all fail. Fix in `write_json_atomic` itself (flush plus `os.fsync` on the temp file, and fsync the parent directory on POSIX) so every ledger user inherits it, then re-run the fsync crash points and require exit 91.

### R4-H2 — HIGH: a lost ledger becomes a green validate and a "successful" rollback

**Owner: backend#2.** With the promote source already removed, deleting or emptying `backups/promote-v2-root-manifest.json` yields `validate_ok: true` and `rollback_ok: true` while `source` is `null` (`ledger_promote_deleted`, `ledger_promote_empty_object`). `_promoted_member_problems` returns `[]` when the manifest is absent (`:758`–`:759`), and `rollback` reports "no promote manifest — nothing to undo" (`:869`–`:873`). `ledger_archive_empty_object` is green the same way, and `ledger_archive_deleted` returns `rollback_ok: true` ("nothing to restore") for a `DATA_HOME` whose V1 files are gone. Only the `corrupt` variants fail closed. A missing ownership record after a migration has run must be a hard validation failure, not silence.

### R4-H3 — HIGH: failed source reconstruction still retracts the ledger

**Owner: backend#2.** This is R3-H1 carried into the new core. When a prune fails and `restore_source_from_dest` also fails, `_prune_phase` still calls `write_committed([])` (`:443`), dropping the entry from the ledger. `permission_prune_recovery_fails` records `source_a: null`, `target_a: "A"` and `ledger: {"promoted": []}` — the source is gone, the only copy sits at the target, and nothing on disk records that the target owns it. A later `rollback` cannot put it back, and `validate` is green. Retain ownership for every entry whose source was not verifiably reconstructed.

### R4-H4 — HIGH: a registered provider home nested in a shared V2 directory is archived

**Owner: backend#2.** `_named_account_home_names()` protects registered account homes among top-level candidates, but `_shared_dir_legacy_candidates()`'s `projects/*/role-providers.json` glob (`:180`–`:183`) never consults it. `never_touch_shared_registered` registers `projects/team` as a `config_dir` in `user-profiles.json`, and after `apply()` the live `role-providers.json` reads `null` — moved into the archive. Apply the registered-home exclusion to the H7 globs as well as to the top-level candidate list.

### R4-H5 — HIGH: promotion and restore overwrite live credentials with stale copies

**Owner: backend#2.** `copy_verified` merges with `shutil.copytree(..., dirs_exist_ok=True)` (`verify_copy.py:72`), which overwrites same-named files. `never_touch_promote_collision` puts `LIVE` in `providers/kimi/default/auth.json` and a stale `OLD` in `v2/providers/kimi/default/auth.json`; after promotion the live credential reads `OLD`. `never_touch_restore_registered` is the mirror case: `restore-v1` writes an older archived copy over a live registered home. A preimage does land in `migration_backups`, so this is recoverable by hand, but the user is never told and a broken provider login is the first symptom. Require an explicit collision policy — refuse, or keep the newer file and report — rather than silent last-writer-wins.

### R4-H6 — HIGH: a crash mid-directory-removal drops the first file from ownership

**Owner: backend#2.** `crash_promote_partial_directory` kills the worker after `rmtree` has deleted one file inside a promoted directory. Three boot replays plus `restore-v1` return `restored: [null, "CRASH-1", "CRASH-2"]` with `restore_ok: true` — the command reports success while the first file is missing from its original location. One copy survives at the target throughout (the harness's per-replay copy assertion holds), so this is inventory loss, not erasure. Same root cause as R4-B3: nothing durable says the transaction was interrupted, so replay reconstructs intent from live disk state.

### R4-H7 — HIGH: `restore-v1` is not all-or-nothing when the final promote rollback fails

**Owner: backend#2.** `_cmd_migrate_restore_v1` reverts to the command snapshot only inside the generation loop (`cli.py:3202`–`3205`). The final `engine.rollback_step("promote-v2-root")` result is appended with no revert, so a failure there leaves every archive generation applied on top of the user's entry state. `final_promote_abort_entry_state` records the reports ending in `false` with the target no longer at its entry value.

### R4-M1 — MED: R2-H3 remains open — preflight is single-volume and under-measures restore

**Owner: backend#2.** `boot._disk_has_room` queries only `DATA_HOME`'s volume: `volume_gate_observed` shows a single queried path and `allowed: true` while the simulated volume holding `migration_home()` (snapshots and step backups) reports zero free. `_estimate_restore_bytes` omits the command snapshot and the overwritten live preimages entirely (`estimate: 374` against a `262144`-byte live file). The Round 3 CLI-preflight gap itself is closed — `disk_cli_apply` and `disk_cli_restore-v1` now pass — but the gate it added measures the wrong thing on the wrong volume, and it is the first line of defence against R4-B1.

### R4-M2 — MED: `TAKKUB_STORAGE_ROOT` pointing at the container is accepted as the root

**Owner: backend#3 for the policy call, backend#2 to land it.** `_has_v2_layout_markers` accepts a directory when the markers are found either at that path or one level down at `<path>/v2` (`v2_target.py:49`), but `effective_data_home` then returns the path as given. `storage_root_bare_nested` records `selected: .../primary` where the real root is `.../primary/v2` — reads and writes split across two roots. The three Round 3 cases (`nonexistent`, `wrong`, `file`) are properly rejected with a logged `storage_root_ambiguous` reason and now pass; only the "one level up" case resolves wrongly. Either resolve to the nested root when the markers are found there, or reject the container.

### R4-L1 — LOW: failure summaries claim more than the code verified

`PromoteV2RootStep.apply` and `ArchiveV1LegacyStep.apply` return "every source has been restored, nothing lost" (`:830`, `:1182`) even when `_prune_phase` appended "restore incomplete" to the same error string. `prune_denied_observed` shows the wording in place. The detail arrives later in the message, but the operator reads the claim first.

### The three claimed invariants

Backend#2's invariant claim is only partly borne out. There are real, well-aimed regression tests for each Round 3 finding — `test_promote_recovery_never_contaminates_ownership_with_a_live_sibling`, `test_promote_recovery_restores_every_file_after_a_partial_directory_removal`, `test_restore_v1_cli_multi_generation_undo_reverts_to_command_entry_state`, `test_promote_apply_refuses_when_existing_manifest_is_corrupt` and four new error-reporting tests — and all 156 tests pass. But they are per-defect regressions, not the property or contract tests the spec asked for.

- **Invariant 1 (copies never below 1)** — no repository test asserts it. The harness's `phase_line_boundary_copies` does (68 Python line boundaries inside `_copy_phase` and `_prune_phase`, minimum 1) and passes, but its scope is precisely the two functions that were rewritten. Outside that scope the invariant is violated twice with zero surviving copies: R4-B1 on the restore path and R4-B2 on the late source file.
- **Invariant 2 (idempotent replay from an arbitrary crash)** — no repository test performs a real process death: `os._exit`, `subprocess` and `fsync` appear **zero** times across `test_core_migration_promote_v1.py`, `test_auto_migrate_boot.py` and `test_cli_migrate.py`. The single crash test (`test_promote_survives_a_crash_right_after_a_real_source_removal`) raises an in-process `RuntimeError` at one chosen point. The harness's real `os._exit` replays confirm the no-foreign-file half of the invariant holds at every crash point tested, but the original-location half fails at four of them (R4-B3, R4-H6).
- **Invariant 3 (multi-step abort returns to command entry state)** — covered for a clean I/O world only. `middle_generation_entry_state` and its nested variant pass, and so does the repository test. Inject an I/O failure into the snapshot or the revert itself and the invariant breaks with zero copies (R4-B1), as does a failure in the final promote rollback (R4-H7).

Required before Round 5: express invariants 1 to 3 as property tests in `tests/`, driven by real `os._exit` workers across every crash point, with fault injection (ENOSPC, EACCES) applied to the snapshot and revert paths too, not only to the happy path.

### Acceptance matrix — Round 4 re-score

| Requirement | Round 4 verdict | Evidence / remaining condition |
| --- | --- | --- |
| Automatic installed promotion; fresh two-boot layout | **PASS** | `504-round2-repro` 16/16 rerun here, including `fresh_two_boots`, `mixed_boot_projects` and `version_upgrade`. |
| Archive complete and validation trustworthy | **FAIL / HIGH** | R4-H2: a deleted or emptied ledger validates green. Round 3's `promoted_member_inventory`, `archive_missing_manifest_validation` and `domain_null_data` are closed. |
| Restore always possible; downgrade safe | **FAIL / BLOCKER** | R4-B1 (zero copies), R4-B3 (`restore-v1` permanently disabled after a crash), R4-H7 (not all-or-nothing). Old-wheel downgrade still not exercised. |
| Provider homes untouched; six providers work | **FAIL / HIGH** | R4-H4 and R4-H5. Round 3's `merged_live_home_after_retry` BLOCKER is closed. Authenticated six-provider coverage still absent. |
| Whole prior state restored on midway failure | **FAIL / BLOCKER** | R4-B2 (late source file), R4-H3 (ownership retracted after failed reconstruction), R4-H6. The Round 2 and 3 delete-phase and manifest-write repros all pass. |
| Disk checked before mutation | **FAIL / MED** | R4-M1: CLI preflight now exists but measures one volume and under-estimates restore by roughly 700 times. |
| Dev/worktree readers agree | **PARTIAL** | `storage_root_empty`, `wrong`, `nonexistent`, `file`, `correct` and pane stamping pass; R4-M2 container case resolves wrongly. |
| Cross-volume-safe implementation | **PASS (simulated)** | `cross_volume_copy_simulated` (EXDEV on every `os.replace` and `shutil.move`) and `windows_long_spaced_home` (332-character path with spaces) both pass. No physical second drive was used. |
| No swallowed errors in migration/restore/ledger | **FAIL / HIGH** | Seven bare `except: pass` remain; three of them cause R4-B1. |
| Release and migration documentation | PASS as unreleased documentation | Release hold retained; this candidate must not be described as safe. |
| Candidate CI on both OSes, six providers, old-wheel, one-week soak | **NOT ESTABLISHED** | No CI run exists for `eda919ea`; none of the four were exercised in this round. |

### #568 — cannot be closed

Both documented gaps are still open at this commit.

1. **H9 domain inventory.** Archive generations carry per-file SHA-256, `validate()` recomputes them, and `_promoted_member_problems` now checks recorded promoted members — genuine progress. But `engine.py` still returns unconditional success for a domain step whose `_pending()` is false, and R4-H2 shows the promote inventory itself can vanish while validation stays green. There is no persisted per-generation inventory of every promoted target that survives losing the ledger.
2. **Detect-and-resume for an interrupted transaction.** Not implemented. Nothing on disk marks a transaction as in flight (R4-H1), nothing at boot notices an incomplete archive generation, and R4-B3 shows that residue actively breaks the manual recovery verb the issue points operators at.

### Release disposition and the gates for Round 5

Not releasable. Fix R4-B1, R4-B2 and R4-B3 first, then R4-H1 through R4-H7, then re-run `504-round4-faults.py` unmodified and require 56/56. R4-M2 needs a written storage-root policy decision with backend#3 before it is coded.

The pre-bump evidence checklist below is **not yet applicable** and is recorded so Lead knows what to collect once Round 5 comes back clean. Every item must name the candidate commit itself.

- [ ] `504-round4-faults.py` 56/56 with `failures=[]`, plus Rounds 1 to 3 still green, all rerun against the candidate commit.
- [ ] Invariants 1 to 3 present as property tests in `tests/`, using real `os._exit` workers, with fault injection on the snapshot and revert paths; full suite green.
- [ ] `gh run list --commit <candidate>` shows a green run on **both** `windows-latest` and `macos-latest`, not a run for an ancestor commit.
- [ ] A real old-wheel downgrade: install the previous released wheel over a `DATA_HOME` migrated by the candidate, boot it, and show the project list and provider logins intact. The headless `git archive` smoke from Round 1 does not satisfy this.
- [ ] All six providers (claude, codex, gemini-agy, opencode, kimi, cursor) authenticated and working after a real migration, with the credential-collision cases from R4-H5 checked by hand.
- [ ] Prod soak on the candidate, one week minimum, with no use of any escape hatch. The 2026-09-07 soak start cannot complete before 2026-09-14 and it was started on a different commit.
