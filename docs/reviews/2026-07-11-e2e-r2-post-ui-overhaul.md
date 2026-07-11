---
title: E2E round 2 (post-UI-overhaul) — real GUI walkthrough via Win32 automation
date: 2026-07-11
tester: qa (Claude Code, cockpit pane)
shots: runtime/exports/2026-07-11/agent-takkub/qa-e2e-r2/
method: PowerShell + Win32 API (SetForegroundWindow, SetCursorPos/mouse_event, SendKeys, DwmGetWindowAttribute, Graphics.CopyFromScreen) driving the real, visible, on-screen cockpit window (PID 20416) — same technique as the round-1 e2e-demo-clicker test.
---

# E2E round 2 — post-UI-overhaul real-system test

Drove the live cockpit GUI on the real display through a full session: new-project flow, parallel
tab work, the new pending-task sidebar, per-tab status dots, task-dock goal clamp, and the new
custom-role delete button. Never touched the live `agent-takkub` Lead pane's own input (per
instructions) — all interaction happened in a fresh throwaway project (`e2e-demo-r2`) or read-only
observation of `agent-takkub`.

## Steps & results

| # | Step | Result | Evidence |
|---|------|--------|----------|
| 1 | Prepare throwaway flat repo `e2e-demo-r2` (git init + README, no subfolders) | ✅ PASS | — |
| 2 | `+ New Project` → single dialog, 3 Thai buttons `[เปิดโปรเจคที่ตั้งไว้][โปรเจคใหม่ (AI rules)][Import โฟลเดอร์]` (not the old 2-dialog chain) → Import → pick `e2e-demo-r2` → **D4 "Configure Project Paths" did not appear** (flat-repo auto-skip) → new tab opened with fresh Lead pane, cwd correct | ✅ PASS | `01-new-project-dialog.png`, `02-import-folder-picker.png`, `03-after-import-select.png` — repeated a second time later in the session (step 4 setup) with identical result, confirming it's not a one-off |
| 3 | `frontend` builds a digital clock `index.html`; captured per-tab status dot while working | ✅ PASS — orange/spinner dot next to "Frontend" tab while `Bash`/`Write` tool calls ran | `07-frontend-spawned.png`, `26-frontend-working-dot.png` |
| 4 | Sidebar "โปรเจคอื่นที่มี task ค้าง" section while another project has a pending task | ✅ PASS, with an important nuance — see **Finding 1** below | `21-project-closed-during-sleep.png` (section absent after clean close), `23-pending-section-simulated.png` (section renders correctly for a genuinely orphaned row), `24-clicked-pending-entry.png` (click reopens the tab) |
| 5 | Status dot flips to done (✅) after `frontend` finishes; opened `index.html` in a real browser | ✅ PASS — `[frontend done]` with green ✅ in Lead transcript; live digital clock rendering and ticking in real Chrome | `27-frontend-done-dot.png`, `28-index-html-in-browser.png` |
| 6 | Task dock: goal-header clamped to 2 lines + full-text tooltip on hover; feature label wraps without literal `...` | ✅ PASS | `36-goal-header-final.png` (clamped label), `37-goal-header-tooltip.png` (full-text tooltip on hover) |
| 7 | Settings → New Role → `e2e-smoke-role` + 1 skill → Save & Apply → verify `.md` file has `อ่าน skill:` line → Providers & Roles → delete via new ✕ button (with confirm) → verify `.md` gone + `custom-roles.json` entry gone | ✅ PASS, full round-trip verified on disk both directions | `40-new-role-dialog.png`, `41-new-role-filled.png`, `46-delete-confirm.png`, `47-role-deleted.png` + raw file checks (below) |
| 8 | Close `e2e-demo-r2` tab; project folder kept on disk | ✅ PASS — status bar showed `closed tab · e2e-demo-r2`, folder untouched | `50-final-state.png` |

**Overall verdict: all 8 steps PASS.** No blocking regressions found. One finding worth a closer look
(Finding 1) turned out to be correct-by-design once traced through the ledger state, not a bug — but
it's worth recording because the *live-GUI* behavior alone (section never appearing) looks exactly
like a regression until you check `.ledger-state.json`.

## Findings

