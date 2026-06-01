# agent-takkub — System Gap Audit & Roadmap

> สร้างโดย multi-agent audit (78 agents, 10 มิติ) · 2026-06-01 · v0.5.2
>  67 findings ดิบ → **58 confirmed** (real/partial), 9 dropped (already-handled/false)

## สรุป

ระบบ takkub cockpit ทำงานได้จริงและมี test coverage ที่ดีในชั้น logic (routing_planner, cli_server, pyte parsing) แต่จุดอ่อนที่สุดอยู่ที่ "ขอบ" ของ runtime — pane lifecycle/recovery มีบั๊กที่ทำให้ pane ที่ค้างถูก recover เป็น session เปล่าและทิ้งงานกลางคันแบบเงียบๆ, watchdog จับ claude ที่หมุนค้างบน MCP ไม่ได้, และ Lead ไม่เคยถูกแจ้งเมื่อ teammate ยอมแพ้ ทำให้ auto-chain รอค้างตลอดกาล จุดที่สองคือ "ความจริงไม่ตรงกับ doc/UI" — routing_planner อ้างว่าเป็น authoritative แต่ไม่เคยรันจริง, ฟีเจอร์ AI-generated rules ที่เพิ่งเพิ่มไม่มีเอกสารเลย, การ substitute Claude แทน codex/gemini ที่ไม่ได้ติดตั้งไม่ถูกบอก Lead ทำให้ cross-check กลายเป็น Claude ตรวจ Claude เอง จุดที่ลงทุนคุ้มสุดคือ: รวม per-pane state เป็นออบเจกต์เดียว (แก้บั๊ก lifecycle ทั้งคลัสเตอร์พร้อมกัน), เพิ่ม integration test ของ IPC/spawn path ที่ยังไม่มีเลย, และเติม UI affordance ให้ assign งานได้โดยไม่ต้องจำ CLI flags ส่วนความปลอดภัยส่วนใหญ่เป็น accepted trust model ของ solo-dev บน Max OAuth — ของจริงที่ค้างคือ dead-code guard ที่อ่านเหมือนยังป้องกันอยู่ และ MCP credential ที่ยัง leak เข้าทุก pane

## Top priorities (เรียงความสำคัญ)

1. Fix stuck-recover state destruction: stop routing _auto_recover_stuck through close() (or capture+restore session UUID, task, auto-chain flag, requires-commit before respawn) so wedged panes don't respawn blank and silently drop the verify hop
2. Replace byte-arrival stuck detection with screen-content-delta over display_lines (excluding the spinner region) so a claude wedged-but-spinning on a slow MCP call is actually detected and recovered
3. Add integration coverage for the real winpty-spawn -> AgentPane -> render seam (and a loopback IPC framing test) — the most-regressed, currently-unverified runtime layer
4. Fix the multi-role UI+API routing shadow so review/test/refactor/design tasks stop being hijacked into parallel impl and dropping reviewer/qa/critic + codex cross-check
5. Escalate dead-end panes to the Lead: _warn_lead_respawn_capped() + cleanup of stale auto-chain keys so capped-respawn and the resulting sibling deadlock surface instead of waiting forever
6. Surface provider substitution truthfully: feed the 'not installed' codex/gemini state (not just the toggle) into Lead context so the Lead pre-warns the user when a cross-check is really Claude-on-Claude
7. Document the just-shipped AI-generated project-rules feature (README + CHANGELOG) and resolve the routing_planner 'code wins' / ARCHITECTURE.md doc-truth drift so docs stop misleading maintainers and new users

## Quick wins (impact สูง effort ต่ำ)

- Multi-role UI+API routing shadow fix (gate on impl verb) — S, fixes review/test/refactor misroutes
- Add missing eng verbs to _ACTIONABLE_EN (skip the question-word override) — S
- _warn_lead_respawn_capped() + clean up stale auto-chain keys on cap — S, unblocks deadlocked chains
- Reset AUTO_RESPAWN_MAX counter on manual/non-auto spawn — S
- Document the AI-generated project-rules feature in README + CHANGELOG — S, high discoverability
- Add 'codex'/'gemini' to _ROLE_MODEL_TIERS at Opus/high — S
- Add '?' help button / QShortcut and correct the shortcut wording — S
- Add project= to spawn/assign/done/close/send _log_event + LogsPanel project filter — S
- Add the two missing pop()s to close() + fix ARCHITECTURE.md count/claude_update.py + delete dead _rebalance_teammates — S each

## รายละเอียดตาม theme

### Pane lifecycle & recovery correctness

_The most concentrated cluster of genuine runtime bugs. Recovery paths route through close() which destroys per-pane state (UUID, task, auto-chain flag, requires-commit gate), watchdogs miss the most important hang mode, and dead/capped panes never escalate to the Lead. These are the failures that cause silent work loss and stalled chains in exactly the multi-recovery sessions the watchdogs exist for._

