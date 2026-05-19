# Lead Auto-routing (Propose-then-fire) — Design Spec

**Date:** 2026-05-19
**Status:** Approved (pending implementation plan)
**Author:** Lead (agent-takkub cockpit)

## Goal

ให้ Lead pane "ลุยงานต่อได้เอง" หลัง user สั่งงานหรือหลัง teammate ทำเสร็จ โดย:

1. **ตรวจจับ** ว่า user message เป็น "งานสั่ง" (actionable) หรือแค่ "คำถาม"
2. **เสนอแผน routing** เป็น markdown table (role · task · cwd) ก่อนลงมือ
3. **รอ confirm** จาก user (propose-then-fire) ห้าม auto-fire เด็ดขาด
4. หลัง `[role done]` เด้งเข้า Lead pane → **auto-propose ขั้นถัดไป** (verify/handoff/done) แล้วรอ confirm อีกครั้ง

target: user สั่ง "add login" 1 ครั้ง → Lead ทำเองตลอด pipeline (implement → verify → review) โดย user แค่กด ok 3-4 ครั้ง

## Non-Goals

- **ไม่ทำ auto-fire (zero confirmation)** — ถูก reject ใน brainstorm เพราะเสี่ยง spawn ผิด role / กิน context
- **ไม่แตะ Python / orchestrator** — เปลี่ยนแค่ `CLAUDE.md` 1 ไฟล์ logic อยู่ใน Lead's system prompt
- **ไม่สร้าง CLI helper ใหม่** (เช่น `takkub route` / `takkub propose`) — Lead เป็น LLM ใช้ judgment เองได้
- **ไม่ทำ deterministic file-type → role mapping** (e.g., `*.tsx` → frontend) — keyword-based ใน decision table พอ ความยืดหยุ่นสำคัญกว่า
- **ไม่ทำ state machine สำหรับ pipeline** — Lead ตัดสินใจ next step ทุกครั้ง ไม่ hard-code "implement→test→review" sequence
- **ไม่กระทบ teammate panes** — auto-routing เป็น Lead-only behavior teammate ทำงานเหมือนเดิม

## Architecture — Single-file edit

```
CLAUDE.md (cockpit root)
    │
    └── เพิ่ม section ใหม่ "## Auto-routing (propose-then-fire)"
        วาง ก่อน "## เมื่อรับงานใหม่" (override กฎเดิม)
            │
            ├── (1) Actionable detector
            ├── (2) Routing decision table
            ├── (3) Proposal template
            └── (4) Done-handoff rule
                │
                ▼
        _render_lead_context() pick up อัตโนมัติทุก spawn
        (ไม่ต้องแก้ orchestrator — function อ่าน CLAUDE.md whole-file)
```

ไม่ต้อง deploy ไม่ต้อง rebuild — push CLAUDE.md ครั้งเดียวจบ Lead spawn ถัดไปก็เห็นพฤติกรรมใหม่

## Component Details

### (1) Actionable detector

หลักการ 1 บรรทัด: **verb ของ user message คืออะไร**

| User verb | คือ | Lead ทำ |
|---|---|---|
| add / build / implement / fix / refactor / migrate / setup / deploy / test | งานสั่ง (actionable) | → propose routing |
| explain / why / show / read / ดู / อธิบาย / ทำไม / สรุป / list / what | คำถาม (informational) | → ตอบปกติ ไม่ propose |
| คำถามผสม "X ทำงานยังไง แล้วช่วย fix หน่อย" | mixed | → ตอบส่วน explain ก่อน แล้ว propose ส่วน fix |

**Edge case rules:**
- งานเล็กใน cockpit เอง (typo CLAUDE.md / 1-line projects.json) → skip proposal Lead ทำเอง (อยู่ใน "ทำเองได้" เดิม)
- User ระบุ role ตรงๆ ("ให้ backend ทำ X") → skip propose fire ตรงๆ
- User ใช้ imperative ลอยๆ ("ลอง X ดู") → propose (default actionable)

### (2) Routing decision table

Keyword-based mapping (ไม่ใช่ exhaustive — Lead ใช้ judgment ปรับ):

| Keyword/pattern ใน task | Primary role | Auto cross-check |
|---|---|---|
| UI / page / form / component / button / style / CSS | frontend | — |
| endpoint / API / route / handler / schema / db / migration | backend | — |
| mobile / iOS / Android / Capacitor / React Native | mobile | — |
| docker / CI / deploy / pipeline / infra / k8s / nginx | devops | — |
| refactor / extract / migrate A→B / rename | primary (ตามไฟล์) | **+ codex** (เทียบ diff) |
| rollout / strategy / phase / migration plan | gemini | — |
| test / smoke / e2e / regression | qa | — |
| review / code review / security | reviewer | — |
| งาน feature ใหญ่ (UI + API) | frontend + backend (parallel) | — |
| งาน complex / สงสัย approach | primary | **+ gemini** (1M context plan) |

**Rule of thumb:** ถ้า task มีคำว่า "refactor/migrate" → +codex; ถ้ามี "rollout/plan/safe deploy" → +gemini

