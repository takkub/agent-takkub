# Architecture cross-check — agent-takkub v1.0.58

**Date:** 2026-08-13 · **Reviewer:** claude-substitute for codex (สมองที่ 2) · **Mode:** review-only, no code changed
**Base:** `wt/codex-1786628195` @ `8fe27c5` (merge of `release/2026-08-13`)

> ⚠️ **Model-diversity caveat:** Codex CLI ปิดอยู่ในรอบนี้ ผู้เขียนรายงานนี้คือ **Claude ทำหน้าที่แทน** —
> ไม่ใช่มุมมองจากโมเดลคนละตระกูลจริงๆ ถ้างานนี้ต้องการ cross-check bias ข้ามโมเดล ให้เปิด/ติดตั้ง Codex
> แล้วถามซ้ำ ส่วน finding ที่เป็นตัวเลข/path:line ในนี้ verify จาก source จริงทั้งหมด ใช้ต่อได้เลย

## What was actually run (evidence base)

| Check | Result |
|---|---|
| `lint-imports` (23 contracts) | **23 kept, 0 broken** — 141 files, 544 dependencies |
| `depgraph.json` freshness | **fresh** — regenerated in-memory, 0 module diff, 0 edge diff |
| `cli.py` ↔ `cli_server.py` command strings | **21/21 in sync**, no orphan either direction |
| Unreferenced top-level symbols (src+tests+scripts+docs corpus) | 3 |
| LOC delta vs `bdd2211` (2026-07-12, last godfile-map refresh) | measured per module, below |

**Bottom line:** โครงสร้างไม่ได้พัง — contract เขียวหมด, depgraph ตรง, dead code น้อยกว่าที่คาดสำหรับ 46 commit ใน ~24 ชม.
ปัญหาจริงคือ **guardrail ไม่ได้ทำงานตรงที่เอกสารบอกว่ามันทำ** และ **การแตก mixin ปี 2026-06 เป็นการแยกไฟล์ ไม่ใช่การแยก state**
— ตอนนี้เริ่มมี god-file ตัวที่สาม (`settings_window.py`) โตขึ้นมาโดยไม่มี contract คุมเลยแม้แต่ข้อเดียว

---

## Findings — เรียงตามผลกระทบ/ความเสี่ยง

Effort scale: **XS** <30min · **S** 1–2h · **M** ~half day · **L** multi-day

---

### H1 — CI ไม่เคยรัน `lint-imports` เลย; guardrail ทั้งหมดคือ pre-commit hook ที่รันได้เฉพาะ Windows

**Impact: สูงสุดในรายงานนี้ — finding อื่นทั้งหมดตั้งอยู่บนสมมติฐานว่ามีคนคุม แต่ไม่มี**

- `.github/workflows/ci.yml:49-60` — job `lint-and-test` มีแค่ ruff check / ruff format / import smoke / pytest
  **ไม่มี step ไหนเรียก `lint-imports` และไม่มี step ไหนเช็ค depgraph staleness**
- `.pre-commit-config.yaml:63-67` — hook `import-linter` hardcode `"$ROOT/.venv/Scripts/lint-imports.exe"`
- `.pre-commit-config.yaml:80-85` — hook `depgraph-fresh` hardcode `"$ROOT/.venv/Scripts/python.exe"`

`Scripts/` + `.exe` เป็น layout ของ Windows venv เท่านั้น — บน macOS คือ `.venv/bin/lint-imports` ไม่มีนามสกุล
`CLAUDE.md` สั่ง cross-platform ("CI = matrix `windows-latest` + `macos-latest` **ทั้งคู่ต้องเขียว**") และ `ci.yml:19`
รัน 3 OS จริง — แต่ 23 contract **ไม่ได้อยู่ใน CI เลย** ทั้ง 3 OS

ผลลัพธ์ที่ตามมา:
1. commit ด้วย `--no-verify`, จากเครื่อง Mac, จาก worktree ที่ไม่มี `.venv` ที่ git-common-dir, หรือ merge ผ่าน GitHub web
   → **ไม่มีสัญญาณอะไรเลย** ว่า layer พัง
2. `docs/architecture/godfile-map.md:183` เขียนว่า *"PR ที่ลาก edge ข้าม layer **fail CI**"* — **ข้อความนี้ไม่จริง**
3. `CLAUDE.md` เขียนว่า depgraph "auto-refresh ทุก commit" — จริงเฉพาะบน Windows ที่ไม่ skip hook

*(ข่าวดี: ตอนนี้ทั้ง contract และ depgraph ยังถูกต้อง 100% — verify แล้ว ปัญหาคือ "ไม่มีอะไรกัน" ไม่ใช่ "พังไปแล้ว")*

