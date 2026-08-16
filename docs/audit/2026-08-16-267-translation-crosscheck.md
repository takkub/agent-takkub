# #267 translation cross-check

วันที่ตรวจ: 2026-08-16  
commit ที่ตรวจ: `1c4ce3a` (`perf(#267): แปลกฎใน role file 16 ใบเป็นอังกฤษ`) เทียบกับ `HEAD~1` (`3a44927`)

## สรุป

- **Critical:** ไม่พบ — ไม่พบกฎหาย ความหมายห้าม/ต้องกลับด้าน หรือตัวเลขเกณฑ์ผิด
- **Major:** พบ 4 กลุ่ม
  1. role ทั้ง 16 ใบมีอย่างน้อยหนึ่ง output example/template ที่ควรคงเป็นไทยแต่ถูกแปลเป็นอังกฤษ
  2. `codex.md:8` แปลแล้วประโยคขาดกรรม ทำให้ instruction คลุมเครือขึ้น
  3. `devops.md:111` เพิ่มเงื่อนไขที่ต้นฉบับไม่มี และทำให้ชนกับคำสั่งที่ `:113`
  4. implementation note ที่ `docs/audit/2026-08-16-token-cost-measurement.md:232` อ้างว่าเก็บ literal `takkub done`/`takkub send` ทุกอันเป็นไทย แต่ diff จริงขัดกับข้อความนี้
- **Minor:** ไม่พบประเด็นที่ควรแยกเป็น minor; ไม่แต่งสำนวนเล็กน้อยให้เป็น finding
- command/flag/path/issue-number invariants ผ่าน: เทียบ multiset ต่อไฟล์แล้ว `takkub`, git/browser/process commands, `--...` flags, `$TAKKUB_...` variables, repo paths และ `#...` references ไม่มีรายการหายหรือเพิ่มจากการแปล
- guard tests 4 ใบรันผ่านด้วย `PYTHONPATH=<worktree>/src`; การเปลี่ยน assertion ยังรักษาเจตนาเดิม (รายละเอียดท้ายรายงาน)

## ตารางต่อไฟล์

| ไฟล์ | ผล | บรรทัดและต้นฉบับไทย → คำแปลที่มีประเด็น |
|---|---|---|
| `.claude/agents/analyst.md` | **มีประเด็น (major)** | `:13` `takkub done "<note สรุปงาน>"` → `takkub done "<summary note>"`; `:105` `"blocked: <ระบุปัญหา + ที่อยากให้ Lead ช่วย>"` → `"blocked: <state the problem + what you'd like Lead's help with>"` |
| `.claude/agents/backend.md` | **มีประเด็น (major)** | `:12` `"<note สรุปงาน>"` → `"<summary note>"`; `:115` blocked-message placeholder ภาษาไทย → อังกฤษ |
| `.claude/agents/codex.md` | **มีประเด็น (major)** | `:66` `"<note สรุป>"` → `"<summary note>"`; `:8` `เทียบ diff กับ implementation role` → `compare the diff against the implementation role's` (possessive ค้าง ไม่มีสิ่งที่ให้เทียบ) |
| `.claude/agents/critic.md` | **มีประเด็น (major)** | `:12` `"<note สรุปงาน + path ของ proposal.md>"` → `"<summary note + path to proposal.md>"`; `:217` blocked-message placeholder ภาษาไทย → อังกฤษ (concrete messages และ generated review template ช่วง `:110-171` ยังเป็นไทยถูกต้อง) |
| `.claude/agents/cursor.md` | **มีประเด็น (major)** | `:64` `takkub done "<note สรุป>"` → `takkub done "<summary note>"` |
| `.claude/agents/designer.md` | **มีประเด็น (major)** | `:12` `"<note สรุปงาน>"` → `"<summary note>"`; `:96` blocked-message placeholder ภาษาไทย → อังกฤษ |
| `.claude/agents/devops.md` | **มีประเด็น (major)** | `:12` `"<note สรุปงาน>"` → `"<summary note>"`; `:146` blocked-message placeholder ภาษาไทย → อังกฤษ; `:111` `ถ้า compose ไม่ parametrize port → เขียน ... override ... ชั่วคราว` → `if compose hardcodes ports and you can't fix it quickly → write ... override` (เพิ่ม `can't fix it quickly`) |
| `.claude/agents/docs.md` | **มีประเด็น (major)** | `:13` `"<note สรุปงาน>"` → `"<summary note>"`; `:118` blocked-message placeholder ภาษาไทย → อังกฤษ |
| `.claude/agents/frontend.md` | **มีประเด็น (major)** | `:12` `"<note สรุปงาน>"` → `"<summary note>"`; `:118` blocked-message placeholder ภาษาไทย → อังกฤษ |
| `.claude/agents/gemini.md` | **มีประเด็น (major)** | `:66` `takkub done "<note สรุป>"` → `takkub done "<summary note>"` (วันที่ `18 มิ.ย. 2026` → `2026-06-18` ยังหมายถึงวันเดียวกัน) |
| `.claude/agents/kimi.md` | **มีประเด็น (major)** | `:65` `takkub done "<note สรุป>"` → `takkub done "<summary note>"` |
| `.claude/agents/mobile.md` | **มีประเด็น (major)** | `:12` `"<note สรุปงาน>"` → `"<summary note>"`; `:121` blocked-message placeholder ภาษาไทย → อังกฤษ |
| `.claude/agents/opencode.md` | **มีประเด็น (major)** | `:64` `takkub done "<note สรุป>"` → `takkub done "<summary note>"` |
| `.claude/agents/qa.md` | **มีประเด็น (major)** | `:86` output template `คะแนน → task → ผล → worked → ...` → `score → task → result → what worked → ...`; `:114` literal shell output `เล็กผิดปกติ — ถ่ายพลาด, ถ่ายใหม่` → `abnormally small — capture failed, re-shoot`; `:120` blocked placeholder `<ปัญหา + ที่อยากให้ช่วย>` → `<problem + what you'd like help with>` |
| `.claude/agents/reviewer.md` | **มีประเด็น (major)** | `:12` `"<note สรุปงาน>"` → `"<summary note>"`; `:121` blocked-message placeholder ภาษาไทย → อังกฤษ |
| `.claude/agents/security.md` | **มีประเด็น (major)** | `:13` `"<note สรุปงาน>"` → `"<summary note>"`; `:119` blocked-message placeholder ภาษาไทย → อังกฤษ |

