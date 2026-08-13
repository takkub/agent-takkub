# #158 (partial) — auto-resume give-up status dump + progress marker

Scope: only the status-dump/hint half of #158 (auto-failover to another
role/provider is NOT part of this change — out of scope by original task spec).

## What changed

Pane was closed mid-work in a prior spawn with 3 files already modified
uncommitted (`auto_resume.py`, `limit_autoresume.py`, `tests/conftest.py`).
Read the existing diff first, confirmed the implementation was functionally
complete, then added the missing test coverage (none existed for the new
code) and verified.

`src/agent_takkub/limit_autoresume.py::_give_up_auto_resume` — when
auto-resume permanently stops nudging a pane (round cap hit, or re-limited
within the post-wake grace window), it now:

1. Reads the pane's cwd (`_pane_cwd`) and last visible screen output
   (`_pane_output_tail`, last `auto_resume.GIVE_UP_TAIL_LINES` non-blank
   lines).
2. Writes a JSON snapshot to `RUNTIME_DIR/progress/<project>/<role>.json`
   (`_write_progress_marker`) — task text, task file path, cwd, output tail,
   park-round count, status (`parked`/`gave_up`/`resumed`), timestamp.
   Written on every park→wake→give-up transition, not just give-up, so a
   pane that crashes mid-park still leaves a recoverable trail.
3. Sends the Lead a message that includes an explicit hint —
   "งานอาจเสร็จสมบูรณ์แล้วแต่ยังไม่ได้รายงานผ่าน `takkub done` ...
   ตรวจสอบสถานะจริงก่อน discard/reassign" — plus a task preview (first
   `auto_resume.GIVE_UP_TASK_PREVIEW_CHARS` chars), the output tail, and the
   marker file path.
4. Kicks off the existing `_check_uncommitted_async` (same git-status-in-cwd
   check `done()` already runs) as a non-blocking follow-up, so a dirty
   working tree gets its own Lead notice instead of silent discard.

`RUNTIME_DIR/progress/<project>/<role>.json` was chosen over the spec's
literal `.takkub/progress/<role>.json` — `RUNTIME_DIR` is the existing
per-install/per-checkout runtime-state root everything else in this module
already writes under (events log, session/brief files); reusing it keeps one
storage convention instead of introducing a second hardcoded path.

## Tests added

`tests/test_limit_autoresume.py` had zero coverage for any of the above
(all pre-existing tests target the park/wake state machine, not #158). Added:

- `TestPaneCwd` (4) — None pane, missing attr, empty string, real value.
- `TestPaneOutputTail` (5) — None pane/session, `display_lines()` raising,
  blank-line filtering, default tail length matches
  `auto_resume.GIVE_UP_TAIL_LINES`.
- `TestProgressMarkerPath` (1) — dir creation + naming.
- `TestWriteProgressMarker` (3) — field-by-field content check, overwrite on
  repeated calls (park→wake→give-up all target the same file), OSError on
  write returns `None` instead of raising.
- `TestGiveUpAutoResume` (5) — Lead message contains the verify-before-discard
  hint + task preview + output tail + marker path; long task text truncates
  at `GIVE_UP_TASK_PREVIEW_CHARS`; marker file actually lands on disk;
  `_check_uncommitted_async` fires only when cwd is known.

All 57 tests in `tests/test_limit_autoresume.py` + `tests/test_auto_resume.py`
pass. `ruff check` clean on all 4 touched files.

## Note on test invocation

The shared root `.venv` (`C:\Users\monch\WebstormProjects\agent-takkub\.venv`)
is editable-installed against the root checkout's `src/`, not this worktree's.
Running pytest from the worktree without `PYTHONPATH=src` silently imports
the ROOT repo's `agent_takkub`, not this worktree's edits — ran with
`PYTHONPATH=src` from the worktree root to test the actual changes here.
