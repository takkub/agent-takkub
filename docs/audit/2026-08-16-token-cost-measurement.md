# Token cost ของการเปิด pane ใหม่ — วัดจริงก่อนตัดตาม #267/#268 (2026-08-16)

Evaluator: devops (`wt/devops-1786849703`, analysis-only — ไม่แตะ source, ไม่ commit ตามคำสั่ง task).
Scope: ตอบ 4 คำถามด้วยตัวเลข token จริงจาก session JSONL ของ Claude Code (`~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`, field `message.usage`), ไม่ใช่การประมาณจากจำนวนอักษร ยกเว้นจุดที่ระบุไว้ชัดเจนว่าเป็นค่าประมาณ

## สรุปผู้บริหาร (อ่านอันนี้พอถ้าไม่มีเวลา)

| # | คำถาม | คำตอบ | หลักฐาน |
|---|---|---|---|
| 1 | เปิด pane ใหม่กิน token เท่าไหร่ก่อนทำงาน | **cold spawn (ไม่มี cache อุ่น): ~37,600–65,900 token** (ผัน 1.8× ตาม role) · **warm spawn (spawn ใกล้กันภายใน ~1hr): ~59,000–64,500 token รวม** (แบ่งเป็น cache_read คงที่ ~22,972 + cache_creation เฉพาะ role ~36,000–42,000) | วัดจาก `message.usage` เทิร์นแรกจริง 16 session วันนี้ (backend/lead/devops/frontend) |
| 2 | ในก้อนนั้นอะไรกินเท่าไหร่ | **วัดตรงได้บางส่วน**: role file (5,181–11,504 tok, cl100k) · root CLAUDE.md ที่ role อื่นไม่ควรได้ (6,728 tok, ทุก pane) · graft MCP schema (847 tok, วัดจริงจาก server) · skills catalog+hook+memory (~6,800 tok, นอกเหนือ cockpit ควบคุม) · **ส่วนที่เหลือ ~35,000–39,000 tok วัดไม่ได้** (Claude Code base system prompt + built-in tool schema เป็น proprietary ไม่ serialize ใน JSONL) | ดู §2 |
| 3 | สัดส่วน pane-open ต่อ token ทั้ง session | **0.199% วันนี้** (16 session, ถ่วงด้วย session ยาวที่มีอยู่จริง) · เทียบกับ **0.4271%** จาก audit 7 วันเดิม (2026-07-24, ก่อน wave3/4 diet) — ทิศทางเดียวกัน คนละ scope/ช่วงเวลา | ดู §3 |
| 4 | ตัดตาม #267 ได้กี่ % | **ตัดไม่ได้เป็น "%" เดียวรวม** — ผลต่างกันมากตามรายการ ตารางจัดอันดับ resend-weighted ด้านล่าง §4 |

**อันดับลงมือ (คุ้มสุด→น้อยสุด, ตาม token ที่กู้คืนได้ × ความถี่):**

| ลำดับ | สิ่งที่ตัด | ผลตอบแทนโดยประมาณ (resend-weighted, 1 วันนี้ 1 โปรเจกต์) | ความยาก |
|---|---|---:|---|
| 1 | **root CLAUDE.md (เอกสาร Lead) ที่ทุก non-Lead pane โหลดเต็มทั้งไฟล์** — พบใหม่นอกเหนือ 4 ข้อใน #267 | ~12.5M tok-เทียบเท่า/วัน (14 non-Lead spawn × 6,728 tok × ~133 เทิร์น/session) | สูง — ต้องคิด mechanism ใหม่ เพราะ Claude Code auto-load `CLAUDE.md` ตามธรรมชาติของ git worktree ไม่ใช่จุดที่ cockpit inject ได้ตรงๆ |
| 2 | **#267 ข้อ 3: เขียนกฎเป็นอังกฤษ** | ~7.0M tok-เทียบเท่า/วัน (ประมาณ, ลด role-file ~50%) | สูง — เสี่ยง nuance หายตอนแปล ต้อง review |
| 3 | **#267 ข้อ 1+2: ตัด/ทำ pull-on-demand หัวข้อ browser-guard ในบทบาทไม่มี MCP browser** | ~1.5M tok-เทียบเท่า/วัน (18 spawn × 612 tok × 133 เทิร์น) | ต่ำ — แก้ 1 constant + gate ตาม `role_mcp_allowlist()`, มี precedent แล้ว (`GRAFT_TOOL_CAVEATS` wave4) |
| 4 | **#267 ข้อ 4: ปิด MCP graft/context7 ที่ role ไม่ใช้** | **ไม่แนะนำตัด** — audit จริง 2026-08-06 วัดแล้วว่า graft net-positive (ceiling ประหยัด 14% vs ต้นทุน 0.56%) | — |
| 5 | housekeeping ไฟล์ `CLAUDE.spawn-*.md` ค้าง | **0 token** — เป็นเรื่อง disk ไม่ใช่ context | ต่ำ (ทำได้แต่ไม่ใช่ token win) |

