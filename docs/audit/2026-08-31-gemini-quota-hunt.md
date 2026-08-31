# Gemini/agy live quota hunt (2026-08-31, #456)

**Question:** is there a live source for the gemini usage card, replacing the
Antigravity desktop app's `quota_api_v1_plugin/authorized/*.json` cache
(stuck at 2026-02-27 since it was first read — see `provider_usage.py`
comments above `fetch_gemini_usage`)?

**Answer: no usable live channel found.** Reported per the bounded-time
instruction (~40 min budget) rather than reverse-engineering further. Four
channels were checked; all four are dead ends for a maintained feature. What
*did* ship from this pass: `GEMINI_STALE_HINT` now appends the cache file's
own date (`_gemini_stale_hint()`), so the card at least says how stale the
number is instead of just "stale".

## What was tried

### 1. `agy` print-mode log at increased verbosity
Ran `agy -p "hi" --log-file <scratch>/agy_quota.log --print-timeout 60s`
(twice, second time with `GLOG_v=5 GLOG_logtostderr=1`). Both runs show
`quota_manager.go:45] doRefreshQuota: starting reload (force=true)` fire
**4 times** in a single one-shot print session, immediately after
`v1internal:loadCodeAssist` / `v1internal:fetchAvailableModels` calls to
`https://daily-cloudcode-pa.googleapis.com`. No corresponding HTTP URL log
line or payload ever appears for the quota fetch itself, at either
verbosity — `agy` computes quota in-memory for its own UI but doesn't log
the response. `GLOG_v` had no visible effect (agy's own flag parser doesn't
appear to wire glog's `-v`; there's no CLI flag for log level in `agy
--help`). **Dead end** — no payload observable via logs with any
undocumented-but-accessible flag.

### 2. Does `agy` write the quota cache itself?
Snapshotted mtimes of the three known `quota_api_v1_plugin/authorized/*.json`
files before and after the print-mode runs above (which triggered 4 forced
quota reloads): all three unchanged at `2026-02-27 08:51:*`. Also diffed
`~/.gemini` and `~/.antigravity_cockpit` for anything touched in the run
window — only conversation/log/annotation files changed, nothing
quota-shaped. **Confirms the existing code comment**: only the Antigravity
*desktop app* writes this cache; the `agy` CLI this cockpit spawns never
does, no matter how often it's run. **Dead end.**

### 3. Local HTTP/gRPC server `agy` opens per-process
Every `agy` boot (print mode included) logs:
```
server.go:599] Language server listening on random port at <N> for HTTPS (gRPC)
server.go:607] Language server listening on random port at <N> for HTTP
```
This is a **random port per process**, torn down when the one-shot print
call exits — there's no long-lived `agy` daemon in this cockpit's model to
poll. Using it would mean keeping a whole `agy` process resident just to
serve `provider_usage.fetch_gemini_usage()` polls, plus reverse-engineering
an undocumented gRPC/HTTPS protocol (likely IDE-extension-facing, not a
public API) with no evidence it even exposes quota over that channel.
**Not investigated further** — wrong shape for a periodic-poll adapter even
if it did expose something.

### 4. Direct call to Google's internal Code Assist endpoint
Extracted `[Qq]uota` strings from `agy.EXE` (Go binary, no source available).
Found the real RPC name and REST-mapped path:
```
v1internal:retrieveUserQuota
v1internal:retrieveUserQuotaSummary
```
on the same host agy already calls, `https://daily-cloudcode-pa.googleapis.com`,
plus matching protobuf message names (`RetrieveUserQuotaRequest/Response`,
`QuotaInfo` — `quotaInfo`/`remainingFraction`/`resetTime` match the cached
JSON's own field names, so this is almost certainly the exact RPC that
populated that cache originally).

This is a real, named endpoint — but calling it needs a valid access token
for the **same** account/auth-flow `agy` uses, and that turned out not to be
available on disk:
- `agy`'s own log states `keyringAuth: loaded token ... authenticated via
  keyring (effective: keyring)` — its credential lives in the **Windows
  Credential Manager (DPAPI-protected)**, not a plain file.
- The only file-based credential found, `~/.gemini/oauth_creds.json`,
  belongs to a **different, unrelated product** — the classic
  `@google/gemini-cli` npm package (separately installed on this machine,
  confirmed via `gemini_helper.py`'s docstring: the cockpit's gemini pane
  spawns `agy` specifically, never the npm `gemini` CLI). Its `access_token`
  is also expired since **2026-06-19** and refreshing it would still be the
  wrong product's quota, not agy's.

So the one live channel found requires pulling a DPAPI-protected credential
out of a different application's OS keyring and calling an **undocumented**
Google-internal RPC with it. That's not something to wire into a maintained
provider adapter unilaterally:
- No public contract — could break silently on any Antigravity update, with
  no changelog to watch.
- Extracting another app's protected keyring entry to drive an internal API
  is a security/maintenance judgment call for Lead, not a unilateral
  backend change.

**Not implemented.** Flagging the endpoint name here (`v1internal:retrieveUserQuota`
on `daily-cloudcode-pa.googleapis.com`) in case Lead wants to pursue an
official/supported path later — e.g. if Antigravity ever ships a real `agy
quota` subcommand, or if this becomes worth the keyring-extraction risk with
explicit sign-off.

## What shipped from this pass

`provider_usage.py`: `_gemini_stale_hint(fetched_at)` — same
`GEMINI_STALE_HINT` text, now suffixed with `(cache YYYY-MM-DD)` from the
cache file's own `updatedAt` when known, so the stale card says *when* the
snapshot is from instead of just "stale". `tests/test_provider_usage.py`
updated to match (`error.startswith(...)` + date substring instead of exact
string equality).

## Bottom line for the usage card

**"Open the Antigravity desktop app" is still the only way to refresh this
number.** No CLI-only, cockpit-side channel exists today. Re-check next
time `agy --help` changes (Antigravity CLI is actively developed and gets
new subcommands regularly) — an official `agy usage`/`agy quota`
subcommand, or `agy --help` verbosity/log-level flags, would be the clean
way in if either ever ships.
