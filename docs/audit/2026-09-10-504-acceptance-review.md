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

---

## Round 5 — 4eb5ec95

Reviewed commit: `4eb5ec95` (merge of `wt/backend-2-1789084115` — round 6: engine 2-pass
barrier, DUPLICATE prune contract, snapshot revert fallback, archive digest regression
fix). Merged into `wt/reviewer-1789085873` and reviewed there. Mode: code.

**Verdict: NOT RELEASABLE for #568, and #566 scope 2 cannot close either.** One
BLOCKER-class data-loss window (latent today), two HIGH, one MEDIUM — all new, all
reproduced on this commit. The regression surface from rounds 2 to 4 is genuinely closed:
all four earlier harnesses are green. The findings below are new ground, reached by a new
harness written against the round-6 structure itself.

### Correction to the task premise

The commit named in the assignment, `7be87118`, does **not** contain round 6.
`git merge-base --is-ancestor 73c6b7e3 7be87118` returns false there; round 6 is a
686-line change to `promote_v1.py` plus 175 lines in `engine.py` that reached `main` only
in `4eb5ec95`. Lead confirmed this mid-review. Every result below is against `4eb5ec95`.

### 1. The four existing harnesses, rerun by the reviewer

Run from this worktree with `PYTHONPATH=<worktree>/src`, artifacts under the session
scratchpad. Evidence copied to `runtime/exports/2026-09-11/agent-takkub/`.

| Harness | Result | Evidence |
| --- | --- | --- |
| `504-round2-repro.py` | `failures: []` | `r5-round2-repro.jsonl` |
| `504-round2-extra.py` | `failures: []` | `r5-504-round2-extra.jsonl` |
| `504-round3-faults.py` | `failures: []` | `r5-504-round3-faults.jsonl` |
| `504-round4-faults.py` | 56 passed / 56 | `r5-round4-faults.jsonl` |

Repository tests: every `tests/*migrat*` file plus `tests/test_doctor.py` exit 0 (327
tests) with `PYTHONPATH=<worktree>/src`. That prefix is required here — the shared venv's
editable install points at a different checkout, and `tests/conftest.py` refuses to run
without it.

### 2. The contract change to three harness files — accepted

backend#2 rewrote the prune-failure expectation in `504-round4-faults.py`
(`permission_prune`), `504-round2-extra.py` (`delete_phase_promote/archive/rollback`) and
`504-round3-faults.py` (`last_remove_promote/archive/rollback`), from "a denied prune
restores every entry back to source" to "the denied entry becomes a DUPLICATE, entries
already pruned stay pruned, no second mutation."

**I accept it, and I could not construct a case that loses data under the new rule.** The
old rule was the more dangerous of the two:

- Every entry still holds at least one good copy at all times. A pruned entry has its
  copy-verified target; the denied entry has both source and target.
- The old rule required writing back over sources the same call had already finished
  pruning. That is a second mutation on an already-failing path, and it was paired with a
  ledger retraction that left the surviving target looking unowned — the exact shape of
  the round-4 blockers.
- `TransferEntry.restore_source_from_dest` (`promote_v1.py:326`) is now scoped to the one
  failed entry's own partially-completed removal, one file at a time, and never unlinks a
  target. Gemini B4 is met.

Acceptance was conditional on the DUPLICATE state being recoverable and visible.
Recoverable: yes — `restore_v1_over_duplicate`, `duplicate_across_boot` and
`duplicate_across_boot_live_write` all pass. Visible: no — see R5-M1.

### 3. Gemini spec B1 to B4, traced against the final structure

| Item | State | Where |
| --- | --- | --- |
| B1 — whole copy-only ladder before any prune | Met on every production path | `engine.py:437` `apply()` and `engine.py:226` `apply_pending()` copy every step, health-check the whole pass, then prune. `PromoteV2RootStep.apply()` and `ArchiveV1LegacyStep.apply()` still copy and prune in one call by design — see R5-B1. |
| B2 — WAL fsync before the first byte and on every transition | Met | `_copy_phase` (`promote_v1.py:427`) records every entry `PENDING` before the first copy, and `registry_copy_step.write_json_atomic:39` now fsyncs the temp file and best-effort fsyncs the parent directory. `wal_before_copy`, `wal_durable`, `wal_states` pass. |
| B3 — prune once, after every domain and every generation validates | Met in pass 1, gap in pass 2 | `_finish_deferred_prune` (`engine.py:401`) runs only when every pass-1 report is ok. `barrier_domain_failure`, `barrier_domain_validation`, `barrier_old_generation` pass. Pass 2 itself has no barrier — R5-H1. |
| B4 — a DUPLICATE has no undo that deletes the target | Met | `_prune_phase` calls only `failed.restore_source_from_dest()`, which copies target to source per file and never unlinks a target. |
| snapshot fallback `earliest_backup_since` when a middle generation fails | Met | `backup.py:74` returns the oldest slot at or after the command snapshot's own timestamp, and `_command_snapshot_backup_fallback` (`promote_v1.py:1113`) probes readability before returning it. `snapshot_capture_then_integrity`, `snapshot_capture_middle`, `snapshot_revert_middle` pass. |
| never-touch a live provider home, every pass | Met | `never_touch_shared_registered`, `never_touch_restore_registered`, `never_touch_promote_collision` pass. |
| T4 — no swallowed errors | Met | AST scan of `src/agent_takkub/core/migration/`: 2 `except: pass` handlers remain, both annotated `swallow-ok` with a stated reason (`registry_copy_step.py:58` directory fsync, `wal.py:134`). Round 4 had 7 unannotated. `no_swallowed_errors` passes. |

### 4. New fault injection — `504-round5-faults.py`

Twelve cases against the round-6 structure, none covered by the earlier harnesses. Source
at `runtime/exports/2026-09-11/agent-takkub/504-round5-faults.py`, results at
`r5-round5-faults.jsonl`. **8 passed, 4 failed.**

#### R5-B1 (BLOCKER class, latent) — `copy_undo_crash_direct`

A crash inside `_copy_phase`'s own copy-failure undo, after the destination has been
rolled back but before `ledger.clear()`, leaves the WAL claiming `VERIFIED` for an entry
whose target no longer exists. On resume, `_copy_phase` trusts that state and skips the
copy without ever checking the target, and `_prune_phase` then removes the source. The
file ends with **zero copies, and `apply()` reports success.**

Observed (`copy_undo_crash_observed`, `path: "direct"`): the worker exits 91; the WAL
holds `models: VERIFIED` with its checksum; three replays each return `True`; final valid
copies of `a.json` are `[]`.

**Production reachability, as Lead asked.** No production path calls
`PromoteV2RootStep.apply()` or `ArchiveV1LegacyStep.apply()` directly. Every one of the
five `.apply()` call sites outside the step classes is safe:

| Call site | What it calls |
| --- | --- |
| `cli.py:3293` | `engine.apply` — the 2-pass barrier |
| `auto_migrate_boot.py:409` | `engine.apply_pending(...)` — the 2-pass barrier |
| `auto_migrate_boot.py:527` | `engine.apply()` — the 2-pass barrier |
| `engine.py:214` | `version-marker` only, not a prune-deferred step |
| `engine.py:316` | `version-marker` re-apply, not a prune-deferred step |
| `engine.py:350` | `_copy_only_apply` — prefers `apply_copy_only()` when it exists |

`auto_migrate_boot.py:208` and `:214` construct the two steps but only to call
`_promote_candidates()` / `_archive_candidates()` for a disk-space estimate. So the
window is not live in 2.1.0 today, and `copy_undo_crash_boot` passes (boot actions
`pending_rolled_back, pending_rolled_back, pending_applied`).

It still blocks #568, because the safety property is being held by an incidental gate
rather than by the WAL that #568 item 2 is about:

- `MigrationEngine._downgrade_on_health` is what rescues the boot path, and
  `_health_problems()` (`promote_v1.py:1510`) checks `is_file()` only. It never compares
  the sha256 the WAL already stores. An existence probe is standing in for a durability
  invariant.
- `apply()`'s own docstring states that direct callers rely on it. One future caller
  re-opens a zero-copy window with no test to catch it.

Fix direction: on resume, `_copy_phase` must re-verify a `VERIFIED` entry's target against
its recorded sha256 before skipping the copy, and the copy undo must demote or clear the
WAL entry *before* it touches the destination, not after.

`copy_undo_crash_merge_direct` and `copy_undo_crash_merge_boot` pass, and it is worth
saying why: `copy_verified` refuses a colliding merge before the window can open, so the
pre-existing-destination variant never reaches the undo at all. That collision guard, not
the WAL, is what closes it.

#### R5-H1 (HIGH) — `prune_failure_stops_later_prunes`

`_finish_deferred_prune` gates pass 2 on pass-1 reports only, and its loop has no early
exit. When an earlier step's `prune()` fails and leaves a DUPLICATE, every later step in
the same pass still prunes its own V1 sources. The engine docstring claims "One late
failure anywhere in the SAME pass means NO step in it prunes," which holds for pass 1 and
not for pass 2.

Observed: a promote DUPLICATE on `v2/state`, and `archive-v1-legacy` removes
`legacy-top.json` from top level in the same call anyway. No data is lost — the archive
step copy-verified its sources into the generation first — so this is HIGH rather than a
blocker. It is still the ladder committing forward across a half-failed step, which is
what B1 exists to prevent.

#### R5-H2 (HIGH) — `promoted_target_integrity`, #568 item 1

The promote manifest now persists a per-file sha256 for every promoted target, and
`validate()` never compares it. `_promoted_member_problems` (`promote_v1.py:1338`) checks
`is_file()` and nothing else.

Observed: promote `v2/models/registry.json`, confirm the manifest records its checksum,
overwrite the promoted target with `CORRUPTED-NOT-JSON`, and `validate()` still returns
ok with "promoted — no legacy v2/ root remains".

#568 item 1 asks for "presence + hash + JSON-readability of required targets per domain".
The domain half is done — `engine.validate` (`engine.py:496`) now calls
`_domain_target_problems` instead of returning unconditional success. The promote
inventory half is not: the data needed is already on disk and simply is not read.

#### R5-M1 (MEDIUM) — `duplicate_doctor_visibility`

`_prune_failure_summary` (`promote_v1.py:530`) tells the operator that
`takkub doctor --storage-layout` surfaces a pending DUPLICATE. It does not.
`check_storage_layout_state` (`doctor.py:3384`) never reads either manifest, so the only
signal is the generic `legacy-leftover` WARN for a mixed layout, whose text blames the
boot ladder or a #566 live writer and names no file. On a dev checkout that WARN is
downgraded to OK, so a DUPLICATE there is invisible.