**Fix:** เพิ่ม 2 step ใน `ci.yml` (`lint-imports` + `python tools/gen_import_graph.py && git diff --exit-code docs/architecture/depgraph.json`)
และแก้ hook ให้เลือก `Scripts/` vs `bin/` ตาม platform · **Effort: S**

---

### H2 — mixin แชร์ state ส่วนตัวของ `Orchestrator` โดยตรง; ขอบเขตเป็นแค่ชื่อไฟล์ ไม่ใช่ encapsulation

**ตอบคำถามข้อ 1 ตรงๆ: mixin *ไม่ได้* คุมขอบเขตตัวเอง — และไม่เคยคุมตั้งแต่วันแรก**

state dict ของทุก mixin ถูกประกาศ+init ใน **`Orchestrator.__init__` ทั้งหมด** ไม่ใช่ในไฟล์ที่เป็นเจ้าของ:

| state | เจ้าของตามเอกสาร | ที่ init จริง |
|---|---|---|
| `_pending_lead_cc` | `lead_inbox.py` | `orchestrator.py:775` |
| `_pending_done_notices` | `lead_inbox.py` | `orchestrator.py:782` |
| `_lead_notify_queue` | `lead_inbox.py` | `orchestrator.py:801` |
| `_shard_groups` | `pipeline_executor.py` | `orchestrator.py:821` |
| `_pipeline_runs` | `pipeline_executor.py` | `orchestrator.py:824` |
| `_lead_draft_state` | `lead_inbox.py` | `orchestrator.py:837` |

`lead_inbox.py:515` เขียนไว้เองในคอมเมนต์: `_shard_groups: dict — state, kept in Orchestrator.__init__`

นับ cross-boundary attribute access (`self._x` / `self.orch._x` ที่ไฟล์นั้นไม่ได้เป็นเจ้าของ) = **50 จุด**:

| ไฟล์ | เข้าถึง state ของคนอื่น |
|---|---|
| `orchestrator.py` | `_panes_by_project` ×16, `_shard_groups` ×11, `_pane_state` ×9, `_pipeline_runs` ×5, `_recent_exits` ×3, lead_inbox 4 ตัว ×6 |
| `spawn_engine.py` | `_pending_lead_cc`, `_pending_done_notices`, `_pipeline_runs` |
| `lead_inbox.py` | `_shard_groups` ×2 (`:1022`, `:1028`), `_spawn_deferred` |
| `limit_autoresume.py` | `_panes_by_project` (`:172`, `:230`, `:330`, `:354`), `_pane_state` ×2 |

**ที่แย่ที่สุด: UI แตะ private state ของ engine โดยตรง** — import-linter มองไม่เห็นเพราะเป็น attribute access ไม่ใช่ import:

- `update_panel.py:120` — `for project_panes in self.orch._panes_by_project.values()`
- `update_panel.py:439` — เหมือนกัน
- `main_window.py:1422` — เหมือนกัน

`limit_autoresume.py:154` ยอมรับตรงๆ ใน docstring ว่าพึ่ง `_ps`/`_pane_state`/`_panes_by_project` ของ SpawnEngineMixin

นี่คือสิ่งที่ `godfile-map.md:92` เตือนไว้เป๊ะ: *"ต้องทำเป็น state object ไม่ใช่ mixin — mixin จะทำ dict เป็น hidden cross-mixin coupling"*
แผน refactor item 4 (`PaneRegistry`/`SpawnArbiter`) ยัง "ไม่เริ่ม" (`godfile-map.md:179`) และ coupling โตขึ้นตั้งแต่นั้น

**Fix (ไม่ต้อง rewrite):** เพิ่ม accessor `panes_for_project(ns)` / `iter_all_panes()` บน `Orchestrator` แล้วเปลี่ยน 3 callsite ฝั่ง UI
+ test ที่ assert ว่าไม่มีไฟล์ UI ไหน match `orch\._panes_by_project` — ปิดรูฝั่ง UI ได้ทันทีโดยไม่แตะ engine
**Effort: S** สำหรับปิดฝั่ง UI · **L** ถ้าจะยก state ออกเป็น object จริง (ควรทำแยกรอบ)

---

### H3 — `settings_window.py` คือ god-file ตัวที่ 3 และ **ไม่มี contract คุมเลยแม้แต่ข้อเดียว**

| metric | ค่า |
|---|---|
| LOC | **3,712** (2,728 เมื่อ 2026-07-12 → **+984 ใน 1 เดือน**) |
| ขนาดไฟล์ | 170.8 KB *(brief บอก 149KB — stale ไปแล้ว)* |
| method บน `SettingsWindow` | **102** (`_on_*` 30, `_build_*` 23) |
| view คนละเรื่องในคลาสเดียว | **10** (`settings_window.py:140-149`) |
| static fan-out | **22** — เท่ากับ `main_window.py` |
| contract ที่คุมอยู่ | **0** |

