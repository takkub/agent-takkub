---
title: E2E real-system test — north-star proof ("one command in → auto plan/divide/build/test/summarize")
date: 2026-07-11
tester: qa (Claude Code, cockpit pane)
shots: runtime/exports/2026-07-11/agent-takkub/qa/e2e-demo/
---

# E2E real-system test — north-star proof

## Goal

Prove the cockpit's north-star promise end-to-end, with real GUI interaction (no mocks): **one plain-language
command in → cockpit autonomously plans → spawns the right teammate → builds real code → verifies it → summarizes
back to the user.**

The live agent-takkub cockpit window (PID 23960) was never touched on its own `agent-takkub` tab — a brand-new
throwaway project (`e2e-demo-clicker`) was created and driven entirely through its own tab, as instructed.

**Method:** since this exercises the actual desktop Qt GUI (not a web page), no browser-automation MCP applies.
Automation was done via PowerShell + Win32 API (`SetForegroundWindow`, `mouse_event`/`SetCursorPos` for clicks,
`SendKeys` for typing, `Graphics.CopyFromScreen` for screenshots) driving the real, visible, on-screen cockpit
window — not headless/offscreen.

## Steps & results

| # | Step | Result | Evidence |
|---|------|--------|----------|
| 1 | Create throwaway project `e2e-demo-clicker` on disk (`git init` + README + commit) | ✅ PASS | — |
| 2 | Click "+ New project" in cockpit GUI → "เพิ่มโปรเจคใหม่" → "Import existing" → type folder path → "Select Folder" → "Configure Project Paths" (left blank, single flat repo) → OK | ✅ PASS — new tab `e2e-demo-clicker` appeared automatically, fresh Lead pane opened, cwd correctly resolved to the new project | `01-new-project.png` (chain: `01a`…`01g`) |
| 3 | Type command into the new Lead pane: *"สร้างหน้าเว็บ static ธรรมดา มีปุ่มกดนับจำนวนคลิก แสดงตัวเลขบนหน้าจอ"* and submit | ✅ PASS | `02-command-typed.png` |
| 4 | Lead plans and proposes | ✅ PASS — Lead correctly classified this as a single-role static-frontend task, proposed `frontend` with correct cwd, and asked for confirmation before firing (per routing policy: "งานเดียว role เดียว → ไม่มี parallel/sequential") | `03-lead-plan-proposal.png` |
| 5 | Confirm plan ("ลุยเลย") | ✅ PASS — Lead fired `takkub assign --role frontend --cwd ... "[ROLE: frontend developer...]"` immediately, no premature action before confirm | `04a-confirmed.png` |
| 6 | Frontend pane spawns, reads task spec, writes real code | ✅ PASS — `index.html` (98 lines) written with inline CSS + JS: click counter, Reset button, light/dark `prefers-color-scheme` support, no framework. Frontend tried `claude-in-chrome` to self-verify, found it not connected, correctly reported that limitation rather than silently skipping verification, and called `takkub done` | `04-frontend-spawn-working.png`, `05-frontend-code-written.png` |
| 7 | Lead receives `[frontend done]`, proposes next step | ✅ PASS — Lead correctly identified this as a static page needing no docker/build, and proposed a `qa` smoke-test row before offering to close the task (this is the "verify sequence" behavior from CLAUDE.md — QA is the finishing gate) | `07a-lead-after-done.png` |
| 8 | Confirm QA step | ✅ PASS | `07b-qa-requested.png` |
| 9 | QA pane spawns, reads task, smoke-tests with **real, visible Chrome** via `mb` CLI (not headless) | ✅ PASS — navigated to `file:///.../index.html`, confirmed initial counter = 0, clicked "Click me" 5×, confirmed counter = 5, clicked "Reset", confirmed counter = 0 again, checked console/network for errors (none), captured 3 screenshots as evidence | `06-page-loaded-browser.png` (real Chrome window, live), `08a`…`22a` progress captures |
| 10 | QA reports `takkub done` with a real score | ✅ PASS — **score 5/5**, all 3 checks explicitly marked PASS, evidence paths attached, session written to `runtime/sessions/2026-07-11/e2e-demo-clicker/qa-065753.md` | see QA session file |
| 11 | Lead receives QA done, gives final summary + next-step proposal | ✅ PASS — Lead summarized all 3 checks, confirmed "งานเสร็จสมบูรณ์ + verify แล้ว", and proposed a `git commit` (not push) as the natural next step — correctly stopped and asked rather than auto-committing | `07-final-summary.png` |
| 12 | Task ledger / dock reflects progress live | ✅ PASS — right-side Task List panel showed `e2e-demo-clicker` project appear, its task tree populate, and flip to complete (2/2) in step with the actual pane activity | visible in `05a`, `23a` |

**Overall verdict: full north-star loop PASS.** One plain Thai-language sentence → real plan → real spawn → real
code on disk → real browser verification → real pass/fail score → real human-readable summary, entirely inside a
brand-new project tab, without ever touching the live `agent-takkub` Lead session.

## Failures / friction encountered (as instructed — recorded even though the overall flow passed)

