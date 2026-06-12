# Fix Round 1 Independent Codex Review

Date: 2026-06-12

Scope:

- All uncommitted changes reported by `git status` and `git diff`
- Findings in `2026-06-12-full-system-review.md`
- Findings in `2026-06-12-codex-crosscheck.md`

Excluded as instructed:

- `docs/reviews/2026-06-12-fix-round1-review.md` was not opened

## Verdict: FAIL

Round 1 fixes a substantial part of the 15 findings, and the full test suite
passes, but four security/lifecycle defects remain. Two are incomplete fixes
for High findings. The TCP implementation also introduces a connection-cap
bypass that its regression tests do not exercise.

## Blocking Findings

### HIGH: stale Codex exit still reaches the generic exit handler

`src/agent_takkub/orchestrator.py:1618-1623` captures the Codex session but
unconditionally calls `_on_codex_exit`. `_on_codex_exit` then unconditionally
calls `_on_session_exit` at line 2129. Unlike the shell, Gemini, and Claude
callbacks, this path does not verify that the pane's current session is the
emitting session.

`AgentPane`'s generation guard protects the replacement pane from being
detached, but it does not protect orchestrator bookkeeping. A late Codex exit
can still overwrite `_recent_exits` for a replacement session and run generic
exit handling against the role/project slot.

Required fix: apply the same current-session identity check to the Codex
orchestrator callback, preferably inside `_on_codex_exit` before diagnostics
and `_on_session_exit`. Add a production-path test rather than a locally
re-created lambda.

### HIGH: pane capability tokens survive crash and failed spawn paths

Tokens are registered before `session.spawn()` at orchestrator lines
1439-1440, 1518-1519, 1576-1577, and 1790-1793. They are revoked only by
`close()` at lines 3364-3369.

Consequences:

- A `session.spawn()` failure leaves an indefinitely valid registered token.
- An unexpected PTY exit goes through `_on_session_exit`, which does not revoke
  the token.
- Auto-respawn registers another token without removing the crashed session's
  token.
- Any surviving child process or previously leaked token can continue calling
  authenticated `send`/`done` as that role after the owning session died.

This does not fully satisfy the per-session capability requirement from H1/M4.
Bind token lifecycle to the actual session and revoke it on every exit and
spawn failure. Tests must cover crash then respawn and verify the old token is
rejected.

### MEDIUM: delayed Enter callbacks still target a mutable pane slot

The delayed `done` close is correctly guarded, but several finding M3 paths
still dereference the pane's current session when their timers fire:

- auto slash command: orchestrator lines 2472-2476
- task injection: lines 2545-2549
- Lead notification pump: lines 3187-3190
- peer send: lines 3257-3262
- additional notice paths such as lines 3443, 3493, 4876, 4896, 4983, 5022,
  and 5030

If a pane is closed and replaced between paste and delayed Enter, the Enter can
be written to the replacement session. Capture the session used for the paste
and write Enter only if the pane still owns that same session.

The added regression test only reconstructs the done-close closure; it does
not drive these production callbacks.

### MEDIUM: TCP size/connection limits are bypassable

`cli_server.py:189` calls unbounded `readLine()` and checks length only after
the complete oversized frame has already been buffered and copied. No
`setReadBufferSize()` or `bytesAvailable()` limit exists for an unterminated
frame.

There is also a new connection-tracking bug: line 199 removes a socket from
`_open_connections` after its first complete frame while leaving the socket
connected. That socket no longer counts toward `_MAX_CONNECTIONS` and is no
longer reaped. A client can send one valid frame, then hold the connection or
stream an unterminated payload indefinitely; repeating this bypasses both caps.

Required fix:

- Keep every connected socket tracked until `disconnected`.
- Track last activity/frame state separately from connection membership.
- Set a bounded Qt read buffer and reject when `bytesAvailable()` exceeds the
  frame limit before a newline.
- Use a bounded `readLine(maxSize)` or equivalent.
- Add tests for an unterminated oversized frame, post-first-frame idle socket,
  and connection-cap enforcement after valid frames.

## Finding Matrix

| Source | Finding | Status | Notes |
|---|---|---|---|
| Full | H1 spawn arbiter flag leak | Fixed | All four spawn branches now reset/drain in `finally`. |
| Full | M1 hot.md triple scan | Fixed | Single-pass scanner plus per-file cache; metric logic matches prior counters. |
| Full | M2 bracketed-paste breakout | Fixed with test gap | ESC/bracket markers are removed on task/send/Lead notification paths. No direct sanitizer breakout test was added. |
| Full | L1 invalid projects JSON crash | Fixed | `JSONDecodeError` degrades to empty config and logs a warning. |
| Full | L2 TCP buffer/connection cap | Not fixed | Limits are bypassable as described above. |
| Full | L3 watchdog slot exception | Fixed | Per-pane exception containment added to idle and stuck watchdog loops. |
| Full | L4 read IPC threat model | Fixed by decision | `list`/`status` are explicitly documented as intentionally open under trust-local. |
| Cross | H1 forged unauthenticated done | Incomplete | Auth works for live panes, but stale session tokens remain valid. |
| Cross | H2 stale PTY exit | Incomplete | Pane guard works; Codex orchestrator callback remains unguarded. |
| Cross | M1 end-session auth | Fixed | Added to Lead-only server gate with token tests. |
| Cross | M2 TCP framing/schema caps | Incomplete | Type validation is fixed; actual size/connection containment is not. |
| Cross | M3 delayed mutable callbacks | Incomplete | Done-close fixed; delayed Enter paths remain. |
| Cross | M4 forged peer identity/project | Incomplete | Server derives identity from token, but stale tokens retain authority after session death. |
| Cross | M5 Anthropic bearer in pane env | Fixed | Removed by default and made explicit opt-in. |
| Cross | L1 defensive terminate | Fixed | Partial `PtySession` teardown is defensive and transcript tests pass. |

## Regression/Test Review

Commands run:

- `python -m pytest -q` - PASS, two existing skips
- `python -m compileall -q src tests` - PASS
- `git diff --check` - PASS

Test quality gaps:

- Stale-session and delayed-close tests in
  `tests/test_regression_findings_2026_06.py` reproduce small local closures
  instead of invoking production signal/timer paths. They can pass even when
  production wiring is wrong, as demonstrated by the unguarded Codex path.
- Oversized-frame test supplies an already complete in-memory line, so it
  cannot detect unbounded partial buffering or the post-first-frame tracking
  bypass.
- Identity tests assert only response success, not the actual arguments passed
  to `orchestrator.send()`/`done()`.
- No tests cover token revocation on crash, close, spawn failure, or respawn.

## Non-blocking Notes

- `_sanitize_pane_text` says it strips CR and LF, but implementation strips
  only CR. The bracketed-paste ESC breakout itself is closed, but sanitizer
  documentation and policy should be made consistent and direct tests should
  cover short and long payloads containing ESC start/end markers and control
  characters.
- Spawn bookkeeping exceptions now release the arbiter, but if an exception
  occurs after `attach_session`, `spawn()` returns failure while a live session
  may remain attached. This pre-existing cleanup ambiguity is made more visible
  by the broader `try`; cleanup should be considered while fixing token
  lifecycle, without widening scope into unrelated refactoring.
