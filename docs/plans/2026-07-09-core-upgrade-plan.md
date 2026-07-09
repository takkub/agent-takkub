# Core upgrade plan — 10 items (2026-07-09)

Lead's feasibility cross-check (code read, file:line verified). Reviewers: verify each verdict
against the actual code, hunt blind spots, and propose better approaches where you see them.

## Core engine

### 1. File-based task handoff + artifacts out of real project folders — verdict: ✅ (M)
- Today: task is pasted whole into the pane — `lead_inbox.py:360` `_send_when_ready` →
  `_paste_payload` + Enter. Root of paste-swallowed bug family (#22/#26) that needs
  `_delayed_enter_verified` self-healing.
- Plan: in `_assign_dispatch` (`orchestrator.py:729`) write the composed task spec to
  `~/.takkub/runtime/tasks/<project>/<YYYY-MM-DD>/<session>/<HHMMSS>-<role>.md`, paste only a
  short pointer (`[ROLE: …] อ่าน task เต็มจากไฟล์ <abs path> …`). Tasks under ~400 chars keep
  pasting directly.
- Artifacts: extend existing convention `runtime/exports/<date>/<project>/screenshots/`
  (`orchestrator.py:1988`) → inject env `TAKKUB_ARTIFACTS_DIR=<runtime>/exports/<date>/<project>/<session>/`
  into every pane via `pane_env.py` + update all role files: temp files/images/test scripts go
  there, never into the project repo. Optional: on `done()` check project git status for junk
  files and warn Lead.
- Known caveat: agent compliance is prompt-level (can't hard-enforce).

### 2. Remove machine capability limits — verdict: ✅ (S)
- Cap lives at `exec_mode.py:45` (`MAX_FANOUT = 4`) + `machine_fanout_cap()` /
  `machine_total_pane_cap()` (CPU/RAM derived). Consumers ~8 sites: `lead_context.py:471`
  (injected into Lead prompt), orchestrator warn paths (`orchestrator.py:3196-3344`),
  `user_actions.py:665`.
- Plan: remove cap from Lead prompt entirely; keep total-pane warning as info-only (it never
  blocked spawns anyway).

### 3. Draft-typing race — done notice drags user's half-typed text — verdict: ✅ (M)
- Root cause: `_pump_lead_notify` (`lead_inbox.py:753`) gates only on
  `session.is_at_ready_prompt()` (pane idle) — it cannot see a user draft sitting in claude's
  input box, so paste+Enter submits the draft along with the notice.
- All keystrokes already flow through `orchestrator._on_pane_input` (`orchestrator.py:3655`).
- Plan: per-Lead-pane "draft pending" tracker — printable bytes ⇒ draft on; Enter/Esc/Ctrl+C ⇒
  clear. Pump holds delivery while draft pending (separate, longer cap than
  LEAD_NOTIFY_BUSY_CAP; timeout ~3 min ⇒ spill to durable `_pending_done_notices` + red dot,
  both already exist). Same guard applies to `_flush_pending_lead_cc` and
  `inject_slash_command_when_ready`.

### 4. Run /remote-control on every Lead spawn — verdict: ✅ (S)
- Mechanism exists: `inject_slash_command_when_ready` (`lead_inbox.py:259`). Today fires only on
  resume (`main_window.py:728` `_on_pane_resumed`) and first user Enter (`_on_lead_input`).
  Fresh boot deliberately silent (hint chip manual).
- Plan: policy change — fire after every Lead spawn (boot, tab open, respawn). claude no-ops
  when bridge already active, so double-fire is safe. Keep `_lead_first_input_fired` set to
  avoid duplicates within a session.

### 5. QA must attach screenshot evidence to every done — verdict: ✅ (S–M)
- qa.md already mandates shots under `runtime/exports/<date>/<project>/screenshots/` + paths in
  the done note — but it's discipline-only.
- Plan: engine-side in `done()` (`orchestrator.py:1506`): auto-scan the shot dir for images
  newer than the pane's assign timestamp, append absolute paths to the done notice
  (`📸 evidence: …`) for ANY role, not just qa. Lead (Claude Code) can Read the images
  directly. Test-ish roles that finish with zero new shots get a warning flag in the notice.

### 6. Role manager + skill library + shipped defaults — verdict: ✅ (L, phased)
- Today: roles are a static tuple (`roles.py:28`); custom roles half-supported
  (`custom_role_colors` in `project_tab.py:102`, no creation UI, no persistence); behavior lives
  in `.claude/agents/<role>.md`; per-role MCP/plugins already has the full pattern
  (`pane_tools_policy.py` + 🔧 Tools chip + CLI).
- Plan: (a) Role Manager UI — create/edit role (name, color, grid slot, default cwd, .md
  template), persist `~/.takkub/roles.json` + generate `.claude/agents/<role>.md`;
  (b) Skill library page — scan `~/.claude/skills/` + project skills, per-role skill matrix
  (same UX as Tools chip), inject at spawn; (c) default role+skill bundle as package data,
  installed by `takkub doctor --fix`.

## Web/mobile (PWA — src/agent_takkub/remote/)

### W1. Close project button — verdict: ✅ (S)
- `open_project` exists (`src/agent_takkub/remote/api.py:135`, reaches main_window via `orch.parent()`).
  Add `close_project` calling the `_on_tab_close_requested` path minus the Qt confirm dialog
  (confirm on the phone instead).

### W2. Brainstorm Q&A — tappable options + comment — verdict: ⚠️ feasible but fragile (M–L)
- Questions with options (AskUserQuestion) appear in the Lead jsonl as `tool_use` blocks;
  `notify.py` already parses jsonl → rendering option buttons in the PWA is straightforward.
- Risk: answering requires driving claude's TUI picker (arrow keys + Enter injected into the
  pane) — coupled to Claude Code's rendering, version-fragile.
- Recommended MVP first: quick-reply chips + comment box via existing `lead_say`
  (`src/agent_takkub/remote/api.py:118`); full TUI-drive later.

### W3. Resume button + session picker — verdict: ✅ (M)
- Engine already spawns with `--resume <uuid>` (`spawn_engine.py:1568`) but only auto within the
  5-min window (`RESUME_WINDOW_SEC`, `spawn_engine.py:86`). Desktop resume button just injects
  `/resume` (interactive TUI picker — not remote-drivable).
- Plan: new API — list recent lead sessions per project (jsonl mapping already known to
  `notify.resolve_lead_jsonl`), then respawn Lead with an explicit chosen uuid (new spawn
  override arg).

### W4. Pulse shows Lead — verdict: ✅ (S)
- `activity()` (`src/agent_takkub/remote/api.py:90`) filters to `state == "working"` panes only, so an idle Lead
  never appears. Include Lead always with working/idle + runtime. Respect the data-minimization
  bar (§7.3): role + state + runtime only.

## Waves — REVISED after codex+gemini cross-check (2026-07-09)

Cross-check findings: `2026-07-09-cross-check-codex.md` + `2026-07-09-cross-check-gemini.md`.
Both reviewers agreed feasibility on all 10; key revisions adopted:

- **Wave 1 (independent, low risk):** #2 (split guidance-cap from safety telemetry; mind
  `TAKKUB_QUEUE_FANOUT` opt-in queue at orchestrator.py:3250; **SHOULD-FIX from final review:
  don't leave Lead prompt silent on capacity — replace numeric cap with a QUALITATIVE advisory
  "sequence independent tasks in waves by per-role cost", no hard K**), W1 (extract headless
  `_close_project_tab(project, confirm=False)` shared by desktop+remote), W4 (Lead entry must not
  reuse `_working_start` when idle)
