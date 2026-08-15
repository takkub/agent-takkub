# #244 — Lead Inbox Digest: computed facts, not agent prose

## Problem

Two related failures from the 2026-08-15 session:

1. **Digest content was agent-authored prose, truncated by prefix.** Lead
   opened the pointed-at `.md` file 8/8 times — the truncated prose never
   sufficed for a decision. Worse: prose is *unreliable*. One report headlined
   "#234 fix" while the actual fix was for #229 (the agent mistyped the issue
   number in its own note).
2. **Worktree merge-proposal near-miss (2x in one night).** `_finalize_worktree`
   announced "N commit พร้อม merge กลับ base" the instant `commit_count > 0`,
   with **no check at all** for uncommitted changes still sitting on top of
   those commits. Lead nearly merged stale work twice, caught only by
   incidentally reading the diff by hand.

## Fix

### 1. Worktree merge-proposal gated on real git state (`worktree_manager.py`)

`WorktreeManager` gained two new read-only fact probes:

- `uncommitted_count(info)` — `git status --porcelain` line count (the
  concrete number the near-miss warning needed; `is_dirty` alone only said
  yes/no).
- `merge_conflicts_with_base(git_root, branch)` — `git merge-base HEAD
  <branch>` then a 3-way `git merge-tree <base> HEAD <branch>` (old 3-tree
  form — read-only, never touches the index/working tree). Compares against
  the git root's **current** HEAD, not the worktree's creation-time
  `base_sha`, since other work may have merged into base while the isolated
  pane was still running. Returns `None` (never a false "clean") when either
  probe itself fails.

`build_merge_proposal()` (pure, unit-tested) now takes `dirty`,
`uncommitted`, `merge_conflicts` and:

- **Never asserts "พร้อม merge" while `dirty=True`.** Shows `⚠ ยังมี N
  ไฟล์ที่ยังไม่ commit ... ยังไม่พร้อมให้ merge` instead, and the numbered
  step list drops the merge command entirely (step 2 becomes "ไป commit ให้
  ครบก่อน"), so the merge command is not even offered as a first — or any —
  action while the worktree is dirty.
- Reports `merge_conflicts` explicitly (conflict / clean / unknown) so a
  clean-but-conflicting branch isn't announced as ready either.
- Includes a computed "ไฟล์ที่แตะ: N ไฟล์ (top-level dirs)" line via the new
  pure `summarize_diffstat()` helper, so Lead sees blast-radius without
  reading the full diffstat block.

`_finalize_worktree` (orchestrator.py) now calls both new probes before
building the proposal and logs `dirty`/`merge_conflicts` on the
`worktree_merge_proposed` event.

**Performance note (explicitly required by the task spec):** this adds 2
more synchronous git subprocess calls to a code path (`_finalize_worktree`)
that already ran 3 (`commit_count`, `diffstat`, `is_dirty`) synchronously on
the Qt main thread. This is a deliberate choice, not an oversight — it stays
in the *same performance class* as the pre-existing calls, and is a
different shape of problem than #229 (which was a per-tick, continuously
polled filesystem walk). `_finalize_worktree` fires **once**, event-driven,
per `done()`/`close()` call on an isolated-worktree pane — not on any poll
loop or timer. The module's own top-of-file comment already documents this
as an intentional design: *"git ops here are all local (no network) and
fast; bound them so a wedged git can never freeze the caller ... the
orchestrator runs create/finalize on the Qt main thread"*. Two more fast
local git reads on a one-shot event does not reproduce #229's failure mode
(continuous per-tick blocking); it was judged not worth the complexity of a
QProcess async chain (`_check_uncommitted_async`-style) for calls that are
already bounded, local, and rare. If a future worktree config makes these
repos large enough that `git merge-tree`/`status` becomes slow, that same
async pattern is the documented escalation path.

### 2. `[role done]` notices carry a cockpit-computed issue ref (`notice_facts.py`, `orchestrator.py`)

New pure module `notice_facts.py::extract_issue_ref(text)` finds the first
`#<number>` token in a string. `Orchestrator.done()` now calls it against
`PaneState.last_assigned_task` — the **original assign spec text Lead
itself sent** at dispatch time — captured before the per-pane state is
popped. This is categorically different from parsing the agent's own
`done()` note: `last_assigned_task` is cockpit state the agent never
touches, so a computed `[ref #244]` badge can't be corrupted by an agent
typo the way the old prose-headline could.

