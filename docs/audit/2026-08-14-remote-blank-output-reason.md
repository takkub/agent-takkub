# #192 — Remote PWA: blank output, no reason shown

2026-08-14 · backend · branch `wt/backend-1786666280`

## TL;DR

No live repro was available (bug reported on a machine other than this dev box). Investigation
confirmed the primary suspected cause — provider without a remote-history scanner — was **already
fixed for the send-time spinner in 1.0.58**, but the *blank chat on open* path was never wired to
that same reason, and a second cause (`session_uuid` set but transcript file not found — drift)
had **no reason surfaced anywhere at all**. Both gaps are now closed. Cockpit version is now also
exposed to the phone (Lead's hypothesis A), since there was previously no way for the user to tell
if they were on an old build.

**If the reporting user's Lead pane is a non-claude/codex/gemini provider (opencode/kimi/cursor),
or a claude/codex/gemini session whose id drifted from its transcript, the fix in this branch
directly explains what they saw. If neither applies, `takkub doctor --live` on that machine is
still the next step — see "Remaining gap" below.**

## What 1.0.58 already fixed (verified against code, not assumed)

CHANGELOG `[1.0.58] - 2026-08-13` claims two things relevant to #192:

1. `takkub doctor --live` gained a `[remote-mirror]` finding group.
   Confirmed live: `doctor.py:1765` (`check_remote_mirror_live`) + `cli_server.py:704`
   (`_remote_mirror_status`, the live-fact gatherer). Desktop-only — the user on the phone has
   no way to trigger this themselves. Covered by `tests/test_remote_mirror_diagnostics.py`
   (pre-existing, all still green).

2. "มือถือค้างสปินเนอร์เงียบๆ เมื่อ Lead ไม่ใช่ claude" — confirmed live:
   `remote/api.py::lead_say()` returns `mirror_supported` + `lead_provider_note`
   (`_lead_provider_note()`, gated on `notify.supports_remote_history(provider)`), and
   `app.js:1284` shows a banner instead of hanging when a message is *sent* to a
   provider-unsupported Lead.

**This means the task spec's "case 1 (provider_unsupported) is the closest match" framing is
half-solved already**: half-solved because it only fires on the *send* action. A user who
opens the app fresh (never types anything, or reopens later) and looks at an already-open,
non-history-capable Lead pane's chat still saw a **generic "ยังไม่มีข้อความ" (no messages yet)**
line — not the reason. That was the real remaining gap, not a duplicate of the 1.0.58 fix.

## What was still missing (the actual #192 gap — proven by reading the code, not guessed)

`remote/api.py::lead_history()` — the endpoint the PWA calls on every connect/reconnect/project
switch to repopulate the chat log — returned an empty `messages: []` with **no indication why**
whenever:

- the provider has no scanner at all (`provider_unsupported`) — already partially explained via
  `lead_provider_note`, but that field was never read by the *empty-chat render path*
  (`app.js::renderSelectedProject()`), only by the send-banner path.
- a session_uuid was recorded, but no matching transcript file resolved
  (`transcript_missing` — session drift, e.g. a manual desktop `/resume`, or a fresh pane
  whose provider hasn't written its file yet). **No field existed for this at all** — this is
  the gap the task spec called "case B / the one nobody has closed yet," and Lead's mid-task
  note confirmed it's the higher-priority hypothesis given a normal-looking claude Lead.
- no session_uuid was recorded yet (`no_session_uuid`) — also unrepresented.

`app.js::renderSelectedProject()` (the code that actually paints the blank-chat placeholder,
`app.js:1122` before this change) only ever branched on `historyLoaded` — "still connecting" vs.
"no messages yet" — with **no path for "connected fine, but here's why it's empty."**

## Fix

**`remote/notify.py::lead_mirror_diagnosis(orch, project_ns)`** (new) — in-process classifier,
computed directly against `orch` the same way `lead_history_snapshot` already does (no cli_server
loopback round trip needed, unlike `pulse()`). Mirrors the exact three-layer logic
`doctor.check_remote_mirror_live` / `cli_server._remote_mirror_status` already prove correct for
`takkub doctor --live`, so a phone user gets the same diagnosis without needing desktop access:

| `code` | Meaning | Provider-neutral? |
|---|---|---|
| `provider_unsupported` | `ProviderSpec.supports_remote_history=False` (opencode/kimi/cursor, #103) | yes — checked before any provider-specific logic |
| `no_session_uuid` | Scanner needs a uuid (claude) and none is recorded yet | yes — `requires_session_uuid` is a scanner property, not a claude-only check |
| `transcript_missing` | Scanner resolved nothing — claude uuid drift, or codex/gemini's own cwd+mtime resolver found no file | yes — same code path for all three history-capable providers |
| `None` | A transcript resolved — a blank chat here is legitimately "no messages yet," not a fault | — |

**`remote/api.py`**:
- `_empty_reason_payload(diagnosis)` (new) — maps a diagnosis to a short Thai, user-facing string.
  Only ever computed when `messages` came back empty (`lead_history()`'s `not messages` guard) —
  a populated chat never pays this extra cost.
- `lead_history()` now returns an `empty_reason: {"code", "text"} | None` field.
- `usage()` now returns `cockpit_version` (Lead's hypothesis A — the phone had no way at all to
  tell if it was talking to an old build). Rides along on the endpoint the PWA already polls
  every `USAGE_POLL_MS` tick — no new request added.

**`remote/static/app.js`**:
- `loadHistory()` stores `lead.emptyReason` from the new field.
- `renderSelectedProject()` prefers `lead.emptyReason` over the generic "no messages yet" text
  when present.
- `fetchUsage()` / `renderUsageCaption()` append `· cockpit vX.Y.Z` to the existing usage-drawer
  caption when a version is present.

## Data-minimization (§7.3) — what was deliberately left out

- `transcript_missing`'s `session_uuid_short` is the **first 8 characters only**, never the full
  uuid (mirrors `doctor.py`'s own `session_uuid[:8]…` convention). Covered by
  `test_empty_reason_transcript_missing_includes_short_uuid_not_full`.
- No filesystem path (config dir, project dir, jsonl path) is ever included in any reason text —
  only provider name + short id.
- `provider_unsupported`'s text names the provider (already exposed elsewhere via `provider`/
  `lead_provider_note`) — not new exposure.

## What was explicitly NOT done

- **No newest-file fallback was reintroduced.** `_resolve_claude_jsonl_path`'s exact-uuid-only
  contract is untouched — `lead_mirror_diagnosis` only *classifies* an existing None result, it
  never changes what `resolve_lead_jsonl` resolves to.
- **No claude-only shortcut.** `requires_session_uuid` and `history_scanner()` are the existing
  provider-neutral abstractions (#103) — `lead_mirror_diagnosis` dispatches through them exactly
  like `resolve_lead_jsonl` does, so codex/gemini get the same three-code classification claude
  does, and opencode/kimi/cursor get `provider_unsupported` the same way regardless of platform.

## Remaining gap (still needs `takkub doctor --live` on the affected machine)

`transcript_missing`'s message covers two distinct real causes with one generic explanation
(uuid drift **or** a `CLAUDE_CONFIG_DIR`/profile-path mismatch, issue's case 3) because
distinguishing them from the phone would require exposing the actual config directory path,
which data-minimization forbids. If the reporting user's issue turns out to be case 3
specifically, `takkub doctor --live`'s `[remote-mirror]` findings (desktop-only, already shipped
in 1.0.58) remain the only way to pinpoint it exactly — this fix gets the phone to "something is
wrong with this session, try /resume or restart," not "your `CLAUDE_CONFIG_DIR` is misconfigured."

## Tests

- `tests/test_remote_notify.py::TestLeadMirrorDiagnosis` (5 new) — one per diagnosis code across
  claude (uuid-requiring) and codex (non-uuid-requiring) scanners, proven red against
  pre-fix code (the function didn't exist) before implementing.
- `tests/test_remote_api.py::TestLeadHistory` — updated the 2 exact-dict-equality tests that broke
  from the new `empty_reason` key, added 4 new tests (omitted-when-populated, each reason code,
  short-uuid-not-full-uuid data-min check).
- `tests/test_remote_api.py::TestUsage::test_reports_cockpit_version` (new).
- Full `test_remote_api.py` + `test_remote_notify.py` + `test_remote_mirror_diagnostics.py` +
  `test_remote_pwa_quick_reply.py` + `test_remote_pwa_resume.py` + `test_remote_settings_dialog.py`
  + `test_remote_chip.py` all green after the change.
- `ruff check` + `ruff format --check` clean on all touched files.
- `node --check app.js` clean (no Node test harness exists for this file — verified syntactically;
  browser-level verification is QA's lane per role policy, not run here).
