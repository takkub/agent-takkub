# #319 — codex 0.148 store verification: forking/archive, drift fix, `/export` gap

## Report

`codex-cli 0.148.0` is installed on this dev box. Per the `provider-cli-schema-drift`
lesson (0.147's `agent_message`→`item_completed` flip went unnoticed because `codex exec`
kept writing the old schema while every real TUI pane wrote the new one, and CI only
probes via `exec`), this issue required testing the resume/mirror adapters against the
**real, on-disk 0.148 store**, not a fixture — then fixing whatever drifted, and assessing
whether `/export` (new in 0.148, exports a session to Markdown) can be adopted into
done-handoff evidence.

## What's actually new on disk in 0.148

Confirmed via `codex --help`, `codex features list`, and direct inspection of
`~/.codex/`:

- New top-level subcommands: `archive`, `unarchive`, `delete`, `fork`, `migrate-rollouts`.
- New file: `~/.codex/thread_history_1.sqlite` (+ `-shm`/`-wal`), tables `thread_items`,
  `thread_turns`, `thread_history_projection_state`, `_sqlx_migrations`.
- `codex migrate-rollouts` (no `--apply`) reports on THIS machine: **744 rollouts scanned,
  671 eligible (legacy), 73 already paginated, 0 failed.**

## Verification method (real store, not a fixture)

1. `codex migrate-rollouts --json` → dumped the full per-thread report, compared an
   `already_paginated` rollout's raw JSONL against an `eligible` (legacy) one.
2. Queried `thread_history_1.sqlite` directly (`sqlite3 .schema`, then a real row) —
   `thread_items.item_json` uses camelCase `item_type` (`userMessage`/`agentMessage`/…),
   confirming this file is a **read-projection Codex itself builds from the `.jsonl`**
   (`thread_history_projection_state.next_rollout_byte_offset` proves it), not a
   replacement store. The `.jsonl` stays the source of truth; nothing here needs adapter
   changes as long as that projection relationship holds.
3. Picked a real 2026-03-05 test session (`019cbcb1-…`, unrelated to any active project),
   ran `codex archive <id>` → **the rollout `.jsonl` physically moved** from
   `sessions/2026/03/05/` to a flat `archived_sessions/` dir. `codex unarchive <id>` moved
   it straight back. This is the one real behavior change that breaks a resolver — see fix
   below.
4. Ran `codex migrate-rollouts --thread <id> --apply` on the same throwaway session and
   diffed the file before/after (size 27776→25418 bytes, same 12 lines). Full record-shape
   diff (Python, `json.dumps` per record) showed:
   - `session_meta.payload` gained `session_id` (duplicate of `id`) and `history_mode`.
   - **Every** record gained an `ordinal` integer.
   - `event_msg.item_completed.item` — the actual message payload our parsers read — is
     **byte-for-byte the same shape**: `type` (`UserMessage`/`AgentMessage`/…), `content`
     blocks with `type: text` (user) / `type: Text` (agent, capitalized) and `text`.
5. Confirmed today's one 0.148.0-authored rollout (`cli_version` checked in
   `session_meta`) independently has the identical `item_completed`/`AgentMessage`/
   `UserMessage`/block-type shape — not just the migrated one.
6. `codex features list | grep export` → nothing; `codex export --help` → not a
   subcommand at all (falls through to the top-level help). `codex fork <id> "<prompt>"`
   piped with `< /dev/null` → `Error: stdin is not a terminal` (same for `resume`) — every
   TUI-driven command in 0.148 hard-requires a real TTY; this Bash tool provides none.

All throwaway-session state (archive → unarchive, the one `migrate-rollouts --apply`) was
restored/left in its migrated form on a 2026-03-05 test session with no relation to any
tracked project; nothing belonging to an active project or pane was touched.

## Drift found and fixed

