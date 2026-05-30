# Token Reduction Review (2026-05-30)

รอบสองต่อจาก [`token-reduction-review-2026-05-28.md`](./token-reduction-review-2026-05-28.md).
ทุกตัวเลข **วัดจากดิสก์/โค้ดจริง** ไม่ใช่ประมาณลอยๆ (char→token: ไทย ~2.5 char/tok, eng/code ~4 char/tok).

## 0. สิ่งที่ทำไปแล้วตั้งแต่รอบก่อน (✅ ปิดได้)

| รอบก่อนเสนอ | สถานะปัจจุบัน (verified) |
|---|---|
| Scope browser MCP ไม่ให้ leak ทุก pane | ✅ **ทำแล้ว** — `--setting-sources project,local` บล็อก user MCP (`chrome-devtools`,`pms`) + `_ROLE_MCP_POLICY` กรองต่อ role · `runtime/shared-mcp-lead.json` = `{}` (26 chars) ยืนยัน lead/backend ไม่โหลด browser MCP เลย ประหยัด ~12-16k tok/pane ตามที่ code comment เขียนเอง |
| Condense global `~/.claude/CLAUDE.md` | ✅ **ทำแล้ว** — เหลือ 902 chars (จากที่เคยมีตาราง RTK ยาว) |
| Uninstall ecc | ✅ ถอนจากดิสก์แล้ว (เหลือ stale entry ในโค้ด — ดู F4) |
| claude-obsidian SessionStart hook leak เข้า pane | ✅ **ไม่ leak** — `--setting-sources project,local` dodge hook นี้อยู่แล้ว (hook ยัง fire เฉพาะ session ตรงของ user เอง นอก cockpit) |

→ งาน MCP/hook/global-CLAUDE รอบก่อน **ปิดหมดแล้ว** ไม่ต้องทำซ้ำ

## 1. เหลือจริง — เรียงตาม impact (token × ความถี่)

### F1 — Plugin skills inject เข้า **ทุก** pane 🔴 (impact สูงสุด)
- **หลักฐาน:** `--plugin-dir` อยู่ใน argv ร่วม (`orchestrator.py:1289`) → inject ทั้ง Lead + teammate ทุกตัว
  - superpowers-dev = **14 skills**
  - addy-agent-skills = **44 skills + 7 agents** ← ตัวหลักที่บวม
  - pordee = 2 skills (Thai compression — ใช้จริง เก็บ)
- **= 60 skill descriptions โหลดเข้า system prompt ทุก pane** ประเมิน ~3-5k tok/pane
- **Aggregate:** session ที่มี Lead + 3-4 teammate = 4-5 pane → **~12-25k tok/session** (ใหญ่กว่า CLAUDE.md เพราะคูณจำนวน pane)
- **ปม:** cockpit ใช้ `takkub assign --role` (pane ของตัวเอง) เป็นกลไกหลัก — addy-agent-skills (spec/planning/frontend-ui ฯลฯ) ซ้ำกับ role `.md` prompt อยู่แล้ว จึงแทบไม่ถูกเรียกใน pane
- **Action:** ถอด `addy-agent-skills` (และพิจารณา `superpowers-dev`) ออกจาก `_SAFE_PLUGINS` · เก็บ pordee
  - หรือทำ **plugin allowlist ต่อ role** (เหมือน `_ROLE_MCP_POLICY`) — ให้เฉพาะ role ที่ใช้ skill จริง
- **ประหยัด:** ~3-5k tok/pane × ทุก pane = **win ใหญ่สุด**
- **Trade-off:** pane จะเรียก `/skill-name` ของ plugin นั้นไม่ได้ (ถ้ายังอยากใช้บาง skill → ใส่ role-scoped)

