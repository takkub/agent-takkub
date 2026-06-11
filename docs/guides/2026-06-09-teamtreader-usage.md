---
title: Team Trader — คู่มือใช้งาน & วิธีรัน
project: teamtreader
date: 2026-06-09
---

# Team Trader — คู่มือใช้งาน & วิธีรัน

ระบบวิเคราะห์ Forex ด้วยทีม Claude 6 agent (News / Fundamental / Technical / Sentiment / Risk / War Room) แล้วสรุปเป็นแผนเทรด **ATLAS** (Entry / SL / TP / R:R) — **ชี้เป้าอย่างเดียว คนกดออเดอร์จริงคือคุณ 100%**

---

## 1. ระบบประกอบด้วย 2 ส่วน

| ส่วน | ภาษา | รันที่ไหน | หน้าที่ |
|---|---|---|---|
| **orchestrator/** | Python 3.11+ | **บน host** (ต้องมี `claude` CLI auth แล้ว) | spawn 6 agent วิเคราะห์ → เขียน `data/plans/latest.json` + `history.json` |
| **web/** | Next.js 16 | host (`npm run dev`) หรือ Docker | dashboard อ่านแผน + หน้า `/remote-control` (JRPG War Room) สั่งรันสด + ดู SSE |

> orchestrator **ต้องรันบน host** เพราะใช้ `claude` CLI ที่ auth ไว้ — รันใน Docker ไม่ได้ (container มีแต่ web + mount `data/plans` แบบ read-only)

---

## 2. สิ่งที่ต้องมีก่อน (Prerequisites)

- **Python 3.11+**
- **Claude Code CLI** (`claude`) — ติดตั้งแล้ว + auth แล้ว (ลอง `claude --version`)
- **Node.js** (สำหรับ web) — ถ้าจะรัน dashboard / `/remote-control`
- (ตัวเลือก) **Docker** — ถ้าจะ deploy web เป็น container

---

## 3. วิธีรัน

### A) รัน orchestrator ตรงๆ (CLI) — ได้แผนเร็วสุด

จาก root ของโปรเจค (`teamtreader/`):

```bash
# พื้นฐาน
python orchestrator/run.py --pair EURUSD --tf H4

# เต็มรูปแบบ
python orchestrator/run.py \
  --pair XAUUSD --tf H1 --secondary-tf M15 \
  --style intraday --risk 1.5 \
  --chart path/to/chart.png \
  --context "ราคาอยู่ 2320 เด้งจากแนวรับสำคัญ"
```

**Arguments:**

| Flag | บังคับ | ค่า default | ความหมาย |
|---|---|---|---|
| `--pair` | ✅ | — | คู่เงิน เช่น `EURUSD`, `XAUUSD`, `GBPJPY` |
| `--tf` | ✅ | — | TF หลัก: `M15` `H1` `H4` `D1` … |
| `--secondary-tf` | ❌ | = `--tf` | TF รอง |
| `--style` | ❌ | `intraday` | `scalp` / `intraday` / `swing` |
| `--risk` | ❌ | `1.0` | ความเสี่ยง % ต่อไม้ (0.1–10) |
| `--chart` | ❌ | — | path รูปกราฟ (Technical agent อ่าน) |
| `--context` | ❌ | — | ข้อความ context ให้ทุก agent |

**ใช้เวลา ~2–3 นาที** (analyst 1–5 รันขนาน ~60–120s + War Room ~30–60s)
**ผลออกที่:** `data/plans/latest.json` (ทับทุกครั้ง) + `data/plans/history.json` (ต่อท้าย)

---

### B) รัน web dashboard

**Dev (host) — แนะนำสำหรับใช้ `/remote-control`:**

```bash
cd web
npm install      # ครั้งแรก
npm run dev      # → http://localhost:3000
```

หน้าที่มี: `/` (แผนล่าสุด) · `/run` (สั่งรัน + SSE) · `/history` (ประวัติ) · **`/remote-control`** (War Room 8-bit)

**Docker (deploy web อย่างเดียว):**

```bash
docker compose build
docker compose up -d            # detached เสมอ
docker compose logs -f web      # ดู log
# → http://localhost:8742  (เปลี่ยน port ที่ .env: HOST_PORT=)
```

> ⚠️ ปุ่ม "เรียกประชุมสภา" / `/api/run` ใน Docker **จะไม่ทำงาน** เพราะ container ไม่มี `claude` CLI — Docker เหมาะกับ "โชว์แผนที่ orchestrator บน host รันไว้แล้ว" เท่านั้น ถ้าจะสั่งรันสดจากเว็บให้ใช้ `npm run dev` บน host

---

### C) ใช้หน้า `/remote-control` (War Room) สั่งรันสด

1. `cd web && npm run dev` → เปิด `http://localhost:3000/remote-control`
2. หน้า **WAR ROOM** โชว์แผน ATLAS ล่าสุดจาก `/api/plan` (อ่าน `latest.json`)
3. กดปุ่ม **"⚔ เรียกประชุมสภา"** → ยิง `POST /api/run` → spawn `python run.py --stream` จริง
4. สลับไปแท็บ **BATTLE LOG** ดู agent ร่ายเวทแบบสด (SSE: `agent_status` → `agent_activity` → `phase` → `plan`)
5. พอจบ War Room อัปเดตแผนใหม่อัตโนมัติ

**ทดสอบ UI โดยไม่เรียก Claude จริง (mock):** ยิง `POST /api/run?mock=1` → ได้ลำดับ event จำลอง ~12 วิ (BUY plan ตัวอย่าง) ใช้เช็คหน้าจอ/SSE โดยไม่เสีย token

---

## 4. 5 หน้าจอใน `/remote-control`

| หน้า | ดูอะไร | ข้อมูลจาก |
|---|---|---|
| **WAR ROOM** | แผน ATLAS (Entry/SL/TP1/TP2/R:R), Confluence meter, Risk Gate, agent 6 ตัว | **จริง** ← `/api/plan` |
| **BATTLE LOG** | feed สดตอนรัน (agent ร่ายเวทวิเคราะห์) | **จริง** ← `/api/run` SSE |
| **PARTY** | character sheet แต่ละ agent (stats/skill/report) | flavor + report จริงถ้ามี |
| **TREASURY** | equity curve / PnL / open positions | จำลอง (ยังไม่มี backend) |
| **QUESTS** | กลยุทธ์เป็นภารกิจ RPG | จำลอง |

**Tweaks** (มุมหน้าจอ): palette 4 ชุด · CRT scanline on/off · ฟอนต์พิกเซล · ความเร็ว animation

---

## 5. อ่านแผน ATLAS ยังไง

```
ทิศทาง:        BUY / SELL / NO TRADE
ความมั่นใจรวม:  __%  (เห็นตรงกัน __/5 ฝ่าย)
🎯 Entry / 🛑 SL / ✅ TP1 / ✅ TP2 / 📦 Lot / R:R
```

**Risk Gate — เงื่อนไข NO TRADE** (ถ้าข้อใดไม่ผ่าน = ห้ามเข้า):
- Confluence ≥ 3/5 ฝ่าย
- R:R ≥ 1:1.5
- ไม่มีข่าวแรง < 1 ชม.
- ความมั่นใจรวม ≥ 60%
- Liquidity ปกติ

**Checklist ก่อนเทรดเอง:** Confluence ≥ 3/5 · R:R ≥ 1.5 · ข่าวแรงไม่ใกล้ · Entry/SL/TP ชัด · Invalidation ชัด · Lot ถูกตาม risk%

---

## 6. ปัญหาที่พบบ่อย

| อาการ | สาเหตุ / แก้ |
|---|---|
| `claude: command not found` | ยังไม่ติดตั้ง/auth Claude CLI หรือไม่อยู่ใน PATH |
| Agent timeout | timeout 240s/agent — แก้ `AGENT_TIMEOUT` ใน `run.py` |
| **แผนออก NO_TRADE / confidence 0% ตลอด** | War Room agent อาจ exit 1 (crash) แล้ว fallback → เช็ค stderr ของ run.py ว่า agent ไหน failed / confluence < 3/5 |
| `/remote-control` กดรันแล้วไม่ไหล | รันใน Docker (ไม่มี claude CLI) → ใช้ `npm run dev` บน host แทน; หรือลอง `?mock=1` |
| เว็บไม่เห็นแผนใหม่ | `/api/plan` เป็น `force-dynamic` อ่านไฟล์ทุก request — เช็คว่า orchestrator เขียน `data/plans/latest.json` สำเร็จจริง |

---

## 7. ⚠️ คำเตือน

ระบบนี้ **วิเคราะห์เพื่อประกอบการตัดสินใจ ไม่ใช่คำแนะนำการลงทุน** — Forex เสี่ยงสูง **คนกดออเดอร์จริงคือคุณ 100%**