รายละเอียดวิธีวัด + ตัวเลขดิบ ↓

---

## 0. แหล่งข้อมูลและ method

1. **Session JSONL จริง** — สแกนทุก dir ใต้ `~/.claude/projects/` ที่ชื่อมี `agent-takkub` และมีไฟล์ `.jsonl` แก้ไขวันที่ 2026-08-16 พบ **16 session** ที่เป็น Claude-provider pane: backend×5 (รวม 1 session cwd=repo root), lead×2, devops×2 (รวม session นี้เอง), frontend×7
   - **qa/codex/gemini/kimi/opencode วันนี้ไม่มี session ในรูปแบบนี้เลย** — role เหล่านี้รันผ่าน CLI ของ provider อื่น (codex CLI / gemini-agy / kimi / opencode) ซึ่งเก็บ log คนละ schema คนละที่ (`~/.codex/...` ฯลฯ) **วัดด้วยวิธีนี้ไม่ได้** — ต้องมีสคริปต์แยกต่อ provider ถ้าจะเทียบ token จริง (ไม่ทำในรอบนี้ time-boxed)
   - ระบุ role ต่อ session ด้วยการ `grep` หา `[ROLE: <x> developer]` หรือ absence ของ marker นั้น (ไม่มี = Lead) ไม่ใช่การเดาจาก path
2. **Token ของ text component คงที่** (role file, root CLAUDE.md, skills listing ฯลฯ) — tokenize จริงด้วย `tiktoken` (`cl100k_base`) เหมือนวิธีที่ `docs/qa-reports/2026-08-06-graft-token-economics.md` ใช้วัด graft schema (847 tok, ตัวเลขที่ผมอ้างอิงตรงมา ไม่วัดซ้ำ) — ติดตั้ง `tiktoken` ชั่วคราวใน `.venv` แล้ว uninstall หลังใช้เสร็จ (ตาม precedent เดียวกัน) **cl100k_base ไม่ใช่ tokenizer จริงของ Claude** — เป็นค่าประมาณอันดับความสำคัญถูกต้อง (order of magnitude) ตามที่ audit เดิมระบุไว้แล้ว ไม่ใช่ตัวเลขที่ผูกกับบิลจริงเป๊ะ
3. **สิ่งที่ประกาศไว้ล่วงหน้าว่าวัดไม่ได้**: Claude Code เอง (base system prompt + built-in tool JSON schema เช่น Read/Write/Edit/Bash/Grep/Glob/Task/WebFetch ฯลฯ) เป็น proprietary — ไม่ serialize เป็น text ใน JSONL เลย มีแต่ผลรวม `usage.cache_creation_input_tokens` ที่รวมทุกอย่างเข้าด้วยกัน แยกส่วนไม่ได้จากข้อมูลฝั่งเรา (audit `2026-07-24-token-audit.md` สรุปไว้แล้วเหมือนกันว่า "assigning exact token counts to those individual pieces would be fabricated" — ผมยึดหลักเดียวกัน ไม่เติมตัวเลขให้ครบ)

---

## 1. Pane-open cost — วัดจริงจาก 16 session วันนี้

| role | n | cold (cache_read=0) | warm | first-prompt เฉลี่ย (tok) | หมายเหตุ |
|---|---:|---:|---:|---:|---|
| backend | 5 | 2 | 3 | 55,404 | รวม 1 session cwd=repo root (ไม่ผ่าน worktree) |
| lead | 2 | 0 | 2 | 65,737 | ไม่มี `[ROLE:]` marker — Lead ไม่ได้รับ task-block prefix แบบทีม |
| devops | 2 | 0 | 2 | 64,407 | รวม session นี้เอง (ยังไม่จบตอนวัด) |
| frontend | 7 | 1 | 6 | 59,543 | |