Observed: no finding mentions `b.json` or `DUPLICATE`. The manifest does record
`state: DUPLICATE` correctly, so what doctor needs is already on disk.

#### Cases that passed

`duplicate_across_boot` and `duplicate_across_boot_live_write` — a DUPLICATE survives
three boots with at least one valid copy, and a source rewritten between passes is
re-copied rather than deleted against the stale digest. `restore_v1_over_duplicate` —
restore-v1 over a generation whose archive left a DUPLICATE puts both originals back.
`late_write_manifest_digests` — the `ledger.clear()`-before-manifest regression stays
fixed; the final manifest carries real per-file checksums, which is what
`PruneOutcome.digests` exists to guarantee. `duplicate_target_not_archived` — the archive
pass does not erase a DUPLICATE entry's promoted target.

On the "crash during prune after N of M, then boot three times" item from the task: it is
already covered by `crash_promote_prune_1..3`, `crash_archive_prune_1..3` and
`crash_promote_partial_directory` in the round-4 harness, all green here. I did not
duplicate it.

### 5. Scoring

| Item | Round 4 | Round 5 |
| --- | --- | --- |
| BLOCKER | 3 | 1, latent (direct-step path only) |
| HIGH | 7 | 2 |
| MEDIUM | 2 | 1 |
| Existing harness pass rate | 24/56 | 56/56 |
| New harness | — | 8/12 |

**#568 — cannot close.** Item 1 is half-done (R5-H2: the promote inventory records
checksums that validate never compares). Item 2's boot-path acceptance test passes, but
the resume invariant rests on an existence check rather than on the WAL, and R5-B1 is a
zero-copy resume window the WAL itself does not close.

**#566 scope 2 — cannot close.** Scope 2 is "replace the 24 fixture usages with a shared
conftest helper that seeds the V2 project registry." Exactly 24 test files besides
`conftest.py` still reference `config.PROJECTS_JSON`, and no such helper exists in
`tests/conftest.py`. Untouched since the issue was filed.

For the record on the rest of #566: scope 1 is functionally met —
`config.save_projects_json` (`config.py:636`) writes into the V2 registry once it exists
and never to both, with the top-level file remaining only as a pre-migration fallback,
which matches Lead's `v1-only-write=0` soak evidence. Scope 3's runtime assertion is
green via `project_edit_reboot` in `504-round2-extra.py`.

### 6. Evidence Lead must supply if this is re-proposed as releasable

- [ ] R5-B1 fixed at the WAL level: resume re-verifies a `VERIFIED` target against its
      recorded checksum, and the copy undo demotes the WAL entry before touching the
      destination. `copy_undo_crash_direct` green.
- [ ] R5-H1 fixed: pass 2 stops at the first failing `prune()`.
      `prune_failure_stops_later_prunes` green.
- [ ] R5-H2 fixed: `_promoted_member_problems` recomputes and compares the manifest's
      sha256. `promoted_target_integrity` green. This is the remaining half of #568 item 1.
- [ ] R5-M1 fixed: `check_storage_layout_state` reads the promote and archive manifests and
      WARNs naming each `DUPLICATE` entry and both of its paths, on dev checkouts too.
- [ ] `504-round5-faults.py` 12/12, with the four earlier harnesses still green, all on the
      same commit.
- [ ] All four new cases present as regression tests under `tests/`, not only in the
      reviewer's harness. The repository suite still contains no `os._exit` crash test; that
      coverage lives entirely in these harnesses.
- [ ] `gh run list --commit <candidate>` green on both `windows-latest` and `macos-latest`
      for that exact commit.
- [ ] The 2.1.0 promote rehearsal on the copy of the real prod `DATA_HOME` (29 projects plus
      provider homes): the run log, `takkub migrate validate` after it, and a per-provider
      login check.
- [ ] #566 scope 2 done, or explicitly deferred out of the 2.1.0 acceptance line in writing.
- [ ] The prod soak continued on the release-candidate commit, not on an ancestor. The
      existing 10/10 `takkub migrate validate` on 2.0.8 does not cover this code.

### 7. Working notes

The round-5 harness never edits repository source. Crash workers use `os._exit(91)` in a
subprocess: this is process-death testing, not a power-cut durability test. Each fixture
gets its own parent directory so the `copies()` helper cannot pick up a sibling fixture's
files — the round-4 helper scans `h.parent`, which is shared there, and that produced one
cross-fixture hit before the isolation fix.

## Round 6 — fdd45604

Reviewed commit: `fdd45604` (`main`) = round 5's `4eb5ec95` + backend#2's round 7
(`b23f44f8`, the R5-B1/H1/H2/M1 fixes) + backend#3's `95f18ed8` (#566 scope 2 fixtures).
Reviewed on `wt/reviewer-1789091375`, which is at that commit. Mode: code.
`git merge-base --is-ancestor b23f44f8 HEAD` is true here, so the premise checks out this
time.

**Verdict: NOT RELEASABLE, and #568 still cannot close — but for the first time since
round 2 the failing findings are not regressions.** Round 7 does close all four round-5
findings, cleanly and with repository regression tests, and it introduces nothing new that
fails. The two findings below are pre-existing defects in the same family as R5-B1, and I
confirmed that by running the same reproduction against a `git archive` of `4eb5ec95`:
byte-for-byte the same outcome there. **#566 can close.**

### 1. The five existing harnesses, rerun by the reviewer on this commit

Run from this worktree with `PYTHONPATH=<worktree>/src` and a session-scratchpad
`TAKKUB_ARTIFACTS_DIR`. Every result below is mine, not a repeat of Lead's run.

| Harness | Result | Evidence |
| --- | --- | --- |
| `504-round2-repro.py` | `failures: []` | `r6-round2-repro.jsonl` |
| `504-round2-extra.py` | `failures: []` | `r6-round2-extra.jsonl` |
| `504-round3-faults.py` | `failures: []` | `r6-round3-faults.jsonl` |
| `504-round4-faults.py` | 56 passed / 56 | `r6-round4-faults.jsonl` |
| `504-round5-faults.py` | **12 passed / 12** | `r6-round5-faults.jsonl` |

Repository tests: `test_core_migration.py`, `test_core_migration_promote_v1.py`,
`test_doctor.py`, `test_doctor_auto_migrate.py`, `test_auto_migrate_boot.py`,
`test_config_project_registry_v2.py`, `test_project_identity.py` — 280 passed, 0 failed.

All evidence under `runtime/exports/2026-09-11/agent-takkub/`.

### 2. The round-7 fixes, traced one at a time

