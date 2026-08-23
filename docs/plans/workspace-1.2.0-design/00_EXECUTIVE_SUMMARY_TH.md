# สรุปภาพรวม

หลังทำชุดนี้ Takkub จะมี workspace แบบ IDE-lite:

```text
┌────────────────┬──────────────────────────────────────────────────────┐
│ PROJECT FILES  │ Lead | Designer | Frontend | Backend | QA | Preview│
│ ▾ src          ├──────────────────────────────────────────────────────┤
│   ▾ app        │                                                      │
│     page.tsx M │          Agent / Monaco / Diff / Preview            │
│ ▾ components   │                                                      │
│                │                                                      │
│ CHANGES        │                                                      │
│ M page.tsx     │                                                      │
│ A Hero.tsx     │                                                      │
│           «    │                                                      │
└────────────────┴──────────────────────────────────────────────────────┘
```

ผู้ใช้ทำได้:
- เปิด/หุบ Explorer
- คลิกไฟล์เปิดใน Takkub
- แก้ด้วย Monaco + Ctrl+S
- ดู diff และ Git changes
- ถ้า Agent แก้ไฟล์เดียวกับที่เราเปิด จะเตือน conflict ไม่ overwrite เงียบ
- Preview localhost หรือ design HTML ใน Cockpit
- Designer publish design แล้ว Preview เด้งขึ้นทันที
- Approve/Revise ใน UI
- Save แล้ว Next/Vite HMR อัปเดต Preview

บทบาท:
- Graft = code structure
- Brain V2 = operational memory
- Conversation V2 = session/checkpoint
- Obsidian = curated human knowledge
- OpenViking = optional AI knowledge retrieval/index
- Capability Hub = skills/MCP/plugins/permissions
- Monaco = human file edit
- Preview = human visual feedback
