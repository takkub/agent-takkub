# QA: shard mode (parallel fan-out)

Read this when your task was assigned via `takkub assign --role qa
--shards N "<task>"`.

`takkub assign --role qa --shards 3 "<task>"` → the orchestrator spawns
`qa#1`, `qa#2`, `qa#3` at the same time.

| Var | Example | Used for |
|---|---|---|
| `TAKKUB_ROLE` | `qa#2` | pane key (`takkub done`) |
| `TAKKUB_BASE_ROLE` | `qa` | role behavior identity |
| `TAKKUB_SHARD` / `_TOTAL` | `2` / `3` | shard index / total |

⚠️ **shards must never use `mb`/`mb-start-chrome` on any platform** — it
hardcodes CDP `127.0.0.1:9222` and doesn't read the port-file/env → every
shard collides on the same Chrome (#92). Sharded QA uses **Playwright MCP
only** (the cockpit already hands out a separate browser profile).

⚠️ **If ToolSearch can't find `browser_navigate`/`browser_snapshot`
(Playwright MCP not connected, #146/#304): don't retry for minutes.** One
retry (~30s) is enough — if it's still missing after that, this is a known,
unproven-root-cause failure mode, not something more retrying fixes:
1. Run `takkub mcp-fallback request --reason "playwright mcp not
   connected"` — if **granted**, you may use `mb` for **this task only**
   (still never `mb-start-chrome`); if **denied** (another shard already
   holds it), the reason tells you how long to wait.
2. Still stuck → report **FAILED** via `takkub done --fail "..."` (or your
   fail path) citing "Playwright MCP not connected, mb fallback
   denied/expired" — don't leave Lead waiting on a pane that will never
   recover on its own. `takkub doctor --pane $TAKKUB_ROLE` (from a pane
   where the CLI can reach the cockpit's `runtime/`) shows what the last
   spawn's MCP handshake actually looked like, if you want to cite evidence
   in the note.

**(A) `--plan --shards N`** (recommended): a planner pane (a bare `qa`
role) analyzes the app → splits it into N balanced, independent buckets →
writes a plan JSON → the orchestrator auto fans-out, injecting scope into
each shard's task (a `━━ SHARD n/N SCOPE ━━` block) — the shard **reads the
scope it was given, doesn't self-select** — and tests only that scope:
```json
{"shards": [{"n": 1, "scope": "/login, /signup", "focus": "invalid creds + rate limit"}]}
```

**(B) bare `--shards N`** (no planner): no scope block → split it yourself
using `TAKKUB_SHARD`/`TAKKUB_SHARD_TOTAL` (modulo `(index % TOTAL) ==
(SHARD-1)`) · if the plan can't be read → it degrades to (B) automatically
and warns Lead, but the work keeps going.

The done report must include the shard index (the orchestrator merges N
shards into 1 handoff):
```bash
takkub done "shard $TAKKUB_SHARD/$TAKKUB_SHARD_TOTAL: หน้า /login /dashboard ผ่าน · shots: $SHOT_DIR"
```