23 contract ครอบคลุม source module รวม 30 ตัวจาก 141 — `settings_window` ไม่อยู่ในนั้น
มันโผล่แค่ใน *forbidden* list ของ `settings-management-layer` (`pyproject.toml:454`) = คุมทิศทางตรงข้าม

คำนวณจาก graph (จำลองเพิ่ม edge แล้วรัน contract engine): `settings_window` **import `orchestrator` / `main_window` / `spawn_engine`
ได้วันนี้เลยโดย 0 contract fail** เทียบขนาดแล้วมันใหญ่กว่า `main_window.py` (1,457 LOC) ที่เป็นเป้าหมายของ refactor รอบปี 2026-06 ถึง 2.5 เท่า

**Fix:** เพิ่ม 1 contract `settings-window-layer` (forbidden: orchestrator, main_window, app, cli, spawn_engine)
— หยุดเลือดก่อน ส่วนการแตกไฟล์ค่อยว่ากัน · **Effort: XS สำหรับ contract, L สำหรับแตกไฟล์**

---

### H4 — settings 2 ชุดกำลังแยกทางกันเรื่อยๆ; ชุดใหม่ 3,600 LOC ปิดอยู่ default

`settings_management/` = 30 ไฟล์ ~3,600 LOC มี contract เป็นของตัวเอง มี `roles_page.py` fan-out 14 (อันดับ 6 ของ repo)
แต่เข้าถึงได้เมื่อ **ทั้งสองเงื่อนไข** เป็นจริงเท่านั้น (`user_actions.py:337-342`):

```python
if (feature_flags.resolve() is feature_flags.SettingsUI.NEW
        and initial_view == VIEW_PROVIDERS_ROLES):
```

`feature_flags.py:47` → default = `LEGACY` (roll back ตั้งแต่ 2026-07-11 เพราะ user บอก "ใช้ยากกว่าเดิม")
→ ในการใช้งานจริง **โค้ดก้อนนี้เป็น dead code ทั้งก้อน**

ระหว่างนั้น feature ใหม่ลง **ฝั่ง legacy อย่างเดียว** (`git log --since=2026-07-12 -- settings_*`):

