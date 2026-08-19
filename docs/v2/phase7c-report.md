# Core V2 — Phase 7c: Context Builder + Reflection hook (epic #309)

> worktree `wt/backend-1787129526`, base `feat/v2-core` (Phase 1–6 + 7a/7b merged) · 2026-08-19

## สรุป

Wires `core.brain` (Phase 7a/7b's candidate pipeline + `RetrievalEngine`) and
`core.conversation` (Phase 6's rolling summary) into the orchestrator for
real: a **Context Builder** that assembles a short memory/summary block and
injects it into a freshly-assigned task, and a **Reflection hook** that turns
a pane's own `done()`/`subagent_done()` note (plus cockpit-measured
`DigestFacts`) into new `MemoryCandidate`s. Both hooks are flag-gated
(`TAKKUB_V2_CONTEXT` new, `TAKKUB_V2_BRAIN` reused) and fail-open, following
Phase 6's exact hook pattern in `src/agent_takkub/orchestrator.py`.

## 7c-1 — Context Builder

- **`src/agent_takkub/core/brain/context_builder.py`** (NEW, pure — no
  orchestrator/PyQt/UI import):
  - `budget_tokens_for(context_window, *, file_read_supported=True) -> int`
    — ~12% of the model's context window, clamped to a floor/ceiling
    (`400`–`6000` tokens); falls back to `src/agent_takkub/token_meter.py`'s own default
    context size (200k) when the window is unknown; halved (with its own
    smaller floor) for a provider whose CLI has no structured file-read
    tool (`ProviderSpec.supports_agent_file_read=False`, #273 — e.g. codex).
  - `build_context(project, role, task_text, budget_tokens) -> str` —
    queries `RetrievalEngine` directly (NOT `facade.recall`, deliberately:
    see the module docstring — going through `facade` would be a circular
    import, since `facade.build_context_for_assign` is this module's own
    caller, and it would also wrongly couple Context Builder's own flag to
    the Second Brain's separate write-path flag) against two buckets — the
    project's own (`scope=PROJECT`, filtered to drop another role's private
    `AGENT`-scoped memories) and the true-global bucket
    (`BrainStore(None)`, `scope=GLOBAL`) — merges, re-trims to the token
    budget, and appends the Conversation rolling summary's headline fields
    (`current_state`/`in_progress`/`pending`/`next_action`) if
    `TAKKUB_V2_CONVERSATION` is on and a summary exists. Empty output
    (`""`) when nothing is found or `budget_tokens<=0`/blank `task_text`.
    Both reads are bounded: one project's `BrainStore` is a single JSONL
    file (never a directory walk), the conversation read is a single
    `summary.json` (never the raw message log); record counts are further
    capped (8 project-scoped + 4 global) on top of `RetrievalEngine`'s own
    token-budget trim.
- **`src/agent_takkub/core/brain/flag.py`** (EXTEND): `v2_context_enabled()` reads
  `TAKKUB_V2_CONTEXT` — a **separate** flag from `TAKKUB_V2_BRAIN` on
  purpose (see the module's updated docstring): Context Builder only
  *reads* whatever the Second Brain already has on disk, so it can be
  enabled independently of the write-path flag.
- **`src/agent_takkub/core/brain/facade.py`** (EXTEND): `build_context_for_assign(project,
  role, task_text, *, context_window=None, file_read_supported=True) ->
  str` — the stable entry point the orchestrator hook calls; flag-gated on
  `v2_context_enabled()`, fail-open (any exception → `""`).

## 7c-2 — Context-Injection hook in `_assign_dispatch`

- **`src/agent_takkub/orchestrator.py`**:
  - New module-level `_inject_v2_context(task, project_ns, role_name,
    base_role_a, effective_provider) -> str`, placed just above the
    `Orchestrator` class (same neighbourhood as the file's other small
    module-level helpers) — factored out of the inline hook body so it's
    directly unit-testable without spinning up a pane/spawn. Flag OFF
    (default): returns `task` unchanged before any import — byte-identical
    to pre-Phase-7c behavior. Flag ON: calls `core.brain.facade.
    build_context_for_assign` inside a **300ms-timeout**
    `concurrent.futures.ThreadPoolExecutor` (a stuck/slow recall must never
    delay a spawn); on timeout, logs `context_builder_timeout` and returns
    `task` unchanged — `executor.shutdown(wait=False)` so the timed-out
    call never blocks *this* call waiting for the background worker to
    finish. Any other exception fails open the same way
    (`context_builder_hook_error`). A non-empty context block is appended
    to `task` with a blank-line separator.
  - **Call site**: `_assign_dispatch`, immediately after `task =
    _append_verify_fail_hint(task, base_role_a)` and before the
    plan/shard `delivery_task` wrapping and `_task_handoff_pointer` call —
    exactly the task spec's ordering, so a long injected block still flows
    through the existing file-handoff-pointer path instead of bypassing it.
    `effective_provider` (already computed a few lines above for the codex
    task-rewrite decision) is reused directly to look up
    `PROVIDER_REGISTRY[...].supports_agent_file_read`.

## 7c-3 — Reflection hook in `done()` / `subagent_done()`

- **`src/agent_takkub/core/brain/sources/reflection_source.py`** (NEW):
  `from_done_note(note, *, project, role, failed, task_id=None) ->
  MemoryCandidate | None` — heuristic, not a classifier: takes the note's
  first non-empty line (the same "first line is the headline" rule
  `orchestrator._condense_done_note`/`core.conversation.summary._headline`
  already use), rejects it if under 8 chars, and files it as
  `MemoryKind.FEEDBACK` (failed — something to avoid next time) or
  `MemoryKind.PROJECT` (clean — a fact about what changed), always
  `trust=AGENT_REPORTED`/`scope=AGENT`/`confidence=INFERRED` — never
  `COCKPIT_MEASURED` (that split stays `digest_facts_source.py`'s job).
- **`src/agent_takkub/core/brain/facade.py`** (EXTEND): `on_pane_done(project, role, *,
  note, digest_facts=None, failed=False, task_id=None) -> None` — flag-
  gated on the SAME `TAKKUB_V2_BRAIN` flag `recall`/`submit` already use
  (this is just another write path into the same Second Brain, not a
  separate feature). Submits the note-derived candidate (if any), and —
  when a `digest_facts.DigestFacts` was passed and `project` is known —
  submits `sources.digest_facts_source.from_digest_facts(...)` too (Phase
  7a's existing adapter, unchanged, now given a real call site). Fail-open,
  same pattern as `recall`/`submit`.
- **`src/agent_takkub/orchestrator.py`**: two call sites, both placed AFTER the Phase 6
  Conversation hook (same comment cross-references it) and both
  `threading.Thread`-wrapped/fail-open/flag-checked-before-import,
  identical shape to Phase 6's hooks:
  - `subagent_done()`: right after the Conversation hook block — no
    `digest_facts` (subagents have no pane/PTY, no worktree git-state
    digest), so only the note-based candidate applies.
  - `done()`: placed after `digest_facts` is fully finalized (both the
    `_compute_digest_facts` happy path and its `except`-fallback), so the
    cockpit-measured facts are available to submit alongside the note —
    this is *later* than the Conversation hook's call site (which fires
    before `digest_facts` exists yet), by necessity.

## Multi-provider / cross-platform

- No path/command specific to any platform in any new/changed file — the
  timeout mechanism (`concurrent.futures`) and thread wrapping are stdlib,
  identical on Windows ConPTY and macOS.
- `_inject_v2_context` reads `PROVIDER_REGISTRY[effective_provider]` — the
  SAME provider-neutral registry every other provider-aware code path in
  the cockpit already reads from (#103); `supports_agent_file_read` is the
  one field that already distinguishes codex from the rest, reused as-is,
  not reinvented.
- `budget_tokens_for`'s `context_window` parameter is currently always
  called with the default `None` (see Gap #1 below) — no per-model context
  size is threaded through yet, so today every provider/model gets the
  same fallback-window budget. Not a claude-only shortcut: the mechanism
  itself is provider-neutral, it just has no real per-model input wired in
  yet.

## Gap / #103 (ประกาศชัด ไม่เงียบ)

1. **`context_window` is never threaded through from a real model
   registry** — `core.models.model.ModelDefinition.context_window` exists
   (Phase 5) but nothing populates a `ModelDefinition` registry anywhere in
   the codebase yet (verified: no `ModelDefinition(...)` construction site
   exists outside its own dataclass definition), and `ProviderSpec` has no
   `context_window` field either. `_inject_v2_context` therefore always
   calls `build_context_for_assign` with `context_window=None`, so
   `budget_tokens_for` always falls back to the 200k-token default window
   for every provider/model alike. `token_meter.context_limit_for_model`
   *does* have real per-model sizes (used for the pane-header token-usage
   display) but is keyed by Claude model id strings, not wired to
   provider/role selection — a future phase could thread it through, but
   guessing a per-provider mapping here risked encoding stale numbers
   nothing tests against.
2. **Role-scoping when `project` is `None`** — `context_builder._recall_
   records`'s AGENT-scope filter is bypassed by its own global-bucket query
   in the one case where `project is None` (both queries hit the same
   `_global` bucket then). Documented in the function's own comment;
   accepted as-is — it only matters for a pane with no active project,
   already a rare/degenerate case elsewhere in the cockpit.
3. **Reflection candidates from a note are single-line, heuristic, not a
   real classifier** — exactly what the task scope asked for ("จาก note
   แบบ heuristic"); a smarter extraction (multi-fact notes, explicit
   decision/pattern/lesson taxonomy) is future work, same category as
   Phase 6's own documented gap #8 ("`RollingSummary.decisions`/
   `important_files` are never populated automatically").
4. **No compaction of the `- (kind, confidence) content` memory lines** —
   `context_builder` renders each retained record as one bullet verbatim;
   no summarization/merging of near-duplicate bullets beyond what
   `MemoryManager`'s own dedup/supersede already did at write time.

## ไฟล์ที่สร้าง/แก้

**สร้างใหม่**:
- `src/agent_takkub/core/brain/context_builder.py`
- `src/agent_takkub/core/brain/sources/reflection_source.py`
- `tests/test_core_brain_context_builder.py`
- `tests/test_core_brain_reflection.py`
- `tests/test_orchestrator_v2_context_hook.py`
- `docs/v2/phase7c-report.md` (this file)

**แก้ไข**:
- `src/agent_takkub/core/brain/flag.py` (+`v2_context_enabled`)
- `src/agent_takkub/core/brain/facade.py` (+`build_context_for_assign`,
  +`on_pane_done`)
- `src/agent_takkub/core/brain/__init__.py` (re-export `v2_context_enabled`)
- `src/agent_takkub/orchestrator.py` (+`_inject_v2_context` module-level
  helper; 3 call sites: `_assign_dispatch`, `done()`, `subagent_done()`)
- `tests/test_core_jsonl_store.py` (no-Qt subprocess probe now imports
  every `core.brain`/`core.brain.sources` module — Phase 7a/7b had not
  added these to the probe; folded in here since 7c both extends and
  newly exercises that whole package)

## Verification

```text
targeted (this phase's own new tests):
  tests/test_core_brain_context_builder.py    25 passed
  tests/test_core_brain_reflection.py          9 passed
  tests/test_orchestrator_v2_context_hook.py   7 passed

targeted (regression — Phase 7a/7b/6 + the no-Qt probe, now covering
core.brain too):
  tests/test_core_brain_models.py
  tests/test_core_brain_pipeline.py
  tests/test_core_brain_sources.py
  tests/test_core_brain_retrieval.py
  tests/test_core_brain_adapter.py
  tests/test_core_conversation.py
  tests/test_core_conversation_ingest.py
  tests/test_core_jsonl_store.py               (no PyQt6 leak, incl. core.brain.*)

targeted (blast-radius — every test file touching orchestrator.
_assign_dispatch()/done()/subagent_done()):
  tests/test_subagent_mode.py
  tests/test_qa_plan_fanout.py
  tests/test_pipeline_executor.py
  tests/test_orchestrator_session_uuid.py
  tests/test_orchestrator_shard.py
  tests/test_orchestrator_done_gate.py
  tests/test_orchestrator_auto_respawn_replay.py
  tests/test_done_evidence.py
  tests/test_done_gate_no_task.py
  tests/test_done_note_symmetrize.py
  tests/test_done_digest_facts_wiring.py
  tests/test_cross_tab_done.py
  tests/test_auto_chain.py
  tests/test_remote_notify.py
  tests/test_orchestrator_notify_lead.py

→ all together: 561 passed, 0 failed, 0 flag-off behavior change
```

Full suite was **not** run (targeted-tests-only policy — full suite runs
once at the qa batch gate before merge).

## lint-imports

`lint-imports` (28 contracts): **28 kept, 0 broken** — `core-is-bottom-
layer`/`core-models-pure`/`core-contracts-pure` all still KEPT;
`context_builder.py`/`reflection_source.py` add no new PyQt6/orchestrator/
UI edges (confirmed structurally by the contract and empirically by the
no-Qt subprocess probe above).

## Ruff

`ruff check` on every file created/modified this phase: **All checks
passed**.
