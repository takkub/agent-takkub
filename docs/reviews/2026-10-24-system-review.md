# System review 2026-10-24 (v2.2.30) — Codex × Gemini(agy) × Lead

Sources: Codex (`codex exec -s read-only`), Gemini via Antigravity `agy --mode plan`, Lead's own pass (vulture + module-reachability script).
Raw reports: session scratchpad `review_codex.md`, `review_gemini.md`. Every item below marks who found it and whether the Lead re-checked it in code.

Legend: ✅ verified by Lead in code · 🔁 found by ≥2 reviewers · ❓ single source, verify before acting · ❌ rejected

## A. Dead code — delete (≈2,000+ lines + their tests)

| # | Target | Lines | Found by | Status |
|---|---|---|---|---|
| A1 | `core/scheduling/process_registry.py`, `runtime_control.py` | 212 | Lead+agy | ✅🔁 0 src refs |
| A2 | `core/brain/sources/{decision_note,knowledge,role_memory}_source.py` | 166 | Lead+agy | ✅🔁 |
| A3 | `core/context_sources/{brain,conversation,resource}_source.py` (keep `base`, `trace_store`, `doctor_section` — live) | 255 | Lead | ✅ |
| A4 | `core/capabilities/plugin_manager.py` + `CapabilityRegistry` in `registry.py` + `design_integrations.build_client` path | ~200 | Lead+agy | ✅🔁 (`PenpotClient` itself is live via settings_knowledge_design — keep) |
| A5 | `core/secrets/backends/wincred_backend.py` (never registered, set/delete raise NotImplementedError) | 106 | Lead+agy | ✅🔁 |
| A6 | `core/contracts/store.py` (`ConfigStore`, `RuntimeStore`) | 30 | Lead | ✅ |
| A7 | `core/model_catalog/legacy.py` + `registry.py` (0 prod importers) | ~280 | Codex+Lead(vulture) | 🔁 — keep migration fixture coverage |
| A8 | `lead_bash_audit.py` (Phase-1 audit hook never wired) | 103 | agy+Lead | ✅🔁 only self-refs |
| A9 | `limit_status.Poller` (superseded by LimitStore) | 93 | agy+Lead(vulture) | 🔁 |
| A10 | `settings_window._build_deferred_role_row`, `_refresh_deferred_role_labels`, `_role_defer_labels` | ~60 | Codex+Lead | ✅🔁 |
| A11 | `provider_usage._usage_fingerprint`, `opencode_helper.opencode_exec`, `issues._warn_cockpit_repo_unresolved`, `backlog.list_projects_with_backlog`, `skill_audit.audit_existing_role` | ~120 | Codex+Lead | 🔁 |
| A12 | agy-only list: fan-out queue `TAKKUB_QUEUE_FANOUT` (~180, default off), `performance_settings.from_dict/validate/to_dict`, `pipeline_config.with_providers/provider_disabled_targets`, `preview_controller.set_device/set_approved/check_navigation`, `project_rules.generate_project_rules`, `provider_config.lead_capability_gap`, `provider_models.all_models`, `provider_update.eligible_providers`, `plugin_installer.list_marketplaces`, `logs_panel._EVENT_COLOR` | ~450 | agy | ✅ removed 2.2.31: performance_settings.from_dict/validate/to_dict, pipeline_config.with_providers/provider_disabled_targets, generate_project_rules, lead_capability_gap, provider_models.all_models, eligible_providers, list_marketplaces, _EVENT_COLOR · kept: preview_controller (feeds doctor nav_block_counts), TAKKUB_QUEUE_FANOUT (opt-in feature flag) |

Rejected (NOT dead): `core/migration/steps_v1.py` (agy) — boot `apply_pending()` still reaches it for any install not yet migrated (Codex) ❌ · `removed_providers.py` kimi handling — rejects `--provider kimi` + migrates stale settings ❌ · `headless.py` — pyproject entry point + Dockerfile ❌ · HTTP `do_GET`/HTMLParser `handle_*`/Qt slots — framework callbacks ❌ · `invalidate_*`/`reset_*` cache helpers — test seams, keep · Jev `decide.py`/`typesafe_bridge.py` — live but default-off; removing is the user's call.

## B. Bugs

