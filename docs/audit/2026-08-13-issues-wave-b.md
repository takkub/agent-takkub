# Issues wave B — #169, #179, #181

Backend pane (`wt/backend-1786615563`), base `release/2026-08-13`. Targeted
tests only, no full-suite gate (that's qa's job at batch verify).

## #169 (med) — teammate pane can run host-wide destructive kill commands

**Fix:** new `host_destructive` rule in `pane_guard.py`, mirroring the
existing `browser_driver` two-layer pattern (issue explicitly asked for
this: "ต้องกันที่ pane_guard และ role file").

- **Hook layer** (`pane_guard.py`, claude panes only): blocks
  `taskkill ... /IM`, `pkill`, `killall`, PowerShell `Stop-Process -Name` —
  all anchored to actual invocation position (start of command / after a
  separator / after `sudo`) so *mentioning* the command in `echo`/`grep`
  stays allowed. Applies to **every** guarded role, no allowlist (unlike
  `browser_driver` — no role legitimately needs a host-wide kill-by-name).
  PID-targeted equivalents (`taskkill /PID`, `Stop-Process -Id`, plain
  `kill <pid>`) stay allowed.
- **Prose layer** (all 16 `.claude/agents/*.md`): new "ห้าม kill process
  ด้วยชื่อ (บังคับ, #169)" section, identical across every role file (no
  browser-role-style permission variant — this rule has none), naming the
  exact commands + the PID alternative + the 2026-07-08 incident. This is
  the *only* enforcement non-claude panes (codex/gemini-agy/opencode/
  kimi/cursor) ever see (#103).

**Tests:** `tests/test_pane_guard.py::TestHostDestructiveDenied` (denial +
no-allowlist + PID/reading false-positive checks — caught and fixed a real
false positive during development: `echo 'use taskkill /PID not /IM'` was
tripping the rule before the invocation-position anchor was added),
`tests/test_cli_guard.py` (end-to-end through the actual hook wiring),
`tests/test_agent_role_files_have_host_destructive_guard.py` (pins both
layers in sync, mirrors `test_agent_role_files_have_browser_guard.py`).

Out of scope (not built): issue's proposal (2), a `takkub kill-my-procs`
helper with per-pane child-PID tracking. That's new CLI/PID-tracking
infrastructure, not a guard-rule fix — flagging as a possible follow-up, not
built here per targeted-scope instruction.

## #179 (high) — AttributeError 'NoneType' object has no attribute 'isalive' @ pty_session.py:610

Debugged via debug-mantra (reproduce → fail path → falsify → cross-reference).

**Root cause (proven by reproduction, not guessed):** `_ReaderThread.run()`
re-reads `self._proc` after catching `EOFError`/`Exception` from
`self._proc.read(4096)`. `PtySession._teardown_resources`'s background
`_teardown()` joins the reader with a **bounded** `.wait(2000)` and then
unconditionally sets `thread_obj._proc = None` regardless of whether that
join actually succeeded. If the reader thread is still mid-iteration past
the 2s timeout — just past `proc.read()` raising `EOFError`, about to check
`isalive()` — the concurrent null lands in between and the re-read
dereferences `None`.

**Repro:** `tests/test_pty_session_reader_proc_race.py` — a fake proc whose
`read()` nulls `reader._proc` as a side effect (standing in for the
concurrent teardown thread), deterministic, no timing dependency. Reproduced
the *exact* nested traceback (EOFError → AttributeError at the `isalive()`
line) before the fix; passes after.

**Fix:** capture `proc = self._proc` once per loop iteration and use that
local reference for the rest of the iteration (read + both `isalive()`
checks), instead of re-reading `self._proc` after the exception. A
concurrent null of `self._proc` can no longer be observed mid-iteration.
Minimal, one method, no behavior change on the non-racing path.

**Tests:** new repro test (above) + reran
`tests/test_pty_session_threading.py` and `tests/test_pty_session_spawn_timeout.py`
to confirm no regression — all pass.

## #181 (high) — auto-resume gives up silently, Lead must verify manually

**Finding: this is a duplicate of #158 (verified, not guessed).** Fetched
both issues from GitHub — identical title, identical body, identical
incident (`local://issue/15` from the `lottery` project, migrated to GitHub
twice). #158 is **already closed**, fixed by commit `be0657e` ("fix(#158):
give-up status dump + on-disk progress marker"), committed 2026-08-13
11:18 — already present on `release/2026-08-13` / this branch's base, ~1.5h
before this session started.

Compared #181's 4 numbered proposals against the merged fix in
`limit_autoresume.py`:

| Proposal | Status |
|---|---|
| (1) dump task/cwd-git-status/output-tail on give-up | ✅ done — `_give_up_auto_resume` sends task preview, pane output tail, and triggers `_check_uncommitted_async` (git status follow-up) |
| (2) hint "งานอาจเสร็จแล้ว — verify ก่อน discard" | ✅ done — literal hint text in the give-up notice |
| (3) on-disk progress marker surviving a pane crash | ✅ done — `_write_progress_marker` writes `runtime/progress/<project>/<role>.json` on every park/give-up/wake transition, written by the orchestrator itself (not the agent), so it survives a pane that crashes rather than cleanly waking |
| (4) auto-failover to another provider on quota exhaustion | ❌ not built — proposal (4) was "พิจารณา" (consider), the softest of the four asks, and is a materially bigger feature (cross-provider quota/session handoff) than the other three |

`tests/test_limit_autoresume.py::TestGiveUpAutoResume`,
`TestWriteProgressMarker`, `TestProgressMarkerPath`, `TestPaneCwd`,
`TestPaneOutputTail` already cover all of (1)–(3) thoroughly (hint text,
task-preview truncation, marker fields, overwrite-on-transition, git-status
follow-up gated correctly by cwd availability, disk-write-failure
graceful-None). Reran the full file — all pass. Confirmed `_session_cwd` is
a real `AgentPane` property (not just a test-mock assumption) so the git
diagnostic isn't fictional.

**No new code written for #181** — writing near-duplicate tests against
already-well-tested code would just be padding, not coverage. Recommend
Lead close #181 as duplicate of #158 (`gh issue close 181 --comment
"duplicate of #158, already fixed on release/2026-08-13 by be0657e"`).

## Files changed

- `src/agent_takkub/pane_guard.py` — `host_destructive` rule
- `src/agent_takkub/pty_session.py` — `_ReaderThread.run()` race fix
- `.claude/agents/*.md` (16 files) — host-destructive prose section
- `tests/test_pane_guard.py`, `tests/test_cli_guard.py`,
  `tests/test_agent_role_files_have_host_destructive_guard.py`,
  `tests/test_pty_session_reader_proc_race.py` — new/extended tests