The badge is prepended to the Lead-facing `notice` string (both the clean
`[role done]` path and the `done --fail` handoff), never mixed into
`notice_body` — so shard-aggregate storage and `role_memory` failure-capture
(which read `notice_body`/`note` directly) are untouched. Verified with a
regression test that types the *wrong* issue number in the note body and
asserts the computed ref (correct) still wins in the notice, with the
agent's own (wrong) text still visible alongside it for audit purposes —
not hidden, just not trusted as the report's identity.

### 3. Prose truncation — left as-is, now redundant as the identity source

`_condense_done_note`'s existing first-line-headline + `.md` file pointer
behavior (issue #241's prior work) already matches #244's requirement 2
("keep a short headline if you want one, never duplicate the full prose,
`.md` stays the one full-detail source"). No change was needed there — the
actual problem wasn't the truncation itself, it was that identity
(*"which issue is this about"*) was being read from that same untrusted
prose. That's what item 2 above now fixes structurally: the ref badge is
independent of and unaffected by whatever the truncated headline says.

### 4. FAILED / blocked notices bypassing the digest batch — verified, no code change needed

Traced both paths:

- **`done(..., failed=True)`**: `_build_verify_fail_handoff`'s output is
  matched by `_is_blocking_lead_notice` (`_FAILED_NOTICE_RE`), which
  `_notify_lead` routes through the `front=True` immediate-delivery branch —
  never queued into `_lead_digest_queue`.
- **`takkub send --to lead "blocked: ..."`**: `Orchestrator.send()` writes
  directly to the Lead pane's PTY session (`_safe_session_write` +
  `_delayed_enter_verified`) when `to_role == LEAD.name` — it never calls
  `_notify_lead`/the digest queue at all, so it was already immediate.

Both were already correct; existing test coverage in
`test_lead_inbox_digest.py` covers the FAILED-bypasses-digest case and was
re-run clean. No new mechanism needed for requirement 4.

### 5. Multi-provider (#103)

Every field this pass adds is git state (`git status`, `git merge-base`,
`git merge-tree`, `git diff --stat`) or cockpit-owned orchestrator state
(`PaneState.last_assigned_task`, the `failed` flag `done()` itself
receives). None of it reads pane terminal text or anything specific to the
Claude CLI — it works identically for a codex/gemini/opencode/kimi/cursor
pane. No provider-specific gap to flag for this change.

## Files changed

- `src/agent_takkub/notice_facts.py` — **new**, pure `extract_issue_ref()`.
- `src/agent_takkub/worktree_manager.py` — `uncommitted_count()`,
  `merge_conflicts_with_base()`, `summarize_diffstat()`, `build_merge_proposal()`
  rewritten to gate readiness on dirty/merge-conflict state.
- `src/agent_takkub/orchestrator.py` — `_finalize_worktree()` computes +
  threads the new facts; `done()` computes `issue_ref` from the assign spec
  and prepends it to both the clean-done and FAILED Lead notices.
- Tests: `tests/test_notice_facts.py` (new), `tests/test_worktree_manager.py`,
  `tests/test_worktree_assign.py`, `tests/test_done_note_symmetrize.py`.

## Verification

```
pytest tests/test_worktree_manager.py tests/test_worktree_assign.py \
       tests/test_notice_facts.py tests/test_done_note_symmetrize.py \
       tests/test_lead_inbox_digest.py tests/test_done_evidence.py \
       tests/test_done_notice_draft_churn.py \
       tests/test_pending_done_notice_visibility.py \
       tests/test_cross_tab_done.py tests/test_orchestrator_done_gate.py
```
→ all passed (targeted, not full suite, per the project's test-tier policy —
full suite runs once at the QA batch gate).

`ruff check` on all changed files: clean.
`lint-imports`: 25/25 contracts kept (worktree_manager stayed a leaf module;
notice_facts.py is a new leaf, no forbidden imports introduced).

## Known gaps / deliberately out of scope

- **Files-touched fact for non-worktree (shared-tree) panes.** The field
  table in #244 lists "ไฟล์ที่แตะ" generally; this pass computed it only for
  isolated-worktree panes (where a `base_sha` gives a clean diff range).
  A shared-tree pane has no equivalent "base" to diff against without adding
  new state (e.g. an assign-time commit-SHA snapshot) — left as a follow-up,
  not silently dropped.
- **Digest per-item rendering (`_format_digest_item`) still shows the
  computed-ref-prefixed prose line**, not a fully structured fact table per
  item. The ref badge is the trust-critical fix (identity can no longer be
  wrong); a richer structured layout for the digest bullet itself would be
  a follow-up UI-only pass, not a correctness fix.
