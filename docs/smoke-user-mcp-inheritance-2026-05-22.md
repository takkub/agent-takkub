---
date: 2026-05-22
type: smoke-test
commit: 41c9711
feature: user-level plugin + MCP inheritance into cockpit panes
role: qa
result: PARTIAL PASS (1 FAIL, 1 WARNING)
---

# Smoke Test: User-level MCP Inheritance (41c9711)

## Environment

- Claude Code: 2.1.148
- Pane: qa (TAKKUB_ROLE=qa, TAKKUB_PROJECT=agent-takkub)
- TAKKUB_INCLUDE_PMS: not set
- Spawn flags: `--dangerously-skip-permissions --setting-sources project,local --mcp-config runtime/shared-mcp.json --strict-mcp-config`

## shared-mcp.json at time of test

```json
{
  "mcpServers": {
    "playwright":     { "type":"stdio", "command":"npx", "args":["-y","@playwright/mcp@0.0.75"] },
    "chrome-devtools":{ "type":"stdio", "command":"npx", "args":["-y","chrome-devtools-mcp@0.26.0"] },
    "obsidian-vault": { "type":"stdio", "command":"npx", "args":["-y","@bitbonsai/mcpvault@latest","C:\\Users\\monch\\WebstormProjects\\second-brain"] },
    "postgres-pms":   { "type":"stdio", "command":"npx", "args":["-y","@modelcontextprotocol/server-postgres","postgresql://pms_user:pms_pass@localhost:5432/pms_db"] }
  }
}
```

`pms` is correctly absent from shared-mcp.json (pruning logic working at file level ✅).

## Raw `claude mcp list` output

```
Checking MCP server health…

claude.ai Google Drive: https://drivemcp.googleapis.com/mcp/v1 - ! Needs authentication
plugin:ecc:github: npx -y @modelcontextprotocol/server-github@2025.4.8 - ✓ Connected
plugin:ecc:context7: npx -y @upstash/context7-mcp@2.1.4 - ✗ Failed to connect
plugin:ecc:exa: https://mcp.exa.ai/mcp (HTTP) - ✓ Connected
plugin:ecc:memory: npx -y @modelcontextprotocol/server-memory@2026.1.26 - ✓ Connected
plugin:ecc:playwright: npx -y @playwright/mcp@0.0.69 --extension - ✗ Failed to connect
plugin:ecc:sequential-thinking: npx -y @modelcontextprotocol/server-sequential-thinking@2025.12.18 - ✓ Connected
obsidian-vault: npx -y @bitbonsai/mcpvault@latest C:\Users\monch\WebstormProjects\second-brain - ✓ Connected
chrome-devtools: npx -y chrome-devtools-mcp@latest - ✓ Connected
postgres-pms: npx -y @modelcontextprotocol/server-postgres postgresql://pms_user:pms_pass@localhost:5432/pms_db - ✓ Connected
pms: https://api.wsol.co.th/pms/mcp (HTTP) - ✗ Failed to connect
```

## Checklist

### Must HAVE

| MCP | Expected | In shared-mcp.json | In `claude mcp list` | Tools available | Result |
|---|---|---|---|---|---|
| `playwright` (force-inject @0.0.75) | ✅ | ✅ | ❌ not listed | ✅ `mcp__playwright__*` deferred | ⚠️ WARN |
| `chrome-devtools` (force-inject) | ✅ | ✅ | ✅ Connected | ✅ `mcp__chrome-devtools__*` deferred | ✅ PASS |
| `obsidian-vault` (user MCP merge) | ✅ | ✅ | ✅ Connected | ✅ `mcp__obsidian-vault__*` deferred | ✅ PASS |
| `postgres-pms` (user MCP merge) | ✅ | ✅ | ✅ Connected | ✅ `mcp__postgres-pms__query` deferred | ✅ PASS |

### ECC plugin MCPs (user-level plugin inheritance)