| Round-5 finding | State at `fdd45604` | How I checked it |
| --- | --- | --- |
| R5-B1 (BLOCKER, latent) | **Closed** | `_verified_target_intact` (`promote_v1.py:454`) recomputes every file the WAL record's own `sha256` names and compares it against `dest`'s current content before the `VERIFIED` resume fast path is taken; `_demote_verified_before_undo` (`promote_v1.py:474`) durably writes the record back to `PENDING` before the undo touches anything. `copy_undo_crash_direct` is green. I also re-ran the crash worker with `_demote_verified_before_undo` monkeypatched out entirely (`undo_crash_without_demote`): the resume re-verify rescues it on its own, which is what its docstring claims. Repository regression test: `test_promote_resume_recopies_a_verified_target_an_earlier_crash_deleted`. |
| R5-H1 (HIGH) | **Closed** | `_finish_deferred_prune` (`engine.py:401`) now carries a `stopped` flag and appends the untouched pass-1 report for every step at and after the first failing `prune()`. `prune_failure_stops_later_prunes` is green, and my own `h1_stop_then_next_pass_completes` goes further: after the denial is lifted, two more passes finish both prunes with no entry left duplicated and no source deleted. Repository test: `test_engine_apply_stops_pass2_prune_after_an_earlier_step_leaves_a_duplicate`. |
| R5-H2 (HIGH, #568 item 1) | **Closed for what it implements; the "hash" half is deliberately not implemented** | `_committed_target_problems` (`promote_v1.py:1346`) adds JSON-readability on top of presence, gated by a per-file flag `TransferEntry._committed_json_shape()` records at manifest-commit time. `promoted_target_integrity` is green. The commit message states a permanent sha256 compare was rejected because a later domain step in the same ladder pass legitimately overwrites a just-promoted target. **That claim is true** — I verified it rather than taking the docstring's word: `build_readonly_registries_step` maps `provider-models.json` onto `layout.models / "registry.json"`, which is the exact path `promote-v2-root` moves up one step earlier, and my `promote_validate_survives_domain_overwrite` case watches a full `MigrationEngine.apply()` do it and still validate green. See §4 for what is still owed on item 1. |
| R5-M1 (MEDIUM) | **Closed, and wider than asked** | `_pending_duplicate_findings` (`doctor.py:3505`) reads the promote manifest and every archive generation's manifest, and WARNs naming the entry and both surviving paths, independently of the dev-checkout downgrade. `duplicate_doctor_visibility` is green; my `doctor_duplicate_in_archive_generation` covers the archive half the round-5 harness never exercised. |

### 3. New fault injection — `504-round6-faults.py`

Twelve cases written against round 7's own new code
(`_verified_target_intact`, `_demote_verified_before_undo`, `_committed_json_shape`,
the `stopped` flag, `_pending_duplicate_findings`). Source at
`runtime/exports/2026-09-11/agent-takkub/504-round6-faults.py`, results at
`r6-round6-faults.jsonl`, supporting probes at `r6-probes.log`. **8 passed, 4 failed.**

#### R6-B1 (BLOCKER class, latent) — `resume_source_pruned_target_gone`

Round 7 taught `_copy_phase` that a resumed `VERIFIED` record is a claim, not proof. It
left the record one state over alone. A `SOURCE_PRUNED` record is still skipped outright
(`promote_v1.py:543`), target unchecked — and `PromoteV2RootStep.rollback()` is exactly
what invalidates such a record, because it deletes the promoted target and puts the source
back while the WAL keeps saying `PRUNED`.

The sequence is the ordinary one the engine already performs on itself:

1. `apply()` with one entry's prune denied — `models` reaches `PRUNED`, `state` becomes a
   DUPLICATE, `apply()` reports not-ok.
2. `rollback()` — this is literally what `MigrationEngine._downgrade_on_health` calls.
   It returns ok. The WAL is now `{models: PRUNED, state: PRUNE_FAILED}` while
   `models/a.json` is gone and `v2/models/a.json` is back.
3. The denial clears. `apply()` again: `_copy_phase` skips `models` on the strength of
   `PRUNED`, `_prune_phase` removes `v2/models` anyway.

Observed (`source_pruned_resume_observed`): retry returns **`ok: true`, "promoted 2
item(s)"**, `models/a.json` is `null`, `v2/models/a.json` is `null`, and a full-content
scan of the fixture tree finds **zero copies of that file anywhere**, backups included.
`validate()` afterwards does say `promoted file missing: …`, which is the only thing that
notices.

**Production reachability — the same shape as R5-B1, and I checked it the same way.** The
loss needs `PromoteV2RootStep.apply()` called directly. No production path does: `cli.py`
and `auto_migrate_boot.py` go through `MigrationEngine.apply`/`apply_pending`, and on that
path the source survives (it becomes R6-H1 below instead). `r6-probes.log` has both runs
side by side, `step_retry` versus `engine_retry`.

**Not a round-7 regression.** I extracted `4eb5ec95` with `git archive` into a temporary
tree and ran the identical probe against it: same WAL states, same zero copies, same
`ok: true`. This defect predates round 7 and was simply never reached by an earlier
harness.

#### R6-H1 (HIGH) — `boot_completes_after_a_transient_prune_denial`

The same stale `PRUNED` record wedges the production path permanently instead of losing
the file. After a single transient prune denial (a locked file, a permission blip — both
ordinary on Windows, where sibling panes hold files open):

- boot 1 rolls the promote step back, as designed.
- The WAL keeps `models: PRUNED`. Every later `MigrationEngine.apply()` skips that entry,
  so its target is never recreated, so the post-ladder check fails with
  `missing: …/models/a.json`. Forever. Five clean boots after the denial is gone leave
  `v2/models/a.json` and `v2/state/b.json` exactly where they were, nothing at top level,
  and `takkub migrate validate` red on `promote-v2-root`.
- From the third boot on, `run_boot_stage()` reports **`pending_applied`** while that same
  validate is failing — the rolled-back steps are parked until the version changes, so the
  action string describes the steps that did run, not the ladder's real state.

The operator is not blind: `doctor --storage-layout` raises
`auto-migrate-pending-rollback` WARN naming both parked steps, plus the `legacy-leftover`
WARN. But nothing self-heals, and the documented escape hatch ("จะลองใหม่เมื่อ version
เปลี่ยน") walks straight into R6-B1's territory on the next release. Also reproduced
identically at `4eb5ec95`.

Fix direction for both: make `SOURCE_PRUNED` re-verify its target the way `VERIFIED` now
does, or have `rollback()` clear the step's WAL as part of putting the sources back. The
second is smaller and closes both.

#### R6-M1 (MEDIUM) — `resume_verified_fastpath_archive`

`_verified_target_intact`'s file branch looks the digest up as `expected.get(entry.name)`,
but `verify_only` (`verify_copy.py:105`) keys a file entry's digest by `src.name`. For
`ArchiveV1LegacyStep` those differ whenever an entry is nested: entry `agents/role.md`
records its digest under `role.md`, so the lookup returns `None` and the guard returns
`False` for every nested archive entry.

Observed (`r6-probes.log`): `projects.json` → `intact= True`; `agents/role.md` →
`intact= False` with `dest exists= True` and unchanged content, and the resume re-copies
it. It fails safe — an unconditional re-copy is the conservative branch, and nothing is
lost or corrupted — but round 7's central new safety check is silently inert for a whole
class of entries, with no test that would notice if the default ever flipped. The promote
step is unaffected (its entry names are single path segments).

#### R6-M2 (MEDIUM) — `resume_verified_wrong_content`

A promoted target that a live writer changed after a crashed copy phase can never be
promoted again. `_verified_target_intact` correctly refuses to trust the stale `VERIFIED`
record, falls through to a real copy, and `copy_only`'s collision guard then refuses to
overwrite the live bytes. Three consecutive retries all return
`promote failed, rolled back: collision: …`. Nothing is lost — the V1 source stays intact
and validate stays honest — but as with R6-H1 there is no path back to a finished
migration without a human deleting the WAL.

#### R6-L1 — promoted non-`.json` payloads are presence-only forever

`_committed_json_shape` only records files whose name ends in `.json`
(`promote_v1.py:327`). `promoted_non_json_corruption` promotes a `runtime/events.jsonl`
store, overwrites it with `CORRUPTED`, and `validate()` stays green. The manifest holds
that file's sha256 the whole time. Same tradeoff as R5-H2, one file type wider.

#### R6-L2 — doctor hardcodes the promote manifest path

`_pending_duplicate_findings` (`doctor.py:3518`) builds
`data_home / "backups" / "promote-v2-root-manifest.json"` as a literal rather than calling
`promote_v1`'s own path helper, which is what `PromoteV2RootStep._manifest_path()` uses.
Two places now have to agree about that filename.

#### Cases that passed

`resume_verified_fastpath_promote` (the guard is alive, not accidentally always-false, for
both file- and dir-kind promote entries) · `undo_crash_without_demote` (the resume
re-verify rescues a real `os._exit(91)` crash in the undo with the demote helper disabled)
· `h1_stop_then_next_pass_completes` · `promote_validate_survives_domain_overwrite` ·
`archive_member_hash_still_compared` (the new `json` key in `to_ledger()` did not displace
the archive step's own sha256 comparison — a member tampered into different but valid JSON
still fails validate) · `doctor_duplicate_in_archive_generation` ·
`duplicate_visible_after_boot` (a boot-path prune denial leaves no DUPLICATE at all,
because the health downgrade rolls the step back first; doctor still WARNs).

### 4. The prod-copy rehearsal — how far it carries

Lead's 2.1.0 rehearsal on a copy of the real `~/.agent-takkub` (2.0.8, 244,659 files,
10.5 GB) is the strongest evidence produced in this whole review, and I am scoring it as
such: full sha256 inventory before and after, 228,850 files unchanged in place, 15,806
moved with identical content, three "lost" accounted for (two are #504 item-5 junk the
spec deletes, one is the append-only journal itself). Zero user data lost, on real data,
at real scale.

**What it proves:** the happy path. Every promote/archive/junk decision the ladder makes
on a real 29-project home is content-preserving, `validate` 11/11, `load_projects()` 29,
provider homes intact, authority v2-only. That closes the whole "ย้ายจริง ไม่ลบ" question
for a clean run, and it is a stronger result than any synthetic fixture in this document.

**What it does not prove, and must not be read as proving:**

- It is not an authenticated provider run. No credential was exercised against a live
  provider endpoint — file-level identity of `accounts/*/account.json` and
  `providers/*/provider.json` is necessary for a working login but not sufficient
  (token-path references, per-provider home resolution and OS keychain state all sit
  outside the sha256 comparison). A per-provider login check is still owed, and it is
  cheap: one real call per provider on the migrated copy.
- It exercises no failure. Every finding in rounds 3 to 6 lives on the failure path — a
  denied removal, a crash between two durable writes, a rollback. A green clean run says
  nothing about R6-B1 or R6-H1, both of which need exactly one prune denial to arm.
- It is a single run on a single OS. The macOS half of the CI matrix is still owed for
  this commit.

Treat it as closing "does the ladder move real data correctly" and as closing nothing at
all about "what happens when one step fails".

### 5. Scoring

| Item | Round 4 | Round 5 | Round 6 |
| --- | --- | --- | --- |
| BLOCKER | 3 | 1, latent | 1, latent — **pre-existing, not a regression** |
| HIGH | 7 | 2 | 1 |
| MEDIUM | 2 | 1 | 2 |
| LOW | — | — | 2 |
| Existing harness pass rate | 24/56 | 56/56 | **5 harnesses, all green** |
| New harness | — | 8/12 | 8/12 |
| Round-5 findings closed | — | — | **4 of 4** |

**#568 — cannot close.**

- Item 1 (per-domain target inventory): presence and JSON-readability are implemented and
  tested, for domain targets and promoted targets both. The recorded sha256 is still never
  compared for a promoted target at any moment, including the one moment it would be safe
  — immediately after the copy phase, before the later domain step that motivated the
  rejection has run. Either add that mid-transaction compare, or close item 1 with the
  deviation written into the issue rather than only into a docstring. Do not close it
  silently: the issue text says "presence + hash + JSON-readability" in so many words.
- Item 2 (detect-and-resume an interrupted transaction): **open, and R6-B1/R6-H1 are
  squarely inside it.** Item 2's own acceptance wording is "complete the remaining items
  or undo from preimages, then log — never mixed". What actually happens after a denied
  prune plus the engine's own rollback is permanently mixed, with no boot able to finish
  or undo it, and the direct-step variant destroys the file outright.

**#566 — can close.** Scope 1 was met in round 5. Scope 3 is green via `project_edit_reboot`.
Scope 2 is now met: `tests/conftest.py` has the `seed_projects` fixture writing through the
V2 registry, and only two test files still name `config.PROJECTS_JSON` — the dev-checkout
test that asserts V1 behaviour is unchanged, and `TestResolveFromV1Fallback`, both of which
are about the V1 file itself and should keep using it. 22 of 24 converted, 2 correctly
exempt.

### 6. If this is re-proposed as releasable

Required — these are defects, not evidence gaps:

- [ ] R6-B1 and R6-H1 fixed together: either `_copy_phase` re-verifies a `SOURCE_PRUNED`
      record's target the way it now re-verifies `VERIFIED`, or `PromoteV2RootStep
      .rollback()`/`ArchiveV1LegacyStep.rollback()` clear their own WAL as part of putting
      the sources back. `resume_source_pruned_target_gone` and
      `boot_completes_after_a_transient_prune_denial` green, plus repository regression
      tests for both — the second one is the production path and must not ship untested.
- [ ] R6-M1 fixed: `_verified_target_intact` looks the digest up by the same key
      `verify_only` writes, with a nested archive entry (`agents/role.md`) in the test.
- [ ] `504-round6-faults.py` 12/12 with all five earlier harnesses still green on one
      commit.

Evidence still owed, assuming the above lands:

- [ ] The old-wheel downgrade rehearsal: 2.1.0 → 2.0.8 on the migrated copy, proving
      `restore-v1` puts a real prod home back in a shape the previous release can boot.
- [ ] `restore-v1` on the prod copy, re-hashed byte-for-byte against the pre-apply
      manifest — Lead's stated next step. This is the one that matters most after R6-B1,
      because restore is the escape hatch a wedged machine has to fall back on.
- [ ] A per-provider authenticated login check on the migrated copy. The sha256 inventory
      does not stand in for it (§4).
- [ ] `gh run list --commit <candidate>` green on both `windows-latest` and
      `macos-latest` for that exact commit.
- [ ] #568 item 1's hash deviation written into the issue, or the mid-transaction compare
      implemented.
- [ ] The prod soak continued on the release-candidate commit rather than an ancestor.

Carried forward from round 5 and now satisfied — recorded so nobody re-litigates them:
the DUPLICATE prune contract (accepted, round 5 §2), all four round-5 findings closed with
repository regression tests, and #566 scope 2.

### 7. Working notes

`504-round6-faults.py` never edits repository source; each fixture gets its own parent
directory so the content scan cannot pick up a sibling's files. The `4eb5ec95` comparison
runs were done by extracting that commit with `git archive` into a scratch tree and
pointing `PYTHONPATH` at it — no branch switch, no worktree mutation. Two harness cases
(`resume_verified_wrong_content`, `duplicate_visible_after_boot`) were rewritten mid-review
once the first run showed my original premise was wrong: a boot-path prune denial does not
leave a DUPLICATE at all, because the health downgrade rolls the step back before one can
be recorded. Both now assert what the code actually guarantees.

## Round 7 — 6203e57b

Reviewed: `4ee48c24` (#504 round10/R8-P2 — batched rollback prune-phase +
`boot_flow` `verify_steps`/`current_path`), plus the `pre_migrate_backup.py`
ladder step and `takkub migrate run --providers/--remember/--no-backup/--json`
that merged ahead of it. Working tree `wt/reviewer-1789101154` at `6203e57b`.

**Verdict: 2.1.0 migration releasable: no.** One blocker, three HIGH. The R8-P2
change itself causes no regression in any existing harness, but it introduces
one new durability-claim defect, and the prod rehearsal exposes a release-
blocking performance problem in the new mandatory backup step.

### 1. Findings

| ID | Sev | Where | What |
|----|-----|-------|------|
| R7-B1 | BLOCKER | `pre_migrate_backup.py:251` | `migrate run` apply did not finish on a real 244,659-file prod copy — killed at 1806 s still inside phase 1/5, item 8/30 (12.1 %). The new first-in-ladder backup copies and sha256s the whole home, via `_copy_phase` with the default `fsync_every=1` — the exact un-batched shape R8-P1/R8-P2 fixed everywhere else. |
| R7-H1 | HIGH (regression, new in `4ee48c24`) | `promote_v1.py:1038-1060`, `:1091` | A *handled* removal failure inside a batch leaves every later sibling of that batch durably `PRUNED` in the WAL with its source fully intact and no manifest record. At `fsync_every=1` the same fault strands zero. |
| R7-H2 | HIGH (pre-existing, **not** an R8-P2 regression) | `promote_v1.py:1086` | After a prune removal failure plus rollback, the promoted copies of the un-pruned tail stay at top level with no ownership record, and nothing cleans them up. |
| R7-H3 | HIGH | `pre_migrate_backup.py:257`, `:262`, `:305` | A backup whose `manifest.json` write fails still reports `apply ok=true`, `validate ok=true` ("no pre-migrate backup manifest yet") — while `restore_from_backup_dir` cannot read it at all. Green ladder, dead escape hatch. |
| R7-M1 | MED | `boot_flow_terminal.py:206` | `--json --no-backup` prints a non-JSON Thai warning into the JSON stream, breaking the one-object-per-line contract `run_cli`'s own docstring states. |
| R7-M2 | MED | `cli.py:5603`, `boot_flow_terminal.py:156` | `--no-backup` does not skip the backup and records nothing durable, while both argparse helps advertise it as "DANGEROUS: skip the pre-migrate backup". |
| R7-M3 | MED | `boot_flow_terminal.py:167-177` | `--providers ask` — the **default** — never prompts. It runs provider updates for everything `check_provider_updates()` pre-selected. |
| R7-M4 | MED | `boot_flow_terminal.py:185-195` | `--remember` sits inside `if any(it.selected)`, so `--providers none --remember` persists nothing. It also always writes mode `"selected"`, never `"skip"`/`"update_all"`, and `run_cli` never reads `remembered_provider_choice()` back. |
| R7-M5 | MED | `promote_v1.py:1015`, `:1034` | When a batch's own `write_committed`/`ledger.write` fails, `batch[0]` is named `failed` and recorded DUPLICATE although nothing was ever attempted on it. |
| R7-L1 | LOW | `boot_flow_terminal.py:226` | `--json` silently implies `--yes`; the confirm prompt is skipped with no record. |
| R7-L2 | LOW | prod rehearsal | `version-marker` validates red ("app component missing/mismatched") on **both** rehearsal legs. Expected for a 2.0.8 marker under a different running build, but Lead should confirm it is rehearsal setup and not product. |

### 2. Harness results — all six existing suites green

Re-run by me on `6203e57b`, `PYTHONPATH=<worktree>/src`, per-suite
`TAKKUB_ARTIFACTS_DIR`, `timeout 600`. Logs:
`runtime/exports/2026-09-11/agent-takkub/r7b-evidence/`.

| Suite | Result |
|-------|--------|
| `504-round2-repro.py` | `failures: []` |
| `504-round2-extra.py` | `failures: []` |
| `504-round3-faults.py` | `failures: []` |
| `504-round4-faults.py` | 56/56, `failures: []` |
| `504-round5-faults.py` | 12/12, `failures: []` |
| `504-round6-faults.py` | 12/12, `failures: []` |

**R8-P2 introduces no regression in the existing corpus.** Targeted tests also
pass: 183 tests across `test_core_migration*.py`, `test_boot_flow.py`,
`test_boot_flow_terminal.py`, `test_cli_migrate.py` and
`test_core_migration_pre_migrate_backup.py` — 0 failures, 0 errors.

### 3. New harness — `504-round7-faults.py`

Rewritten from scratch this round. The version left on disk by the previous
session reported 88/95 failures; 70 of those were a single harness defect (it
read the WAL under a key whose payload is a list of records, then indexed that
list by string) and most of the rest asserted that `rollback()` must succeed
when called a second time after a *successful* rollback, which correctly
refuses. Those 88 were not product findings and are not carried forward.

29 cases, 18 pass. Every fault case runs **twice** — once at the batched
`fsync_every` and once at `fsync_every=1`, which is exactly the pre-`4ee48c24`
shape — so a finding is only ever reported as a regression when the batched run
is strictly worse than its own control.

Invariants asserted after every observed mutation:

- **I1** every seeded byte sequence still readable at source or destination
- **I2** no WAL record `PRUNED` whose target files are missing
- **I3** no WAL record `PRUNED` whose source is still fully intact and which the
  committed manifest no longer lists

I1 and I2 never fired, at any batch size, on either the apply or the rollback
path, under process kill or handled exception. **No data-loss path was found.**

### 4. R7-H1 — the batching regression, with its control

`_prune_phase` records a whole batch as `PRUNED` in both the manifest and the
WAL before removing any of that batch's sources (`promote_v1.py:1013-1038`).
That is the T1 "record before delete" contract and is correct. What is new is
the handling of a removal that fails *without* crashing: the loop breaks at
`:1056`, then `:1086` rewrites the manifest as baseline plus newly plus failed —
dropping the batch's untouched tail — while `:1091` writes back a
`ledger_states` that still carries that same tail as `PRUNED`.

Measured, 7 entries, injected `OSError` on a source removal:

| Case | `fsync_every=3` | `fsync_every=1` (control) |
|------|-----------------|---------------------------|
| apply, fault at removal 2 | 2 entries stranded | 0 |
| apply, fault at removal 4 | 2 entries stranded | 0 |
| rollback, fault at removal 2 | 1 entry stranded | 0 |
| rollback, fault at removal 4 | 2 entries stranded | 0 |
| rollback, 121 entries, native sizing | 1 entry stranded | n/a |

The commit message states the opposite invariant for the write-failure case it
did fix: "a failed `ledger.write` must never leave sibling entries in that batch
looking pruned". The removal-failure path has the identical shape and was not
covered — and there the false claim is already durable on disk.

Scope: `promote_v1.py:2028` sizes this with `_restore_fsync_batch(len(entries))`,
which is `ceil(n/60)`. On the 244,659-file prod copy that is a batch of 4,078,
so one handled removal failure can strand up to 4,077 entries.

Severity is HIGH, not BLOCKER, because it self-heals: the `baseline` resume loop
at `promote_v1.py:985-998` re-attempts removal on the next call, and the
post-resume stranded count was 0 in every case. The risk is a window in which
the WAL durably asserts something false about thousands of entries — the same
condition R6-B1 (still open) turns into an entry skipped by `_copy_phase`.

A process **kill** mid-batch strands the whole recorded batch too, but that is
the documented T1 window and it resolves on resume, so the harness reports it as
a blast-radius metric rather than a failure.

### 5. R7-H2 — confirmed pre-existing, not this commit's fault

After an apply whose prune fails at `d003`, the committed manifest holds
`d000..d003`; rollback moves those four back and leaves `d004`, `d005`, `d006`
sitting at top level, owned by nobody. Cross-checked against the parent commit
`899e572a` with `git archive` into a scratch tree (no branch switch), using a
standalone probe that imports cleanly on both:

```
PARENT 899e572a  orphaned_top_level: ["d004/f004.json","d005/f005.json","d006/f006.json"]
HEAD   6203e57b  orphaned_top_level: ["d004/f004.json","d005/f005.json","d006/f006.json"]
```

Identical, and identical again between `fsync_every` 3 and 1 on HEAD. Carried as
a standing HIGH against #504, not against `4ee48c24`.

### 6. Prod rehearsal cross-check (item 5)

**`resume/resume.txt` — DONE.** The prune-phase resume on `home2` completed;
`load_projects` reads 29 projects; `v2/` holds 9 entries; a `v1-archive-*`
generation exists. Post-resume validate is **red** on `version-marker`
(R7-L2). `pre-migrate-backup` validates green with the message "no pre-migrate
backup manifest yet" — direct prod corroboration of R7-H3.

**`final/final.txt` — NOT complete at the time I read it.** No `DONE` marker; it
was still inside `restore-v1`. So the restore/apply ratio and the byte-identical
exit I was asked to cross-check are **not yet available and I have not seen
them**.

What the apply leg does show is R7-B1:

```
apply exit=124 elapsed=1806s
last progress: phase 1/5 (pre-migrate-backup), done 8 / total 30, 12.1% overall
progress events emitted in 1806s: 10
```

244,659 files in the copy before apply. Exit 124 is `timeout`'s exit code, but
the script's own limit is `timeout 3600` (`final-rehearsal.sh:34`) and the run
stopped at 1806 s — I cannot attribute that gap from the artifacts, and Lead
should confirm whether something else killed it. Either way the measurement
stands on its own: 30 minutes of wall clock did not get the mandatory backup
step past 12 % of phase 1 of 5.

`pre_migrate_backup.py:251` and `:383` are the two `_copy_phase` calls left in
the package at the default `fsync_every=1`, against the largest entry set in the
whole ladder. That is a candidate contributor, not a proven root cause — the
step also sha256s and copies the entire home, which is inherently expensive.
Backend should measure the split before choosing a fix.

### 7. What still has to happen before 2.1.0 ships

- [ ] R7-B1: make a full prod-scale apply complete in a defensible time, and
      show the number. This is the release gate.
- [ ] R7-H1: demote the batch's untouched tail out of `PRUNED` before the final
      `ledger.write` at `promote_v1.py:1091`.
- [ ] R7-H3: fail `apply()` when the manifest write fails, and stop `validate()`
      reporting ok for a backup directory with no manifest.
- [ ] R7-M1/M2: guard the `--no-backup` warning behind `not args.json`, and
      either implement the skip or correct both help strings.
- [ ] The `final/final.txt` leg finishing, with the restore/apply ratio and the
      byte-identical exit actually read.
- [ ] Everything still open from round 6: R6-B1, R6-H1, the #568 item-1 hash
      deviation, the per-provider authenticated login check, the old-wheel
      downgrade rehearsal, and CI green on both `windows-latest` and
      `macos-latest`.

### 8. Working notes

`504-round7-faults.py` never edits repository source and never touches a real
home; every fixture is its own directory under `TAKKUB_ARTIFACTS_DIR`. Batch
size is forced through the real production callers by patching
`promote_v1._prune_phase`/`_copy_phase` to inject `fsync_every`, so the code
under test is the shipped code path, not a reimplementation. The parent-commit
comparison used `git archive <sha> src` into a scratch tree with `PYTHONPATH`
pointed at it — no branch switch, no worktree mutation. I did not touch
`home2`/`home3`; both were read-only.

Evidence: `runtime/exports/2026-09-11/agent-takkub/504-round7-faults.py` and
`runtime/exports/2026-09-11/agent-takkub/r7b-evidence/` (seven suite logs plus
`fixtures-path.txt`). `runtime/` is gitignored, so those live outside git.

## Round 8 — fceead0f

Reviewed: `64e1bc53` + `8d41be39` (#504 round 11/11b — backup-scope reduction,
`verify_copy` single-hash, `on_file` throttle, prune-batch WAL safety,
restore-v1 version-marker, CLI `--providers`/`--remember`/`--json`) and
`57d3db04` (#574 round 12 — `migrate run --json` progress stream, 5 bugs plus
the three round-3 interface gaps). Working tree `wt/reviewer-1789113910` at
`fceead0f`.

**Verdict: 2.1.0 migration releasable: no.** Round 11/11b/12 close ten of the
eleven round-7 findings and introduce **no regression** — every failure below
reproduces byte-for-byte on a `git archive` of `6203e57b`. What stops the
release is that the pre-existing defects now left exposed include one that makes
the documented recovery verb a silent no-op, and two that let the ladder mutate
a home with no usable pre-migrate backup.

### 1. Findings

| ID | Sev | Where | What | Regression? |
|----|-----|-------|------|-------------|
| R8-B1 | BLOCKER | `cli.py:3221-3222` | `takkub migrate restore-v1` returns **ok having done nothing** whenever no `v1-archive-*` generation exists — it returns before ever rolling back `promote-v2-root`. A store promoted but not archived (any ladder stop between the two steps, or a store that had no V1 leftovers to archive) gets "no v1-archive found — nothing to restore", exit 0, while `v2/` stays empty and every promoted entry stays at top level. The documented "put V1 back" escape hatch does nothing and reports success. | No — identical on `6203e57b` |
| R8-H1 | HIGH | `engine.py:225-226`, `pre_migrate_backup.py:3-7` vs `engine.py:332-335` | Two places claim "a failed backup aborts the whole ladder before anything else is touched, because `apply()`/`apply_pending()` already stop at the first non-ok step". `apply_pending()` explicitly does **not** stop — "No stop-the-line", its own docstring, with one exception for `promote-v2-root`. With `pre-migrate-backup` forced to fail, `apply_pending()` ran the remaining 12 steps, created 30 files and overwrote `models/registry.json` and `runtime/core/version.json` with no pre-migrate backup in existence. Reachable on every already-promoted machine: `run_boot_stage` routes layout state `v2`/`mixed` straight into `_run_apply_pending`, which is also the only path that ever picks up a ladder step added by a later release — exactly how `pre-migrate-backup` arrives on a machine upgrading into 2.1.0. | No — identical on `6203e57b` |
| R8-H2 | HIGH | `pre_migrate_backup.py:356-372` | `_already_backed_up()` matches manifest **names** only. Delete one recorded payload and `apply()` returns ok, "already backed up … (resumed)", without re-copying it; the ladder then walks on and mutates the home. `validate()` does catch it, but only after the whole copy pass has run. | No — identical on `6203e57b` |
| R8-H3 | HIGH | `promote_v1.py:2278-2287` | `archive-v1-legacy` archives any top-level name outside `_V2_TOP_LEVEL_NAMES` — **including one `promote-v2-root` created in the same pass**. End-to-end on a 1,000-file fixture, `migrate run --json` exits 0 with `ok:true, data_intact:true`, and `migrate validate` then reports `promote-v2-root: promoted file missing` — permanently, on every later `migrate validate` and `doctor`. Content survives inside `backups/v1-archive-*`, so this is not data loss, but apply and validate flatly contradict each other. | No — identical on `6203e57b` |
| R8-H4 | HIGH (carried) | `promote_v1.py:1086` | R7-H2 unchanged: after a prune removal failure plus rollback, five of seven promoted copies stay at top level with no ownership record and nothing cleans them up. The only round-7 finding still open. | No — pre-existing, re-confirmed |
| R8-M1 | MED | `boot_flow.py:593-599` | `percent_overall` goes **backwards** twice in a real run: 99.0 to 72.73 when `promote-v2-root`'s deferred prune fires `on_entry` after `archive-v1-legacy` has already advanced the stream to phase 4, and again on the next `done=None` text event, which recomputes the bar at its phase's floor. The phase number itself regresses 4 to 2. Round 12 fixed the `done > total` half of this same deferred-prune double-notify and left the percent/phase half. | No — identical on `6203e57b` |
| R8-M2 | MED | `boot_flow.py:701-731` and `:364-370` | Phase 3 is labelled `ตรวจสอบ` and every event says `กำลังตรวจสอบ` / `ตรวจสอบแล้ว`, but `MigrationEngine._notify_step` fires around `_copy_only_apply` — the step's **apply**, not its validate. On the `apply_pending` path (every promoted machine) no `validate()` ever runs: the run reported phase 3 at 11/12 "verified" while `MigrationOutcome.validated_steps` stayed `0`. | New in `57d3db04` |
| R8-M3 | MED | `verify_copy.py` `_source_files`, via `_FileProgressThrottle` | The `on_file` throttle holds its 2 s ceiling inside a pass, but the copy-to-verify turnover re-enumerates the whole entry with no progress call. Measured on 5,000 files: worst gap 2.17 s on an idle box and 2.94 s under load, `worst_is_pass_boundary: true` both times, with the enumeration itself costing 0.92 s. Linear at prod scale that is roughly 45 s of a frozen bar on a 244k-file entry. | New in round 11 |
| R8-M4 | MED | `boot_flow.py:585-591` | The `done = min(done, total_for_phase)` clamp hides a real plan/runtime mismatch instead of surfacing it. In the 1,000-file run `archive-v1-legacy` genuinely processed 4 entries (`bulk`, `legacyfile.json`, `projects.json`, `agents`) against a planned total of 3; the counter simply stuck at 3/3. | New in `57d3db04` |
| R8-L1 | LOW | `tests/test_boot_flow.py:141-149` | `TestProviderChoice` has an order dependency: the round-trip test writes `_choice_path()` and `test_no_choice_yet_returns_none` never clears it, so the file fails on a plain `pytest tests/test_boot_flow.py` and passes in isolation. Makes the gate order-sensitive. | No — present since `8b1faa8b` |

### 2. Round-7 findings, one row at a time

| Round-7 ID | Verdict | Evidence |
|------------|---------|----------|
| R7-B1 — backup copied the whole home, prod apply killed at 1806 s | **CLOSED** | Lead's rehearsal on the same 244,659-file copy: `migrate plan` now says "4 item(s) (25 file(s), 0.1 MB)", apply **exit 0 in 772 s**, validate 12/12 green (`final/final.txt`). Harness `scope_shape` and `scope_plan_estimate` PASS — only the merge collision, existing domain targets, the version marker and item-5 junk are copied; `agents/dev.md`, `legacyfile.json`, `projects.json` and `v2/system` are skipped as move-only. |
| R7-H1 — batch removal failure stranded siblings as PRUNED | **CLOSED, with control** | `prune_strand_fsync3` and `prune_strand_fsync1` both PASS with `stranded: []`. Same injected fault, same 7 entries, both batch sizes: the batched run is no longer worse than its `fsync_every=1` control. |
| R7-H2 — orphaned top-level copies after prune failure plus rollback | **OPEN** | `prune_orphans_after_rollback` FAILs with `['d2/f002.json', 'd3/f003.json', 'd4/f004.json', 'd5/f005.json', 'd6/f006.json']`, identical on `6203e57b`. Carried as R8-H4. |
| R7-H3 — manifest write failure reported green | **CLOSED** | `backup_manifest_failure` PASS (the ladder stops at one failed report and `validate()` is red), `backup_content_without_manifest` PASS, `backup_stale_item` PASS. |
| R7-M1 — `--no-backup` printed a non-JSON warning into the JSON stream | **CLOSED** | Flag removed; `cli_no_backup_removed` PASS, argparse rejects it outright. |
| R7-M2 — `--no-backup` did not actually skip | **CLOSED** | Same removal. |
| R7-M3 — `--providers ask` never prompted | **CLOSED** | `cli_ask_tty`, `cli_ask_all_none`, `cli_ask_non_tty` PASS: y/n/all/none on a tty, `none` plus a `provider_prompt_skipped` event off one. |
| R7-M4 — `--remember` persisted nothing | **CLOSED** | `cli_remember_modes` and `cli_remember_readback` PASS: every mode persists and the remembered value is read back before asking. |
| R7-M5 — `batch[0]` blamed DUPLICATE | **CLOSED** | `prune_batch_level_failure` PASS: `unknown_batch: true`, `failed_name: "d0 (+2 more in the same batch)"`, `manifest_duplicates: []`. |
| R7-L1 — `--json` silently implied `--yes` | **CLOSED** | `cli_auto_confirmed` PASS, an `auto_confirmed` event is on the stream. |
| R7-L2 — `version-marker` red on both rehearsal legs | **CLOSED** | Rehearsal setup, as suspected. `final/final.txt` now shows `version-marker: app component matches running build`. |

### 3. New cases this round

**(a) Backup scope — is everything the ladder destroys actually backed up?**
`scope_conservation` walks the whole ladder and reports `lost: []` with 47 files
after. `scope_restore_roundtrip` restores all 5 recorded items with
`mismatched: []`. Ten `scope_fault_<step>` cases inject a failure at every step
after the backup, from `version-marker` through `archive-v1-legacy`; each one
reports `lost: []` and `restore_ok: true`. Nothing move-only is copied. All 14
PASS.

**(b) Cross-version resume.** `resume_cross_version` seeds a
`backups/pre-migrate-<ts>` written under the old, wider scope and re-applies:
`recopied: []`, the two now-out-of-scope names are flagged `out_of_scope`,
nothing is copied twice and nothing on disk is dropped. PASS, together with
`resume_partial_payload`. The `_already_backed_up()` hole is R8-H2, a separate
case.

**(c) Throttle and stream schema.** `throttle_gap_5000` measures 72 calls over a
5,000-file entry; the single worst gap is always the copy-to-verify pass
boundary. It passed at 2.17 s on an idle box and failed at 2.94 s while the box
was running the rest of the suite — R8-M3. `progress_schema` PASS: 57 events,
phases 1 through 5, 22 events with file counts, 28 with `eta_s`,
`done_over_total: []`, `non_json_lines: []`. `progress_monotonic`, new this
round, FAILs — R8-M1.

**(d) `verify_copy` after the double-read was removed.** `verify_bitflip`,
`verify_truncated`, `verify_missing`, `verify_lying_copier` and
`verify_digest_identity` all PASS: every corruption shape is still caught and
the manifest digests still match their sources.

### 4. Round 12 end-to-end on a real 1,000-file store

`AGENT_TAKKUB_HOME=<fixture> … migrate run --providers none --yes --json`, over
1,010 seeded files with one 1,000-file promote entry. Exit 0 in 68.7 s.

| Round-12 claim | Result |
|----------------|--------|
| 1. every `--json` stdout line parses | **PASS** — 92 lines, 0 non-JSON. Lead's prod stream on `7b48de93` carries one bare `ok: migrate run finished`. |
| 2. `done <= total` everywhere | **PASS** — 0 violations across 84 progress events. The prod `7b48de93` stream has 36. |
| 2b. dedup drops no genuine entry | **PASS** — `promote-v2-root` emitted exactly its 3 planned entries. Dedup is keyed on step id plus name, and top-level directory entries are unique within a step, so a real duplicate cannot arise; the repeat it suppresses is the deferred-prune re-notify. |
| 3. phase 3 emits | **PASS** for existence, 16 events. **FAIL for truthfulness** — R8-M2. Prod on `7b48de93` showed phases 1, 2, 4, 5 only. |
| 4. `eta_s` after 2 s | **PASS** — 38 of 84 events carry it, first at 0.69 s into phase 1. |
| 5. `files_done`/`files_total` reach phases 2 and 4 | **PASS** — 45 events, peaking at 1000/1000. |
| R3-M4 per-phase `unit` | **PASS** — all three unit words observed. |
| R3-M5 structured log parts | **PASS** — `log_operation`, `log_detail`, `log_timestamp` on every event. |
| R3-M6 `previous_version` | **PASS** — reported as `2.0.8`. |

Left wrong by round 12: `percent_overall` monotonicity (R8-M1), the phase-3
label (R8-M2), and the clamp masking a short plan (R8-M4).

### 5. Prod rehearsal cross-check

Lead's `home3`, 244,659 files. Read-only on my side; I did not touch `home2` or
`home3`.

| Check | Result |
|-------|--------|
| apply elapsed | **772 s**, exit 0 — was exit 124 at 1806 s in round 7 |
| validate after apply | **12 steps, all green** |
| `load_projects` | active `tak-ea`, 29 projects, before and after restore |
| restore-v1 | exit 0, **448 s** |
| restore / apply ratio | **0.58**, well inside the 3.0 ceiling |
| copies >= 1 | **not measured** — `final-resume.sh` compares against `manifest-after-apply-3.json`, which the first leg never wrote, so `compare.py` exits 1 with `FileNotFoundError`. A rehearsal-script gap, not a product one |
| byte-identical | **not yet available** — the post-restore hash was still running when this report was written |
| validate after restore | reports `promote-v2-root: legacy v2/ root still present`. **Correct, not a defect**: a successful restore-v1 puts `v2/` back, so the forward-migration validate is expected to be red there |

On the `runtime/core/version.json` question: restore-v1 deliberately re-applies
`version-marker` instead of byte-reverting it (`cli.py:3315-3330`), so a single
changed file at that path after a restore is **the intended outcome and is
acceptable** — resurrecting a 2.0.8 stamp on a machine running the current build
would be worse. It should still be named in the release note so nobody reads it
as a failed round-trip.

### 6. Harness results

**The six existing suites — all green, but only with the right recipe.**

```
env -u TAKKUB_STORAGE_ROOT -u TAKKUB_PORT_FILE \
PYTHONPATH=src TAKKUB_ARTIFACTS_DIR=<an empty scratch dir per suite> \
timeout 900 python runtime/exports/2026-09-11/agent-takkub/<suite>.py
```

`504-round2-extra.py` and `504-round3-faults.py` additionally need
`504-review-repro.py` sitting beside them — they read it as text and patch it.

| Suite | Result |
|-------|--------|
| `504-round2-repro.py` | `failures: []` |
| `504-round2-extra.py` | `failures: []` |
| `504-round3-faults.py` | `failures: []` |
| `504-round4-faults.py` | 56/56 |
| `504-round5-faults.py` | 12/12 |
| `504-round6-faults.py` | 12/12 |

Dropping `env -u TAKKUB_STORAGE_ROOT` makes `504-round2-repro.py` fail
`dev_worktree` every time. A takkub pane's environment stamps
`TAKKUB_STORAGE_ROOT` with the main checkout's own storage root, that suite's
`fixture()` clears only `TAKKUB_PORT_FILE`, and `_primary_data_home()` then
resolves the pane leg to the real repository instead of the fixture. Harness
leak, not product: the round-7 and round-8 suites clear the variable themselves
and are unaffected.

**`504-round7-faults.py` — superseded, do not gate on it.** Run as committed on
`fceead0f` it reports **97 cases, 17 PASS, 80 FAIL**, close to the 90 and 86
Lead measured. That does not match the "29 cases, 18 pass" written in the
round-7 report: the file on disk enumerates far more cases than the run that
report was written from, so those numbers do not reproduce and must not be
treated as a baseline. 71 of the 80 failures are one observation, not 71.
`fault_case` ends by calling `_cmd_migrate_restore_v1` on an engine where only
`promote-v2-root` ever applied, then asserts the `v2/` tree is byte-identical
(`504-round7-faults.py:174` and `:181`). No archive generation exists, so
restore-v1 no-ops — which is R8-B1, a genuine finding, better reported once than
71 times. The unguarded `:181` variant raises `FileNotFoundError` (33 cases) and
the guarded `:174` variant raises `restore-v1 not byte-identical` (38 cases);
same defect, two spellings. Rather than re-fixture round 7 against the round-11
backup scope, round 8's suite supersedes it: `backup_*` and `scope_*` cover the
same ground against the current scope, and `cli_no_backup_removed` replaces
`cli_no_backup`.

**`504-round8-faults.py` on `fceead0f` — 45 cases, 40 PASS, 5 FAIL.**

| Fixture | Verdict | Control on `6203e57b` |
|---------|---------|------------------------|
| `backup_failure_aborts_apply_pending` | FAIL — R8-H1 | FAIL, identical |
| `backup_resumed_missing_payload_aborts` | FAIL — R8-H2 | FAIL, identical |
| `prune_orphans_after_rollback` | FAIL — R8-H4 | FAIL, identical |
| `progress_monotonic` | FAIL — R8-M1 | FAIL, identical |
| `throttle_gap_5000` | FAIL under load, PASS idle — R8-M3 | n/a, round-11 code |
| the other 40 | PASS | — |

Before and after, against the previous pane's run on `7b48de93`:
`progress_schema` and `cli_outer_json_pure` were FAIL there and are PASS here,
so round 12's items 1 and 3 land. Nothing that passed on `7b48de93` fails on
`fceead0f`. R8-B1 and R8-H3 were found by the round-12 end-to-end probe rather
than by a fixture and are not yet in the suite.

**Targeted tests.** 119 tests across `test_boot_flow.py`, `test_cli_migrate.py`,
`test_core_migration.py`, `test_boot_flow_terminal.py` and
`test_core_migration_pre_migrate_backup.py`: 118 pass, 1 fails —
`TestProviderChoice::test_no_choice_yet_returns_none`, the order dependency of
R8-L1, which passes when run alone.

### 7. What has to happen before 2.1.0 ships

- [ ] **R8-B1**: make `restore-v1` roll back `promote-v2-root` even with no
      archive generation, or refuse with a clear error instead of reporting ok.
- [ ] **R8-H1**: either give `apply_pending()` a real stop-the-line for
      `pre-migrate-backup` — the `promote-v2-root` exception already shows the
      shape — or delete the false claim from `engine.py:225-226` and
      `pre_migrate_backup.py:3-7` and state what actually protects that path.
- [ ] **R8-H2**: have `_already_backed_up()` check payload presence, not just
      manifest names.
- [ ] **R8-H3**: exclude names `promote-v2-root` promoted in this pass from
      `_archive_candidates()`, or teach `promote-v2-root.validate()` to accept an
      archived target.
- [ ] **R8-H4**, still R7-H2: clean up or record the orphaned top-level copies.
- [ ] **R8-M1**: clamp `percent_overall` and `phase` so neither can decrease.
- [ ] **R8-M2**: fire phase 3 around real validation, or relabel it.
- [ ] **R8-M3**: emit one progress call across the copy-to-verify enumeration.
- [ ] Read the byte-identical exit from `final/final.txt` once the post-restore
      hash finishes, and fix `final-resume.sh`'s missing
      `manifest-after-apply-3.json` so `copies >= 1` is actually measured.
- [ ] Still open from round 6: the #568 item-1 hash deviation, the per-provider
      authenticated login check, the old-wheel downgrade rehearsal, and CI green
      on both `windows-latest` and `macos-latest`.

### 8. Working notes

`504-round8-faults.py` was carried over from the pane the cockpit restart
interrupted, with one new `progress_monotonic` fixture added. It never edits
repository source and never touches a real home: every fixture is its own
directory under `TAKKUB_ARTIFACTS_DIR`, with `config.DATA_HOME`,
`SETTINGS_HOME`, `RUNTIME_DIR` and `migration_home()` all redirected there.
Batch sizes are forced through the real production callers by patching
`promote_v1._prune_phase` and `_copy_phase`, so the code under test is the
shipped path. The round-12 end-to-end probe drives the real CLI with
`AGENT_TAKKUB_HOME` pointed at a scratch store, the same way Lead's rehearsal
does. Every parent-commit control used `git archive 6203e57b | tar -x` into a
scratch tree with `PYTHONPATH` pointed at it — no branch switch, no worktree
mutation. `home2` and `home3` were read-only throughout.

Evidence, all under `runtime/exports/2026-09-11/agent-takkub/`, which is
gitignored: `504-round8-faults.py`, the six copied legacy suites plus
`504-review-repro.py`, `r8c-restore-probe.py`, `r8c-round12-seed.py`, and
`r8c-evidence/` holding every suite log and stderr, `r12-apply.stdout.json` and
`r12-ctl-apply.json`.

## Round 9 — d46487b7

Reviewed: `3ba889a6` (#504/#574 round 13 — the fourteen findings round 8 and the
round-4 UI review left open, plus Lead's two byte-rehearsal items), merged to
`main` as `d46487b7`. Working tree `wt/reviewer-1789119877`.

**Verdict: 2.1.0 migration releasable: no.** Round 13 is the strongest round so
far: all fourteen assigned findings close, every one of them reproducibly — my
own round-9 suite scores **19/22 on `d46487b7` and 2/22 on a `git archive` of
its parent `39d4c8aa`**, so nothing here passes vacuously. What stops the
release is one **HIGH** the new fixes finally made visible rather than caused:
`restore-v1` still leaves `models/registry.json` holding V2 content, and a
2.0.8 downgrade onto the restored store now gets far enough to say so.

### 1. Findings

| ID | Sev | Where | What | Regression? |
|----|-----|-------|------|-------------|
| R9-H1 | HIGH | `cli.py:3357-3410`, `pre_migrate_backup.restore_deleted_outright_items` | `takkub migrate restore-v1` restores the archive, the promote and #504 item-5's junk, but **not the one V1 file a domain step overwrote in place**. After a full apply → restore-v1 round trip on a 41-file fixture, `missing: 0` and exactly one non-marker file differs: `models/registry.json`, whose V1 payload `{"v1":"models-registry"}` has become `readonly-registries`' V2 envelope. Item 14 unblocked 2.0.8's own `migrate validate` past `version-marker`, and the very next step is now red: **`readonly-registries: 5 target(s) mismatched`** under the shipped 2.0.8 code. The V1 bytes are intact in `backups/pre-migrate-<ts>/models/registry.json` and `restore_from_backup_dir()` puts them back correctly — restore-v1 simply never asks for them, because item 13's new call filters the manifest down to the junk names only. | No — `models/registry.json` differs identically on `39d4c8aa` |
| R9-M1 | MED | `verify_copy.copy_verified` (the new boundary call) via `boot_flow.on_file_progress` | R8-M3's fix fires an explicit, unthrottled `on_file(0, len(files), "")` at the copy-to-verify boundary. That call reaches `boot_flow.on_file_progress` exactly like a real one, so the within-entry file counter **snaps back to zero once per directory entry**: on the 1,000-file run, `promote-v2-root` reports `1001/1001` and then `0/1001` for the same entry, with `current_path` degrading to `"models/"` and `log_detail` to `"0/1001"`. Four such resets-to-zero on `d46487b7`, zero on `39d4c8aa` (whose two resets are the pre-existing pass turnover, `401 → 200`, never to 0). | **Yes — new in `3ba889a6`** |
| R9-L1 | LOW | `cli.py:3410-3421` | The R8-B1 "nothing to restore" report the commit message advertises is **unreachable**. `promote_had_something` false means the promote manifest and the legacy root are both absent, so `engine.rollback_step("promote-v2-root")` at `cli.py:3314` returns not-ok and the function returns at `:3320` — long before the new block at the end. The operator still gets a non-zero exit and a failing `done` event, but the message is the internal `promote manifest at …\runtime\… not found`, never the sentence written for them. | New dead code in `3ba889a6` |
| R9-L2 | LOW | `engine.py:278` | `MigrationEngine._notify_step`'s bare `except Exception: return` carries no `# swallow-ok:` comment, so the repo's own linter test `test_t4_no_unexplained_or_delete_write_path_bare_except_swallow` fails. That single failure is the only red in my 253-test targeted run. Backend flagged it as out of scope; confirmed pre-existing at `engine.py:275` on `39d4c8aa`. A one-line comment. | No — identical on `39d4c8aa` |
| R9-L3 | LOW | `engine.py` `apply()` vs `apply_pending()` | The second gap backend flagged: `apply()` has no H6-style `version-marker` re-apply after `promote-v2-root` flips `core_home()` mid-pass. Real, and red when forced — on a seed carrying a legacy nested `v2/` root, `apply()` ends with `version-marker: post-ladder validate failed` and `core-internal-store: 1 entrie(s) mismatched`. **Not reachable in production**: `apply()` only runs when `layout_state()` returns `"v1"`, which requires no `v2/` directory at all, and on that shape the ladder is green end to end (verified both ways, on `d46487b7` and `39d4c8aa` alike). Every fixture I could build that trips it routes through `apply_pending()`, which already has the fix. | No — identical on `39d4c8aa` |

Not a finding, recorded so the next round does not re-open it: 15 events per run
carry a `log_line` and an empty `log_detail`. All 15 are plain entry-copy
events, which `docs/v2/574-boot-flow-interface.md` ("log structure", item 4)
explicitly allows — "Empty string for a plain entry-copy event (the path IS the
detail)".

