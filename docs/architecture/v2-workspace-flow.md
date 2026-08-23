# Flow ของระบบใหม่ — 1.1.0 → 1.2.0 → 2.0.0 (วาดตามสถานะจริง 2026-08-23)

คู่กับ `docs/plans/2026-08-23-master-dev-plan.md` (แผน/owner/ลำดับ) · `docs/v2/2.0.0-migration-plan.md` (V2 layout) ·
`docs/plans/workspace-1.2.0-design/` (workspace) · epic #365 (workspace) · #362 (Phase 10) · #364 (RAM diet)

## 1. โครงสร้าง runtime

```
                               TAKKUB COCKPIT  (1.1.0 → 1.2.0 → 2.0.0)
                                        |
               +------------------------+-------------------------+
               |                        |                         |
         Boot Splash              Project Workspace          Lead Inbox / Watchdog
   +-------------------+     +----------+-----------+      (#343 nudge · #359 verify
   | provider update   |     |                      |       · #357 wait filter)
   | AUTO-MIGRATE #361 |  Sidebar+Tree(*)    Workspace Tabs
   |  gate -> apply/   |   (1.2.0 ph.1)   +----+----+----+------+-------+
   |  apply_pending()  |   file tree      |    |    |    |      |       |
   |  -> validate ->   |   git changes   Lead Agents Editor Preview Review
   |  rollback + issue |   (QTreeView,    |    |   (Monaco (1 WebView
   +-------------------+    no WebEngine) |    |    1/app)  1/app, lazy)
                        (*) embedded under the sidebar's project card, not a
                            separate splitter panel — user feedback 2026-08-23
                                          |    |      |       |
                   RAM rule (#364): pane = claude CLI ~540MB + WebEngine ~99MB
                   -> discard hidden renderer · subagent for short tasks · proactive cap
                                        |
                                Workspace Services
                                        |
     +-----------+-----------+----------+----------+-----------+------------+
     |           |           |          |          |           |            |
   Graft      Brain V2   Conversation  Router   Capability   Git/File      Scheduler
  code        memory      session     provider+  Hub         services      governor
  structure   decisions   checkpoint  model      MCP/skills  (1.2.0:       RAM/CPU
                                      resolver   Permission   atomic save,  gate
                                      (shadow-   Engine ->    conflict,
                                       read V1   cmd_guard    diff)
                                       vs V2)    (Wave C)
                                        |
                                 STORAGE  (copy-never-move)
                +-----------------------+------------------------+
                |                                                |
         V1  ~/.agent-takkub/*.json, runtime/       V2  ~/.agent-takkub/v2/
         ====  SOURCE OF TRUTH (1.1.0)  ====        models/ agents/ projects/ state/
                |  Settings writes here              capabilities/ providers/ system/
                |                                                ^
                |   migrate apply (9-step ladder, #360 step 8)   |
                +----------------------------------------------->+
                |   dual-write (#362 part 1)  -------------------->+
                |                                                |
                |<---- shadow-read + model_pin_v2_drift ---------+   <- 1.1.0 measures parity
                |                                                |
             2.0.0: TAKKUB_V2_AUTHORITY=1 -> readers flip ------>+   <- Phase 10
                                                                 |
                                             auto-issue reports rollback/drift back (gh)
```

- **กล่องบน** = สิ่งที่ user เห็น — Explorer/Editor/Preview เป็นของใหม่ใน 1.2.0 (#365) · Boot splash มี auto-migrate
  (#361) เป็น stage ที่ 2 ก่อน MainWindow/spawn
- **กล่องกลาง** = services ที่มีอยู่แล้วและเพิ่งต่อสายใน Wave C: Router resolve model แบบ shadow-read (คืน V1 เสมอ,
  log drift) · PermissionEngine เข้า `cmd_guard` · Scheduler/governor เป็นที่ที่ RAM levers (#364) จะเข้าไป
- **กล่องล่าง** = หัวใจ 1.1.0: V2 ถูกสร้างอัตโนมัติข้าง V1 แล้ว "ส่องกระจก" เทียบกันตลอด · ไม่มีใครอ่าน V2 เป็นหลัก
  จนกว่า 2.0.0

## 2. Flow ตามเวลา

```
1.0.87 -> 1.1.0 ------------------------------------> 1.2.0 ----------------------> 2.0.0
          | V2 layout เกิดทุกเครื่องเอง (boot)          | Workspace IDE-lite        | สลับ source of truth
          | ยังอ่าน V1 · เก็บ drift                     | Explorer/Monaco/Diff/     | readers -> v2/
          | auto-rollback + auto-issue                  | Preview/Design Director   | V1 = fallback
          | apply_pending ทุก boot (#360 ตามไปเอง)       | RAM rule บังคับ           | (Phase 10)
          +-- #362 dual-write เริ่มทันที ----------- soak drift = 0 >= 1-2 wk ------+
```

## 3. Flow ตอน boot (1.1.0) — ละเอียด

```
เปิด cockpit
  |
  v
Boot splash: provider update (เดิม)
  |
  v
auto_migrate_boot.run_boot_stage()
  |-- gate: TAKKUB_AUTO_MIGRATE=0 / Settings toggle off  --> skip (log)
  |-- gate: DATA_HOME == REPO_ROOT (dev checkout)          --> skip
  |-- gate: เคย rollback แล้วที่เวอร์ชันนี้ (retry-guard)   --> skip
  |-- layout_state() == "v1"  --> ดิสก์ >= 2x ประมาณการ? --> engine.apply() (ladder 9 step)
  |                                                          --> engine.validate()
  |                                                          |-- ผ่าน  -> auto_migrate_applied
  |                                                          +-- ไม่ผ่าน -> engine.rollback() -> auto_migrate_rolled_back -> auto-issue
  +-- layout_state() == "mixed" --> engine.apply_pending()  (เฉพาะ step ที่ยังไม่ applied/validate ไม่ผ่าน
                                                              รวม version-marker · step ใหม่พัง -> rollback_step เฉพาะตัว)
  |
  v
MainWindow -> spawn panes (ไม่มี pane ไหนเกิดก่อน migration จบ)
```

## 4. Flow ของ model resolver (Wave C-1, shadow-read)

```
spawn_engine ต้องการ model ของ (role, provider)
  -> core.routing.effective_model_for_v2()
       flag off  -> role_models/provider_models (V1) ตรง
       flag on   -> Router.effective_model_for():
                      v1 = V1 lookup (authoritative)
                      ถ้ามี v2/models/*.json: v2 = legacy reader (provider-match ของ role pin คงไว้)
                      v1 != v2 -> log model_pin_v2_drift (rate-limited) -> auto-issue (>=1/24h)
                      return v1   <- เสมอ จนกว่า 2.0.0
```

## 5. RAM ต่อ pane (วัดจริง 2026-08-23)

```
1 pane ~= 650 MB  =  claude CLI ~540 MB  +  QtWebEngineProcess ~99 MB
levers (#364): 6 วัด (doctor --ram) -> 1 discard renderer pane ที่ซ่อน -> 2 subagent งานสั้น
               -> 3 proactive max_panes_global -> 4 MCP/node -> 5 main-process profile
workspace 1.2.0: Monaco 1/app + Preview 1/app, lazy/destroy, native editor opt-in
```
