# Role-registry sync audit — backend, 2026-07-11

Task: make the role list a single source of truth (`roles.py`) end-to-end, so a
custom role created via "New Role" shows up everywhere a role should show up,
without hand-editing N files per new role. This closes Wave 3 #6 phase (1)
"dynamic role registry as source-of-truth" from
`docs/plans/2026-07-09-core-upgrade-plan.md` (the ProviderSpec phase 0 of that
item is issue #103, separate, not touched here). Colors (`roles.py`
`Role.color` vs `cockpit_theme.ROLE_COLORS`) are explicitly out of scope per
task instructions — not touched.

## Root cause

Two independent hand-maintained tuples duplicated the role list instead of
reading `roles.py`'s registry, and both had drifted:

- `pipeline_config.VALID_ROLES` = `DEFAULT_TEAMMATES` only — **never included
  custom roles at all**. Everything downstream of it (Pipeline Builder
  palette, Providers & Roles rows/toggles, the pipeline hop validator, the
  status-strip role chips) inherited the gap.
- `pane_tools_dialog.ROLES` was a fully hand-typed 12-name tuple that had
  drifted **in both directions**: missing real roles (`codex`, `gemini`,
  custom roles) while still listing four roles that were never real anywhere
  in the system (`designer` — retired from defaults 2026-0x, `analyst` /
  `security` / `docs` — never existed as a role at all, no `roles.py` entry,
  no role file, never spawnable; traced to a 2026-07-09 wave-2 stopgap that
  hand-added them to `pane_tools_policy.KNOWN_ROLES` only, never to
  `roles.py` or `pane_tools_dialog.ROLES`, and never cleaned up).
- `pane_tools_policy.KNOWN_ROLES` was a third hand-typed copy with the same
  four phantom names, and was itself inconsistent with
  `pane_tools_dialog.ROLES` (had `codex`/`gemini`, the other didn't; both
  omitted `shell`).

Both `custom_roles.py` (A6 registry) and `skill_audit.load_all_role_docs()`
were already correct — they read `roles.py`/the filesystem dynamically. The
bug was everywhere ELSE.

## Audit table

| Surface | File | Status before | Fix |
|---|---|---|---|
| Pipeline Builder role palette | `settings_window.py` `_pipeline_palette_roles` | hardcode (via `pipeline_config.VALID_ROLES`, missing custom) | now dynamic |
| Providers & Roles rows + toggles | `settings_window.py` `_overridable_roles`, `Roles` panel | hardcode (same root) | now dynamic |
| Status-strip role chips | `settings_window.py` `_build_status_strip` | hardcode (same root) | now dynamic |
| Pipeline hop validation (`_norm_entry`) | `pipeline_config.py` | hardcode, frozen at import — would have **silently dropped** a custom role from a saved hop even if the UI showed it | now dynamic |
| `rolesEnabled` map keys | `pipeline_config.py` `_normalize` | hardcode (same root) | now dynamic |
| MCP Matrix rows | `settings_window.py` `_matrix_roles` (`pane_tools_dialog.ROLES`) | hardcode, drifted (missing codex/gemini/custom, phantom designer/analyst/security/docs) | now dynamic |
| Plugins Matrix rows | same as above | same | now dynamic |
| CLI `takkub mcp/plugins allow\|deny\|list --role` | `cli.py` → `pane_tools_policy.known_roles()` | hardcode base (`KNOWN_ROLES`), drifted (phantom roles, missing `shell`) | now dynamic (`known_roles_base()`) |
| Skill Catalog list | `settings_window.py` `_build_skill_catalog_view` → `skill_audit.load_all_role_docs()` | **already correct** — scans `AGENTS_DIR` + `CUSTOM_AGENTS_DIR` on disk | no change |
| New Role's own create flow | `custom_roles.py` + `settings_window._on_create_role_clicked` | **already correct** — writes registry + calls `roles.register_role()` live | no change |
| Task Dock role rows | `task_dock.py` | **already correct** — renders actual `task_ledger` assignments, no enumerated role list | no change |
| Status-bar live role chips (running panes) | `status_header.py` | **already correct** — iterates actual spawned panes, not a static list | no change |
| `agent_pane.py` state dots | — | **already correct** — no hardcoded role list found | no change |
| `main_window.py` pane grid | — | grid enumeration was already removed in an earlier refactor; `DEFAULT_TEAMMATES` import is dead weight kept only to silence an unused-import lint ("for a future role picker UI" per its own comment) | not a sync bug — nothing to fix; left as-is |
| `project_wizard.py` grid-position picker | — | no such picker exists in that file; New Role's column/row picker (`settings_window.py`) is free-form numeric input, not an enumerated role list | not a sync bug |
| `pane_tools_policy` / `shared_dev_tools` / `lead_context` per-role **default** dicts (`_ROLE_MCP_POLICY`, `_ROLE_PLUGIN_POLICY`) | multiple | these are policy-default maps with a documented fallback for any unlisted role (including customs) — not "list every role" enumerations | not a sync bug, no change |
| `routing_planner._EXPLICIT_ROLE` (Thai/EN "ให้ backend ทำ..." regex) | `routing_planner.py` | hardcoded role-name alternation, does include phantom `designer` | **not fixed — see note below** |
| `cockpit_theme.ROLE_COLORS` keys | `cockpit_theme.py` | has the same phantom names (`designer`/`analyst`/`security`/`docs`) as extra unused dict keys | **explicitly out of scope (colors) — not touched**, see note below |

### Note: `routing_planner._EXPLICIT_ROLE` left unfixed

