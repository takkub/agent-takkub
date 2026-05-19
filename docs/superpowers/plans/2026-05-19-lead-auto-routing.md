# Lead Auto-routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ให้ Lead pane ทำ "propose-then-fire" routing อัตโนมัติเมื่อ user สั่งงาน actionable หรือเมื่อ `[role done]` event เด้งเข้า — ลด manual dispatch ของ user เหลือแค่กด confirm

**Architecture:** Single-file edit ใน `CLAUDE.md` (cockpit root) เพิ่ม 1 section ใหม่ชื่อ `## Auto-routing (propose-then-fire)` วาง**ก่อน** `## เมื่อรับงานใหม่` (override พฤติกรรมเดิม) `_render_lead_context()` ใน `orchestrator.py` อ่าน CLAUDE.md ทั้งไฟล์อยู่แล้ว → ไม่ต้องแตะ Python

**Tech Stack:** Markdown only — เปลี่ยน Lead's system prompt ผ่าน CLAUDE.md ใช้ผ่านทุก spawn ของ Lead pane (renders fresh ทุกครั้ง พร้อม BLOCKED_DIRS)

**Spec reference:** `docs/superpowers/specs/2026-05-19-lead-auto-routing-design.md`

**TDD note:** เป็น behavioral/prompt change ไม่ใช่ code change ดังนั้น "test" ไม่ใช่ pytest แต่เป็น manual dry-run 5 cases ใน fresh Lead pane (ตามที่ spec ระบุ) verification = observe Lead's behavior matches expected pattern

---

## File Structure

**Files modified:**
- `CLAUDE.md` (cockpit root) — เพิ่ม 1 section ก่อนบรรทัด 207 (ก่อน `## เมื่อรับงานใหม่`)

**Files NOT touched:**
- `src/agent_takkub/orchestrator.py` — `_render_lead_context()` อ่าน CLAUDE.md whole-file ใช้ได้เลย
- `.claude/agents/*.md` — teammate specialist prompts ไม่กระทบ (auto-routing เป็น Lead-only)
- Tests — ไม่มี automated test สำหรับ prompt content

---

## Task 1: Insert Auto-routing section into CLAUDE.md

**Files:**
- Modify: `CLAUDE.md` (insert before line 207 `## เมื่อรับงานใหม่`)

- [ ] **Step 1: Confirm insertion anchor exists exactly once**

Run:
```bash
rtk grep -n "^## เมื่อรับงานใหม่" CLAUDE.md
```
Expected: exactly 1 match at line 207 (หรือใกล้เคียง — line อาจ shift ถ้ามี edit ก่อนหน้า)

ถ้าได้ 0 หรือ ≥2 match → หยุด ปรึกษา user (anchor ผิด หรือ CLAUDE.md ถูกแก้ไประหว่าง brainstorm)

- [ ] **Step 2: Apply Edit — insert new section before anchor**

ใช้ Edit tool ใน `CLAUDE.md`:

**old_string:**
```
## เมื่อรับงานใหม่
```

