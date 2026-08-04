# Issue #146 live repro — shard qa#1 (of 3 concurrent shards)

Date: 2026-08-04
Pane: `qa#1` (spawned concurrently alongside qa#2 and qa#3)

## Result: Playwright MCP connect = **SUCCESS**

`mcp__playwright__browser_navigate` to `about:blank` succeeded — first attempt,
no timeout, no error. Wall-clock from just before the call to result: ~3s
(started 2026-08-04T17:30:03+07:00, snapshot stamped 2026-08-04T10:30:06Z =
17:30:06+07:00).

```
### Ran Playwright code
await page.goto('about:blank');
### Page
- Page URL: about:blank
### Snapshot
- [Snapshot](.playwright-mcp\page-2026-08-04T10-30-06-496Z.yml)
```

## Env vars (this pane)

```
TAKKUB_SHARD=1
TAKKUB_SHARD_TOTAL=3
TAKKUB_ROLE=qa#1
TAKKUB_BASE_ROLE=qa
MCP_TOOL_TIMEOUT=180000
```

## Shard-specific MCP config file — NOT FOUND

Checked `~/.agent-takkub/runtime/shared-mcp-agent-takkub-qa-shard1.json` —
does not exist. Only the non-sharded `shared-mcp-agent-takkub-qa.json`
(637B) and `shared-mcp-agent-takkub-critic.json` are present under
`~/.agent-takkub/runtime/` for this project — same as shard2's finding.

One odd transient observed while poking around `~/.takkub/` (a *different*,
wrong directory — not where the real config lives, see above): an `ls -la`
briefly showed a `shared-mcp-agent-takkub-qa.json` entry that a subsequent
`ls -la` in the same directory no longer showed, a few seconds later. This
is almost certainly me misreading stale output / wrong-directory confusion
on my end (`~/.takkub` vs `~/.agent-takkub/runtime/`), not a real
appear/disappear event — flagging only for completeness, **not** presenting
it as evidence of anything; not reproduced on repeat checks and the correct
directory (`~/.agent-takkub/runtime/`) was stable across 3 polls.

## Summary (1 line)

shard qa#1/3: **connect = SUCCESS** (~3s), no per-shard MCP config file exists for
this project (same gap shard2 found) — all 3 shards likely share one browser profile.