ทุกไฟล์ในตารางถูกอ่านละเอียดจริง โดยเทียบ source ไทยจาก `git show HEAD~1:<path>` กับ source อังกฤษที่ `HEAD` ทีละบรรทัด ไม่ใช่ spot-check: `analyst`, `backend`, `codex`, `critic`, `cursor`, `designer`, `devops`, `docs`, `frontend`, `gemini`, `kimi`, `mobile`, `opencode`, `qa`, `reviewer`, `security`.

## Findings แยกตามความรุนแรง

### Critical

ไม่พบกฎหายหรือความหมายกลับด้าน การใช้ `Never` / `must` / `only` / `unless` ในกฎบังคับและข้อยกเว้นหลักยังตรงกับต้นฉบับ และตัวเลขสำคัญ เช่น 2.5 seconds, 2.88 GB / 4 builds, 10 KB, 40-100 KB, port/PID/shard thresholds ยังเท่าเดิม

### Major 1 — output examples/templates ถูกแปล ทั้งที่ต้องคงเป็นไทย

พบ inline command string ที่ถูกแปล 26 จุดใน 16 role:

- standard role 10 ใบเปลี่ยน `takkub done "<note สรุปงาน>"` (critic มี `+ path ของ proposal.md`) เป็น English placeholder และเปลี่ยน blocked-message placeholder อีกใบละหนึ่งจุด
- substitute-provider role 5 ใบ (`codex`, `cursor`, `gemini`, `kimi`, `opencode`) เปลี่ยน `"<note สรุป>"` เป็น `"<summary note>"`
- `qa.md:120` เปลี่ยน blocked-message placeholder เป็นอังกฤษ

นอกจาก inline strings ข้างต้น `qa.md:86` ยังแปล schema ของ done report ซึ่งเป็น output template โดยตรง และ `qa.md:114` แปล literal warning ที่ shell ต้องพิมพ์ออกจอ จึงไม่ใช่เพียง model-facing prose

ส่วนที่คงเป็นไทยถูกต้องและตรวจแล้ว ได้แก่ concrete `takkub done`/`takkub send` examples ส่วนใหญ่, critic→gemini message body และ critic generated design-review template แต่การคงไว้บางส่วนไม่ได้ชดเชยจุดที่แปลผิดขอบเขตด้านบน

### Major 2 — `codex.md:8` instruction คลุมเครือขึ้น

