# #248/#247 fix, round 1 — detector core (pty_session.py output-quiescence signal)

## Root cause

`pty_session.py::_feed_and_log` stamped `_last_output_ts` (the signal behind
`seconds_since_output()`, which `lead_inbox.py`'s delivery busy-wait #130/#144
and the stale-marker watchdog #20 both poll) on **every raw PTY byte chunk**,
with no distinction between "the process wrote a byte" and "the pane's
visible content actually changed". Two situations both read as "output just
happened, pane is not quiet":

- A freshly-spawned pane's terminal-init handshake (cursor-position report,
  focus-reporting/mouse-mode/bracketed-paste mode sets — e.g.
  `\x1b[1t\x1b[c\x1b[?1004h\x1b[?9001h\x1b[?2004h`, 31 bytes, zero visible
  glyphs) kept the clock "fresh" even though the CLI hadn't printed anything
  — a codex pane that never got that far read as "output 99s ago (not
  quiet)" instead of "never produced content".
- An animated spinner glyph redrawing next to otherwise-static status text
  (an agy pane wedged on `Signing in...`) advanced the clock on every frame
  even though nothing about the pane's actual state was progressing — the
  busy-wait extension in `lead_inbox.py` rode this all the way to
  `BUSY_WAIT_CEILING_SEC` (1800s) before giving up.

## Fix

`_feed_and_log` now separates two signals, both on `PtySession`:

- **`_last_byte_ts`** (`seconds_since_byte()`) — raw PTY liveness, stamped
  unconditionally on every chunk exactly like before. For "is the process
  still writing to the pty at all" callers.
- **`_last_output_ts`** (`seconds_since_output()`, `last_output_monotonic()`
  — unchanged public names/semantics, just a stricter trigger now) — stamped
  only when a new `_content_fingerprint(lines)` differs from the previous
  chunk's. `lines` is the `_safe_screen_display(self.screen)` result already
  computed for `_classify_ready` inside the same lock acquisition — no extra
  full-screen render.

