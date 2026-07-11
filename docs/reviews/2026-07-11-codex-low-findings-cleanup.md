# Codex LOW-finding cleanup (2026-07-11)

Follow-up on the 3 LOW findings from `docs/reviews/2026-07-11-full-system-review-codex.md`
(the 2 HIGH findings are out of scope for this task — separate work item).

## 1. Resume-token leak on rejected explicit resume — FIXED

**Was:** `spawn_engine.py`'s non-Lead branch minted and registered the per-pane capability
token (`_mint_pane_token`, line ~1419 pre-fix) *before* the explicit `resume_uuid` was validated
against the JSONL store (`_resume_uuid_matches_cwd`, was checked at line ~1681). A rejected
resume returned early from that later check, leaving a valid, never-revoked token registered for
`(project_ns, role_name)` — bounded severity because the next mint for the same key revokes the
predecessor and the leaked secret was never delivered to a child process (codex's own
assessment), but still a real ordering bug.

**Fix:** moved the `resume_uuid` validation to right after `spawn_cwd` is settled for both the
Lead and teammate branches (both need only `project_ns` / `resume_uuid` / `spawn_cwd`, all
available well before token minting) — now it runs *before* `_mint_pane_token` is ever called, so
a rejected resume never registers a token in the first place. The original validation site
(further down, right before `argv.extend(["--resume", ...])`) now just trusts the earlier check
and wires the flag.

**Test:** extended the existing
`TestSpawnResumeUuid::test_invalid_resume_uuid_rejected_before_spawn` with
`assert not orch._pane_tokens` (codex's suggested regression assert) instead of adding a new
test — same scenario, stronger assertion.

**Files:** `src/agent_takkub/spawn_engine.py`, `tests/test_resume_session_picker.py`.

## 2. Dormant slash-command injector — REMOVED (decision + evidence below)

**Finding:** `LeadInboxMixin.inject_slash_command_when_ready` (~140 LOC + `_slash_inject_busy`/
`_slash_inject_queue` state) had zero production callers — its only two callers (the
`/remote-control` auto-bridge and the ↻ Resume button) were deliberately removed in commit
`28136df` (2026-07-10, "Resume cancelled" 2-day saga: the auto-bridge raced claude's `/resume`
picker and cancelled it). The method's own docstring at the time already said it was "kept as
generic slash-command-injection infrastructure but currently has no callers."

**Decision: remove, not keep.** Evidence checked before deciding:

- `docs/plans/2026-07-09-core-upgrade-plan.md` (the only place a near-term reuse could be
  hiding) mentions this exact method twice — plan item #4 (which *became* the auto-bridge that
  was just ripped out) and the shared-guard note for item #3. Neither describes a future caller
  beyond the one that got removed.
- The one plan item that *does* look superficially related — **W2b, "true AskUserQuestion
  TUI-drive"** — is explicitly a different mechanism: arrow-key navigation of claude's interactive
  picker, not typing a slash command + Enter. Re-using `inject_slash_command_when_ready` for it
  wouldn't even fit the shape of the problem (picking an option vs. submitting a whole command).
  W2b is also explicitly "deferred" in that doc, with no target wave.
- Commit `28136df`'s own message ("`inject_slash_command_when_ready` ยังอยู่แต่ไม่มี caller")
  reads as ambivalence from the person who wrote it, not a concrete plan.
- codex's own recommendation (suggested fix order #4): "Remove or isolate the production-dead
  slash injector."

No caller, no concrete plan that would reuse it as-is, and a real next candidate (W2b) needs a
structurally different mechanism — so it's dead weight, not held-in-reserve infrastructure.
Removed.

**What was removed:**
- `LeadInboxMixin.inject_slash_command_when_ready` (`lead_inbox.py`, ~140 LOC)
- `Orchestrator._slash_inject_busy` / `_slash_inject_queue` state + their init-comment block
  (`orchestrator.py`)
- `_REMOTE_BRIDGE_MAX_WAIT_MS` constant (`spawn_engine.py`) — already orphaned before this
  cleanup; it existed only to size a call to the method being removed here and had no other
  reference anywhere in `src/`.
- `tests/test_slash_inject_serialize.py` (entire file — dedicated to this method's call-
  serialization behavior, now moot)
- `TestInjectSlashCommandDraftGuard` (3 tests) from `tests/test_lead_draft_guard.py` — the
  draft-guard integration tests specific to this method; `_pump_lead_notify` and
  `_flush_pending_lead_cc`'s own draft-guard tests in the same file are untouched, since the
  shared `_lead_can_accept_injection()` gate itself is still very much alive.