- **Wave 2 (assign/done/inject core — one track, ordered):**
  1. #3 draft guard FIRST — `LeadDraftState` per project (states: empty / nonempty /
     unknown_nonempty for paste·arrows·history-recall; Ctrl+U/W/C, Esc clear; backspace-to-empty
     clears), single gate helper `_lead_can_accept_injection()` used by pump + CC flush + slash
     inject; separate hold-counter from LEAD_NOTIFY_BUSY_CAP
  2. #4 rides on the guard — trigger on successful Lead spawn path keyed project+session-uuid
  3. #1 file handoff — keep FULL task in `PaneState.last_assigned_task` for crash replay
     (spawn_engine.py:1871), paste pointer only; TaskSpec file + `takkub task show <id>`;
     `TAKKUB_ARTIFACTS_DIR` must join `_PANE_ENV_ALLOWLIST` (pane_env.py:41); pointer paths
     normalized to forward slashes; mind 64KiB TCP frame cap (cli_server.py:29)
  4. #5 — add `assign_ts` to PaneState (captured before done() pops state), evidence-append for
     all roles, zero-shot WARNING only for qa/critic/designer; mtime settle ~1s + Windows
     PermissionError retry on scan
  5. **NEW: codex enter-swallow fix (issue #99)** — verify false-stop + codex ready-marker
     ordering; belongs with this track (same lead_inbox/pty code)
- **Wave 3 (large / phased):**
  - #6 = XL, 5 phases (user requirement 2026-07-09: "เผื่อหยิบ CLI อื่นๆ มาใช้ด้วย ให้ครอบคลุม
    ทุกอย่าง" — pluggable providers beyond claude/codex/agy):
    (0) **ProviderSpec registry** — pull every vendor-specific knob out of code into a data
    descriptor per CLI: binary discovery, spawn argv builder (autonomy/sandbox flags),
    ready/busy footer markers (pty_session table), context-injection strategy
    (CLAUDE.md / AGENTS.md / system-prompt flag), MCP-config adapter (claude
    --strict-mcp-config · codex -c mcp_servers overrides · agy plugin import — issue #100),
    paste/enter quirks (#99 family). Adding a new CLI = write one ProviderSpec + role file,
    zero engine edits. `VALID_PROVIDERS` (provider_config.py:40) reads from this registry.
    (1) dynamic role registry as source-of-truth (role names duplicated in
    ≥7 static tables incl. pane_tools_policy KNOWN_ROLES + pane_tools_dialog ROLES), (2) tools
    policy reads registry, (3) role .md template generation + doctor-installed defaults,
    (4) skill library + matrix (skill semantics differ per provider — resolved per
    ProviderSpec from phase 0)
  - W3 — session listing requires scanning Claude JSONL roots (resolve_lead_jsonl only knows the
    OPEN session); new explicit `resume_uuid` spawn arg, cwd-matched
  - W2 split: **W2a** MVP quick-reply chips + comment via `lead_say` (+ Lead instructed to ask
    remote-answerable questions as numbered plain text — cockpit-native protocol, no TUI drive)
    **+ SHOULD-FIX from final review: detect-and-surface fallback — when Lead fires a REAL
    AskUserQuestion picker (notify can see the dropped tool_use block), PWA shows "Lead is
    waiting on a desktop picker — answer on desktop" instead of hanging silently**;
    **W2b** true AskUserQuestion TUI-drive deferred (notify.py deliberately drops
    tool_use payloads today; arrow-key injection collides with draft guard)
- Windows-specific risks logged: screenshot file-locks (retry), worktree removal needs
  process-tree kill first, ConPTY keystroke pacing 20–50ms.
- Minor notes from final review (fold into wave tickets — `2026-07-09-final-review-claude.md`):
  #1 must STAMP `TAKKUB_ARTIFACTS_DIR` at spawn, not merely allowlist it; #1 400-char threshold
  measured on the COMPOSED payload (goal block + role decl + spec); #1 verify `takkub status` /
  `harvest` readers still work with pointer-only transcripts; #1 pin down the `<session>` path
  dimension (PaneState.session_uuid vs takkub session id) before writing tasks path; #6 name the
  Qt grid-reflow work (dynamic slot assignment + collision guard) explicitly in phase 1/4.
- Every wave: full pytest before push; CI matrix windows+macos must both be green.
- Final gate review: **PASS** (`2026-07-09-final-review-claude.md`, 8/8 code spot-checks) —
  green-light Wave 1.