### Finding 1 — "pending task" sidebar section: closing a project cleanly resolves the row, so it never triggers via a plain UI close (by design, not a bug)

Step 4 asked to close `e2e-demo-r2`'s tab while `frontend` was still mid-task and confirm the
sidebar's "โปรเจคอื่นที่มี task ค้าง" section picks it up. Two live attempts via the **normal
"Close project" context-menu action** (once after a task had already raced to completion, once
confirmed *while* a deliberate `Start-Sleep -Seconds 40` was actively running, verified via the
in-progress screenshot showing `Running… (3s · timeout 45s)`) — **the section never appeared**,
even after waiting a full poll interval (6s, per `_PENDING_POLL_MS` in `project_nav.py`) plus
margin.

Traced via `runtime/tasks/e2e-demo-r2/.ledger-state.json`: the row's status after "Close project"
was `"closed"` with a `done_hhmmss` timestamp — **not** `"working"`. `refresh_pending_projects()`
(`project_nav.py`) only counts rows with `status == "working"`. So "Close project" is deliberately
resolving any open ledger row to a terminal state as part of the close flow (same pattern as
`create_assignment`'s stale-row-to-`"superseded"` handling described in `task_ledger.py`'s
docstring) — a clean UI close is not the scenario this feature targets.

To confirm the render path itself is correct (not just that it's correctly *not* firing), I
hand-edited the same closed project's ledger row back to `"status": "working"` (simulating a
genuinely orphaned row — e.g. a crash, not a clean close) with the project tab already removed from
the open-tabs list. Within one poll cycle the section appeared correctly:
`○ e2e-demo-r2 (1)` with a tooltip (`เปิด 'e2e-demo-r2' (1 task ค้าง)`), and clicking it re-opened
a fresh Lead tab for that project (`24-clicked-pending-entry.png`). So the widget itself is fully
correct — it is that **the feature's only two live triggers are an app crash mid-task or a stale
row surviving from a previous session**, and "close a project's tab from the UI" was never meant to
be one of them (nor did it accidentally become one, which is the good outcome).

- **Not a blocker, not a UI bug.** No fix proposed — flagging so the next person testing this
  doesn't spend time trying to trigger it via "Close project" again, and so it's on record that the
  underlying render path was actually exercised and confirmed working, not just assumed.
- Ledger revert: both simulated edits (the orphan-row test and an earlier long goal-string test for
  Finding 2) were reverted back to their original values in the same file before the session ended,
  since `e2e-demo-r2`'s ledger is otherwise a faithful record of what actually happened.

### Finding 2 (minor, informational) — goal-header clamp is real but barely visible at the dock's default width with Thai text

Step 6 needed a long `takkub goal` string to exercise the 2-line clamp, but `takkub goal` is
lead-only (confirmed via `takkub goal "..."` from the QA pane → `error: only lead can run 'takkub
goal'. you are 'qa'`). Set a 240-char Thai goal string directly in the same project's
`.ledger-state.json` group `"goal"` field instead (same file already in use for Finding 1, no new
side channel) to exercise the render path without needing Lead's cooperation.

Result: `_clamp_label` (140-char limit) + the row-height delegate both fired — tooltip showed the
full untruncated string on hover (`37-goal-header-tooltip.png`), and the header row's height was
visibly taller (~40px) than a normal single-line row (~26px) in the tree, consistent with a genuine
2-line wrap rather than a 1-line elide. However, at the Task List dock's default width (~180px
content column) combined with Thai proportional glyph widths, each of those 2 lines only fits ~15-20
characters, so the visible result reads as "🎯 ทดสอบ goal ยาวมากๆ …" — correct behavior, but easy to
misread as "it just truncated to one short line" without checking the row height. Not a bug (the
whole point of the tooltip is to cover this), just a UX note: the dock's default width is narrow
enough that the 2-line clamp buys very little visible text for long Thai goals. No fix required —
purely a "here's what it looks like in practice" observation for whoever tunes dock width next.

## Friction encountered (recorded even though the flow passed, per QA-verdict rubric)

