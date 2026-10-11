# Active input and quota handover regression (#841, related #840)

The owner's screenshot shows Backend Codex working in `ai-vdo` while a new
cockpit Markdown pointer is under **Queued follow-up inputs**. This overlaps
the previously fixed #809; the screenshot alone does not prove that quota
handover caused that particular queue entry.

## Reproduced causes and changes

- `_delayed_enter_verified(steer=True)` tried Enter once, then changed retries
  to the provider's queue key. For Codex this is Tab, which postpones the
  correction until the turn ends. Every steering retry now remains Enter.
  Automatic notices retain their separate queue behaviour.
- Generic queue chrome could acknowledge a different, older message while a
  new correction remained in the composer. Verification now checks actual
  composer content first. When queue chrome is visible, matching the echoed
  message text is insufficient: it also appears above an empty composer.
- Human Lead inputs waited for the ordinary busy-notice window and used its
  queue mode. They now bypass that idle preference and request steering.
  Question pickers, trust/permission prompts and existing user drafts still
  hold injection. Concurrent human inputs retain FIFO priority over reports.
- Durable spill paths discarded the human flag. Human inputs now retain that
  flag across an unavailable Lead, failed writes and unverified session
  replacement. A late failure callback preserves occurrence order.
- Quota reroute lost the registered task's ID, task file, scope, backlog link,
  assignment timestamp, budget and confirmation digest. These now survive
  both the close/spawn gap and the replacement CLI. Queued corrections carry
  the task ID so continuation inputs are not labelled stale after recovery;
  messages belonging to a different assignment retain the stale warning.
- Recovery called `close()` and marked the still-continuing durable assignment
  `closed` (#840). Recovery now keeps that ledger entry open. Ordinary close
  still terminates the assignment.
- Direct handover bypassed fallback policy injection. Fallback context is now
  captured when delivery actually occurs, including after deferred spawn, and
  is cleared only after accepted delivery. Lead `send` and notice delivery
  also retain fallback policy after failed or unverified transport.
- The Lead takeover brief now explicitly requires `takkub context read`,
  `takkub context status` and `takkub team status` before substantive action,
  respects solo-lead/auto/full and visible cockpit assignment, and preserves
  transcript/output sources. Bootstrap failure requires reporting the failure.

## Validation (2026-10-11, Windows)

- 890 tests passed across 38 related files in 25.89 seconds. The selection
  covers changed submission helpers and their callers, quota/account recovery,
  durable messages, task ledger, Lead inbox/draft/modal guards, provider specs
  and actual `PtySession` screen parsing.
- Provider CLIs (`codex`, `agy`, `gemini`) were removed from the test subprocess
  PATH. Live `TAKKUB_*` variables were removed from that subprocess only; tests
  used isolated runtime/settings fixtures.
- A regression test feeds real terminal escape sequences into `PtySession`:
  old queue chrome plus a new draft requires another Enter; an empty composer
  with the new steering entry confirms submission without another paste/Tab.
- Ruff checks and `git diff --check` passed. Recovery/provider tests use test
  doubles; these results are not new live-provider or macOS evidence.

## Pre-push validation

The 185-file selection includes guards, issue/core contracts, references to
changed methods and orchestrator test doubles. With a temporary Windows user
profile, 6,273 tests passed and 10 skipped; two host-path isolation guard tests
correctly required the real home path instead of a directory already under
Temp. Re-running their five-test file with the ordinary subprocess profile
passed. Combined unique results: 6,275 passed, 10 skipped. Provider CLIs were
removed from PATH. The first attempt was deliberately stopped after identifying
a test scanning the real Codex session store; it is not counted as a pass.

Commit hooks also passed secret scanning, Ruff, docs verification, architecture
contracts, import collection and depgraph freshness.

## Provider and release limits

The existing [2026-10-07 live audit](2026-10-07-busy-input-steer-vs-queue.md)
records steering at a tool boundary for Claude 2.1.292, Codex 0.160 and
OpenCode 1.18.34. Gemini/agy 1.3.1 applies new input only after the turn ends;
Cursor remains unverified. ProviderSpec records those gaps and busy `send`
responses expose them. No automatic Escape interrupts an executing tool.

CLI submission/receipt is not proof that the model applied the instruction.
This patch does not claim application acknowledgement. The user authorized
shipping this batch as 2.2.14. Publication requires successful CI for the
release commit, including macOS and installed-wheel gates. The final CI and
publication evidence is recorded in #841 and the GitHub release. The running
cockpit is not restarted during validation.
