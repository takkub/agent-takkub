# Lead patterns — parallel dispatch, verify flow, routing details

> ย้ายมาจาก cockpit CLAUDE.md (token diet 2026-08-04) — CLAUDE.md เก็บเฉพาะกฎ ไฟล์นี้คือตัวอย่างเต็ม

## Pane vs native subagent (`assign --mode`)

`takkub assign` ไม่ใส่ `--mode` ยังคงเท่ากับ `--mode pane` ทุกประการ เพื่อคง
พฤติกรรมเดิมและให้ user เห็นงานสดใน cockpit. `routing_planner` แค่แนะนำ mode;
Lead เป็นผู้ตัดสินสุดท้าย.

| รูปทรงงาน | Mode | เหตุผลหลัก |
|---|---|---|
| scan / audit / ค้นหา / triage เทสเสีย / first-pass review | `subagent` | boot เร็วและผลกลับ parent โดยตรง |
| fan-out งาน implement ของ role เดียว หลายชิ้นอิสระ | `pane` + `--shards N` (#641) | เปิด pane เดียว แล้ว pane ยิง native subagent N ตัวเอง — ไม่สร้าง pane/PTY จำนวนมาก และ Lead ไม่ติดรอ (ดูหัวข้อ Shard fan-out) |
| scan/triage ชิ้นเล็กจำนวนมากที่ Lead อยากรวมผลเอง | `subagent` | Lead รัน native child เอง; ผลกลับ Lead โดยตรง — ใช้เมื่อ Lead ต้องเป็นคนรวม ไม่ใช่ implement |
| implement / fix / refactor ที่ user อยากดูหรืออาจสั่งแทรก | `pane` | มองเห็นสด ส่งข้อความแทรก และจัดการ permission ได้ |
| cross-check ต่าง provider/model (เช่น codex เทียบ gemini) | `pane` เท่านั้น | native subagent ใช้ provider เดียวกับ parent เสมอ |

```bash
# ค่าเริ่มต้นเดิม — เปิด pane
takkub assign --role backend --mode pane "implement endpoint"

# ลงทะเบียน capsule แล้ว Lead dispatch ผ่าน native subagent tool ของ provider ปัจจุบัน
takkub assign --role reviewer --mode subagent "scan auth package for unsafe defaults"
# native child ปิดงานผ่านคำสั่งที่อยู่ใน capsule:
# takkub subagent-done --role reviewer "summary"
```

ข้อจำกัดที่ยอมรับโดยตั้งใจ: subagent ไม่มี pane ให้ user ดูสด, `takkub send` แทรก
กลางทางไม่ได้, approve permission จาก cockpit ไม่ได้, token ตอนทำงานไม่ลด และผลลัพธ์
ยังเข้ากลับ context ของ parent. ที่สำคัญที่สุดคือมัน **ไม่ใช่ model diversity** — ห้าม
อ้างว่า subagent ของ Claude/Codex/Gemini เป็น cross-check จาก provider อื่น.

## Parallel pattern (`&` + `wait`)

```bash
takkub assign --role frontend --cwd <web> "เพิ่ม /login form ใช้ POST /auth/login {email,password} → {token,user}" &
takkub assign --role backend  --cwd <api> "เพิ่ม POST /auth/login รับ {email,password} ส่ง {token,user} JWT HS256 24h" &
wait
# ทั้ง 2 panes spawn คู่ขนาน → ทำงานพร้อมกัน Lead รอ report จาก done event
```

> ⚠️ `wait` ข้างบนคือ **bash builtin** — รอแค่ `takkub assign` เอง return (ack "queued" ทันที ไม่ใช่รอ teammate ทำงานเสร็จ) ถ้าต้องการบล็อกจริงจนกว่า teammate จบงาน (done report ถึง Lead แล้ว) → ใช้ `takkub wait [--role <r>]... [--timeout <s>]` แทน (#242) ไม่ใช่เขียน polling loop เอง — ดูกฎ "ห้ามกอง background waiter" ใน CLAUDE.md

## Sequential pattern (รอ done ทีละตัว)

ใช้เมื่อ task หลังต้องการ artifact จาก task ก่อน:
```bash
takkub assign --role backend "implement /auth/login + tests"
# (รอ backend done event)
takkub assign --role reviewer --mode e2e "smoke test /auth/login: happy path + invalid creds + rate limit"
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
takkub assign --role reviewer --mode e2e "e2e /login flow ที่ <urls จาก devops done note>"
```

## Auto-chain (skip propose for verify sequence)

ใส่ `--auto-chain` บน impl assign → เมื่อ **ทุก** auto-chain pane ใน project report done, orchestrator inject handoff prompt เข้า Lead อัตโนมัติ สั่งรัน verify sequence: (ถ้ามี docker compose) devops ยก stack ขึ้น port-safe → รอ done → QA ท้ายสุด (devops/qa assigns ห้ามใส่ `--auto-chain` — เป็น terminal hop)
```bash
takkub assign --role frontend --auto-chain --cwd <web> "หน้า /login form" &
takkub assign --role backend  --auto-chain --cwd <api> "POST /auth/login endpoint" &
wait
```

## Shard fan-out + Plan-first

### Subagent fan-out = default ของ `--shards` (#641, 2026-09-16)

`takkub assign --role frontend --shards 5 "<งาน 5 ชิ้นอิสระ>"` → เปิด `frontend` **pane เดียว** แล้ว pane นั้นแบ่งงานและยิง native subagent 5 ตัวคู่ขนานเอง (task ลงท้ายด้วยบล็อก `━━ SUBAGENT FAN-OUT 5 ━━` ที่บอกขั้นตอน + fallback) → `takkub done` ครั้งเดียว ไม่มี ShardGroup/timeout 45 นาที

| provider | subagent tool ที่ pane ใช้ | หมายเหตุ |
|---|---|---|
| claude | `Agent` (เปิดให้เฉพาะ pane fan-out; pane ปกติยัง deny) | Claude Code เปลี่ยนชื่อ Task→Agent แล้ว |
| codex | `spawn_agent` + `wait_agent` | `multi_agent` stable เปิดอยู่แล้ว (0.154) |
| gemini (agy) | `run_subagent` | ถ้า build ไม่มี → pane ทำทีละชิ้นเอง ไม่ค้าง |
| opencode | `task` (`subagent_type="general"`) | `opencode agent list` มี general/explore |
| kimi / cursor | — | fallback เป็น N pane อัตโนมัติ + note |

- **ประหยัดอะไร:** ค่า boot CLI + role prompt + MCP init + ~0.5 GB RAM + PTY ต่อ pane × (N-1) · **ไม่ประหยัด:** ค่าอ่านไฟล์/CLAUDE.md ของแต่ละ subagent (context แยกกันคนละก้อน ไม่ได้แชร์กับ pane แม่)
- **fallback เป็น N pane เอง (มี note ใน assign ack):** reviewer `--mode e2e|ui` (= qa/critic/designer — browser profile ต่อ shard pane), `--plan`, provider ไม่มี subagent · `--mode subagent` ของ Lead ไม่เกี่ยว (คนละ feature)
- `--fanout pane` = บังคับแบบเดิม (อยากดูสดทีละตัว) · `--fanout subagent` = บังคับแบบใหม่ error ถ้าทำไม่ได้ (ไม่ fallback เงียบ)
- ใช้ร่วม `--isolation worktree` ได้: worktree เดียว branch เดียว subagent แก้คนละไฟล์ในนั้น → merge ง่ายกว่า N branch · ใช้ร่วม `--auto-chain` ได้ (done เดียว)
- pane claude ที่เปิดอยู่แล้วโดยไม่ได้ fan-out ไม่มี Agent tool → assign fan-out ไปจะได้ warning และ pane ทำทีละชิ้นแทน (`takkub close --role <r>` ก่อนถ้าต้องการคู่ขนานจริง)
- ข้อจำกัดที่ยอมรับ: เห็นแค่ pane แม่บนจอ (subagent ไม่มี pane) · pane แม่ตาย = subagent ตายหมด · งานต้องแยกไฟล์กันจริง (โฟลเดอร์เดียวกัน)

### Pane fan-out แบบเดิม (browser QA / --plan / บังคับ --fanout pane)

**#513/#590:** use `--role reviewer --mode e2e` (shown below) as the canonical form — `--role qa` still works as a deprecated alias (>= 1 release, shard/browser machinery untouched: `resolve_role_alias("qa") == ("reviewer", "e2e")`), but its provider/model/effort now resolve against reviewer's Settings row too (#590 — the roster never renders qa its own row under any built-in preset), so `--role qa` can look like it's on a different CLI than what Settings shows while `--role reviewer --mode e2e` says so plainly in its result line.

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

**#513/#590:** use `--role reviewer --mode ui` (shown below) as the canonical form — `--role critic` still works as a deprecated alias (>= 1 release, gemini cross-check pipeline untouched: `resolve_role_alias("critic") == ("reviewer", "ui")`), and unlike qa, critic's provider/model/effort have ALWAYS resolved against reviewer's row (the roster never had a checker slot for critic — #590), never its own.

```bash
# Hop 1: QA smoke + shots — เขียนลง $TAKKUB_ARTIFACTS_DIR (central, นอก repo)
takkub assign --role reviewer --mode e2e --cwd <web> "smoke /login → /dashboard · save shots to \$TAKKUB_ARTIFACTS_DIR/screenshots/"
# (รอ qa done)
# Hop 2: critic + gemini parallel — design-review เขียนลง $TAKKUB_DOCS_DIR/design-review/
takkub assign --role reviewer --mode ui --cwd <web> "design review screenshots — เสนอ เพิ่ม/ลบ/ปรับ" &
takkub assign --role gemini --cwd <web> "เตรียม view images ที่ critic จะส่งมาผ่าน takkub send" &
wait
# Hop 3: frontend implement proposals (focus high-impact ก่อน)
takkub assign --role frontend --cwd <web> "implement proposals จาก \$TAKKUB_DOCS_DIR/design-review/<date>-<view>.md"
```

⚠️ **รูปภาพแพงกว่าไฟล์ข้อความมาก (ชาร์จตาม resolution ไม่ใช่ byte, #157)** — hop นี้คือจุดเสี่ยงสุด เพราะ critic+gemini เปิดรูปชุดเดียวกันพร้อมกัน แล้ว frontend ที่ implement หลายรอบ (แก้ไม่ผ่าน review รอบแรก) มักเปิดรูปเดิมซ้ำทุกรอบ — ให้ role แรกที่เปิดรูป (มักเป็น critic) สรุปเป็น markdown note ให้ role ถัดไปอ่าน note แทนการเปิดไฟล์รูปตรงๆ ซ้ำ ถ้าจำเป็นต้องเปิดจริงหลายรอบ ให้พิจารณาลดจำนวน hop ที่เปิดรูป หรือขอ mockup เวอร์ชัน crop/downscale เฉพาะจุดที่ต้องแก้

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
**critic** (#513: `reviewer --mode ui` alias) — หลัง QA smoke + screenshots → pre-ship gate (parallel กับ reviewer) · เปลี่ยน design/redesign · user บ่น UI งง

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
