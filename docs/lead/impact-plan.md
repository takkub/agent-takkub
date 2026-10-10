# Impact plan และหลักฐานปิดงาน (#833)

ใช้กับงานที่เปลี่ยน mode, identity, account หรือ shared state และงานอื่นที่ Lead
เห็นว่ามีผลต่อหลายขั้นตอน ใส่ `[impact-required]` ใน task เพื่อบังคับใช้กับงานอื่นได้
การตรวจเป็นส่วนของงานที่ได้รับอนุญาตแล้ว ไม่ต้องเปิด approval prompt เพิ่ม

## ก่อน assign

เขียนไฟล์ JSON นอก repository ที่กำลังแก้ เช่นใน runtime ของ cockpit ระบุ:

```json
{
  "trigger": "audio_mode=seedance_only, มี dialogue",
  "before": "assembly เรียก external TTS",
  "after": "เสียง native จาก clip ไปถึง export ไม่มี external TTS/Music",
  "source_of_truth": "project.audio_mode",
  "upstream": ["project settings", "episode continuation"],
  "downstream": ["voice UI", "API", "assembly", "QA", "export"],
  "checks": [
    {"id":"voice-ui","flow":"ui","expected":"แสดงตัวละครและโหมดเสียงถูกต้อง","method":"เปิดหน้าเสียงหลายตัวละคร","owner":"frontend"},
    {"id":"voice-api","flow":"api","expected":"API เคารพ audio_mode","method":"เรียก fake API ของโหมดนี้","owner":"backend"},
    {"id":"mode-state","flow":"state","expected":"queue เก็บ audio_mode เดิม","method":"ตรวจ state/queue หลังสร้างงาน","owner":"backend"},
    {"id":"assembly-native","flow":"worker","expected":"external TTS/Music calls = 0","method":"fake provider + call ledger","owner":"backend"},
    {"id":"shot-qa","flow":"qa","expected":"raw clip QA คาดหวังเสียง native","method":"ตรวจ fake QA request","owner":"qa"},
    {"id":"export-audio","flow":"export","expected":"export มีเสียง native","method":"ตรวจไฟล์ export จริงจาก fake provider","owner":"qa"},
    {"id":"resume-mode","flow":"resume","expected":"resume ใช้ audio_mode เดิม","method":"หยุดแล้วทำต่อใน fixture","owner":"qa"},
    {"id":"retry-mode","flow":"retry","expected":"retry ไม่เรียก external TTS","method":"จำลอง failure แล้วตรวจ call ledger","owner":"qa"},
    {"id":"regenerate-mode","flow":"regenerate","expected":"regenerate ไม่เปลี่ยนโหมดเสียง","method":"สร้าง shot ซ้ำใน fixture","owner":"qa"},
    {"id":"episode-mode","flow":"continuation","expected":"ตอนต่อสืบทอด audio_mode และโมเดล","method":"สร้างตอนต่อแล้วอ่าน options","owner":"backend"},
    {"id":"mode-cost","flow":"cost","expected":"ไม่มีต้นทุน external TTS/Music","method":"ตรวจ cost plan","owner":"backend"},
    {"id":"cached-final","flow":"cache","expected":"final เก่าคนละโหมดไม่ถูกนำมาใช้","method":"เปลี่ยนโหมดแล้วตรวจ assembly signature","owner":"qa"},
    {"id":"hybrid-mute","flow":"other_modes","expected":"hybrid ยังมีเสียงแยก; mute export เงียบ","method":"รัน fake pipeline ทั้งสองโหมด","owner":"qa"}
  ]
}
```

ทุก flow ใน `ui, api, state, worker, qa, export, resume, retry, regenerate,
continuation, cost, cache, other_modes` ต้องมี check หรือเหตุผลที่ไม่เกี่ยวข้อง
ระบุ expected behavior, วิธีพิสูจน์ และ owner ในแต่ละ check; ใช้ `required:false`
เฉพาะการตรวจเสริม เช่น live generation ที่มีต้นทุน และต้องบันทึกข้อจำกัดหากยังไม่ทำ
ห้ามใช้ `not_applicable` เพื่อซ่อน flow ที่เปลี่ยนจริง

สั่งงานด้วย `takkub assign --role backend --cwd <repo> --impact-plan-file <plan.json> "<task>"`
ไฟล์ plan ถูกฝังใน task และเก็บใน ledger จึงอยู่ต่อเมื่อเปลี่ยน provider หรือ resume
หากแก้ scope ระหว่างงาน ให้ assign แผนฉบับใหม่; หลักฐานของแผนเก่าจะใช้ปิดไม่ได้

## หลังแก้และก่อน done

เทียบแผนกับ diff จริง เพิ่ม check เมื่อพบผลกระทบใหม่ แล้วตรวจตามความเสี่ยง
ใช้ mock/fake และ fixture ที่มีอยู่ก่อน live paid call; บันทึกจำนวน provider calls,
state/queue/ledger และไฟล์ export ตามที่เกี่ยวข้อง อย่าอ้างผล live หากยังไม่ได้รัน

`takkub task impact --role backend` แสดง `task_id`, `plan_digest`, `revision`
ปัจจุบัน สร้าง evidence JSON นอก repo ที่กำลังแก้เพื่อไม่ให้ไฟล์หลักฐานเปลี่ยน
revision ของตัวเอง:

ตัวอย่าง evidence ข้างล่างแสดงรูปแบบของหนึ่งรายการเท่านั้น แผนตัวอย่างข้างบน
จะผ่าน `done` ได้เมื่อมีผลตรวจครบทุก `id` ใน `checks` ตาม revision เดียวกัน

```json
{
  "task_id": "<จาก task impact>",
  "plan_digest": "<จาก task impact>",
  "revision": "<จาก task impact หลังแก้เสร็จ>",
  "checks": [
    {
      "id": "assembly-native",
      "status": "pass",
      "evidence": "fixture export path; fake call ledger TTS=0 Music=0",
      "kind": "fake"
    }
  ]
}
```

`kind` เป็น `fake`, `mock`, `manual` หรือ `live` ให้ตรงกับสิ่งที่ตรวจจริง
check เสริมที่ยังไม่ได้รันใช้ `status:"pending"` และ `limitation` ที่ชัดเจน
check บังคับที่ยัง pending ทำให้ done ไม่ผ่านและ task ยังเปิดอยู่

ส่ง `takkub done --impact-evidence-file <evidence.json> "<ผลลัพธ์>"`
หรือ `takkub subagent-done --role <role> --impact-evidence-file <evidence.json>`
ระบบตรวจ task ID, แผน, revision และผลทุก check ก่อนเปลี่ยน ledger เป็น `ok`
หาก Git revision อ่านไม่ได้ งานยังค้างให้ตรวจสภาพ repository และรายงานข้อจำกัด
