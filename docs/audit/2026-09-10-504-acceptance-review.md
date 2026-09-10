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
