# System Review — agent-takkub cockpit
**Date:** 2026-05-29  
**Reviewer:** reviewer pane  
**Scope:** orchestrator · routing_planner · pane_env · cli_server · cli · issues · cheatsheet (codex_agents_md / gemini_md)

---

## 1. Correctness

### C-1 · `send()` CC condition carries redundant guard (non-critical, minor)
**File:** `orchestrator.py:1728`
```python
if from_role and from_role not in (None, LEAD.name) and to_role != LEAD.name:
```
`from_role not in (None, ...)` is always True here because the outer `if from_role` already rules out falsy values. The `None` in the tuple is unreachable. Not a functional bug, but misleading to a reader who assumes the `None` case is live.  
**Fix:** `if from_role and from_role != LEAD.name and to_role != LEAD.name:`

### C-2 · `restore_teammates()` sets `_recent_exits` before spawn then may confuse future cooldown logic
**File:** `orchestrator.py:2499`
```python
self._recent_exits[_exit_key(project, role)] = {"cwd": cwd, "ts": time.time()}
ok, _ = self.spawn(role, cwd=cwd, project=project)
```
`spawn()` clears the entry (`del self._recent_exits[_ekey_spawn]`) on success, so the write is immediately discarded. The intent was to give crash-recovery bookkeeping a baseline — but since `spawn()` erases it, this line is a no-op. If `spawn()` fails, the stale entry remains and the freshly failed pane's next auto-respawn sees a false-positive "recent exit" with the current timestamp — not harmful but semantically wrong.

### C-3 · `scan_artifacts` follows directory symlinks via `rglob`
**File:** `orchestrator.py:156-174`
```python
for p in base.rglob("*"):
    if p.is_symlink() or p.is_dir():
        continue
```
`p.is_symlink()` skips symlinked *files*, but `Path.rglob()` on Python < 3.12 follows symlinked *directories* without any flag. A circular directory symlink (symlink → parent) causes infinite recursion. The `seen: set[pathlib.Path]` doesn't protect against this because each iteration resolves to a new path.  
**Fix:** Use `os.walk(base, followlinks=False)` or catch `RecursionError`, or add `p.is_symlink()` check before descending.

### C-4 · `_flush_pending_lead_cc()` writes `b"\r"` via lambda but message body is `str`
**File:** `orchestrator.py:1627-1631`
```python
lead.session.write(payload)          # str via _paste_payload()
QTimer.singleShot(...,
    lambda s=lead.session: s and s.write(b"\r"),   # bytes
)
```
Every injection site mixes `str` payload + `b"\r"` Enter. This works if `PtySession.write()` accepts both. But `_inject_auto_chain_handoff` at line 1662 also does `lead.session.write(prompt)` (str) then `write(b"\r")`. If `PtySession.write()` is strictly typed at some point, all these sites break silently. No test currently catches the type mismatch.  
**Nit:** Standardise on `str` or `bytes` throughout; document the contract in `PtySession.write()`.

### C-5 · `requires_commit` gate silently passes when git is unavailable
**File:** `orchestrator.py:1895`
```python
except Exception:
    dirty = ""  # can't check; allow done
```
If git is not installed, the cwd is not a repo, or subprocess is blocked, `done()` accepts the call silently. An operator using `--requires-commit` on a non-git project gets no warning. The log captures `done_rejected` on dirty-tree, but there's no `done_commit_gate_skipped` event for the failure-to-check path.  
**Fix:** Log a `done_commit_gate_skipped` event with the exception so the operator knows the gate didn't fire.

### C-6 · `pane_status_report()` has O(roles × day-dirs) filesystem walk
**File:** `orchestrator.py:2253-2271`
```python
for day_dir in sorted(sessions_root.iterdir(), reverse=True):
    ...
    for f in sorted(proj_dir.iterdir()):
```
For each role, the method scans all session day-dirs. With months of session history, this can be slow. Not a correctness bug but worth noting as a latency concern for `takkub status`.

---

## 2. Architecture

### A-1 · `orchestrator.py` is still 2,964 lines after extractions
The previous `pane_env`, `lead_context`, `vault_mirror` extractions were good, but `Orchestrator` still mixes pane lifecycle, watchdog timers, vault writes, session snapshots, and hot.md generation in one class. Suggested further decomposition:
- `SessionStorage` — `_save_decision_note`, `end_session`, `write_resume_briefs`, `write_daily_digest`
- `WatchdogManager` — `_check_idle_teammates`, `_check_stuck_panes`, `_auto_recover_stuck`

This would make unit-testing each concern independently feasible without spinning up a `QObject`.

