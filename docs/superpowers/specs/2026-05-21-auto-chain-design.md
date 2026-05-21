# Auto-chain (one-hop impl → verify) — design

**Date:** 2026-05-21
**Owner:** Lead
**Status:** spec, ready for implementation plan
**Estimated effort:** ~2–3 ชม. (single PR, mirror of `requires_commit` pattern)

---

## 1. Goal

ให้ Lead สามารถ tag impl-phase task ตอน `takkub assign` ว่า "เสร็จแล้วจุดวาบ verify ต่อเลย" โดยไม่ต้อง propose-then-confirm รอบใหม่ ระบบ chain แค่ **one hop**: impl (frontend/backend/...) → verify (qa+reviewer) จากนั้นกลับเข้าโหมด propose-then-confirm ปกติ

**Non-goals (V1 จงใจ scope แคบ):**
- ❌ ไม่ใช่ full loop (verify→fix→verify จน pass) — verify hop คือ terminal hop
- ❌ ไม่ใช่ session-wide toggle — opt-in per assign call เท่านั้น
- ❌ ไม่ generate verify spec แบบ orchestrator-template — Lead เป็นคนเขียน spec (รักษาคุณภาพ)
- ❌ ไม่แตะ verify→ship hop หรือ fix-loop
- ❌ ไม่มี max-iteration counter / state machine ของ phase
- ❌ ไม่มี per-project default (เช่น projects.json ตั้ง "always auto-chain")

---

## 2. Design decisions (จาก brainstorm 2026-05-21)

| Dimension | Decision | Rationale |
|---|---|---|
| Loop scope | One-hop: impl→verify เท่านั้น | กัน fix-loop runaway, อิง user choice "one-hop" |
| Trigger gating | รอจน **ทุก auto-chain pane ใน project** done | feature ใหญ่ที่ run parallel (FE+BE) ต้องเสร็จทั้งคู่ก่อน verify |
| Default mode | **Opt-in** ผ่าน `--auto-chain` flag | Default behavior คงเดิม, ไม่มี surprise สำหรับ task ที่ไม่ใช่ impl |
| Verify spec | Lead เขียนเอง (orchestrator inject prompt) | คุณภาพ verify task ดีกว่า template, Lead เห็น diff/note ครบ |
| Multi-project | Per-project group | tab A's auto-chain ไม่ trigger tab B's verify |
| Verify hop chain | **ไม่ propagate** — qa/reviewer assign **ไม่ได้** มี `--auto-chain` | One-hop hard guarantee — verify ไม่ chain ต่อ |
| State storage | `dict[str, bool]` keyed `<project>::<role>` | Mirror exact pattern ของ `_requires_commit_on_done` |
| Persistence | In-memory เฉพาะ run | Cockpit restart = state ล้าง (ตรง intent: เริ่มงานใหม่) |

---

## 3. Architecture

```
┌──────────────────────────────────────────────────────────────┐
│ Lead pane (propose plan → user confirms with --auto-chain)   │
│   $ takkub assign --role frontend --auto-chain "UI login" &  │
│   $ takkub assign --role backend  --auto-chain "API login" & │
│   $ wait                                                     │
└──────────────────┬───────────────────────────────────────────┘
                   ▼ CLI request: assign + auto_chain=True
┌──────────────────────────────────────────────────────────────┐
│ orchestrator.assign(..., auto_chain=True)                    │
│   self._auto_chain_panes["proj_a::frontend"] = True          │
│   self._auto_chain_panes["proj_a::backend"]  = True          │
└──────────────────┬───────────────────────────────────────────┘
                   │
       ┌───────────┴───────────┐ frontend + backend impl ทำงาน
       ▼                       ▼
┌─────────────┐         ┌─────────────┐
│ frontend    │         │ backend     │
│ takkub done │         │ takkub done │
└──────┬──────┘         └──────┬──────┘
       └────────┬──────────────┘
                ▼ orchestrator.done() for each
┌──────────────────────────────────────────────────────────────┐
│ done() flow:                                                 │
│   1. inject "[X done] <note>" → Lead (เดิม)                 │
│   2. pop self._auto_chain_panes[key] — was this auto-chain? │
│   3. ถ้า pop ได้ True → ตรวจ pending ใน project เดียวกัน    │
│   4. pending == 0 → inject auto-chain handoff prompt → Lead │
└──────────────────┬───────────────────────────────────────────┘
                   ▼
┌──────────────────────────────────────────────────────────────┐
│ Lead pane (receives 2 messages: done note + handoff prompt)  │
│   reads notes/diff → writes verify spec → fires:             │
│   $ takkub assign --role qa       --cwd <path> "<spec>" &    │
│   $ takkub assign --role reviewer --cwd <path> "<spec>" &    │
│   $ wait                                                     │
│   (NOTE: no --auto-chain — verify is terminal hop)           │
└──────────────────────────────────────────────────────────────┘
```

