# Codex Independent Cross-check

Date: 2026-06-12

Scope reviewed:

- `src/agent_takkub/`
- `src/agent_takkub/cli.py` and `src/agent_takkub/cli_server.py` (the repository has no top-level `cli/` directory)
- `tests/`

Excluded as requested:

- `docs/reviews/2026-06-12-full-system-review.md`
- `docs/reviews/2026-06-03-improvement-audit.md`

Verification: `python -m pytest -q` completed with 2 failures and 2 skips. Both failures are described under Low severity below.

## High

### H1. Unauthenticated `done` requests can close any teammate pane, including across projects

**Locations:** `src/agent_takkub/cli_server.py:145`, `src/agent_takkub/cli_server.py:194`, `src/agent_takkub/cli_server.py:256`, `src/agent_takkub/orchestrator.py:3424`, `src/agent_takkub/orchestrator.py:3565`

`done` is deliberately outside `_LEAD_ONLY_CMDS`. The server trusts caller-controlled `from` and `from_project`, then calls `Orchestrator.done()`. That method marks the named pane done, mutates pipeline/shard/auto-chain state, notifies Lead, writes session artifacts, and schedules the pane to close. Any local process that can read the loopback port can therefore impersonate an existing teammate. A teammate pane can also alter its own environment or send raw TCP to target another project.

This is more than message spoofing: it is an unauthenticated lifecycle transition and can prematurely advance pipelines or terminate active work.

**Fix:** give every pane a per-session capability bound to `(project, role, session-id)` and require it for `send` and `done`. Derive caller identity server-side from the capability rather than accepting `from`/`from_project` as authority. Reject missing identity instead of preserving the current raw-client fallback.

### H2. A late exit signal from an old PTY session can detach and mark a replacement session as exited

**Locations:** `src/agent_takkub/agent_pane.py:328`, `src/agent_takkub/agent_pane.py:344`, `src/agent_takkub/agent_pane.py:437`, `src/agent_takkub/agent_pane.py:448`, `src/agent_takkub/orchestrator.py:1494`, `src/agent_takkub/orchestrator.py:1573`, `src/agent_takkub/orchestrator.py:2124`

`AgentPane.attach_session()` connects every session's `processExited` directly to `self._on_exit`. `_on_exit()` does not check which session emitted the signal and calls `detach_session()`, which clears `self.session` (the current session). If an old reader thread emits after a replacement session has attached, the stale callback can set the pane to `exited` and detach the new live session. The generic orchestrator exit callback is similarly keyed only by role/project, so it can schedule auto-respawn based on state damaged by the stale pane callback.

The existing 2-second recovery delay reduces probability but does not establish ordering; `terminate()` waits only 500 ms per thread and explicitly allows threads to outlive the call.

**Fix:** capture the session in the signal connection and ignore callbacks unless `pane.session is emitting_session`. Pass session identity through `_on_session_exit` too. Disconnect the old session's `processExited` signal during teardown, and add a regression test where session A exits after session B attaches.

## Medium

### M1. `end-session` is documented Lead-only but bypasses the Lead capability token

**Locations:** `src/agent_takkub/cli_server.py:29`, `src/agent_takkub/cli_server.py:201`, `src/agent_takkub/cli_server.py:260`, `tests/test_end_session.py:273`, `tests/test_end_session.py:295`

`end-session` is in the client-side `LEAD_ONLY_COMMANDS` set but absent from server `_LEAD_ONLY_CMDS`. The server accepts both `from: "lead"` without `auth` and an empty/missing `from`. A raw local client can therefore forge Lead session summaries and daily-digest entries for any supplied project.

The tests encode this gap: the accepted Lead request contains no token, and manual terminals with no role are explicitly allowed.

**Fix:** add `end-session` to `_LEAD_ONLY_CMDS`, require `from == lead` plus a valid capability token, and update tests to reject missing/wrong auth.

### M2. TCP JSON framing has no type, size, connection, or partial-frame limits

**Locations:** `src/agent_takkub/cli_server.py:124`, `src/agent_takkub/cli_server.py:130`, `src/agent_takkub/cli_server.py:137`, `src/agent_takkub/cli_server.py:143`