### A-2 · `__all__` re-export antipattern in `orchestrator.py`
**File:** `orchestrator.py:92-119`  
`orchestrator.py` re-exports ~20 symbols from `pane_env`, `lead_context`, `vault_mirror` in a large `__all__` block "for backwards compat". This hides where symbols actually live and makes grep-based navigation unreliable (searching for `_build_pane_env` imports might land on orchestrator, not pane_env). Tests importing from `orchestrator` also depend on this — if they were updated to import from the canonical module, the `__all__` shim could be removed and the module boundary would be clean.

### A-3 · `_flush_pending_lead_cc` and `_flush_pending_done_notices` are structural duplicates
**File:** `orchestrator.py:1609-1631` vs `1671-1691`  
Both methods check the queue, check Lead alive, pop-and-iterate, write+delay, and log. The only difference is the dict name and log event. Extract:
```python
def _flush_pending_queue(
    self, project_ns: str, queue: dict, log_event: str
) -> None: ...
```

### A-4 · Hardcoded role groups in multiple places
`("qa", "critic", "designer")` appears at lines 2147, 2241, and implicitly in `broadcast_design_review`. `"qa"` alone appears in the `CHROME_BIN` probe at spawn. These should be named constants in `roles.py`, e.g.:
```python
SCREENSHOT_ROLES = frozenset({"qa", "critic", "designer"})
```

### A-5 · `_send_when_ready` and `inject_slash_command_when_ready` are near-identical
**File:** `orchestrator.py:1476-1574`  
Both poll `is_at_ready_prompt()` at 500ms intervals, share the same `sent[0]` guard, and share the same timeout fallback. The only differences: `_send_when_ready` flips `pane.set_state("working")` and falls back on timeout; `inject_slash_command_when_ready` drops the command silently on timeout. A common `_deliver_when_ready(role, payload, on_timeout, set_working)` with kwargs would eliminate the duplication.

### A-6 · `routing_planner.py` `FIRE_ONESHOT` kind is dead code
**File:** `routing_planner.py:27`  
`ActionKind.FIRE_ONESHOT` exists and is returned for one-shot codex/gemini queries. But CLAUDE.md says "Lead ห้ามใช้ `takkub codex` / `takkub gemini` (one-shot)". The `FIRE_ONESHOT` path may never be acted upon in current Lead behavior. If it's intentionally preserved for future use, a comment explaining this would prevent future cleanup from removing it.

---

## 3. Security

### S-1 · Path traversal in vault writes via unvalidated `project` name
**Files:** `orchestrator.py:2033, 2108, 2607`
```python
sessions = vault / "01-Projects" / project / "sessions"
```
`project` is passed from `done()` via `_resolve_project()`. When project comes from `active_project()` (JSON file read), the name is not run through `validate_name()`. If `projects.json` is edited to include a project like `"../../../sensitive"`, `pathlib.Path / project` would construct a traversal path. `pathlib` resolves `..` literally:
```python
>>> Path("/vault/01-Projects") / "../../../etc" / "sessions"
PosixPath('/vault/01-Projects/../../../etc/sessions')
```
**Fix:** Call `validate_name(project, "project")` inside `_save_decision_note` and `write_daily_digest`, or add a new "safe path component" helper that rejects `..` and path separators.

### S-2 · `_auto_trust()` silently accepts Claude's folder trust prompt
**File:** `orchestrator.py:1414-1438`  
Every spawned pane has `_auto_trust()` called, which auto-presses Enter on the "Trust this folder?" modal. If a user opens a project whose cwd contains a malicious `CLAUDE.md` (e.g., from a cloned repo), the cockpit auto-trusts it without any user confirmation step. The cockpit's `--dangerously-skip-permissions` already implies broad trust, so this may be acceptable, but the interaction between the two trust surfaces should be documented.

### S-3 · TCP token in Lead pane env is inherited by all Lead subprocess tools
**File:** `orchestrator.py:1030`  
`TAKKUB_LEAD_TOKEN` is injected into Lead's env. Any subprocess the Lead pane spawns (Bash tool, shell commands) inherits this token. A compromised subprocess (e.g., via a malicious script in the project) can use the token to call Lead-only orchestrator commands (spawn/assign/close). For a local developer tool this is an acceptable threat model — document it explicitly.

