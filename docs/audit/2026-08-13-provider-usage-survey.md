# Provider usage/limit survey (2026-08-13)

Spike, not implementation. Goal: for each provider CLI the cockpit can spawn
(claude, codex, gemini/agy, opencode, kimi, cursor), find out **whether** and
**how** we could surface "usage / remaining quota / reset time" in the UI,
without calling any live provider endpoint that could trip rate limits or
require login. All findings below are backed by an actual command run or file
read on this machine (dev box, 2026-08-13) — none are from memory or docs.

No source code was changed. No login/logout was run. No state-changing
command was run. `claude`'s live `fetch_usage()` was never called directly
(per task instructions) — only `limit_status.py` was read.

## Environment

CLIs actually present on PATH on this machine (`command -v`):

| provider | binary found | version |
|---|---|---|
| claude | `...\npm\claude` | 2.1.229 |
| codex | `...\npm\codex` | codex-cli 0.146.0 |
| gemini (agy) | `...\Local\agy\bin\agy` | 1.1.12 |
| opencode | `...\npm\opencode` | 1.18.16 |
| kimi | `...\.local\bin\kimi` | 1.49.0 |
| cursor | **not found** | — |

Cursor's `cursor-agent`/`agent` binary is not installed here — only the
unrelated Cursor desktop IDE (`~/.cursor`) is present. Per
`src/agent_takkub/provider_spec.py::_discover_cursor`, the cockpit looks for
`cursor-agent`/`cursor-agent.exe`/`agent`/`agent.exe` on PATH. Cursor is
therefore **not empirically tested** in this survey — reported as untested,
not as "no channel exists".

---

## claude

**1. Channel that exists:** local credential file
(`~/.claude/.credentials.json`, or the macOS Keychain entry
`"Claude Code-credentials"`) read by `limit_status.py`, then an authenticated
HTTP GET to the undocumented endpoint `https://api.anthropic.com/api/oauth/usage`
with the OAuth bearer token. No CLI subcommand or flag exposes this — verified
via `claude --help | grep -i "usage|limit|status|quota"` → no matches. This is
100% the mechanism already implemented in this repo
(`src/agent_takkub/limit_status.py`); nothing new found.

**2. Fields:** three rolling windows (`five_hour`, `seven_day`,
`seven_day_sonnet`), each with `utilization` (float % or `None` if the
response omitted it) and `resets_at` (ISO datetime); plus `plan` label
(Free/Pro/Max 5x/Max 20x) and `extra_usage_enabled` (bool).

**3. Stability:** structured JSON, but an **undocumented, private** endpoint.
Already proven fragile in production for this exact project — see
`usage-endpoint-hardened-shared-backoff` project memory: as of 2026-07-17 it
began returning 403/429 aggressively with `Retry-After` up to ~60 min,
re-armed by every further hit. `limit_status.py` now has a whole
cross-process shared-state/backoff layer (`LimitStore`, `fetch_usage_shared`)
built specifically to survive that. Any UI work here **must** go through
`fetch_usage_shared()` / `LimitStore`, never `fetch_usage()` directly.

**4. Auth:** requires a valid OAuth access token (refreshed via
`console.anthropic.com/v1/oauth/token` if expired, using the stored refresh
token). No credentials file / no Keychain entry / expired token with no
refresh token → `fetch_usage()` returns `None` cleanly (never fabricates
0%). Confirmed by reading the function; not re-tested live.

---

## codex

**1. Channel that exists:** the `codex app-server` JSON-RPC protocol (stdio),
via the request method `account/rateLimits/read` (params: `null`) and a
matching push notification `account/rateLimits/updated`. This is a real,
versioned part of the protocol — confirmed by generating the protocol's own
JSON Schema bundle:

```
codex app-server generate-json-schema --out <dir>
```

`ClientRequest.json` contains the `"account/rateLimits/read"` method entry;
`ServerNotification.json` contains `"account/rateLimits/updated"` →
`AccountRateLimitsUpdatedNotification` → `RateLimitSnapshot`. Independent
corroboration: `~/.codex/config.toml` has
`[tui].status_line = [..., "five-hour-limit", "weekly-limit", "used-tokens", ...]`
— the interactive TUI already renders this data from the same internal state,
proving codex tracks it even though no plain CLI flag surfaces it. No
`codex usage`/`codex status`/`codex limits` subcommand exists (checked
`codex --help`, `codex features`, `codex debug --help`).

