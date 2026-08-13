# Provider usage/limit implementation (2026-08-13, Wave 2)

Backend implementation of the abstraction from
`docs/design/2026-08-13-provider-usage-abstraction.md`, built on the
empirical findings in `docs/audit/2026-08-13-provider-usage-survey.md`.
Ships:

- `src/agent_takkub/provider_usage.py` — the `ProviderUsage` shape, one
  adapter per provider, `ProviderUsageStore` (background poller +
  singleton), and `fetch_provider_usage()` dispatch.
- `remote/api.py::usage()` + `GET /api/usage` in `remote/http_server.py`.
- `tests/test_provider_usage.py` (38 cases) + additions to
  `tests/test_remote_api.py` / `tests/test_remote_http_server.py`. No test
  hits a real network endpoint; the codex adapter is exercised against a
  throwaway fake-server subprocess instead of mocking away its actual
  reader/timeout logic.

This doc is for whoever builds the UI on top of `/api/usage` (frontend) —
it says which providers are real numbers today and which are placeholders,
and why.

## Per-provider status

| provider | implemented as | status shape used | notes for UI |
|---|---|---|---|
| **claude** | wraps `limit_status.fetch_usage_shared()` (existing, hardened) | `active` / `stale` / `error` | `utilization`/`resets_at` from the `five_hour` window; `plan` from credentials. `stale` = the shared cache is serving a backed-off/rate-limited read. |
| **codex** | `codex app-server` JSON-RPC, `account/rateLimits/read` — **verified live** against codex-cli 0.146.0 during this task (spawned a real app-server, did the handshake, got a real `RateLimitSnapshot` back) | `active` / `unsupported` (no binary) / `error` (spawn/RPC failure) | `utilization` = `primary.usedPercent`, `resets_at` = `primary.resetsAt` (epoch seconds), `plan` = `planType`. Full `rateLimits` object rides along in `raw_data`. Every subprocess is throwaway — spawned, queried once, killed — never pooled. Bounded by a 10s timeout end-to-end. |
| **gemini (agy)** | reads agy's own `~/.antigravity_cockpit/cache/quota_api_v1_plugin/authorized/*.json` cache file | `active` if `fetched_at` < 24h old, else `stale`; `unsupported` if no cache file exists (never logged in / cache never written) | `utilization` = worst-case (lowest remaining fraction) across every model in the cache — a single meter, not per-model. Per-model count only in `raw_data`. **This can be stale for months** — there is no CLI command that forces a fresh read, confirmed in the spike. `fetched_at` always rides along so the UI can show "last updated". |
| **opencode** | `opencode db "<SELECT ... json_extract(data,...)>" --format json` — sums opencode's own self-tallied per-message cost/token fields | `active` / `unsupported` (no binary) / `error` (query/parse failure) | **`utilization` is ALWAYS `None` for opencode.** There is no quota/rate-limit table anywhere in opencode's local DB — only its own spend tally. That number lives in the separate `spend` field (`cost_usd`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `message_count`). **The frontend must render `spend` as its own distinct UI ("tokens sent through opencode"), never blended into a utilization/quota meter** — doing so would actively mislead ("you have quota left" when there's no such concept here). |
| **kimi** | none — statically `unsupported` | `unsupported` | No usage/quota surface of any kind exists (survey checked `kimi --help`, `kimi info --json`, and every local state file). Nothing to poll. |
| **cursor** | none — `unsupported`, with a reason that distinguishes "not installed on this machine" from "installed but channel unverified" | `unsupported` | `cursor-agent`/`agent` was never available to test. If a future machine has it installed, the reason text changes to say so, but the status stays `unsupported` until someone actually verifies a channel exists (#103 follow-up). |

## API contract (`GET /api/usage`)

Same auth as every other remote route (bearer + password gate). Response:

```json
{
  "providers": [
    {
      "provider": "claude",
      "status": "active",
      "plan": "Max 5x",
      "utilization": 7.0,
      "resets_at": "2026-08-13T12:00:00+00:00",
      "fetched_at": "2026-08-13T11:55:00+00:00",
      "error": null,
      "spend": null,
      "raw_data": { "...": "provider-specific extras" }
    }
  ]
}
```

One entry per `provider_usage.PROVIDER_NAMES` (`claude`, `codex`, `gemini`,
`opencode`, `kimi`, `cursor`), always — a provider with no cache entry yet
reports `status: "loading"`, not a missing key. Missing numeric fields are
JSON `null`, never `0`.

**The handler never fetches live.** It reads
`provider_usage.get_store().get_all()` — a process-wide singleton whose own
background thread does all the polling (first call lazily starts that
thread; the HTTP response itself never blocks on a fetch). This was a hard
requirement from the task spec: a phone poll must never turn into an extra
hit against a rate-limited provider endpoint.

## What's NOT done (left for follow-up / frontend)

- **Desktop UI wiring**: `ProviderUsageStore` is not yet instantiated
  anywhere in `main_window`/status-bar code — only the remote endpoint uses
  it today (via the module-level singleton). A desktop status-bar chip
  would want to either share that same singleton or get its own
  `on_update` callback wired to a Qt signal, per the design doc's §3
  desktop tooltip section.
- **Refresh-on-generation-complete** (design doc §4): `ProviderUsageStore`
  exposes `refresh_now(provider)` for this, but nothing calls it yet — no
  hook into "an AI generation just completed" exists in this task's scope.
- **Cross-process shared state for non-claude providers**: claude already
  persists `{fetched_at, backoff_until}` to disk via `limit_status`'s
  existing shared-state file (multi-instance safe). Codex/gemini/opencode
  do not — each cockpit process polls independently. Given codex/opencode
  fetches are cheap local calls (not rate-limited HTTP) and gemini only
  ever reads a file it doesn't write, this was judged low-risk to skip for
  now; flag if that assumption turns out wrong in practice.
- **Cursor**: still fully unverified. Needs testing on a machine that
  actually has `cursor-agent` installed before it can move past
  `unsupported`.
