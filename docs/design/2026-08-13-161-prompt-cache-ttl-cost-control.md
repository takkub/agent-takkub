# #161 — Reduce token burn on prompt-cache TTL resume

2026-08-13 · backend

## Problem

Claude's server-side prompt cache is written once, at whatever size the
conversation transcript is when the cache entry is (re)created, and expires
after a TTL — normally ~1h, but Anthropic shortens it to ~5min while an
account is in **usage-overage** (over its quota, burning pay-as-you-go
credits). A pane (Lead or teammate) that piles up a long conversation and
then sits fully idle across that TTL boundary pays the *entire* accumulated
transcript as one big cache-**write** (not the cheap cache-**read** normal
turns get) the next time anyone talks to it. The cost is proportional to how
much context piled up before the pane went idle, not to the TTL itself — so
the fix isn't "wait less", it's "have less to re-cache by the time the TTL
lapses".

A previous attempt at this task (issue #161, run on a different provider
pane) reported done without landing any commit — the work was lost. This is
a from-scratch redo on Claude.

## Scope

1. **Proactive idle compaction** — send `/compact` into a pane once it's
   been sitting genuinely idle long enough to be at real risk of crossing
   the cache TTL, instead of waiting for the CLI's own near-full-context
   auto-compact (which only fires under context *pressure*, not idle time).
2. **Surface usage/overage state in the UI** — a small warning chip in the
   status bar for the specific "5-hour window fully exhausted" state, since
   that's exactly when the TTL degrades to ~5min.
3. *(Optional, evaluated and intentionally not built further — see below.)*
   A session-brief-before-long-idle mechanism.
4. Multi-provider awareness — flag rather than silently ignore non-Claude
   panes.

### Item 3 — why no new code

The cockpit already has a session-brief mechanism: the 🏁 **End Session**
button writes a Lead summary to `runtime/sessions/` + the vault, and the
next spawn for that project auto-inherits it (`_recent_session_brief` in
`lead_context.py`). Auto-firing a similar brief per-pane every time it
crosses the idle threshold was considered and rejected: it would write a
session file for every ordinary multi-hour gap in normal work (lunch, a long
QA run, waiting on a teammate), which is much noisier than the deliberate,
user-initiated "wrap up this session" action the existing button already
covers well. The proactive `/compact` in item 1 already addresses the actual
cost problem (a smaller transcript to re-cache); a written brief only helps
a *human* re-orient, which is a different problem the End Session button
already solves. Revisit if idle-episode telemetry (added below) shows this
assumption is wrong.

## Design

### 1. Proactive idle compaction (`orchestrator.py`)

New watchdog tick, `_check_proactive_compact(now)`, called from
`_check_idle_teammates()` (the existing 5s-interval idle watchdog) alongside
the other per-tick checks (`_check_stuck_panes`, `_check_stale_markers`,
etc.) — no new `QTimer`.

Unlike the forgot-`takkub done` reminder loop it rides alongside — which
only tracks panes with `pane.state == "working"` — this targets panes
regardless of state, because the actual target is a pane that has *already*
reported done (or is Lead, who never calls `done` on itself) and is now
sitting idle. Those are exactly the panes the forgot-`done` loop drops
tracking for.

Per pane, per tick:

- Busy / booting (`shows_startup_marker()`) / TTY-blocked / rate-limited →
  reset `proactive_compact_idle_since` to `None` and skip. A pane can't
  usefully run `/compact` in any of these states.
- Non-Claude provider (`effective_provider_for(role, project=...) !=
  "claude"`) → skip, no state change. See "Multi-provider gap" below.
- First idle tick → record `proactive_compact_idle_since = now`, wait.
- `now - proactive_compact_idle_since >= PROACTIVE_COMPACT_IDLE_AFTER_S`
  (default 25 min, `TAKKUB_PROACTIVE_COMPACT_IDLE_AFTER_S` env override, `0`
  disables) **and** not already compacted this idle episode
  (`proactive_compact_sent_ts < proactive_compact_idle_since`) → write
  `/compact` + a delayed Enter (`_delayed_enter`, same primitive the idle-
  reminder escalation and limit-autoresume wake already use), record
  `proactive_compact_sent_ts = now`, log `proactive_idle_compact`.
- Pane going busy again clears `proactive_compact_idle_since`, so the next
  idle stretch is a fresh episode and can trigger another `/compact`.

Two new `PaneState` fields (`spawn_engine.py`) hold this bookkeeping:
`proactive_compact_idle_since: float | None` and
`proactive_compact_sent_ts: float`. Both live in the same per-pane transient
dataclass the forgot-`done` state and every other watchdog uses, and are
implicitly cleared on `close()`/`done()`'s existing `PaneState` pop.

This runs **in addition to**, never instead of, the CLI's own automatic
near-full-context compaction (`_on_session_cap_exceeded`'s docstring:
"the CLI's own auto-compact already handles context pressure" — that
statement is still true; this is a different trigger — idle *time*, not
context *pressure*).

### 2. Usage-overage warning chip (`status_header.py`)