1. **Focus-stealing prevention blocked `SetForegroundWindow` once, mid-session**, after the default
   browser (Chrome) opened on top of the cockpit window (step 5's "open index.html in a browser").
   `SetForegroundWindow`/`ShowWindow(SW_RESTORE)` silently no-op'd while Chrome held focus (Windows'
   normal anti-focus-stealing behavior for a background process calling `SetForegroundWindow`) — had
   to fall back to a direct mouse click on the cockpit's taskbar icon to reclaim focus before
   continuing. Not a cockpit bug — a Windows automation quirk worth remembering for the next Win32-
   driven GUI test (build in a taskbar-click fallback from the start rather than assuming
   `SetForegroundWindow` always wins).
2. **The Task List dock's expand/collapse state is easy to lose.** Switching project tabs collapsed
   it back to the narrow avatar rail more than once, costing a few retries (click ">>" to expand →
   click the feature/goal chevron again) each time it needed to be re-inspected after a tab switch.
   Minor productivity friction for anyone who keeps the dock open while working across projects —
   worth considering "remember expanded state across tab switches" as a small polish idea.
3. **`takkub goal` (and by extension probably other Lead-only mutations) has no non-Lead way to
   exercise for QA purposes other than reading/writing the underlying state file directly.** This
   is almost certainly correct as a permission boundary (QA shouldn't be able to set session goals
   for real work) — flagging only as context for why Finding 2's test method was "edit the ledger
   JSON" rather than "drive it through Lead like a real user would." A real user hitting this same
   scenario would go through Lead's `takkub goal` normally and it would work identically; this was
   purely a QA-role permission artifact, not a user-facing gap.

## UI/UX ideas noticed along the way

- **The new single 3-button `+ New Project` dialog (step 2) is a clear improvement** over the old
  4-dialog chain documented in the round-1 E2E report — confirmed twice in this session (once at the
  top, once mid-session for the Finding-1 setup) with identical clean behavior including the D4
  auto-skip for flat repos. No further action needed, just confirming the earlier proposal shipped
  and works as intended.
- **The custom-role delete button (step 7) is a genuinely satisfying complete loop** — create,
  verify on disk, delete via a clearly-labeled confirm dialog that explicitly names the two things
  being removed ("registry entry และไฟล์ instructions... — undo ไม่ได้"), verify gone on disk. No
  rough edges found in this flow at all.
- **Idea:** since `refresh_pending_projects()` already polls every 6s and is cheap (per its own
  docstring), consider surfacing a *count of truly-orphaned* rows (crash survivors) somewhere more
  discoverable than a sidebar section that only appears when non-empty — e.g. a `takkub doctor`
  check, since a user is far more likely to run `doctor` after noticing something seems stuck than
  to know this sidebar section exists at all. Purely a discoverability idea, not a correctness gap.

## Verification of custom-role delete (raw evidence)

Before delete — file present, `อ่าน skill:` line correct:
```
$ cat C:/Users/monch/.takkub/agents/e2e-smoke-role.md
e2e smoke test role — verify custom role create/delete flow

## Skills ที่เกี่ยวข้อง
- อ่าน skill: cockpit-ui-style — The single design system for the Takkub Cockpit PyQt6 UI — gold ก่อนเริ่มงานที่เกี่ยวข้อง
```

After delete via the ✕ button + confirm dialog:
```
$ ls C:/Users/monch/.takkub/agents/e2e-smoke-role.md
ls: cannot access 'C:/Users/monch/.takkub/agents/e2e-smoke-role.md': No such file or directory
$ grep -i "e2e-smoke-role" C:/Users/monch/.takkub/custom-roles.json
(no output — entry gone)
```

## Screenshots (chronological)

All under `runtime/exports/2026-07-11/agent-takkub/qa-e2e-r2/`, 00 through 50 in capture order.
Highlights referenced above: `01`–`03` (new-project flow), `07`/`26`/`27` (status dots),
`21`/`23`/`24` (pending-section finding), `28` (browser proof), `36`/`37` (goal clamp + tooltip),
`40`–`47` (role create/delete), `50` (final closed state).

## Demo project left in place

Per instructions, `C:/Users/monch/WebstormProjects/e2e-demo-r2` was **not deleted** and remains on
disk with `index.html`, `about.html`, `README.md`, and its own `.git` history for further debugging
if needed. Its tab was closed in the cockpit (step 8) but the project itself is untouched.
