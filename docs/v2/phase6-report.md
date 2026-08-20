# Core V2 — Phase 6 Report: Conversation V2 + Checkpoint

> epic #309 · branch `wt/backend-1787127765` (base `feat/v2-core`, on top of Phase 1–3 commits
> `c12a87e`/`ffbddeb`/`dac585e`/`6d1606b`) · 2026-08-19

Implements plan §5/§6/§7/§8 ("Conversation", "Hybrid Chat Storage", "Structured Summary",
"Checkpoint", "Native Resume Fallback") — the epic's own biggest gap: D2 in
`docs/v2/CURRENT_ARCHITECTURE_AUDIT.md` ("no Takkub-owned conversation store"), matrix decision
`Conversation store: NEW` / `Checkpoint: NEW` in `REUSE_VS_REWRITE_MATRIX.md` §4.

---

## 1. Files created

```text
src/agent_takkub/core/conversation/__init__.py
src/agent_takkub/core/conversation/_json_io.py
src/agent_takkub/core/conversation/flag.py
src/agent_takkub/core/conversation/store.py
src/agent_takkub/core/conversation/summary.py
src/agent_takkub/core/conversation/checkpoint.py
src/agent_takkub/core/conversation/resume.py
src/agent_takkub/core/conversation/facade.py

src/agent_takkub/core/conversation/ingest/__init__.py
src/agent_takkub/core/conversation/ingest/base.py
src/agent_takkub/core/conversation/ingest/cursor_store.py
src/agent_takkub/core/conversation/ingest/claude_adapter.py
src/agent_takkub/core/conversation/ingest/codex_adapter.py
src/agent_takkub/core/conversation/ingest/gemini_adapter.py
src/agent_takkub/core/conversation/ingest/opencode_adapter.py

tests/test_core_conversation.py            # 30 tests
tests/test_core_conversation_ingest.py     # 25 tests

docs/v2/phase6-report.md   # this file
```

## 2. Files modified

| File | Change |
|---|---|
| `src/agent_takkub/core/models/conversation.py` | +`MessageRole` StrEnum, +`Message` dataclass. Existing `Conversation`/`ProviderSessionBinding`/`Checkpoint` untouched. |
| `src/agent_takkub/core/storage/jsonl_store.py` | `JsonlStore.__init__` +`true_append: bool = False` (default preserves the exact old read-modify-`os.replace` behavior byte-for-byte — proven by `test_true_append_default_is_off_preserves_old_behavior`); +`read_from(offset) -> OffsetReadResult` (byte-offset incremental read, the idempotent-ingest primitive). |
| `src/agent_takkub/core/storage/paths.py` | +`conversations_root()`, +`conversation_dir(project_id, conversation_id)`. |
| `src/agent_takkub/core/accounts/selector.py` | `CooldownFailoverSelector.select()` — the ONE existing "account switch" call site in the whole codebase — now also calls `core.conversation.facade.on_account_switch(...)` right beside its existing `log_switch(...)` call. |
| `src/agent_takkub/orchestrator.py` | `done()` (~line 3831) and `subagent_done()` (~line 1463): one fail-open, flag-gated, background-`threading.Thread` hook each, inserted immediately after `_save_decision_note(...)`. Flag OFF (default): a single cheap import + `if` short-circuits before any thread is spawned — zero behavioral or performance delta. |
| `src/agent_takkub/remote/notify.py` | `read_recent_lead_messages()` — one new helper `_read_from_conversation_store_v2()` tried first; `None` (flag off / nothing ingested yet / any exception) falls through to the existing scanner-dispatch path unchanged. |
| `tests/test_core_jsonl_store.py` | +5 tests (`true_append`/`read_from`); Qt-leak subprocess probe now imports every new `core.conversation`/`core.conversation.ingest.*` module. |

## 3. What's wired vs. what's a documented gap

### Wired for real