- ต้นฉบับ: `เทียบ diff กับ implementation role`
- แปล: `compare the diff against the implementation role's`

คำว่า `role's` เป็น possessive ที่ไม่มี noun ตามหลัง จึงไม่ชัดว่าต้องเทียบกับ implementation, output, diff หรือ spec ของ role นั้น ควรเติม object ให้ครบโดยรักษาความหมายเดิม

### Major 3 — `devops.md:111` เพิ่มเงื่อนไขและชนกับ `:113`

- ต้นฉบับ `:111`: `ถ้า compose ไม่ parametrize port → เขียน docker-compose.override.yml ชั่วคราว (ports เท่านั้น)`
- แปล `:111`: `if compose hardcodes ports and you can't fix it quickly → write a temporary docker-compose.override.yml (ports only)`
- กฎ `:113` ทั้งก่อนและหลัง: ถ้า hardcode ports และแก้ไม่ได้เร็ว ให้ `send --to lead` ขอการตัดสินใจ

`and you can't fix it quickly` ถูกเพิ่มใน `:111` โดยไม่มีในบรรทัดเดิม ทำให้เงื่อนไขเดียวกับ `:113` สั่งทั้ง “เขียน override” และ “ถาม Lead” พร้อมกัน ความหมายจึงคลุมเครือขึ้น ควรเอาเงื่อนไขที่เพิ่มออกจาก `:111` หรือทำให้ลำดับตัดสินใจสองบรรทัดชัดเจนตามเจตนาต้นฉบับ

### Major 4 — implementation note อ้างไม่ตรง diff

`docs/audit/2026-08-16-token-cost-measurement.md:232` ระบุว่า:

> Every literal example string passed to `takkub done "..."` / `takkub send --to <role> "..."` ... [was left in Thai]

แต่ diff มี inline command strings ที่ถูกแปล 26 จุดตาม Major 1 จึงควรแก้ audit trail พร้อม role files ไม่เช่นนั้น reviewer รอบถัดไปจะได้รับหลักฐานที่สรุปตรงข้ามกับ source จริง

### Minor

ไม่พบ สำนวนอังกฤษอื่นมีจุดที่ไม่เป็นธรรมชาติบ้าง แต่ไม่แยกเป็น finding เมื่อไม่เปลี่ยนความหมายหรือการทำงาน

## Glossary consistency

คำหลักที่ใช้ซ้ำตรง glossary โดยรวม: `required`, `Never`, `Do instead:`, `Why:`, `Real incident`, `Scope`, `Workflow`, `Communication between agents`, `Reporting back when done`, `Blocked / need clarification`, `Roles you can send to`, และ `Bash commands you're allowed to use` ใช้สม่ำเสมอใน shared blocks

ข้อยกเว้นที่เป็น finding ไม่ใช่ศัพท์ไม่สม่ำเสมอ แต่เป็นการแปลสิ่งที่ glossary section เองบอกว่าต้องคงเป็นไทย

## Guard tests 4 ใบ

ผลรัน:

```text
$env:PYTHONPATH=(Resolve-Path 'src').Path
pytest -q tests/test_agent_role_files_have_browser_guard.py \
  tests/test_agent_role_files_have_git_guard.py \
  tests/test_agent_role_files_have_host_destructive_guard.py \
  tests/test_agent_role_files_have_pip_editable_guard.py

exit code 0 (100%; expected skips only)
```

semantic review:

- browser guard เปลี่ยน exact Thai literals เป็น exact English literals เท่านั้น; ยังตรวจ section, whole-drive prohibition, browser allow/deny split และ QA hand-off ครบ
- host-destructive และ pip-editable guards เปลี่ยน exact Thai prohibition เป็น exact English prohibition; ยังตรวจ dangerous commands และ safe PID/pytest alternative ครบ
- git guard เปลี่ยน `"NEVER" in content` เป็น `"never" in content.lower()` เพื่อรับ title-case `Never`; แม้รับ casing กว้างขึ้น แต่ไม่ได้ลดข้อกำหนดเชิงความหมายจากเดิม และ tests แยกยังบังคับให้มี `git commit`, `git push`, และ prohibition marker อยู่

สรุป: guard ทั้ง 4 ใบไม่ได้ถูกทำให้อ่อนลงเพื่อให้ผ่านในแง่เจตนาของ guard; ปัญหาที่พบอยู่ใน translated role content/output-language boundary ตาม findings ด้านบน