- Docstring/comment references to the method across `lead_inbox.py` (module docstring + the
  draft-guard block comment), `orchestrator.py`, `spawn_engine.py`, and
  `tests/test_lead_draft_state.py`'s scope note.

**Files:** `src/agent_takkub/lead_inbox.py`, `src/agent_takkub/orchestrator.py`,
`src/agent_takkub/spawn_engine.py`, `tests/test_lead_draft_guard.py`,
`tests/test_lead_draft_state.py`, `tests/test_slash_inject_serialize.py` (deleted).

## 3. Stale `docs/architecture/godfile-map.md` — REFRESHED

Verified every LOC count, every listed module's existence, and (as close to exhaustively as
practical for a LOW-severity doc task) every method name in every cluster against the current
source with grep, rather than eyeballing it. Findings beyond what codex flagged:

- `orchestrator.py` is **4,045 LOC** (codex already caught the map's stale 2,618 claim; confirmed
  and updated), `main_window.py` is **1,270 LOC** (map said 3,997/1,048 depending on which line —
  both wrong).
- `broadcast_actions.py` **never shipped** — confirmed zero matches for any of its 4 named
  methods anywhere in `src/`, and the two UI buttons that would have called them
  (`_on_ui_review_clicked`, `_on_bug_check_clicked` in `user_actions.py`) are also gone. This
  wasn't a rename; the feature was cut.
- `transcript_scan.py`, also listed as an existing module, doesn't exist either — those helpers
  (`prune_old_transcripts`, `scan_artifacts`, …) live directly in `orchestrator_text.py` and
  apparently always have.
- Import-linter is at **18 contracts**, not the map's claimed 13 (matches codex's own gate run:
  "18 kept, 0 broken").
- Three clusters the map implied were extracted (`command_surface`, `session_persistence`,
  `watchdogs`) were **never actually split into separate modules** — every method in them is
  still, today, directly inside `orchestrator.py`. This is the real reason the file regrew past
  4k LOC, not "the 4 already-extracted modules leaked code back in" (they didn't — verified each
  extracted module still owns exactly the methods it's supposed to).
- `main_window.py`'s UI-mixin method lists had drifted in both directions: some methods gone
  (`_on_resume_clicked` — deliberately, same commit as finding #2 above; `_refresh_version_label`,
  `_copy_version_to_clipboard`, `_show_changelog`, `_on_claude_update_clicked`,
  `teammate_split`/`main_split` as `MainWindow` attributes, `_on_tab_bar_clicked`,
  `_plus_tab_index` — all confirmed gone, not renamed), some added (npm self-update sub-flow,
  exec-mode/auto-resume/remote status chips, tutorial overlay, task dock wiring, `_close_project_tab`
  per the W1 core-upgrade-plan item, `_on_team_chip_clicked` for the 2026-07-11 Users tab).
- Updated fan-out numbers from `depgraph.json`: `orchestrator.py` is now the repo's highest
  **static** fan-out (23, not the map's old 20) — `main_window.py` dropped to 19 (not 24, and no
  longer "สูงสุดในเรปอ" as the old doc claimed).
- Removed the `inject_slash_command_when_ready` mentions (finding #2) and updated the
  `/remote-control` auto-bridge history note to say the generic injector is now fully gone too,
  not "kept but callerless."

Rewrote the doc's status section to explain *why* it drifted (real feature work landing directly
in the two god-files instead of new modules, not doc neglect) and added a one-line discipline
note since there's no automated staleness check for this file the way `depgraph.json` has one.

**Files:** `docs/architecture/godfile-map.md`.

## Gates run

- Targeted tests only (per project convention — full suite is QA's batch-gate job, not owed by a
  doc/cleanup task): `test_resume_session_picker.py`, `test_lead_draft_guard.py`,
  `test_lead_draft_state.py`, `test_pane_token_mint.py`, `test_spawn_codex_argv.py`,
  `test_spawn_gate.py`, `test_spawn_log_dedup.py`, `test_orchestrator_auto_respawn_replay.py`,
  `test_orchestrator_notify_lead.py`, `test_peer_cc_durability.py`, `test_delivery_unconfirmed.py`
  — **378 passed, 0 failed**.
- `ruff check` on every touched `.py` file — clean.
- `ruff format --check` on every touched `.py` file — already formatted.
- `lint-imports` — **18 kept, 0 broken** (unchanged from before this task — no import edges were
  added or removed, only a method body and its state deleted).

## Out of scope (explicitly, per task spec)

The 2 HIGH findings from the codex review (Lead-notice dequeue-before-write race,
overlapping-pipeline pane ownership) were **not** touched here — this task was scoped to the 3
LOW findings only.
