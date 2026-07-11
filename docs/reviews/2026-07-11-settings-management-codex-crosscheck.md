# Settings Management code cross-check — 2026-07-11

## Verdict

**FAIL.** The package has solid repository boundaries and the routing choke point is mostly correct, but it is not safe to ship as an atomic settings editor yet. The review found 4 high-severity partial-state/data-loss paths, 5 medium-severity behavior gaps, and one coverage gap. The most urgent issue is that editing an MCP containing credentials persists the masked placeholder over the real secret.

Scope reviewed: all 30 Python files under `src/agent_takkub/settings_management/`, the swap wiring in `src/agent_takkub/user_actions.py`, and the underlying stores called by the repositories. Graphify AST result: 337 graph nodes / 701 edges / 18 communities.

Verification run:

- `pytest -q tests/test_settings_window_routing.py tests/test_settings_management_transaction.py tests/test_settings_management_roles.py tests/test_settings_management_mcps.py tests/test_settings_management_skills.py tests/test_settings_management_plugins.py tests/test_settings_management_providers.py tests/test_settings_management_feature_flags.py tests/test_settings_management_ui.py tests/test_settings_management_ui_phase2.py tests/test_settings_management_ui_phase3.py tests/test_settings_management_ui_phase4.py`
- Result: **131 passed**. These tests characterize happy paths but do not inject failures between repository writes, so they do not cover the partial-state bugs below.

## Findings

### HIGH-1 — MCP edits destroy stored credentials

**FAIL — data loss / credential corruption.**

`mcps.get()` deliberately returns a secret-masked config (`repositories/mcps.py:91-102`). The page puts those masked `env` and `args` values into editable controls (`pages/mcp_page.py:172-191`), then `_draft()` sends them back verbatim on every Save (`pages/mcp_page.py:236-251`). `_draft_to_cfg()` preserves unknown top-level keys but unconditionally replaces `args` and `env` with the draft (`repositories/mcps.py:39-48`). Consequently, changing only `command` or `type` on a credential-bearing MCP replaces the actual token/password/URL credential with `••••••••`. The masker confirms that exact placeholder behavior at `shared_dev_tools.py:634-654`.

The existing test only proves the read is masked (`tests/test_settings_management_mcps.py:112-120`); it never performs a masked read → unrelated edit → update → raw-store round trip.

Required fix: carry explicit “unchanged secret” sentinels or merge masked fields against the raw existing config field-by-field. Add a regression test covering secret `env`, credential-bearing `args`, and secret `headers`.

### HIGH-2 — Role create/update/delete is not one transaction

**FAIL — partial state across registry + markdown + access stores.**

- Create commits the role registry and markdown through `custom_roles.create_role()`, registers it in memory, and only then writes Access (`repositories/roles.py:104-124`). If Access fails it intentionally returns `ok=True` and leaves a role without the requested access (`repositories/roles.py:125-134`). This directly contradicts the required aggregate transaction.
- Update saves `custom-roles.json`, mutates the live registry, writes the markdown file, and only afterward starts the separate Access transaction (`repositories/roles.py:147-168`). A markdown failure leaves the registry changed; an Access failure leaves both General stores changed. The live registry is outside rollback as well.
- Delete removes the role registry/file and live registration first, then resets tool/provider/skill stores without a transaction and ignores all cleanup return values (`repositories/roles.py:191-205`). `custom_roles.delete_role()` itself returns success even when unlinking the markdown file fails (`custom_roles.py:238-256`).

Required fix: snapshot all final paths (registry, role markdown, providers, pane-tools, skill-policy) before the first mutation; stage the new in-memory role and apply it only after disk commit. Treat any cleanup false/exception as failure and rollback all stores.

### HIGH-3 — Skill delete can permanently orphan policy or delete the wrong half

**FAIL — partial state across `SKILL.md` + skill policy.**

The repository deletes `SKILL.md` first (`repositories/skills.py:231-233`), then mutates skill policy and ignores the return from `save_policy()` (`repositories/skills.py:235-243`). If policy persistence fails, the operation still returns success while policy retains references to a now-missing skill. There is no `FileTransaction` in this repository.

