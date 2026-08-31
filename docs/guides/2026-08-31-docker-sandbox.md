# Docker sandbox (takkub-sim) — #457

ลอง agent-takkub cockpit แบบ headless ในคอนเทนเนอร์ โดยไม่แตะ `~/.agent-takkub` จริงบนเครื่อง — scope ปัจจุบัน: **claude + codex เท่านั้น**.

## Quick start (Windows PowerShell / macOS·Linux bash เหมือนกัน)

```bash
cp .env.example .env               # ปรับ TAKKUB_SIM_PASSWORD ก่อน expose พอร์ตออกนอก localhost
docker compose --profile sim up -d --build
docker compose --profile sim exec -it takkub-sim claude login    # ครั้งแรกเท่านั้น ถ้ายังไม่มี login
docker compose --profile sim logs takkub-sim | grep pairing      # เอาลิงก์ pairing มาเปิด
```

`claude login` พิมพ์ URL ให้เปิดในเบราว์เซอร์ + โค้ดให้ paste กลับ — ทำครั้งเดียวต่อ `volumes/auth/claude/` (bind-mount ทับ container restart ก็ยังจำได้). ถ้ามี `~/.claude/.credentials.json` อยู่แล้วบนเครื่อง โฟลเดอร์ `docker/` มี `seed-creds.ps1` / `seed-creds.sh` ให้ copy เข้ามาแทนการ login ใหม่ (best-effort — ไม่เจอไฟล์ก็ข้ามเงียบๆ ให้ไป `claude login` เอง); codex เลือก `~/.agent-takkub/codex-home/auth.json` ก่อนเสมอ ถ้าไม่มีค่อย fallback `~/.codex/auth.json`.

## ทดสอบว่าตอบได้จริง

```bash
docker compose --profile sim exec -T takkub-sim takkub status   # ["lead] ready" = boot สำเร็จ
docker compose --profile sim exec -T takkub-sim takkub send --to lead "ping"   # ผ่าน CLI ในคอนเทนเนอร์ — เทียบเท่าพิมพ์ผ่าน PWA (#457)
```
เช็คคำตอบจริงจาก Claude ผ่าน `/api/lead/history` (ไม่ใช่แค่ `{"ok": true}` จาก `/api/lead/say`) — ถ้าคำตอบไม่ขึ้นหรือติด `pending` นาน แปลว่า write ถูกทิ้งเงียบ ไม่ใช่ boot สำเร็จแล้วจบ.

## เข้า shell ในคอนเทนเนอร์

```bash
docker compose --profile sim exec -it takkub-sim bash
```

## Reset (ผู้ใช้รันเองเท่านั้น — ล้างสถานะทั้งหมด)

```bash
docker compose --profile sim down
rm -rf volumes/data/*      # เก็บ .gitkeep ไว้
```

## ข้อจำกัด

- headless ไม่มีหน้าต่าง cockpit — มีแค่ remote-control PWA (พอร์ตที่ตั้งใน `.env`)
- มีแค่ claude + codex ใน image (ไม่มี gemini-agy/opencode/kimi/cursor)
- image ยังต้องมี PyQt6-WebEngine runtime libs แม้ไม่เปิดหน้าต่างจริง (ดู `docs/guides/2026-07-11-headless-docker.md`)

## ยกขึ้น VPS

โฟลเดอร์ `volumes/` ทั้งก้อนย้ายไป `/srv/agent-takkub/volumes/` บน VPS ได้ตรงๆ (bind-mount path เดียวกัน) — ขั้นต่ำแนะนำ 2 vCPU / 4GB RAM.

---

ดูสถาปัตยกรรม headless เต็มๆ ที่ [`docs/guides/2026-07-11-headless-docker.md`](2026-07-11-headless-docker.md) — ไฟล์นี้เป็นเฉพาะส่วน sandbox/first-boot เท่านั้น.
