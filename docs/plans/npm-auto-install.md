# Plan — `npm install -g agent-takkub` full auto-provisioner

> **เป้าหมาย (user):** ผู้ใช้พิมพ์ `npm install -g agent-takkub` แล้วได้ cockpit ที่ทำงาน
> **เหมือนเครื่องต้นแบบเป๊ะๆ** — Python+PyQt, claude CLI, plugins, MCPs, browser QA
> runtime — โดย **ไม่ต้องลงอะไรเพิ่มเอง**. ทำงานทั้ง **Windows + macOS**.
>
> **สถานะ:** PLAN + decisions locked (ดู §0) · ยังไม่เริ่ม build installer (รอ user ตื่น + clean-machine test)

---

## 0. Decisions locked (2026-07-03, ตัดสินใจแทน user ตอนหลับ)

**หลักการที่ user ยืนยัน:** *"สิ่งสำคัญต่อระบบ = seed/init default (ship) · ของจริงบนเครื่อง = per-user (ไม่ ship)"* — ตรวจแล้ว **ระบบทำแบบนี้อยู่แล้ว ~90%**:

| สิ่งของ | model | สถานะ |
|---|---|---|
| โปรเจคจริง (pms/unirecon/line_websupport) | `projects.json.example` (generic seed) + `projects.json` gitignored | ✅ มีแล้ว |
| runtime (role-memory/sessions/browser-profiles) | per-user gitignored | ✅ มีแล้ว |
| pane-tools policy · plugins · MCPs | code default (seed) + `~/.takkub` per-user + doctor provision | ✅ มีแล้ว |
| role prompts (`.claude/agents` ×10) | shipped system (ตัวสินค้า) | ✅ ship ถูก |

**Decision 1 — Distribution = keep repo PRIVATE + bundled wheel** (พลิกจาก "make public" ที่เลือกไว้ก่อน)
- เหตุผล: constraint "อย่าให้อะไรหลุด" + make-public จะเปิด **docs (83 pms hits) + git history ทั้ง 60+ commit** → ต้อง rewrite history (อันตราย, repo push แล้ว)
- wheel ส่งออกแค่ `src/**/*.py` + `.example` seeds + role prompts = **ไม่มี personal data** (docs/history/projects.json ไม่อยู่ใน wheel)

**Decision 2 — Source comment scrub = DONE** (commit นี้): pms/unirecon/monch/line_websupport ในคอมเมนต์ src → generic (`app`/`project-a`/`alice`/`my_app_web`). เหลือ **แค่ `_DEFAULT_VAULT`** (vault_mirror.py:43) ที่ยัง = `~/WebstormProjects/second-brain`

**Decision 3 — `_DEFAULT_VAULT` เก็บค่าเดิมไว้ก่อน** (ไม่แตะ functional) เพื่อ **ไม่ให้ vault ของ user พังตอนหลับ** — vault เป็น optional feature (fresh user ไม่มี folder → skip เงียบๆ)
> **TODO (user awake):** env-ize `_DEFAULT_VAULT` — ตั้ง `$TAKKUB_VAULT_DIR` ใน env cockpit ของ user แล้วเปลี่ยน default เป็น generic → shipped code ไม่มี author path เลย

**ไม่ทำ (รอ user):** make repo public · `npm publish` · build installer ที่ยังไม่เทส · แตะ `_DEFAULT_VAULT` functional

---

## 1. Inventory เครื่องต้นแบบ (audit จริง 2026-07-03)

สิ่งที่ต้อง reproduce ให้ครบ = ทุกอย่างด้านล่าง (จัดเป็น tier ตามความจำเป็น):

| Tier | Component | เวอร์ชัน/ที่มา | จำเป็น? | reproduce ยังไง |
|---|---|---|---|---|
| **0 sys** | Python | 3.11.8 (+3.12.12) | **ต้องมี** | detect; ถ้าไม่มี → guide/`uv python install` |
| 0 sys | Node + npm | v22.22.1 / 10.9.4 | **ต้องมี** | มาพร้อม npm อยู่แล้ว |
| 0 sys | git | mingw64 git | **ต้องมี** (clone plugin marketplaces) | detect; guide ถ้าไม่มี |
| **1 core** | **agent-takkub** (cockpit) | 0.9.0 · PyQt6 **6.8 LTS** pin | **หัวใจ** | postinstall → venv + `pip install` |
| 1 core | **claude Code CLI** | `@anthropic-ai/claude-code@2.1.199` | **หัวใจ** | npm dep / `npm i -g` |
| **2 plugins** | superpowers | gh `obra/superpowers` | ทีม dev | `claude plugin marketplace add` + install |
| 2 plugins | pordee | gh `kerlos/pordee` | Thai compress | เหมือนกัน |
| 2 plugins | agent-skills | gh `addyosmani/agent-skills` | มี (off ทุก role) | เหมือนกัน |
| 2 plugins | claude-plugins-official | gh `anthropics/claude-plugins-official` | frontend-design, code-review, security-guidance, remember | เหมือนกัน |
| 2 plugins | claude-obsidian | gh `AgriciDaniel/claude-obsidian` | vault | เหมือนกัน |
| 2 plugins | ecc | gh `affaan-m/everything-claude-code` | (known mk) | เหมือนกัน |
| **3 mcp** | playwright | `@playwright/mcp@0.0.75` | qa/critic/designer | **npx lazy** (cockpit warm อยู่แล้ว) |
| 3 mcp | chrome-devtools | `chrome-devtools-mcp@0.26.0` | qa/critic/designer | npx lazy |
| **4 browser** | Chrome | `C:\Program Files\Google\Chrome` | mb + CDP | detect; Playwright MCP มี chromium ของตัวเอง |
| 4 browser | mb (mini-browser) | `@runablehq/mini-browser@0.7.0` | QA `mb` path | `npm i -g` (optional) |
| **5 provider** | codex | `@openai/codex@0.142.5` | 2nd opinion | optional — cockpit degrade เป็น claude |
| 5 provider | gemini-cli | `@google/gemini-cli@0.47.0` | — | optional |
| 5 provider | agy (Antigravity) | `~/AppData/Local/agy` | สมองที่ 3 | optional |
| **6 wild** | **rtk** | custom Rust PE32+ 8.3MB `~/bin/rtk` | token opt | ⚠️ **ไม่มี public source** — ดู Q4 |

