# Roadmap Audit Cross-Check — Gemini Findings (Third Brain)
**Date:** 2026-07-10  
**Author:** Gemini (Third-Brain Planner & Reviewer)  
**Status:** Completed

---

## 🔍 Overview of Verdicts (A1–A7, B1–B4)

This cross-check audit evaluates every claim in the roadmap document [2026-07-10-roadmap-audit.md](file:///C:/Users/monch/WebstormProjects/agent-takkub/docs/reviews/2026-07-10-roadmap-audit.md) against the actual implementation in the workspace root.

| # | Item | Status | Verified Evidence (file:line) & Rationale | Cross-Platform / Multi-Provider Notes |
|---|------|--------|-------------------------------------------|---------------------------------------|
| **A1** | File-based task delivery | ✅ | [orchestrator_text.py:434](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/orchestrator_text.py#L434) (`_task_handoff_pointer`). Composed tasks exceeding `TASK_HANDOFF_THRESHOLD` (400 chars) are written to `tasks/` as markdown, and the pointer is pasted into the PTY. | **Cross-Platform:** Slashes are converted to `/` to work seamlessly on both Windows and macOS/Linux. Wording is generic ("file-read tool") so it fits any provider. |
| **A2** | Fan-out capacity cap removal | ✅ (code)<br>✅ (doc) | [lead_context.py:467-494](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/lead_context.py#L467-L494) (no numeric caps in prompt). [exec_mode.py:14-19](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/exec_mode.py#L14-L19) and [orchestrator.py:3486](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/orchestrator.py#L3486) (`_warn_lead_over_cap()`). | **Audit Correction:** The stale documentation at [CLAUDE.md:75](file:///C:/Users/monch/WebstormProjects/agent-takkub/CLAUDE.md#L75) claiming a hard K cap has been updated to reflect the qualitative wave scheduling approach. |
| **A3** | Lead draft-hold | ⚠️ | State machine in [lead_draft_state.py](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/lead_draft_state.py) and wiring in [lead_inbox.py](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/lead_inbox.py) (e.g. line 308-325). **User report of leaks is confirmed.** Root causes identified below. | Windows and Mac PTY behaviors can exacerbate escape sequence chunk splits. |
| **A4** | `/remote-control` auto-bridge | ⛔ | Cancelled. Verified code is clean and references are documented as removed (e.g. [app.py:436](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/app.py#L436), [spawn_engine.py:1767](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/spawn_engine.py#L1767)). | N/A |
| **A5** | QA screenshot/evidence to Lead | ✅ | [orchestrator.py:1591](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/orchestrator.py#L1591) (`_scan_done_evidence`) scans artifacts dir, prioritizes per-role subdirs, and appends to done notices. | **Cross-Platform:** Includes `_evidence_stat_mtime` retry loops to bypass transient Windows file locking. |
| **A6** | UI Role & Skill Manager | ❌ | No GUI dialog exists. Only basic models exist: `roles.py`, `role_memory.py`, `skill_audit.py`, and tools policy. | Large net-new feature. |
| **A7** | Task Ledger | ⚠️ | A1 is in place for long tasks but lacks comprehensive metadata, per-task generation, status tracking, and central ledger presentation. | Net-new extension of A1. |
| **B1** | Close project (web) | ✅ | [app.js:405](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/remote/static/app.js#L405) and [app.js:473](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/remote/static/app.js#L473) (`closeProject`) send POST `api/close`. | Verified. |
| **B2** | Q&A/brainstorm web | ⚠️ | Free-text comments work ([app.js:1163](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/remote/static/app.js#L1163)). Choice picking falls back to generic reply chips ([app.js:905](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/remote/static/app.js#L905)) and error banner. Missing native `AskUserQuestion` option picking. | Needs frontend + API work. |
| **B3** | Resume / session picker (web) | ✅ | [app.js:1174](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/remote/static/app.js#L1174) sheet lists sessions via `api/lead/sessions` and resumes via `api/lead/resume`. | Verified. |
| **B4** | Pulse status display (web) | ✅ | Pulse page lists active panes and runtimes via `api/pulse` mapped to `activity(orch)` in [api.py:91](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/remote/api.py#L91). | Data minimized (B2) compliant. |

---

## 🐛 A3: Root Cause Analysis (Why the Draft-Hold still leaks)

Analyzing [lead_draft_state.py](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/lead_draft_state.py) and [lead_inbox.py](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/lead_inbox.py) reveals three primary reasons why user drafts are leaked and clobbered:

### 1. The Done Notice Reaper Staleness Escalation (Primary Leak Path)
In [lead_inbox.py:1075-1087](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/lead_inbox.py#L1075-L1087), the done notice reaper (`_reap_pending_done_notices`) triggers after a message remains pending for `_DONE_NOTICE_STALE_S` (60 seconds) in an alive Lead. 
```python
if lead.session.is_at_ready_prompt() and self._lead_can_accept_injection(project_ns):
    self._pending_done_since.pop(project_ns, None)
    self._flush_pending_done_notices(project_ns)
else:
    # alive but either not-ready or holding an unsubmitted draft...
    since = self._pending_done_since.setdefault(project_ns, now)
    if now - since >= _DONE_NOTICE_STALE_S:
        self._force_deliver_done_notices(project_ns)
```
- **The Bug:** The reaper lumps "busy/not-ready" (which could be a false-negative) together with "holding an unsubmitted draft" (which is a genuine user block).
- **The Result:** If the user spends more than 60 seconds editing/drafting a prompt while a teammate is done, the reaper will force-flush the notice. This writes the done notice payload into the Lead's live session and presses Enter, dragging the user's unsubmitted draft along with it.

### 2. Bare Escape (Esc Key) Premature Clears
In [lead_draft_state.py:193](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/lead_draft_state.py#L193), if an `Esc` byte (`0x1B`) is processed and does not match a valid CSI/SS3 mouse sequence, it is treated as a proven bare Escape key:
```python
m = _CSI.match(data, i) or _SS3.match(data, i)
if m is None:
    st = _cleared()  # proven bare Esc key
```
- **The Bug:** Pressing the physical `Esc` key (often done by users to close search/completion overlays or switch windows) immediately clears the draft state back to `EMPTY`.
- **The Result:** The state tracker believes the input buffer is empty, while the typed text is actually still fully visible on the input line. Any subsequent engine injection clobbers it immediately.

### 3. Draft Length Drift on Unhandled Control Keys
Keystrokes like Tab auto-completion, Ctrl+W (delete word), Ctrl+H, or Alt+Backspace are treated as no-ops or default controls in `advance_draft_state`, yet they actively change the text length on the PTY input line.
- **The Bug:** If a user types `takkub status` (13 chars), presses Ctrl+W (ignored, but shell deletes the word `status`), and then presses Backspaces 13 times. The `draft_len` will decrement past the real string length, either underflowing/clearing to `EMPTY` prematurely, or leaving the tracker in a stuck `NONEMPTY` state which eventually hits the 60s reaper escalation.

---

## 🛠️ A7: Task Ledger & Checklist Design Outline

### 1. Spawning / Writing Phase (Reusing A1)
Extend `_task_handoff_pointer` in [orchestrator_text.py](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/orchestrator_text.py) to write **every** task assignment to a markdown file, regardless of character length (removing the `TASK_HANDOFF_THRESHOLD` gate for ledger records, though we can still paste directly for short tasks).
- **File Path:** `runtime/tasks/<project_ns>/<YYYY-MM-DD>/<HHMMSS>-<role>.md`
- **Metadata Header:** Write frontmatter headers detailing metadata:
```markdown
---
task_id: <uuid>
date: 2026-07-10T13:13:40+07:00
role: frontend
cwd: C:/Users/monch/WebstormProjects/agent-takkub/web
status: working
---
# Task Specification
<Task details...>
```

### 2. Done/Finalization Phase (Flipping to ✅)
Hook this at the `Orchestrator._on_pane_done` done handler in [orchestrator.py:1741](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/orchestrator.py#L1741).
- **Behavior:** Find the task markdown file matching the active pane's session, read its frontmatter, update `status: done`, and optionally prepend a `✅` to the markdown title or update a checkbox `- [x]`.

### 3. Architecture Comparison: Per-File Status vs. Central Ledger JSON
* **Approach A: Per-File Status (Markdown frontmatter)**
  * **Pros:** Highly modular. Each task is self-contained. Zero risk of write-race locks in parallel execution since each pane only writes its own task file. Native integration with Obsidian Dataview.
  * **Cons:** Requires file reads/parsing to generate a checklist index or UI dashboard view.
* **Approach B: Central Ledger JSON (`tasks.json`)**
  * **Pros:** Trivial to read and render in a UI or API response. Faster queries for the project's checklist state.
  * **Cons:** Parallel panes (A2 execution mode) reporting done at similar times will trigger concurrent write requests, risking write-locks, corruptions, or database collisions.
* **Recommendation:** **Hybrid Approach.** Write independent markdown task files to avoid write-race locks during parallel teammate execution. When the UI or API needs the dashboard, scan/compile the files on-demand or use an asynchronous SQLite DB index that handles concurrency gracefully.

---

## 🎨 A6: Role & Skill UI Design Outline

To build a role/skill dialog builder that interfaces with `roles.py`, `role_memory.py`, and `skill_audit.py`:

### 1. PyQt6 Dialog Components (`pane_tools_dialog.py` extension)
* **Create-Role Panel:**
  * Add a dialog button `+ New Role` which pops a dialog containing inputs for:
    * `Name` (lowercase identifier for CLI commands like `--role newrole`).
    * `Label` (display string in the cockpit grid).
    * `Grid Row & Column` coordinates (restricting to grid constraints).
    * Color (using standard `QColorDialog` for visual identification).
* **Skill Catalog Tab:**
  * List all role files available in `.claude/agents/*.md`.
  * Display a side-by-side markdown viewer to inspect instructions.
  * Display a "Skill Overlap Warning" banner using `skill_audit.py` (e.g. if the new role has > 0.6 similarity with an existing one).

### 2. Catalog Persistence & Default Assignment
* Save newly created roles into `.claude/agents/<name>.md`.
* Save layout settings and custom roles registry in `~/.takkub/custom-roles.json` which is read at boot by `roles.py`'s `register_role()`.
* Map default tool capabilities (MCP/plugins) for the new roles inside `~/.takkub/pane-tools.json` (supported by `pane_tools_policy.py`).