### 2. Round-8 findings, one row at a time

Every row below is my own repro on `d46487b7` with the same fixture run against
`git archive 39d4c8aa` as a control. "FIXED" means the case passes on
`d46487b7` and fails on the control — the fix is doing work, not the fixture.

| ID | Verdict | Evidence |
|----|---------|----------|
| R8-B1 — `restore-v1` a silent no-op with no archive generation | **CLOSED** | `restore_v1_promoted_only` FIXED: on a store promoted but never archived, `promote-v2-root`'s rollback now runs and both `v2/models/promoted.json` and `v2/system/keep.json` come back; the control fails with "promoted v2/ entries were not moved back". `restore_v1_never_applied` FIXED: with nothing promoted and nothing archived the command is **not ok** (so the CLI's `all(r.ok)` exit is non-zero) and the stderr stream carries a `restore_phase` `done` event with `ok: false`; the control reported `ok: true`. Nothing on disk is touched either way (`changed: []`, `lost: []`). See R9-L1 for the message wording. |
| R8-H1 — `apply_pending()` walked on past a failed backup | **CLOSED** | Two fixtures, both FIXED. Through the real boot gate on a promoted (`layout_state: mixed`) home, `run_boot_stage` returns `pending_rolled_back`, reason `pre-migrate-backup: R9 injected: backup unwritable (ENOSPC)`, `steps: [("pre-migrate-backup", false)]` and **nothing else** — 0 created, 0 changed, 0 lost. The control overwrites `models/registry.json`. The only two files that appear are `runtime/core/migration_journal.jsonl` and `system/auto-migrate-state.json`, the boot gate's own bookkeeping, written by `run_boot_stage` itself and carrying no user data. Straight through `MigrationEngine.apply_pending()` the report list is exactly `["pre-migrate-backup"]`; the control ran all 13. |
| R8-H2 — `_already_backed_up()` checked names only | **CLOSED** | `already_backed_up_recopies` FIXED: a deleted payload is genuinely re-copied (`recopied: true`), apply and validate are both ok and the summary no longer says "resumed". `already_backed_up_corrupted`, new this round, FIXED too — a payload whose **content** drifted is also re-copied byte-for-byte, so the fix is the hash check it claims to be and not an existence probe. Both fail on the control. |
| R8-H3 — `archive-v1-legacy` archived what `promote-v2-root` just created | **CLOSED** | `archive_excludes_promoted` FIXED, with the repro shape sharpened: a nested-`v2/` child named `plugin-store`, deliberately outside `promote_v1._V2_TOP_LEVEL_NAMES`. `migrate run --providers none --yes --json` exits 0 and `migrate validate` is then **12 steps, all green**, with `plugin-store` sitting at the top level. The control's validate is red on `promote-v2-root: promoted file missing`. |
| R8-H4 (= R7-H2) — orphaned top-level copies after prune failure | **CLOSED** | `prune_orphans_after_rollback` FIXED: `orphans: []` and all seven `v2/d*/f*.json` back where they started. The control leaves `['d2/f002.json', 'd3/f003.json', 'd4/f004.json', 'd5/f005.json', 'd6/f006.json']`. Open since round 7; this is the one that finally closes it. |
| R8-M1 — `percent_overall`/`phase` walked backwards | **CLOSED** | `progress_monotonic` FIXED over a real 400-file `run_migration()`: 57 events, phases 1–5, zero percent regressions, zero phase regressions. The control regresses `99.0 → 70.0` three times, all around `promote-v2-root`'s deferred prune. |
| R8-M2 — phase 3 claimed a validation that never ran | **CLOSED** | `validated_steps_real` FIXED: on the `apply_pending` path `MigrationOutcome.validated_steps` is now **12** (control: 0), and the phase-3 wording is `กำลังย้ายข้อมูล` / `ย้ายข้อมูลแล้ว` — no longer claiming verification around a bare apply. `validate_ok_steps_truthful`, new this round, confirms the new helper is not a rubber stamp: inject a red `validate()` into `core-internal-store` and it comes back red, all 12 steps still returned, still in ladder order. |
| R8-M3 — silent gap at the copy-to-verify turnover | **CLOSED**, with R9-M1 | `throttle_gap_no_reenumerate` FIXED: over a 5,000-file entry the source is walked **once** (control: twice) and the worst progress gap is 0.33 s against the 2.0 s ceiling. The boundary check-in that achieves it is also what causes R9-M1. |
| R8-M4 — the clamp hid a short plan | **CLOSED** | `progress_done_le_total`: zero `done > total` across 55 events, and the phase-4 total grows 2 → 3 rather than sticking. The commit's own new regression test, `test_a_real_done_past_the_plans_own_total_grows_the_total_instead_of_clamping`, passes and asserts the `undercounted` note lands in `log_detail`. Worth saying plainly: no natural run of mine drove `done` past its plan, so the growth-and-note path is demonstrated by that test, not by my stream capture. |
| R8-L1 — `TestProviderChoice` order dependency | **CLOSED** | `pytest tests/test_boot_flow.py` on its own: **33 passed**. That is the exact command that failed in round 8. |