> **หมายเหตุ npm globals อื่น** (copilot, 9router, openclaw, pm2, pnpm, better-sqlite3, sql.js) = ไม่เกี่ยวกับ cockpit → **ไม่ reproduce**

---

## 2. ความจริงที่ต้องยอมรับ (honest limits ของ "ไม่ต้องลงอะไรเพิ่ม")

3 อย่างนี้ **auto ไม่ได้ 100%** — ต้องออกแบบให้ชัด ไม่งั้นสัญญาเกินจริง:

1. **🔴 Authentication (irreducible):** `claude` / `codex` / `gemini` ต้อง **login ด้วยบัญชีของ user เอง** — provision แทนไม่ได้เด็ดขาด (เป็น credential ส่วนตัว). ทางออก: installer จบด้วย **post-install checklist** บอกให้รัน `claude login` (+ optional codex/gemini) — เหลือ step เดียวนี้เท่านั้นที่ user ต้องทำ
2. **🟡 rtk:** custom binary ไม่มี public release → ต้อง (a) host binary เอง (GitHub release), (b) หา source มา build, หรือ (c) **ทำเป็น optional** (cockpit ใช้ได้ไม่มี rtk แค่กิน token มากขึ้น). แนะนำ (c) + (a) ทีหลัง
3. **🟡 Per-user config:** `~/.takkub/`, `projects.json`, auth pins = ส่วนตัว/per-machine → installer **seed default** ไม่ copy ของคนอื่น (มี project wizard อยู่แล้ว)

> ผลลัพธ์จริง: **"1 คำสั่ง + login 1 ครั้ง"** ไม่ใช่ "0 step" — แต่ใกล้เคียงมากและเป็นไปได้จริง

---

## 3. สถาปัตยกรรม installer

```
npm install -g agent-takkub            (repo → PUBLIC, Phase 0)
        │
        ▼  package.json → postinstall
  scripts/npm-postinstall.js  (Node, cross-platform orchestrator)
        │
        ├─ Phase A  bootstrap Python runtime
        │     detect python>=3.11 → python -m venv ~/.agent-takkub/venv
        │     venv/pip install "agent-takkub @ git+https://github.com/takkub/agent-takkub"
        │        └─ pulls PyQt6 6.8 + deps จาก PyPI
        │
        ├─ Phase B  ensure claude CLI
        │     npm i -g @anthropic-ai/claude-code  (ถ้ายังไม่มี)
        │
        ├─ Phase C  provision plugins + MCPs   ◄── REUSE `takkub doctor --fix`
        │     venv/takkub doctor --fix
        │        └─ marketplace add (6 public repos) + plugin install
        │        └─ browser MCP warm (playwright, chrome-devtools)
        │        └─ pane-tools policy → default (browser=qa/critic/designer)
        │
        ├─ Phase D  optional tools (best-effort, ไม่ fail ถ้าลงไม่ได้)
        │     mb, codex, gemini-cli  (npm i -g, degrade ได้)
        │     Chrome detect (guide ถ้าไม่มี)
        │
        └─ Phase E  post-install report
              ✓ installed matrix + ⚠️ "รัน `claude login` เพื่อเริ่มใช้"

  bin: agent-takkub / takkub  →  exec ~/.agent-takkub/venv python -m agent_takkub[.cli]
```

**หลักการ:**
- **Idempotent** — รันซ้ำได้ ไม่พัง (ข้ามของที่มีแล้ว) · เหมือน `doctor --fix`
- **Graceful degradation** — Phase D/optional fail ไม่ทำ install ล้ม (cockpit ยังเปิดได้)
- **Reuse `doctor --fix`** — logic provision plugins/MCPs มีอยู่แล้ว ไม่เขียนใหม่ (ขยาย doctor ให้ครอบ marketplace-add)
- **Cross-platform** — venv path (`Scripts/` vs `bin/`), Chrome path ต่าง OS → gate `process.platform`

