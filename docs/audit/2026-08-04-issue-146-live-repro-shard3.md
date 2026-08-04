# #146 live repro — shard 3/3 (qa#3)

- **shard id**: 3
- **connect ได้ไหม**: ✅ ต่อได้ปกติ — `mcp__playwright__browser_navigate` ไป `about:blank` สำเร็จ
  - เวลาที่ใช้: ~7s (17:29:08 → 17:29:15, spawn พร้อมกับ shard อื่นอีก 2 ตัว)
  - error ดิบ: ไม่มี (ไม่มี error เกิดขึ้นเลย)
- **env ของ pane**:
  - `TAKKUB_SHARD=3`
  - `TAKKUB_SHARD_TOTAL=3`
  - `MCP_TOOL_TIMEOUT=180000`
- **config file ของ shard ตัวเอง**: ⚠️ **ไม่มีไฟล์ shard-specific**
  - หาไฟล์ `~/.agent-takkub/runtime/shared-mcp-agent-takkub-qa-shard3.json` → **ไม่พบ**
  - มีแค่ `~/.agent-takkub/runtime/shared-mcp-agent-takkub-qa.json` (ไฟล์เดียว ไม่มี shard suffix) ที่ทั้ง 3 shard panes (qa#1/#2/#3) น่าจะใช้ร่วมกัน — content ของไฟล์นี้ชี้ `--user-data-dir` เดียวกัน (`browser-profiles\agent-takkub-qa-playwright`) ให้ทั้ง playwright และ chrome-devtools MCP
  - เทียบกับโปรเจคอื่นในเครื่องเดียวกัน (เช่น `pms`, `wash-locker`, `unirecon`, `TK-ERP`, `oracle`) ซึ่งมีไฟล์แยกจริง `-qa-shard1.json` / `-qa-shard2.json` / `-qa-shard3.json` — agent-takkub เองไม่มีไฟล์ชุดนี้เลย

## ข้อสังเกต (ไม่ใช่ข้อสรุปสาเหตุ — แค่สิ่งที่เห็นจริง)
- shard นี้ **connect ได้ปกติ ไม่มีปัญหา** แม้ spawn พร้อมกับอีก 2 shard
- แต่ config file ของโปรเจคนี้ไม่มี per-shard isolation เหมือนโปรเจคอื่น — ถ้า config นี้ถูกใช้จริงโดยทั้ง 3 shard panes พร้อมกัน แปลว่าทั้ง 3 ตัวชี้ `--user-data-dir` เดียวกัน ซึ่งเป็นความต่างจาก config pattern ของโปรเจคอื่นที่เห็นในเครื่องเดียวกัน — ไม่ได้พิสูจน์ว่าเป็นสาเหตุของ #146 แต่เป็น evidence ที่ต่างจากที่ audit เดิมสันนิษฐาน (static trace บอกว่า argv/env/policy เหมือนกันทุกจุด — จุดนี้ไม่เหมือน)