### S-4 · `issues.py` silent GitHub → local fallback: data loss risk
**File:** `issues.py:166-198`  
When `gh` CLI fails (expired token, network partition, transient error), `new_issue()` silently writes to a local `.takkub_issues.json`. The user sees no indication the issue didn't reach GitHub. If `gh` recovers later, `takkub issue list` switches to GitHub and the local issues are invisible (no sync). An issue reported during a `gh` outage is effectively lost.  
**Fix:** Print a clear warning (not silently degrade): "gh unavailable — issue stored locally at `.takkub_issues.json`. Sync manually when GitHub is accessible."

### S-5 · `codex_crash_dump` filename uses unescaped `role_name`
**File:** `orchestrator.py:1312`
```python
safe_project = project.replace("/", "_").replace("\\", "_")
dump_path = dump_dir / f"{ts_str}-{safe_project}-{role_name}.log"
```
`role_name` comes from `validate_name()` — likely safe. But `safe_project` only strips slashes; characters like `;`, `$`, spaces, and null bytes are preserved. On Windows, filenames with spaces or `$` can cause issues in some tools. Recommend stripping to `[a-zA-Z0-9_-]` only.

---

## 4. Maintainability

### M-1 · `_pending_cc_path` files accumulate and are never deleted
**File:** `orchestrator.py:1581-1607`  
`_save_pending_cc(project_ns)` writes `[]` when the queue is emptied — but never deletes the file. Over time, the runtime dir accumulates `pending-lead-cc-<project>.json` files for every project ever opened. `_load_pending_cc()` at startup re-loads them all (finding empty lists, doing nothing). Add:
```python
if not self._pending_lead_cc.get(project_ns):
    self._pending_cc_path(project_ns).unlink(missing_ok=True)
```

### M-2 · `GEMINI.md` cheatsheet is missing the codex "Override rule" section
**File:** `gemini_md.py`  
`CODEX_AGENTS_MD` contains a "Override rule for inline `[ROLE: ...]` directives" section explaining that `ห้าม spawn subagent` applies only to AI subagents, not shell commands like `takkub done`. `GEMINI_MD` does not have this section. Gemini panes receive the same `[ROLE: ... ห้าม spawn subagent]` task prefix. Without the override notice, Gemini may — like earlier Codex — refuse to call `takkub done` as a shell command. The `_CODEX_TASK_NOTICE` prepend only applies to Codex panes, not Gemini.  
**Fix:** Add the equivalent override section to `GEMINI_MD`, or extend `_rewrite_task_for_codex`-style logic to Gemini assigns.

### M-3 · Mixed use of `None` and `0.0` for "unset" timestamps
**File:** `orchestrator.py:2709`  
`_idle_state` entries use `{"first_idle_ts": None, "last_reminder_ts": 0.0}` — mixing `None` and `float` for "unset". Similarly `_last_send_ts` defaults to `0.0`, `_last_stuck_recover` defaults to `0.0`, but `_codex_spawn_times` can pop None. A typed `@dataclass` for idle bookkeeping would remove the nullable ambiguity.

### M-4 · `routing_planner.py` Thai lookbehind covers only 4 compounds
**File:** `routing_planner.py:108`
```python
_UI_TH_FRAGMENT = r"หน้าจอ|(?<!ก่อน)(?<!ข้าง)(?<!ด้าน)(?<!เบื้อง)หน้า(?=\s*[/a-zA-Z])|ปุ่ม"
```
Lookbehind excludes ก่อน/ข้าง/ด้าน/เบื้อง but not "หน้าอก" (chest), "หน้าผาก" (forehead), or "ก่อนหน้านั้น" (before that). The `(?=\s*[/a-zA-Z])` lookahead provides real disambiguation (หน้า must be followed by `/` or an ASCII route/word) — this is the effective guard, not the lookbehinds. Documenting that the lookahead is the primary filter would help future maintainers.

### M-5 · `_CODEX_TASK_NOTICE` idempotence check uses substring match
**File:** `orchestrator.py:305`
```python
if _CODEX_TASK_NOTICE in task:
    return task
```
Any task whose body happens to contain the exact marker string (e.g., a test task that quotes the notice) would silently skip the rewrite. The marker includes the Thai text `"[orchestrator note] อ่านก่อนเริ่มงาน"` which is unlikely to appear in real tasks, but fragile as a uniqueness signal. A less collision-prone sentinel (UUID or magic bytes) would be more robust.

---

## 5. Blind Spots

### B-1 · `scan_artifacts` potentially follows circular symlinks (expanded from C-3)
As noted above: on Linux/macOS with Python < 3.12, `Path.rglob("*")` follows symlinked directories. If a project has `ln -s . loop`, `rglob` will descend infinitely. The `seen` set tracks `pathlib.Path` objects (not resolved paths), so it doesn't catch cycles. This can freeze the cockpit during harvest or pane_status_report calls. **Recommended fix at the rglob call site:** wrap in a try/except for `RecursionError` or port to `os.walk(followlinks=False)`.