### F2 — `CLAUDE.md` 18.3KB inject ทุก Lead spawn 🟡 (⚠️ แก้สมมติฐาน)
- **หลักฐาน:** 18,285 chars / 289 บรรทัด ไทยหนัก ≈ **6-7k tok/Lead spawn** (rendered `lead-context.md` = 20KB รวม BLOCKED_DIRS + brief) · เฉพาะ Lead (1 pane) ไม่คูณ
- **‼️ สมมติฐานเดิมผิด:** คิดว่า routing table ซ้ำกับ `routing_planner.py` → **ผิด** verify แล้ว `routing_planner.py` **ไม่ถูก import ใน `src/` เลย (test-only spec)** · `classify()` ไม่ได้ inject เข้า Lead → Lead อ่าน prose ใน CLAUDE.md ตรงๆ เป็น behavior จริง
- **สรุป:** routing decision table / confirm-handling / proposal template / auto-fire exceptions = **load-bearing ห้ามตัด** (ตัด = routing/confirm พัง)
- **เหลือตัดได้แบบปลอดภัยจริง (เล็ก):** ย่อ bash example ซ้ำใน "Parallel dispatch" (parallel/sequential/mixed/auto-chain/spec-interpolation = 5 บล็อก → 2 + note) — เป็น illustration ไม่ใช่กฎ
- **ประหยัด:** ~1-1.5k tok/Lead spawn เท่านั้น (ไม่ใช่ 2-3k ตามที่ประเมินผิดตอนแรก)
- **Trade-off:** ⚠️ แตะไฟล์ behavior-critical เพื่อ win เล็ก — **ควรให้ user เห็น diff ก่อน apply**

### F3 — Role agent prompts 5.4-8.8KB/role 🟢 (เล็ก)
- qa.md 8.8KB, critic.md 7.9KB, ที่เหลือ ~5.5KB · inject เฉพาะ role ที่ spawn (ไม่คูณ)
- **Action (optional):** ย่อ qa/critic ลงให้เท่า role อื่น (~5.5KB) ประหยัด ~1k tok เฉพาะตอน spawn role นั้น — impact ต่ำ ทำทีหลังได้

### F4 — stale `ecc` ใน `_SAFE_PLUGINS` ⚪ (housekeeping, 0 token)
- `lead_context.py:83` ยัง list `"ecc"` แต่ถอนจากดิสก์แล้ว — `_default_plugin_dirs()` ข้ามเพราะเช็ค existence → ไม่กิน token แต่เป็น dead entry ลบให้ clean

## 2. No-touch (อย่าแตะ)
- **MCP scoping / `--setting-sources`** — ทำดีแล้ว
- **per-role model tier / fallback-model** — เรื่อง latency/cost ไม่ใช่ token context
- **recent-session brief (เพิ่งเพิ่ม)** — cap 4KB, +300-400 tok/Lead spawn เท่านั้น คุ้ม (agent อ่าน note กลับได้)

## 3. สรุป win + สถานะ
| # | งาน | ประหยัด | risk | สถานะ |
|---|---|---|---|---|
| F1 | role-scoped plugin (`_ROLE_PLUGIN_POLICY`): drop addy-agent-skills ทุก role · lead=pordee · teammate=superpowers-dev+pordee | ~3-5k tok × ทุก pane | กลาง (pane เสีย /skill addy) | ✅ **DONE** (commit) — 5 tests + full suite + ruff ผ่าน |
| F2 | ย่อ bash example ซ้ำใน CLAUDE.md (ห้ามแตะ rule tables — routing_planner ไม่ wired เข้า Lead) | ~1-1.5k tok/Lead spawn | กลาง (behavior-critical file) | ⏸️ propose diff รอ user |
| F4 | ลบ stale ecc จาก _SAFE_PLUGINS | 0 (cleanup) | **กลาง** — `doctor.py` special-case ecc + test assert | ❌ **skip** (เสี่ยงพัง doctor เพื่อ 0 token ไม่คุ้ม) |

**ผลจริง:** F1 (win ใหญ่สุด คูณทุก pane) ทำเสร็จแล้ว · F2 เหลือ win เล็กกว่าที่คิด + เสี่ยง → รอ user ดู diff · F4 ตัดสินใจไม่ทำ

> **บทเรียน review รอบนี้:** อย่าเชื่อว่า "prompt ซ้ำกับ code" จนกว่าจะ verify ว่า code ตัวนั้น **wired เข้า runtime จริง** — `routing_planner.py` ดูเหมือน source of truth แต่เป็น test-only spec, Lead อ่าน CLAUDE.md prose ตรงๆ
