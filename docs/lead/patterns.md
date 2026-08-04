# Lead patterns — parallel dispatch, verify flow, routing details

> ย้ายมาจาก cockpit CLAUDE.md (token diet 2026-08-04) — CLAUDE.md เก็บเฉพาะกฎ ไฟล์นี้คือตัวอย่างเต็ม

## Parallel pattern (`&` + `wait`)

```bash
takkub assign --role frontend --cwd <web> "เพิ่ม /login form ใช้ POST /auth/login {email,password} → {token,user}" &
takkub assign --role backend  --cwd <api> "เพิ่ม POST /auth/login รับ {email,password} ส่ง {token,user} JWT HS256 24h" &
wait
# ทั้ง 2 panes spawn คู่ขนาน → ทำงานพร้อมกัน Lead รอ report จาก done event
```

## Sequential pattern (รอ done ทีละตัว)

ใช้เมื่อ task หลังต้องการ artifact จาก task ก่อน:
```bash
takkub assign --role backend "implement /auth/login + tests"
# (รอ backend done event)
takkub assign --role qa "smoke test /auth/login: happy path + invalid creds + rate limit"
```

## Pattern ผสม (parallel ใน group, sequential ระหว่าง group)

```bash
# Group 1: impl parallel — DEV งานหลัก ทำให้จบ "ทุกอย่าง" ก่อน (ห้ามแทรก QA กลางทาง)
takkub assign --role frontend "หน้า /login form" &
takkub assign --role backend  "POST /auth/login endpoint" &
wait
# Group 2: devops ยก stack ขึ้น local (เฉพาะโปรเจคที่มี docker compose) — port ห้ามชนกับ docker ที่รันอยู่
takkub assign --role devops --cwd <api> "docker compose up -d local · เช็ค docker ps เลือก port ว่าง · healthcheck · report URLs"
# (รอ devops done — QA ต้องการ stack ที่รันอยู่)
# Group 3: QA ท้ายสุดเสมอ — เทสกับ stack จริงที่ devops ยกขึ้น
takkub assign --role qa "e2e /login flow ที่ <urls จาก devops done note>"
```

## Auto-chain (skip propose for verify sequence)

ใส่ `--auto-chain` บน impl assign → เมื่อ **ทุก** auto-chain pane ใน project report done, orchestrator inject handoff prompt เข้า Lead อัตโนมัติ สั่งรัน verify sequence: (ถ้ามี docker compose) devops ยก stack ขึ้น port-safe → รอ done → QA ท้ายสุด (devops/qa assigns ห้ามใส่ `--auto-chain` — เป็น terminal hop)
```bash
takkub assign --role frontend --auto-chain --cwd <web> "หน้า /login form" &
takkub assign --role backend  --auto-chain --cwd <api> "POST /auth/login endpoint" &
wait
```

## Shard fan-out + Plan-first

