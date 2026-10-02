# Skill Learning — cockpit เรียนรู้ skill เองจากงานจริง (ทุก provider)

> backlog `2c6cb77c` · แรงบันดาลใจ: [tigerless-labs/autoharness](https://github.com/tigerless-labs/autoharness)
> (Claude-only plugin, Linux/macOS) — เราเขียนใหม่เป็นของ cockpit เอง ไม่ install plugin

## ทำไมไม่ใช้ autoharness ตรงๆ

- ผูกกับ Claude Code hook/Skill tool/MCP — provider อื่นของเรา (`supports_hooks=False`) ใช้ไม่ได้
- hook command เป็น POSIX shell (`PYTHONPATH=… python3`) — Windows ไม่รัน
- spawn `claude -p --dangerously-skip-permissions` นอก pane guard/quota ของ cockpit
- เขียนโฟลเดอร์จริงลง `.claude/skills/` ข้าม central skill store → codex/อื่นๆ ไม่เห็น

## หลักการ (ยก idea มาเต็ม แต่ทำแบบของเรา)

| autoharness | Skill Learning (cockpit) |
|---|---|
| hook นับ tool call | trigger = `takkub done` — ทุก provider ผ่านจุดนี้ |
| อ่าน transcript Claude | `core.conversation.ingest` adapter ครบ 5 provider + PTY transcript fallback |
| `claude -p` + MCP stage_skill | reflector = one-shot exec ของ provider ไหนก็ได้ที่ว่าง/มีโควตา, อ่าน bundle `.md` (นโยบาย handoff ของเรา) ตอบ JSON |
| promoter → `.claude/skills` | promoter → **central store** (`project-skills/<ns>` / `skills/`) แล้ว link/appendix เดิมกระจายเอง |
| SessionStart index | index ฉีดเข้า spawn context ทุก provider + Lead context |
| นับ use จาก Skill tool | `[skill: name]` ใน done note (ทุก provider) + Skill tool / อ่าน `SKILL.md` ใน transcript |
| curator | curator รวม skill ซ้ำทุก N การเปลี่ยนแปลง + snapshot ก่อน |

## Pipeline

```
done() ──bg_pool──▶ pipeline.on_done(DoneEvent)
   1. counters.tick(layer)            ← ตัวหารของ usage rate (request = done 1 ครั้ง)
   2. episode.build()                 ← task + note + diff + transcript (redact แล้ว)
   3. usage.record()                  ← use/view จาก note+transcript
   4. lifecycle.review()              ← graduation + capacity → archive (ไม่ลบ)
   5. gate: mode≠off, episode คุ้ม, rate-limit, daily cap, มี provider ว่าง
   6. reflector.run()                 ← เขียน bundle .md → exec → JSON intents
   7. promoter.apply() / stage()      ← validate → atomic write + ledger + evidence
   8. curator (ทุก CURATE_EVERY การเปลี่ยนแปลง)
   9. run record + signal → Lead 1 บรรทัด (เฉพาะตอนมีของ land/รออนุมัติ)
```

ทั้งหมดรันบน worker thread (`bg_pool`) — ไม่แตะ Qt thread, ไม่บล็อก done

## โครงสร้างไฟล์ (V2: 1 domain 1 โฟลเดอร์)

```
<project-skills>/<ns>/<name>/          # layer=project   (global: <DATA_HOME>/skills/<name>/)
  SKILL.md                             # native format — claude/codex ค้นเจอเองผ่าน link เดิม
  .ledger.jsonl                        # append-only: create/patch/merge/archive + reason + evidence
  .sidecar.json                        # managed marker + counters (uses/views/patches/requests_at_create)
  references/evidence-<sha8>.md        # ชิ้น transcript (redact) ที่เป็นหลักฐาน
<store>/.archive/<name>-<ts>/          # archive = ย้ายโฟลเดอร์ (revive = ย้ายกลับ)

<runtime>/skill-learning/<ns>/         # state ของ project (global ใช้ _global)
  counters.json · runs/<run_id>/{episode.md,result.json} · pending/<run_id>.json · snapshots/
<settings>/skill_learning.json         # mode / provider / model / knobs
```

**แตะเฉพาะ skill ที่ระบบเขียนเอง** — ต้องมี `.sidecar.json` ที่ `managed_by == "takkub-skill-learning"`
skill ที่ user เขียน/ติดตั้งมองไม่เห็นในทุกขั้น (promoter, lifecycle, curator)

## Mode (default `auto`)

- `auto` — promoter validate ผ่านแล้ว land เลย, Lead ได้ 1 บรรทัด
- `propose` — เก็บไว้ที่ `pending/` ให้ `takkub skills learned approve <run>` / `reject`
- `off` — ไม่ reflect (ยังนับ usage + lifecycle เพื่อให้ข้อมูลไม่ขาดช่วง)

default เป็น `auto` ตามบทเรียน #641 (opt-in = ไม่มีใครใช้) — ปิดได้จาก CLI/Settings หรือ env
`TAKKUB_SKILL_LEARNING=off`

## Reflector provider (multi-provider)

`learner_provider=auto` → เลือกตัวแรกที่: ติดตั้งอยู่ · ไม่ถูกปิด (`provider_state`) · โควตาพร้อม
(`is_quota_ready`) ตามลำดับ `claude → codex → opencode → cursor`

**gap ที่ระบุชัด:** gemini (`agy -p`) ไม่ใช้เป็น reflector — print mode ต้องมี TTY จริง คืนค่าว่างเมื่อ
capture (ดู `gemini_helper.gemini_exec`) · แต่ **งานของ gemini pane ยังถูกเรียนรู้** (adapter อ่าน transcript ได้)
· opencode ไม่มี JSONL → ใช้ adapter DB / PTY transcript

reflector รันใน `cwd = runs/<run_id>/` · ตอบ JSON เท่านั้น · ไม่มี write tool (promoter เป็นผู้เขียนคนเดียว)

**ต้องใช้ได้ทุกเครื่อง ไม่ใช่แค่เครื่อง dev** — 3 กติกาที่ได้จาก live test 2026-10-02:

1. **env = env ของ pane จริง** (`pane_env._build_pane_env` + account ของ project) ไม่ใช่ `os.environ` ดิบ —
   cockpit ที่เปิดจาก shell ใน Claude Code สืบทอด bridge vars ของ parent → `claude -p` ต่อ socket ที่ตายแล้ว
   (ECONNREFUSED) · API key ที่หลุดมาก็ไม่ติดไปด้วย · ใส่ `TAKKUB_SKILL_LEARNER=1` กัน recursion
2. **ส่ง bundle ทาง stdin** ให้ claude (`-p` + stdin) และ codex (`exec -`) — codex บน Windows ใต้ sandbox
   read-only spawn shell อ่านไฟล์ไม่ได้ (ShellExecuteExW 1223) · opencode/cursor ใช้ pointer `.md` ตามนโยบาย handoff
3. **fallback ข้าม provider** — ลองตามลำดับจนได้ JSON; ตัวที่ล้มพัก 30 นาที (ไปอยู่ท้ายคิว ไม่ถูกตัดทิ้ง)
   เช่นเครื่องที่ `~/.claude/settings.json` ชี้ proxy ที่ปิดอยู่ ก็ยังเรียนรู้ผ่าน codex/opencode ได้ ·
   `model` ใช้เฉพาะตอน pin provider (ชื่อ model ผูกกับ provider)

ผล live test: claude ล้ม (proxy ของเครื่องปิด) → codex (ก่อนแก้ข้อ 2 อ่านไฟล์ไม่ได้) → opencode land สำเร็จ ·
หลังแก้ข้อ 2 codex land สำเร็จใน 20s

## Promoter — กติกา validate (ยกจาก autoharness + ของเรา)

- op ∈ `create | patch | merge | archive | none`
- name = slug `[a-z0-9][a-z0-9-]{1,63}` · ห้ามชนกับ skill ที่ไม่ใช่ของเรา
- description ≤ 300 ตัว และ **trigger ต้องอยู่ใน 80 ตัวแรก** (index ตัดที่ 80)
- body ≤ 30 บรรทัดที่ไม่ว่าง (เป็น "กฎ" ไม่ใช่ transcript) — รายละเอียดไปที่ `references/`
- secret scan (`secret_redact`) ต้องไม่เจออะไร
- patch/merge/archive ทำได้เฉพาะ skill ที่ managed · merge ต้องระบุ umbrella ที่มีอยู่จริงและ managed
- evidence ต้องเป็นข้อความที่อยู่ใน episode จริง (กันแต่งหลักฐาน)
- intent ต่อรอบ ≤ 3 · เขียนแบบ atomic (tmp + `os.replace`)

## Lifecycle (ไม่มี daemon)

- **request** = done 1 ครั้งใน layer นั้น (project: done ใน project · global: done ทุก project)
- **use** = `[skill: name]` ใน done note หรือ Skill tool เรียกจริง (claude) · **view** = transcript อ่าน path `SKILL.md`
- probation: ก่อนครบ `maturity` request → ห้าม archive
- graduation: ครบ maturity แล้ว `uses == 0 and views == 0` → archive ("ไม่มีหลักฐานว่าใช้" ≠ "หลักฐานว่าไม่ใช้" จึงต้องทั้งคู่เป็น 0)
- capacity: mature เกิน cap → archive ตัว usage rate (uses / requests since create) ต่ำสุดก่อน
- archive ไม่ลบเสมอ · `takkub skills learned revive <name>` คืนชีพพร้อมประวัติ

## Surface

- spawn context ทุก role ทุก provider: index จัดกลุ่มตาม category, 1 บรรทัด/skill, ตัด 80 ตัว,
  provider ที่ไม่มี native discovery ได้ path ให้เปิดอ่าน + คำสั่ง "ใส่ `[skill: name]` ใน done note ถ้าใช้"
- Lead context: index เดียวกัน + สรุปรอบล่าสุด (landed/rejected)
- CLI: `takkub skills learned [list|show|status|runs|approve|reject|archive|revive|mode|reflect]`

## สิ่งที่ตั้งใจไม่ทำ

- ไม่แก้ `docs/lead/` / CLAUDE.md อัตโนมัติ — กฎกลางยังเป็นของคน; reflector ถูกสั่งให้ไม่ซ้ำกับกฎกลาง
- ไม่แทน `role_memory` (โน้ตต่อ role ที่ pane เขียนเอง) — skill = บทเรียน class-level ข้าม role
