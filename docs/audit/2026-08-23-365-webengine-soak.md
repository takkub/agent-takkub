# #365 WebEngine soak — release gate 1.2.0 (รันจริงบน Windows 2026-08-23)

เกณฑ์ใน `docs/release-checklist.md` §0b ข้อ "WebEngine soak ผ่าน" — ต้องรันบนเครื่อง Windows จริง (real
`QWebEngineView`, ไม่ใช่ CI offscreen runner) · สคริปต์ `tools/soak_workspace_webengine.py` (#365 เฟส 10)

## คำสั่ง

```bash
AGENT_TAKKUB_QT_WEBENGINE_SMOKE=1 .venv/Scripts/python.exe -m pytest tests/test_workspace_webengine_soak.py -q   # 4 passed
.venv/Scripts/python.exe tools/soak_workspace_webengine.py --cycles 25 --projects 3 --json-out soak25.json     # exit 0
```

## ผล (25 cycles × 3 โปรเจกต์)

| ส่วน | ผล |
|---|---|
| editor (Monaco `EditorHost` open/close สลับโปรเจกต์) | 25/25 · open_errors 0 · stuck_open 0 · stuck_closed 0 · **gc EditorWebView ค้าง 0** (before 0 → after 0) |
| preview (`PreviewController` state machine) | 25/25 · errors 0 |
| discard/reattach (`TerminalWidget` จริง, #364 lever 1) | 25/25 all_ok · boot 2.25 s · reattach 0.12–0.19 s/cycle |
| main-process RSS | 46.1 → 117.4 MB (+71 MB = Monaco/WebEngine init ครั้งแรก — ไม่โตตามจำนวน cycle) |
| crash / abort | ไม่มี (exit 0) |

สรุป: ไม่มี lifecycle/reparent crash regression และไม่มี WebView object รั่วข้าม cycle → ข้อ "WebEngine soak ผ่าน" ✅
(ตัวเลข RAM acceptance "+300 MB รวม / ปิดแล้ว 0" ของ 3 โปรเจกต์จริง อยู่คนละข้อ — ดู
`docs/audit/2026-08-23-365-workspace-ram-acceptance.md`)
