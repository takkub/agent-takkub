# Remote mirror blank on non-claude Lead (BlueParking / OpenCode)

## Report

Friend's machine: macOS (`/Users/assawin.t/.agent-takkub`), cockpit v1.0.57,
project **BlueParking**, Lead pane = **OpenCode** (status bar: "Opencode
(default)", replies tagged "Build · DeepSeek V4 Flash Free").

Evidence captured desktop + phone side-by-side:

- **Desktop**: `[remote → lead] ดี` appears in the pane, OpenCode replies
  "สวัสดีครับ! มีอะไรให้ช่วยไหมครับ?" in 2.4s.
- **Phone (Remote PWA via tunnel)**: stuck on "⌛ OpenCode กำลังทำงาน…",
  no reply ever appears.

## Root cause

Message **delivery** and message **mirroring** are two separate code paths
that happen to look like one feature:

1. `POST /api/lead/say` → `cli_server`'s `send` command writes straight into
   the Lead pane's pty (`orchestrator.py::send`). This is provider-agnostic
   and always works — it's why the desktop showed the reply fine.
2. The phone's live reply comes from a **completely different** mechanism:
   `remote/notify.py` tails the Lead pane's own structured transcript file
   (Claude's JSONL / Codex's rollout JSONL / Gemini's session JSONL) and
   forwards new assistant text over SSE. This requires a registered
   `_HistoryScanner` for the provider.
3. `provider_spec.py`'s `PROVIDER_REGISTRY` only sets
   `supports_remote_history=True` for **claude, codex, gemini**. OpenCode,
   Kimi, and Cursor all have `supports_remote_history=False` — no scanner is
   registered for any of them, so `notify.history_scanner("opencode")`
   always returns `None`. The "lead" SSE event that would carry the reply
   text — and clear the phone's spinner — never fires for these providers.
4. The phone's composer already had a client-side 30s optimistic-spinner
   timeout (`beginOptimisticWorking` in `app.js`), so it wasn't a truly
   *infinite* hang, but it silently gave up with **zero explanation** — the
   user has no way to tell "broken" from "this provider just can't mirror
   replies yet".
5. Separately: `api.py::lead_history`/`lead_sessions` already computed a
   `lead_provider_note` explaining the gap ("Lead provider = opencode —
   remote history/session unavailable") — but the PWA (`app.js`) never read
   that field. The explanation existed server-side and was discarded
   client-side. `POST /api/lead/say` (the actual send path) didn't compute
   or return it at all.

This matches hypothesis #1 exactly, confirmed by the friend's screenshots,
not guessed.

## What was NOT the cause (ruled out)

- `_resolve_claude_jsonl_path`'s exact-uuid-only resolution (no newest-file
  fallback) — correct as-is, not touched, not re-added.
- `pane.state` "working"/"idle" tracking — provider-agnostic, unrelated to
  this bug (a `send`-to-Lead message never even flips `pane.state` to
  `"working"` for *any* provider; that field is driven by `assign()`'s task
  dispatch, a different flow entirely from the phone's chat composer).

## Fix (this branch)

1. **`remote/api.py::lead_say`** now returns `provider`, `mirror_supported`,
   and `lead_provider_note` in its response — computed immediately, not
   discovered later via a blind timeout. `lead_upload_image` forwards the
   same fields.
2. **`remote/static/app.js`**:
   - `sendLeadMessage`'s POST response now checks `mirror_supported`; when
     `false`, the spinner is cleared immediately and a system message
     explains the provider can't mirror replies here — check the desktop.
   - The 30s blind optimistic-timeout (for a *supported* provider whose
     mirror silently fails, e.g. uuid drift) no longer vanishes silently —
     it now leaves a "ยังไม่เห็นคำตอบใน 30 วิ — เช็คที่เดสก์ท็อป" trace.
3. **`takkub doctor --live`**: new `remote-mirror` check
   (`cli_server.py`'s `remote-mirror-status` command +
   `doctor.check_remote_mirror_live`) reports, per project: Lead pane open?
   provider? does that provider have a registered scanner? session_uuid
   present? (claude only) does a transcript file exist for that exact
   uuid? See the checklist below.
4. **`provider_spec.py`**: `opencode_spec` / `kimi_spec` / `cursor_spec`
   each get an explicit `GAP (#103)` comment on `supports_remote_history`
   pointing at this fix, so the gap stays documented instead of silent.
5. **Never reintroduced**: the newest-file resolution fallback in
   `_resolve_claude_jsonl_path` — that was a proven-bad guess, removed on
   purpose in an earlier fix, and stays removed.

### Architecture note (why doctor.py duplicates a few lines instead of
reusing `remote/notify.py`)

`doctor.py` and `cli_server.py` are both barred from importing
`agent_takkub.remote` by the `remote-bolt-on-isolation` import-linter
contract (remote/ must stay delete-to-uninstall). The new
`remote-mirror-status` cli_server command therefore re-derives the Lead
pane's live provider + `session_uuid` + (claude-only) transcript-existence
check directly from `orch._panes_by_project` / `orch._pane_state`, mirroring
`notify.py`'s `pane_provider_name` / `_lead_session_uuid` /
`_resolve_claude_jsonl_path` logic rather than importing it. Confirmed with
`lint-imports` — all 23 contracts still kept.

## Checklist for the friend to run (macOS)

```bash
# 1. Confirm the cockpit build has this fix (needs a version past 1.0.57)
takkub --version

# 2. Live remote-mirror diagnostic — run with BlueParking's Lead pane open
takkub doctor --live
# Look for the [remote-mirror] section. For an OpenCode Lead you should see:
#   ✓ lead-pane        project=BlueParking provider=opencode
#   ✗ history-scanner  provider=opencode has NO remote-history scanner registered ...
#     → fix: known gap for opencode/kimi/cursor (issue #103) — desktop is
#       the only place to read this Lead's replies until a scanner ships
# That FAIL is EXPECTED for opencode today — it confirms the diagnosis,
# it does not mean something else is broken.

# 3. If the Lead is claude/codex/gemini and the phone is STILL blank,
#    the same command will instead show a `transcript-file` FAIL — that is
#    a different bug (session_uuid drift), not this one. Send that output.
```

## What the friend should see after the fix (OpenCode Lead)

- Sending a message from the phone still delivers to Lead instantly (no
  regression to that path).
- The phone shows a one-line system note instead of hanging:
  "Lead provider = opencode — remote history/session unavailable" — with
  the spinner cleared immediately, not after 30 seconds.
- Answers are still only readable on the desktop pane until a scanner is
  registered for OpenCode's local session store, if one exists (issue #103
  follow-up — not investigated further here since no OpenCode CLI was
  available on this machine to probe its transcript format; the audit item
  is #6 below).

## Follow-ups (not done in this pass, flagged for Lead / issue #103)

- Investigate whether OpenCode actually writes any local session transcript
  (format unknown, no OpenCode CLI installed on this dev machine to probe)
  — if one exists, a `_HistoryScanner` could be added the same way
  codex/gemini were, closing the gap for real instead of just explaining it.
  Same question applies to Kimi and Cursor.
- `sendLeadImage`'s upload flow in `app.js` was NOT updated to react to
  `mirror_supported` the way `sendLeadMessage` was (the reported bug was
  about the text composer) — worth a small follow-up for parity.
