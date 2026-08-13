# #186 — gemini-family (agy) folder-trust prompt hangs a worktree spawn

**Status:** fixed (proven, targeted tests). **Severity:** med (silent task loss on
Multi-mode worktree fan-out). **Files:** `pty_session.py`, `spawn_engine.py`,
`lead_inbox.py`.

## Root cause (proven, not assumed)

Issue #186 opened with the root cause tagged **unproven** — it only had a
hypothesis ("worktree = new path the CLI never trusted → first-run trust
gate"). That hypothesis was investigated and **not** what actually broke
delivery. The real cause is a plain wording mismatch, confirmed by direct
string inspection against the live-captured screen text pasted into the
issue:

```python
# pty_session.py, BEFORE:
if "trust this folder" in text and "enter to confirm" in text:
    return True
```

The issue's own incident log captured agy's actual modal text verbatim:

```
> Yes, I trust this folder
  No, exit
  up/down Navigate . enter Confirm
```

Lower-cased, that screen contains `"trust this folder"` but **not**
`"enter to confirm"` — agy's own confirm hint omits the word "to"
(`"enter Confirm"`, not `"Enter to confirm"`). Verified directly:

```python
>>> 'enter to confirm' in "up/down navigate . enter confirm"
False
```

So `is_at_trust_prompt()` — the single detector both `_auto_trust()` (the
poller that auto-presses Enter on this exact modal) and the stale-marker
detector rely on — silently never recognised agy's modal. `_auto_trust`'s
watcher ran its full 30s window seeing `is_at_trust_prompt() == False` the
entire time and gave up having never pressed Enter. Meanwhile
`is_at_ready_prompt()` correctly stayed `False` throughout (the shared,
provider-agnostic `READY_HARD_BLOCKERS` table — built by concatenating every
provider spec's `ready_hard_blockers`, so claude's `"trust this folder"`
entry applies globally — already caught the modal), which is why the pane
read as `active` but never `ready`, matching the reported symptom exactly.

The worktree-path theory from the issue was not needed to explain the
incident and no evidence for it was found (no per-path trust state file was
located for agy in the time available) — the wording bug alone fully
explains the hang. It may still be a contributing factor in principle, but
it is not required and is not fixed here (see "not done" below).

A second, independently-provable gap compounded the first: `_auto_trust`'s
poll window was a hardcoded 30s regardless of provider, while agy's own
documented cold-boot allowance (`gemini_spec.ready_wait_ms`) is up to 90s.
Under the 4-way parallel worktree fan-out that triggered the incident, a
trust modal rendering any time after ~30s into a slow boot would have
outlived the watcher even with the wording fixed. This is a pure code-reading
proof (no live repro needed): the watcher's own `max_ms = 30_000` constant
vs. the provider's own `ready_wait_ms = 90_000` constant, both already in
the codebase.

## What was fixed

1. **`is_at_trust_prompt()` wording (`pty_session.py`)** — the confirm-hint
   match is now `enter\s+(?:to\s+)?confirm` (regex, case-insensitive)
   instead of the exact substring `"enter to confirm"`, so it matches both
   claude/codex's `"Enter to confirm"` and agy's `"enter Confirm"`. This is
   the actual fix for the reported incident — `_auto_trust` and the
   stale-marker detector both consume this one detector, so both are fixed
   by the same change with no provider-specific branching.

2. **`_auto_trust`'s poll window now provider-aware (`spawn_engine.py`)** —
   `_auto_trust(role_name, project=None, max_ms=30_000)` gained a `max_ms`
   parameter; `_launch_session` threads it through as `auto_trust_wait_ms`
   (default unchanged at 30s — claude's inline call site, and any caller
   that doesn't override, keeps the historical value). The spec-driven
   codex/gemini spawn branch now passes
   `auto_trust_wait_ms=max(30_000, spec.ready_wait_ms)`, so a cold-boot
   provider's watcher runs at least as long as that provider is itself
   allowed to take to boot — generic via the existing `ProviderSpec.
   ready_wait_ms` field, no hardcoded provider name.

3. **Item 3 — distinguish "blocked on a prompt" from "busy" and notify
   immediately (`lead_inbox.py`)** — before this, a pane sitting on ANY
   recognised interactive prompt (trust modal or generic shell y/N) that
   happened to periodically redraw (a spinner, agy's boot-phase "verifying
   your account" text) advanced `seconds_since_output()` exactly like
   genuine work output. `_send_when_ready`'s busy/stall split (#130/#144)
   couldn't tell the two apart, so it silently fell into the ordinary
   busy-wait extension and the Lead heard nothing concrete until either it
   cleared on its own or the `BUSY_WAIT_CEILING_SEC` absolute ceiling fired
   — up to 30 minutes later by default (this is what the issue's "รอจนครบ
   1800s" describes: not that the incident literally ran 30 minutes, but
   that this is the warning text the mechanism would have produced).

   `_prompt_block_reason(session)` (new, checks `is_at_trust_prompt()` then
   `is_blocked_on_tty_prompt()`) now runs on every delivery poll,
   independent of `elapsed`/`max_wait_ms`, and `_warn_lead_delivery_blocked_
   prompt()` fires a distinct `[delivery-blocked-prompt]` Lead notice the
   *first* poll that recognises it — not gated behind the hard timeout,
   the stall threshold, or the busy ceiling. One-shot per delivery (a
   `prompt_blocked_warned` flag), so a pane blocked the whole way through
   still only produces a single notice.

4. **Item 4 — never blind-paste into an active modal (`lead_inbox.py`)** —
   the existing best-effort blind-paste at the hard timeout (`#26`/`#131`)
   could previously fire while the pane was still visibly on a trust/tty
   prompt. Writing the task payload there lands as keystrokes on the modal
   itself (navigation / accept-default noise), not the composer — the task
   is lost outright, which is *worse* than the "merely unconfirmed" case
   that branch was designed for (that one at least lands in the composer,
   just unverified). `_deliver()` now checks `_prompt_block_reason()`
   immediately before committing to a blind paste; if still blocked, it
   defers for a further bounded grace (`_PROMPT_BLOCK_DEFER_CEILING_MS`,
   30s) instead of pasting, giving `_auto_trust` (now fixed by #1+#2, or a
   human) a real chance to clear it first. Only after that grace is also
   exhausted does it fall through to the original best-effort paste +
   warning — same last-resort contract as before, just no longer racing a
   guaranteed-lost paste against the responder that's supposed to clear the
   block.

## Multi-provider generality (#103)

Nothing here is gemini/agy-specific in the implementation:

- `is_at_trust_prompt()` is shared by every provider (claude, codex, agy);
  the regex loosening helps all three, not just agy.
- `_auto_trust`'s `max_ms` is a plain parameter; the spec-driven spawn
  branch reads it from `ProviderSpec.ready_wait_ms`, an existing per-provider
  field — a future slow-booting provider (opencode/kimi/cursor, once
  calibrated) gets the correct window automatically by setting that one
  field, no new branch needed.
- `_prompt_block_reason()` / the blocked-prompt notice / the blind-paste
  guard operate purely on the two existing generic `PtySession` detectors
  (`is_at_trust_prompt()`, `is_blocked_on_tty_prompt()`) — no provider
  string appears anywhere in `lead_inbox.py`'s new code.

## What was **not** done, and why

- **Pre-trust the worktree path in the CLI's own config before spawn**
  (issue's suggestion #2/part of #1). No agy trust-state config file was
  located in the time available to prove its format, and per the task's own
  instruction ("ห้ามเขียนไฟล์ config ของ CLI อื่นแบบเดา") this was not
  attempted blind. Given the root cause turned out to be the wording bug
  (not a genuine "never trusted" first-run gate), the `_auto_trust`
  auto-answer path already now handles this correctly at runtime without
  needing to pre-seed any external config — so this alternative is lower
  priority than it looked before the investigation. If a real
  never-answered first-run gate is found for some other provider later,
  this is the remaining option to revisit.
- **The worktree-new-path hypothesis itself** was not proven or fixed
  because it was not needed to explain the incident (see root cause above).
  Left unconfirmed, not ruled out.

## Cases still not covered

- A provider whose trust/onboarding modal wording is recognised by *neither*
  `is_at_trust_prompt()` nor `is_blocked_on_tty_prompt()` still falls back to
  the old behavior: no early notice, and (after fix #4's bounded 30s grace)
  an eventual best-effort blind paste — same residual risk as any
  unrecognised upstream prompt reword (the existing stale-marker detector's
  `ready_marker_possibly_stale` log is the general-purpose net for that
  class of gap, not something new here).
- `_deliver()`'s blind-paste guard only engages on the `unconfirmed=True`
  paths (hard-timeout / busy-ceiling / stall). The normal ready-prompt
  delivery path was left unguarded on purpose — `is_at_ready_prompt()`
  already excludes a trust-blocked screen globally via the shared
  `READY_HARD_BLOCKERS` table, so that path structurally cannot fire while
  blocked; adding a redundant check there would be dead code.

## Tests

- `tests/test_pty_ready_prompt.py::TestIsAtTrustPrompt` — 5 tests, including
  the exact live-captured agy screen from the issue. Verified red against
  the pre-fix regex (`enter to confirm` in "...enter confirm" → `False`)
  before applying the fix.
- `tests/test_auto_trust_wait_window.py` — 4 tests proving `_auto_trust`'s
  `max_ms` actually changes how many polls run, and the exact #186 timing
  scenario (modal appears at ~22.5s: missed by the old 30s/60-tick default,
  caught by a 90s/180-tick window).
- `tests/test_launch_session.py` — 2 new tests proving `_launch_session`
  forwards `auto_trust_wait_ms` to `_auto_trust(max_ms=...)`, and that it
  still defaults to 30s when unspecified (claude's inline call site is
  unaffected).
- `tests/test_delivery_blocked_prompt.py` — 10 tests: early one-shot Lead
  notice (trust and generic-tty variants), no false notice for a pane that
  never blocks, the direct `_warn_lead_delivery_blocked_prompt` unit tests,
  and the `_deliver()` blind-paste-defer guard (both "still blocked at the
  ceiling → falls through to best-effort paste" and "clears in time →
  delivers normally, no blind paste at all").
- All new tests confirmed **red** against the pre-fix source via
  `git stash` (source-only, tests kept) before the fix was applied, per the
  task's requirement.

Ran targeted, not the full suite (per project convention — full suite is
qa's batch-gate job): the above files plus every other test file found via
`grep _send_when_ready` / `grep assign(` across `tests/` (to check for
blast radius from the new per-poll `_prompt_block_reason()` check) — all
green except two pre-existing, unrelated failures:

- `test_project_scoping.py::TestRenderLeadContext` (2 tests) — this worktree
  checkout has no `runtime/` directory, so `_render_lead_context` fails to
  write its output file. Confirmed pre-existing/environmental, not caused by
  this change (untouched code path).
- `test_lifecycle_recovery.py::TestPtySessionTerminateJoinsThreads::
  test_terminate_calls_quit_and_wait_on_reader` — passes in isolation and
  as part of its own file; only fails as part of one particular large
  combined `pytest` invocation alongside several other files. Looks like
  cross-file QThread/mock state bleed, unrelated to any file this fix
  touches (`PtySession.terminate()` was not modified).

Two pre-existing tests (`test_delivery_unconfirmed.py::_live_session`,
`test_delivery_busy_wait_notice.py::_live_session`) had their `MagicMock`
session helper updated to pin `is_at_trust_prompt()` / `is_blocked_on_tty_
prompt()` to their real non-blocked defaults — an unconfigured `MagicMock()`
call returns a truthy `MagicMock`, which the new per-poll check would
otherwise misread as "blocked" on every test in those two files (caught via
the red/green stash check above, not shipped broken).

`ruff check` + `ruff format` clean on every touched file.