---

## 4. Components

### 4.1 Orchestrator state (`orchestrator.py`)

**New attribute** in `__init__`:
```python
# Opt-in auto-chain: when assign() was called with auto_chain=True,
# done() injects a pre-authorisation handoff prompt to Lead AFTER all
# auto-chain panes in the same project have reported done. The flag
# tracks "is this pane part of an active auto-chain group?". Keyed
# `<project>::<role>`. Cleared by close() and on successful done().
self._auto_chain_panes: dict[str, bool] = {}
```

**Mirror exactly** the existing pattern of `self._requires_commit_on_done`.

### 4.2 `assign()` signature change

```python
def assign(
    self,
    role_name: str,
    cwd: str | None,
    task: str,
    requires_commit: bool = False,
    auto_chain: bool = False,           # NEW
    project: str | None = None,
) -> tuple[bool, str]:
    ...
    if auto_chain:
        self._auto_chain_panes[key] = True
    ...
```

`_log_event("assign", ..., auto_chain=auto_chain)` — log สำหรับ audit.

### 4.3 `done()` flow extension

After existing "notify Lead" block (line ~2013 in current code), add:

```python
# Auto-chain handoff: if this pane was tagged --auto-chain at assign
# time, and it was the LAST pending auto-chain pane in the project,
# inject a pre-authorisation prompt so Lead fires verify (qa+reviewer)
# without proposing/confirming.
if self._auto_chain_panes.pop(key, False):
    pending = [
        k for k in self._auto_chain_panes
        if k.startswith(f"{project_ns}::")
    ]
    if not pending:
        self._inject_auto_chain_handoff(project_ns)
```

### 4.4 New method `_inject_auto_chain_handoff()`

```python
def _inject_auto_chain_handoff(self, project_ns: str) -> None:
    """Send a pre-authorisation prompt to Lead telling it to fire
    verify (qa + reviewer) in parallel WITHOUT proposing or waiting
    for user confirmation. Fires after all panes assigned with
    --auto-chain in the project have reported done."""
    lead = self._project_panes(project_ns).get(LEAD.name)
    prompt = (
        "[auto-chain handoff] impl panes spawned with --auto-chain "
        "in this project have all reported done.\n"
        "You are pre-authorized to fire verify (qa + reviewer) "
        "in parallel WITHOUT proposing or waiting for user confirmation.\n"
        "\n"
        "Steps:\n"
        "1. Re-read the recent [<role> done] notes above\n"
        "2. (Optional) `git -C <project_path> diff --stat` for changed files\n"
        "3. Write a verify spec covering the implemented changes\n"
        "4. Fire in parallel: takkub assign --role qa ... & "
        "takkub assign --role reviewer ... & wait\n"
        "\n"
        "Do NOT add --auto-chain on the verify fire (verify is the "
        "terminal hop). After qa+reviewer done events arrive, resume "
        "normal propose-then-confirm flow."
    )
    if lead and lead.session and lead.session.is_alive:
        lead.session.write(prompt)
        QTimer.singleShot(150, lambda: lead.session and lead.session.write(b"\r"))
        self.leadInjected.emit(prompt)
        _log_event("auto_chain_handoff", project=project_ns)
    else:
        self._pending_done_notices.setdefault(project_ns, []).append(
            {"role": "system", "note": "auto-chain handoff", "body": prompt}
        )
        _log_event("auto_chain_handoff_queued", project=project_ns)
```

### 4.5 `close()` cleanup

Add 1 line at line ~1884 alongside other dict pops:
```python
self._auto_chain_panes.pop(key, None)
```

### 4.6 CLI (`cli.py`)

```python
sa.add_argument(
    "--auto-chain",
    action="store_true",
    dest="auto_chain",
    default=False,
    help="after impl done, auto-trigger Lead to fire qa+reviewer "
         "without proposing (one-hop only — verify is terminal)",
)
```

