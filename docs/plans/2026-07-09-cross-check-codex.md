# Codex cross-check: core upgrade plan (2026-07-09)

Scope: cross-check of `docs/plans/2026-07-09-core-upgrade-plan.md` against the current working tree and `docs/architecture/godfile-map.md`.

## Executive summary

I agree with most feasibility calls, but the plan understates several coupling points:

- Item 1 is feasible, but **M is too small unless session resume/replay/history semantics are designed**. `PaneState.last_assigned_task` currently stores the full task and auto-respawn can replay it into a blank session. Replacing the transcript text with a pointer changes more than paste length.
- Item 3 is feasible, but a byte-level draft tracker is not enough unless it handles paste/control sequences/IME/backspace-to-empty and only gates human-driven Lead input, not orchestrator injection into non-Lead panes.
- W2's "JSONL parse -> option buttons" claim is too optimistic. Current remote notify code intentionally drops `tool_use` payloads and emits only coarse activity.
- Item 5 needs new state (`assign_ts`) and role filtering. Current `PaneState` has no assignment timestamp.
- Item 6 is feasible but likely **XL/phased**, not just L, because role identity is duplicated in multiple static tables and policy validators.

## Item 1. File-based task handoff + artifacts out of project folders

Evidence check: agree with cited paths. `_assign_dispatch` starts at `src/agent_takkub/orchestrator.py:729`; it stores `ps_assign.last_assigned_task = task` at `orchestrator.py:804`, later sends the same full task at `orchestrator.py:863`. `_send_when_ready` is `src/agent_takkub/lead_inbox.py:360` and writes `_paste_payload(_sanitize_pane_text(task))` at `lead_inbox.py:388-389`, followed by verified Enter at `lead_inbox.py:390-395` and `790-802` for Lead notifications. `_paste_payload` is in `src/agent_takkub/orchestrator_text.py:348`. Existing screenshot convention is real at `orchestrator.py:1988` and `2087`.

Verdict: agree on feasibility, disagree on size. This is **M-L**, not M, if implemented without regressions.

Blind spots:

- The plan only calls out paste reliability, but the task text is also the durable replay unit. `PaneState.last_assigned_task` is replayed after crash/blank respawn in `src/agent_takkub/spawn_engine.py:1871-1885`. If it stores only a pointer, the replay works only if the file still exists and the spawned agent obeys the pointer. If it stores the full text while the pane receives only a pointer, then transcript/history no longer contains the actual assignment but replay still does.
- `done()` captures transcript path and writes decision notes at `orchestrator.py:1644-1649`; status/harvest/reporting may still expect transcript tails to explain what a pane was asked to do. The architecture map's hidden IPC edge matters: `takkub assign` reaches `_assign_dispatch` via `cli_server.py:352-388`, not by importing orchestrator directly, so CLI/API contract and payload size limits must be considered. The TCP frame cap is 64 KiB at `src/agent_takkub/cli_server.py:29`.
- Session resume is a key dependency. If a pane resumes with `--resume <uuid>` (`spawn_engine.py:1561-1569`), the actual task may not be in the Claude transcript, only the pointer. That can be acceptable, but it is a product choice: future readers of the pane transcript may need to resolve an external file.
- Artifacts env is also an allowlist change. `TAKKUB_ARTIFACTS_DIR` must be added to `_PANE_ENV_ALLOWLIST` in `src/agent_takkub/pane_env.py:41-103`; simply setting it in spawn will be easy, but documenting it in role files is still prompt-level.
- The proposed runtime path includes `<session>`, but there is no obvious "current takkub session id" in the cited assign path. There are per-pane Claude UUIDs in `PaneState.session_uuid` (`spawn_engine.py:153-155`) and runtime sessions elsewhere; the plan should specify which session dimension is meant.

Better approach:

Use a structured `TaskSpec` file and store both forms in state: `last_assigned_task_pointer` plus `last_assigned_task_inline` or a checksum-backed snapshot. Paste a short pointer only for large tasks, but keep the full text in orchestrator state/pending task file for replay and audit. Add a `takkub task show <id>` or status/report hook so Lead can resolve task files without relying on transcript text. For artifacts, add `TAKKUB_ARTIFACTS_DIR` in `pane_env.py`, set it once in spawn, and update role docs to prefer it while keeping `runtime/exports/<date>/<project>/screenshots` compatibility for screenshot scanners.