| Need | Impact | Effort |
|---|---|---|
| Stuck-recover destroys session UUID + task + auto-chain/requires-commit state (merged: respawns blank, drops verify hop) | high | M |
| Stuck watchdog cannot detect a claude spinning forever on a wedged/slow MCP tool call | high | M |
| Auto-respawn cap dead-ends silently — Lead never told a teammate gave up; auto-chain waits forever | medium | S |
| AUTO_RESPAWN_MAX counter never resets on healthy run — manually-revived pane has crash recovery permanently off | medium | S |
| Crash auto-respawn re-pastes full task into a --resume'd conversation, risking duplicate work | medium | S |
| Rate-limit detection relies on unverified version-fragile screen-scrape markers with no drift canary | medium | S |
| _send_when_ready fires submitting Enter on a fixed timer with no readiness/placeholder re-check | low | M |
| PtySession reader/writer QThreads never joined and PtySession never freed — leaks across respawns | medium | M |

- **Stuck-recover destroys session UUID + task + auto-chain/requires-commit state (merged: respawns blank, drops verify hop)** (high/M)
  - why: _auto_recover_stuck routes through close() which pops _session_uuids, _last_assigned_task, _auto_chain_panes, _requires_commit_on_done — so a wedged mid-task pane respawns into an EMPTY claude session (no --resume despite the docstring claiming it), with no task, no auto-chain tag (verify hop silently dropped), and no commit gate. Single root cause behind two findings.
  - evidence: `orchestrator.py:3247 close() inside _auto_recover_stuck; close() pops _session_uuids/_last_assigned_task/_auto_chain_panes/_requires_commit_on_done at 2130-2134; spawn() can_resume needs _session_uuids at 1417-1424; docstrings at 3229-3231 and 3191-3193 falsely claim --resume; contrast _auto_respawn (1624-1644) preserves the flag`
- **Stuck watchdog cannot detect a claude spinning forever on a wedged/slow MCP tool call** (high/M)
  - why: _check_stuck_panes keys only on _last_output_ts, bumped on EVERY PTY byte — the animated 'esc to interrupt' spinner resets the clock, so a claude deadlocked on a slow MCP call (the exact failure the comment names) never trips the threshold and never auto-recovers. Fix needs screen-content-delta over display_lines (excluding the spinner region), NOT transcript mtime (same spinner bytes).
  - evidence: `orchestrator.py:3211-3219 uses _last_output_ts; agent_pane.py:268-272 bumps on every byte; pty_session.py:448-449 'esc to interrupt' keeps is_at_ready_prompt False; idle reset at orchestrator.py:3144-3148`
- **Auto-respawn cap dead-ends silently — Lead never told a teammate gave up; auto-chain waits forever** (medium/S)
  - why: After AUTO_RESPAWN_MAX crashes, _on_session_exit logs and bare-returns with no Lead notification and without cleaning up _auto_chain_panes/_last_assigned_task, so the sibling group's handoff guard stays permanently non-empty and the chain deadlocks. Add a _warn_lead_respawn_capped() (mirror existing warn helpers) AND clean up the stale keys.
  - evidence: `orchestrator.py:1599-1606 capped logs+returns no notify; warn-helper pattern exists at 1846-1865; _auto_chain_panes popped only in done()/close() at 2338/2132`
- **AUTO_RESPAWN_MAX counter never resets on healthy run — manually-revived pane has crash recovery permanently off** (medium/S)
  - why: spawn() deliberately doesn't clear _auto_respawn_attempts and the UI Spawn button calls bare spawn() (no close()), so a deliberately-revived pane that worked for hours then crashes is refused recovery. Reset on manual/non-auto spawn or decay after sustained healthy uptime.
  - evidence: `orchestrator.py:1598-1607 increment+cap, 957-965 spawn won't clear; only pops at 2129/2297; UI Spawn at 3479-3480 bare spawn; settings-panel-design.md:85 confirms intended path is close+spawn`
- **Crash auto-respawn re-pastes full task into a --resume'd conversation, risking duplicate work** (medium/S)
  - why: _auto_respawn unconditionally replays the full cached task; a resumed claude already holds the task plus partial progress, so non-idempotent steps (migrations, file creation) can re-run. Gate the replay on the resumed flag — but note spawn() doesn't currently return it, so plumb it out or re-derive can_resume.
  - evidence: `orchestrator.py:1633-1644 unconditional replay; spawn() resume selection 1415-1431 (resumed bool internal, only in msg string at 1477); backoff comment 1608-1610 references replayed-task bug`
- **Rate-limit detection relies on unverified version-fragile screen-scrape markers with no drift canary** (medium/S)
  - why: If a Claude Code update changes the limit-banner wording, suppression never engages and the stuck watchdog force-respawns a rate-limited pane straight back into the limit — the exact thing the feature prevents. Tests co-pin the assumed wording so they stay green on drift. Add a log/canary when 'working but fully silent past threshold' is seen with no rate-limit match.
  - evidence: `pty_session.py:36-92 markers self-flagged as needing real-banner verification; orchestrator.py:3206-3210 stuck skip + 3253-3284 suppression both gate on it; tests/test_rate_limit_watchdog.py:27-61 pin assumed wording`
