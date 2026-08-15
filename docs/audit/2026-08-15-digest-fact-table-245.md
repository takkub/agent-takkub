# #245 — Lead Inbox Digest: fact-table bullet + shared-tree files-touched

Follow-up to #244 (`docs/audit/2026-08-15-digest-computed-facts-244.md`),
which explicitly left two items open:

1. `_format_digest_item` still rendered the digest bullet as a prose line
   (`[role] done: <condensed note>`), not a table of cockpit-computed facts.
2. "ไฟล์ที่แตะ" (files touched) was only ever computed for isolated worktree
   panes (which have `WorktreeInfo.base_sha` to diff against) — a shared-tree
   pane had no equivalent baseline.

Both are closed here.

## 1. Digest bullet is now a computed fact table

New pure module `digest_facts.py`:

- `DigestFacts` — frozen dataclass: `role`, `verdict` ("done" — the only
  value that ever reaches the digest; FAILED/blocking notices bypass the
  debounce queue entirely, per `_is_blocking_lead_notice`, unchanged from
  #244), `ref`, `branch`, `commits_ahead`, `uncommitted`, `merge_conflicts`
  (`True`/`False`/`None`), `merge_note`, `files_touched` (`None` means
  *unverifiable*, never a misleading `0`), `files_dirs`, `files_note`,
  `report_path`, `headline` (context only, explicitly documented as never
  the identity source).
- `format_digest_fact_line(facts, stamp="")` — pure renderer. Every clause
  up to the trailing `↳ headline` / `report:` lines is a `key:value` token
  the cockpit measured; the headline is visually set apart and is prose
  context, not identity (the `[ref #N]` badge, computed by #244, remains
  the identity source).
- `union_files_touched(diffstat_text, porcelain_text)` — combines committed
  and uncommitted paths into one deduped count + top-level-dir list (a
  shared-tree pane's work can be committed, uncommitted, or both).

`lead_inbox.py`:

- `_format_digest_item(body, queued_ts, now_ts, facts=None)` — when *facts*
  is supplied, delegates straight to `format_digest_fact_line` instead of
  regex-parsing `body`. `facts=None` (every other digestible body — peer-CC
  relays, or any future caller that doesn't pass one) falls back to the
  original prose rendering, byte-for-byte unchanged — proven by the
  pre-existing `test_lead_inbox_digest.py` suite passing with zero
  modification to its assertions on rendered text.
- `_lead_digest_queue` entries grew a 4th tuple element (`digest_facts`),
  additive only: `_unwrap_notice_item` (3-tuple return) is untouched, so
  every other consumer (`inbox_report`, `_has_pending_lead_notice`,
  `_pump_lead_notify`, `poll_wait`) needs zero changes — they already
  tolerate variable-length tuples. A new `_unwrap_notice_facts` helper reads
  the 4th slot only where a bullet is actually rendered
  (`_flush_lead_digest`).
- `_notify_lead(..., digest_facts=None)` — threads the facts through to the
  queue append. `None` for every FAILED/blocking notice (they never reach
  the digest queue) and for every caller written before #245.

## 2. `done()` computes the fact table once, reuses it for the worktree proposal

`Orchestrator._compute_digest_facts` (new, `@staticmethod`) is the single
place all the git reads happen, fired exactly once per `done()` event:

- **Worktree pane** (`had_worktree` truthy): `commit_count` / `is_dirty` /
  `uncommitted_count` / `merge_conflicts_with_base` / `diffstat` — the SAME
  five calls `_finalize_worktree` already made a few lines later in `done()`
  for the merge-proposal notice. Returns `(facts, precomputed)`; `done()`
  now calls `_finalize_worktree(..., precomputed=precomputed)`, which reuses
  those exact values instead of re-running the identical subprocess calls a
  second time. Verified with a call-counting fake
  (`tests/test_done_digest_facts_wiring.py::TestWorktreePaneDigestFacts::
  test_digest_facts_passed_to_notify_lead_and_git_reads_not_duplicated`) —
  each probe fires exactly once even though both the digest bullet and the
  merge-proposal notice need the same numbers.
- **Shared-tree pane** (`had_worktree` is `None`): no `WorktreeInfo.base_sha`
  to diff against, so uses a NEW baseline — `PaneState.assign_base_sha`
  (below) — with the generic (non-`WorktreeInfo`) probes added to
  `WorktreeManager`. `merge_conflicts` is always `None` with an explanatory
  `merge_note` ("N/A — commit อยู่บน branch ที่ Lead เห็นอยู่แล้ว ไม่ต้อง
  merge") rather than run a merge-tree probe that doesn't apply — a
  shared-tree pane commits directly onto the tracked branch, there is
  nothing to merge, so this also SAVES two git calls versus the worktree
  path, not just adds new ones. `files_touched` unions the committed diff
  since the baseline with the current `git status --porcelain` (a
  shared-tree pane's work can be committed, uncommitted, or both).
  `files_note` explicitly flags the one real limitation: **multiple panes
  can share this same tree, so a commit landing between assign and done is
  not necessarily this pane's own work** — the count can overcount, and the
  note says so rather than implying an exact attribution the cockpit cannot
  prove.
- **No baseline available** (cwd not a git repo, or HEAD unborn at assign
  time): `files_touched=None` with `files_note="ตรวจไม่ได้ (...)"` — never a
  bare `0` standing in for "couldn't check" (#245's explicit requirement).
- Wrapped in `try/except` in `done()` — a git hiccup degrades to a minimal
  `DigestFacts` (ref/report_path/headline still present, files marked
  unverifiable) and logs `digest_facts_error`; it can never break the
  done()-report path itself, same doctrine as `_finalize_worktree`'s own
  try/except.

## 3. New state: `PaneState.assign_base_sha` (shared-tree baseline)

- Set in `_assign_dispatch`, immediately after `spawn()` resolves the real
  cwd, ONLY for the shared-cwd branch (`worktree is None`): one `git
  rev-parse HEAD` via `WorktreeManager().head_sha(cwd)`. `None` for a
  worktree assign (the equivalent baseline already lives in
  `WorktreeInfo.base_sha`) and for a non-git/unresolvable cwd.
  Refreshed on every fresh task dispatch — the same "one baseline per
  assign" semantic `assign_ts` right above it already has.
- Restored (not lost) across a stuck-pane auto-recover respawn
  (`_auto_recover_stuck`/`_do_respawn`): that path resumes the SAME task
  via `close()`+`--resume`, not a fresh `assign()` dispatch, so it needed
  the same snapshot/restore treatment `session_uuid`/`last_assigned_task`/
  `auto_chain`/`requires_commit_on_done` already get. Without this, a
  stuck-recovered pane would silently lose its baseline and `done()` would
  wrongly report "ตรวจไม่ได้" for a pane that actually had one.

## 4. `WorktreeManager` gained generic (cwd-based) probes

`commit_count(info)` / `diffstat(info)` / `uncommitted_count(info)` /
`is_dirty(info)` are now thin wrappers over new generic forms that take a
plain `(cwd[, base_sha])` instead of a `WorktreeInfo` — `commits_since`,
`diffstat_since`, `uncommitted_count_at`, `status_porcelain`. Same git args,
same behaviour (`test_worktree_manager.py`'s pre-existing tests on the
`WorktreeInfo`-based methods pass unchanged, proving the refactor is
behavior-preserving), now reusable for a shared-tree pane's digest facts
which has no `WorktreeInfo` to construct. New pure `parse_porcelain_paths`
extracts changed-file paths from `git status --porcelain` (handles the
rename `old -> new` form, keeps the new path) — used by
`digest_facts.union_files_touched`.

## 5. Performance (explicitly required by the task spec)

No git subprocess call added anywhere runs per-tick — every one is fired
exactly once from an event (`_assign_dispatch` on assign, `done()` on
completion), matching the #229/#244 boundary (`_finalize_worktree`'s own
module comment: *"the orchestrator runs create/finalize on the Qt main
thread ... git ops here are all local and fast; bound them"*):

- **Assign path**: +1 git call (`head_sha`) for a shared-cwd assign only —
  none for a worktree assign (already has its own `head_sha` call inside
  `create()`).
- **Done path, worktree pane**: **net zero new git calls** versus #244's
  baseline — the 5 calls `_compute_digest_facts` makes are the exact same 5
  `_finalize_worktree` used to make on its own; they are now made ONCE and
  shared via `precomputed`, not made twice.
- **Done path, shared-tree pane**: +4 git calls
  (`uncommitted_count_at`/`diffstat_since`/`commits_since`/
  `status_porcelain`) when a baseline exists, +1 (`current_branch`) when it
  doesn't — same bounded/local/one-shot shape #244 already justified for
  the worktree-pane case, applied to the (previously uncomputed) shared-tree
  case for the first time.

## Files changed

- `src/agent_takkub/digest_facts.py` — **new**: `DigestFacts`,
  `format_digest_fact_line`, `union_files_touched`.
- `src/agent_takkub/lead_inbox.py` — `_format_digest_item` accepts `facts`;
  `_unwrap_notice_facts` (new); `_notify_lead` threads `digest_facts`
  through to the queue append.
- `src/agent_takkub/orchestrator.py` — `_compute_digest_facts` (new);
  `_finalize_worktree` accepts optional `precomputed`; `done()` computes
  facts once and wires both consumers; `_assign_dispatch` snapshots
  `assign_base_sha`; `_auto_recover_stuck` snapshots/restores it across a
  stuck-recover respawn.
- `src/agent_takkub/spawn_engine.py` — `PaneState.assign_base_sha` (new
  field).
- `src/agent_takkub/worktree_manager.py` — `commits_since`, `diffstat_since`,
  `uncommitted_count_at`, `status_porcelain`, `parse_porcelain_paths` (new);
  `commit_count`/`diffstat`/`uncommitted_count`/`is_dirty` refactored to
  delegate to the generic forms (behavior-preserving).
- Tests: `tests/test_digest_facts.py` (new), `tests/test_done_digest_facts_
  wiring.py` (new), `tests/test_worktree_manager.py` (new
  `TestGenericCwdProbes` + `TestParsePorcelainPaths`), `tests/test_done_
  evidence.py` (new `TestAssignBaseShaCapture`), `tests/test_lifecycle_
  recovery.py` (new `test_assign_base_sha_restored_after_recover`),
  `tests/test_lead_inbox_digest.py` (2 existing tuple-unpack assertions
  extended to the new 4th element).

## Multi-provider (#103)

Every field `_compute_digest_facts` produces is git state (`git status`,
`git diff --stat`, `git rev-list`, `git rev-parse`) or cockpit-owned
orchestrator state (`PaneState.assign_base_sha`, `pane._session_cwd`,
`WorktreeInfo`). None of it reads pane terminal text or anything specific to
the Claude CLI — identical behavior for a codex/gemini/opencode/kimi/cursor
pane. No provider-specific gap to flag.

## Verification

```
PYTHONPATH=<worktree>/src python -m pytest \
  tests/test_worktree_manager.py tests/test_worktree_assign.py \
  tests/test_notice_facts.py tests/test_done_note_symmetrize.py \
  tests/test_lead_inbox_digest.py tests/test_done_evidence.py \
  tests/test_done_notice_draft_churn.py \
  tests/test_pending_done_notice_visibility.py \
  tests/test_cross_tab_done.py tests/test_orchestrator_done_gate.py \
  tests/test_inbox_report.py tests/test_lead_wait.py \
  tests/test_digest_facts.py tests/test_done_digest_facts_wiring.py \
  tests/test_lifecycle_recovery.py tests/test_daily_digest.py
```
→ all passed (targeted, per the project's test-tier policy — full suite
runs once at the QA batch gate, not mid-task). A broader `-k "worktree or
digest or done or assign_dispatch or lifecycle_recovery or notice_facts"`
sweep across the whole `tests/` tree also passed clean (502 tests) as an
extra safety net given how many call sites `_lead_digest_queue`'s tuple
shape touches.

`ruff check` on every changed file: clean.
`lint-imports`: 25/25 contracts kept (`digest_facts.py` is a new leaf
module — imports only `worktree_manager`, which is itself a pure leaf by
contract; no cycle introduced).

## Known gaps / deliberately out of scope

- Shared-tree `files_touched`/`commits_ahead` can overcount when another
  pane commits to the same shared tree between this pane's assign and done
  — documented in `files_note`, not silently hidden, but not eliminated
  (eliminating it would require per-pane commit attribution the shared-tree
  model doesn't have; the honest fix is isolation via `--isolation
  worktree`, which already has an exact baseline).
- `verdict` is currently always `"done"` in practice (FAILED/blocking
  notices structurally never reach the digest queue, unchanged from #244)
  — kept as an explicit `DigestFacts` field rather than hardcoded in the
  renderer so a future digest-policy change can't silently mislabel a
  verdict the formatter never expected to see.