---

## 4. Phases (งานจริง + effort + risk)

| Phase | งาน | effort | risk | ต้องปิด cockpit? |
|---|---|---|---|---|
| **0. Pre-flight** | **secret audit** ทั้ง history → **make repo public** · จอง npm name | S | 🔴 irreversible | ไม่ |
| **1. npm skeleton** | `package.json` (bin, postinstall, os, engines) + `bin/*.js` launchers (exec venv python) | S | 🟢 | ไม่ |
| **2. Python bootstrap** | `npm-postinstall.js` Phase A: detect python, venv, pip install git+ · cross-platform | M | 🟡 PyQt ~200MB, python detect | ไม่ |
| **3. claude + provision** | Phase B+C: claude CLI + **ขยาย `doctor --fix`** ให้ marketplace-add 6 repos + plugin install + MCP warm + policy default | **L** | 🟡 marketplace/plugin API drift | ไม่ |
| **4. optional + Chrome** | Phase D: mb/codex/gemini best-effort · Chrome detect+guide | M | 🟢 (degrade ได้) | ไม่ |
| **5. rtk decision** | ตาม Q4 — optional/host binary | S–M | 🟡 | ไม่ |
| **6. E2E test** | ลอง **install จริงบนเครื่องสะอาด** (VM/clean user) ทั้ง Win+Mac · CI job ใหม่ | **L** | 🔴 ตัวพิสูจน์ "เหมือนเป๊ะ" | — |
| **7. publish** | `npm publish` (outward) — เมื่อทุกอย่างเขียว | S | 🔴 outward | — |

**ประเมินรวม:** ~M–L (หลายวัน) · หัวใจความเสี่ยงอยู่ Phase 3 (provision) + Phase 6 (พิสูจน์ clean-machine)

---

## 5. Open questions (ตอบก่อนเริ่ม Phase 1)

1. **Q1 — venv location:** `~/.agent-takkub/venv` (stable ข้าม npm update, แนะนำ) หรือใน npm global dir (self-contained แต่หายตอน update)?
2. **Q2 — plugins scope:** ลง **ครบ 6 marketplace** หรือเฉพาะที่ cockpit ใช้จริง (superpowers/pordee/official/obsidian — ตัด ecc/agent-skills ที่ off)? ยิ่งลงเยอะ = install ช้า
3. **Q3 — providers (codex/gemini/agy):** ลงให้ default หรือ opt-in flag (`--with-providers`)? (cockpit degrade ได้อยู่แล้ว → default ไม่ลงจะเบากว่า)
4. **Q4 — rtk:** (a) host binary บน GitHub release แล้ว installer ดึง, (b) ทำ optional เฉยๆ, (c) หา source มา build cross-platform? → กระทบ macOS ด้วย (rtk ปัจจุบันเป็น .exe Windows เท่านั้น — mac ต้อง build ใหม่)
5. **Q5 — publish target:** npm registry สาธารณะเลย หรือ scoped/private (`@takkub/agent-takkub`) ก่อน?

---

## 6. Prerequisite gate — ก่อน make public (Phase 0)

**ต้องผ่านก่อน flip visibility (ทำแล้วบางส่วน 2026-07-03):**
- [x] `.gitignore` ครอบ `runtime/` `.venv/` `.takkub/` `*.log` `.takkub_issues.json` ✓
- [x] ไม่มี `.env`/`.pem`/`.key`/token/transcript/pane-tools.json ใน tracked files ✓ (ที่เจอเป็น false-positive: `token_meter.py` = ฟีเจอร์มิเตอร์ ไม่ใช่ secret)
- [ ] **`git grep` ทั้ง history** หา bearer/DSN/API-key (code comment พูดถึง pms bearer + postgres DSN — ต้องยืนยันว่าอยู่แค่ใน comment ไม่ใช่ค่าจริง)
- [ ] ตรวจ QA test-account plaintext (role-memory) = gitignored จริง
- [ ] review git history 60+ commits หาไฟล์ sensitive ที่เคย commit แล้วลบ

> ถ้าเจอ secret ใน history → **ห้าม make public** จนกว่าจะ scrub (git-filter-repo / BFG) — flag ให้ user

---

## 7. ผลลัพธ์ที่ user จะได้

```bash
npm install -g agent-takkub     # ← 1 คำสั่ง (postinstall provision ทุกอย่าง ~3-5 นาที)
claude login                    # ← step เดียวที่เหลือ (credential ส่วนตัว)
agent-takkub                    # ← เปิด cockpit เหมือนเครื่องต้นแบบเป๊ะ
```

**ได้ครบ:** cockpit + claude CLI + 6 plugins + browser MCPs + policy default + (optional) mb/codex/gemini
**เหลือ manual:** `claude login` เท่านั้น (+ optional provider auth ถ้าใช้)