**cold vs warm ชัดเจน**: session แรกของวัน/ของ wave จ่ายเต็มราคา (cache_creation = เกือบทั้งก้อน, cache_read = 0) — session ถัดไปที่ spawn ภายใน cache TTL (~1 ชม., ระบุใน system context ของ session นี้เอง) จะ hit `cache_read` ตรง **22,972 token คงที่ทุก role ที่สุ่มตรวจ** (backend/devops/frontend เท่ากันเป๊ะ) ส่วนที่เหลือ (36,000–42,000) เป็น cache_creation เฉพาะ role นั้น (role file + MCP schema + task text ที่ยังไม่เคยถูก cache มาก่อน)

**นัยสำคัญ**: 22,972 token คงที่นี้คือ prefix ที่ทุก role เหมือนกันเป๊ะ (ไม่ขึ้นกับ role file ที่ต่างกัน) — เดาได้อย่างมีเหตุผลว่าประกอบด้วย Claude Code base system prompt + built-in tool schema + skills catalog + hook block + `~/.claude/CLAUDE.md` + MEMORY.md (ผลรวมที่วัดตรงได้ของ 5 อย่างหลัง = 6,818 tok จาก §2 — เหลือ ~16,000 tok เป็นส่วนของ Claude Code เองที่วัดไม่ได้) **นี่เป็นการอนุมานจากลบเลข ไม่ใช่การอ่านค่าตรง — ทำเครื่องหมายเป็นประมาณ**

---

## 2. Breakdown ของก้อน token (แยกวัดตรง / ประมาณ / วัดไม่ได้)

### วัดตรง (tiktoken cl100k_base, ต่อ 1 spawn)

| component | chars | tok (cl100k) | ใช้กับใคร |
|---|---:|---:|---|
| root `CLAUDE.md` (เอกสาร "Dev Team Lead") | 14,179 | **6,728** | **ทุก pane ทุก role** (Claude Code auto-load ตามที่อยู่ของไฟล์ในทุก worktree — verify แล้วว่า byte-identical ทุก worktree) |
| role file `runtime/agents/qa/CLAUDE.md` | 23,393 | 11,504 | เฉพาะ qa |
| role file `runtime/agents/devops/CLAUDE.md` | 21,529 | 11,228 | เฉพาะ devops |
| role file `runtime/agents/backend/CLAUDE.md` | 20,964 | 10,120 | เฉพาะ backend |
| role file `runtime/agents/frontend/CLAUDE.md` | 20,929 | 10,158 | เฉพาะ frontend |
| role file `runtime/agents/critic/CLAUDE.md` | 21,637 | 9,544 | เฉพาะ critic |
| role file `runtime/agents/reviewer/CLAUDE.md` | 15,946 | 8,518 | เฉพาะ reviewer |
| role file `runtime/agents/maintainer/CLAUDE.md` | 11,044 | 5,287 | เฉพาะ maintainer |
| role file `runtime/agents/docs/CLAUDE.md` | 10,357 | 5,480 | เฉพาะ docs |
| role file `runtime/agents/codex/CLAUDE.md` | 9,775 | 5,524 | เฉพาะ codex-role-on-claude-substitute (ไม่ใช่ codex CLI ตัวจริง) |
| role file `runtime/agents/mobile/CLAUDE.md` | 8,604 | 4,558 | เฉพาะ mobile |
| role file `runtime/agents/gemini/CLAUDE.md` | 5,581 | 3,078 | เฉพาะ gemini-role-on-claude-substitute |
| role file `runtime/agents/designer/CLAUDE.md` | 9,289 | 5,181 | เฉพาะ designer |
| graft MCP `tools/list` schema (6 tools) | 3,436 | **847** | backend/frontend/mobile/devops/qa/reviewer/critic (`role_mcp_allowlist`) — **วัดจริงจาก server handshake, อ้างอิงจาก `docs/qa-reports/2026-08-06-graft-token-economics.md` ไม่วัดซ้ำ** |
| skills catalog (รายการ skill ที่ available) | 12,222 | 2,720 | ทุก pane (global, ไม่ขึ้นกับ role/project) |
| `using-superpowers` SessionStart hook block | 5,670 | 1,376 | ทุก pane (global) |
| deferred-tools reminder list | 464 | 109 | ทุก pane (global) |
| `~/.claude/CLAUDE.md` (graphify+RTK, user-level) | 838 | 235 | ทุก pane บนเครื่องนี้ (ไม่ใช่แค่ project นี้) |
| MEMORY.md (user auto-memory, ผูกกับ project dir) | 8,896 | 2,378 | ทุก pane ของ project นี้ (Claude Code native feature) |
| "Browser & เครื่องมือหนัก" guard block (1 instance) | 1,259 | 612 | มีอยู่ใน 7 role file (backend/codex/devops/frontend/qa/reviewer/critic) แต่ MCP browser จริงมีแค่ qa/critic/designer — **5 role ถือ block นี้ไว้เฉยๆ โดยไม่มี MCP ให้ guard** (pane_guard บล็อกที่ tool level อยู่แล้ว) |