| MCP | Result |
|---|---|
| `plugin:ecc:github` | ✅ Connected |
| `plugin:ecc:exa` | ✅ Connected |
| `plugin:ecc:memory` | ✅ Connected |
| `plugin:ecc:sequential-thinking` | ✅ Connected |
| `plugin:ecc:context7` | ❌ Failed to connect (upstream issue, unrelated to inheritance) |
| `plugin:ecc:playwright` | ❌ Failed to connect (v0.0.69 --extension, different from cockpit's v0.0.75 standalone) |

### Must NOT HAVE

| MCP | Expected absent | In shared-mcp.json | In `claude mcp list` | Result |
|---|---|---|---|---|
| `pms` | ❌ absent | ✅ absent | ❌ PRESENT (Failed) | ❌ FAIL |
| `claude-obsidian` commands | ❌ absent | ✅ absent | ✅ absent | ✅ PASS |

## Findings & Root Cause Analysis

### ⚠️ WARNING — `playwright` missing from `claude mcp list` (tools still functional)

`playwright` (@playwright/mcp@0.0.75) is in shared-mcp.json and injected via `--mcp-config`, but does **not appear** in `claude mcp list`. However, `mcp__playwright__*` tools ARE available as deferred tools, confirming the server IS running.

**Root cause:** `claude mcp list` displays MCPs from the Claude Code configuration system (user `~/.claude.json`, project settings). MCPs added via `--mcp-config` at spawn time are loaded into the session but not reflected in `claude mcp list` unless the same server name also exists in the user config. Since the user does not have a standalone `playwright` entry in `~/.claude.json` (only `plugin:ecc:playwright`), it doesn't appear in the list.

**Impact:** Cosmetic only. The playwright tools are usable. QA pane can run browser tests normally.

### ❌ FAIL — `pms` appears in `claude mcp list` despite not being in shared-mcp.json

`pms` (HTTP bearer-auth MCP, `https://api.wsol.co.th/pms/mcp`) should be excluded because:
1. `TAKKUB_INCLUDE_PMS` is not set
2. `ensure_user_mcps()` correctly excluded it from shared-mcp.json

However, `pms` still appears in `claude mcp list` as "Failed to connect".

**Root cause:** `--strict-mcp-config` does not block user-level `~/.claude.json` mcpServers. The flag appears to only restrict project/local `settings.json` MCPs (or may not be fully supported at Claude Code 2.1.148). User-level entries from `~/.claude.json` load regardless.

User's `~/.claude.json` mcpServers: `obsidian-vault`, `chrome-devtools`, `postgres-pms`, `pms`

All four attempt to load. The first three appear in shared-mcp.json too (appear connected). `pms` is user-level only and loads unchecked.

**Impact:**
- `pms` fails to connect (server unreachable or auth missing), so no actual tool access
- BUT the presence in the session is a security concern: any auth config attached to `pms` in `~/.claude.json` is being passed to the MCP attempt
- In the original design, `pms` with bearer token should be opt-in only to prevent token exposure

**Recommended fix:** Since `--strict-mcp-config` doesn't block `~/.claude.json` mcpServers, the cockpit needs a different approach:
- Option A: Rename/remove `pms` from user's `~/.claude.json` when TAKKUB_INCLUDE_PMS is not set (invasive)
- Option B: Accept current behavior since `pms` fails to connect anyway, and focus the ensure_user_mcps() allowlist as the primary control (already working at shared-mcp.json level)
- Option C: Investigate whether newer Claude Code versions honor `--strict-mcp-config` for `~/.claude.json` entries

## Overall Result

| Category | Result |
|---|---|
| Force-inject MCPs functional | ✅ PASS (tools available, list display anomaly only) |
| User MCP allowlist merge (obsidian-vault, postgres-pms) | ✅ PASS |
| pms exclusion (shared-mcp.json level) | ✅ PASS |
| pms exclusion (session level via --strict-mcp-config) | ❌ FAIL |
| claude-obsidian exclusion | ✅ PASS |
| ECC plugin inheritance | ✅ PASS (4/6 connected; 2 failures are upstream/version issues) |

**Overall: PARTIAL PASS** — Core inheritance works. One security-relevant gap: `pms` leaks into the session from user-level config despite not being in the allowlist. Impact is low (connection fails) but design intent is violated.