Required fix: wrap the skill file and `SKILL_POLICY_FILE` in one `FileTransaction`, check `save_policy()`, and raise on failure so the skill file is restored.

### HIGH-4 — MCP role variants are neither transaction members nor reliably regenerated

**FAIL — master/policy/variants can disagree.**

MCP delete snapshots only the master and pane-tools policy (`repositories/mcps.py:186-188`). Removing the master immediately rewrites every role variant (`shared_dev_tools.py:502-526`), but variant writes are best-effort and their failures are swallowed (`shared_dev_tools.py:569-605`). If the subsequent policy save fails (`repositories/mcps.py:191-200`), `FileTransaction` restores master and policy but cannot restore the already-regenerated variants.

There is a second runtime gap: Role Access saves MCP allowlists through `pane_tools_policy.set_role_items()` (`services/relationships.py:69-76`), whose implementation only writes `pane-tools.json` (`pane_tools_policy.py:248-271`). Variant regeneration is tied to master mutations (`shared_dev_tools.py:495`, `shared_dev_tools.py:525`), so a successful Access Save can leave running/spawned panes consuming a stale role variant. This violates the “no fake button” rule: the UI reports success but the effective MCP config may not change.

Required fix: expose a checked variant-generation operation, include all affected variant paths in the transaction, and regenerate only after both master/policy staging succeeds. Role Access MCP changes must invoke it too.

### MEDIUM-1 — Provider toggle bypasses required live broadcast

**FAIL — persistence succeeds but the operational action is incomplete.**

The new Providers page calls repository `update()` (`pages/providers_page.py:197-211`), which directly calls `provider_state.set_disabled()` (`repositories/providers.py:137-148`). The established operational API is `orchestrator.toggle_provider()`, which persists and broadcasts the change to every live Lead (`orchestrator.py:1387-1425`). Legacy settings explicitly routes its staged toggle through that API (`user_actions.py:365-374`). The new window has no orchestrator callback, so Save reports success without the live-session notification promised by the existing behavior.

Required fix: inject an application-level toggle callback into the window/page, or move the broadcast-capable operation behind a service used by both surfaces.

### MEDIUM-2 — Dirty guard does not cover page navigation, legacy jump, or close

**FAIL — drafts can be hidden/lost, although entity-to-entity Save targets are correct.**

Within one entity page, selection and “New” transitions correctly prompt Save/Discard/Cancel and restore the selection on cancel/failure (`widgets/management_page.py:120-165`). Concrete pages save through their own `_current`, so no direct cross-entity wrong-target path was found.

However, sidebar navigation switches the stacked page unconditionally (`window.py:127-134`), the legacy link invokes its callback directly (`window.py:67-70`), and the window has no `closeEvent` dirty guard. A dirty page can therefore be hidden behind another page, abandoned by opening legacy settings, or closed without any warning. The draft remains attached to the old page rather than leaking into another entity, but the save-model contract is incomplete and users can lose work.

Required fix: make the shell query the active page's `is_dirty/save/discard` hooks before every page/window transition and before close.

### MEDIUM-3 — Lead provider override has no capability-loss notice in the new UI

**FAIL — issue #101 requirement missing on the redesigned surface.**

Lead is correctly unlocked by the repository/relationship model, and the Access combo is enabled whenever `provider_forced` is false (`pages/roles_page.py:211-217`). But the only adjacent note is the forced-provider note; there is no warning tied to selecting non-Claude (`pages/roles_page.py:118-150`, `pages/roles_page.py:211-217`). The legacy UI does show the required warning and tooltip before Save (`settings_window.py:886-905`).

Required fix: add a visible warning driven by the staged combo selection (not only current persisted state), using `provider_config.lead_capability_gap()` or the provider spec flags, and cover it with a UI test.

### MEDIUM-4 — `compare` flag is documented as “both” but routes to legacy only

**FAIL — swap contract mismatch.**