**รวมส่วนที่วัดตรงได้และ "ไม่ควรอยู่ในมือ specialist role"**: root CLAUDE.md (6,728) + browser-guard ส่วนเกิน (612 × 5 role ที่ไม่มี MCP) = แกนหลักของสิ่งที่ #267 พูดถึง

### ประมาณ (ระบุวิธีคิด + error bar)

- **สัดส่วน Thai vs English token-efficiency**: role file ภาษาไทยล้วน (เช่น devops) ได้ 1.92 char/tok ส่วน `~/.claude/CLAUDE.md` (อังกฤษ+โค้ดเป็นหลัก) ได้ 3.57 char/tok — **English ประหยัดกว่า Thai ~1.7–1.9× ต่อความหมายเดียวกัน** (สอดคล้องกับสูตร `thai_chars/1.2 + other_chars/4` ที่ wave3/wave4 ใช้) ถ้าแปล role file เป็นอังกฤษล้วน (เก็บเฉพาะ output-format ไว้เป็นไทย) คาดว่าลดได้ **~45–55%** ของ token ต่อไฟล์ — **นี่คือค่าประมาณ ไม่ใช่การแปลจริงแล้ววัด** error bar กว้าง เพราะขึ้นกับว่าแปลแล้วยาวขึ้น/สั้นลงแค่ไหนจริง
- **prefix ~23K ที่ cache ใช้ร่วมกัน (§1) ประกอบด้วยอะไรบ้าง** — วัดตรงได้ 6,818 tok (skills+hook+deferred+global CLAUDE.md+MEMORY.md) เหลือ ~16,000 tok เป็นส่วนของ Claude Code base system prompt + built-in tool schema ที่**อนุมานจากการลบเลข ไม่ใช่วัดตรง**

### วัดไม่ได้เลยด้วยข้อมูลที่มี (บอกตรงๆ ไม่เติมให้ครบ)

- **Claude Code base system prompt เอง** — ไม่ serialize เป็น text ที่เข้าถึงได้จาก JSONL หรือจากภายใน session ใดๆ ต้องมีทีม Anthropic เปิดเผยเอง หรือวัดทางอ้อมด้วยการทำ session ว่างเปล่าสุดๆ เทียบกับ session ที่มี component ครบ (ไม่ทำในรอบนี้)
- **Built-in tool JSON schema** (Read/Write/Edit/Bash/Grep/Glob/Task/TodoWrite/WebFetch/WebSearch ฯลฯ) — เหตุผลเดียวกัน
- **playwright + chrome-devtools MCP schema** (สำหรับ qa/critic/designer) — วิธีวัดที่ถูกต้อง (spawn server จริงแล้ว handshake `tools/list` เหมือนที่ทำกับ graft ใน 2026-08-06) **แต่ policy ของ role นี้ (devops) ห้ามรัน/ติดตั้ง browser driver ไม่ว่าช่องทางไหน แม้จะเป็นแค่ handshake ไม่เปิด browser จริงก็ตาม** — ไม่ทำในรอบนี้ ต้องให้ qa (เจ้าของสิทธิ์ MCP นั้น) รันสคริปต์แบบเดียวกับ `mcp_probe.mjs` เอง จึงจะได้ตัวเลขจริง — จากข้อมูล ecosystem ทั่วไป Playwright MCP + chrome-devtools MCP มักมี tool count มากกว่า graft (6 tools) หลายเท่า **แต่ไม่มีตัวเลขที่พิสูจน์ได้ในมือตอนนี้ ไม่เดา**
- **การแยก cache_creation ของ role file ออกจาก task-text ที่แนบมาด้วยในเทิร์นแรก** — JSONL มีแค่ผลรวม ไม่มีจุดคั่นให้แยก (audit 2026-07-24 สรุปไว้แล้วเหมือนกัน — "assigning exact token counts to those individual pieces would be fabricated")