**new_string:**
```
## Auto-routing (propose-then-fire)

Lead ต้องทำ **propose-then-fire** ทุกครั้งที่ user สั่งงาน — ห้าม auto-fire `takkub assign` โดยไม่ confirm ก่อน

### 1. Actionable detector

อ่าน verb ของ user message ก่อนตอบ:

| User verb | คือ | Lead ทำ |
|---|---|---|
| add / build / implement / fix / refactor / migrate / setup / deploy / test / ทำ / สร้าง / แก้ | actionable | → propose routing |
| explain / why / show / read / ดู / อธิบาย / ทำไม / สรุป / list / what | informational | → ตอบปกติ ไม่ propose |
| mixed (\"X ทำงานยังไง แล้วช่วย fix หน่อย\") | mixed | → ตอบ explain ก่อน แล้ว propose ส่วน fix |

**Edge cases:**
- งานเล็กใน cockpit (typo CLAUDE.md / 1-line projects.json) → skip propose Lead ทำเอง (อยู่ใน \"Lead direct-edit policy\" เดิม)
- User ระบุ role ตรงๆ (\"ให้ backend ทำ X\") → skip propose fire `takkub assign --role backend` ตรงๆ
- User imperative ลอยๆ (\"ลอง X ดู\") → propose (default actionable)

### 2. Routing decision table

| Keyword/pattern ใน task | Primary role | Auto cross-check |
|---|---|---|
| UI / page / form / component / button / style / CSS | frontend | — |
| endpoint / API / route / handler / schema / db / migration | backend | — |
| mobile / iOS / Android / Capacitor / React Native | mobile | — |
| docker / CI / deploy / pipeline / infra / k8s / nginx | devops | — |
| refactor / extract / migrate A→B / rename | primary (ตามไฟล์) | **+ codex** เทียบ diff |
| rollout / strategy / phase / migration plan | gemini | — |
| test / smoke / e2e / regression | qa | — |
| review / code review / security | reviewer | — |
| feature ใหญ่ (UI + API) | frontend + backend (parallel) | — |
| complex / สงสัย approach | primary | **+ gemini** (1M context) |

**Rule of thumb:** คำว่า \"refactor/migrate\" → +codex; คำว่า \"rollout/plan/safe deploy\" → +gemini

### 3. Proposal template (ใช้ทุกครั้ง)

\`\`\`markdown
**แผน:**

| Role | Task | cwd |
|---|---|---|
| frontend | <task ของ frontend> | <project path> |
| backend  | <task ของ backend>  | <project path> |
| codex    | review approach: <question> | <project path> |

<note: parallel หรือ sequential + เหตุผล สั้นๆ>

**ok ลุยเลย หรือแก้ไข?** (เช่น \"ใช้ qa แทน codex\" / \"เอาแค่ backend\" / \"เปลี่ยน cwd X→Y\")
\`\`\`

**Rules:**
- ทุก row ต้องมี cwd ชัดเจน (ห้าม blank — กัน spawn ผิด project)
- ใส่ note ว่า parallel หรือ sequential เสมอ
- ปิดท้ายด้วยคำถาม confirm **ห้าม fire ก่อน user ตอบ**

### 4. Done-handoff rule

หลัง `[<role> done] <note>` เด้งเข้า Lead pane:

1. **อ่าน report** สรุป 1-2 บรรทัด ว่า role ทำอะไรเสร็จ
2. **ตัดสิน next step:**
   - implementation done → propose verify (qa + reviewer parallel)
   - verify pass → propose ship (commit/PR — แต่ห้าม push เอง)
   - verify fail → propose fix loop (กลับไป primary role)
   - งานเสร็จไม่มี work เหลือ → สรุป \"เสร็จ ปิด session?\" ไม่ propose role ใหม่
   - role report ว่า blocked → propose unblock action (เช่น \"ให้ backend ส่ง spec มา + frontend wait\")
3. **Render proposal** ตาม template ใน (3)
4. **รอ confirm** อีกครั้ง **ห้าม chain auto-fire**

### 5. Confirm handling

| User reply | Lead ทำ |
|---|---|
| \"ok\" / \"ลุย\" / \"ลุยเลย\" / \"go\" / \"เอาเลย\" | fire ทุก row ใน table — parallel ถ้าได้ |
| \"แก้: X→Y\" / \"ใช้ Z แทน W\" | update row นั้น re-render รอ confirm ใหม่ |
| \"แก้ X แล้วลุยเลย\" (edit + fire) | apply edit + fire ทันที (ไม่ต้อง re-confirm) |
| \"ไม่เอา\" / \"stop\" / \"หยุด\" | abort plan คุยต่อปกติ |
| \"เออๆ\" / \"ok แต่...\" / กำกวม | **ห้าม assume confirm** ถามซ้ำให้ชัดก่อน fire |

**Rollback:** ถ้าเผลอ fire ก่อน confirm (ผิดกฎ) → `takkub close --role <X>` ปิด pane ที่ผิด แล้ว propose ใหม่

---

## เมื่อรับงานใหม่
```

หมายเหตุการ escape: ใน new_string ข้างบน backticks ใน code block ตัวอย่าง (block ที่เริ่ม `\`\`\`markdown`) ใส่ backslash escape เพื่อให้ Edit tool ยอมรับ ตอน apply จริงให้ลบ backslash ออก ใช้ backtick triple ตามปกติ