- `--shards 4` → spawn `qa#1…qa#4` คู่ขนาน แต่ละ pane ได้ env `TAKKUB_SHARD`/`TAKKUB_SHARD_TOTAL` split งานเอง (modulo)
- `--plan --shards 4` → planner pane วิเคราะห์แอป → แบ่ง N buckets balanced+independent → orchestrator auto fan-out พร้อม scope ต่อ shard → consolidated handoff
- **ใช้ --plan เมื่อ:** browser e2e/smoke หลายหน้า/flow ผ่าน Playwright MCP (cockpit แยก browser-profile ต่อ shard: `runtime/shared-mcp-<project>-qa-shard<N>.json`) · งานรวม >~5 นาทีถึงคุ้ม planner hop
- **ไม่ใช้เมื่อ:** flow เดียว หรือ non-browser test · ต้อง `--shards ≥ 2` (sweet spot 3–4) · ใช้ร่วม `--auto-chain` ไม่ได้ · plan อ่านไม่ได้ → degrade เป็น self-split + เตือน Lead
- ⚠️ **`mb` ห้ามใช้กับ `--plan/--shards`** — mb hardcode CDP `127.0.0.1:9222` ทุก shard ขับ Chrome ตัวเดียวกัน (#92) · sharded browser QA = Playwright MCP เท่านั้น

## Session goal (กัน scope drift)

```bash
takkub goal "RBAC 3 roles (viewer/editor/admin) ผ่าน JWT · scope = API + form เท่านั้น ห้ามแตะ DB migration"
takkub assign --role backend  --cwd <api> "POST /roles, GET /user/role" &
takkub assign --role frontend --cwd <web> "role selector dropdown"      &
wait
# orchestrator prepend goal เข้าทุก assign หลังจากนั้น · เคลียร์ด้วย takkub goal --clear เมื่อจบ
```

ส่ง spec เดียวกันให้หลาย role: ตั้ง `SPEC="..."` แล้ว interpolate `$SPEC` เข้าทุก assign — กัน drift

## Critic pipeline (design review 3 hops)

```bash
# Hop 1: QA smoke + shots — เขียนลง $TAKKUB_ARTIFACTS_DIR (central, นอก repo)
takkub assign --role qa --cwd <web> "smoke /login → /dashboard · save shots to \$TAKKUB_ARTIFACTS_DIR/screenshots/"
# (รอ qa done)
# Hop 2: critic + gemini parallel — design-review เขียนลง $TAKKUB_DOCS_DIR/design-review/
takkub assign --role critic --cwd <web> "design review screenshots — เสนอ เพิ่ม/ลบ/ปรับ" &
takkub assign --role gemini --cwd <web> "เตรียม view images ที่ critic จะส่งมาผ่าน takkub send" &
wait
# Hop 3: frontend implement proposals (focus high-impact ก่อน)
takkub assign --role frontend --cwd <web> "implement proposals จาก \$TAKKUB_DOCS_DIR/design-review/<date>-<view>.md"
```

## Explain-system → HTML explainer (ActionKind.EXPLAIN_SYSTEM)

intent = "รีวิวระบบ / อธิบายระบบ / explain architecture / system overview" (เข้าใจระบบ ไม่ใช่ code/design review):
1. วิเคราะห์ codebase → เขียน markdown ที่ `$TAKKUB_DOCS_DIR/system-overview/<YYYY-MM-DD>-<project>.md` (front matter `shots:` ถ้ามีภาพ)
2. `python -m agent_takkub.design_review_html "<md path>"` → self-contained HTML
3. ส่ง path `.html` ให้ user (คลิกใน pane เปิด browser ได้)

## Generate guide → HTML (ActionKind.GENERATE_GUIDE_HTML)

intent = "เขียน setup guide / how-to / คู่มือ / วิธีตั้งค่า / เขียน docs ให้ user" (เอกสาร user-facing ให้ทำตาม):
1. เขียน markdown ที่ `$TAKKUB_DOCS_DIR/guides/<YYYY-MM-DD>-<topic>.md`
2. converter เดียวกับ explainer → HTML → ส่ง path ให้ user

**กันสับสน:** `setup docker/CI` = งาน infra → devops · `add checklist component` = งาน UI → frontend · `อธิบาย/รีวิวระบบ` → EXPLAIN_SYSTEM ไม่ใช่ guide · md/html: intent explain/guide → HTML · งานปกติ → md หรือไม่มี doc

## เมื่อไหร่เรียก role เสริม

**codex** — refactor pattern ชัด (คู่ขนาน claude เทียบ diff) · code review รอบสอง (blind spot) · brainstorm list เร็ว · cross-check plan (ใส่ row ใน propose table — pane เสมอ ห้าม one-shot)
**gemini** — planning/outline (1M context) · second opinion มุมที่ 3 · long-context summarisation · brainstorm (pane เสมอ)
**critic** — หลัง QA smoke + screenshots → pre-ship gate (parallel กับ reviewer) · เปลี่ยน design/redesign · user บ่น UI งง

## Verification ที่ใช้ได้จริง

**Bad ❌** poll marker file ที่ไม่มี process touch / `sleep N && check` เดาเวลา / `until` ไม่มี timeout

**Good ✅ (เรียงตามความน่าเชื่อ):**
1. `healthcheck:` ใน docker-compose + `depends_on.condition: service_healthy` → `docker compose up -d` block จริงจน ready
2. `curl -fsS http://localhost:PORT/health` poll endpoint จริง
3. `docker compose logs --follow <svc> 2>&1 | grep -m1 'ready signal'` exit ทันทีพอเจอ
4. `docker compose ps --format json` ดู health column

## Long-running commands — ตัวอย่าง detach

```bash
docker compose up -d                                          # detach
docker compose logs --tail=50 <svc>                           # one-shot
docker compose logs --follow <svc> 2>&1 | grep -m1 'ready'    # exit on match
nohup npm run dev > /tmp/dev.log 2>&1 &                       # background dev
```
ห้าม foreground: `docker compose up` (ไม่มี -d), `logs --follow` เปล่า, `npm run dev`/`vite`/`nest --watch`, `python -m http.server`, `until` ไม่มี timeout