---

## 3. สัดส่วน pane-open ต่อ total session tokens

| Scope | sum(first-prompt) | sum(session total) | share |
|---|---:|---:|---:|
| **วันนี้ 16 session (agent-takkub เท่านั้น)** | 954,109 | 478,698,804 | **0.199%** |
| 7-day rolling ทั้งเครื่อง (2026-07-24, ก่อน wave3/4 diet) | 18,454,763 | 4,321,402,081 | 0.4271% |

ตัวเลขวันนี้ต่ำกว่า 7 วันก่อน ~2 เท่า — สอดคล้องทิศทางกับ wave3/wave4 ที่ตัด role file ไปแล้วบางส่วน (ยังไม่ใช่หลักฐานเชิงสาเหตุ เพราะ sample คนละขนาด คนละช่วงเวลา คนละ mix ของ session ยาว/สั้น — **บอกทิศทางได้ ไม่ควรอ้างเป็น "ลดลง X% เพราะ wave3/4"**)

**อ่านตัวเลขนี้ให้ถูก**: ตัวหารคือ**ทั้ง session** รวมทุกเทิร์น การที่ pane-open share ต่ำมาก **ไม่ได้แปลว่าตัดไม่คุ้ม** — เพราะ prompt เดิมถูกจ่ายซ้ำเป็น `cache_read` ทุกเทิร์นที่เหลือของ session (กลไกเดียวกับที่ `docs/qa-reports/2026-08-06-graft-token-economics.md` §3 พิสูจน์ไว้กับ MCP schema) — นี่คือเหตุผลที่ §4 ใช้ resend-weighted ไม่ใช่ first-render share

---

## 4. โปรเจกชันผลตอบแทนจากการตัดตาม #267 (resend-weighted)

สูตร: `spawn_frequency (วันนี้, project นี้) × token_saved_per_spawn × mean_turns_per_session`
`mean_turns_per_session` ใช้ 133.4 (ค่าเฉลี่ยจาก sample 31 session ของ `2026-08-06-graft-token-economics.md` — ไม่ได้คำนวณใหม่ในรอบนี้ ใช้ค่าที่มีอยู่แล้วเพื่อความสอดคล้อง) **นี่คือ ceiling แบบเดียวกับที่ audit เดิมเตือนไว้ — สมมติว่า content นั้นถูก resend ทุกเทิร์นจนจบ session จริง (ไม่หัก compaction) เป็นค่าสูงสุดทางทฤษฎี ไม่ใช่คำมั่นสัญญา**

| ลำดับ | รายการ | token/spawn ที่กู้คืนได้ | spawn วันนี้ (project นี้) | resend-weighted ceiling | ความเห็น |
|---|---|---:|---:|---:|---|
| 1 | ตัด root CLAUDE.md เนื้อหา Lead-only ออกจาก non-Lead pane | 6,728 | 14 (backend5+devops2+frontend7, ไม่รวม lead) | **~12.5M** | ใหญ่สุดแต่แก้ยากสุด — Claude Code auto-load `CLAUDE.md` จาก cwd tree เอง ไม่ใช่จุดที่ spawn_engine.py inject ตรงๆ ต้องคิด mechanism ใหม่ (เช่น per-role symlink/ไฟล์แยก หรือทำให้ root CLAUDE.md สั้นลงเหลือแค่สิ่งที่จำเป็นสำหรับทุก role + ให้ Lead-only เนื้อหาย้ายไปที่อื่นที่ specialist ไม่โดน auto-load) — **ไม่อยู่ใน 4 ข้อของ #267 เดิม เป็น finding ใหม่จากรอบวัดนี้** |
| 2 | #267 ข้อ 3 — เขียนกฎอังกฤษ | ~3,750 (ประมาณ, เฉลี่ย ~50% ของ role file 7,500 tok) | 14 | **~7.0M** (ประมาณ) | effort สูง เสี่ยง nuance หาย ต้อง review ทุกไฟล์หลัง rewrite |
| 3 | #267 ข้อ 1+2 — ตัด/ทำ pull-on-demand browser-guard block ใน role ไม่มี MCP browser | 612 (เหลือ pointer ~20, ประหยัดสุทธิ ~592) | 18 (backend5+frontend7+devops2, ไม่รวม codex เพราะวันนี้รันบน codex CLI ไม่ใช่ claude) | **~1.4M** | effort ต่ำสุด — มี precedent ชัดจาก wave4 (`GRAFT_TOOL_CAVEATS` gate ผ่าน `role_mcp_allowlist()` แล้ว) ทำแบบเดียวกันกับ block นี้ได้เลย |
| 4 | #267 ข้อ 4 — ปิด MCP schema ที่ role ไม่ใช้ | 847 (graft) | ตัวเลขนี้ **ไม่ควรตัด** | — | audit 2026-08-06 วัดแล้วว่า net-positive ชัดเจน (ceiling ประหยัด 14% ของ Read/Grep/Glob token vs ต้นทุนคงที่แค่ 0.56%) — การตัดจะเสียมากกว่าได้ **สวนทางกับสมมติฐานตั้งต้นของ #267 ข้อนี้** — เขียนไว้ตรงๆ ให้ Lead เห็น ไม่ implement |
| 5 | #267 ข้อ 5 — housekeeping ไฟล์ `CLAUDE.spawn-*.md` ค้าง | 0 | — | 0 tok | ไม่กิน context (แค่ disk 876KB) — ทำได้แต่ไม่ใช่ token win อย่าจัดลำดับสูงเพราะเป็น token task |

