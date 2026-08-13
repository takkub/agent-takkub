# Issue #162 — "Providers & Roles" vs "Role Overlap" show different role lists

**Status:** fixed (registry gap) + documented (intentional gaps)
**Branch:** `wt/backend-2-1786589559`

## Symptom

Two Settings views disagreed on "the list of every role in the system":

- **Role Overlap** (`settings_window.py::_build_role_overlap_view`, always the
  legacy window — Role Overlap has no equivalent in the new Settings surface
  yet): Analyst, Backend, Codex, Design Critic, Cursor, Data-eng, Designer,
  DevOps, Docs, Frontend, Gemini, Kimi, Maintainer, Mobile, Opencode, QA,
  Reviewer, Security (18 rows).
- **Providers & Roles** ("TEAM ROSTER" panel — legacy `settings_window.py`
  by default, or `settings_management/pages/roles_page.py` if
  `TAKKUB_SETTINGS_UI=new`): Lead, Frontend, Backend, Mobile, DevOps, QA,
  Reviewer, Design Critic, Maintainer (9 rows).

## Root cause

The two views read "every role" from **two structurally different sources**,
and those sources had drifted apart:

1. **Role Overlap** calls `skill_audit.load_all_role_docs()`, which does a
   raw filesystem scan: every `*.md` file under `config.AGENTS_DIR`
   (`.claude/agents/`, shipped with the repo) + `config.CUSTOM_AGENTS_DIR`
   (`~/.takkub/agents/`, user-writable). It shows a role the moment a doc
   file exists, registered or not — correct for its own purpose (a
   TF-IDF overlap audit needs every candidate doc, including not-yet-live
   ones).
2. **Providers & Roles** calls `pipeline_config.valid_roles()` →
   `roles.all_role_names()` — the **role registry**: built-in `Role` objects
   in `roles.py.ALL_DEFAULT` plus runtime-registered custom roles loaded
   from `~/.takkub/custom-roles.json` by
   `custom_roles.load_and_register_all()`. `roles.all_role_names()`'s own
   docstring calls it "the single source of truth ... for every role" — it
   wasn't actually complete.

Both settings_management/pages/roles_page.py (`RoleRepository.list()`) and
the legacy window's `_overridable_roles()` bottom out at this same
`roles.all_role_names()` call — so the fix is the same regardless of which
Settings UI variant (`TAKKUB_SETTINGS_UI=legacy|new`) is active. (The task
brief's hypothesis that the bug lived in `roles_page.py` vs
`providers_page.py` was half right: `roles_page.py` is one of the two real
render paths for "Providers & Roles" when the new-UI flag is on, but
`providers_page.py` shows *providers* — claude/codex/gemini/opencode/
kimi/cursor CLIs — not roles, and isn't involved in this bug.)

### Two independent gaps in the registry, found by diffing (1) vs (2)

- **`opencode` / `kimi` / `cursor` had no `Role()` entry in
  `roles.py.DEFAULT_TEAMMATES`**, even though `provider_config.py`'s
  `_FORCED_PROVIDER` / `FORCED_ROLES` already treats all five
  (codex/gemini/opencode/kimi/cursor) as full forced-provider-identity
  roles on equal footing ("the role's whole point" — the module's own
  docstring), `.claude/agents/{opencode,kimi,cursor}.md` docs already
  shipped, and `cockpit_theme.py` even had an unused
  `PROVIDER_OPENCODE` brand-color constant sitting there with a comment
  saying it mirrors "codex/gemini" in `roles.py` — but only 2 of the 3
  color constants existed, and 0 of them were wired into `ROLE_COLORS`/
  `roles.py`. This was a half-finished #103 (multi-provider) rollout, not
  a deliberate exclusion — `roles.by_name("opencode"|"kimi"|"cursor")`
  returned `None` everywhere in the codebase, not just this Settings page.
- **`data-eng`** (a real, user-created custom role — its `.md` lives under
  the user-writable `~/.takkub/agents/`, not the repo-shipped
  `.claude/agents/`) had no entry in `~/.takkub/custom-roles.json`. Every
  role created through `custom_roles.create_role()` writes both atomically
  (file-then-registry commit order, HIGH-2 in that module), so this can
  only happen when a `.md` is dropped into `CUSTOM_AGENTS_DIR` by hand
  instead of through the "+ New Role" flow — `agent_role_dir()` (used at
  spawn time) already treats such a file as a valid spawn target
  regardless of registry state, so the role WAS really spawnable, just
  invisible to every registry-backed surface.

### Not a bug — confirmed intentional, left unchanged

- **`analyst` / `designer` / `docs` / `security`** ship `.md` docs under
  `.claude/agents/` (repo-checked-in) and have pre-reserved entries in
  `cockpit_theme.ROLE_COLORS`, but are deliberately **not** registered as
  live roles. `roles.py`'s own comment documents exactly this pattern for
  `designer`: *"Designer was removed from defaults; .claude/agents/
  designer.md is preserved so custom-slot add still works for users who
  want it."* These four are starter templates for someone who later
  creates that named role via "+ New Role", not roles that already exist.
  Forcibly registering them would fabricate roles nobody asked to create.
- **codex/gemini/opencode/kimi/cursor are still absent from the "Team
  Roster" CLI-override list** in Providers & Roles even after this fix.
  `_overridable_roles()` explicitly filters out every
  `provider_config.FORCED_ROLES` member — you cannot override the CLI for
  a role whose entire identity IS that CLI, so a "pick your CLI per role"
  list correctly excludes them. (They already appear, unaffected by this
  bug, in the same page's "MODEL CONNECTIONS" panel above the Team Roster,
  which lists `provider_state.TOGGLABLE` providers directly.) Making them
  visible in the Team Roster too — e.g. as locked/non-editable rows, which
  `_build_role_row(locked=True)` already has unused scaffolding for — would
  be a real UI change needing a design/critic review pass, out of scope
  for this backend registry fix.

## Fix

- `src/agent_takkub/roles.py` — added `Role` entries for `opencode`,
  `kimi`, `cursor` to `DEFAULT_TEAMMATES` (column 1, rows 5–7), mirroring
  the existing `codex`/`gemini` pattern exactly.
- `src/agent_takkub/cockpit_theme.py` — added `PROVIDER_KIMI` /
  `PROVIDER_CURSOR` brand-color constants (completing the set started by
  the pre-existing, previously-unused `PROVIDER_OPENCODE`) and wired all
  three into `ROLE_COLORS` (required by
  `tests/test_role_registry_sync.py::test_every_builtin_role_has_a_cockpit_theme_color`).
- `src/agent_takkub/custom_roles.py` — `load_and_register_all()` (the
  boot-time hook) now also scans `CUSTOM_AGENTS_DIR` for orphan `.md`
  files with no matching `custom-roles.json` entry, registers them with
  the same sane defaults `load_custom_roles()` already falls back to for a
  malformed JSON row, and persists them back to `custom-roles.json` so the
  drift self-heals instead of recurring on every boot. Fixes `data-eng`,
  and any future case of a role doc dropped in by hand.
- `src/agent_takkub/settings_window.py` — comment-only: the
  `_overridable_roles()` docstring now lists all five `FORCED_ROLES`
  members instead of just codex/gemini, matching what the code already did.

## Result

`roles.all_role_names()` now includes `opencode`/`kimi`/`cursor` (13 → 16
non-lead built-ins wasn't right terminology; concretely: `DEFAULT_TEAMMATES`
grew 10 → 13) and self-heals any future `data-eng`-shaped drift. Every
surface driven by that function — Providers & Roles' Team Roster (custom
roles like `data-eng` now show up; the five forced-provider roles remain
correctly excluded from the CLI-override list, by design), the Pipeline
Builder palette, the MCP/Plugins matrices, `takkub mcp/plugins allow|deny
--role`, and `roles.by_name()` callers throughout the codebase (grid
colors, labels, spawn) — now resolve `opencode`/`kimi`/`cursor`/`data-eng`
correctly instead of silently falling back to `None`/generic gray.

