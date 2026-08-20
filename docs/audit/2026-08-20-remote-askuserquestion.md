# Remote (mobile PWA) can't answer AskUserQuestion — root cause + fix

Reported by user: Lead pane in project `saas_admin` fired `AskUserQuestion`
twice; the mobile PWA could not be used to answer either question.

## Method

Read the code path end-to-end first (`notify.py` → `app.js` → `api.py` →
`cli_server.py` → `orchestrator.send()`), then proved the actual terminal
behavior empirically: spawned real `claude --dangerously-skip-permissions`
sessions in isolated scratch dirs via `_pty_backend.spawn_pty_bounded` +
`pyte.Screen` (the same libraries `pty_session.py` itself uses), prompted the
model to call `AskUserQuestion`, and replayed the exact bytes the mobile
"answer" path writes (`_paste_payload` + the orchestrator's delayed-Enter
self-heal), capturing the real terminal screen before/after. No guessing —
every claim below has a captured screen backing it.

## Root cause #1 (the reported bug): typed text does not drive the picker

`AskUserQuestion` renders a real interactive TUI picker:

```
 ☐ Fruit
Pick a fruit for the test?
❯ 1. Apple
     Select Apple
  2. Banana
     Select Banana
  3. Cherry
     Select Cherry
  4. Type something.
────────────────────────────────────────────────────────────────
  5. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel
```