## Item 2. Remove machine capability limits

Evidence check: mostly verified. `MAX_FANOUT = 4` is at `src/agent_takkub/exec_mode.py:45`; `machine_fanout_cap()` and `machine_total_pane_cap()` are at `exec_mode.py:74` and `102`. Lead prompt injection uses the fanout cap at `src/agent_takkub/lead_context.py:471`. Orchestrator warning/queue paths reference total cap at `orchestrator.py:3193-3345`. User actions reference the fanout cap at `src/agent_takkub/user_actions.py:665`.

Verdict: agree feasible, **S-M** rather than pure S if tests and wording are cleaned up.

Blind spots:

- There is an opt-in actual queue, not only info-only warning: `_should_queue_assign` can block/defer spawns when `TAKKUB_QUEUE_FANOUT` is enabled (`orchestrator.py:3250-3273`). The plan says total-pane warning never blocked spawns; true by default, but not true with the queue flag.
- Removing the Lead prompt cap without revisiting queue/warning copy may produce inconsistent UX: Lead thinks unlimited, engine still warns or queues under a flag.

Better approach: split "guidance cap" from "safety telemetry." Remove `machine_fanout_cap()` from Lead planning text, keep `machine_total_pane_cap()` only for warnings/optional queue, and rename prompt copy/tests to make clear it is advisory or flag-gated.

## Item 3. Draft-typing race

Evidence check: agree with root cause. `_pump_lead_notify` gates only on `lead.session.is_at_ready_prompt()` at `src/agent_takkub/lead_inbox.py:753`; it then writes pasted text at `lead_inbox.py:782-785`. All pane input currently flows through `orchestrator._on_pane_input` at `src/agent_takkub/orchestrator.py:3655-3671`, which just writes bytes to the PTY. `_flush_pending_lead_cc` directly writes queued CCs without any ready/draft guard at `lead_inbox.py:608-646`. Slash command injection also only checks ready prompt at `lead_inbox.py:259-307`.

Verdict: agree feasible, **M** is fair if limited to Lead-pane input and covered by focused tests.

Blind spots:

- Printable-byte tracking is insufficient. Paste can arrive as bracketed paste/control sequences; arrow keys and Ctrl+A/Ctrl+E should not mark a draft; Backspace/Delete must clear only when the tracked buffer becomes empty; Ctrl+U/Ctrl+W/Ctrl+C/Esc should clear; Enter should clear only after user submission, not when an injected Enter is sent.
- IME/composition can produce bytes late or in multi-byte chunks. A byte-level UTF-8 decoder with "has any text" state is safer than treating every byte between 0x20 and 0x7e as printable.
- Arrow keys/history recall are hard: pressing Up at an empty prompt can populate the input line with a previous command without emitting printable bytes. If the tracker does not treat Up/Down as "unknown draft risk", the bug can persist.
- The guard must be per project Lead pane, not global, matching the multi-tab sender fix in `orchestrator.py:3655-3667`.
- It must not stall automated delivery forever. Current spill cap is about 30s (`lead_inbox.py:724-727`), while the plan wants about 3 minutes and durable red-dot behavior. That needs a separate retry counter/reason so a human draft does not look like Lead busy/wedged.

Better approach:

Create `LeadDraftState` keyed by project. Feed it only from the actual Lead `AgentPane` sender. Track states `empty`, `nonempty`, and `unknown_nonempty` where Up/Down/paste-start put it in a conservative hold until Enter/Esc/Ctrl+C/Ctrl+U or a ready-prompt plus timeout clears it. Gate `_pump_lead_notify`, `_flush_pending_lead_cc`, and `inject_slash_command_when_ready` through a single helper such as `_lead_can_accept_injection(project_ns)`. Add tests for paste, arrows/history, backspace-to-empty, Ctrl+U, Esc, Ctrl+C, Enter, and multi-byte text.

## Item 4. Run `/remote-control` on every Lead spawn

Evidence check: verified. `inject_slash_command_when_ready` exists at `src/agent_takkub/lead_inbox.py:259-307`. Current auto-bridge is triggered from `_on_pane_resumed` at `src/agent_takkub/main_window.py:728-737` and first Lead input at `main_window.py:739-745` onward. The plan is correct that fresh boot is deliberately silent today.

Verdict: agree feasible and **S**, with one caveat.

Blind spots:

- This overlaps item 3. Slash injection into a Lead with user draft text has the same corruption risk unless it uses the new draft guard.
- The plan says "every Lead spawn" but also "avoid duplicates within a session." The actual session identity should use pane/session UUID or project-scoped fired state reset on respawn; `_lead_first_input_fired` alone is project-scoped and currently suppresses later injections for that project.

Better approach: move the trigger to the successful Lead spawn path or `paneResumed`/spawn signal with a project+session UUID key, and run it through the same injection queue/draft guard as done notices.

## Item 5. QA screenshot evidence on every done

Evidence check: qa role docs do mandate screenshot paths under `runtime/exports/<date>/<project>/screenshots/` (`.claude/agents/qa.md:116` and examples around `122`/`162`). `done()` starts at `src/agent_takkub/orchestrator.py:1506`; normal notice creation is at `orchestrator.py:1551-1565`. Screenshot directories are scanned for status/stall only for `qa`, `critic`, and `designer` at `orchestrator.py:1986-1994` and `2084-2095`.

Verdict: agree feasible, **M** not S-M unless warning scope is narrow.

Blind spots:

- `PaneState` has no `assign_ts`; its fields are listed at `src/agent_takkub/spawn_engine.py:153-259`. The plan's "newer than assign timestamp" requires adding and setting one in `_assign_dispatch`, then capturing it before `done()` pops state at `orchestrator.py:1523-1545`.
- "ANY role" with zero-shot warning will be noisy for backend/devops/reviewer/codex tasks. The current code only treats `qa`, `critic`, `designer` as screenshot-producing roles for progress/status.
- File race: screenshots may still be writing when `done()` scans. A simple mtime scan can pick half-written files.

Better approach: add `assign_ts` to `PaneState`, append evidence paths for all roles when present, but warn only for configured evidence-expected roles or when task text/role indicates UI smoke. Scan common image extensions, require `mtime >= assign_ts`, and optionally ignore files modified in the last ~1s to avoid half-written captures.

## Item 6. Role manager + skill library + shipped defaults

Evidence check: static default roles are at `src/agent_takkub/roles.py:28-66`. `project_tab.py:102` owns `custom_role_colors` as cited by the plan; current `main_window.py:393-402` can render unknown/custom roles with default/user color. Per-role tools policy exists, but it hard-codes `KNOWN_ROLES` at `src/agent_takkub/pane_tools_policy.py:35-50`, and the Tools dialog has another static `ROLES` tuple at `src/agent_takkub/pane_tools_dialog.py:39-51`.

Verdict: agree feasible, disagree on size. This is **XL if done as a complete Role Manager + skills matrix**, or L only for a narrow first phase.

Blind spots:

- Hidden string-key role coupling from the architecture map is real. Role names appear in `roles`, `provider_config`, `pipeline_config`, `pane_tools_policy`, `pane_tools_dialog`, `shared_dev_tools`, role markdown files, routing/prompt logic, and orchestrator literals. A persisted role registry must become the source of truth or the manager will create roles that other subsystems reject.
- Tool policy currently filters unknown roles out (`pane_tools_policy.py:95-101`), so custom roles cannot reliably participate in the existing Tools chip policy.
- Skill injection has provider-specific semantics. Claude skills, Codex skills, and Gemini instructions are not equivalent.

Better approach: phase this as:

1. Dynamic role registry API and persistence, with all static role consumers reading from it.
2. Tools policy validation switched from static `KNOWN_ROLES` to the registry.
3. Role markdown/template generation.
4. Skill library after roles are dynamic.

## W1. Close project button

Evidence check: `open_project` exists at `src/agent_takkub/remote/api.py:135-150` and reaches MainWindow dynamically through `orch.parent()`. There is no adjacent `close_project` API today. `cli_server.py` has `close` and `close-all` commands for panes, not project tabs (`cli_server.py:397-400`).

Verdict: agree feasible and **S-M**, not necessarily S.

Blind spots:

- Closing a project tab is a UI lifecycle action, not just an orchestrator close. The architecture map flags `mw_tab_project_lifecycle` and signal bridge as dangerous because it mutates ProjectTab maps and orchestrator registry together.
- The desktop close path likely has persistence/open-tab side effects and confirmation. A remote close must preserve those side effects while replacing only the confirm dialog.

Better approach: expose a MainWindow method like `_close_project_tab(project, confirm=False)` that both desktop and remote call, rather than duplicating `_on_tab_close_requested` logic in `src/agent_takkub/remote/api.py`.