**2. Fields (from `RateLimitSnapshot` in the generated schema):**
- `primary` / `secondary`: each a `RateLimitWindow` = `{usedPercent (int, required), resetsAt (epoch, nullable), windowDurationMins (nullable)}` — these are the 5-hour and weekly windows the TUI status line labels.
- `planType`, `limitId`, `limitName`
- `credits`: nested `CreditsSnapshot`
- `individualLimit`: nested `SpendControlLimitSnapshot` = `{limit, used, remainingPercent, resetsAt}` (all required)
- `rateLimitReachedType`: enum `rate_limit_reached | workspace_owner_credits_depleted | workspace_member_credits_depleted | workspace_owner_usage_limit_reached | ...`
- `spendControlReached`: nullable bool

**3. Stability:** this is a **generated, versioned JSON-RPC schema**
(`codex_app_server_protocol.schemas.json`) — the most structurally stable
option of all six providers, in principle. Two caveats: (a) the whole
`app-server` surface is labelled `[experimental]` in `codex --help` itself, so
its shape can still change across codex-cli releases without a deprecation
window; (b) reading it requires standing up a JSON-RPC session over stdio
(spawn `codex app-server`, do the `initialize` handshake, send the request) —
meaningfully more integration work than a single HTTP GET or a `--json` flag.
**Not live-verified in this spike** — I generated and inspected the schema and
read the config/doctor evidence, but did not actually open an app-server
session and call `account/rateLimits/read`, since that would mean hand-rolling
a JSON-RPC client for a one-off spike probe; recommend doing that as a small
throwaway script in implementation, not here.

**4. Auth:** `codex login status` → `Logged in using ChatGPT` (read-only,
confirmed working). `codex doctor --json` confirms
`"stored ChatGPT tokens": "true"`, `"stored auth mode": "chatgpt"` without
exposing the token itself. Credentials live in `~/.codex/auth.json`
(`auth_mode`, `OPENAI_API_KEY` (null here), `tokens.{id_token,access_token,
refresh_token,account_id}`, `last_refresh`). Not tested: what
`account/rateLimits/read` returns when logged out.

---

## gemini (agy / Antigravity)

**1. Channel that exists:** **no CLI subcommand or flag** — `agy --help`'s
full subcommand list is `agent, agents, changelog, help, install, models,
plugin, plugins, update`; none of them touch quota. Instead there is a local
**cache file the agy/Antigravity client itself writes** after it polls quota
internally:

- `~/.antigravity_cockpit/cache/quota_api_v1_plugin/authorized/<hash>.json` — raw cached copy of the provider's quota API response (~21KB, one file per logged-in account/session hash).
- `~/.antigravity_cockpit/cache/quota_history/<hash>.json` — compact time-series roll-up of the same data (~1KB).

Both were read directly (well under the 300KB read-without-grep threshold).

**2. Fields:**
- `quota_api_v1_plugin` file: `{version, source, customSource, email, projectId, updatedAt, payload.models.<modelId>: {displayName, ..., quotaInfo: {remainingFraction, resetTime}, apiProvider, modelProvider, ...}}`. Notably this file lists quota **per model**, and on this machine that included not just Gemini models (`gemini-3-flash`, `gemini-3.1-pro-high`) but also `claude-sonnet-4-6` — Antigravity multiplexes several backend models under one subscription's quota pool.
- `quota_history` file: `{email, updatedAt, models.<modelId>: {modelId, label, points: [{timestamp, remainingPercentage, resetTime, countdownSeconds}], hasCountdownDropAt100}}`.

**3. Stability:** structured JSON with a `version`/`source` header, which
looks like a genuine cached API response rather than TUI-scraped text — but
it is an **undocumented internal cache path**, the filename is a hash (one
per account, discovered by directory listing, not a stable name), and
critically **freshness is not guaranteed**: the sample found here has
`updatedAt` = 2026-02-27, i.e. roughly 5.5 months stale relative to today
(2026-08-13) — meaning the file only updates whenever the Antigravity
IDE/agy CLI last happened to poll quota, and there is no CLI command found
that forces a fresh read on demand. Any consumer must treat this as "last
known, possibly very stale" telemetry, not live state.

**4. Auth:** file presence + populated `email` field imply an authenticated
session exists (this file is written post-login by the client). Not
independently verified what happens pre-login (no fresh un-authenticated
profile was available to test against without logging out, which is
forbidden this spike).

---

## opencode

**1. Channel that exists:** a real, documented local subcommand —
`opencode stats` ("show token usage and cost statistics") — plus direct
read-only SQL access to opencode's own sqlite store via
`opencode db "<SELECT ...>" --format json` (path from `opencode db path`).
Both were run.