**หมายเหตุสำคัญ**: อันดับ 1 กับ 2 มีขนาด ceiling ใหญ่กว่าอันดับ 3 มาก แต่ effort สูงกว่ามาก (อันดับ 1 ต้องคิด mechanism ใหม่ทั้งหมด, อันดับ 2 เสี่ยง regression เชิงความหมาย) — ถ้าจะเริ่มจากของที่ *ship ได้เร็วและมี precedent อยู่แล้ว* อันดับ 3 คือจุดเริ่มที่ปลอดภัยสุด ก่อนลงทุนกับ 1/2

---

## Verification

- ไม่แก้ source, ไม่ commit, ไม่ push — ตามคำสั่ง task (analysis-only)
- `tiktoken` ติดตั้งชั่วคราวใน `.venv` เพื่อวัด token component แล้ว uninstall ทันทีหลังใช้ (`pip uninstall -y tiktoken` — ยืนยันว่า "Successfully uninstalled") ไม่ทิ้งเป็น dependency
- ไม่รัน/ติดตั้ง playwright/chrome-devtools MCP driver ใดๆ (ตาม role policy) — เป็นเหตุผลที่ §2 ทิ้งช่องว่างนั้นไว้ตรงๆ แทนการเดา
- ตัวเลข graft MCP schema (847 tok) และ 31-session resend methodology **อ้างอิงจาก** `docs/qa-reports/2026-08-06-graft-token-economics.md` ไม่ได้วัดซ้ำ — cross-check แล้วว่าตัวเลขนั้นยังสอดคล้องกับ `_ROLE_MCP_POLICY` ปัจจุบัน (`shared_dev_tools.py:730-741`, อ่านสดวันนี้)
- ตัวเลข role-file ขนาดไบต์ตรงกับที่ issue #267 อ้างไว้เอง (qa 36.8KB, devops 36.2KB, frontend 33.5KB, backend 33.3KB) — cross-check ผ่าน `wc -c` สด ไม่ได้เชื่อตามตัวเลขใน issue เฉยๆ

## Not done / ส่งต่อ

- playwright + chrome-devtools MCP schema token count — ต้อง **qa** รัน `mcp_probe.mjs`-style handshake เอง (devops ทำไม่ได้ตาม role policy)
- แยก token cost ของ non-Claude provider (codex CLI / gemini-agy / kimi / opencode) — ต้องเขียนตัวแยกวิเคราะห์ log แยกต่างหากทีละ provider เพราะ schema ไม่ตรงกับ JSONL ของ Claude Code
- แปล role file เป็นอังกฤษจริง + วัด token หลังแปล (ตอนนี้เป็นค่าประมาณจากสัดส่วน char/token เท่านั้น)

---

## 5. Implementation (2026-08-16, worktree `wt/devops-1786850783`) — ตัดจริงตาม §4 อันดับ 1, ข้าม 3 พร้อมเหตุผล