`_content_fingerprint` (module-level, pure): drops blank rows, then strips
spinner animation before comparing —
braille spinner block (`⠀`–`⣿`, covers claude/agy/codex's various frame sets),
`●○◐◑◒◓` dot/circle frames, an *isolated* ASCII `|/-\` glyph (bounded by
whitespace/edges so `--force`, `a-b`, `path/to/x` are left untouched), and
trailing `.` runs (so `Signing in.`/`..`/`...` all fingerprint the same). A
terminal-init-only screen fingerprints to `""`.

Added **`first_content_ts()`** (`float | None`) — the monotonic timestamp of
the first fingerprint change, `None` while the CLI has produced no visible
content yet. Public accessor on `PtySession` for orchestrator/CLI use.

### Status surfacing (item 3)

`Orchestrator._pane_display_state(pane)` (new, `orchestrator.py`) refines
`pane.state == "active"` (set once at spawn by `AgentPane.attach_session`,
previously identical whether the CLI had printed a single byte or was fully
idle at its ready prompt) into three labels using the new signal:

- `spawning` — `pane.session is None`, or `first_content_ts() is None`
- `active` — has content, not at `is_at_ready_prompt_cached()`
- `ready` — has content, at ready prompt

Every other `pane.state` value (`working`, `done`, `empty`, and the existing
pending-notice/queued synthetic labels) passes through unchanged — this is
additive to `list_status()`/`list_status_detailed()` only, `pane.state`
itself is untouched, so every `pane.state == "working"` etc. check elsewhere
(status_header.py, lead_wait.py, spawn_engine.py, …) is unaffected. Wired
into both `list_status()` and the `"state"` field of
`list_status_detailed()`; `cli_server.py`'s `list`/`status` handlers and
`cli.py`'s printers already pass the state string through verbatim (no
hardcoded `"active"` match anywhere in either file), so no changes were
needed there. `status_header.py`'s one `pane.state` read
(`_update_status`'s active/working pane counter) reads `pane.state` directly,
not the derived dict, so it's also unaffected — left untouched.

Fails open: any exception from a stand-in `session` object without the new
accessors (a loose test double elsewhere in the suite) falls back to the old
`"active"` label rather than raising.

## Files changed

- `src/agent_takkub/pty_session.py` — `_content_fingerprint` +
  `_SPINNER_GLYPH_RE`/`_TRAILING_DOTS_RE` (module-level, near
  `_safe_screen_display`); `PtySession.__init__` new fields `_last_byte_ts`,
  `_last_content_fingerprint`, `_first_content_ts`; `_feed_and_log` reworked
  per above; new accessors `seconds_since_byte()`, `first_content_ts()`.
- `src/agent_takkub/orchestrator.py` — new `_pane_display_state`; wired into
  `list_status()` and `list_status_detailed()`.
- `tests/test_output_content_fingerprint.py` (new) — `_content_fingerprint`
  unit coverage (blank screen, spinner-frame equivalence, ellipsis growth,
  isolated-glyph vs real-hyphen/slash/word discrimination) + `PtySession`
  integration coverage (init-escape-only produces no output signal but does
  advance the raw-byte clock; spinner redraw doesn't advance
  `_last_output_ts` across multiple frames while `first_content_ts()` stays
  pinned to the first real content; a genuine content change after a spinner
  does advance it; `is_at_ready_prompt()`/`_cached` classification unchanged).
- `tests/test_pane_display_state.py` (new) — `_pane_display_state` unit
  coverage for all three new labels + pass-through for non-`"active"` states
  + fail-open on a bare stand-in session.
- `tests/test_orchestrator_stall.py` — `_FakeOrch` (a thin
  bound-real-methods stub used by `TestListStatusDetailed`/
  `TestPaneStatusReport`) now also binds `_pane_display_state =
  Orchestrator._pane_display_state`, since `list_status_detailed` calls it
  unconditionally; verified none of that file's existing assertions checked
  for the literal `"active"` state string (they only assert
  `stall_minutes`/`blocked_reason`), so no other change was needed there.

## Cross-provider / cross-platform

Spinner-glyph set covers braille (claude/agy/codex's common cli-spinners
frame families), dot/circle, and generic ASCII `|/-\` — not tied to one
provider's exact wording. `_content_fingerprint`/`_feed_and_log` operate
purely on the already-cross-platform pyte screen buffer (Windows ConPTY /
macOS `_pty_backend` both feed through the same `_feed_and_log`), no
platform-specific branching added.

## Scope note (round 2)

This round only touches the detector core + its direct
`list_status`/`list_status_detailed` consumers. Not covered here (left for
round 2 per the task split): any UI-facing chip/label rendering of the new
`spawning`/`ready` states beyond the raw string passthrough already verified
in `cli.py`/`cli_server.py`.

## Verification run (this worktree)

```
.venv\Scripts\python.exe -m pytest tests/test_output_content_fingerprint.py tests/test_pane_display_state.py -q
# 17 passed

.venv\Scripts\python.exe -m pytest tests/test_pty_session_threading.py tests/test_pty_ready_prompt.py \
    tests/test_stale_marker_detector.py tests/test_orchestrator_stall.py \
    tests/test_pending_done_notice_visibility.py tests/test_resource_queue_visibility.py -q
# all passed

.venv\Scripts\python.exe -m pytest tests/test_lifecycle_recovery.py tests/test_update_splash_recovery.py \
    tests/test_stuck_recover.py tests/test_delivery_unconfirmed.py tests/test_delivery_supersede.py \
    tests/test_delivery_busy_wait_notice.py tests/test_delivery_blocked_prompt.py \
    tests/test_throughput_watchdog.py tests/test_fix_round2_edge_cases.py tests/test_pipeline_executor.py \
    tests/test_fan_out_delivery_race.py tests/test_headless_pane.py tests/test_pane_transcript.py \
    tests/test_agent_pane_auto_clear.py -q
# 267 passed

.venv\Scripts\python.exe -m pytest tests/test_cli.py tests/test_cli_server.py tests/test_cli_status.py \
    tests/test_lead_write_guard.py -q
# all passed

.venv\Scripts\python.exe -m ruff check src/agent_takkub/pty_session.py src/agent_takkub/orchestrator.py \
    tests/test_output_content_fingerprint.py tests/test_pane_display_state.py tests/test_orchestrator_stall.py
.venv\Scripts\python.exe -m ruff format --check <same files>
.venv\Scripts\lint-imports.exe
# all clean — 25/25 import-linter contracts kept
```

Full suite deferred to qa's batch gate per project convention (targeted
tests only mid-flight).