`api.lead_say()` → `orchestrator.send()` is the ONLY path the mobile PWA has
to answer (`sendLeadMessage(label)` → `POST /api/lead/say` → writes
`"[remote → lead] <label>"` into the pane's PTY, then Enter). This pipeline
was built for **chat messages**, not picker control:

- Every character of the typed label (`[remote → lead] Banana`) is silently
  **discarded** by the picker one keystroke at a time — the screen is
  byte-for-byte identical before and after writing it. The picker is a
  selection menu, not a text field; it only understands digits/arrows/Enter.
- The subsequent Enter does not "fail" — it submits **whatever option is
  currently highlighted**, which is option 1 (the default cursor position)
  regardless of what the user tapped.

**This is worse than "can't answer": it silently answers with the WRONG
option.** Repro: mobile taps "Banana" → typed text has zero effect → Enter
fires → transcript shows `● User answered Claude's questions: · ... → Apple`
(the untouched default), not the intended answer. Full raw screens in the
scratch harness output (not committed — ephemeral repro only).

## Root cause #2: `notify.py` only forwards the first question

`remote/notify.py::_ask_question_options` reads
`inp["questions"][0]` only — confirmed by code inspection (no PTY repro
needed for this one, it's a straight code read). A Lead turn that fires
`AskUserQuestion` with 2+ questions in one call only ever reaches the phone
as the first question; the picker payload for question 2 is never sent at
all, so the phone can't even show the second question, let alone answer it.
Matches the reported "Lead fired 2 questions, mobile couldn't answer either."

## What actually works (proven, not guessed)

Reused one running `claude` session across multiple `AskUserQuestion` calls
to test key sequences without re-paying boot cost per round:

1. **Single-select, single question**: a bare **digit keypress** (`"2"` for
   the 2nd option, 1-based) selects **and submits immediately** — no Enter
   needed. Verified 3x independently (Apple/Banana/Cherry → picked Green
   for "1.Red/2.Green/3.Blue" via down-arrow+Enter; picked Square for
   "1.Circle/2.Square/3.Triangle" via digit `"2"` alone).
2. **Multiple questions in one `AskUserQuestion` call**: renders as a tab
   bar `←  ☐ Pet  ☐ Drink  ✔ Submit  →`, one question's picker visible at a
   time. Answering the current tab's question with its digit key
   **auto-advances to the next tab** — no manual tab-switch keys needed.
   After the **last** question is answered, the CLI shows an extra **"Review
   your answers"** confirmation screen (`❯ 1. Submit answers / 2. Cancel`)
   that requires **one more digit `"1"`** (or Enter, since it's the default)
   to actually finalize. Skipping this step leaves the whole multi-question
   answer un-submitted and the picker still open. Verified: 2-question call
   (Pet→Cat, Drink→Coffee), both answers landed correctly on the review
   screen, confirmed by the `⎿ · Pick a pet? → Cat` / `⎿ · Pick a drink? →
   Coffee` transcript lines.
3. **multiSelect questions**: render differently — checkboxes (`[ ]`) per
   option plus the same top tab bar. The hint line says **"Enter to select"**
   (not Space) — Space does nothing. One round-trip attempt (down-arrow,
   Space, Enter) left the picker **stuck open** (never submitted) inside the
   180s test window, consistent with Space being the wrong key. A follow-up
   attempt to nail the exact "navigate to the Submit action" key count did
   not get a clean repro within the session's time budget (a parallel-spawn
   contention issue on one run, and a stalled trigger on the retry — neither
   is a finding about the picker itself, just scratch-harness flakiness).

## Scope decision: multiSelect not wired for mobile yet

Given (2) and (3) above are proven-safe/proven-working but multiSelect's
submit sequence is **not** independently proven, shipping a guessed key
sequence for it risks repeating exactly the bug this fix closes (silently
submitting the wrong checkbox state). `answer_picker()` in `api.py`
explicitly rejects (400) any picker where `multiSelect` is true on any
question, and the PWA falls back to the existing plain "answer on desktop"
banner for that case. Follow-up: nail the multiSelect submit sequence with
the same PTY-repro method and wire it in — tracked here, not silently
dropped (multi-provider/gap-flagging convention, see `#103`).

## Fix shipped

- `notify.py`: `_ask_question_options` now returns **every** question in the
  tool call (`{"questions": [{prompt, options, multiSelect}, ...]}`), capped
  defensively at `_MAX_ASK_QUESTIONS`. Added `current_ask_state(orch,
  project_ns)` — a **fresh, uncached** re-read of the Lead's JSONL tail
  (reuses `resolve_lead_jsonl`, walks backward to the single most recent
  meaningful record) used as the answer endpoint's guard: if the picker has
  already been dismissed on desktop (text reply, or a different tool_use,
  came after it), this returns `None` and the endpoint refuses to inject
  keys into whatever the pane is now showing instead of typing garbage into
  a live chat turn.
- `orchestrator.py`: `answer_picker(key_sequence, project)` — writes a raw
  ASCII key sequence (digits + optional trailing `\r`) straight to the Lead
  pane's PTY, bypassing `send()`'s chat-message pipeline entirely (that
  pipeline's `_sanitize_pane_text` strips control bytes a chat message
  should never carry — irrelevant here since every sequence this endpoint
  builds is plain digits/`\r`, no ESC needed, precisely because multiSelect
  — the only case that would need arrow-key ESC sequences — is out of scope
  above).
- `cli_server.py` / `api.py` / `http_server.py`: new `answer-picker` cmd,
  gated the same way `send` is when called from remote (Lead-token
  required, `from: "remote"`, off-thread loopback call — same pattern
  `lead_say` already uses). New `POST /api/lead/answer-picker` route,
  control-mode gated like every other mutating remote endpoint.
- `static/app.js`: `showPickerBanner` renders every question with tappable
  option chips; a single "ส่งคำตอบ" button posts all answers in one call
  once every question has exactly one selection. Any `multiSelect` question
  in the payload falls back to the plain "ตอบจากเดสก์ท็อป" banner (see scope
  decision above) instead of rendering chips it can't safely submit.
  non-claude providers keep the exact same fallback banner as before
  (`_HistoryScanner.live_ask` default `None` — untouched).

## Tests added

- `notify.py`: `_ask_question_options` returns all N questions (not just
  the first) for a multi-question tool_use record; single-question shape
  still round-trips as a 1-element `questions` list.
- `api.py`: `answer_picker()` key-sequence builder — single question →
  bare digit, no trailing Enter; multi-question → digits per question +
  trailing `\r`; multiSelect present anywhere → rejected (400); guard
  returns 409 when `current_ask_state` reports no active picker; option
  index out of range → rejected (400).