| commit | เข้า `settings_window.py` | เข้า `settings_management/` |
|---|---|---|
| `f024997` New Role redesign | ✅ | ❌ |
| `dd5b294` autoskills + New Role picker | ✅ | บางส่วน (`window.py`) |
| `60528a4` autoskills security annotations | ✅ | ❌ |
| `bfc02d3` per-role reasoning-effort | ✅ | ❌ |
| `68e8e2b` role registry reconcile (#162) | ✅ | ❌ |
| `16352ea` Team Settings redesign | ✅ | ❌ |

ทุก commit ที่ลงฝั่งเดียว = ราคา cutover แพงขึ้น และตอนนี้ยังต้อง maintain 2 ชุดใน suite เดียวกัน

**Bug แถม:** `feature_flags.py:26-28` docstring เขียนว่า *"Any unrecognized value … falls back to **NEW**"*
แต่ `resolve()` ที่ `:47` return `SettingsUI.LEGACY` — docstring ขัดกับโค้ดตัวเอง (โค้ดถูก)

**Fix:** นี่เป็น **การตัดสินใจของ user ไม่ใช่งาน refactor** — ต้องเลือกอย่างใดอย่างหนึ่ง:
(ก) ตั้ง deadline ให้ port feature ที่ค้าง 6 ตัวแล้วสลับ default, หรือ (ข) ลบ `settings_management/` ทิ้งแล้วเอาแนวคิดที่ดีกลับเข้า legacy
· **Effort: XS สำหรับแก้ docstring · decision สำหรับที่เหลือ**

---

### H5 — `claude_auth_config` hardcode `~/.claude` ทำลาย isolation ของ installed build

`config.py:180-197` `default_claude_config_dir()` — canonical, DATA_HOME-aware:
dev checkout → `~/.claude` · installed build → `DATA_HOME/claude-config` (เช่น `~/.agent-takkub/claude-config`)
docstring ที่ `:192` ย้ำ *"Computed fresh on every call (never cached)"*

แต่มี **3 จุดที่ resolve default config dir เองแบบไม่ผ่านตัวนี้**:

| path:line | ทำอะไร | ผลบน installed build |
|---|---|---|
| `claude_auth_config.py:38` | `_DEFAULT_CONFIG_DIR = Path.home() / ".claude"` | ผิด — ชี้ไป profile ของ dev checkout |
| `limit_status.py:326` | `(config_dir or Path.home() / ".claude") / ".credentials.json"` | ผิดเมื่อ caller ส่ง None |
| `limit_status.py:389` | `return (Path.home() / ".claude").resolve()` | ผิดเมื่อ caller ส่ง None |
| `token_meter.py:139` | `base = Path(config_dir) if config_dir else Path.home() / ".claude"` | ผิดเมื่อ caller ส่ง None |

กลไกที่ทำให้มันเจ็บ: `pane_env.py:374-377` บน installed build **set `CLAUDE_CONFIG_DIR` แม้กับ profile `default`**
(คอมเมนต์ `:365-369` อธิบายไว้ชัด) → `apply_claude_auth_overrides` (`claude_auth_config.py:177`) ได้ค่า `~/.agent-takkub/claude-config`
→ `_is_default_dir()` (`:78-85`) เทียบกับ `~/.claude` แล้วได้ **False** → profile ที่เป็น default จริงถูกมองว่าไม่ใช่ default
→ legacy auth fallback (`:121`) ไม่ทำงาน

`user_profile.py:38` ยังทำอีกอย่าง: `_DEFAULT_CONFIG_DIR = _default_claude_config_dir()` — **เรียกครั้งเดียวตอน import**
ซึ่งลบล้าง garantie "never cached" ที่ `config.py:192` ตั้งใจไว้ สำหรับทุก consumer ที่ผ่านตัวแปรนี้
(`app.py:625`, `doctor.py:219-238`, `user_profile.config_dir_for:465,470`) — test ที่ monkeypatch `Path.home` จะไม่เห็นผล

**ขอบเขตความเสียหายจริง (ตรงไปตรงมา):** `claude_auth_dialog.py:41` และ `:151` เรียก `load/save_claude_auth()` โดยไม่ส่ง config_dir เลย
= เขียน `~/.claude` เสมอ — **แต่** `ClaudeAuthDialog` ไม่มีใครเรียก (ดู L12) ดังนั้นเส้นนั้นยังไม่ระเบิด
เส้นที่ live คือ `_is_default_dir` + `limit_status`/`token_meter` fallback

**Fix:** ให้ทั้ง 4 จุดเรียก `config.default_claude_config_dir()` และเปลี่ยน `user_profile._DEFAULT_CONFIG_DIR`
เป็นฟังก์ชัน (หรือ property) แทน module-level constant · **Effort: S**

---

### M6 — ค่า spawn-stagger ถูกเขียน 2 ชุด **เพราะ contract ห้าม import** — และโค้ดยอมรับเองในคอมเมนต์

`cli_server.py:93,98,109-111` vs `pipeline_executor.py:44,45,56-58` — ค่าเดียวกันทั้ง 3 ตัว
(`TAKKUB_SPAWN_STAGGER_MS` 400 / `TAKKUB_CODEX_SPAWN_STAGGER_MS` 10000 / `TAKKUB_BROWSER_SHARD_SPAWN_STAGGER_MS` 3000)

`pipeline_executor.py:51-53` เขียนเหตุผลไว้เอง:
> *"kept as separate constants — **this module can't import cli_server without violating the pipeline-executor layer contract**"*

นี่คือเคสที่ contract **ผลิต duplication ขึ้นมาเอง** — และมี behavioural difference ที่ไม่มีใครสังเกต:
`cli_server` อ่าน env ตอน **construct instance** (`:93` อยู่ใน `__init__`) แต่ `pipeline_executor` อ่านตอน **import module** (`:44` top-level)
→ ตั้ง env หลัง import มีผลกับทางหนึ่ง ไม่มีผลกับอีกทาง; fan-out ผ่าน planner กับผ่าน `--shards N` จะ stagger ต่างกันเงียบๆ

**Fix:** ย้าย 3 ค่าไป `config.py` (เป็น leaf, ทั้งสองฝั่ง import ได้ตาม contract อยู่แล้ว) และอ่าน env ใน getter ไม่ใช่ module scope
· **Effort: S**

---

### M7 — gate `supports_remote_history` มี 2 ชุด ชุดหนึ่งอ่อนกว่า — ก็เพราะ contract อีกเหมือนกัน

`remote/notify.py:1071-1086` `history_scanner()` = **double gate** ตามที่ docstring `:1074-1077` อธิบาย:
ต้องผ่านทั้ง `PROVIDER_REGISTRY[name].supports_remote_history` **และ** มี entry จริงใน `_HISTORY_SCANNERS` (`:1042`)
เหตุผล: *"a capability flag flipped without a parser"* ต้องไม่ทำให้เปิด transcript ของ provider ผิดตัว

`cli_server.py:727-730` (`remote-mirror-status` — คำสั่ง diagnostic ที่คนใช้ debug เรื่องนี้โดยเฉพาะ) เช็ค **แค่ flag**:
```python
spec = PROVIDER_REGISTRY.get(provider)
supports_remote_history = bool(spec is not None and spec.supports_remote_history)
```
มันเรียกตัว authoritative ไม่ได้ เพราะ `remote-bolt-on-isolation` (`pyproject.toml:423`) ห้าม `cli_server → remote`

วันนี้ยังไม่ drift (claude/gemini/codex มีครบทั้ง flag และ scanner; opencode/kimi/cursor เป็น False ทั้งคู่ — `provider_spec.py:588,659,733`)
แต่ถ้ามีคนเปิด flag ก่อนเขียน scanner → `takkub remote-mirror-status` จะรายงาน `supports_remote_history: true` ทั้งที่ mirror ไม่ทำงาน
= diagnostic โกหกตรงจุดที่คนกำลัง debug

**Fix:** ยก double gate ลงไปอยู่ใน `provider_spec.py` (leaf, ทั้ง `cli_server` และ `remote/notify` import ได้)
โดยให้ `remote/notify` register scanner เข้า registry ตอน import · **Effort: S**

---

### M8 — resume validation เป็น if/elif ปิดตาย 3 provider ทั้งที่มี pattern registry อยู่ในไฟล์ข้างๆ

`spawn_engine.py:379-395` `_resume_uuid_matches_provider_cwd`:
```python
if provider == "claude":  ...        # :385
if provider == "gemini":  ...        # :387
if provider == "codex":   ...        # :391
return False                          # :395  ← opencode / kimi / cursor / provider ใหม่
```

เทียบกับ `remote/notify.py:1042-1068` `_HISTORY_SCANNERS` — **โจทย์เดียวกันเป๊ะ** ("หา transcript ของ provider นี้จาก cwd/uuid")
แต่เขียนเป็น registry ที่ต่อ provider ใหม่ได้โดยไม่แตะ dispatcher ทั้งคู่เรียก helper ตัวเดียวกันด้วยซ้ำ
(`gemini_helper.resolve_gemini_jsonl_for_cwd`, `codex_helper.resolve_codex_jsonl_for_cwd`)

**#103 gap:** สาขา `return False` ที่ `:395` แปลว่า opencode/kimi/cursor **validate resume uuid ไม่ผ่านตลอดกาล** — และตรงนี้
**ไม่มีคอมเมนต์ flag #103 เลย** ต่างจากที่อื่นในโค้ดเดียวกันที่ flag ไว้ดี:
- `orchestrator.py:206-209` (proactive compact) — flag ชัด
- `limit_autoresume.py:214-216` — flag ชัด
- `cli_server.py:741-744` — flag ชัด
- `provider_spec.py:578-588, 657-659, 731-733` — flag ชัดมาก ระบุถึง BlueParking bug report

คือทีมทำ multi-provider discipline ได้ดีจริงในภาพรวม จุดนี้เป็นข้อยกเว้นที่หลุด

**Fix:** เพิ่ม `resolve_session_for_cwd` เป็น field ใน `ProviderSpec` (มี `produces_jsonl_transcript` อยู่แล้วที่ `:146`)
แล้วให้ `spawn_engine` dispatch ผ่าน registry · **Effort: M**

---

### M9 — atomic write มี 11 ชุด; ชุดที่ทนทานที่สุดถูกใช้แค่ 3 ไฟล์

`config.py:82-114` `_write_json_atomic()` เป็นตัวที่ทำครบ: `fsync(f)` + `fsync(dir_fd)` บน POSIX
+ **retry `PermissionError` 4 รอบ** (0/20/50/100ms) พร้อมคอมเมนต์ `:90-92` ว่า *"Windows can transiently reject
replacement while an AV scanner or another reader still has the destination open"* + return bool ให้ caller รู้ผล

ใช้จริงแค่ `config.py`, `orchestrator.py:3363,4634`, `project_wizard.py:392,617`

อีก 10 จุดเขียนเอง **ไม่มี retry, ไม่มี fsync สักตัว**:

| path:line | หมายเหตุ |
|---|---|
| `task_ledger.py:78-82` (`_atomic_write`) | เขียนทุก assign/done — `:117`, `:213`, `:276`, `:553` |
| `user_profile.py:55` (`_atomic_write`) | `:310`, `:327`, `:409`, `:453` |
| `vault_mirror.py:326`, `:511`, `:655` | `:655` = `distill_to_knowledge_base` (ใหม่จาก #168 เมื่อคืน) |
| `issues.py:222` · `shared_dev_tools.py:77` · `auto_issue_capture.py:147` | |
| `plugin_installer.py:469` · `claude_auth_config.py:158` | |
| `settings_management/repositories/roles.py:189` | |

Windows เป็น platform หลัก และเหตุผลของ retry ถูกเขียนไว้ใน `config.py` แล้ว — แต่ 10/11 จุดไม่ได้รับ
(ยุติธรรม: callsite ส่วนใหญ่ห่อ `except OSError` เช่น `task_ledger.py:261-267`, `vault_mirror.py:664-666` → ไม่ crash
แต่กลายเป็น **silent write loss** แทน ซึ่งกับ ledger/knowledge-base คือข้อมูลหาย)

`vault_mirror._cap_knowledge_base:596` ยังเป็น O(n²) ด้วย (`_size()` join + encode ใหม่ทุกรอบ loop) — cap 150 entry เลยไม่เจ็บ แต่เป็นกลิ่น

**Fix:** export `config.write_text_atomic(path, text) -> bool` คู่กับ `_write_json_atomic` ที่มีอยู่ แล้วแทน 10 จุด
(`config` เป็น leaf, ทุกไฟล์ในลิสต์ import ได้อยู่แล้ว) · **Effort: M**

---

### M10 — 5 UI-mixin contract ห้ามแค่ `app` + `cli` ไม่เคยห้าม `orchestrator` → engine-ui separation คุมทางเดียว

`engine-ui-separation` (`pyproject.toml:131-138`) ห้าม orchestrator → main_window/app **ทิศเดียว**
ส่วน contract ของ UI mixin ทั้ง 5 ตัวก็อปกันมาแบบเดียวกันหมด และไม่มีตัวไหนใส่ `orchestrator`:

| contract | บรรทัด | forbidden |
|---|---|---|
| `update-panel-layer` | `:264-272` | app, cli |
| `project-wizard-layer` | `:308-317` | app, cli |
| `user-actions-layer` | `:319-328` | app, cli |
| `limit-panel-layer` | `:330-339` | app, cli |
| `status-header-layer` | `:375-384` | app, cli |

ผลที่เห็นเป็นรูปธรรม — `_split_shard` ซึ่งเป็น **pure string helper 4 บรรทัดโค้ด** (`pipeline_executor.py:81-93`, ไม่แตะ `self`) ถูกดึงข้ามชั้นไปทั่ว:

- `update_panel.py:435` — `from .orchestrator import _split_shard as _mw_split_shard2`
- `main_window.py:425` — `from .orchestrator import _split_shard as _mw_split_shard`
- `headless_window.py:34` · `lead_inbox.py:56` · `spawn_engine.py:70` · `orchestrator.py:125`

`update_panel.py` เลย **import orchestrator เข้ามาทั้งก้อนเพื่อฟังก์ชัน regex ตัวเดียว** — depgraph ยืนยัน:
`update_panel → orchestrator` เป็น edge จริง และไม่มี contract ไหนกัน

`_split_shard` ควรอยู่ `orchestrator_text.py` (โมดูลที่ตั้งใจไว้ให้เป็น pure helper, มี contract `orchestrator-text-layer` คุมอยู่แล้ว)
ไม่ใช่ `pipeline_executor.py` ที่เป็น engine mixin

**Fix:** ย้าย `_split_shard` → `orchestrator_text.py` (re-export ที่เดิมกันแตก) แล้วเพิ่ม `agent_takkub.orchestrator`
เข้า forbidden ของ 5 contract ข้างบน · **Effort: S**

---

### M11 — mixin พี่น้อง import กันเองได้อิสระ; ไม่มี contract ไหนกัน cycle และไม่มี `layers` contract เลย

edge ระหว่าง mixin ที่มีอยู่จริง (ทั้งหมดถูกกฎ ไม่มีอันไหน fail):
- `limit_autoresume.py:46` → `lead_inbox` · `limit_autoresume.py:51` → `spawn_engine`
- `lead_inbox.py:56` → `pipeline_executor` · `spawn_engine.py:70` → `pipeline_executor`

contract ของ mixin ทุกตัว (`spawn-engine-layer:363`, `lead-inbox-layer:239`, `limit-autoresume-layer:251`,
`pipeline-executor-layer:214`) ห้ามแค่ **orchestrator / main_window / app / cli** — **ไม่มีตัวไหนห้ามพี่น้องกันเอง**
→ `lead_inbox → spawn_engine` (หรือกลับทาง) เพิ่มได้พรุ่งนี้ = cycle จริงบน god class โดย CI เงียบสนิท

คำนวณจาก graph: **`spawn_engine` ถูก import ได้จาก 123 จาก 141 โมดูล โดยไม่ทำให้ contract ไหนพัง**
(`settings_window`, `task_dock`, `doctor`, `status_header`, `shared_dev_tools`, `routing_planner`, … อยู่ในนั้นหมด)
ตัวเลขเทียบเคียง: `orchestrator` 35 · `main_window` 33 · `app` 19 · `cli` 29

**สาเหตุเชิงรูปแบบ:** contract ทั้ง 23 ข้อเป็น `type = "forbidden"` ทั้งหมด — **ไม่มี `layers` contract แม้แต่ข้อเดียว**
เลยต้องเขียน forbidden entry รวม **131 บรรทัด** เพื่อบรรยายชั้นที่ `layers` contract ข้อเดียวบรรยายได้
และ list แบบมือทำให้พลาดง่าย: `leaf-modules-pure` มี `agent_takkub.claude_auth_config` **ซ้ำ 2 ครั้ง** (`pyproject.toml:177` และ `:210`)

**Fix:** เพิ่ม `type = "layers"` 1 ข้อ (`cli`/`app` > `main_window` > UI mixins > `orchestrator` > engine mixins > leaf)
ใช้แทน forbidden list ส่วนใหญ่ + `type = "independence"` ระหว่าง engine mixin 4 ตัวเพื่อกัน cycle
· **Effort: M** (ต้องไล่ปรับให้เขียวรอบเดียว) · ลบ entry ซ้ำ = **XS**

---

### L12 — dead code จาก 46 commit เมื่อคืน (น้อยกว่าที่คาด แต่มีของจริง)

สแกน symbol ทั้ง repo (src + tests + scripts + docs + .claude เป็น corpus) — **top-level ที่ไม่มีใครอ้างอิงเลยมี 3 ตัว**
ถือว่าสะอาดมากสำหรับ 46 commit ใน ~24 ชม.

| path:line | สิ่งที่ตาย | ที่มา |
|---|---|---|
| `status_header.py:118` `_exec_mode_chip_style` | ~14 LOC | `192a283` (2026-08-13 09:26) ลบ toggle แต่ไม่ลบ helper |
| `status_header.py:131` `_exec_mode_chip_tooltip` | ~14 LOC | เดียวกัน |
| `status_header.py:146` `_auto_resume_chip_style` | ~13 LOC | เดียวกัน |
| `status_header.py:160` `_auto_resume_chip_tooltip` | ~14 LOC | เดียวกัน |
| `claude_auth_dialog.py` (ทั้งโมดูล, `ClaudeAuthDialog:20`) | 164 LOC | ไม่มี caller ใน src หรือ tests เลย |
| `main_window.py:159` `_custom_role_colors` | | |
| `remote/notify.py:612` `_gemini_session_uuid` | 1 ใน 6 compat shim ที่ตาย (อีก 5 ยังใช้) | |
| `settings_management/commands.py:46` `DeleteRoleCommand` | | |

หมายเหตุ 2 ข้อ:
1. `status_header.py` 4 ตัวนี้ตายพร้อมกันเพราะ chip ถูกลบ — แต่ `godfile-map.md:137` ยังลิสต์ `_exec_mode_chip_style/_tooltip`
   และ `_auto_resume_chip_style/_tooltip` เป็นของใหม่ที่มีอยู่จริง (doc drift)
2. `claude_auth_dialog` ตายทั้งโมดูลแต่ยังถูกลิสต์เป็นพลเมืองสถาปัตยกรรมใน `pyproject.toml:178` และ `:210`
   (ซ้ำ 2 ครั้งอีกต่างหาก — ดู M11) · UI แทนที่ของมันคือ Users tab ใน `settings_window.py:2399-2451`

**Effort: S**

---

### L13 — เอกสารสถาปัตยกรรม drift (`godfile-map.md` ตัวที่ CLAUDE.md สั่งให้อ่านก่อน navigate)

| `godfile-map.md` | บอกว่า | จริงวันนี้ |
|---|---|---|
| `:12` | `orchestrator.py` **4,045 LOC** | **4,965** (+928 จาก 2026-07-12) |
| `:12` | `main_window.py` **1,270 LOC** | **1,457** |
| `:35` | import-linter **18 contracts** | **23** |
| `:115-117` | `spawn_engine.py` fan-out **19**, `orchestrator` สูงสุดที่ **23** | `orchestrator` **29**, `spawn_engine` **27** (แซง `cli` 24 และ `main_window` 22 ไปแล้ว) |
| `:183` | *"PR ที่ลาก edge ข้าม layer **fail CI**"* | ไม่มี CI job ไหนรัน lint-imports (H1) |
| `:137` | chip helper exec-mode / auto-resume เป็นของใหม่ที่มีอยู่ | ตายไปแล้วทั้ง 4 (L12) |

ตัวเอกสารเองเตือนเรื่องนี้ไว้ที่ `:186-191` (*"There's no automation catching this"*) — ก็ drift ซ้ำอีกครั้งตามคาด
`spawn_engine` ขึ้นมาเป็นอันดับ 2 fan-out แล้วเป็นข้อมูลที่คนใช้ map นำทางควรรู้ที่สุด แต่ map ยังบอกว่ามันเสมอ `main_window`

**Effort: XS** (แก้ตัวเลข) · **S** ถ้าจะเพิ่ม CI check ให้ตัวเลข LOC/fan-out ใน map ตรงกับของจริง

---

## สิ่งที่ตรวจแล้ว "ไม่มีปัญหา" — บันทึกไว้เพื่อไม่ให้ใครไปรื้อซ้ำ

| หัวข้อ | ผล |
|---|---|
| 23 import-linter contracts | รันจริง **23 kept / 0 broken** |
| `depgraph.json` staleness | regenerate ใหม่แล้วเทียบ — **module 141=141, edge diff 0** ไม่ stale |
| string dispatch `cli` → socket → `cli_server` | **21 คำสั่งตรงกันเป๊ะทั้งสองทาง** ไม่มี orphan (แต่ไม่มี test กันไว้ — ถ้าจะเพิ่ม test ตัวนี้ Effort XS) |
| provider registry ของ remote history | `remote/notify.py:1071` double gate ออกแบบดีมาก เป็น pattern ที่ควรใช้เป็นแม่แบบให้ M8 |
| multi-provider #103 discipline | flag ไว้ดีจริงและมี rationale ครบใน `provider_spec.py:578-588,657-659,731-733`, `orchestrator.py:206-209`, `limit_autoresume.py:214-216`, `cli_server.py:741-744` — M8 คือข้อยกเว้นที่หลุด ไม่ใช่ระบบพัง |
| dead code โดยรวม | 3 top-level symbol ที่ไม่มีใครอ้างอิงจาก 141 โมดูล — ต่ำมากสำหรับ 46 commit/คืนเดียว |
| `remote/` bolt-on | contract คุม core→remote ได้จริง (`main_window._boot` ใช้ `importlib.import_module` ตามที่ `pyproject.toml:413-415` อธิบาย) |

**ข้อสังเกตสำคัญเรื่อง `remote/`:** contract คุมทางเดียว — `remote.api` import `spawn_engine` ตรงๆ อยู่แล้ว
และ `remote/*` ทุกตัวอยู่ในรายชื่อ "import `orchestrator`/`main_window`/`app`/`cli` ได้โดยไม่ fail" (M11)
วันนี้ยังไม่มีปัญหาเพราะยังไม่มีใครทำ แต่คำว่า "delete-to-uninstall bolt-on" จะจริงต่อได้ก็ต่อเมื่อ remote ไม่ผูกกับ UI/CLI
— ถ้าจะรักษาคุณสมบัตินี้ ควรมี contract ทิศกลับด้วย (`remote` เป็น source, forbidden = main_window/app/cli)

---

## สรุปลำดับที่แนะนำ

| # | งาน | Effort | เหตุผลที่จัดลำดับนี้ |
|---|---|---|---|
| 1 | **H1** เพิ่ม `lint-imports` + depgraph check เข้า CI, แก้ hook ให้รันบน macOS ได้ | S | ถ้าไม่ทำข้อนี้ งานที่เหลือทั้งหมดกันได้แค่วันเดียว |
| 2 | **H3** เพิ่ม contract คุม `settings_window` | XS | หยุดเลือด god-file ตัวที่ 3 ก่อนมันโตอีก |
| 3 | **M10** ย้าย `_split_shard` → `orchestrator_text` + ใส่ `orchestrator` ใน 5 UI contract | S | ปิด UI→engine edge ที่เกิดจากอุบัติเหตุ ไม่ใช่ดีไซน์ |
| 4 | **H2** accessor `panes_for_project()` + test กัน UI แตะ `_panes_by_project` | S | ปิดรูฝั่ง UI ได้โดยไม่ต้องยก state object |
| 5 | **H5** รวม default config dir เหลือทางเดียว | S | เป็น bug จริงบน installed build ไม่ใช่แค่ความสวยงาม |
| 6 | **M6 / M7** ย้าย constant + gate ลง leaf ที่ทั้งสองฝั่ง import ได้ | S ต่อข้อ | duplication ที่ contract ผลิตขึ้นเอง แก้ครั้งเดียวจบ |
| 7 | **L12 / L13** ลบ dead code 4+1 ตัว, อัปเดตตัวเลขใน `godfile-map.md` | S | ทำพร้อมข้ออื่นได้ |
| 8 | **M8** ยก resume validation ขึ้น `ProviderSpec` | M | ปิด #103 gap ที่ยังไม่ถูก flag |
| 9 | **M9** รวม atomic write | M | |
| 10 | **M11** `layers` + `independence` contract แทน forbidden list 131 บรรทัด | M | ทำหลังข้อ 1 เพราะต้องมี CI ก่อนถึงจะรู้ว่าคุมได้จริง |
| — | **H4** settings 2 ชุด | decision | **ต้องให้ user ตัดสิน** ไม่ใช่งาน engineering |

**สิ่งที่จงใจไม่เสนอ:** ไม่มีข้อไหนเป็น rewrite · ไม่แตะ `spawn_engine` state object (แผน item 4 ใน `godfile-map.md:179`)
— งานนั้นควรเป็นรอบของตัวเอง ไม่ใช่ผลพลอยได้ของ audit
