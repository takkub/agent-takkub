# Wave E — issues #183, #180 (2026-08-13)

Assigned to devops to "เก็บ 2 issue สุดท้าย" of the bug batch. Both turned out
to be **duplicates of issues already fixed and merged into `release/2026-08-13`**
before this branch was rebased onto it. No new code changes were needed —
verified the fixes are present, still correct, and still pass their targeted
tests.

## #183 (high) — shard fan-out overwrites report files

Same bug as **#160**, fixed in `ee045e8` ("fix(#160): stop parallel QA shards
from overwriting each other's report file"), already an ancestor of this
branch's HEAD after `git rebase origin/release/2026-08-13`.

**What the fix does** (`src/agent_takkub/orchestrator.py`,
`src/agent_takkub/pipeline_executor.py`):
- `_assign_dispatch` wraps every real shard's task (`shard_total > 0`, not
  the planner pane) with `_wrap_shard_task`, which forces a `.shard{n}`
  filename suffix instead of a shared output path, and tells the shard to
  report the real path it wrote back in `takkub done`.
- Safety net: `_inject_shard_fanout_handoff` runs once all shards in a group
  report done/failed and cross-checks done-notes for a path mentioned by 2+
  shards, flagging it to Lead before the consolidated report is trusted —
  covers the case a shard ignores the suffix instruction (issue's proposal
  item 4).
- This applies to both the plain `--shards N` path and the
  `--plan --shards N` planner-fanout path (`_fire_qa_plan_fanout` in
  `pipeline_executor.py` calls the same `assign()` → `_assign_dispatch` →
  `_wrap_shard_task` chain — verified by reading the call chain, not just
  assuming from the function name).

Full original writeup: `docs/audit/2026-08-13-160-shard-report-path-collision.md`.

**Verified this session:**
- `git merge-base --is-ancestor ee045e8 HEAD` → yes, already in branch.
- Targeted tests re-run on this branch after rebase, all pass:
  `test_done_evidence.py`, `test_done_note_symmetrize.py`,
  `test_orchestrator_shard.py`.

## #180 (high) — role prompts don't warn about image token cost

Same bug as **#157**, fixed in `4288623` ("fix(#157): warn role prompts
about image token cost"), already an ancestor of this branch's HEAD.

**What the fix does** (`src/agent_takkub/lead_context.py`): extended the
existing `BIG_FILE_GUARD` constant (already injected into both the Lead
spawn prompt and every teammate role-md appendix via `spawn_engine.py`)
with a `🖼️ รูปภาพ` subsection — threshold (>300KB or long side >1500px),
and behavioral guidance since offset/limit doesn't apply to images: reuse
an earlier hop's written note instead of re-`Read`ing, ask for a
cropped/downscaled version, don't re-open "just to be sure" across
iteration rounds. Matching anti-pattern bullets were added to `CLAUDE.md`
and `docs/lead/patterns.md` (critic pipeline hop 2) for the Lead-side
fan-out decision, not just the read-time reflex.

This is in fact the exact guardrail text present in this pane's own
system-prompt role-file appendix right now (`📦 ไฟล์ยักษ์ / รูปภาพใหญ่`
section), which is itself live confirmation the fix is deployed and
reaching real panes.

Full original writeup: `docs/audit/2026-08-13-157-image-token-guard.md`.

**Verified this session:**
- `git merge-base --is-ancestor 4288623 HEAD` → yes, already in branch.
- Targeted tests re-run on this branch after rebase, all pass:
  `test_lead_context_compact.py`, `test_session_brief.py`,
  `test_lead_write_guard.py`, `test_provider_substitution_note.py`,
  `test_orchestrator_reexports.py`, `test_skill_policy.py`.

## Outcome

No code changes made — both issues are already resolved on
`release/2026-08-13`. Recommend closing #183 and #180 as duplicates of
#160 and #157 respectively (same handling as the wave A duplicates
#170/#171/#172/#176).