| # | Where | What breaks | Found by | Status |
|---|---|---|---|---|
| B1 | `core/migration/steps_v1.py:1121` CoreInternalStoreStep re-entry | re-validate mismatch → recopy frozen source over live `system/` → newer Brain memories/messages lost, account tombstone undone | Codex (repro) | ✅ fixed 2.2.31 |
| B2 | `core/storage/jsonl_store.py:56` `append` read-whole/replace, no lock, fixed `.tmp` | concurrent done workers lose Brain records | Codex (repro) | ✅ fixed 2.2.31 |
| B3 | `core/conversation/ingest/cursor_store.py:31` unlocked read-modify-write | cursor of another role lost → transcript re-ingested (duplicate history) | Codex (repro) | ✅ fixed 2.2.31 |
| B4 | `core/conversation/ingest/{codex,claude,gemini,cursor}_adapter.py` `read_new` | partial last JSON line skipped forever; growth between stat/read → duplicates | Codex (repro) | ✅ fixed 2.2.31 |
| B5 | `core/conversation/ingest/__init__.py:96` | claude ingest ignores pane's curated config dir/project dir → missing or wrong transcript | Codex | ✅ fixed 2.2.31 |
| B6 | `provider_usage.py:1625` `_merge_identical_account_rows` | two codex accounts with equal quota merged into one card | Codex (repro) | ✅ fixed 2.2.31 |
| B7 | `headless_window.py:117` shutdown | quits before PTY cleanup threads finish → orphan processes (POSIX/Docker) | Codex | ✅ fixed 2.2.31 |
| B8 | `orchestrator.py:17954` `_on_pane_close_clicked` → `self.close(role_name)` | project_ns resolved then dropped → can close the same role in the ACTIVE project | agy | ✅ fixed 2.2.31 |
| B9 | `orchestrator.py:14694,15241,15266,15696,16286,16299` `getattr(pane,"provider")` | attribute doesn't exist → always None → feedback-prompt auto-skip uses no provider markers/keys, turn tokens unattributed | agy | ✅ fixed 2.2.31 |
| B10 | `orchestrator.py:8923` language nudge | `task_id=None` → `None == None` → nudge never sent | agy | ✅ fixed 2.2.31 |
| B11 | `orchestrator.py:4267` provider-switch respawn via `_assign_dispatch` | bypasses ResourceGovernor slot | agy | ✅ fixed 2.2.31 |
| B12 | `orchestrator.py:6688` `_child_scan_cache` | never evicted; PID reuse → stale children | agy | ✅ fixed 2.2.31 |
| B13 | `orchestrator.py:3180` `_ps()` before validation | ghost PaneState after rejected assign | agy | ✅ fixed 2.2.31 |
| B14 | `provider_usage.py:1021` sort by `p.stat()` outside try | file replaced mid-glob → gemini card "unexpected error" | agy | ✅ fixed 2.2.31 |
| B15 | `core/secrets/backends/file_backend.py:37` | secrets written 0644 on macOS | agy | ✅ fixed 2.2.31 |
| B16 | `orchestrator_text.py:441` tiny-diff screenshot exemption | untracked new file counts as 0 lines → big new CSS skips screenshot gate | agy | ✅ fixed 2.2.31 |

## C. Main-thread stalls (perf)

| # | Where | Found by |
|---|---|---|
| C1 | `orchestrator.py:7922` `_find_evidence_files` → `list(rglob("*"))` before pruning node_modules, image hashing + sleep on Qt thread in `done()` | Codex+agy 🔁 |
| C2 | `status_header.py:1539` `taskkill /T /F` sync, no timeout (viewer replace + app close) | Codex |
| C3 | `main_window.py:1579` tab open → `ensure_project_skill_links` → `git ls-files` (timeout 15s) on Qt thread | agy |
| C4 | `logs_panel.py:149` 1s poll + parse while dock hidden | agy |
| C5 | `status_header` 2s tick reads profile/projects uncached | agy (partly fixed by #899/#915 — re-measure) |

Status 2.2.31: C1 fixed (os.walk prune) · C2 fixed (taskkill timeout 5s) · C3 fixed differently — link repair must finish before Lead spawns, so it stays on the Qt thread; `git ls-files` now runs only when a real (non-link) skill folder exists · C4 fixed (skip poll while hidden, catch up on show) · C5 no change — profile/projects reads are already stat-validated `cached_read` (#658).

## D. Architecture debt → backlog (not issues)

- God-files: orchestrator 18,291 lines (`done` 1,310 lines), `spawn_engine._spawn_once` 2,213, `lead_inbox._send_when_ready` 1,283, cli `build_parser` 1,906; `godfile-map.md` still says 4,045 → stale.
- Transcript parsers duplicated core-adapter vs `remote/notify.py` and already diverged (codex `local_image` dropped in core).
- `write_json_atomic` lives in `core.migration.registry_copy_step` but storage/custom_roles/auto_issue import it → layer inversion.
- codex/gemini/cursor helpers copy-paste CLI runner + history scan.

## Proposed order

1. Batch A (A1–A11 now; A12 after per-item check) — one commit, run guard+contract tests + tests touching deleted names.
2. Batch B P1: B2+B3+B4 (one shared locked JSONL/cursor primitive), B8, B9, B5, B7, C1.
3. Verify ❓ items, open issues only for real bugs; D → backlog cards.
