# Final Closeout Pack (after 1.3.0) — Phase 0 audit (2026-08-24)

Pack: `docs/plans/final-closeout-after-1.3.0/` · HEAD at audit: `72c9bc4` (v1.3.0 + final doc) · open issues: #376 (high, in progress), #362 (untouched)

| Item | State | Evidence |
|---|---|---|
| A #376 delivery correctness | **IN PROGRESS** | backend worktree — ungated account-pending marker + `can_accept_input` predicate + accepted-on-busy-marker |
| B OpenViking strict project isolation | **MISSING** | `core/context_sources/{openviking,resource}_source.py` accept `project` but never filter; `context_builder.merge_openviking*` has no scope validation (backend#5 flagged in #372 report) |
| C Context/token gating by task size | **MISSING** | `context_builder.budget_tokens_for` = 12% of window only; no small/medium/large gate, no per-source opt-out |
| D Settings UI Knowledge / OpenViking / Design Tools | **MISSING** | `settings_window.py` views: Providers/Models/Routing/Brain(CORE V2)/… — no Knowledge & Design group; OV = env only, design integrations = CLI only |
| E Context Debug / retrieval trace UI | **PARTIAL** | `core/context_sources/trace_store.py` + `doctor [knowledge/context] last-trace` exist; no UI, no scope-reject/trust-reject/task-size fields |
| F real service validation (21st/Figma/Penpot/OV) | **BLOCKED (needs credentials / server)** | user-provided |
| G real GUI acceptance | **BLOCKED (needs eyes)** | `27_MANUAL_END_TO_END_SCRIPT.md` items 3,4,8,11-14,17 |
| H rollback/failure drill | **MISSING** | `23_ROLLBACK.md` documented, never exercised — dev instance only (never prod) |
| I observability polish | **PARTIAL** | doctor sections exist; missing scope/trust rejects, task gate chosen, provider delivery ready/blocked reason |
| Hard constraints (canonical owners, no local LLM, #362 untouched) | **HOLDS** | reviewer `docs/audit/2026-08-24-master-upgrade-review.md` |

Plan: A → patch **1.3.1** (user directive) · B+C+D+E+I → wave (worktrees; B/C serialize merge on `context_builder.py`) → **1.4.0** · H → qa on dev cockpit without restart step · F/G → user.