**ทำ:** อันดับ 1 (root CLAUDE.md diet) เท่านั้น ตามคำสั่ง user (ห้ามแตะข้อ 2/แปลอังกฤษ, ห้ามแตะ MCP)

**ข้าม (ไม่ทำ) อันดับ 3 (browser-guard pull-on-demand) — พบ blocking risk ระหว่าง investigate จริง ไม่ใช่แค่ effort สูง:**

สมมติฐานเดิมใน §4 อันดับ 3 ที่ว่า "มี precedent ชัดจาก wave4 (`GRAFT_TOOL_CAVEATS`)" ตรวจแล้วไม่ตรงกับ risk profile ของ browser-guard เพราะ:

- `tests/test_agent_role_files_have_browser_guard.py` (docstring) ระบุตรงๆ ว่า role `.claude/agents/*.md` prose คือ **enforcement ชั้นเดียวที่ non-claude pane (codex/gemini-agy/opencode/kimi/cursor) เห็น** — `pane_guard.py`'s PreToolUse hook บล็อกจริงเฉพาะ **claude pane เท่านั้น**
- ตาม source ที่อ่านจริง (`spawn_engine.py:1702-1946`, `codex_agents_md.py`): non-claude teammate (ไม่ใช่ Lead) ได้แค่ `CODEX_AGENTS_MD` cheatsheet ทั่วไป + skill appendix เท่านั้น — **ไม่เคยอ่าน `.claude/agents/<role>.md` เลยในเส้นทางปัจจุบัน** ไม่ว่ากรณีไหน ต่างจาก `GRAFT_TOOL_CAVEATS` ที่เป็นแค่ "nice-to-have" caveat (ไม่ใช่ hard safety block) และไม่มี dedicated regression test ผูกกับ multi-provider ไว้แบบนี้
- สรุปได้ 2 อย่างที่ยังพิสูจน์ไม่ได้ 100% จากขอบเขตงานนี้: (a) test docstring อาจ stale จริง (ไม่ตรง behavior ปัจจุบัน) หรือ (b) มี pathway อื่นที่ยังไม่เจอที่ทำให้ non-claude เห็น prose นี้จริง — ทั้งสองกรณีคือคำถาม **multi-provider correctness** ไม่ใช่ token-diet ล้วนๆ แล้ว
- เอา token-saving มาแลกกับ "อาจถอด safety net เดียวที่ non-claude มีต่อ browser-driver bloat (เคยกิน cache ถึง 2.88GB/4 builds)" โดยไม่มั่นใจ 100% ว่า assumption ถูก = ไม่คุ้มความเสี่ยงสำหรับ solo devops ตัดสินใจเอง — ไม่ implement, รายงานเป็น finding ให้ Lead ตัดสินว่าจะส่งต่อใคร (แนะนำ: ต้องมีคนตรวจ context_strategy ของ opencode/cursor/gemini เต็มๆ ก่อน ไม่ใช่แค่ codex/kimi ที่ตรวจในรอบนี้ + อาจต้องเขียน test ใหม่แทนที่จะแก้ test เดิม)

**ไฟล์ที่แก้ (อันดับ 1 เท่านั้น):**
- `CLAUDE.md` (root) — เหลือเฉพาะกฎที่ทุก role ต้องรู้ (godfile-map pointer, multi-provider, cross-platform, test tiers) + pointer บังคับให้ Lead อ่าน `docs/lead/role-and-workflow.md`
- `docs/lead/role-and-workflow.md` (ใหม่) — ย้ายเนื้อหา Lead-only ทั้งหมดมาแบบ verbatim (บทบาท Lead, parallel dispatch, multi-project tabs, quick reference, vault, auto-routing table, proposal template, confirm handling, done-handoff, lead reply style, auto-fire exceptions, cockpit self-bug auto-issue, unavailable-provider substitution, spawn+assign, anti-patterns) — ไม่ลบกฎใดทิ้ง

**Before/after (วัดจริงด้วย tiktoken cl100k_base เหมือน §0-§2, ติดตั้ง/ถอน `tiktoken` ชั่วคราวรอบใหม่แล้วยืนยัน uninstall สำเร็จอีกครั้ง):**