### (3) Proposal template

Lead ใช้ template เดียวกันทุกครั้ง — markdown table 3 columns + คำถามปิดท้าย:

```markdown
**แผน:**

| Role | Task | cwd |
|---|---|---|
| frontend | เพิ่มหน้า /login form (email+password) ใช้ shadcn/ui | pms/pms-web |
| backend | POST /auth/login {email,password}→{token,user} JWT HS256 24h | pms/pms-api |
| codex | review approach: edge cases I miss in /auth/login design? | pms/pms-api |

3 role ทำ parallel ใช้เวลา ~10s spawn รวม

**ok ลุยเลย หรือแก้ไข?** (ระบุ "ใช้ qa แทน codex" / "เอาแค่ backend" / "เปลี่ยน cwd X เป็น Y")
```

**Rules:**
- ทุก row ต้องมี cwd ชัดเจน (ห้าม blank — กัน spawn ผิด project)
- ถ้า task ใหญ่ ใส่ note ว่าเป็น parallel หรือ sequential
- ปิดท้ายด้วยคำถาม confirm เสมอ ห้ามตอบความถัดไปก่อน user ตอบ

### (4) Done-handoff rule

ตอน `[<role> done] <note>` เด้งเข้า Lead pane:

1. **อ่าน report** สรุปสิ่งที่ done ทำเสร็จ (1-2 บรรทัด)
2. **ตัดสิน next step** จาก current pipeline state:
   - implementation done → propose verify (qa + reviewer parallel)
   - verify pass → propose ship (commit/PR — แต่ห้าม push เอง)
   - verify fail → propose fix loop (กลับไป primary role)
   - ไม่มี work เหลือ → สรุป "งานเสร็จ ปิด session?" ไม่ propose role ใหม่
3. **Render proposal** ตาม template ใน (3)
4. **รอ confirm** อีกครั้ง (ห้าม chain auto-fire)

**Edge case:** ถ้า role report ว่า blocked (เช่น "ขาด API spec") → Lead propose unblock action (เช่น "ให้ backend ส่ง spec มา + frontend wait") ไม่ใช่ verify

## Data flow

```
user message
    │
    ▼
Lead: actionable detector
    │
    ├── informational ──→ reply ปกติ (จบ flow)
    │
    └── actionable
            │
            ▼
        Lead: routing decision (read table + judge)
            │
            ▼
        Lead: render proposal table
            │
            ▼
        wait user response
            │
            ├── "ok ลุย"     ──→ fire takkub assign (parallel ถ้าได้)
            ├── "แก้: X→qa"   ──→ update plan → re-render
            └── "ไม่เอา"     ──→ abort คุยต่อปกติ
                    │
                    ▼ (หลัง fire)
                wait [role done] event
                    │
                    ▼
                Lead: อ่าน report → propose next step
                    │
                    └── loop กลับไป "wait user response"
```

## Error handling / redirect paths

| สถานการณ์ | Lead ต้องทำ |
|---|---|
| Lead เดา role ผิด user แก้ "ใช้ qa แทน reviewer" | update proposal row นั้น re-render รอ confirm ใหม่ |
| User ตอบกำกวม ("เออๆ" / "ok แต่...") | ห้าม assume confirm ถามซ้ำให้ชัดก่อน fire |
| User บอก "หยุด propose ก่อน" (Lead propose ผิดบริบท) | abort plan ปัจจุบัน คุยต่อปกติ |
| User reply "แก้ X แล้วลุยเลย" (one-shot edit + fire) | apply edit + fire ทันที (ไม่ต้อง re-confirm — user ระบุชัด) |
| Lead ลืม propose ลุย takkub assign เอง | ผิดกฎ user reply "หยุด" → Lead `takkub close --role X` rollback |
| Task เล็กมาก (typo บรรทัดเดียวใน cockpit) | skip proposal ทำเองได้ (อยู่ใน "Lead ทำเองได้" เดิม) |
| Multiple actionable items ใน 1 message | render proposal เดียวที่รวมทุก row พร้อม note ว่า parallel/sequential |

## Testing — Manual dry-run

หลัง deploy spawn Lead pane ใหม่ ลอง 5 cases:

| # | Input | Expected behavior |
|---|---|---|
| 1 | "add /logout endpoint pms-api" | propose backend (+ codex cross-check) ปิดท้าย "ok?" |
| 2 | "ทำไม login ช้า" | reply ปกติ ไม่ propose |
| 3 | "build login UI + API" | propose frontend+backend parallel ใน table เดียว |
| 4 | (สมมติ [backend done] ส่งจาก teammate) | Lead อ่าน report → auto-propose qa+reviewer verify |
| 5 | "ok แต่ใช้ qa แทน reviewer" | re-render proposal เปลี่ยน row reviewer → qa รอ confirm |

ผ่าน 5/5 = design ใช้งานได้ deploy ถาวร
ผ่าน <5 = แก้ rules ใน CLAUDE.md (เพิ่ม example / sharper verb list)

## Open questions (defer ตอน implement)

ไม่มี — ทุกอย่าง lock ใน brainstorm แล้ว