The server waits for newline-delimited frames with no maximum buffered size or connection timeout. A client can hold many loopback connections open with unterminated data, or send a very large line that is decoded and parsed on the Qt UI thread. Valid JSON that is not an object (`[]`, `null`, strings) reaches `_dispatch()` and fails at `req.get()` outside the defensive `try`. Non-string values for `cmd`, `from`, or `auth` similarly raise before the protected dispatch body.

This allows local denial of service and UI stalls; PyQt slot exception handling is not a reliable containment boundary.

**Fix:** cap frames (for example 64 KiB), cap concurrent clients, close idle/oversized connections, require `isinstance(req, dict)`, validate a strict request schema before authorization, and keep all parsing/type failures inside one top-level exception boundary.

### M3. Delayed callbacks target a mutable pane slot instead of the session they were created for

**Locations:** `src/agent_takkub/orchestrator.py:2477`, `src/agent_takkub/orchestrator.py:3120`, `src/agent_takkub/orchestrator.py:3189`, `src/agent_takkub/orchestrator.py:3565`, `src/agent_takkub/orchestrator.py:3271`

Several `QTimer.singleShot` callbacks dereference `pane.session` or close by `(project, role)` when they eventually run. If the pane is closed and respawned before the callback fires, an Enter key or the delayed `done` close can hit the replacement session. The `done` timer is especially broad: it closes whichever pane currently occupies that role 2.5 seconds later.

**Fix:** capture the current session object/generation in every delayed callback and no-op if it no longer matches. For delayed close, pass an expected session or pane generation into `close()` and compare before terminating.

### M4. Peer message identity and project scope are forgeable

**Locations:** `src/agent_takkub/cli_server.py:33`, `src/agent_takkub/cli_server.py:177`, `src/agent_takkub/cli_server.py:245`, `src/agent_takkub/orchestrator.py:3185`, `src/agent_takkub/orchestrator.py:3201`

Only `from: lead` is guarded. Any local client can claim any teammate role and any `from_project`, inject commands/text into another pane, create forged CC messages to Lead, and alter `blocked_on_lead_ts`. Since pane agents paste received text into an interactive autonomous agent, forged peer messages can steer tool execution.

**Fix:** use the same per-pane authenticated identity proposed for H1. Treat `from` and project as derived claims, not payload fields. Apply message length/rate limits as a second layer.

### M5. Teammate environment allowlist includes a reusable Anthropic bearer credential

**Locations:** `src/agent_takkub/pane_env.py:31`, `src/agent_takkub/pane_env.py:57`, `src/agent_takkub/pane_env.py:101`

The module states that secret-bearing variables are kept out of teammate panes, but `_PANE_ENV_ALLOWLIST` includes `ANTHROPIC_AUTH_TOKEN`. Every teammate process and all of its subprocesses receive that token. A compromised MCP, dependency, or prompt-injected pane can read and exfiltrate it.

**Fix:** avoid passing reusable API/proxy credentials to untrusted teammate shells. Prefer provider login state or a scoped local broker. If proxy-token passthrough is required, make it an explicit opt-in and document that it weakens the isolation guarantee.

## Low

### L1. Full test suite currently fails because `terminate()` assumes `_pid` always exists

**Locations:** `src/agent_takkub/pty_session.py:405`, `src/agent_takkub/pty_session.py:418`, `tests/test_pane_transcript.py:141`, `tests/test_pane_transcript.py:160`

Both transcript teardown tests construct a minimal `PtySession` with `__new__`. `terminate()` accesses `self._pid` directly and raises before closing the transcript. Production construction initializes `_pid`, so this is primarily a regression/test-contract failure, but it also makes cleanup less defensive for partially initialized objects.

**Fix:** call `_tree_kill(getattr(self, "_pid", None))` and use defensive `getattr` consistently in teardown, or update the fixture to initialize `_pid`. The former better matches the method's best-effort cleanup contract.

## Test Gaps To Add

- Raw TCP `done` with forged role/project must be rejected.
- `end-session` must reject missing and wrong capability tokens.
- JSON arrays/scalars, non-string auth fields, oversized frames, and unterminated frames.
- Session A emits `processExited` after session B attaches; B must remain attached/alive.
- Delayed Enter and delayed done-close after a pane generation changes.
- Peer `send` cannot claim another role or project.

