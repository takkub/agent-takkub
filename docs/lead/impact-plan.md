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
    {
      "id": "assembly-native",
      "flow": "worker",
      "expected": "external TTS/Music calls = 0",
      "method": "fake provider + call ledger",
      "owner": "backend"
    }
  ],
  "not_applicable": {
    "ui": "ตรวจแยกในงาน frontend; worker นี้ไม่แสดง UI",
    "api": "ไม่มี API ที่เปลี่ยนในงาน worker นี้",
    "state": "ไม่มี state/queue ที่เปลี่ยนในงาน worker นี้",
    "qa": "ตรวจแยกในงาน QA ที่มี task ID ของตนเอง",
    "export": "ตรวจแยกในงาน export ที่มี task ID ของตนเอง",
    "resume": "ไม่มี resume path ที่เปลี่ยนในงาน worker นี้",
    "retry": "ไม่มี retry path ที่เปลี่ยนในงาน worker นี้",
    "regenerate": "ไม่มี regenerate path ที่เปลี่ยนในงาน worker นี้",
    "continuation": "ตรวจแยกในงาน episode continuation",
    "cost": "ไม่มีค่าใช้จ่ายที่เปลี่ยนในงาน worker นี้",
    "cache": "ไม่มี cache ที่เปลี่ยนในงาน worker นี้",
    "other_modes": "ตรวจ hybrid และ mute ในงาน QA แยก"
  }
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
