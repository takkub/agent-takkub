# Audit: Stream Watchdog (CLI 2.1.196) vs cockpit `_auto_recover_stuck`

**Date:** 2026-06-30 · **Author:** Lead (Claude) · **Scope:** read-only audit, no code change
**Question:** CLI 2.1.196 ships a default-ON Stream Watchdog. Does it fight the cockpit's own stuck-pane auto-recovery → double-recovery (pane respawn ซ้อน)?

---

## TL;DR — double-recovery risk is **LOW**. No urgent fix. The two layers are well-separated by threshold and by *what* they measure.

---

## The two recovery layers

| | CLI Stream Watchdog (2.1.196) | Cockpit `_check_stuck_panes` → `_auto_recover_stuck` |
|---|---|---|
| **Fires after** | **5 min** of stream silence | **10 min** (`STUCK_THRESHOLD_S = 10*60`) |
| **Measures** | no tokens arriving from the API stream | **content-delta of the rendered screen** (`display_lines()` blake2b hash, spinner/volatile-counter lines filtered out) |
| **Action** | abort the in-flight request + retry internally (same process) | **close → respawn** the whole claude process with `--resume <uuid>` |
| **Weight** | light (re-request within the live session) | heavy (kills+restarts claude.exe, full process teardown) |
| **Disable** | `CLAUDE_ENABLE_STREAM_WATCHDOG=0` | n/a (constant; `STUCK_RECOVER_MAX=3` then gives up) |
| **Tick** | CLI-internal | every ~5 s (`IDLE_WATCHDOG_INTERVAL_MS`) |

## Why they don't collide in the common case

1. **Threshold ordering:** cockpit's 10 min > CLI's 5 min, so the **CLI always gets first crack**. The cockpit is, by construction, the *slower backstop*.
2. **Different signals, and the CLI retry resets the cockpit clock:** the cockpit recovers on *content-static* screen, not stream silence. When the CLI watchdog aborts+retries at 5 min it re-renders (retry notice / spinner / fresh tokens) → the screen content changes → `last_content_change_ts` is bumped → the cockpit's 10-min static clock **resets**. So a pane the CLI is actively retrying never reaches the cockpit threshold. The cockpit only fires if the screen stays byte-identical (spinner-filtered) for a full 10 min — i.e. the CLI's lighter retry already failed to produce any progress.
3. **Cockpit owns the cases the CLI watchdog can't see:** MCP-tool deadlock, TTY-prompt block (`_maybe_surface_tty_block`), update-splash, runaway-output loop (`_warn_lead_runaway_pane`), malformed-XML no-op. None of these are "stream silence" → the CLI watchdog never touches them → **no overlap, cockpit is complementary, not redundant.**

## The narrow residual-risk window

A wedge where the CLI watchdog aborts+retries **silently on screen** (no visible re-render) and keeps failing for a full 10 min. Then the cockpit also close→respawns *on top of* an in-flight CLI retry. But:
- This only triggers **after the CLI's lighter recovery has already failed for 10 min** — at which point the heavier close+respawn (`--resume`) is the *correct* escalation, not a wasteful duplicate.
- `STUCK_RECOVER_COOLDOWN_S = 5 min` + `STUCK_RECOVER_MAX = 3` then `_give_up_stuck` bound it — it can't loop.

**Assumption (medium confidence):** that a CLI watchdog retry produces *some* terminal output captured by `display_lines()`. If a future CLI version retries with zero screen change, the reset-clock argument (#2) weakens and the residual window widens. Worth a one-off empirical check (below) rather than a code change now.

## Recommendation

1. **No code change required now.** The layering is already sound: light/fast CLI retry first, heavy/slow cockpit respawn as backstop, cockpit-only coverage for non-stream hangs. This is the "don't build what isn't needed" outcome — same as finding the Reaper already existed.
2. **Keep `STUCK_THRESHOLD_S` strictly > 5 min** so the CLI always acts first. It is (10 min). If anyone tunes it down, keep a floor above the CLI's 5 min. *Optional:* make it env-overridable (`TAKKUB_STUCK_THRESHOLD_SEC`) with a documented `>300` floor, mirroring `STALL_THRESHOLD_SEC`.
3. **One-off empirical confirmation (no build):** next time a pane genuinely wedges, watch `runtime/events.log` for `stuck_pane_recover` and check whether a CLI retry visibly preceded it. If `stuck_pane_recover` fires while the CLI was mid-retry with no screen change, revisit. Until observed, treat as low risk.
4. **Do NOT set `CLAUDE_ENABLE_STREAM_WATCHDOG=0`** — the CLI layer is the cheap front line; disabling it would push all recovery onto the heavy cockpit path.

## Verdict for the feature backlog

The red-team's "Process-Tree Stuck Watchdog (improve B)" item and any "tune our watchdog vs CLI" work is **low priority / mostly already handled**. The cockpit watchdog is not redundant with the CLI's; it covers a strictly larger, complementary set of failure modes. No Phase-2 watchdog rewrite needed — at most the optional env-tunable threshold (3 lines).

Related: `[[cli-2196-stream-watchdog-overlap]]` memory, `cockpit-freeze-blocking-main-thread`.