In `cmd_assign()`:
```python
"auto_chain": bool(getattr(args, "auto_chain", False)),
```

### 4.7 `cli_server.py`

```python
elif cmd == "assign":
    ok, msg = self._orch.assign(
        req["role"],
        cwd=req.get("cwd"),
        task=req.get("task", ""),
        requires_commit=bool(req.get("requires_commit", False)),
        auto_chain=bool(req.get("auto_chain", False)),   # NEW
        project=from_project,
    )
```

---

## 5. Data flow (sequence diagram)

```
User → Lead pane:  "ทำหน้า login + API ขนาน auto-verify"
Lead → CLI:        takkub assign --role frontend --auto-chain "..." &
                   takkub assign --role backend  --auto-chain "..." &
                   wait
CLI → cli_server → orchestrator.assign():
                   _auto_chain_panes["proj::frontend"] = True
                   _auto_chain_panes["proj::backend"]  = True
                   spawn + send task

(frontend impls + tests)
frontend pane → orchestrator.done(from_role="frontend", note="login UI shipped"):
   1. write "[frontend done] login UI shipped\n" to Lead session
   2. pop _auto_chain_panes["proj::frontend"] = True
   3. check pending in proj: ["proj::backend"] → still 1, skip handoff

(backend impls + tests)
backend pane → orchestrator.done(from_role="backend", note="POST /auth/login ready"):
   1. write "[backend done] POST /auth/login ready\n" to Lead session
   2. pop _auto_chain_panes["proj::backend"] = True
   3. check pending in proj: [] → ALL DONE → call _inject_auto_chain_handoff(proj)
   4. handoff prompt appears in Lead pane

Lead (reads handoff):
   writes verify spec from done notes + git diff
   fires: takkub assign --role qa       "<spec>" &
          takkub assign --role reviewer "<spec>" &
          wait
   (no --auto-chain — chain stops here)

qa done → "[qa done] ...":
   _auto_chain_panes empty for qa key → no handoff (just regular flow)
   Lead resumes propose-then-confirm: "verify pass → propose ship?"
```

---

## 6. Edge cases