### 3. Round-4 UI review rows, and Lead's two rehearsal items

| ID | Verdict | Evidence |
|----|---------|----------|
| R4-H1 — the entry counter's unit flipped mid-phase | **CLOSED** | `progress_unit_stable` FIXED. Over a real run the unit is constant within every phase: phases 1, 2 and 4 are `รายการ`, phase 3 is `ขั้น`, and `phases_with_multiple_units` is empty. The control still flips phase 1 between `รายการ` and `ไฟล์`. |
| R4-H2 — the two bracketing `info` events carried no `log_detail` | **CLOSED** | `progress_log_detail` FIXED: **zero** `info`/`validate` events with a `log_line` and no `log_detail`. The control has exactly two, `เริ่มย้ายข้อมูล` at index 0 and `เสร็จ` at index 48 — the pair the finding named. |
| item 13 — restore the outright-deleted junk | **CLOSED** | `restore_junk_deleted_outright` FIXED. `archive-v1-legacy` deletes both `openviking/junk.txt` and `.takkub_issues.synced-2020.bak.json`; after restore-v1 both are back **byte-identical**, reported as `restored 2 deleted-outright item(s)`. The control loses both permanently. |
| item 14 — the version-marker mirror | **CLOSED** | `version_marker_mirror` FIXED: after restore-v1 the legacy `v2/system/version.json` exists and matches `version_doc_path()` byte for byte. On the control the mirror is absent entirely, and it is precisely what made 2.0.8's validate stop at `version-marker: app component missing/mismatched`. With item 14 in, 2.0.8 gets past that step — and straight into R9-H1. |

