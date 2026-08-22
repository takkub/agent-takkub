# #346 — agy "Verifying your account..." misread as READY, task blind-pasted and lost

**Status:** fixed (proven, targeted tests). **Severity:** high (silent task
loss + actively misleading diagnosis pointing at the wrong bug class).
**Files:** `pty_session.py`, `provider_spec.py`, `orchestrator.py`,
`lead_inbox.py`, `maintenance.py`.

## Root cause (proven, not assumed)

The issue reported `blocked:trust-prompt` + an auto-answered Enter + an
eventual `delivery-uncertain` for a gemini/agy pane frozen on:

```
⚠ Verifying your account...
  We're finishing verifying your account eligibility.
  This usually takes a moment. Please try again shortly.
>
```

`is_at_trust_prompt()` (the detector behind `blocked:trust-prompt`) requires
`"trust this folder"` in the screen text — this banner never contains that
phrase, so a direct repro against the exact captured screen proved
`is_at_trust_prompt()` returns `False`, `is_blocked_on_tty_prompt()` and
`is_blocked_on_permission_prompt()` return `None` too. The literal
`blocked:trust-prompt` label in the issue's evidence does not trace to any
code path touched by this fix; treat it as the reporter's best paraphrase of
what the incident *felt* like, not a precise code citation.

The actual bug, found by running the exact issue screen through every
`PtySession` classifier:

```
is_at_ready_prompt(): True   <-- the real bug
auth_failure_reason("gemini"): None
is_hard_blocked_for("gemini"): False
```

`is_at_ready_prompt()` returning `True` for a screen that is visibly, verbatim
telling the operator "please try again shortly" (i.e. *not* accepting input)
is the actual defect. Traced to commit `d5ab420` (2026-07-25), which added,
on an unproven theory:

```python
ReadyRule("please try again shortly", True)  # provider_spec.py, gemini_spec
```

plus a matching bypass in `_classify_ready` / `_classify_ready_for_provider` /
`is_hard_blocked_for`:

```python
if b == "verifying your account" and "please try again shortly" in text_lower:
    continue  # i.e. NOT hard-blocked
```

and a self-test case asserting this exact banner text is `ready=True`. The
theory was: "if the account check failed, agy drops back to a normal idle
composer, and 'please try again shortly' is the tell." Issue #346 is direct
field evidence that theory is wrong — the CLI can show precisely this text
while genuinely frozen, accepting no input at all.

**Mechanism of the actual incident:** `is_at_ready_prompt() == True` made
`lead_inbox._send_when_ready`'s `_check` loop treat the pane as idle after
its usual consecutive-ready-poll streak, and call `_deliver()` with
`unconfirmed=False` — the NORMAL (non-blind-paste-guarded) delivery path.
The task text + submit Enter were written straight into the banner. Nothing
there could process it; the task was lost outright. Whatever
self-heal/verify logic ran afterward correctly could not confirm the paste
landed, producing the `delivery-uncertain` outcome the issue describes.

## What was fixed

1. **Removed the false ready classification** — `ReadyRule("please try
   again shortly", True)` deleted from `gemini_spec.ready_rules`; the
   `"verifying your account"` + `"please try again shortly"` bypass removed
   from `_classify_ready`, `_classify_ready_for_provider`, and
   `is_hard_blocked_for` (`pty_session.py`). The shipped self-test case for
   this exact banner now asserts `ready=False`. Confirmed via direct repro:
   `is_at_ready_prompt()` now `False`, `is_hard_blocked_for("gemini")` now
   `True` for the issue's live-captured screen.