Role Overlap's raw doc-scan list is unchanged (still correctly includes
the four intentional templates); the remaining count difference between
the two views is now fully explained by documented, intentional design
(FORCED_ROLES exclusion + unregistered starter templates), not by an
undiagnosed data-source split.

## Tests

- `tests/test_roles.py` — updated `test_default_teammates_registry` /
  `test_default_columns_assigned` for the 3 new roles; added
  `test_forced_provider_roles_resolve_and_have_distinct_colors`.
- `tests/test_custom_roles.py` — new `TestBootLoadSelfHealsOrphanDocs`
  class (5 tests): orphan doc gets registered + persisted, an
  already-registered doc is not duplicated/overwritten, an invalid orphan
  name is skipped, missing `CUSTOM_AGENTS_DIR` is a no-op.
- Targeted run (this fix's blast radius): `test_roles.py`,
  `test_role_registry_sync.py`, `test_custom_roles.py`,
  `test_settings_management_providers.py`,
  `test_settings_management_ui_phase4.py`,
  `test_settings_management_ui.py`, `test_settings_management_roles.py`,
  `test_kimi_provider.py`, `test_opencode_provider.py`,
  `test_cursor_provider.py`, `test_agent_role_files_have_browser_guard.py`,
  `test_agent_role_files_have_git_guard.py`, `test_cli_pane_tools.py`,
  `test_cli_server_role_gate.py`, `test_cockpit_theme.py`,
  `test_pane_tools_dialog.py`, `test_pane_tools_policy.py`,
  `test_pipeline_config.py`, `test_role_models.py`, `test_skill_audit.py`,
  `test_main_window_status_bar.py`, `test_main_window_tasks_dock.py`,
  `test_provider_config.py` — all green. Full-suite run left to the QA
  batch gate per project policy (targeted-tests-only mid-flight).

## Follow-ups (not done here — out of scope for a backend registry fix)

1. If product wants codex/gemini/opencode/kimi/cursor visually present in
   the Team Roster too (not just the Providers panel above it), wire
   `_build_role_row(locked=True)`'s already-built-but-unused locked-row
   path for `FORCED_ROLES` instead of filtering them out entirely — needs
   a frontend/critic UI pass (visual review, not something verifiable
   headless).
2. `roles.ALL_DEFAULT`/`DEFAULT_TEAMMATES` also drives the main cockpit
   pane grid (`roles.py`'s own module docstring: "reserves 8 slots in a
   3-column grid") — adding opencode/kimi/cursor makes them appear as
   spawnable grid slots there too. CLAUDE.md flags kimi/cursor's
   ready/busy PTY markers as "ยังไม่ calibrate" (uncalibrated) — this fix
   only completes the *registry* (colors/labels/`by_name()` resolution);
   it does not change or claim to fix ready/busy detection reliability for
   those two providers. Worth a explicit QA smoke pass on kimi/cursor pane
   spawn/state detection before leaning on this in production workflows.