New pure signal in the data layer: `limit_status.is_in_overage(data) ->
bool` — `True` once the `five_hour` window's utilization is `>= 100.0`.
`None`/unknown utilization is never guessed into overage (matches
`LimitWindow.utilization`'s existing "unknown ≠ 0%" contract).

New status-bar chip `self._chip_overage` ("⚠ Usage overage"), built in
`_build_status_bar` next to the existing plan chip, hidden by default.
`_refresh_overage_chip()` — called from `_update_status()` on the existing
2s status timer, same as `_refresh_graft_chip`/`_refresh_remote_chip` —
reads the **already-polled** `LimitStore` cache for the active project
(`self._limit_store.get(config_dir)`, registered per-tab in
`limit_panel.py`) and calls `is_in_overage()` on it. No new network fetch;
this only reads state the tab-corner usage meter (`usage_meter.py`) already
maintains.

This is a narrower, more specific signal than the existing usage meter's
80%-utilization amber/red color coding — that meter already tells you
"getting close"; this chip specifically flags the one state where the
prompt-cache TTL is provably degraded, and explains why in its tooltip.

### 3. Multi-provider gap (flagged, not silently assumed)

`/compact` is a Claude Code CLI slash command with no confirmed equivalent
on codex/gemini/opencode/kimi/cursor. `_check_proactive_compact` gates on
`effective_provider_for(...) == CLAUDE` and simply skips non-Claude panes —
no alternative action is taken for them today. This is called out explicitly
in the constant's comment (`PROACTIVE_COMPACT_IDLE_AFTER_S`) and the
method's docstring, and is tracked under #103 (the standing multi-provider
gap tracker) rather than assumed to work everywhere. The usage-overage chip
is provider-agnostic (it reflects the *account's* Claude usage window, not a
per-pane CLI feature) so it needs no such gate.

## Testing

Targeted tests only (per this project's test-tier policy — full suite is a
QA batch-gate concern, not run here):

- `tests/test_idle_watchdog.py::TestProactiveIdleCompact` (6 tests) — idle
  episode start/threshold/single-fire-per-episode, re-fire after a new idle
  episode, non-Claude provider skip, rate-limited pane skip, Lead-pane
  eligibility (explicitly the case the forgot-`done` loop excludes).
- `tests/test_limit_status.py::TestIsInOverage` (6 tests) — the pure
  `is_in_overage` signal: none/below/exactly/above 100%, unknown
  utilization, no matching window.
- `tests/test_main_window_status_bar.py::TestOverageChip` (5 tests) — chip
  visibility across no-store-yet, no-active-project, in-overage,
  not-in-overage, and the `__dict__`-membership guard for the Qt test-stub
  case other chips already guard against.

All three files pass in full (87 tests total across the three files) after
the change, run via an isolated `.venv --system-site-packages` (this
worktree had no venv of its own — see the worktree's own `.venv/`, not
committed).

## Known gaps / follow-ups

- Non-Claude panes get no proactive-compaction mitigation at all (#103).
- No UI surface for *how often* proactive compaction has fired — it's
  log-only (`events.log`, event `proactive_idle_compact`) today.

## 2026-09-01 update (#465)

Real-world `runtime/events.log` evidence (09:39:26 / 10:36:31) showed Lead
getting `/compact`ed while a specialist was still mid-task — the "idle at
ready prompt" check above says nothing about whether Lead is actually done
orchestrating. `_check_proactive_compact` now runs two extra "not really
idle" gates before letting the idle clock accumulate at all, both resetting
`proactive_compact_idle_since` (not merely skipping the fire) so a full
fresh threshold is required once the project genuinely settles:

- **Lead** (`_lead_orchestrate_busy_reason`): blocked while any other role
  in the project is still `pane.state == "working"`, an open `takkub wait`
  registration exists, or an inbox digest is still queued (not yet written
  into Lead's pane).
- **Specialist** (`_pane_waiting_for_lead`): the #463 "waiting-lead" tier —
  turn genuinely ended while blocked on Lead's reply.

Each skip logs `proactive_idle_compact_skipped` with a `reason` (deduped to
once per busy stretch, not every 5s tick) so `takkub ma` can show why a
compact didn't fire.

This session also resolved the previous entry's "default is a judgment
call" gap: the base default moved from 25min to 55min (close to the real
~1h TTL without routinely crossing it — the old 25min value compacted panes
long before they were ever at risk, paying the summarize cost + losing
context for zero TTL benefit), and a separate, much shorter
`PROACTIVE_COMPACT_OVERAGE_IDLE_AFTER_S` (default 4min) is used instead
whenever the project's cached usage state reports
`limit_status.is_in_overage` — the state the TTL itself collapses to ~5min
under. Read via `limit_status.load_shared_state` (cross-process shared
file, already written by the usage-overage chip's own poller) once per
project per tick — no new network fetch. Both thresholds independently
disable via `=0`.

Also fixed in the same pass: the "nothing new since last compact" gate's
skip used to stamp `proactive_compact_sent_ts`, the same field an actual
`/compact` stamps — which made the episode's "already handled" guard true
for the rest of that idle episode regardless of how much real output
arrived afterward (while the pane never left its ready prompt), directly
contradicting that gate's own "never re-fires until real output arrives"
comment. The skip now leaves `sent_ts` untouched and dedupes its logging
via a separate `proactive_compact_skip_logged_bytes` field instead, so a
later tick with enough new output still fires.

Targeted tests: `tests/test_idle_watchdog.py::TestProactiveCompactOrchestrateGate`
(7 tests — Lead blocked by a working specialist/open wait/unread digest and
resuming its idle count once each clears, specialist waiting-lead, the
overage threshold, and the nothing-new-skip re-fire fix).