**2. Fields (from `opencode stats --days 7`):** Sessions, Messages, Days,
Total Cost, Avg Cost/Day, Avg/Median Tokens per Session, Input, Output, Cache
Read, Cache Write (token counts). **Important:** this is opencode's own
**self-tallied spend/token counter**, computed client-side from each
response's usage field — confirmed by inspecting the sqlite schema:
`message.data` is an opaque JSON blob (no dedicated usage/limit columns), and
the only account-related table, `account_state`, holds just
`active_account_id`/`active_org_id` pointers — there is **no quota or
rate-limit table anywhere in opencode's local DB**. So `opencode stats` can
never answer "how much of my plan's quota is left" — only "how much did I
send/receive through opencode so far" (and cost was $0.00 here, i.e. it isn't
even tracking an underlying paid metered key on this box).

**3. Stability:** `opencode stats` is a documented, versioned CLI subcommand
with a stable flag surface (`--days`, `--tools`, `--models`, `--project`) —
but it renders a **plain text table only**, no `--json`/`--format` flag on
`stats` itself (would need to be text-scraped for parsing). `opencode db` does
give real `--format json` output, but that means depending on an internal
sqlite schema (table/column names) that opencode does not document as a
stable API and can change silently between releases.

**4. Auth:** `opencode providers list` reported **0 stored credentials** on
this machine — this cockpit's opencode instance is driven via env-var API
keys rather than opencode's own login flow, and `opencode.jsonc` is empty
(`{"$schema": ...}` only, no provider config). `stats` still worked because it
reads local session records regardless of how auth was supplied.

---

## kimi

**1. Channel that exists: none found.** `kimi --help` subcommands are
`login, logout, term, acp, info, export, mcp, plugin, vis, web` — no
usage/quota/stats/status command. `kimi info --json` (read-only, run) returns
only `{kimi_cli_version, agent_spec_versions, wire_protocol_version,
python_version}` — nothing about the account. Local state files were
inspected directly (all tiny, safe to read):
- `~/.kimi/kimi.json` (167B): just a `work_dirs` list with `last_session_id` bookkeeping, nothing usage-related.
- `~/.kimi/credentials/kimi-code.json` (1.5KB, keys only — not printed): `access_token, refresh_token, expires_at, scope, token_type, expires_in` — a plain OAuth token blob, no quota/usage fields.

**2/3/4.** N/A — there is nothing to report fields for, evaluate stability of,
or check the auth-gate of, because no local channel exists at all. Kimi *is*
logged in on this machine (well-formed, apparently non-expired token
structure), so this isn't an auth gap — it's a genuine absence of any exposed
usage surface, CLI or local-file. The only remaining option would be calling
Moonshot's backend account API directly with the stored OAuth token, which is
undocumented, unverified, and out of scope for a no-login/no-probing spike.

---

## cursor

**Not tested.** `cursor-agent`/`agent` is not installed on this machine
(`command -v cursor-agent`, `command -v agent` → not found; only the
unrelated Cursor desktop IDE under `~/.cursor` is present, which is a
different product from the `cursor-agent` CLI this cockpit would spawn per
`src/agent_takkub/provider_spec.py::cursor_spec`). No `--help`, config file,
or local state could be inspected. Reported as untested, not as "no channel
exists" — do not treat this as a negative finding.

---

## Summary — what the UI could actually show

| provider | usable today? | what it'd show | fragility risk |
|---|---|---|---|
| **claude** | ✅ yes (already implemented) | %used + reset time for 3 windows, plan label | endpoint is private/undocumented, already hardened against 403/429 (`fetch_usage_shared`) |
| **codex** | ⚠️ plausible, not yet verified live | %used + reset + duration for primary/weekly windows, plan type, spend-control state | needs a JSON-RPC app-server client (real integration work); surface marked `[experimental]` |
| **gemini (agy)** | ⚠️ plausible, but stale-prone | remaining fraction/% + reset time, per model | reads a cache file the client fills in on its own schedule — can be many months stale, no on-demand refresh found |
| **opencode** | ❌ not for "remaining quota" | only self-tallied tokens/cost spent through opencode — not the provider's plan limit | wrong kind of number for a "quota remaining" UI; would mislead if labeled as such |
| **kimi** | ❌ no | nothing | no channel of any kind found |
| **cursor** | ❓ unknown | — | CLI not installed here; needs testing on a box that has it |

**Recommendation for the UI:** only claude should show a real "X% used,
resets in Y" meter today. Codex and gemini/agy are worth a follow-up spike
(a throwaway app-server JSON-RPC probe for codex; a staleness/refresh check
for agy's cache file) before committing to them. opencode's `stats` must
**never** be labeled "quota remaining" — it's spend telemetry, the opposite
kind of number, and showing it as a limit would actively mislead. kimi and
cursor should render as "not supported" (never a fabricated 0%), matching
this repo's existing `LimitWindow.utilization: None` → "—" convention.