### B-2 · `issues.py` dual-backend divergence is invisible in `list`
**File:** `issues.py:234-314`  
`list_issues()` delegates entirely to GitHub when `gh` is available, and entirely to the local file when it's not. There's no merge, no warning that some issues may be local-only. A user who filed issues during a `gh` outage has them in `.takkub_issues.json`, but when `gh` recovers, `takkub issue list` shows only GitHub issues. The user has no way to detect the local backlog without inspecting the file manually.

### B-3 · `ANTHROPIC_AUTH_TOKEN` in pane env is not audit-logged
**File:** `pane_env.py:50-51`  
`ANTHROPIC_AUTH_TOKEN` is in `_PANE_ENV_ALLOWLIST` but excluded from the crash dump's `env_keys` output (the dump only lists key names, not values). However, the transcript files (`<role>-<HHMMSS>.transcript.log`) may contain any text that appeared in the PTY — including error messages that echo env vars. If Claude Code's error path prints env var names or partial values, the transcript captures it. The vault mirror then copies it to Obsidian, which may sync to cloud. This is unlikely but is an implicit data path to track.

### B-4 · Gemini pane gets `-y` (yolo) flag without equivalent to `--dangerously-skip-permissions`
**File:** `orchestrator.py:877-879`
```python
gemini_argv = [
    gemini_bin,
    "-y",  # yolo: skip per-command approval prompts
]
```
Gemini's `-y` suppresses approval prompts, which parallels Codex's `--ask-for-approval never`. But unlike Claude's `--dangerously-skip-permissions`, the Gemini CLI's approval scope and what it actually permits is less documented in the codebase. If Gemini CLI changes its `-y` semantics in a future version, the cockpit might silently become more or less permissive. A comment referencing the Gemini CLI docs/version tested would help future maintainers.

### B-5 · Hot.md write on every `done()` event + every 60s tick can cause Obsidian conflicts
**File:** `orchestrator.py:1977`  
`_write_hot_md()` is called directly after `done()` in addition to the `_hot_md_timer` 60s tick. With multiple parallel panes finishing simultaneously, several consecutive hot.md writes occur within milliseconds. If Obsidian Sync or iCloud is active on the vault, rapid overwrites can generate sync conflicts (`hot.md vs hot (conflicted copy).md`). The content is ephemeral (status snapshot), so conflicts are harmless, but they accumulate as junk files. Consider debouncing with a short `QTimer.singleShot(2000, ...)` after `done()` rather than writing immediately.

### B-6 · `_pending_cc_path` and `_save_pending_cc` use project_ns in filenames without path-safe validation
**File:** `orchestrator.py:1581`
```python
return RUNTIME_DIR / f"pending-lead-cc-{project_ns}.json"
```
`project_ns` from `_resolve_project()` goes through `validate_name()` only when passed explicitly; the fallback `active_project()` returns the raw project name from `projects.json`. If the JSON contains a name with spaces or special chars (e.g. `"my project"`), the filename is `pending-lead-cc-my project.json` — valid on most OSes but unusual. Recommend sanitising: `project_ns.replace(" ", "_")` before the f-string, or running all project names through `validate_name`.

---

## Summary

| Axis | Severity | Top finding |
|---|---|---|
| Correctness | Medium | `scan_artifacts` can infinite-loop on circular symlinks (B-1/C-3) |
| Architecture | Low | Orchestrator still 2,964 lines; `_flush_pending_*` duplication; hardcoded role sets |
| Security | **High** | Path traversal in vault writes via unvalidated project name (S-1) |
| Security | Medium | `issues.py` silent data loss — issues that miss GitHub are invisible (S-4) |
| Maintainability | Medium | `GEMINI.md` missing `[ROLE: ...]` override notice → `takkub done` refusal risk (M-2) |
| Blind spot | Medium | Pending CC files accumulate but are never deleted (M-1) |

**Critical (blocks merge):** None of these are release-blockers for a local dev tool, but **S-1** (vault path traversal) and **S-4** (silent issue data loss) should be addressed before giving external users access to `projects.json` editing.

**Recommended priority order:**
1. S-1 — add `validate_name` / path-component check in `_save_decision_note`
2. M-2 — add override-rule section to `GEMINI_MD`
3. C-3 / B-1 — fix `scan_artifacts` symlink traversal
4. S-4 — warn visibly when `gh` fallback is used
5. M-1 — delete empty `pending-lead-cc-*.json` files on flush