- [ ] **Step 3: Verify diff via git**

Run:
```bash
rtk git diff CLAUDE.md
```

Expected เห็น:
- Insertion ของ `## Auto-routing (propose-then-fire)` section พร้อม sub-section 1-5
- บรรทัด `## เมื่อรับงานใหม่` ยังอยู่ครบไม่ถูกแก้
- ไม่มีบรรทัดอื่นของ CLAUDE.md ที่ถูกลบ/แก้

ถ้าเห็นการแก้นอกเหนือจาก insertion → `rtk git checkout CLAUDE.md` แล้ว retry Step 2

- [ ] **Step 4: Sanity-check rendered Lead context (optional)**

ถ้า Lead pane เปิดอยู่ตอนนี้ แตะให้ orchestrator re-render context ผ่าน respawn (ปิดแล้วเปิดใหม่ใน UI) ไม่ก็เปิดไฟล์ `runtime/lead-context.md` ดูตรงๆ:

```bash
rtk read runtime/lead-context.md
```

Expected: section `## Auto-routing (propose-then-fire)` อยู่ก่อน `## เมื่อรับงานใหม่` และ BLOCKED_DIRS suffix ยังอยู่ปลายไฟล์

ถ้าไม่มี runtime/lead-context.md (Lead ยังไม่เคย spawn ใน session นี้) ข้าม step นี้ได้ — ไฟล์จะ render ตอน spawn ครั้งถัดไป

- [ ] **Step 5: Commit**