1. **`mb logs` streams indefinitely and forced a 2-minute Bash timeout mid-smoke-test.** QA ran
   `mb shot "<path>"` immediately followed (in the same reasoning turn) by an implicit `mb logs` check; on a static
   page with no console/network activity, `mb logs` never returns on its own, so the combined shell call hit
   Claude Code's Bash timeout (exit 143, "Command timed out after 2m 0s") *after* the screenshot itself had
   already succeeded. QA self-diagnosed this correctly in the transcript ("mb logs streams continuously and timed
   out (expected for a static no-JS-error page); screenshot succeeded before that") and proceeded without being
   blocked — but this cost roughly 2 real minutes of wall-clock per occurrence, and it happened once per screenshot
   step in this run. **This is a real tooling gotcha, not a demo-specific fluke** — any QA smoke test against a
   quiet/static page will hit the same wall unless `mb logs` is bounded (e.g. `mb logs --timeout 5s` or similar) or
   QA is told not to chain an unbounded `mb logs` after a screenshot call.
2. **`mb-start-chrome`'s visible browser window covers the entire screen**, including the cockpit window itself —
   which is *correct* per the task's "must use a real, visible display" requirement, but during this test it meant
   the outer observer (this QA session) briefly lost visibility into the cockpit's own progress while Chrome had
   focus. Not a bug, just worth knowing when designing future GUI-driving tests: budget separate full-screen shots
   to catch both windows.
3. **Minor:** typing Thai text into the Lead input via `SendKeys` dropped one word ("บน" in "แสดงตัวเลข**บน**หน้าจอ")
   — the rendered command read "แสดงตัวเลขหน้าจอ" (missing "on") instead of "แสดงตัวเลขบนหน้าจอ" ("show the number
   **on** the screen"). Meaning was still unambiguous and Lead/frontend interpreted intent correctly, so this did
   not affect the outcome — flagging only because it's a `SendKeys`-via-Win32-automation artifact, not a cockpit
   bug (any real user typing this by hand would not hit it).

None of the above blocked the flow or produced an incorrect result — they are documented per the QA-verdict rubric's
"record friction even when the flow completes" rule.

## UI/UX ideas noticed along the way (per user's ask: "cross-check หา idea UI เพิ่ม")

- **Smart reply-suggestion chips are a nice touch, worth surfacing more.** Twice during this run, the moment Lead
  asked a yes/no-ish question ("ok ให้ QA เช็ค หรือปิดงาน เลย?" → "ให้ QA เช็คเลย"; and later "จะ commit เก็บไหม
  หรือปิด session เลย?" → "commit this"), the cockpit had already pre-filled a plausible reply in the input box.
  This is genuinely useful — for a "one command in" north-star product, one-key-press confirmation loops matter a
  lot. **Idea:** make this affordance more discoverable (e.g. a faint "Tab to accept" hint under the pre-filled
  text, or render it as a distinct clickable chip above the input rather than pre-filled text that looks
  identical to something the user typed themselves) — right now a first-time user watching over someone's shoulder
  could easily mistake the suggestion for something they/someone already typed.
- **New-project flow has 4 sequential dialogs** (New project → Add project: New vs Import → native folder picker →
  Configure Project Paths) for the simplest possible case (a flat repo with no sub-folders to map). For solo-dev
  throwaway/demo projects (a very plausible use case given this cockpit's own north-star of low-friction "one
  command in"), **idea:** auto-skip the "Configure Project Paths" dialog when the selected folder has zero
  subdirectories at all (nothing to map), defaulting silently to "no roles" instead of requiring an explicit OK.
- **Task List (right dock) is genuinely great feedback** — watching the `e2e-demo-clicker` project section
  appear and its checkbox flip to done in lockstep with the actual pane activity gave strong "the system is
  really doing this" confidence during the test. No change needed here — flagging as a **positive** finding,
  since post-mortem notes said do save confirmations too, not just complaints.
- **Frontend's self-verify attempt via `claude-in-chrome`** (found not connected, reported the gap, and still
  reported done responsibly) is a good failure mode — no proposal needed, just noting it worked as intended
  rather than silently skipping verification or hallucinating a success claim.

## Screenshots (chronological)

All under `runtime/exports/2026-07-11/agent-takkub/qa/e2e-demo/`:

1. `01-new-project.png` — new empty `e2e-demo-clicker` tab just created (full add-project dialog chain in `01a`–`01g`)
2. `02-command-typed.png` — the one-line Thai command typed into the fresh Lead pane
3. `03-lead-plan-proposal.png` — Lead's plan proposal (role=frontend, correct cwd, single-role/no-parallel note)
4. `04-frontend-spawn-working.png` — frontend pane spawned, reading its task spec
5. `05-frontend-code-written.png` — frontend's actual `Write(index.html)` tool call, 98 lines, real content shown
6. `06-page-loaded-browser.png` — the real, visible Chrome window (not headless) showing the working page, counter = 0
7. `07-final-summary.png` — Lead's final wrap-up: QA score 5/5, all checks itemized, commit proposed (not executed)

Additional interim progress captures (`0Xa/0Xb…`) document every polling interval while frontend/QA worked, including
the `mb logs` timeout moment (`19a`–`20a` window) described above.

Evidence screenshots from QA's own smoke test (proof the deliverable actually works):
`runtime/exports/2026-07-11/e2e-demo-clicker/screenshots/01-initial.png` (counter=0),
`02-after-5-clicks.png` (counter=5), `03-after-reset.png` (counter=0 again).

## Demo project left in place

Per instructions, `C:/Users/monch/WebstormProjects/e2e-demo-clicker` was **not deleted** and remains available for
further debugging if needed. It contains `index.html`, `README.md`, and its own `.git` history (init commit +
frontend's work).
