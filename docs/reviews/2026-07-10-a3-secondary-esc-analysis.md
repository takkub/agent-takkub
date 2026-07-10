# Issue #114 (A3-secondary): bare-Esc / Ctrl+W draft_len drift — analysis + fix

## Scope

Issue #114 flagged two related `lead_draft_state.py` drift sources left out of
the primary A3 fix (commit `2ef9250`):

1. Bare Esc (`0x1B` that proves out as neither CSI/SS3/mouse) unconditionally
   clears the tracked draft to `"empty"`, even when the real input line still
   has text on it.
2. Ctrl+W / Alt+Backspace ("delete previous word") weren't tracked at all —
   `draft_len` never moved, causing it to drift from the real line length.

## Part 2: Ctrl+W / Alt+Backspace — fixed

Both encodings are **unambiguous** text-editing requests: Claude CLI never
uses Ctrl+W (`0x17`) or Alt+Backspace (`Esc` + Backspace/Del, `\x1b\x7f` /
`\x1b\x08`) to close a menu, dialog, or autocomplete overlay — they only ever
mean "delete the word behind the cursor." Unlike bare Esc, there is no second
meaning to disambiguate, so this half of the issue was safe to fix directly.

**Fix:** added a `word_len` field to `LeadDraftState` — the count of
printable chars typed since the last whitespace in the current unbroken
`"nonempty"` run. It's maintained incrementally (reset on whitespace, on any
clear, and after a word-delete consumes it; decremented alongside `draft_len`
on backspace). Ctrl+W / Alt+Backspace now decrement `draft_len` by
`word_len` when it's known (the common case — deleting the word just typed —
becomes exact), falling back to a conservative 1-char step once it's
exhausted (e.g. a second word-delete in a row with no tracked boundary).

The 1-char fallback is a deliberate choice, not an oversight: any residual
error from the heuristic is biased toward *overcounting* `draft_len` rather
than under. Overcounting only delays injection (it spills to the durable
queue after `DRAFT_HOLD_TIMEOUT_S` — safe, just slower) where undercounting
would let `draft_len` reach 0 while real text remains, unblocking injection
into a still-nonempty line — a clobber, which is exactly the class of bug
#114 exists to close.

See `advance_draft_state`'s `_CTRL_W` branch and the `Esc`+backspace
special-case in the `_ESC` branch, plus `_apply_word_delete` — all in
`src/agent_takkub/lead_draft_state.py`. Tests:
`TestWordDeleteCtrlW` / `TestWordDeleteAltBackspace` in
`tests/test_lead_draft_state.py`.

## Part 1: bare Esc — analysis, left unchanged (recommendation for Lead)

**Conclusion: not fixed. The two Esc meanings are provably indistinguishable
from the data this module has access to, and the existing fail-safe
(over-clear) is the safer of the two possible wrong guesses.**

### Why it can't be disambiguated here

`advance_draft_state()` is fed **exclusively** from
`Orchestrator._on_pane_input` (confirmed by reading `lead_inbox.py`) — i.e.
keystrokes the user typed *into* the pane, going toward the PTY. It never
sees the Lead pane's *rendered output* — the text Claude CLI is currently
painting to the terminal. Whether a given Esc keypress is:

- clearing a draft the user was typing (state genuinely *should* clear), or
- dismissing a slash-menu/autocomplete/dialog Claude CLI has open, where the
  actual input line the user was drafting is untouched underneath it (state
  should *not* clear — the draft is still there)

is a fact about the CLI's current *render* state, which this module has no
visibility into. The raw input byte stream — a single `0x1B` with nothing
else distinguishing it — is bit-for-bit identical in both cases. There is no
timing heuristic, preceding-byte pattern, or elapsed-time signal available in
the input stream alone that reliably separates them; anything built on it
would be guessing, not detecting.

### Why over-clear is the safer failure mode

The two possible wrong answers have asymmetric costs:

| Approach | Wrong on a **menu-dismiss** Esc | Wrong on a **real draft** Esc |
|---|---|---|
| Clear (current) | Draft state reads "empty" one keystroke early — at worst a stray injection lands on an empty line slightly sooner than a human would've resumed typing into it. No data loss. | (Not wrong — this is the case clearing is *for*.) |
| Don't clear | (Not applicable — menu-dismiss doesn't touch a real draft.) | `draft_len`/state stays stuck `"nonempty"` over text that no longer exists. Injection is silently blocked until `DRAFT_HOLD_TIMEOUT_S` (180s) forces a spill to the durable queue. |

Clearing too eagerly costs, at worst, a slightly-early injection into an
input line the user has genuinely moved on from — no worse than the
pre-A3 baseline this project already tolerated for months. Refusing to clear
when it should have costs up to 3 minutes of silently blocked delivery, which
is the *other* class of bug A3 was written to fix (delivery getting stuck
behind a draft that "isn't really there"). Given genuinely uncertain input,
the existing over-clear behavior is the fail-safe direction, so it stays
unchanged, per the task's own instruction to skip a guess that risks
regressing A3 rather than land one.

### What would be needed to actually fix this

Correctly distinguishing the two cases would require plumbing the Lead
pane's *render/output* text (not just input) into the draft-state decision —
e.g. a menu-open marker check similar to `pty_session.is_at_resume_picker()`
(#113's approach for detecting the `/resume` picker from rendered footer
text). That's a materially different, Qt/render-coupled data source than
this module currently touches, and `lead_draft_state.py` is deliberately
scoped as a "pure byte-level state machine — no Qt, no orchestrator" (see its
module docstring). Wiring that in is a larger, separate change than this
issue's scope — flagging it here for Lead to decide whether it's worth
opening as a new issue, not attempting it under #114.

## Tests / verification

- `tests/test_lead_draft_state.py`: `TestWordDeleteCtrlW`,
  `TestWordDeleteAltBackspace`, `TestBareEscFailSafeUnchanged` (regression
  pin for the deliberately-unchanged behavior, not an endorsement it's fully
  correct).
- Targeted suite green: `test_lead_draft_state.py` + `test_lead_draft_guard.py`
  + `test_done_notice_draft_churn.py` (87 tests, no A3 regressions).
- `ruff check` / `ruff format --check`: clean.
- `lint-imports`: 18/18 contracts kept.
