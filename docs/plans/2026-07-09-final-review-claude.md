# Final gate review — core upgrade plan (Claude reviewer, 2026-07-09)

Scope: final review of `docs/plans/2026-07-09-core-upgrade-plan.md` (focus: **Waves — REVISED**
section) against the two cross-checks (`2026-07-09-cross-check-codex.md`,
`2026-07-09-cross-check-gemini.md`) and against the actual working-tree code.

**Overall verdict: ✅ PASS — green-light to execute.** The REVISED waves absorbed essentially
every substantive finding from both reviewers, with file:line specificity that I verified against
real code (8/8 spot-checks confirmed). Two non-blocking SHOULD-FIX items and three minor notes
below — none block starting Wave 1.

---

## Spot-check: plan claims vs real code (all verified)

| Plan claim | File:line | Verdict |
|---|---|---|
| `last_assigned_task` replayed on crash-respawn (pointer-only would break it) | `spawn_engine.py:1871-1885` | ✅ real — `_auto_respawn` replays `cached_task` via `_send_when_ready` iff `cached_task and not spawn_resumed`. Confirms #1 must keep FULL task in state. |
| `PaneState` has **no** `assign_ts` (must be added for #5) | `spawn_engine.py:153-259` | ✅ confirmed absent — field list has `last_assigned_task`, `codex_spawn_ts`, `last_send_ts`, etc. but no assign timestamp. |
| `TAKKUB_ARTIFACTS_DIR` must join `_PANE_ENV_ALLOWLIST` | `pane_env.py:41-104` | ✅ absent from the frozenset — needs adding. **Nuance below.** |
| KNOWN_ROLES filters custom roles out of tools policy | `pane_tools_policy.py:95-101` | ✅ real — `role not in KNOWN_ROLES → continue`. Confirms #6 needs registry-as-source-of-truth. |
| 64 KiB TCP frame cap | `cli_server.py:29` | ✅ `_MAX_FRAME_BYTES = 64 * 1024`. #1 pointer keeps payloads tiny — cap is a non-issue for pointers but real for the "≤400 chars paste directly" path. |
| notify busy-retry spill cap ~30s (separate from #3's 3-min hold) | `lead_inbox.py:724-727` | ✅ `LEAD_NOTIFY_BUSY_CAP (~30 s)`. Confirms #3 needs a separate hold-counter. |
| `TAKKUB_QUEUE_FANOUT` is a real deferral queue, not info-only | `orchestrator.py:3242-3273` | ✅ `_should_queue_assign` actually defers spawns when flag on + over `machine_total_pane_cap()`. Default OFF. Central to #2 (below). |
| notify deliberately drops `tool_use` payloads | `notify.py:133-150` | ✅ `_lead_text_blocks` skips `tool_use`/`tool_result`/`thinking`. Confirms W2 "render option buttons is straightforward" was optimistic. |

---

## (a) Did REVISED waves incorporate all significant reviewer findings?

**Verdict: ✅ PASS.** Near-complete coverage. Mapping every material finding → wave:

**Codex findings — all addressed:**
- #1 replay unit (`last_assigned_task`) → Wave 2.3 "keep FULL task in PaneState.last_assigned_task" ✓
- #1 64KiB cap → Wave 2.3 "mind 64KiB TCP frame cap" ✓
- #1 artifacts allowlist → Wave 2.3 "TAKKUB_ARTIFACTS_DIR must join _PANE_ENV_ALLOWLIST" ✓
- #1 `takkub task show <id>` resolver → Wave 2.3 "TaskSpec file + takkub task show <id>" ✓
- #2 split guidance-cap/telemetry + queue flag → Wave 1 "split guidance-cap from safety telemetry; mind TAKKUB_QUEUE_FANOUT" ✓
- #3 LeadDraftState (empty/nonempty/unknown), single gate helper, per-project, separate counter → Wave 2.1 (verbatim) ✓
- #4 key project+session-uuid + share guard → Wave 2.2 ✓
- #5 assign_ts + warn-scope narrow + mtime settle → Wave 2.4 ✓
- #6 4-phase registry-first (KNOWN_ROLES + pane_tools_dialog ROLES) → Wave 3 #6 ✓
- W1 extract `_close_project_tab(confirm=False)` → Wave 1 ✓
- W2 split W2a/W2b → Wave 3 ✓
- W3 scan JSONL roots + explicit `resume_uuid` arg → Wave 3 ✓
- W4 don't reuse `_working_start` when idle → Wave 1 ✓

**Gemini findings — addressed:**
- #3 backspace-to-empty net-length clear → Wave 2.1 "backspace-to-empty clears" ✓
- All 4 cross-platform risks (file-lock retry, worktree process-tree kill, backslash→forward-slash, ConPTY 20–50ms pacing) → captured in the "Windows-specific risks logged" line + Wave 2.3/2.4 ✓

**Gaps (findings raised but NOT fully carried into REVISED waves):**
1. **Codex: transcript/harvest readers of task text** — codex flagged `done()` writes decision notes
   and "status/harvest/reporting may still expect transcript tails to explain what a pane was asked
   to do" (`orchestrator.py:1644-1649`). REVISED #1 covers *replay* but is silent on *readers*
   (`takkub status`, `harvest`) when the transcript holds only a pointer. → Minor note 3 below.
2. **Gemini: Qt grid reflow** for dynamic role add/edit — not explicitly called out in the 4-phase
   #6 breakdown (folds into phase 1/4, but should be named). → Minor.
3. **#1 `<session>` path dimension** — codex explicitly asked "which session dimension?" (per-pane
   `session_uuid` vs a takkub-session id — the assign path has no obvious current-session id). The
   REVISED wave still writes `tasks/<project>/<date>/<session>/` without disambiguating. → Minor.

---

## (b) Did the plan pick a side in a codex↔gemini conflict without reason?

Two real conflicts. Assessment:

### W2 (the headline conflict): gemini "arrow-key day one" vs codex "avoid TUI-drive" — plan chose codex

**Verdict: ✅ plan's choice is CORRECT and justified — not a reason-free pick.**

- Gemini's technical claim is *literally true*: `lead_say` writes plain text to stdin; a real
  `AskUserQuestion` picker consumes arrow keys (`\x1b[B`/`\x1b[A`/`\r`) and will not select from
  arbitrary text. If Claude emits a genuine picker, plain text can't answer it.
- BUT the plan does not naively claim lead_say drives the picker. It **reframes** the problem: W2a
  instructs Lead to ask remote-answerable questions as **numbered plain text** (cockpit-native
  protocol) → the picker is sidestepped entirely, and `lead_say "2"` works. This is an
  architectural choice with two explicit, sound reasons the plan states:
  - *"notify.py deliberately drops tool_use payloads today"* — **verified** (`notify.py:133-150`).
    Gemini's own render-buttons path also needs this plumbing, so day-one arrow-key is strictly
    *more* work, not less.
  - *"arrow-key injection collides with draft guard"* — real: injecting `\x1b[B`+`\r` into a Lead
    pane races the exact draft state #3 is built to protect.
- Net: gemini's approach = new tool_use plumbing **+** arrow-key injection **+** draft-guard
  coordination **+** ConPTY keystroke pacing = the most fragile corner of the whole plan, shipped
  first. Codex's MVP is genuinely lower-risk. **Plan chose correctly, with stated reasoning.**

**SHOULD-FIX (residual gap, non-blocking):** the plan says W2b is *"deferred/avoided"*. "Avoided"
is too strong — if Claude Code autonomously fires a real `AskUserQuestion` (it can, for its own
reasons, regardless of the plain-text instruction), the remote user is **silently stuck**. W2a
should include a **detect-and-surface fallback**: notify *can see* the dropped `tool_use` block, so
when Lead is blocked on a real picker, surface *"Lead is waiting on a desktop picker — answer on
desktop"* rather than hanging. This closes the honest hole without building the fragile TUI-drive.

### #2: gemini "keep an advisory cap / wave-sequencing hint" vs codex "remove from Lead prompt entirely" — plan chose codex

**Verdict: ⚠️ SHOULD-FIX — plan sided with codex without addressing gemini's concrete risk.**

- The user's stated goal for #2 *is* "remove machine capability limits", so removing the hard `K ≤ cap`
  aligns with intent, and codex's "remove guidance cap, keep total-pane telemetry" is directionally right.
- **But** gemini raised a specific failure mode the REVISED wave does not answer: with the hard cap
  gone from the Lead prompt **and** `TAKKUB_QUEUE_FANOUT` default OFF (**verified** — real queue at
  `orchestrator.py:3250`, off by default), Lead assumes infinite capacity → can fire 10+ concurrent
  spawns on a 2-core box → thrash/freeze. The info-only total-pane *warning* fires after the fact and
  doesn't shape Lead's planning.
- Gemini's softer compromise — replace the numeric cap with a **qualitative** advisory ("sequence
  independent tasks in waves based on per-role cost") — satisfies the user's "no hard limit" goal
  **and** mitigates the freeze. The plan drops this entirely.
- **Recommendation:** in Wave 1 #2, don't leave the Lead prompt fully silent on capacity. Keep a
  short *qualitative* advisory (no hard K), so "unlimited" doesn't read as "spawn everything at once."
  Cheap, aligns with the north-star, and closes gemini's only real objection.

### #5 warn-scope: codex {qa,critic,designer} vs gemini {qa,designer,frontend} — plan chose codex

**Verdict: ✅ fine.** Plan's `qa/critic/designer` matches the roles the existing code already treats
as screenshot-producing (`orchestrator.py:1986-1994`). Defensible, consistent. `frontend` is a
reasonable optional add later, not a conflict worth blocking on.

---

## (c) Wave ordering + dependency sanity — Wave 2 track #3→#4→#1→#5→#99

**Verdict: ✅ PASS — ordering is sound.**

- **#3 first** — correct: both reviewers say #4 depends on #3's draft guard; #3 also builds the
  shared `_lead_can_accept_injection()` helper that #1's pointer-send and #4's slash-inject both use.
- **#4 after #3** — correct: rides the guard, keyed project+session-uuid.
- **#1 after #4** — fine: #1 touches `_assign_dispatch`/`PaneState`/`pane_env`; sequencing (not
  parallel) with #3/#4 avoids merge conflict on the shared assign/delivery paths. #1 can now use the
  guard helper.
