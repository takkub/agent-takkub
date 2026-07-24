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

## Round 2 — post-submit recovery

The first fix (`690a1b7`) correctly prevents delivery while an account gate is
already visible during boot, but it cannot cover a gate triggered by the
request being submitted. The decisive runtime evidence is:

- `runtime/sessions/2026-07-24/agent-takkub/gemini-173055.transcript.log`
- line 47: agy is at its normal `? for shortcuts` ready footer.
- lines 48–49: cockpit renders the complete `[ROLE: gemini ...]` smoke-test
  task in the composer.
- line 51 onward: the idle footer remains visible after submit.
- lines 59–61: only then does agy paint `⚠ Verifying your account...` and the
  Google eligibility explanation.
- the transcript ends back at the idle composer without `Generating...`, a
  model response, or `takkub done`.

This order proves the first request itself triggered eligibility verification
and was consumed. A pre-send blocker cannot prevent that sequence by design.

### Change

- Added provider-declared `post_submit_recovery_markers`, working markers, a
  90-second observation window, and a two-redelivery bound to `ProviderSpec`.
  Only agy/gemini currently declares a recovery marker; all other providers
  take the generic no-op path (#103).
- After normal ready-gated paste plus verified Enter, task delivery arms the
  short watcher. If `verifying your account` appears and then clears back to
  ready, it sends the entire original task through `_send_when_ready()` again
  and logs `task_redeliver_after_verify`.
- Each re-delivery carries its attempt count, so at most two re-pastes occur.
- `Thinking`, `Generating`, and interrupt/cancel status markers cancel
  recovery. The re-delivery also captures the exact session and aborts if it
  is no longer ready at its first delivery poll, closing the race where real
  work starts between the watcher verdict and paste.
- Provider marker scanning uses the same bottom-row status region as ready
  detection, preventing task/body text that quotes the marker from triggering
  recovery.

### Round 2 verification

Targeted tests cover:

- ready → deliver → verification appears → clears → full-task re-delivery;
- verification clears into a real work marker → no re-delivery;
- work starts before the guarded re-delivery poll → no re-delivery;
- two-retry cap and providers without recovery markers → no-op;
- footer-scoped provider marker matching.

```text
PYTHONPATH=src pytest -q tests/test_delivery_unconfirmed.py tests/test_pty_ready_prompt.py
python -c "import sys, pytest; sys.path.insert(0, 'src'); raise SystemExit(pytest.main(['-q']))"
ruff check src/agent_takkub/provider_spec.py src/agent_takkub/pty_session.py src/agent_takkub/lead_inbox.py tests/test_delivery_unconfirmed.py tests/test_pty_ready_prompt.py
ruff format --check src/agent_takkub/provider_spec.py src/agent_takkub/pty_session.py src/agent_takkub/lead_inbox.py tests/test_delivery_unconfirmed.py tests/test_pty_ready_prompt.py
```

Both the targeted suite and the full repository suite pass. The full-suite
launcher inserts this worktree's `src` only in the parent pytest process rather
than exporting `PYTHONPATH`, so installed-mode subprocess tests still exercise
their isolated wheel/venv as intended.

## Round 3 — root-cause warm-up ping

The post-submit recovery from round 2 is a safety net that catches swallowed tasks and re-delivers them, but it's treating the symptom rather than preventing it. The root cause is that Google's server-side eligibility check sometimes swallows the *very first* request submitted by a fresh AGY session, silently doing nothing after the verify banner clears. User prefers to prevent the first task from being swallowed.

### Change

- Added a `needs_warmup_ping: bool = False` capability flag to `ProviderSpec` and enabled it for AGY (`gemini`).
- The task delivery watcher (`_send_when_ready`) detects this flag on fresh spawns. Before sending the actual queued task, it sends a sacrificial "ready check — ตอบ ok สั้นๆ" payload and waits for it to finish.
- If the ping turn finishes normally or clears a verification banner, the watcher observes the return to the ready prompt and then proceeds to send the real task.
- Added a 120-second hard timeout to the ping turn. If it hangs, the watcher falls back to sending the real task anyway.
- Event logs record each step (`warmup_ping_sent`, `warmup_ping_ok`, `warmup_ping_timeout`).
- The round-2 `post_submit_recovery` remains fully active as a secondary safety net for both the ping and the real task.

### Round 3 verification

Targeted test suite updated to cover:
- Providers that don't need warmup (like Claude) route tasks normally.
- Fresh AGY sessions send the warmup ping, wait, and sequence the real task correctly.
- Hard timeouts trigger the fallback routing to ensure tasks don't get permanently stuck in a stalled ping.