**`codex archive` moves the rollout out of the day-sharded `sessions/YYYY/MM/DD/` tree
into a flat `archived_sessions/` dir.** Every resolver that does an exact id+cwd lookup
(`codex_helper.resolve_codex_jsonl_for_cwd`, `core/conversation/ingest/codex_adapter.
resolve_source`, `remote/notify._resolve_codex_jsonl_path`'s `wanted_uuid` branch) only
ever scanned `sessions/`, so a session archived mid-conversation would resolve to `None`
— silently, matching the exact "`None` is a diagnosis, not a state" failure mode the
`provider-integration` skill calls out, not a crash or a logged error.

**Fix**: added `codex_helper.codex_archived_sessions_root()` and wired it as a fallback
(archived checked only after the live `sessions/` root misses, and only for exact
id+cwd lookups — the no-id "newest session for this cwd" scan never falls back, since a
session actively being spawned/resumed can't already be archived) into all three call
sites. `remote/notify.py`'s existing `_codex_rollout_candidates` already had a
flat-layout fallback (`test_unknown_layout_falls_back_to_whole_tree`, from #293) built in
for exactly this shape, so no new day-dir logic was needed there — just pointing it at
the archived root too.

`_list_recent_codex_sessions` (the Remote Mobile resume-picker list) was deliberately
**left unchanged** — hiding archived sessions from a resume picker is the same behavior
`codex resume`'s own picker has by default, so the pre-existing `sessions/`-only scan
already does the right thing there.

## Not drift: message schema is stable across 0.147→0.148 and across pagination

The `item_completed`/`AgentMessage`/`UserMessage`/content-block shape our parsers depend
on (`codex_adapter._parse_record`, `remote/notify._codex_record_message`) is **unchanged**
— both before and after `migrate-rollouts --apply`, and in a rollout natively authored by
0.148.0. Added regression fixtures pinning this (session-scoped to codex 0.148, both
duplicated-parser layers per the D3 lesson):

- `tests/test_core_conversation_ingest.py::test_codex_read_new_parses_0148_paginated_schema`
- `tests/test_remote_notify.py::TestCodexRemoteHistory::
  test_reads_item_completed_messages_from_codex_0_148_paginated`

Plus fallback-behavior tests for the archive fix:
`tests/test_codex_helper.py::TestCodexArchivedSessionFallback` (3 tests),
`tests/test_core_conversation_ingest.py::test_codex_resolve_source_falls_back_to_archived_sessions`
(+ the no-id negative case), `tests/test_remote_notify.py::TestCodexRemoteHistory::
test_archived_session_still_resolves_by_id` (+ live-wins-over-stale-archived and
no-id-never-checks-archived).

## Risk flagged, not yet real (documented per #103, not silently hidden)

`codex features list` shows two disabled-but-under-development flags:
`background_paginated_rollout_migration` and `local_thread_store_compression`. Neither is
active on this install. If either ships enabled, Codex could stop keeping full message
text inline in the rollout `.jsonl` (today `thread_history_1.sqlite` is only a
read-projection built FROM the `.jsonl`, confirmed above) — that would break every
raw-jsonl-scrape adapter silently, the same class of outage this whole issue exists to
catch. No code change is possible pre-emptively without real data to test against; the
action is re-verifying against a real store the next time codex bumps a minor version,
per the existing `provider-cli-schema-drift` project memory.

## `/export` — assessed, not adopted

Acceptance criteria: adopt `/export` into done-handoff evidence **if it can be invoked
non-interactively**. It cannot, on 0.148:

- No CLI subcommand (`codex --help`'s command list has no `export`; `codex export --help`
  falls through to the top-level help rather than an unknown-subcommand error).
- Not gated behind a `codex features` flag either (checked the full list) — it is purely
  a TUI-session slash command.
- Every TUI-driving invocation (`resume`, `fork`) hard-fails outside a real TTY
  (`Error: stdin is not a terminal`) — `codex exec` (the one genuinely headless surface,
  and the one `codex_helper.codex_exec` wraps) doesn't run inside a session with slash
  commands available at all.

The one theoretically viable path — typing `/export` into a **live** codex-tui pane's
PTY, the same mechanism `takkub send --to <role>` already uses to inject text into a
running pane, then reading back whatever `/export` produces — was not tested here. It
needs an actual running pane to drive, which is out of scope for a backend-role
investigation (no pane-spawning access, and spawning one just to poke at `/export` is a
Lead-level call). Flagged in `docs/v2/phase6-report.md`'s gap list and here for whoever
next touches done-handoff, per #103.

## Files changed

- `src/agent_takkub/codex_helper.py` — `codex_archived_sessions_root()` +
  `resolve_codex_jsonl_for_cwd` archived-fallback.
- `src/agent_takkub/core/conversation/ingest/codex_adapter.py` — `resolve_source`
  archived-fallback (factored via `_scan_root_for_cwd`).
- `src/agent_takkub/remote/notify.py` — `_resolve_codex_jsonl_path`'s `wanted_uuid`
  branch now checks both roots.
- `docs/v2/phase6-report.md` — gap #9 (this writeup, summarized for V2's ingest adapter).
- Tests: `tests/test_codex_helper.py`, `tests/test_core_conversation_ingest.py`,
  `tests/test_remote_notify.py` (see above).

## V2 alignment

This is Conversation ingest adapter territory (`docs/v2/phase6-report.md`), not the
Phase 3 spawn-side `ProviderAdapter` (`docs/v2/phase3-report.md`) — no session-storage
resolution lives in the spawn/account/router layer. V2's eventual replacement for
`core/conversation/ingest/codex_adapter.py` must carry forward the same dual-root
(`sessions/` + `archived_sessions/`) lookup; gap #9 in `phase6-report.md` is the durable
pointer so this doesn't get silently dropped when that layer gets rewritten.
