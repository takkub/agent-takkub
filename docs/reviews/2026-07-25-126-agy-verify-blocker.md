# Issue #126 — agy account-verification submit blocker

## Finding

agy 1.1.6 can paint its normal idle footer while an account check is active.
During that interval the task text reaches the composer, but Enter is swallowed.
The ready detector therefore must treat the account-check text as a hard blocker.

The existing delivery implementation already covers agy: `_send_when_ready()`
has no provider gate around `_delayed_enter_verified()`, and passes both the
paste payload and the original task as `content_fragment`. When the pasted task
is still visible despite a not-ready verdict, the verifier keeps resending Enter
on its separate busy budget. No additional delivery wiring was needed.

## Transcript evidence

The two QA sessions named in the issue contain the same sequence:

- `runtime/sessions/2026-07-24/agent-takkub/qa-161527.transcript.log`
  (session report `qa-162839.md`)
- `runtime/sessions/2026-07-24/agent-takkub/qa-163220.transcript.log`
  (session report `qa-163733.md`)

In both raw transcripts:

1. The initial `[ROLE: qa]` task pointer is rendered in the composer.
2. agy then renders the exact account-check UI:
   - `⚠ Verifying your account...`
   - `We're finishing verifying your account eligibility.`
   - `This usually takes a moment. Please try again shortly.`
3. The idle-looking `? for shortcuts` / model footer remains visible.
4. After verification clears, the first task is still not running.
5. A second `[lead → qa]` delivery is rendered and is followed by
   `Generating...`.

Both transcripts also contain the startup text `⣷  Signing in...`; the spinner
glyph is transient, so the stable blocker substring is `signing in`.

## Change

- Added `verifying your account` and `signing in` to
  `gemini_spec.ready_hard_blockers`.
- Added ready-prompt regression coverage proving each account marker overrides
  agy's otherwise-ready footer, including provider-scoped doctor self-tests.
- Added agy-specific delivery coverage proving task assignment routes through
  `_delayed_enter_verified()` with the original task as `content_fragment` and
  a non-empty paste payload.

## Verification

Targeted tests:

```text
.venv\Scripts\pytest.exe -q tests\test_pty_ready_prompt.py tests\test_delivery_unconfirmed.py
```

Lint:

```text
.venv\Scripts\ruff.exe check src\agent_takkub\provider_spec.py src\agent_takkub\pty_session.py tests\test_pty_ready_prompt.py tests\test_delivery_unconfirmed.py
```