| Case | Handling |
|---|---|
| Lead pane is absent/dead when handoff fires | Queue via existing `_pending_done_notices` mechanism (mirrors current done-notice queueing). Flushed when Lead next spawns. |
| Only 1 of N auto-chain panes done, others still working | `pending != 0` check prevents handoff. Waits for last done. |
| Mixed group (some `--auto-chain`, some not) | Only auto-chain panes count for the group. Non-auto-chain panes' done events behave as today (no influence on handoff timing). |
| Lead anchored at cockpit (no Lead session) | `lead.session` check guards write. Queued for later. |
| `close --role frontend` mid-flight | `close()` pops `_auto_chain_panes[key]` → group shrinks → if last auto-chain pane was the closed one, no handoff (intentional — user explicitly cancelled). If remaining auto-chain panes still pending, they continue normally; handoff fires when THEY done. |
| User Ctrl+C / Esc on Lead mid-handoff | The handoff is just a regular text injection. Lead can be interrupted. User stays in control. |
| Multi-project (tab A and tab B both have auto-chain) | `pending` filter uses `<project_ns>::` prefix → isolated per project. tab A's done events never trigger tab B's handoff. |
| qa/reviewer ALSO assigned with `--auto-chain` (misuse) | Tracked the same way, would inject handoff again when both done. **Documented as user error** — design says "verify is terminal". Hard guard not implemented (YAGNI: Lead's CLAUDE.md instructs not to). |
| Same role reassigned mid-task (replacing auto-chain task) | New assign overwrites pane state. `_auto_chain_panes[key]` already True; remains True. Behavior: pane's CURRENT task completes → done() fires handoff check as expected. Acceptable. |
| Auto-respawn case (`_auto_respawn_done`) | Auto-respawn re-runs assign with the same args including the auto_chain flag from `_last_assigned_task` metadata. **V1 simplification:** auto-respawn does NOT preserve auto_chain flag (would require extending `_last_assigned_task` schema). Result: respawned pane behaves as non-auto-chain. Documented limitation — acceptable since respawn is rare. |

---

## 7. CLAUDE.md updates

Add note in "Done-handoff rule" section (#4):

```markdown
**Auto-chain exception:** ถ้า assign() ใช้ flag `--auto-chain` orchestrator
จะ inject handoff prompt อัตโนมัติเมื่อทุก auto-chain pane ใน project done →
Lead **pre-authorized** ให้ fire qa+reviewer โดยไม่ต้อง propose-confirm
จำกัด one hop: verify ไม่ chain ต่อ
```

Add example to parallel dispatch section:

```bash
# Auto-chain: impl ขนาน → orchestrator chain verify ให้อัตโนมัติ
takkub assign --role frontend --auto-chain --cwd <web> "..." &
takkub assign --role backend  --auto-chain --cwd <api> "..." &
wait
# พอทั้งคู่ done → handoff prompt เด้งเข้า Lead → Lead fire qa+reviewer ทันที
```

---

## 8. Testing strategy

New test file: `tests/test_auto_chain.py`. Tests use `qapp` fixture pattern like `test_lead_write_guard.py`.

| Test | Asserts |
|---|---|
| `test_assign_auto_chain_stores_flag` | `assign(auto_chain=True)` populates `_auto_chain_panes[key] = True` |
| `test_assign_default_no_auto_chain` | `assign()` without flag → key NOT in `_auto_chain_panes` |
| `test_done_clears_auto_chain_flag` | After `done()`, key removed from `_auto_chain_panes` |
| `test_done_injects_handoff_when_last_auto_chain_done` | Spawn 2 auto-chain panes (frontend + backend). First `done()` → Lead session.write called once (normal notice only). Second `done()` → Lead session.write called TWICE (notice + handoff prompt with "[auto-chain handoff]" string). |
| `test_done_no_handoff_for_non_auto_chain` | Regular `assign()` + `done()` → only 1 write to Lead (no handoff) |
| `test_done_no_handoff_when_other_pending` | 2 auto-chain panes, first `done()` → no handoff (other still pending) |
| `test_close_clears_auto_chain_flag` | After `close()`, key removed from `_auto_chain_panes` |
| `test_multi_project_isolation` | proj_a auto-chain done does NOT trigger proj_b handoff |
| `test_cli_auto_chain_flag` | `takkub assign --auto-chain` → request body has `"auto_chain": true` |
| `test_handoff_queued_when_lead_absent` | If Lead pane is dead, handoff queued in `_pending_done_notices` |

**Smoke (manual, after merge):** Spawn cockpit, open a project, in Lead pane run:
```bash
takkub assign --role backend --auto-chain "echo hello && takkub done"
```
Wait for done. Lead should receive `[backend done] ...` + auto-chain handoff prompt.

---

## 9. Out-of-scope (future work)

- **V2 full-loop:** verify→fix→verify until pass with max-N iterations
- **V2 ship hop:** verify pass → auto-propose commit/PR (with explicit user confirm — ship is irreversible)
- **V2 state machine:** explicit phase tracking per pane (impl|verifying|done)
- **V2 session toggle:** status bar chip for "auto-mode" mode

These are NOT designed here. If V1 lands well, brainstorm V2 separately.

---

## 10. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Lead misinterprets handoff prompt and fires extra `--auto-chain` on verify | Prompt explicitly says "Do NOT add --auto-chain on the verify fire". CLAUDE.md doc reinforces. No hard guard (YAGNI). |
| User regret: handoff fires while user busy reading impl reports | One-hop scope limits damage. User can `takkub close --role qa` to abort verify if not wanted. |
| `_pending_done_notices` queue grows if Lead never spawns | Pre-existing behavior; not new to auto-chain. Same retention as today (cleared on next Lead spawn). |
| Tests rely on mocking `lead.session.write` — fragile to refactors | Mirror existing `test_lead_write_guard.py` style; refactors will break those tests in lockstep. Acceptable. |

---

## 11. Implementation order (for the plan phase)

1. Add `_auto_chain_panes` dict + extend `assign()` signature (smallest unit, test in isolation)
2. Add `close()` cleanup line + unit test
3. Add `_inject_auto_chain_handoff()` method + unit test (with mocked Lead session)
4. Wire `done()` to call handoff trigger + unit tests (single-pane, multi-pane, mixed)
5. CLI flag in `cli.py` + `cli_server.py` + unit test for request body
6. Multi-project isolation test
7. Lead-absent queue test
8. Update CLAUDE.md documentation
9. Manual smoke test (cockpit run)
10. Commit + push

Each step compiles independently; tests pass at each step before next change. Mirror pattern from `requires_commit` PR history.