- **_send_when_ready fires submitting Enter on a fixed timer with no readiness/placeholder re-check** (low/M)
  - why: Size-scaled Enter delay (capped 3s) is a heuristic that breaks on very large pastes / loaded machines — the \r gets swallowed as a soft newline (the issue #22 symptom, pushed to a larger edge). Poll display_lines for the '[Pasted text]' placeholder instead. Acknowledged residual in gemini.md:7.
  - evidence: `orchestrator.py:1785-1797 fixed-timer Enter, no re-check; _enter_delay_ms cap 3000 at 451/454-464; gemini system review documents the residual race`
- **PtySession reader/writer QThreads never joined and PtySession never freed — leaks across respawns** (medium/M)
  - why: terminate() never quit()/wait()s the threads, and every PtySession is parent=orchestrator and never deleteLater'd, so each recover/respawn allocates a new session+2 threads while the old lingers for the orchestrator's lifetime. Accumulates over long multi-recovery sessions. deleteLater on detach + join threads in terminate().
  - evidence: `pty_session.py:333-349 no thread join/deleteLater; orchestrator.py:1005/1071/1142/1433 PtySession(parent=self); agent_pane.py attach/detach only reassign reference; no deleteLater on sessions anywhere`

### Orchestration & routing truth

_routing_planner is a testable spec the docs falsely call the authoritative live router, and the actual decision rules have real misclassification bugs. Plus the done-handoff loop is pure Lead prose with no orchestrator state, so verify-fail and runaway fix-loops are invisible. Fixing the spec bugs is cheap; reconciling the 'code wins' claim removes a maintainer trap._

| Need | Impact | Effort |
|---|---|---|
| Multi-role UI+API detection shadows review/test/refactor/design routing | high | S |
| Common eng verbs missing from actionable detector (optimize/investigate/upgrade/enable/patch/...) | medium | S |
| routing_planner.classify() is dead code while CLAUDE.md/docstring claim it is authoritative ('code wins') | low | S |
| No verify-fail / fix-loop state tracking — done-handoff is pure Lead prose | medium | M |
| No docs intent + actionable-but-domainless fallthrough silently routes to backend | low | S |

- **Multi-role UI+API detection shadows review/test/refactor/design routing** (high/S)
  - why: _route returns [frontend,backend] unconditionally whenever a message mentions any UI noun + any API noun, BEFORE the route table — so 'review the API endpoint for the login form' and 'test the login page and auth endpoint' become parallel impl, dropping reviewer/qa/critic and the codex cross-check. Gate the multi-role branch on an impl verb (add/build/implement) or run intent checks first.
  - evidence: `routing_planner.py:378-383 multi-role branch before _ROUTE_TABLE loop at 384; empirically review/test/refactor/design all -> [frontend,backend]; tests only pin impl-verb inputs (331,338)`
- **Common eng verbs missing from actionable detector (optimize/investigate/upgrade/enable/patch/...)** (medium/S)
  - why: _ACTIONABLE_EN omits many routine verbs so pure-imperative tasks ('upgrade next.js', 'enable dark mode', 'patch the XSS') fall to INFORMATIONAL with no role. Add the verbs. NOTE: skip the question-word-override half — tests deliberately pin 'why is X slow?' as informational, so that sub-fix conflicts with intent.
  - evidence: `routing_planner.py:48-55 _ACTIONABLE_EN; verbs return act_en=False -> INFORMATIONAL; tests/test_routing_planner.py:104,120 pin question-word inputs as informational`
- **routing_planner.classify() is dead code while CLAUDE.md/docstring claim it is authoritative ('code wins')** (low/S)
  - why: classify() has no runtime caller (Lead routes from prose CLAUDE.md); the 'authoritative ... code wins' claims at CLAUDE.md:146 and routing_planner.py:1-6 are false and could mislead a maintainer into trusting a router that never runs. Either wire a `takkub route` advisory hint, or downgrade docs to 'executable spec / regression guard'. The dead-code-as-spec decision itself is intentional (token-review notes) — only the misleading prose is the defect.
  - evidence: `routing_planner.py:401 classify(), no caller in src/; lead_context.py:204 _render_lead_context never references it; CLAUDE.md:146; token-reduction-review-2026-05-30.md:34/61 already noted the fact but left the prose`
- **No verify-fail / fix-loop state tracking — done-handoff is pure Lead prose** (medium/M)
  - why: done(note) forwards free text only — a qa 'tests FAILED' is structurally identical to a pass, no link to the impl pane verified, no fix-cycle cap/escalation. A lightweight `takkub done --result pass|fail` surfaced distinctly (natural extension of the existing --requires-commit done-side gating) would make the gate auditable and detect a stalled loop.
  - evidence: `orchestrator.py:2242 done(note) free text; cli.py:602-604 only positional note; auto-chain spec explicitly excludes fix-loop support; --requires-commit gate already does structured done-side warnings at 2262-2310`
- **No docs intent + actionable-but-domainless fallthrough silently routes to backend** (low/S)
  - why: No README/changelog/docstring route entry, and 'no domain keyword' defaults to a backend PROPOSE (human-gated, so not silent autonomous misroute). Minor: add a docs->Lead intent and consider ASK_CLARIFY for genuine no-signal actionable tasks. Note 'document X' is actually INFORMATIONAL (not backend) — finding's example was slightly off.
  - evidence: `routing_planner.py:199-276 no docs entry; 392-393 default backend; classify('update the README') -> backend`

### Testing & CI safety net

_The highest-churn, most-regressed runtime layers (wire IPC framing, the real winpty spawn -> AgentPane -> render seam) have zero integration coverage; CHANGELOG shows these exact seams produced silent hand-debugged regressions before. The Qt test harness also has latent event-loop bleed and no CI timeout, so future tests will flake or hang._

| Need | Impact | Effort |
|---|---|---|
| Real pane-spawn path (winpty launch -> AgentPane -> terminal render) has no test coverage | high | L |
| No end-to-end IPC test: cli.py socket client <-> CliServer framing is entirely mocked | medium | M |
| Qt tests share module-scoped QCoreApplication with no isolation; no pytest-timeout/--maxfail in CI | medium | M |
| _RulesGeneratorThread QThread worker is untested (main_window IS otherwise tested) | low | M |

- **Real pane-spawn path (winpty launch -> AgentPane -> terminal render) has no test coverage** (high/L)
  - why: All spawn tests stub PtySession/AgentPane to MagicMock; the live ConPTY launch, attach_session, signal wiring, and reader->render seam are unverified — and CHANGELOG shows this seam silently ate keystrokes / broke read() before, all hand-debugged. argv/env ARE covered via mocks; the uncovered part is the launch+widget+render wiring.
  - evidence: `test_orchestrator_session_uuid.py:97-103 patches PtySession.__new__; agent_pane.py only ever MagicMock'd in tests; test_terminal_widget.py:101-104 notes the integration test was removed; CHANGELOG winpty str/bytes + read()-signature regressions`
- **No end-to-end IPC test: cli.py socket client <-> CliServer framing is entirely mocked** (medium/M)
  - why: Every test calls srv._dispatch directly with a _FakeSock and monkeypatches cli._request, so the newline-framing loops are never exercised. Framing is correct by construction today (Qt readLine + client accumulate-until-newline), so value is regression-guarding future refactors. A loopback listen(0) + real cli._request test (~30-50 lines) closes it. Lower impact than originally rated.
  - evidence: `cli_server.py:67-78 framing; cli.py:60-74 recv loop; _FakeSock at test_cli_server.py:21-28; cli._request monkeypatched at test_cli.py:22`
- **Qt tests share module-scoped QCoreApplication with no isolation; no pytest-timeout/--maxfail in CI** (medium/M)
  - why: Latent cross-test event-loop bleed: timed singleShots scheduled in one test can fire during another's processEvents() (suite avoids it only via ad-hoc per-test stubbing today). No pytest-qt, no per-test event-loop drain, and no CI timeout — a test that blocks on a real socket hangs CI forever. Add qtbot/conftest drain + pending-timer assert + pytest-timeout + --maxfail.
  - evidence: `module-scoped qapp across many test files; cli_server.py:159-176 singleShot deferral; orchestrator timed timers 1842/2345/3181; pyproject has no pytest-timeout/pytest-qt; ci.yml:37 bare pytest`
- **_RulesGeneratorThread QThread worker is untested (main_window IS otherwise tested)** (low/M)
  - why: Incremental coverage gap, not a correctness defect: the new rules-generation thread's finished/failed contract has no test (project_rules.py pure helpers are covered). The cancel-race the finding leads with is near-zero (single adjacent statement, bounded by communicate(timeout=150) + thread.wait). NOTE: 'main_window completely untested' is FALSE — test_cli_bind_error.py / test_restart_cockpit.py already import and test it.
  - evidence: `main_window.py:106-144 _RulesGeneratorThread; test_project_rules.py covers helpers only; tests/test_cli_bind_error.py:20 + test_restart_cockpit.py:18 import main_window`

### Architecture & code health

_The orchestrator god-object with ~14 parallel per-pane state dicts is the root structural cause of the lifecycle-cleanup divergence bugs in the recovery cluster — extracting a PaneState object removes the entire 'forgot to pop a dict' bug class. Plus small, safe dead-code/duplication cleanups. The doc-map drift is a CI-guardable maintenance hazard._

| Need | Impact | Effort |
|---|---|---|
| Extract a PaneState/PaneRuntimeState object from the 2770-line Orchestrator god-object | high | L |
| Per-pane state cleanup duplicated/divergent across close()/done(); two+ dicts leak forever | medium | S |
| ARCHITECTURE.md module map stale: claims '40 modules', omits committed claude_update.py | medium | S |
| MainWindow._rebalance_teammates defined twice; both copies dead code | low | S |
| Consolidate two near-identical rules dialogs + duplicated --since parsing block | low | S |
| Extract update-flow UI logic (~700 lines) from MainWindow into an UpdateController | low | M |

- **Extract a PaneState/PaneRuntimeState object from the 2770-line Orchestrator god-object** (high/L)
  - why: ~14 parallel dicts all keyed {project}::{role} mean every lifecycle transition must remember to update/clear N maps — the direct cause of the cleanup-divergence and stuck-recover state-loss bugs. One PaneState removed on close eliminates the whole error class. Structural smell, not a current functional bug, but the highest-leverage refactor for the lifecycle cluster.
  - evidence: `orchestrator.py:686-3489 single class ~58 methods; ~14 state dicts at __init__ 767-852; no PaneState abstraction exists`
- **Per-pane state cleanup duplicated/divergent across close()/done(); two+ dicts leak forever** (medium/S)
  - why: _harvest_hint_ts, _last_stuck_recover (and _rate_limited_until) are written but never popped on teardown — bounded leak O(projects*roles). Immediate fix: add the pops to close(). Caveat: claim mis-stated _auto_chain_panes as leaking (done() pops it at 2338) and overstated unbounded growth — re-cycling a role overwrites the key.
  - evidence: `orchestrator.py:2127-2134 close pops vs 2295-2300 done pops diverge; writes-only at 3186/3234; _rate_limited_until (814) only self-clears on reset`
- **ARCHITECTURE.md module map stale: claims '40 modules', omits committed claude_update.py** (medium/S)
  - why: Map drops claude_update.py (committed v0.5.1 feature) from the self-update group and the count is wrong (raw 45 files; ~44 real modules excluding __init__.py + uncommitted project_rules.py). Add the module, correct the count, and extend the existing docs_verify.py to assert every src module appears in the map.
  - evidence: `ARCHITECTURE.md:48 '40 modules'; Glob=45 *.py; claude_update.py committed bec3301, absent from map (grep 0 hits); docs_verify.py exists`
- **MainWindow._rebalance_teammates defined twice; both copies dead code** (low/S)
  - why: Two MainWindow defs (178 shadowed by 901), zero callers (live callers use tab.rebalance_teammates()), line-901 body is a verbatim copy of project_tab.py. Delete both.
  - evidence: `main_window.py:178 and 901 duplicate defs; project_tab.py:82-89 canonical; grep _rebalance_teammates -> only the two defs`
- **Consolidate two near-identical rules dialogs + duplicated --since parsing block** (low/S)
  - why: Two clusters of pure tech-debt: _show_rules_preview_dialog vs _show_rules_editor_dialog are behaviorally identical with allow_regenerate=True (parameterize + delete one), and the --since HH:MM parse block is copy-pasted verbatim in the status and harvest branches (extract _parse_since). Low value, trivial effort.
  - evidence: `main_window.py:2783-2835 vs 2993-3042 dialogs; cli_server.py:207-227 (status) vs 232-252 (harvest) duplicate --since`
- **Extract update-flow UI logic (~700 lines) from MainWindow into an UpdateController** (low/M)
  - why: Quality/altitude refactor (not a bug): ~700 lines of view-controller bloat whose non-UI logic already lives in update_helper/update_worker/claude_update; an UpdateController QWidget would match the existing claude_auth_dialog/provider_dialog/logs_panel extraction pattern and make the update flow testable. orchestrator.py is the bigger target, so lower priority.
  - evidence: `main_window.py:1751-2464 update methods; logic in update_helper.py/update_worker.py/claude_update.py; precedent: provider_dialog.py/logs_panel.py extracted`

### Cockpit UX & discoverability

_For a desktop cockpit the operator can't drive the team without memorizing `takkub assign` flags — the once-built assign/role-picker UI was removed in the multi-project rewrite. Help is F1-only with no visible entry point, and several smaller polish gaps. These directly affect daily usability._

| Need | Impact | Effort |
|---|---|---|
| No GUI affordance to assign a task or spawn an arbitrary teammate role | high | M |
| Help is F1-only with zero visible entry point; no menu bar or '?' button | medium | S |
| Status bar packs ~19 widgets with no overflow handling — narrow windows clip buttons | medium | M |
| Surface 'not installed' provider substitution to the Lead (currently only toggle case is surfaced) | medium | M |
| Rules-generation busy dialog has no progress feedback (modal, static label, up to 2.5 min) | low | S |
| Minor polish: empty/exited pane guidance, raw-markdown rules preview, session-search UI, events.log project attribution | low | M |

- **No GUI affordance to assign a task or spawn an arbitrary teammate role** (high/M)
  - why: Spawn button only re-launches an idle slot; there's no UI to create a new role pane or send a task — everything funnels through hand-typed `takkub assign --role X --cwd Y`. The assign/role-picker UI was actually built (Iter 3/6) then dropped in the multi-tab rewrite (CHANGELOG ✅ entries are stale; custom_role_colors is now write-never). Add a per-pane right-click 'Assign task…' + 'Add agent' button.
  - evidence: `agent_pane.py:152-154 Spawn->spawnRequested; orchestrator.py:3479-3480 spawn no task; main_window.py:3134-3136 'future role picker UI' vestige; CHANGELOG.md:579/TASKS.md:90 stale ✅`
- **Help is F1-only with zero visible entry point; no menu bar or '?' button** (medium/S)
  - why: _show_help reachable only via F1; no menuBar/QToolBar, no help button among ~13 status widgets. New operators have no on-screen path to learn the CLI verbs the cockpit depends on. The advertised font/scroll shortcuts also have no app-level binding (xterm.js-internal only). Add a '?' ghost button / QShortcut and correct the help wording. Docs over-claim a `?` entry that was never built.
  - evidence: `main_window.py:1113-1117 F1-only; grep menuBar|QToolBar 0 matches; CHANGELOG.md:578/TASKS.md:89 over-claim '?'; no app-level ctrl+/-/0/wheel handler`
- **Status bar packs ~19 widgets with no overflow handling — narrow windows clip buttons** (medium/M)
  - why: QStatusBar doesn't scroll/collapse permanent widgets and there's no menu/toolbar fallback, so narrowing clips the rightmost chips (🔄Update first, not End Session as claimed). A '⋯ More' overflow or moving toggles into a menu adds resilience. Prior cleanup was visual-only. Several widgets are conditionally hidden, so practical impact is low-medium.
  - evidence: `main_window.py:705-737 three addPermanentWidget loops; grep menuBar|QToolBar 0 matches; no resizeEvent/min-width; cleanup was visual-only (CHANGELOG:98-103)`
- **Surface 'not installed' provider substitution to the Lead (currently only toggle case is surfaced)** (medium/M)
  - why: lead_context builds the substitution note purely from the toggle file (all_disabled()), never from find_codex/gemini_executable() — so when codex/gemini was never installed (the common case), the Lead confidently proposes them as a model-diversity cross-check when it's actually Claude-on-Claude. Feed the not-installed state into both the Lead-context section and the Lead-supplied disabled_providers. (Cross-listed under Providers.)
  - evidence: `lead_context.py:302-321 uses all_disabled() only; provider_config.py:113-147 _provider_available has 2 paths; orchestrator.py:1482-1483 not-installed only in status-bar string`
- **Rules-generation busy dialog has no progress feedback (modal, static label, up to 2.5 min)** (low/S)
  - why: Modal QDialog with only a static 'up to 2 minutes' label during a 0-150s headless claude call. NOT a freeze (work is on a QThread, busy.exec() keeps repainting, Cancel works — finding's freeze/wait()-stall claims are wrong). Reuse the existing working-pane spinner/elapsed pattern (already shipped) for an indeterminate progress bar.
  - evidence: `main_window.py:2737-2742 modal+static label; project_rules.py:37 _TIMEOUT=150; existing spinner/elapsed at TASKS.md:60/CHANGELOG.md:558`
- **Minor polish: empty/exited pane guidance, raw-markdown rules preview, session-search UI, events.log project attribution** (low/M)
  - why: Cluster of low-impact UX/observability nits: (a) empty/exited placeholder + Spawn tooltip could teach `takkub assign`; (b) rules preview/editor uses raw QPlainTextEdit (correct for an EDIT surface — add an optional Preview toggle, not a defect); (c) chatlog session-search is CLI-only and undiscoverable — a read-only decisions panel would close the loop; (d) add project= to spawn/assign/done/close/send _log_event + a project filter in LogsPanel (multi-project log is un-attributable today).
  - evidence: `agent_pane.py:194-200 placeholder; main_window.py:2805-2807 QPlainTextEdit; cli.py:519-557 search CLI-only; orchestrator.py:1471/1706/2146/2346/2091 _log_event omit project; logs_panel.py:178-198 no project filter`

### Security & trust boundaries

_Most 'security' findings are the deliberate accepted trust model of a single-user solo-dev cockpit on Max OAuth (all panes run --dangerously-skip-permissions; BLOCKED_DIRS is soft Lead-only policy). The genuinely actionable items are narrower: dead-code that READS like live protection, an MCP credential that still leaks into every pane, and a few cross-project isolation hardenings. Don't over-invest here._

| Need | Impact | Effort |
|---|---|---|
| Lead write-boundary guard is dead code that reads as live protection — wire a real PreToolUse hook or delete it | medium | M |
| User-level ~/.claude.json MCP credentials still leak into every pane despite the allowlist | medium | L |
| Cross-project isolation: single global Lead token + unvalidated from_project lets one project's Lead drive another's panes | low | M |
| Document/de-risk the rest: stale from_project comment, Windows codex sandbox bypass, transcript secret exposure | low | M |

- **Lead write-boundary guard is dead code that reads as live protection — wire a real PreToolUse hook or delete it** (medium/M)
  - why: render_lead_settings() still generates lead-guard-<project>.json with deny rules and a docstring claiming it 'blocks Lead from editing any path', but it has ZERO call sites and the Lead runs --dangerously-skip-permissions anyway. The soft-policy regression is a KNOWN deliberate tradeoff (Phase 2 unfinished); the actionable defect is the misleading dead code. Either implement the PreToolUse deny hook (lead_bash_audit.py sketches the shape) or delete render_lead_settings + its tests.
  - evidence: `orchestrator.py:1277 Lead --dangerously-skip-permissions; 1351-1355 deny-file removed comment; lead_context.py:388-419 unused guard json; grep render_lead_settings only at 49/122; tests assert guard absent`
- **User-level ~/.claude.json MCP credentials still leak into every pane despite the allowlist** (medium/L)
  - why: --strict-mcp-config does not block user-level ~/.claude.json mcpServers at CC 2.1.148 (proven by smoke test: pms bearer token loaded into the qa pane). The allowlist only governs the file the cockpit writes, giving a false sense of secret isolation. pane_env passes HOME/APPDATA through unchanged. Real fix: per-pane CLAUDE_CONFIG_DIR/HOME pointing at a sanitized config. Known-but-unfixed (pms currently fails to connect, so no live exfil yet).
  - evidence: `smoke-user-mcp-inheritance-2026-05-22.md:91-113 FAIL; shared_dev_tools.py:226-242,317-338 gate only the written file; orchestrator.py:1371-1376 --strict-mcp-config; pane_env.py passes HOME through; no fix since (git log)`
- **Cross-project isolation: single global Lead token + unvalidated from_project lets one project's Lead drive another's panes** (low/M)
  - why: One global _lead_token injected into every Lead pane; the server never checks the caller's project matches the stamped from_project, so a Lead can assign/close-all against another tab by setting TAKKUB_PROJECT. Loopback single-user, so it's an isolation/robustness gap (confused Lead cross-talk) not an external hole — 'medium security' is overstated. Bind the token per-project or reject lifecycle cmds whose from_project != caller's bound project.
  - evidence: `orchestrator.py:793 single token, 1220 same to every Lead; cli_server.py:87,105-112,161-188 token check + verbatim from_project routing; TASKS.md:172 gate stops at 'is caller Lead?'`
- **Document/de-risk the rest: stale from_project comment, Windows codex sandbox bypass, transcript secret exposure** (low/M)
  - why: Mostly doc/accepted-model items: (a) cli_server.py:84-87 comment falsely says from_project is 'informational' though it's load-bearing — fix the comment; (b) Windows codex runs --dangerously-bypass-approvals-and-sandbox (same trust level as every claude pane — NOT a codex-specific hole; only the undocumented tradeoff is the gap); (c) raw PTY transcripts capture secrets verbatim and vault notes link to them — an opt-out (TAKKUB_DISABLE_TRANSCRIPTS=1) already exists; remaining = optional redaction pass + stop advertising the path in vault notes.
  - evidence: `cli_server.py:84-87 stale comment; orchestrator.py:1129-1141 win codex bypass + 1270-1280 all panes skip-permissions; pty_session.py:280 raw write; orchestrator.py:669-676 TAKKUB_DISABLE_TRANSCRIPTS exists; codex.md:71-77 redaction declined`

### Docs, onboarding & provider polish

_A just-shipped, fully-wired feature (AI-generated project rules) has zero user-facing docs, and a couple of provider/model defaults degrade cross-checks quietly. Cheap, high-discoverability wins plus small provider correctness fixes._

| Need | Impact | Effort |
|---|---|---|
| Document the new 'AI-generated project rules' feature (README + CHANGELOG) | high | S |
| Substituted codex/gemini panes run weakest tier (Sonnet medium) — give them an Opus/high tier | medium | S |
| Auto-detect Pro-plan 1M-context hard-fail instead of relying on manual chip flip | medium | M |
| Smaller docs/provider gaps: claude health probe in UI, designer role reconciliation, codex/gemini model env knob, provider-mapping doc drift | low | M |

- **Document the new 'AI-generated project rules' feature (README + CHANGELOG)** (high/S)
  - why: Fully wired into the UI (New-project button, headless claude generation, edit-rules editor, auto-injection of per-project CLAUDE.md into every Lead spawn capped at 3000 chars) but ZERO mention in README/CHANGELOG/TASKS/ARCHITECTURE. README Step 4 still tells users to copy projects.json + notepad. Add a README section (New vs Import paths, generated <project>/CLAUDE.md location, auto-injection) + a [vNEXT] CHANGELOG entry.
  - evidence: `main_window.py:2562/2603/2944 wired; lead_context.py:266-297 auto-inject 3000-char cap; README.md:170-193 manual flow only; grep project_rules/AI-generated across docs = 0 hits`
- **Substituted codex/gemini panes run weakest tier (Sonnet medium) — give them an Opus/high tier** (medium/S)
  - why: codex/gemini aren't keys in _ROLE_MODEL_TIERS, so a claude-backed substitute falls to _DEFAULT_TEAMMATE_TIER (Sonnet medium) — weaker than the reviewer/critic (Opus high) it's meant to cross-check, degrading the substitute twice over. Add 'codex'/'gemini' to _ROLE_MODEL_TIERS at Opus/high. Only fires on the substitution path, so low-medium real-world frequency.
  - evidence: `orchestrator.py:279-284 _ROLE_MODEL_TIERS lacks codex/gemini; 287-293 fallback; 1300-1307 tier applied to all non-Lead incl substitutes (role_name unchanged per 1040/1482-1483)`
- **Auto-detect Pro-plan 1M-context hard-fail instead of relying on manual chip flip** (medium/M)
  - why: plan_tier defaults to MAX and the only way to set Pro is the status-bar chip; there's no detection of the 'Usage credits required for 1M context' banner, so a new Pro user hard-errors on the very first Lead turn — the failure plan_tier exists to prevent. The pane-banner detector machinery (rate_limit_reset_at) already exists; add a sibling detector that prompts/auto-sets Pro.
  - evidence: `plan_tier.py:8-14 documented error, 37-65 manual default MAX; main_window.py:2533-2542 only set path; no 'Usage credits' detection (the rate-limit markers don't match this banner)`
- **Smaller docs/provider gaps: claude health probe in UI, designer role reconciliation, codex/gemini model env knob, provider-mapping doc drift** (low/M)
  - why: (a) doctor.py::check_claude already probes binary+auth — surface it in the GUI as a persistent indicator and upgrade Windows auth from file-presence to an active round-trip; (b) drop '/designer' from CLAUDE.md:115 cwd line (designer is intentionally opt-in custom, not a default teammate — roster omission is correct); (c) optionally thread TAKKUB_CODEX/GEMINI_MODEL into codex/gemini pane argv for cross-check parity; (d) fix provider_config/provider_dialog docstring drift (the dialog does NOT restart on accept — finding's mechanism claim is inverted) + add a per-pane provider badge.
  - evidence: `doctor.py:69-146 check_claude (CLI-only, not in main_window); CLAUDE.md:115 designer cwd vs roster omits it (roles.py:38 intentional); orchestrator.py:1067-1070/1129-1141 no model flag; provider_dialog.py docstring drift, main_window.py:1057-1072 no restart on accept`

## Dropped — verify บอกว่าทำไปแล้ว/ไม่จริง (ไม่ต้องทำ)

- [false] **Auto-chain group is built incrementally with no up-front declaration — premature verify handoff race on parallel fire** (orchestration) — 
- [false] **No coverage measurement or gate; CI cannot detect untested modules regressing** (testing) — 
- [already-handled] **pane_env env-allowlist (secret-leak boundary) is untested as a unit despite being security-critical** (testing) — 
- [false] **CliServer status/harvest --since HH:MM time-parsing logic is duplicated and untested** (testing) — Server-side --since parsing is tested via srv._dispatch: day-rollover at tests/test_cli_status.py:214-225 (since="23:59" → since_ts in past), bad-format error at tests/test_cli_server_harvest.py:110-116 (since="bad" → ok False, "--since"/"format" in msg), and happy-path at tests/test_cli_server_harv
- [false] **routing_planner's disabled_providers context carries only the toggle state, never 'CLI not installed' — substitution note suppressed for uninstalled providers** (providers) — classify() is dead code at runtime (only called in tests) — it is the spec, the Lead model does the routing (CLAUDE.md:146). The substitution that matters happens at spawn time via effective_provider_for→_provider_available (provider_config.py:113-167, 150-167), which checks installation (lines 135-
- [false] **ANTHROPIC_AUTH_TOKEN in teammate allowlist passes a proxy bearer secret to codex/gemini panes that never use it** (security) — 
- [false] **ARCHITECTURE.md references a nonexistent 'run.bat' launcher and contradicts itself on WinPTY vs ConPTY** (docs) — 
- [false] **README still advertises 'API key' as a login option, contradicting the Max-OAuth-only invariant** (docs) — 
- [false] **No CONTRIBUTING guide; the EXPLAIN_SYSTEM output dir and project-rules generation flow have no documented contract for contributors** (docs) — Each cited concern is already addressed in-repo: Contributing section = README.md:452-456; EXPLAIN_SYSTEM/converter reuse documented = CLAUDE.md:176-179 + intentional comment routing_planner.py:469-475; design_review_html front-matter is generic (design_review_html.py:143 optional fm.get; only line 
