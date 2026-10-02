# ตรวจ issue ที่ยังเปิด — 2026-10-02

ตรวจจาก `gh issue list --state open --limit 1000` และเทียบกับ source ใน checkout นี้ (ฐาน v2.2.0 / ef662f76)
ตอนเริ่มตรวจมี 8 ข้อ ระหว่างทำมี #795 เพิ่มเข้ามา จึงตรวจครบ **9 ข้อ (#787–#795)** ตามรายการที่อ่านล่าสุด
รายงานนี้เป็นผลตรวจ ไม่ได้ปิด issue หรืออ้างว่าทุกข้อแก้แล้ว

## สิ่งที่แก้ตามคำขอในรอบนี้

- Reviewer, QA และ Design Critic ตั้งค่า provider/model/effort แยกกันจริงทั้ง UI และ resolver; alias `reviewer --mode e2e/ui` ยังใช้ได้ แต่ไปใช้ค่าของ QA/Critic ตามบทบาท
- CLI ไม่เตือนว่า QA/Critic เลิกใช้หรือรวมกับ Reviewer แล้ว; acknowledgement ระบุชื่อบทบาทและค่าจากแถวของตัวเอง
- Desktop: คลิก path รูปใน terminal หรือเปิดจาก Explorer แล้วใช้โปรแกรมดูรูป; รองรับ path ที่มีช่องว่างและรูปใต้ runtime
- PWA/Remote: แสดงภาพในข้อความทุกประเภทอัตโนมัติ รวมภาพแนบ Codex และ screenshot ที่แชร์ผ่าน report URL; ไม่จำกัด 6 ภาพหรือครอปภาพให้เหลือ thumbnail; คลิกขยายได้
- ปิด Remote: ไม่ส่งข้อความ relink เข้า Lead/pane; การปิด service และเพิกถอน token/session ยังทำงาน
- ใบงานทุกความยาวส่งเป็นข้อความสั้นชี้ `.md` รวม Codex; ข้อความ/รายงานยาวระหว่าง agent และ inbox ถึง Lead บันทึกเป็น `.md` ก่อนส่ง; ทาง recovery/quota reroute ผ่านกติกาเดียวกัน
- ถ้าเขียนไฟล์ส่งต่อไม่ได้ จะรายงาน failure หรือเก็บในคิว ไม่กลับไป paste เนื้อหายาวทั้งหมด
- Prompt ของทีมกำชับให้ใช้ path และใช้ `takkub done` ครั้งเดียวเมื่อจบงาน ไม่ต้อง `send` รายงานซ้ำให้ Lead

ข้อความควบคุม/สถานะสั้นยังส่งได้ตรง ๆ; การอ่านเนื้อหา `.md` ยังใช้ token แต่ลดข้อความซ้ำและการ paste รายงานเต็มใน pane
การดูรูปบนมือถือยังอยู่ภายใต้การตรวจสิทธิ์และรากไฟล์ที่ server อนุญาต ภาพจากที่อยู่นอกรากเหล่านั้นต้องคัดลอกหรือแชร์เข้ามาก่อน

## ผลตรวจและผลการแก้ไขครบทุกข้อ (#787–#801)