## W2. Brainstorm Q&A: tappable options + comment

Evidence check: AskUserQuestion is intentionally allowed only in Lead panes and denied for teammates at `src/agent_takkub/spawn_engine.py:1521-1536`. JSONL parsing exists in `src/agent_takkub/remote/notify.py`, but current behavior deliberately drops `tool_use` payloads: `_lead_activity` maps tool names to coarse activity at `notify.py:114-130`; `_lead_text_blocks` skips `tool_use` and `tool_result` at `notify.py:133-150`; live polling emits only text or coarse "working" at `notify.py:461-479`. `lead_say` exists at `src/agent_takkub/remote/api.py:118-132`.

Verdict: agree with the plan's caution that full TUI-drive is fragile, but disagree that rendering option buttons is straightforward from current notify code. Call it **M for MVP quick replies, L/fragile for real AskUserQuestion control**.

Blind spots:

- The PWA currently does not receive the option schema. It receives text conversation and coarse activity, not raw tool_use blocks.
- Driving the TUI picker by arrow keys couples to Claude Code rendering and focus state. It also collides with item 3: if the user has a Lead draft, injected arrow/Enter can corrupt that draft.
- JSONL "tool_use" records may be followed by tool_result/assistant text; correlating a question, options, and final answer needs IDs, not just latest activity.

Better approach:

Do the recommended MVP first: display quick-reply chips generated from visible Lead text or a new explicit lightweight question event, and submit through `lead_say`. For true AskUserQuestion, avoid TUI driving if possible: add a Lead instruction that remote answers arrive as plain text ("Answer: X") and let Claude consume it, or introduce a cockpit-native question protocol that Lead can call through `takkub send/lead_say` rather than the vendor TUI picker.

## W3. Resume button + session picker

Evidence check: `RESUME_WINDOW_SEC = 5 * 60` is at `src/agent_takkub/spawn_engine.py:86`. Spawn uses `--resume <uuid>` only when prior UUID/cwd/recent exit match within the window at `spawn_engine.py:1561-1569`; otherwise it creates `--session-id` at `spawn_engine.py:1570-1575`. `remote.notify.resolve_lead_jsonl` resolves only the open Lead pane's JSONL from its current session UUID at `src/agent_takkub/remote/notify.py:250-258`.

Verdict: agree feasible, **M-L** rather than M if it includes a reliable session picker.

Blind spots:

- The plan says "jsonl mapping already known to notify.resolve_lead_jsonl", but that helper only resolves the current open Lead session. Listing recent sessions per project requires scanning Claude JSONL roots and mapping UUIDs to project/cwd/session metadata.
- Spawn currently has no explicit resume override arg; adding one touches `spawn()` and probably `MainWindow`/remote API.
- Project/cwd disambiguation matters. Existing code deliberately avoids `--continue` bleed by matching UUID and cwd (`spawn_engine.py:1548-1554`).

Better approach: first implement read-only recent session listing keyed by project/cwd/session UUID with timestamps, then add an explicit `resume_uuid` spawn parameter guarded to Lead only. Keep the 5-minute auto-resume logic separate from user-chosen resume.

## W4. Pulse shows Lead

Evidence check: `activity()` filters to `pane.state == "working"` at `src/agent_takkub/remote/api.py:90-115`, so idle Lead is omitted. The data-minimization comment explicitly says role + project + runtime only (`src/agent_takkub/remote/api.py:90-101`).

Verdict: agree feasible and **S**.

Blind spots:

- Runtime for idle Lead should not reuse `_working_start`, because it may be `None` or stale. The plan says working/idle + runtime; define runtime as "since state changed" if available, or omit/zero when idle.
- Including Lead always should still avoid task text/cwd and should be project-scoped like current `activity()`.

Better approach: include `{role:"lead", state:"idle|working", runtime_sec}` for each project with an open Lead pane, and keep other roles filtered or include only working roles as today.

## Wave sequencing notes

- Move item 5 into Wave 2 with items 1 and 3 because all touch assign/done/pane state.
- Item 4 should wait for or share the item 3 injection guard if the intent is "every Lead spawn."
- W2 should be split: W2a quick-reply via `lead_say` can ship earlier; W2b true AskUserQuestion/TUI control should be later or avoided.
- Item 6 should be its own multi-phase track after a role registry source-of-truth design.

