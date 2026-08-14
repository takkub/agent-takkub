# #200 — Pulse page ไม่โชว์ว่ามี pane ไหนเปิด/ทำงานอยู่

**Date:** 2026-08-14
**Issue:** [#200](../..) — `remote Pulse: ไม่โชว์ว่ามี pane ไหนเปิด/ทำงานอยู่ (LEAD_ONLY_STREAM บังคับ roles=[])`
**Files touched:** `src/agent_takkub/remote/config.py`, `src/agent_takkub/remote/api.py`,
`src/agent_takkub/remote/static/app.js`, `src/agent_takkub/remote/static/index.html`,
`tests/test_remote_api.py`, `tests/test_remote_pwa_pulse_roles.py`

## Root cause (as diagnosed in the issue, confirmed by reading code)

`LEAD_ONLY_STREAM = True` (2026-07-23) was one flag doing two jobs:

1. **Push** — suppress `notify.LeadNotifier._on_done`'s SSE push for a teammate's `takkub done`
   (this was the actual complaint that shipped the flag — "เป็นขยะเยอะเกินไป").
2. **Pull** — force `api.activity()`'s `roles` list to always `[]` and `api.pulse()`'s count to
   scope down to the `lead` entry, even when the user opens the Pulse page themselves.

(2) was never asked for — it was collateral from reusing one flag for both concerns. The user
opening Pulse to check what's running got "Claude idle" with zero information about three other
open panes actively working.

## Fix: split into two flags

`remote/config.py` now has:

- `LEAD_ONLY_STREAM = True` (unchanged default) — still gates only the push path
  (`notify.LeadNotifier._on_done`). Teammate `done` events still don't ping the phone unsolicited.
- `PULSE_SHOW_TEAM = True` (new, defaults **on** — opposite polarity from what
  `LEAD_ONLY_STREAM`'s old pull-side behavior effectively was) — gates the two pull paths:
  - `api.activity()` — `roles` now lists every open teammate pane (not just working ones), each
    with `{role, state: "working"|"idle", runtime_sec, provider}`.
  - `api.pulse()` — `total`/`working` count every open pane instead of scoping to Lead.

Setting `PULSE_SHOW_TEAM = False` restores the exact pre-#200 pull behavior (Lead-only), for
anyone who wants to go back to it independent of the push flag.

## Data minimization (§7.3) — unchanged bar, one field added

Same fields as before plus a coarse `state` string. Still never task text, cwd, command, or
fine-grained status (`"working (stalled 12m)"` collapses to `"working"` like everywhere else in
this API). Verified by `TestActivity::test_never_leaks_task_cwd_or_transcript_fields` (now also
checks a teammate role's key set, not just Lead's).

## Provider-neutral (#103)

No change needed — `notify.pane_provider_name()` already resolves the provider per-pane
(model-recorded first, config fallback), and the PWA's `makeRoleChip`/`providerIdentity`/
`providerMeta` were already generic (no `"claude"` hardcoding). A codex/gemini/opencode/kimi/
cursor teammate pane renders through the same code path as before — confirmed via
`TestActivity::test_uses_provider_recorded_on_each_live_pane`.

## PWA changes

- `app.js`: `renderPulse()`'s `roles.forEach` now passes `r.state !== "working"` to
  `makeRoleChip` instead of a hardcoded `false` — idle teammates render with the existing
  `.role-chip.idle` dimmed style (CSS already supported this; only the Lead chip used it before).
  `visible` counting logic unchanged (already counted any non-empty `roles`), so idle-only
  projects now correctly earn a card instead of being invisible.
- `VIEW_SUBTITLES.pulse`: `"เห็นแค่จำนวน"` → `"pane ที่เปิดอยู่ตอนนี้"`.
- `index.html`'s `#pulse-caption`: `"ไม่แสดงว่าใครทำ task อะไร — ดูรายละเอียดที่หน้า Lead"` →
  `"แสดงเฉพาะ role + สถานะ + เวลาทำงาน — ดู task จริงที่หน้า Lead"` (accurate now: role + state +
  runtime *are* shown; only the task text itself stays Lead-only).
- Empty state copy: `"ไม่มีงานกำลังรันอยู่"` → `"ไม่มี pane เปิดอยู่"` (now fires only when
  literally nothing is open, not just when nothing is "working" — an idle-only project still
  renders a card).

## Tests

- `tests/test_remote_api.py`:
  - `TestPulseDataMinimization` — now pins the *default* (no monkeypatch needed — `PULSE_SHOW_TEAM`
    defaults to the count-every-pane behavior this class always pinned).
  - `TestPulseLeadOnlyStreamGate` → renamed `TestPulseShowTeamGate` — same assertions, now reached
    via `monkeypatch.setattr(api._remote_config, "PULSE_SHOW_TEAM", False)` instead of the old
    default-true `LEAD_ONLY_STREAM`.
  - `TestActivity` — rewritten for the new default: idle teammates included
    (`test_idle_teammates_are_included_too`), a project with teammates but no Lead pane now shows
    (`test_project_with_working_teammates_but_no_lead_is_shown`, previously omitted), a working
    pane with no `_working_start` now reports `runtime_sec: 0` instead of being dropped entirely,
    an explicit `PULSE_SHOW_TEAM=False` off-switch test, and a same-test flip-off-then-on test
    proving the gate is live logic, not a hardcoded branch.
- `tests/test_remote_pwa_pulse_roles.py` (new) — structural checks (no JS runtime in this repo,
  same pattern as `test_remote_pwa_scroll_pin.py`): role chip reads `r.state` instead of a
  hardcoded `false`, the stale "count only" subtitle/caption copy is gone, empty-state copy no
  longer implies "nothing working" when idle panes exist.
- `tests/test_remote_notify.py`'s push-path tests (`test_teammate_done_is_dropped_when_lead_only`,
  `test_teammate_done_still_pushes_when_flag_off`) — untouched, still correct: they exercise
  `LEAD_ONLY_STREAM` against `_on_done`, which this fix deliberately left alone.

Ran (`.venv` editable-reinstalled against this worktree first — the shared venv's editable
install otherwise points at the main repo's `src/`, see `run-tests-with-venv-editable` memory):

```
tests/test_remote_api.py tests/test_remote_notify.py tests/test_remote_http_server.py
tests/test_remote_pwa_quick_reply.py tests/test_remote_pwa_resume.py
tests/test_remote_pwa_scroll_pin.py tests/test_remote_pwa_pulse_roles.py
```

246 passed. `ruff check` clean on all touched files. Full suite left to QA's batch gate per
project convention (targeted tests only mid-flight).

## Not changed (deliberately)

- `LEAD_ONLY_STREAM` default and push behavior — the original 2026-07-23 directive against
  notification spam stands untouched, per the issue's explicit warning not to revert it wholesale.
- `/api/pulse` route wiring in `http_server.py` — no change needed, it already forwards straight
  to `api.pulse()`.