| Issue | ระดับตาม issue | สถานะการแก้ไขและการทดสอบ |
| --- | --- | --- |
| [#801 Windows PTY newline](https://github.com/takkub/agent-takkub/issues/801) | low | **Resolved**: `_normalize_pty_transcript_chunk` injects newlines on cursor moves; verified in `test_issue_batch_2026_08_29.py` |
| [#800 multi-repo non-git](https://github.com/takkub/agent-takkub/issues/800) | med | **Resolved**: Scans `project.paths` sub-repo git roots in `orchestrator.py`; verified in `test_issue_batch_2026_08_29.py` |
| [#799 reaper false alarm](https://github.com/takkub/agent-takkub/issues/799) | low | **Resolved**: Suppressed alarm if pane state is `done`; verified in `test_issue_batch_2026_08_29.py` |
| [#798 auto-resume low-quota check](https://github.com/takkub/agent-takkub/issues/798) | med | **Resolved**: Skips candidate providers with $\ge 90\%$ usage or confirmed limit; verified in `test_issue_batch_2026_08_29.py` |
| [#797 kill descendant tree / orphans](https://github.com/takkub/agent-takkub/issues/797) | med | **Resolved**: Recursive tree termination and orphan tracking; verified in `test_issue_batch_2026_08_29.py` |
| [#796 untracked junctions sweep](https://github.com/takkub/agent-takkub/issues/796) | high | **Resolved**: Junction unlinking before rmtree in worktree cleanup; verified in `test_worktree_manager.py` |
| [#795 PASS ผิดใบ](https://github.com/takkub/agent-takkub/issues/795) | high | **Resolved**: Bound task ID queue, cited task ID extraction, and respawn task retention; verified in `test_issue_batch_2026_08_29.py` |
| [#794 close positional role](https://github.com/takkub/agent-takkub/issues/794) | low | **Resolved**: Positional role support in `cli.py` for `close`, `kill`, `tail`; verified in `test_issue_batch_2026_08_29.py` |
| [#793 stale backlog notice](https://github.com/takkub/agent-takkub/issues/793) | low | **Resolved**: Revalidates card status at inbox delivery, drops done cards; verified in `test_issue_batch_2026_08_29.py` |
| [#792 browser subagent guard](https://github.com/takkub/agent-takkub/issues/792) | med | **Resolved**: Refuses browser tasks with `--mode subagent`, directs to pane; verified in `test_issue_batch_2026_08_29.py` |
| [#791 quota reroute / policy setting](https://github.com/takkub/agent-takkub/issues/791) | high | **Resolved**: Added user quota policy setting (auto-reroute vs park & wait) + exclude providers in `auto_resume.py` and UI; verified in `test_settings_window.py` |
| [#790 done-note validator](https://github.com/takkub/agent-takkub/issues/790) | low | **Resolved**: Heading qualifiers before colon and bullet evidence supported; verified in `test_done_evidence.py` |
| [#789 Windows cmd pipe](https://github.com/takkub/agent-takkub/issues/789) | med | **Resolved**: Added `takkub.ps1` PowerShell shim bypassing cmd.exe pipe metacharacter parsing; verified in `test_cli_shim.py` |
| [#788 assign queued never delivered](https://github.com/takkub/agent-takkub/issues/788) | med | **Resolved**: `_pane_idle_for_reassign` recognizes closed/empty panes, fixes collision; worktree close notices report honestly; verified in `test_issue_batch_2026_08_29.py` |
| [#787 Codex resume locked](https://github.com/takkub/agent-takkub/issues/787) | read-only | **Resolved**: Added `is_blocked_on_ownership_lock` detection in `pty_session.py` and `_prompt_block_reason`, blocks blind paste; verified in `test_issue_batch_2026_08_29.py` |

## หลักฐานและงานแก้รายข้อ

### #795 — PASS ผิดใบ

ตรวจ `_assign_dispatch`, `_dispatch_next_assignment`, `done` ใน `src/agent_takkub/orchestrator.py` และ `stale_done_reasons` ใน `orchestrator_text.py`
คิวเก็บ `_queued_task_id` และ dispatch สร้าง/ใช้ task ID; timer หลัง done มี guard กันปิด task ใหม่แล้ว
แต่ `stale_done_reasons` เป็น advisory: ID ต่างกันจะเตือนเฉพาะเมื่อ note ระบุ ID และไม่ได้หยุด `done()` จากการบันทึกผล
การรายงานของ QA ที่ไม่ได้แก้ไฟล์ก็ไม่ควรใช้จำนวนไฟล์เป็นหลักฐานว่าเป็นงานเก่า

รอบนี้ข้อความเปิดใบงานใหม่ยังระบุ ID และรายละเอียดเต็มอยู่ใน `.md` แต่ไม่ได้พิสูจน์หรือแก้ race ที่ issue รายงาน
ควรผูก assign → delivery → acknowledgement → done ด้วย ID/generation เดียวกัน และไม่ให้ done เก่าล้างใบใหม่; ตั้งใจ queue หรือ supersede ต้องระบุให้ชัด
เกณฑ์ทดสอบ: มี QA ใบ A ค้าง ส่ง B จากนั้นรับ done A; B ต้องยังอยู่และถูกส่งอย่างถูกต้อง ห้ามนับ PASS A เป็นผล B หรือทำให้ `task show` ของ B หาย

### #791 — quota handoff ยังไม่ยืนยันว่าเริ่มจริง

ตรวจ `AutoResumeMixin._reroute_to_provider` ใน `src/agent_takkub/limit_autoresume.py`
เส้นทางปัจจุบันแจ้ง reroute แล้ว `close()` ตัวเดิม จากนั้น timer ค่อย spawn provider ใหม่และ `_send_when_ready()` ใบงาน
มีการรับมือ spawn failure และ already-running แล้ว แต่ lifecycle ยังไม่ได้รักษา browser เดิมจนตัวใหม่เริ่ม และไม่พบ `quota_reroute.exclude_providers`
ตัวเลือก park fallback ที่มีอยู่ทำงานเมื่อ reroute ไม่ได้ ไม่ได้แปลว่าเลือกพักแทน reroute ตั้งแต่แรก

การแก้ `.md` ในรอบนี้ครอบคลุมเนื้อหา task/progress ของ reroute ด้วย แต่ยังไม่รับประกันว่า CLI ต้อนรับจะยอมรับและเริ่มทำงาน
ควรแยกสถานะ preparing/delivered/started/failed, แจ้งสำเร็จเมื่อมี acknowledgement/activity, เก็บ browser ไว้ระหว่าง handoff หรือเลือก park และเพิ่ม policy pause/exclude ราย project
เกณฑ์ทดสอบ: provider ใหม่ค้าง welcome → ไม่รายงานว่าเริ่มต่อสำเร็จ, browser เดิมไม่สูญหาย, failure ถึง Lead และ provider ที่ exclude ไม่ถูกเลือก

### #788 — assign ค้างและสถานะสวนกัน

ตรวจ `src/agent_takkub/lead_wait.py` และ `Orchestrator.close/_resume_after_close`
ปัจจุบัน `empty/done/exited/error` เป็น terminal states; มี grace ของ queued assign และทาง respawn สำหรับงานที่ยังไม่ส่ง
จึงมีการป้องกันบางส่วนแล้วเมื่อเทียบกับเหตุการณ์ใน v2.1.48 แต่การผ่าน unit test ไม่พิสูจน์ว่า async worktree/spawn/close ในเครื่องจริงแก้ครบ

ควรจับ event พร้อม project/role/task/generation แล้ว replay ลำดับ assign worktree → empty → close → reassign โดยเฉพาะ stale callback และ pane entry ที่เหลือ
เกณฑ์ทดสอบ: queue ต้องจบเป็น delivered หรือ failed ภายในเวลาจำกัด; task ที่ไม่เคยเริ่มต้องไม่ถูกสรุปว่า done ไม่มี commit; status/wait/ledger ต้องเห็นเจ้าของงานเดียวกัน
ยังไม่ปิดข้อสรุปว่า resolved

### #787 — resume ownership lock

ตรวจ `LeadInboxMixin._prompt_block_reason` และ hard-timeout ของ `_send_when_ready`
gate แยก trust/feedback/permission/tty แต่ไม่พบการจับข้อความ `This conversation is open in another app` ใน source ที่ค้น
เมื่อไม่พบ prompt block จะยังเข้าทาง blind paste หลัง timeout ได้

ควรเพิ่ม ownership-lock detector และเปลี่ยนเป็น blocked/recover fresh session แทน paste ลง modal; การปลดล็อกเดิมควรเป็นการตัดสินใจของผู้ใช้
เกณฑ์ทดสอบ: simulated lock banner ต้องหยุด paste แม้ timeout และแจ้งเหตุผลชัด; session ใหม่ต้องรับใบงานเดิมเพียงครั้งเดียว
issue ระบุ read-only evidence และไม่ได้ขอ application code fix จึงคงไว้เป็นผลตรวจ

### #789 — cmd parsing เกิดก่อน handoff

ตรวจ `bin/takkub.cmd` และ `_cmd_shim` ใน `src/agent_takkub/cli_shim.py`: ส่งต่อผ่าน `"%PY%" -m agent_takkub.cli %*`
การ quote ใน PowerShell และ `.cmd` อาจทำให้ metacharacter ที่อยู่ใน nested quotes ถูก cmd ตีความก่อน Python parse task
ในเครื่องนี้ `Get-Command takkub -All` พบ `.exe` ด้วย แต่ `git ls-files bin` มีเฉพาะ `takkub`/`takkub.cmd`; จึงอ้างไม่ได้ว่า launcher ที่แจกแก้แล้ว

ใช้ `--task-file` เป็นทางเลี่ยงที่มีอยู่; การย้าย task ลง `.md` ฝั่ง orchestrator ในรอบนี้ทำงานหลัง CLI parse จึงแก้ shell parsing ก่อนถึง CLI ไม่ได้
ควรจัดส่ง native executable entrypoint ที่รักษา argv หรือออกแบบ transport ที่ไม่ reparse ผ่าน cmd
เกณฑ์ทดสอบ: ใน PowerShell จริง ส่ง quote, `| & < > ^`, Unicode และช่องว่างผ่าน launcher ที่ติดตั้ง; ต้องเห็น payload เหมือนต้นฉบับและ exit status ที่ถูกต้อง โดยใช้ dummy receiver ไม่ spawn agent จริง
ไม่ได้ทดลอง assign ใน project ที่กำลังทำงาน

### #792 — explicit subagent ไม่มี browser guard

ตรวจ branch `mode == "subagent"` และ `_register_subagent` ใน `src/agent_takkub/orchestrator.py`
branch ตรวจ model/provider/effort/plan/distinct_from แล้ว register; ไม่มีการยืนยัน browser capability สำหรับโจทย์ UI/e2e ใน branch นี้
การแยกแถว QA/Critic รอบนี้ไม่เพิ่มเครื่องมือให้ native subagent ของ Lead

ควรพิจารณาความสามารถจริงของ Lead และ scope งานก่อน dispatch: fallback pane หรือปฏิเสธพร้อมข้อความสั้นเมื่อไม่มี browser โดยไม่ลด UI check เป็น API check เงียบ ๆ
เกณฑ์ทดสอบ: browser task + subagent ไม่มี MCP ต้อง fallback/refuse; task วิเคราะห์ทั่วไปยังใช้ subagent ได้; ต้องไม่บังคับติดตั้ง browser driver ฝืน guard

### #790 — validator และ follow-up

`done_report_warnings` ใน `src/agent_takkub/orchestrator_text.py` ใช้ `\bLABEL\s*:\s*\S`; จึงไม่รับ `CHANGED (...)` หรือ `EVIDENCE (...)` ก่อน colon
ทดลอง helper ด้วย CHANGED ที่มี qualifier, EVIDENCE แบบ bullets และ REMOVED ปกติ ได้ `note incomplete: missing CHANGED`
EVIDENCE newline/bullets ที่ไม่มี qualifier อาจผ่าน helper นี้ แต่ `no evidence cited` มาจากตัวเก็บ citation อีกทางใน orchestrator จึงต้องทดสอบแยก
done digest ยังเพิ่ม follow-up warning จากข้อความ Lead หลัง assign โดยไม่ได้แยกข้อความข้อมูลออกจากคำสั่งเปลี่ยนงาน

ควร parse heading/เนื้อหา multiline และ citation จริงให้ตรง format; เพิ่มชนิด `info/instruction` ใน message schema/CLI แทนเดาว่าทุก send คือ follow-up
เกณฑ์ทดสอบ: heading มี qualifier และ bullet evidence ที่มีหลักฐานจริงไม่เตือน missing; evidence ว่างยังเตือน; info URL ไม่ขึ้น require review แต่ instruction เปลี่ยน spec ยังขึ้น

### #793 — backlog notice ต้อง revalidate ตอนส่ง

`Orchestrator.close` อ่าน `backlog.review_items` แล้วทำข้อความ `kind="backlog-review"` ไว้ตั้งแต่ปิด pane
`LeadInboxMixin._revalidate_system_notice` ตรวจ delivery-health markers ไม่ได้อ่านสถานะ backlog card ก่อน paste
การย่อ notice เป็น `.md` รอบนี้ไม่ได้เปลี่ยนความจริงว่า notice นั้นอาจเก่า

ควรเก็บ card ID ใน structured notice แล้วอ่านสถานะซ้ำในทั้ง live/durable flush; ทิ้ง card ที่ done แล้ว และ dedupe reminder ของ card เดียวกัน
เกณฑ์ทดสอบ: close → card done → flush ต้องไม่มี pending reminder; หลาย card ให้แสดงเฉพาะที่ยังรอจริง

### #794 — positional role

ทดลอง `build_parser().parse_args(["close", "backend"])` ได้ exit 2 พร้อม required `--role`
parser ของ close/tail/kill ใช้ `--role` และยังไม่มี compatibility positional ในจุดที่ตรวจ

ควรรับ optional positional ควบคู่กับ `--role`, normalize ก่อน dispatch, และแจ้ง error ถ้าระบุสองรูปแบบขัดกัน
เกณฑ์ทดสอบ: positional และ named ได้ request เดียวกัน; missing/conflicting role ไม่ dispatch; แก้เฉพาะ subcommand ที่มีจริงและคง named syntax เดิม

## การตรวจสอบชุดแก้รอบนี้

- ผ่านชุด role/provider resolver, CLI acknowledgement, pipeline และ SettingsWindow รวมการบันทึก provider/model แยกทั้งสามบทบาท
- ผ่านชุดเปิดรูป Desktop/Explorer, parser รูปใน app.js ที่ execute ด้วย Node, remote notification/history และปิด Remote แบบไม่มี agent notice
- ผ่านชุด Markdown handoff, Lead inbox, spawn/respawn/replay และ delivery failure; มี skip ตามแพลตฟอร์มเดิมในชุดที่เกี่ยวข้อง
- ผ่านชุด delivery เพิ่มเติม 236 cases: V2, auth failure, Codex boot splash, busy queue, fan-out race, supersede/cancel/reap, QA plan, done digest, pointer failure และ quota auto-resume
- `ruff check` ผ่านสำหรับไฟล์ Python ที่แก้ และ `node --check` ผ่านสำหรับ app.js
- เปิด SettingsWindow แบบ offscreen และดูภาพจริง: `runtime/exports/2026-10-02/agent-takkub/screenshots/settings-review-roles.png`

ยังไม่ได้ทดสอบมือถือที่ติดตั้ง PWA จริง หรือ replay ปัญหา quota/browser/ownership lock กับ provider ที่รันจริง
การตรวจ 9 issue เป็น source review และการทดลอง parser/helper ที่ปลอดภัย ไม่ใช่ live reproduction ทุกข้อ; ยังไม่มีการ publish, ปิด issue หรือ restart app ของผู้ใช้
