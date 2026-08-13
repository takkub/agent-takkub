# Wave C — CLI issues #173, #174, #175

Branch: `wt/backend-2-1786615563` (rebased onto `release/2026-08-13`, base `df1f65f`)

All three were migrated from the local-only tracker (`local://issue/7,8,9`,
created 2026-08-04T08:31–08:37) into GitHub as #173–#175 on 2026-08-13, as
part of the same 17-issue backlog migration (#167–#183) that motivated #174
itself. The migration copied the *local* records verbatim without checking
whether same-day follow-up work had already fixed the underlying bug —
which turned out to be true for two of the three.

## #173 (low) — `takkub status` leaks raw ANSI escapes — already fixed, duplicate of #145

**Status: no code change needed. Added a regression-lock test.**

The local issue was filed 2026-08-04T08:31, describing the exact symptom
(`[?25h[?25l]`, `[3GPondering…]` leaking into `transcript_tail`). The same
day at 16:33, commit `8ad72e1f` ("fix(#145,#127): full ANSI/OSC strip in
status tail...") replaced the old CSI-finals allowlist regex (which missed
private-mode toggles `\x1b[?25h/l` and other CSI finals like `\x1b[3G`) with
a full ECMA-48 CSI + OSC stripper in `orchestrator.py::_ANSI`, and shipped
regression tests in `tests/test_orchestrator_stall.py`. The local-tracker
entry was never reconciled against that fix before migration, so it
resurfaced as #173 nine days later.

Verified on this branch: `_ANSI.sub()` strips both sequences from the
issue's exact repro string. Added
`test_transcript_tail_strips_issue_173_repro` in
`tests/test_orchestrator_stall.py` reproducing the literal report text
(`\x1b[?25h\x1b[?25l\x1b[3GPondering…`) as an explicit #173 regression lock,
alongside the existing #145 test suite (6 tests, all green).

**Recommend:** close #173 as duplicate of #145.

## #174 (med) — `takkub issue list` returns "no issues" despite local backlog — real gap, fixed

**Status: code fix landed.**

`#142` (fixed 2026-08-04 16:41, commit `0b354ca`) made `list_issues()` /
`close_issue()` / `show_issue()` default to the same `cockpit_bug=True`
routing `new_issue()` uses, so list/new stopped silently querying different
repos/stores. That fix left one gap: once `gh` recovers from an outage
*after* `new_issue()` had already fallen back to writing
`.takkub_issues.json` locally, `list_issues()` takes the successful GitHub
branch and returns straight away — it never looks at the local fallback
file again. Any backlog written while `gh` was down becomes permanently
invisible, reproducing as "(no issues)" while the file genuinely holds open
records (exactly the 7-issue scenario in the report).

**Fix** (`src/agent_takkub/issues.py`):
- Extracted the local-store filter logic (status/severity/role/noticed_in)
  into a shared `_filter_local_issues()` helper, used by both the pure-local
  fallback path and the new merge path.
- After a successful GitHub `issue list` query, also read the local
  fallback store (via the existing `_local_store_cwd` redirect) and merge
  in any *matching* (same filters) unreconciled entries instead of dropping
  them. Prints a one-line stderr warning naming the count and file path so
  a user sees "N unreconciled local issue(s)" instead of silent data loss.
- Local-file read errors during the merge check are swallowed (don't break
  an otherwise-successful GitHub listing).

**Tests added** (`tests/test_issues.py`):
- `test_list_issues_merges_unreconciled_local_backlog` — gh succeeds with an
  empty result, local store has 1 open record → record appears in output +
  warning printed.
- `test_list_issues_no_backlog_no_warning` — no local file → no spurious
  warning.
- `test_list_issues_backlog_respects_filters` — a closed local issue must
  not leak into a `--open` listing.

64/64 tests in `tests/test_issues.py` pass.

## #175 (med) — `takkub assign` accepts invalid cwd, fails async later — already fixed, duplicate of #143

**Status: no code change needed. Strengthened existing test's doc trail.**

Local issue filed 2026-08-04T08:37. Same day at 16:41, commit `0b354ca`
("fix(cli): #142 ...; #143 assign validates cwd async") added a synchronous
`cwd_validation_error()` check in `cli_server.py`'s `_dispatch()`, run
*before* the "task queued" ack is sent — an invalid `--cwd` now gets
rejected in the same round-trip instead of surfacing as a `[spawn-failed]`
notice ~2 minutes later. The same fix also made `_cwd_within_project()`
accept the project's own root (common parent of its configured paths) as a
legal cwd for *any* role, not just Lead — covering both (a) and (b) from
the #175 report.

Verified via existing tests in `tests/test_cli_server.py`:
`test_assign_rejects_cwd_outside_project_before_ack` (rejection is
synchronous, `orch.assign_calls == []`) and
`test_assign_accepts_project_root_cwd` (role=`devops`, cwd=project root →
synchronous `ok: True`). Updated the latter's docstring to cross-reference
#175 explicitly as the duplicate this also closes.

**Recommend:** close #175 as duplicate of #143.

## Test summary

```
tests/test_issues.py tests/test_orchestrator_stall.py tests/test_cli_server.py
114 passed in 0.86s
```

`ruff check` clean on all touched files.

Unrelated, pre-existing: `tests/test_project_scoping.py::TestRenderLeadContext`
(2 tests) fails in this worktree with `FileNotFoundError` on
`runtime/lead-context.md` — this worktree has no `runtime/` directory
materialized yet. Not caused by this change (files untouched); not
investigated further as out of scope for #173/#174/#175.

## Recommendation for Lead

- Merge #174's fix (real bug, real regression risk if left unpatched again).
- Close #173 and #175 on GitHub as duplicates of #145 and #143 respectively
  — both were already fixed same-day in the local tracker's history, just
  never reconciled before the batch migration to GitHub. Worth a beat on
  the migration script/process to check "was this already fixed after it
  was filed but before migration?" for the remaining #167–#183 batch.