### 4. The three deep-dives Lead asked for

**(a) `restore-v1` on three store shapes.** All three behave correctly and the
exit code matches.

| Store shape | `v2/` back | reports | CLI exit |
|-------------|-----------|---------|----------|
| promoted **and** archived | yes | archive / promote / version-marker / junk, all ok | 0 |
| promoted, **never** archived | yes, both entries | `archive-v1-legacy: no v1-archive found`, `promote-v2-root: moved 2 item(s) back`, `version-marker` ok | 0 |
| never applied at all | n/a | `promote-v2-root` not ok | **non-zero**, plus a `done` event with `ok: false` |

**(b) Backup failure on the `apply_pending` path.** Covered in the R8-H1 row
above: nothing is touched after the failure, on the direct call and through the
real boot gate alike.

**(c) Byte-identical apply → restore-v1, with junk and a marker.** 41 files
before, `missing: 0`. `changed` is two entries: `runtime/core/version.json`, the
deliberate re-stamp round 8 §5 already accepted as correct, and
`models/registry.json` — **R9-H1**. The control fails this case differently, on
the two junk files item 13 now restores.

**(d) Downgrade onto 2.0.8.** `git archive v2.0.8 src`, run against the restored
store in its own subprocess. The 2.0.8 build imports cleanly, reports
`version: 2.0.8`, and **reads its projects correctly** (`{"p1": {"name": "P1"}}`
— no data loss). Its `migrate validate` walks `version-marker` green, then stops
red on `readonly-registries: 5 target(s) mismatched`. On the control it never
got that far, stopping red on `version-marker` itself.