- **#5 after #1** — correct and *necessary*: both add `PaneState` fields and both touch `done()` /
  the state-pop. Verified `done()` pops state — assign_ts must be captured before the pop
  (Wave 2.4 says exactly this). Sequential avoids a `PaneState`/`done()` conflict.
- **#99 last** — safe. Note: #99 (enter-swallow / ready-marker ordering) touches the *same*
  lead_inbox/pty delivery path #3 rewrites, so doing it after #3 means it inherits the new draft
  gate — correct direction. (Could equally sit adjacent to #3; last is fine.)

**Wave 1 independence** — #2 (exec_mode/lead_context), W1 (main_window tab lifecycle + remote/api),
W4 (remote/api activity) are mutually independent and independent of Wave 2. ✓ W1 does mutate the
ProjectTab/orchestrator registry pair the architecture map flags as dangerous, but the plan's
"extract headless `_close_project_tab`" is the right containment.

**Wave 3** — #6 (own XL track), W3, W2 are independent; W3+W2 are PWA/remote and can run parallel to
#6. ✓

---

## Minor implementation notes (fold into wave tickets, not blockers)

1. **`TAKKUB_ARTIFACTS_DIR` — allowlist is necessary but not sufficient.** The allowlist *permits*
   an env var through; it does not *set* it. Note in `pane_env.py:87-98`: `TAKKUB_ROLE`/`PROJECT`/
   `PORT_FILE` are listed "for clarity" but actually **stamped explicitly** at spawn. #1 must
   likewise *compute and stamp* the artifacts dir into each pane's env, not merely allowlist it.
2. **#1 pointer for the ≤400-char direct-paste path** still flows through the 64KiB frame — trivially
   under cap, but the *composed* task (goal block + role decl + spec) is what's measured; keep the
   "paste directly under 400 chars" threshold measured on the composed payload, not the raw task.
3. **#1 transcript readers** — verify `takkub status` progress rendering and `takkub harvest`
   artifact-scan still behave when the pane transcript holds only a pointer (codex's readers gap).
4. **#6 Qt grid reflow** (gemini) — name it explicitly in the phased breakdown (dynamic grid slot
   assignment + collision guard), it's currently implicit.
5. **#1 `<session>` path dimension** — pin down which id (`PaneState.session_uuid` vs a takkub
   session id) before writing the tasks path; the assign path has no obvious takkub-session id today.

---

## Bottom line

- (a) coverage — **PASS** (3 minor gaps, none blocking)
- (b) conflicts — **W2: correctly resolved with stated reasons (PASS)** + 1 residual W2a fallback to
  add; **#2: SHOULD-FIX — keep a qualitative capacity advisory** to answer gemini's thrash concern
- (c) ordering — **PASS**, #3→#4→#1→#5→#99 dependency chain is correct
- code spot-checks — **8/8 verified against real files**

Execute Wave 1 now. Fold the two SHOULD-FIX items into the Wave 1 (#2) and Wave 3 (W2a) tickets
before those waves land; the five minor notes into their respective wave tickets.
