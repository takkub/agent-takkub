# #203 / #204 — mobile usage stuck 23 days + only shows one window

## #203 root cause (confirmed)

`provider_usage._FETCHERS["claude"]` is called by `fetch_provider_usage()`
with no arguments, so `fetch_claude_usage(config_dir=None)` always fell
through to `limit_status._resolve_config_dir(None)` → `~/.claude`. On an
installed cockpit build that is **not** the directory panes actually run
under — `CLAUDE_CONFIG_DIR` is `~/.agent-takkub/claude-config`
(`config.default_claude_config_dir()`). Two independent
`takkub-usage-state.json` files exist as a result; `ProviderUsageStore`
(the only source `/api/usage` reads) kept polling the wrong one and never
refreshed.

**Desktop's own chip/popup was never affected.** It builds its claude
`ProviderUsage` through a completely separate path —
`limit_panel._claude_provider_usage(data)`, fed by `LimitStore`, which is
registered per open project tab via `user_profile.config_dir_for(project)`
(`limit_panel._init_limit_store`) — already correct.
`limit_panel._refresh_limit_label` explicitly excludes `"claude"` from the
`provider_usage.get_store()` cache it merges in
(`if name != "claude"`), so the broken store entry never reached desktop UI.
This is why only the phone showed stale numbers.

### Fix

- `provider_usage._resolve_claude_config_dir()` — new helper, mirrors
  `limit_panel._init_limit_store`'s own resolution: `user_profile
  .config_dir_for(config.active_project()[0] or "")`. Same shared
  `takkub-usage-state.json` desktop already reads/writes via
  `limit_status.fetch_usage_shared`/`LimitStore`, so once both resolve the
  same dir the two surfaces can never disagree (#204 item 3).
- `fetch_claude_usage(config_dir=None)` now resolves through that helper
  instead of handing `None` down to `limit_status` (which still defaults to
  `~/.claude` — untouched, since other direct callers may rely on that).
- Age-based staleness: `data.status == "rate_limited"` alone can't catch a
  wrong-directory read (it's neither backed off nor an HTTP error) — added
  `_CLAUDE_STALE_THRESHOLD_S` (24h, matching gemini's existing threshold) so
  the PWA's already-existing `stale-tag` badge (`app.js` — previously
  gemini-only in practice) now also fires for claude.
- Verified codex/gemini do **not** have the analogous bug: cockpit never
  injects a per-project `CODEX_HOME` (`codex_helper.py` only reads it from
  the ambient OS env, same for every project), and gemini's adapter reads a
  fixed global cache path (`~/.antigravity_cockpit/...`), not a
  cockpit-controlled per-profile dir. Nothing to fix there.

## #204 — show both windows

- Added `ProviderUsage.windows: list[{"name","utilization","resets_at"}]
  | None`, serialized by `usage_to_dict`. Populated for:
  - **claude**: every window `limit_status` returned (five_hour, seven_day,
    seven_day_sonnet when present) — previously only `raw_data["windows"]`
    carried this (undocumented, desktop-only shape used by
    `_claude_provider_usage`, itself a *different* dict-keyed shape built
    from the same `UsageData` — the two never collide since
    `_claude_provider_usage` never reads `provider_usage.fetch_claude_usage`'s
    output).
  - **codex**: `_parse_codex_rate_limits` now also parses `rateLimits
    .secondary` (the weekly window — confirmed live in the codex app-server
    survey, previously fetched and silently dropped).
  - Everyone else (gemini/opencode/kimi/cursor): left `None` — single-window
    or no-quota providers keep the existing single-% card unchanged
    (provider-neutral: nothing new to break).
- `remote/static/app.js` `buildUsageCard`: when `status` is active/stale and
  `windows` is non-empty, renders one row per window (label, %, thin bar,
  reset countdown) instead of the single headline %/bar — never both, so
  no number appears twice. A window with `utilization: null` renders "—",
  never 0%. Providers without `windows` render exactly as before.
- Desktop stays unaffected (`_claude_provider_usage`/`usage_meter.py` never
  reads the new `windows` field), matching item 3's "must not conflict".

## Tests

`tests/test_provider_usage.py`:
- `TestClaudeConfigDirResolution` — fetcher receives the resolved cockpit
  dir, resolution goes through `active_project`/`config_dir_for` not a
  hardcoded `~/.claude`, and a missing active project doesn't raise.
- `TestClaudeAdapter::test_stale_by_age_even_when_status_ok` /
  `test_missing_fetched_at_is_stale_not_active` — age-based staleness.
- `TestCodexAdapter::test_parse_codex_rate_limits_surfaces_secondary_window`
  (+ no-secondary / no-data variants) — windows list, never fabricated rows.
- Existing `test_active_data_maps_five_hour_window` updated to a live
  `fetched_at` (was a fixed past date that predates today under the new 24h
  staleness rule — an intentional fixture update, not a behavior revert).

Run: `pytest tests/test_provider_usage.py tests/test_limit_status.py -q` —
both green. `tests/test_remote_api.py`/`test_usage_meter_*` need PyQt6,
not installed in this worktree's isolated venv; neither file's assertions
touch anything this change modifies (`usage_to_dict` only gains a key,
`_claude_provider_usage`/`usage_meter.py` are untouched).