2. **New distinct state: `blocked:provider-account`** — deliberately NOT
   folded into `is_at_trust_prompt()`'s "trust"/"tty"/"permission" set (those
   assume an answerable prompt safe to Enter-through) and NOT folded into
   the existing `login-required` state (that implies a credentials problem
   fixable by logging in again, which is actively wrong advice here — the
   gate is external, on the provider's own backend).

   - `ProviderSpec.account_pending_markers` (new field, `provider_spec.py`)
     — gemini's `("verifying your account",)`. Moved OUT of
     `auth_transient_markers` (which now holds only the two genuine sign-in
     markers, `"signing in"` / `"not signed in"`).
   - `PtySession.account_pending_reason(provider)` (new) — same grace-gating
     shape as `auth_failure_reason()`'s transient tier
     (`AUTH_TRANSIENT_GRACE_SEC`, spinner-normalized via
     `seconds_since_output()`), so a normal cold boot never false-positives.
   - `Orchestrator._derive_display_state` — new tier, checked ahead of
     `login-required`, returns `"blocked:provider-account"`.
   - `lead_inbox._send_when_ready`'s `_check` — new fast-fail branch
     (mirrors the existing `auth_failure` branch: `_AUTH_FAILURE_CONFIRM_POLLS`
     consecutive polls, ready-prompt wins over a stale marker), never
     blind-pastes, routes straight to `_recover_account_pending_pane` (close
     + respawn + degrade-to-claude via the shared `_recover_broken_pane`,
     `kind="account_pending"`).
   - `_warn_lead_account_pending` (new) — fires unconditionally (like
     `_warn_lead_auth_failure`'s first notice, NOT gated behind the
     pane-health "live" policy the auth-failure *degrade* notice uses —
     gating this would silently defer it to pane-close under the default
     `terminal` policy, defeating the point of a fast, concrete diagnosis).
     Wording explicitly says this is *not* a login/credentials problem and
     names the two real remedies: wait and retry, or use a different
     provider — the automatic degrade-to-claude respawn already does the
     second one.

## Multi-provider generality (#103)

Checked every registered provider's `auth_error_markers` /
`auth_transient_markers` for a similar account-side gate before assuming
this is agy-only:

- claude, codex, opencode, cursor: `auth_error_markers=()`, no
  `auth_transient_markers` set — nothing confirmed, nothing to migrate.
- kimi: `auth_error_markers=("send /login to login",)` — a genuine, instant
  login failure (no model selected, nothing will run), not an
  account/eligibility gate. Correctly stays where it is.
- gemini/agy is the only provider with a confirmed account-pending gate as
  of this round. `account_pending_markers_for()` returns `()` for every
  other provider until a real screen is captured for one — never guessed.

## Tests

- `tests/test_pty_ready_prompt.py` — new regression test feeding the exact
  live-captured issue #346 screen, asserting `is_at_ready_prompt() is False`
  AND `is_at_trust_prompt() is False` AND `is_blocked_on_tty_prompt() is
  None` (proves this incident does not resurface as `blocked:trust-prompt`
  either). `ready_marker_selftest()`'s shipped-table case flipped to
  `False` with the correction documented inline.
- `tests/test_auth_failure_detection.py` — new `TestAccountPendingReason`
  (grace-gating, full live banner, provider isolation, disjoint from
  `auth_failure_reason`); `TestIsHardBlockedFor`'s carve-out test flipped to
  assert `True` (was asserting the now-removed bypass).
- `tests/test_derive_display_state.py` — new priority-order tests
  (`blocked:provider-account` beats `login-required`, falls through when
  absent) and a real-transcript test using the verbatim issue screen.
- `tests/test_delivery_account_pending.py` (new file) — full `_check`
  integration mirroring `test_delivery_auth_failure.py`'s shape: ready-prompt
  wins over a stale marker, streak reset on interruption, requires
  `_AUTH_FAILURE_CONFIRM_POLLS` consecutive polls, fires the correct Lead
  wording exactly once, never blind-pastes, routes to
  `_recover_broken_pane(..., kind="account_pending")`.

Ran targeted (per project convention — full suite is qa's batch-gate job):
the files above plus every test file touching `is_at_trust_prompt`,
`is_at_ready_prompt`, `auth_failure_reason`, `is_hard_blocked_for`,
`_derive_display_state`, and the delivery/`_send_when_ready` polling loop —
all green (`takkub qa-gate --targeted`, PYTHONPATH=src override for this
worktree's shared-venv checkout mismatch, see repo-known #202 issue).
`ruff check` clean on every touched file.

## What was **not** done, and why

- No new `quota_marker`-style extra field on `list_status_detailed`'s
  per-role dict for the account-pending reason text — the Lead notice
  already carries the concrete marker text, and the state is short-lived
  (auto-recovers within ~46s via close+respawn), so a bare
  `blocked:provider-account` in `takkub status` is enough signal, consistent
  with how the existing `login-required` state also carries no extra detail
  column.