`routing_planner.classify()` is not called anywhere in the running engine —
grepped the whole `src/` tree, zero call sites. Its own docstring says what
it is: an executable **spec/test** of the CLAUDE.md prompt's routing rules,
so the prompt and the tests can't silently drift from each other. At
runtime, actual routing is Lead (the LLM) reasoning over CLAUDE.md in
natural language, not this regex. So a custom role name missing from
`_EXPLICIT_ROLE` has zero effect on real behavior — it only means the
*regression-test module* can't parse "ให้ salesbot ทำ..." the same way it
parses "ให้ backend ทำ...". Making it dynamic is possible (role names are
already charset-restricted to `[a-z0-9_-]`, so no regex-escaping hazard) but
was left alone: it's a real (if low-value) hardcode site, flagged here
rather than silently skipped, per the task's own instruction.

### Note: `cockpit_theme.ROLE_COLORS`

Confirmed by reading every call site (`settings_window.py`, `app.js`): always
used as `.get(role, fallback)`, never iterated as "the list of roles" — so
its four phantom keys are inert, not a functional sync bug. Left untouched
per the task's explicit color-is-out-of-scope instruction (needs user
sign-off separately).

## The fix

Added `roles.all_role_names(*, include_lead=True)` — the single function
every surface above now calls (directly or transitively). Recomputed on
every call (never cached at import time), because a custom role registers
live when "New Role" creates one, with no cockpit restart.

- `pipeline_config.VALID_ROLES` (frozen constant) → `pipeline_config.valid_roles()`
  (function). Hop validation (`_norm_entry`/`_norm_hop`/`_norm_hops`/
  `_norm_custom_template`) now takes the valid-role set as an explicit
  parameter computed once per `_normalize()` call, instead of reading a
  frozen module global.
- `pane_tools_dialog.ROLES` (frozen constant) → `pane_tools_dialog.matrix_roles()`
  (function): every registered role except `shell`/`codex`/`gemini` (intentional
  filter — those panes never load `--mcp-config`, kept from the original list's
  intent, just now correctly derived instead of hand-typed).
- `pane_tools_policy.KNOWN_ROLES` (frozen constant) → `pane_tools_policy.known_roles_base()`
  (function): every built-in role name from the registry. `known_roles()`
  (the custom-role union) is unchanged in shape, just now built on a correct
  base.
- `settings_window.py`'s `_OVERRIDABLE_ROLES` / `_MATRIX_ROLES` /
  `_PIPELINE_PALETTE_ROLES` module-level constants → `_overridable_roles()` /
  `_matrix_roles()` / `_pipeline_palette_roles()` functions. This mattered
  independently of the `pipeline_config`/`pane_tools_dialog` fixes: even with
  those fixed, a **module-level constant** in `settings_window.py` would
  still freeze at cockpit-boot import time, before any custom role ever
  existed. Since `SettingsWindow` is deliberately reconstructed fresh on
  every open (not a singleton — see its own class docstring), moving these
  into functions called from inside `_build_*_view()` is what actually
  delivers "create a role, reopen Settings, see it everywhere" with no
  restart.

## Files touched

- `src/agent_takkub/roles.py` — add `all_role_names()`
- `src/agent_takkub/pipeline_config.py` — `VALID_ROLES` → `valid_roles()`; hop
  validators take an explicit valid-set param
- `src/agent_takkub/pane_tools_dialog.py` — `ROLES` → `matrix_roles()`
- `src/agent_takkub/pane_tools_policy.py` — `KNOWN_ROLES` → `known_roles_base()`
- `src/agent_takkub/settings_window.py` — three module constants → functions;
  fixed the one remaining `pipeline_config.VALID_ROLES` call site (status
  strip)
- `tests/test_roles.py` — `TestAllRoleNames` (4 new tests)
- `tests/test_pipeline_config.py` — updated 2 call sites to `valid_roles()`
- `tests/test_pane_tools_dialog.py` — replaced the phantom-role-locking test
  with `matrix_roles()` coverage (built-ins + a registered custom role)
- `tests/test_pane_tools_policy.py` — replaced the phantom-role-accepting
  test with a registered-custom-role test + an explicit
  never-registered-name-is-rejected test; renamed `KNOWN_ROLES` references
- `tests/test_settings_window.py` — updated 2 call sites to `_matrix_roles()`
- `tests/test_role_registry_sync.py` (new) — end-to-end integration test:
  creates a real custom role the same way the New Role dialog does, asserts
  it appears in every surface from the audit table above (pipeline palette,
  Providers & Roles, MCP/Plugins matrix, CLI known-roles, Skill Catalog),
  that Lead/shell stay correctly excluded from pipeline-eligible sets, that
  unregistering drops the role everywhere again, and that a pipeline hop
  referencing the custom role survives a save/load round trip (the "silent
  drop" bug hop validation would have hit before this fix).

## Verification

- `pytest tests/test_roles.py tests/test_pipeline_config.py tests/test_pane_tools_dialog.py tests/test_pane_tools_policy.py tests/test_settings_window.py tests/test_custom_roles.py tests/test_skill_audit.py tests/test_role_registry_sync.py tests/test_provider_config.py`
  — 273 passed, 0 failed
- `ruff check` + `ruff format --check` — clean on every touched file
- `lint-imports` — 18/18 contracts kept (the new `pane_tools_dialog → roles`
  import doesn't cross any layering boundary; both are leaf-pure modules)
- Cross-platform: every change is pure Python logic (dict/tuple derivation),
  no OS-specific paths or commands — identical behavior on Windows/macOS