**(e) UI side, one real `run_migration()` over 1,000 files.** 63 events.

| Claim | Result |
|-------|--------|
| unit constant per phase | **PASS** — 1/2/4 `รายการ`, 3 `ขั้น`, no flips |
| every `log_line` event has `log_detail` | **PASS** for `info`/`validate`; the 15 entry-copy events with an empty detail are what the interface contract prescribes |
| `percent_overall` / `phase` monotonic | **PASS** — zero regressions |
| `done <= total` | **PASS** — zero violations |
| phases present | **PASS** — 1 through 5 |
| `validated_steps` | **12** |
| file counts | peak `1001/1001`, 35 events carrying `eta_s` |
| within-entry file counter | **FAIL** — four resets to zero, R9-M1 |

### 5. Harness results

Every suite run as:

```
env -u TAKKUB_STORAGE_ROOT -u TAKKUB_PORT_FILE \
PYTHONPATH=src TAKKUB_ARTIFACTS_DIR=<an ABSOLUTE empty scratch dir per suite> \
timeout 2400 python runtime/exports/2026-09-11/agent-takkub/<suite>.py
```

`TAKKUB_ARTIFACTS_DIR` must be **absolute**. A relative one makes
`504-round2-repro.py` fail `tagged_roundtrip` with an `ImportError`: it passes
its own derived `oldroot/src` as `PYTHONPATH` to a subprocess whose `cwd` is
`oldroot`, so a relative base resolves one level too deep and the 2.0.8 import
falls through to whatever `agent_takkub` is in site-packages. Harness recipe,
not a product defect — round 8's note about clearing the two env vars still
applies on top of it.

