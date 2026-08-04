# Issue #146 live repro — shard qa#2 (of 3 concurrent shards)

Date: 2026-08-04
Pane: `qa#2` (spawned concurrently alongside qa#1 and qa#3)

## Result: Playwright MCP connect = **SUCCESS**

`mcp__playwright__browser_navigate` to `about:blank` returned immediately with a
clean result — no timeout, no error, no retry needed. Response:

```
### Ran Playwright code
await page.goto('about:blank');
### Page
- Page URL: about:blank
### Snapshot
- [Snapshot](.playwright-mcp\page-2026-08-04T10-29-08-865Z.yml)
```

No visible latency/hang before the tool returned (single call, no wait needed).

## Env vars (this pane)

```
TAKKUB_SHARD=2
TAKKUB_SHARD_TOTAL=3
TAKKUB_ROLE=qa#2
MCP_TOOL_TIMEOUT=180000
```

## Shard-specific MCP config file — NOT FOUND

Checked for `~/.agent-takkub/runtime/shared-mcp-agent-takkub-qa-shard2.json` —
**does not exist**. Only a non-sharded `shared-mcp-agent-takkub-qa.json` is present
for this project:

```json
{
  "mcpServers": {
    "playwright": {
      "type": "stdio",
      "command": "npx",
      "args": [
        "-y",
        "@playwright/mcp@0.0.75",
        "--user-data-dir",
        "C:\\Users\\monch\\.agent-takkub\\runtime\\browser-profiles\\agent-takkub-qa-playwright"
      ],
      "env": {}
    },
    "chrome-devtools": {
      "type": "stdio",
      "command": "npx",
      "args": [
        "-y",
        "chrome-devtools-mcp@0.26.0",
        "--userDataDir",
        "C:\\Users\\monch\\.agent-takkub\\runtime\\browser-profiles\\agent-takkub-qa-chrome-devtools"
      ],
      "env": {}
    }
  }
}
```

For comparison, a full listing of `~/.agent-takkub/runtime/shared-mcp-*.json`
shows other projects **do** have per-shard variants (e.g.
`shared-mcp-TK-ERP-qa-shard1.json`, `shared-mcp-TK-ERP-qa-shard2.json`,
`shared-mcp-TK-ERP-qa-shard3.json`, same pattern for `pms`, `unirecon`,
`wash-locker`, and `oracle` shard2/3) — but **agent-takkub has none**. Only
the plain `shared-mcp-agent-takkub-qa.json` and
`shared-mcp-agent-takkub-critic.json` exist for this project.

This raises the possibility that all 3 concurrently-spawned shards
(`qa#1`, `qa#2`, `qa#3`) for this project resolve to the **same**
`--user-data-dir` browser profile (`agent-takkub-qa-playwright`) instead of
isolated per-shard profiles — unconfirmed without checking the other two
shards' panes directly and without knowing which config file the spawn
mechanism actually loaded for each pane (not verified from inside this pane;
env/config files read from disk here, not proof of what was injected at
spawn time).

## Summary (1 line)

shard qa#2/3: **connect = SUCCESS**, but this project's shard MCP config has no
per-shard file — possible shared-profile collision candidate, not confirmed.
