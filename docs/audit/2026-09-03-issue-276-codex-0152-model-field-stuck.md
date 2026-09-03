# #276 (round 3) — codex-cli 0.152.0's `model:` banner field sticks at
"loading" forever, tripping the boot-stall ceiling on a pane that is
genuinely idle at its ready prompt

## Symptom (from the reopen)

2026-09-02→03 night, `saas_admin_amb`: 3 independent panes on 3 different
roles (`reviewer`, `codex`, `critic`) all hit `delivery_boot_timeout_failed`
within ~11 minutes of each other. Every one had to be re-fired with
`--provider claude`, losing model diversity for the whole session. This was
a reopen — round 1/2 of #276 (boot-stall ceiling, boot-phase-marker window)
already shipped and were assumed to have fixed the underlying issue.

## Evidence

Read directly from the 3 transcript files (`runtime/sessions/2026-09-02/
saas_admin_amb/{reviewer-231929,codex-232127,critic-233350}.transcript.log`,
ANSI-stripped) — not inferred. All 3 are byte-for-byte identical in shape:

**Frame 1** (immediately after spawn):
```
╭───────────────────────────────────────╮
│ >_ OpenAI Codex (v0.152.0)            │
│                                       │
│ model:     loading   /model to change │
│ directory: loading                    │
╰───────────────────────────────────────╯
› Ask Codex to do anything
  ? for shortcuts
```

**Frame 2** (the very next paint captured in each transcript — and the LAST
one; nothing further arrives before the pane is torn down at the ceiling):
```
╭────────────────────────────────────────────────╮
│ >_ OpenAI Codex (v0.152.0)                     │
│                                                │
│ model:       loading   /model to change        │
│ directory:   ~\WebstormProjects\saas_admin_amb │
│ permissions: YOLO mode                         │
╰────────────────────────────────────────────────╯
› Ask Codex to do anything
  ? for shortcuts
```

`directory:` and `permissions:` resolved. `model:` did not — in any of the
3 transcripts, ever. The composer (`› Ask Codex to do anything` /
`? for shortcuts`) is drawn and stable in both frames. Cross-referenced
against `events.log.old`: the boot-ceiling reprobe for all 3 panes logged
`heartbeat_age_s` between 0.02 and 0.17 — the cockpit's own main thread was
never stalled; the PTY reader was receiving live redraws (cursor-visibility
toggles, `[?25h`/`[?2026h`) the entire time, just never a `model:` value.

## Root cause (proven by code trace, not guessed)

`pty_session._BOOT_PHASE_MARKER_RES` (added for #380) matches
`\bmodel:\s+loading\b` OR `\bdirectory:\s+loading\b` over a WIDE
(`_BOOT_MARKER_TAIL_ROWS` = 20-row) window specifically so the wide window
still sees codex's banner box even once its bordered composer has grown
enough rows to push the banner out of the tight 6-row `_ready_region`
window `is_at_ready_prompt()` uses.

That is exactly why `is_at_ready_prompt()` reads **True** here — the
composer's own text (`ask codex`, matched by `ReadyRule("ask codex", True)`
in `provider_spec.codex_spec.ready_rules`) is well within the tight
6-row window in both frames; the `model:`/`directory:` box never is. But
`lead_inbox._send_when_ready`'s `_check()` deliberately does NOT trust that
verdict alone for codex — it separately computes `_still_booting =
pane.session.shows_boot_phase_marker(rows=_BOOT_MARKER_TAIL_ROWS)` (the wide
window) and gates delivery on `not _still_booting` (see the `#284` comment
by that gate). In frame 2, `directory: loading` is gone but `model: loading`
is still there — `_BOOT_PHASE_MARKER_RES` still matches, so
`_still_booting` reads True. Forever, in this build. The pane never
accumulates a ready streak, the boot-stall ceiling eventually fires, the
one bounded reprobe (#387/#448) sees the identical frame again, and
`_fail_boot_stalled_delivery` runs — failing a task on a pane that was
idle at its prompt the whole time.

This is NOT a marker-recognition gap (the composer text is already
correctly classified ready by the existing table) and NOT the #380
fresh-boot race (that race has BOTH fields reading "loading" together,
which frame 2 does not). It is a new, narrower failure mode: this codex
build can leave `model:` permanently unresolved independent of the rest of
the banner, and the existing wide-window boot check has no way to tell that
apart from a genuine still-booting pane — because, until now, `model:` and
`directory:` always cleared together.

## Fix

`pty_session._has_stuck_model_field_marker(region)`: True only when
`model:\s+loading` matches AND `directory:\s+loading` does NOT — i.e.
`directory:` has already resolved. This can never fire during the genuine
#380 race (both fields loading together), so it cannot reopen that bug.
Exposed as `PtySession.shows_stuck_model_field_marker()`.

`lead_inbox._send_when_ready`'s `_check()`: once `_still_booting` is True
AND the pane's own tight-window verdict (`is_at_ready_prompt()`) already
reads True AND the codex-only MCP-splash settle window
(`ProviderSpec.boot_splash_paste_after_s`, 10s, already 0 for every other
provider — reused rather than adding a new field) has elapsed since the
session came alive, check `shows_stuck_model_field_marker()`. If it is the
round-3 quirk, override `_still_booting` to False for the rest of this poll
— logging `ready_marker_stuck_field_override` once — and let the ordinary
ready-streak path deliver normally instead of failing the delivery.

Scoped narrowly on purpose: the override requires BOTH the pane's own
ready verdict AND the directory-resolved distinction, so a pane that is
genuinely still loading its MCP servers (composer not yet live,
`is_at_ready_prompt()` still False, or `directory:` still loading) is
completely unaffected and still gets the existing #271/#276/#284/#387/#448
protections.

## Multi-provider / cross-platform

`_splash_paste_after_ms` is 0 for every provider except codex
(`test_codex_has_window_and_others_do_not`), so the override is a pure
no-op everywhere else — claude/gemini-agy/opencode/kimi/cursor panes that
are genuinely stuck still fail out through the unmodified existing path.
No platform-specific code; the regexes operate on already-decoded screen
text from the shared pyte-backed `display_lines()`, identical on Windows
ConPTY and macOS `_pty_backend`.

## Tests

- `tests/test_codex_model_field_stuck_override.py` — pure-function coverage
  for `_has_stuck_model_field_marker` (fresh-boot frame → False, stuck
  frame → True, neither field present → False) + delivery-level coverage
  via `Orchestrator._send_when_ready` (delivers once the settle window
  passes; does not override before the settle window; no-ops for a
  provider with no splash window; the genuine #380 race still fails
  normally when the pane never reaches ready).
- `pty_session._READY_SELFTEST_CASES`: added the exact v0.152.0 composer
  wording (`? for shortcuts`, not the older `? for help`) as a regression
  fixture — confirms it stays ready-classified via `ready_marker_selftest()`
  (already exercised by `tests/test_pty_ready_prompt.py`).