| Suite | Result |
|-------|--------|
| `504-round2-repro.py` | `failures: []` |
| `504-round2-extra.py` | `failures: []` |
| `504-round3-faults.py` | `failures: []` |
| `504-round4-faults.py` | 56/56 |
| `504-round5-faults.py` | 12/12 |
| `504-round6-faults.py` | 12/12 |
| `504-round8-faults.py` | **44/44** |
| `504-round9-faults.py` (new) | **19/22** on `d46487b7`, **2/22** on `39d4c8aa` |

One correction to carry forward: the `504-round8-faults.py` sitting in
`runtime/exports/` is the **44-case** variant, not the 45-case file round 8 was
written from — it has no `progress_monotonic` fixture, which is why 44/44 is the
right number here rather than 45/45. Round 9's suite carries that fixture
instead, along with 21 others.

Round-9 suite, case by case: **17 FIXED** (pass on `d46487b7`, fail on
`39d4c8aa`) — `restore_v1_promoted_only`, `restore_v1_never_applied`,
`backup_failure_boot_apply_pending`, `backup_failure_apply_pending_direct`,
`already_backed_up_recopies`, `already_backed_up_corrupted`,
`archive_excludes_promoted`, `progress_monotonic`, `progress_unit_stable`,
`progress_log_detail`, `validated_steps_real`, `validate_ok_steps_truthful`,
`throttle_gap_no_reenumerate`, `restore_junk_deleted_outright`,
`version_marker_mirror`, `prune_orphans_after_rollback` and
`ui_run_migration_1000`; **2 pass on both**, `restore_v1_promoted_and_archived`
and `progress_done_le_total`, neither of which round 8 had flagged; **3 fail on
both** — `apply_restore_byte_identical` and `downgrade_2_0_8_validate` (R9-H1),
and `progress_file_counter_monotonic`, whose head run adds four resets-to-zero
the control does not have (R9-M1).

**Targeted tests.** 253 tests across `test_boot_flow.py`, `test_cli_migrate.py`,
`test_core_migration.py`, `test_boot_flow_terminal.py`,
`test_core_migration_pre_migrate_backup.py`,
`test_core_migration_promote_v1.py`, `test_core_migration_round4_wal.py` and
`test_auto_migrate_boot.py`: **252 pass, 1 fails** —
`test_t4_no_unexplained_or_delete_write_path_bare_except_swallow`, which is
R9-L2 and reproduces on the control.

**Import contracts.** `python -m importlinter.cli lint-imports` exits 0 against
all **29** contracts declared in `pyproject.toml`.

### 6. Prod rehearsal

Not re-run, per the assignment. The latest numbers are Lead's, in the #504
comment: apply 772 s, restore-v1 448 s, `missing: 2`, `changed: 1`. Read against
this round: the `missing: 2` is item 13's junk, which `restore_junk_deleted_outright`
now restores byte-identically, and the `changed: 1` is the version marker, which
round 8 §5 already accepted. What that rehearsal predates is R9-H1 — a re-run on
`d46487b7` should be expected to report `missing: 0` and `changed: 2`, the marker
plus `models/registry.json`.

### 7. What has to happen before 2.1.0 ships

- [ ] **R9-H1**: have `restore-v1` restore the domain-step half of the
      pre-migrate backup too, not only the deleted-outright names. The data and
      the mechanism both already exist — `restore_from_backup_dir()` on the same
      `backups/pre-migrate-<ts>` puts `models/registry.json` back byte-identically.
      This also closes #568 item 1, open since round 6.
- [ ] **R9-M1**: make the copy-to-verify check-in carry the completed count
      rather than 0 — `on_file(len(files), len(files), "")` keeps the anti-stall
      guarantee without resetting the counter, or let `boot_flow` ignore a
      `files_done` lower than the one it last reported for the same entry.
- [ ] **R9-L1**: either drop the unreachable block, or move the
      `promote_had_something` check ahead of the promote rollback so the message
      written for the operator is the one they actually see.
- [ ] **R9-L2**: add the `# swallow-ok:` comment at `engine.py:278`. The repo's
      own gate is red until it lands.
- [ ] **R9-L3**: give `apply()` the same H6 re-apply `apply_pending()` has, or
      write the reachability argument down. Not a shipping blocker.
- [ ] Still open from earlier rounds and untouched by round 13: the per-provider
      authenticated login check, and CI green on both `windows-latest` and
      `macos-latest`.

### 8. Working notes

`504-round9-faults.py` never edits repository source and never touches a real
home: every fixture is its own directory under `TAKKUB_ARTIFACTS_DIR`, with
`config.DATA_HOME`, `SETTINGS_HOME`, `RUNTIME_DIR` and `migration_home()` all
redirected there. Where round 8 seeded a migrated store by calling
`MigrationEngine.apply()` directly, round 9 seeds through `run_boot_stage()` —
on a store with a legacy nested `v2/` root `layout_state()` reads `mixed`, so
that is `apply_pending()`, the path a real 2.0.x machine takes and the one round
13's fixes were written against. `apply()` on that same shape is red for the
R9-L3 reason, identically on both commits. Every control used `git archive
39d4c8aa | tar -x` into a scratch tree with `PYTHONPATH` pointed at it — no
branch switch, no worktree mutation. Lead's `home2` and `home3` were not read or
written at all this round.

Evidence, all under `runtime/exports/2026-09-11/agent-takkub/`, which is
gitignored: `504-round9-faults.py`, the seven copied legacy suites plus
`504-review-repro.py`, and `r9-evidence/` holding `504-round9-faults.jsonl` and
`504-round9-faults.control.jsonl` (the head/control pair), every legacy suite's
log and stderr, `pytest-targeted.xml`, `lint-imports.log`, the `ctl-39d4c8aa`
control tree, and the per-case artifact directories including each run's
captured `events.jsonl`, `run.jsonl`, `validate.json` and `downgrade.txt`.