The flag declares `compare` as “both, dev-only” (`settings_management/feature_flags.py:1-2`, `settings_management/feature_flags.py:18-21`). The sole user-action router only special-cases `NEW`; every other value, including `COMPARE`, opens legacy only (`user_actions.py:307-325`). There is no routing test for compare (`tests/test_settings_window_routing.py:30-73`).

Required fix: either implement opening both surfaces for the one redesigned landing view or remove/rename the unsupported flag value and documentation.

### MEDIUM-5 — Relationship write leaks raw filesystem exceptions instead of returning `OperationResult`

**FAIL — rollback happens, but UI error handling is bypassed.**

`write_access()` calls `provider_config.save_role_overrides()` before the other stores (`services/relationships.py:56-76`) but catches only `RuntimeError` (`services/relationships.py:77-78`). Provider persistence can raise `OSError` from mkdir/write/replace (`provider_config.py:127-141`). `FileTransaction.__exit__` will attempt rollback for that exception, but the exception escapes the repository and can tear through the Save handler instead of producing a user-visible failure result.

Required fix: catch filesystem/JSON errors at the aggregate boundary after rollback and return a failed `OperationResult`; do not obscure rollback failures (next finding).

### LOW-1 — Rollback is best-effort and cannot report rollback failure

**FAIL — base mechanism passes normal tests but does not guarantee restoration.**

`FileTransaction` correctly snapshots existing/missing files and rolls them back for any exception (`transaction.py:33-47`); its four unit tests pass. But rollback restores with direct `write_bytes()` and catches/logs every `OSError` without surfacing it (`transaction.py:49-59`). Callers can therefore report an ordinary operation failure while the rollback itself left partial state. Restoration is also not atomic.

Required fix: restore via temp + replace and collect rollback errors into a transaction-specific exception/result that clearly reports “operation failed and rollback was incomplete.”

### LOW-2 — Role Access cannot assign shipped skills shown elsewhere in the same UI

**FAIL — incomplete relationship affordance.**

The Skills repository lists project plus shipped roots (`repositories/skills.py:47-48`, `repositories/skills.py:91-104`), but the Role Access checklist scans only `Path.cwd()` (`pages/roles_page.py:196-200`). A shipped skill can be visible on the Skills page with a “Manage roles” button yet absent from the destination checklist.

Required fix: obtain assignable skills from the repository/service contract rather than importing `skill_scan` directly with a narrower root set.

## Swap wiring and legacy reachability

**PASS with the `compare` exception above.**

- Repository-wide search found only one construction of the legacy `SettingsWindow`, at `user_actions.py:346-363`; all external entry points call `_open_settings_window()` (`user_actions.py:293-305`, `user_actions.py:909-920`).
- With `new`, Providers & Roles routes to the redesigned window; legacy-only initial views route directly to the old window (`user_actions.py:319-325`). Routing tests cover new/legacy/default and Users (`tests/test_settings_window_routing.py:30-73`).
- Pipeline Builder, Templates, Users, Role Overlap, and the MCP/Plugin/Skill matrices remain reachable through the redesigned window's “Open legacy settings” action (`window.py:62-70`, wired at `user_actions.py:335-340`). It lands at Providers & Roles rather than preserving a legacy subview, but the legacy sidebar still exposes all those views.

## “No fake buttons” summary

**FAIL overall.** Honest behavior includes hiding the unavailable Provider create button (`pages/providers_page.py:40-51`) and hiding Danger Zone when delete is not a capability (`widgets/danger_zone.py:42-58`). Real gaps are:

1. Role Access MCP Save can succeed without refreshing effective role variants (HIGH-4).
2. Provider enable/disable Save persists without the required live broadcast (MEDIUM-1).
3. Shipped Skill “Manage roles” leads to a checklist that cannot assign that skill (LOW-2).

## Recommended fix order

1. Prevent MCP masked-secret writeback and add regression coverage.
2. Introduce aggregate transactions for role create/update/delete and skill delete.
3. Make MCP variant generation checked, transactional, and invoked after role policy changes.
4. Route provider toggles through the broadcast-capable application service.
5. Add shell-level dirty guards and the Lead capability-loss warning.
6. Resolve `compare` semantics and expand failure-injection tests.