| ไฟล์ | ก่อน (chars → tok) | หลัง (chars → tok) |
|---|---:|---:|
| `CLAUDE.md` (root, ทุก non-Lead pane โหลดเต็ม) | 14,179 → **6,728** | 2,080 → **948** |
| `docs/lead/role-and-workflow.md` (ใหม่ — pull-on-demand, Lead อ่านเองตอน spawn) | — (ไม่เคยมีไฟล์นี้) | 13,130 → **6,331** |

**ผลตอบแทนที่วัดได้จริง (ไม่ใช่ ceiling ประมาณ):** non-Lead pane ประหยัด **6,728 − 948 = 5,780 tok/spawn** — เอาไปคูณ resend-weighted เดิมของ §4 อันดับ 1 (14 spawn/วัน × 133.4 เทิร์นเฉลี่ย) ได้ **~10.8M tok-เทียบเท่า/วัน** (ต่ำกว่า ceiling เดิมที่ประมาณไว้ 12.5M เพราะไม่ได้ตัดทุกบรรทัด — เหลือ godfile-map/multi-provider/cross-platform/test-tiers ที่ universal จริง + เพิ่ม pointer บรรทัดใหม่)

Lead เองได้ประโยชน์เพิ่ม (นอกเหนือ 14 spawn ที่นับใน §4): `_build_lead_context_text()` (`lead_context.py:336-341`) อ่าน root `CLAUDE.md` เป็น `base` ของ system-prompt-file ตัวเองด้วย — ไฟล์เล็กลงจึงลด base ของ Lead เองไปด้วยเป็นผลพลอยได้ (ไม่ได้นับในตัวเลขข้างบนเพราะ §4 เดิมตั้งใจไม่รวม Lead)

**Verification:**
- `docs-verify`: `python -m agent_takkub.cli docs-verify` รันจาก worktree นี้ → 10 broken ref เดิม (ไม่เกี่ยวกับไฟล์ที่แก้ — `runtime/*.json` ที่ gitignore ไว้) **ไม่มี broken ref ใหม่จาก `CLAUDE.md` หรือ `docs/lead/role-and-workflow.md`**
- targeted tests เขียวหมด (ไม่รัน full suite): `test_lead_write_guard.py` `test_lead_context_compact.py` `test_lead_context_docs_lead_rewrite.py` `test_lead_project_rules.py` `test_lead_provider_unlock.py` `test_docs_verify.py` `test_project_rules.py` `test_agent_role_files_have_browser_guard.py` `test_pane_guard.py` — ผ่านทั้งหมด (รันผ่าน `PYTHONPATH=<repo>/src` ตาม conftest's #202 guard, ไม่แตะ shared venv editable install)
- โค้ดที่อ่าน root `CLAUDE.md` เชิงโปรแกรม (`lead_context.py`, `spawn_engine.py`, `project_rules.py`, `docs_verify.py`) ตรวจแล้วว่าไม่ parse เนื้อหาเฉพาะเจาะจงจากไฟล์ (แค่อ่านทั้งไฟล์เป็น base string + string-replace `docs/lead/` prefix สำหรับ installed build ซึ่งยังทำงานถูกกับ path ใหม่ที่เพิ่ม) — `orchestrator_text.py`/`codex_agents_md.py`/`custom_roles.py` ไม่ได้อ้างอิง `CLAUDE.md` เลย (grep ยืนยันแล้ว)
- แก้เพิ่มนอกขอบเขตเดิม (ตามคำสั่งแทรกจาก Lead): `tests/test_kimi_provider.py::TestKimiSpec::test_tui_markers_remain_an_explicit_gap` แดงเพราะ #257 (commit 7080ec9) calibrate `kimi_spec.ready_rules` และ `auth_error_markers` ของจริงแล้ว (deterministic, ไม่ใช่ flaky) — อัปเดต assertion ให้ตรง reality ใหม่ (`ready_rules` = calibrated marker, `auth_error_markers` = calibrated `"send /login to login"`) **ยังคง assert `ready_hard_blockers == ()`** (busy-marker gap ที่ยังไม่ calibrate จริง — เจตนาเดิมของเทสไม่หาย) เปลี่ยนชื่อเทสให้ตรงสิ่งที่มันกันจริง — `tests/test_kimi_provider.py` + `tests/test_pty_ready_prompt.py` เขียวหมด, `ruff check` ผ่าน

**ไม่ commit** — รอ Lead review diff บน branch `wt/devops-1786850783`