```bash
rtk git add CLAUDE.md
rtk git commit -m "$(cat <<'EOF'
feat(lead): auto-routing propose-then-fire behavior

Adds "Auto-routing" section to cockpit CLAUDE.md so Lead detects
actionable tasks, proposes a routing plan (role/task/cwd table),
waits for confirm, then fires takkub assign. After [role done]
events, auto-proposes the next step (verify/handoff) and waits
again. Parallel + cross-check (codex/gemini) default for non-
trivial work.

Spec: docs/superpowers/specs/2026-05-19-lead-auto-routing-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Expected: commit สำเร็จ 1 file changed มี `+` ประมาณ 85-100 บรรทัด

---

## Task 2: Dry-run validation (manual, in fresh Lead pane)

**Files:** — (ไม่แก้ไฟล์ — เป็น behavioral test)

ก่อนเริ่ม: ต้อง spawn Lead pane ใหม่ (หรือ respawn ตัวเดิม) เพื่อให้ context รวม section ใหม่ ถ้า Lead เดิมยัง running จะใช้ system prompt เก่า

- [ ] **Step 1: Respawn Lead pane**

ใน cockpit UI:
1. ปิด Lead pane เดิม (ถ้ามี — ปกติคือ pane นี้ที่กำลังคุยอยู่ ดังนั้น user ต้องเปิด pane Lead ใหม่จากภายนอก หรือ test ผ่าน Lead instance ใหม่ใน cockpit)
2. เปิด project ที่มี multi-path setup (เช่น pms ที่มี pms-web + pms-api) เพื่อให้ test parallel ได้

**Note สำหรับ executor:** ถ้า dry-run ใน Lead pane ที่ executing plan อยู่นี้ Lead จะใช้ system prompt เดิมที่ไม่มี section ใหม่ ดังนั้น dry-run ที่แท้จริงต้องทำกับ Lead pane ที่ spawn **หลัง** Task 1 commit แล้ว — บอก user ให้ test เองหลัง commit แทนการ self-test

- [ ] **Step 2: Case 1 — actionable single role + cross-check**

Input ใน Lead pane: `add /logout endpoint pms-api`

Expected behavior:
- Lead **ไม่ fire** `takkub assign` ทันที
- Lead render proposal table ที่มี:
  - row 1: `backend` | `<task เกี่ยวกับ /logout endpoint>` | `pms/pms-api` (หรือ path จาก projects.json)
  - row 2 (optional): `codex` | `review approach: ...` | `pms/pms-api`
- ปิดท้ายด้วย "ok ลุยเลย หรือแก้ไข?"

ถ้า Lead fire ตรงๆ ไม่ propose → fail (rules ใน section 3 "ห้าม fire ก่อน user ตอบ" ไม่ทำงาน)

- [ ] **Step 3: Case 2 — informational (no propose)**

Input: `ทำไม login ช้า`

Expected:
- Lead reply ปกติ explain reasoning ที่เป็นไปได้ (DB query, network, etc.)
- **ไม่มี** proposal table
- **ไม่มี** "ok ลุยเลย?" prompt

ถ้า Lead propose dispatch → fail (actionable detector misclassify)

- [ ] **Step 4: Case 3 — parallel multi-role**

Input: `build login UI + API`

Expected:
- Lead render proposal ที่มี 2 row อย่างน้อย:
  - row 1: `frontend` | `<task UI>` | `<web path>`
  - row 2: `backend` | `<task API>` | `<api path>`
- Note ระบุว่า "parallel" หรือ "ทำขนานกัน"
- ปิดท้ายด้วย confirm question

ถ้า Lead propose แค่ row เดียว หรือ sequential → fail (decision table "feature ใหญ่ (UI + API) → frontend + backend parallel" ไม่ทำงาน)

- [ ] **Step 5: Case 4 — done-handoff**

Simulate: ส่ง message เข้า Lead pane manually:
```
[backend done] /auth/logout endpoint + unit tests merged
```
(หรือทำ end-to-end: assign backend task จริง รอ teammate `takkub done` ส่ง event เข้า Lead pane)

Expected:
- Lead อ่าน report สรุป 1-2 บรรทัด
- Lead **auto-propose** verify step: `qa` (smoke test) + `reviewer` (code review) — parallel
- ปิดท้ายด้วย confirm question

ถ้า Lead แค่สรุปแล้วเงียบ → fail (done-handoff rule ไม่ทำงาน)
ถ้า Lead fire qa/reviewer ทันทีไม่ propose → fail (chain auto-fire ที่ห้ามไว้)

- [ ] **Step 6: Case 5 — re-render after redirect**

Context: หลัง Case 4 Lead propose qa+reviewer แล้ว
Input: `ok แต่ใช้ qa แทน reviewer`

Expected:
- Lead **ไม่ fire** สอง row ที่ propose ไว้
- Lead update proposal: ลบ row reviewer (หรือเปลี่ยน task ให้ qa ทำของ reviewer)
- Lead render proposal ใหม่ ปิดท้ายด้วย confirm question อีกครั้ง

ถ้า Lead fire ทันทีตาม proposal เดิม → fail (confirm handling "แก้: X→Y" ไม่ทำงาน)
ถ้า Lead fire reviewer ลงไปด้วย → fail (interpret "ใช้ X แทน Y" ผิด)

- [ ] **Step 7: Tally results**

ผ่าน 5/5 → design ใช้งานได้ จบ plan
ผ่าน <5 → identify case ที่ fail เพิ่ม example หรือ sharper wording ใน section ที่เกี่ยวข้องของ CLAUDE.md แล้ว retry Task 1 Step 2 (apply Edit เพิ่มเติม)

Document ผลใน commit message ตอน push (ถ้ามี iterate)

---

## Self-Review Results

✅ **Spec coverage:** ทุก section ใน spec มี task ที่ implement:
- "Architecture — Single-file edit" → Task 1 Step 2
- "Component Details (4 sub-sections)" → fully expanded ใน Task 1 Step 2 new_string
- "Data flow" → encoded in Confirm handling table + Done-handoff rule
- "Error handling / redirect paths" → encoded in Confirm handling row "เออๆ / ok แต่..." + Rollback note
- "Testing — Manual dry-run (5 cases)" → Task 2 Steps 2-6

✅ **Placeholder scan:** ไม่มี TBD/TODO/vague — ทุก step มี exact text + exact command + exact expected output

✅ **Type consistency:** ไม่มี code change → ไม่มี type/signature drift; section heading text consistent ระหว่าง CLAUDE.md insertion กับ dry-run verification

✅ **No "see Task N":** ทุก step self-contained อ่านแล้วทำได้โดยไม่ต้องย้อนกลับ