- **`ConversationStore`** (§5.2): one directory per `(project_id, conversation_id)` —
  `conversation.json` metadata, true-append `messages.jsonl` (+ size-capped rotation to
  `messages.N.jsonl`), true-append `bindings.jsonl` for `ProviderSessionBinding` pointers.
- **4 ingest adapters** (claude/codex/gemini/opencode) — the same 4 providers
  `remote/notify.py::_HISTORY_SCANNERS`/`ProviderSpec.supports_remote_history` already cover.
  Each **WRAPS** the existing reader (`src/agent_takkub/chatlog_scanner.py`, `src/agent_takkub/codex_helper.py`, `src/agent_takkub/gemini_helper.py`,
  `src/agent_takkub/opencode_helper.py`, `token_meter.py`'s `find_session_by_uuid`/`find_latest_session`) — none of
  those files were modified. Idempotent via a persisted cursor (byte offset for the 3 JSONL
  providers, message count for opencode's shared sqlite db — see §4 below for the ceiling).
  Codex's two schema generations (0.146 `agent_message`/`user_message` vs. 0.147+
  `item_completed`, the D3 lesson) are both exercised by
  `test_codex_read_new_parses_legacy_schema` / `..._parses_0147_item_completed_schema` /
  `..._mixed_schema_file_never_double_counts`.
- **`RollingSummary`** (§6): structured fields matching the blueprint
  (`objective/currentState/decisions/completed/inProgress/pending/importantFiles/warnings/next`,
  snake_case). `apply_done_note()` extends `orchestrator._condense_done_note`'s "first line is the
  headline" heuristic into an update rule: clean `done()` promotes the headline into `completed`;
  failed `done()` records it into `pending`+`warnings`+`next_action`. Bounded at 20 items/list.
- **`CheckpointManager`** (§7): `checkpoint-NNNNN/{summary,working-context,tasks,git-state,
  provider-binding,runtime-state}.json` — exactly the 6 files the task scope named. Auto-created
  on every `done()`/`subagent_done()` when the flag is on (a clean simplification — see §4).
- **Native resume fallback API** (§6d / blueprint §7): `resume.build_resume_context()` +
  `render_resume_prompt()` turn a checkpoint into a markdown payload a fresh provider session
  could be seeded with.
- **Remote mirror read-through** (§6d): `src/agent_takkub/remote/notify.py`'s ONE touch point, fail-open, flag off
  by default.
- **3 hook call sites**, all fail-open + flag-gated: `orchestrator.done()`, `orchestrator.
  subagent_done()`, `CooldownFailoverSelector.select()`.

### Documented gaps

1. **kimi/cursor have no ingest adapter** — same gap `src/agent_takkub/remote/notify.py` already has
   (`ProviderSpec.supports_remote_history=False` for both). Issue #103.
2. **Account → Conversation linkage doesn't exist yet.** `on_account_switch()`'s call site is
   real (wired into `CooldownFailoverSelector`), but nothing today supplies a `conversation_id` to
   it, so in practice it is currently a no-op every time it fires — documented in the function's
   own docstring rather than faked with a guessed mapping. `test_facade_on_account_switch_
   without_conversation_id_is_noop` pins this.
3. **Native resume fallback is not wired to a real spawn/resume call site** — task scope said
   "ยังไม่ต้องเชื่อม spawn จริง — เตรียม API + test", so `build_resume_context`/
   `render_resume_prompt` exist and are tested but nothing calls them from `spawn_engine.py` yet.
4. **Codex/gemini record parsing is a deliberate, documented DUPLICATE**, not a reuse, of
   `src/agent_takkub/remote/notify.py`'s `_codex_record_message`/`_antigravity_record_message`/
   `_gemini_record_messages` — `core.*` cannot import `src/agent_takkub/remote/notify.py` (it imports
   `PyQt6.QtCore` at module level, which `core-is-bottom-layer` forbids transitively). Each
   adapter's docstring calls this out explicitly and points at the schema-drift lesson (D3) so a
   future codex/gemini schema flip fails a **core** test, not just the remote-mirror one.
5. **Opencode's ingest cursor is a message COUNT, not a byte offset** — its shared sqlite db has
   no offset to seek by. Documented ceiling in `opencode_adapter.py`'s docstring: correct for
   normal session growth, would only start silently dropping the oldest tail (matching
   `opencode_helper`'s own existing `limit` behavior) if a session grew past 100k messages between
   two ingests.
6. **No compaction/merge of rotated `messages.N.jsonl` parts.**
7. **Checkpoint fires on every `done()`/`subagent_done()`**, not selectively on "major task
   boundary" — simpler than trying to classify boundary significance, and checkpoint writes are
   cheap (6 small JSON files). If checkpoint volume becomes a real problem, add a debounce in
   `facade._on_pane_done_impl`.
8. **`RollingSummary.decisions`/`important_files` are never populated automatically** — only
   `current_state`/`completed`/`in_progress`/`pending`/`warnings`/`next_action` update from
   `apply_done_note`. Extracting decisions/files would need something like
   `chatlog_scanner._first_h2_heading`'s heuristic; left for whoever builds the Memory Candidate
   Pipeline (plan §15) since it's the same "pull structured claims out of prose" problem.
9. **#319 (2026-08-20): codex 0.148 verified against a real store — one real drift found and
   fixed, one risk documented, `/export` confirmed non-adoptable.** Full writeup:
   `docs/audit/2026-08-20-issue-319-codex-0148-verify.md`. Summary for whoever builds V2's
   Conversation ingest adapter (this is the layer that must absorb it, not the Phase 3
   spawn-side `ProviderAdapter`):
   - `codex archive` (0.148+) **moves** the rollout file out of the day-sharded `sessions/`
     tree into a flat `archived_sessions/` dir — an id-based lookup that only scans `sessions/`
     silently returns nothing for a session archived mid-conversation (the exact "None is a
     diagnosis, not a state" failure the `provider-integration` skill warns about). **Fixed**
     in all three V1 call sites (`codex_helper.resolve_codex_jsonl_for_cwd`,
     `core/conversation/ingest/codex_adapter.resolve_source`,
     `remote/notify._resolve_codex_jsonl_path`) with an archived-root fallback, id-match only
     — V2's adapter must carry the same dual-root lookup forward, not just `sessions/`.
   - The `item_completed`/`AgentMessage`/`UserMessage` message schema itself is **unchanged**
     between 0.147 and 0.148, including after running `codex migrate-rollouts --apply`
     (verified: migrated a real rollout, diffed before/after). The migration only adds an
     `ordinal` index per record and a duplicate `session_id` key + `history_mode` field to
     `session_meta` — harmless today, pinned by a regression test in both
     `tests/test_core_conversation_ingest.py` and `tests/test_remote_notify.py`.
   - **Risk, not yet a drift**: `codex features list` shows `background_paginated_rollout_migration`
     and `local_thread_store_compression` both `under development`/disabled. If either ships
     enabled, Codex may stop keeping full message text inline in the rollout `.jsonl` (today it
     is a projection *cache* in `thread_history_1.sqlite`, source-of-truth stays the `.jsonl`) —
     that would break every raw-jsonl-scrape adapter (V1 and this V2 layer alike) silently. No
     action possible now beyond flagging it; re-verify against a real store on the next codex
     minor bump per the `provider-cli-schema-drift` lesson.
   - **`/export` (TUI slash command, Markdown transcript to clipboard/file) has no CLI/headless
     equivalent** — `codex --help`'s subcommand list has no `export`, and `codex exec`/`resume`/
     `fork` all hard-require a real TTY (`stdin is not a terminal` when piped). Not adopted into
     done-handoff evidence. The only theoretically viable path is typing `/export` into a LIVE
     codex-tui pane's PTY the same way `takkub send` already injects text — untested here (would
     require spawning and driving a real pane, out of scope for this backend-role
     investigation); flagged as a #103 follow-up for whoever owns done-handoff.

## 4. Design decisions made without asking

- **One conversation per `(project, role)`**, not a general multi-conversation-per-project scheme
  — `store.conversation_id_for(project_id, role)` is deterministic
  (`f"conv-{project_id}-{role}"`), letting `done()`'s hook and the remote-mirror read-through agree
  on the same conversation id without a separate lookup table. A real multi-conversation scheme
  (per-task, explicit conversation switching) is future work.
- **`true_append` is an opt-in constructor flag on the EXISTING `JsonlStore`**, not a new class —
  every other Phase 1–5 caller (`account_switches.jsonl`, etc.) is untouched because the default
  is `False`.
- **`ConversationStore.conversation_dir()` is the single source of truth for a store's paths** —
  `CheckpointManager`/`facade.py` derive from `store.conversation_dir(...)`, never from
  `core.storage.paths.conversation_dir()` directly. Doing it the other way was an actual bug caught
  during implementation: a `ConversationStore(root=<custom>)` (tests, or a future per-user root)
  would have had its messages/summary go to the custom root while checkpoints silently went to the
  real `RUNTIME_DIR` instead.
- **Ingest adapters expose a plain `(resolve_source, read_new)` module-level pair**, not a class —
  matches how `src/agent_takkub/remote/notify.py`'s own `_HistoryScanner` treats its callables, and keeps
  `monkeypatch.setattr(claude_adapter, "resolve_source", ...)` trivial in tests.

## 5. Verification

```text
targeted (this phase's own tests):
  tests/test_core_conversation.py            30 passed
  tests/test_core_conversation_ingest.py     25 passed
  tests/test_core_jsonl_store.py             15 passed (5 new)

targeted (blast-radius — every test file touching orchestrator.done()/
subagent_done(), remote/notify.py, or core.accounts.selector):
  tests/test_subagent_mode.py
  tests/test_regression_findings_2026_06.py
  tests/test_qa_plan_fanout.py
  tests/test_pipeline_executor.py
  tests/test_pending_done_notice_visibility.py
  tests/test_pane_health_reporting.py
  tests/test_pane_health_close_report.py
  tests/test_orchestrator_session_uuid.py
  tests/test_orchestrator_shard.py
  tests/test_orchestrator_done_gate.py
  tests/test_orchestrator_auto_respawn_replay.py
  tests/test_lead_self_protection.py
  tests/test_installed_cwd_fallback.py
  tests/test_idle_watchdog.py
  tests/test_done_evidence.py
  tests/test_done_gate_no_task.py
  tests/test_done_note_symmetrize.py
  tests/test_done_digest_facts_wiring.py
  tests/test_delivery_pointer_failure.py
  tests/test_cross_tab_done.py
  tests/test_cli_server.py
  tests/test_cli.py
  tests/test_auto_chain.py
  tests/test_remote_notify.py
  tests/test_orchestrator_notify_lead.py
  → all pass, 0 failures, 0 flag-off behavior change

lint-imports:  Contracts: 28 kept, 0 broken  (core-is-bottom-layer, core-models-pure,
               core-contracts-pure all still KEPT — new conversation package respects the
               PyQt6/engine/UI/CLI boundary; remote/notify.py's new import of core.conversation
               is a remote -> core edge, which no contract forbids)
ruff:          All checks passed (every file created/modified this phase)
```

Real-store smoke tests (`test_*_adapter_reads_a_real_local_session_if_present`) are `pytest.skip`
guarded, not forced green against a specific machine's store — they run for real whenever the
corresponding provider has local session history and skip cleanly otherwise (CI has none of the 4
provider CLIs authenticated, so all 4 would skip there). On this dev machine all 4 ran for real
against actual claude/codex/gemini/opencode session stores and passed (verified: `pytest -k
real_local -v` → `4 passed, 0 skipped`).

Full suite was **not** run (targeted-tests policy — full suite runs once at the qa batch gate per
project convention).
