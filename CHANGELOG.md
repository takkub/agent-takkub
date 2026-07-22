# Changelog

All notable changes to agent-takkub. Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [SemVer](https://semver.org/).

## [Unreleased]

## [1.0.26] - 2026-07-21

### Added (ใหม่)
- **เลือก model ได้ต่อ provider และต่อ role** — Settings → Providers & Roles มี dropdown เลือก model ให้ทุก provider และทุก role (รายการ preset เปลี่ยนตาม CLI ที่ role นั้นเลือก, พิมพ์ id เองได้เพราะแต่ละ CLI ออก model ใหม่คนละจังหวะ). เก็บที่ `~/.takkub/provider-models.json` + `~/.takkub/role-models.json`. ลำดับความสำคัญตอน spawn: **model ของ role > model ของ provider > default ของ CLI** (ฝั่ง claude: `TAKKUB_TEAMMATE_MODEL` env ยังชนะทุกอย่างเหมือนเดิม และค่าว่างยังแปลว่า "ไม่ส่ง `--model`"). CLI: `takkub provider model <name> [<model>|--clear]`.
- **provider ใหม่ 2 ตัว** — **Kimi CLI** (MoonshotAI, `uv tool install --python 3.13 kimi-cli`, autonomy `--yolo`, Windows ต้องมี Git Bash / ตั้ง `KIMI_CLI_GIT_BASH_PATH`) และ **Cursor CLI** (`cursor-agent`, autonomy `--force`, ติดตั้งเองเท่านั้นเพราะ installer เป็น remote script — cockpit ไม่รันสคริปต์จากเน็ตให้). ทั้งคู่อ่าน `AGENTS.md` ได้จริง cockpit จึง plant teammate cheatsheet ให้ (ไม่งั้น pane ไม่รู้ว่าต้องเรียก `takkub done`). **ready/busy marker ยังไม่ calibrate** — spawn ได้แต่ยังไม่ควรใช้เป็น role หลักจนกว่าจะเก็บ marker จาก TUI จริง.
- **ติดตั้ง provider CLI จาก cockpit** — `takkub provider list` ดูสถานะ, `takkub provider install <name>` ติดตั้งรายตัว (lead-only), verify ว่า binary ขึ้น PATH จริงก่อนบอกว่าสำเร็จ.

### Changed (ปรับ)
- **doctor ไม่ติดตั้ง provider ให้อัตโนมัติแล้ว** — `takkub doctor --fix` จะ **ข้าม** การติดตั้ง provider พร้อมพิมพ์ `[skipped (opt-in)]` ต้องสั่ง `--install-providers` (หรือ `takkub provider install <name>`) เอง — กันการลง CLI หลายตัวโดยไม่ตั้งใจบนทุกเครื่องที่รัน `--fix`.
- **ถอด provider toggle chips ออกจาก status bar** — เปิด/ปิด provider ทำที่ Settings ที่เดียว (chips ซ้ำซ้อนกับหน้า Providers & Roles อยู่แล้ว).
- **ready-wait ตอนส่ง task แรกอ่านจาก ProviderSpec แล้ว** — เดิม hardcode ให้เฉพาะ codex/gemini ได้ 90 วิ ทำให้ opencode/kimi/cursor ตกไปใช้ค่า claude 45 วิ แล้วโดน blind paste ตอน cold-boot; ตอนนี้แต่ละ provider ใช้ `ready_wait_ms` ของตัวเอง.

### Fixed (แก้)
- **usage/limit meter รับมือ endpoint ที่ถูก harden แล้ว** — `oauth/usage` เริ่มตอบ 403/429 จริงจัง ทำให้ meter เดิมยิงซ้ำจนโดนบล็อกและโชว์ 0% ทั้งที่แค่อ่านค่าไม่ได้. ตอนนี้ทุก cockpit/instance ใช้ shared state ร่วมกันที่ `<config_dir>/takkub-usage-state.json` (cache + backoff ที่ persist ข้าม process), poll ห่างขึ้น 120→600 วิ และค่าที่อ่านไม่ได้แสดงเป็น `—` ไม่ใช่ 0%. **ข้อจำกัดที่รู้อยู่:** shared state ยังไม่มี inter-process lock — สอง instance ที่เริ่มพร้อมกันเป๊ะอาจยิง fetch ซ้อนกันหนึ่งรอบ (เท่าพฤติกรรมเดิมก่อนมี cache จึงไม่ใช่ regression) ไว้ปิดใน release ถัดไป.
- **auto-reminder ยิงรัวใส่ pane ตอน codex/agy กำลัง boot** — ระหว่าง cold-boot MCP servers, codex จะ **queue** task ที่เพิ่งส่งไว้ก่อน แต่ status bar อ่านว่า idle (`Fast off`) ทำให้ idle-watchdog เข้าใจผิดว่า "ทำเสร็จแล้วลืม `takkub done`" แล้วยิง `[auto-reminder]` พอกอยู่ในช่อง composer ทุก 90 วิ (งานไม่เคยพัง — พอ boot เสร็จ codex กลืน queue แล้วรันจนจบ แต่รกและกิน context). ตอนนี้ watchdog จะเริ่มนับก็ต่อเมื่อ pane **เคยเข้า turn ทำงานจริง** อย่างน้อยหนึ่งครั้งหลังรับ task และหยุดนับระหว่างเห็น marker ของ boot/queue — งานที่ทำเสร็จจริงแล้วลืมรายงานยังโดนเตือนเหมือนเดิม.

## [1.0.25] - 2026-07-13

### Added (ใหม่)
- **Instance banner แยก dev/prod (v1.0.25)** — `takkub list`/`status` ขึ้นหัวบอกว่ากำลังคุม cockpit ตัวไหน (`dev · <repo>` / `v<ver>` + port + path) และเตือนเมื่อมีอีก instance (dev↔prod) รันพร้อมกัน — เลิกงมว่าคำสั่งเข้า cockpit ตัวไหน.
- **`TAKKUB_NPM_REGISTRY` override (v1.0.25)** — ตั้ง npm registry สำหรับ public package (Claude CLI / takkub) ได้เอง เผื่อองค์กรที่ mirror แพ็กเกจไว้ใน private registry ของตัวเอง.
- **Task Ledger + Task Dock ครบวงจร** — ทุก assign มี markdown record, สถานะ flip ตอน done/failed/reassign และ cockpit แสดงงานข้าม project แบบ responsive.
- **Role/Skill lifecycle และ multi-provider architecture** — custom roles, skill catalog/matrix, shipped skill bundle, ProviderSpec registry, per-provider skill injection และ MCP bridge สำหรับ Codex/AGY.
- **Headless server mode** — แยก pane model ออกจาก Qt view, เพิ่ม headless entrypoint, Docker/Compose และ Ubuntu CI โดยยังคง desktop cockpit เดิม.
- **Remote/PWA controls** — close project, Lead pulse, quick replies, AskUserQuestion option chips และ session resume picker.
- **Settings Management รุ่นใหม่แบบ opt-in** — CRUD สำหรับ Roles/Skills/MCP/Plugins/Providers พร้อม aggregate transactions, secret-safe MCP editing และ checked role-variant regeneration; เปิดทดลองด้วย `TAKKUB_SETTINGS_UI=new`.

### Changed (ปรับ)
- **Settings ค่าเริ่มต้นกลับเป็น legacy** — correctness ของ UI ใหม่ผ่าน gate แล้ว แต่ feedback ผู้ใช้จริงพบว่า workflow ใช้ยากกว่าเดิม จึงเก็บรุ่นใหม่หลัง feature flag จนกว่า issue #115 จะผ่าน usability acceptance.
- **งานยาวและ done note ใช้ file handoff** — ลด paste/Enter race, เก็บ artifacts ใต้ runtime และแนบ screenshot evidence อัตโนมัติสำหรับ role ตรวจสอบ.
- **Parallel guidance ไม่บังคับ numeric cap** — capacity เป็น telemetry/warning เท่านั้น พร้อมแนะนำแบ่งงานเป็น waves ตามภาระจริง.
- **README ยกเครื่องใหม่ (v1.0.25)** — hero/badges โปรขึ้น, callout `-g` เตือนต้องลง global, เพิ่มจุดขาย **3 model brains** (Claude/Codex/Gemini + substitution) และ execution mode **1:1 ↔ Multi**; installer เลิก set npm registry global (เหลือ report อย่างเดียว).

### Fixed (แก้)
- **Full-system code review sweep + reliability hardening (v1.0.24)** — รีวิวโค้ดทั้งระบบแบบ multi-agent + adversarial verify แก้ครบ 92 findings: กัน **PyQt6 exit-127** (unhandled exception ใน Qt/QTimer slot ที่ทำ cockpit ตายเงียบ) ทั่ว config/remote-server/spawn/pane-tools/project-wizard, teardown **PTY resource leak** บน exit→respawn, sharded `done --fail` → เข้า fix-loop, route timer/watchdog notices ผ่าน `_notify_lead` (draft-guard กัน draft-clobber), canonical MEMORY path encoder, Windows `npm/npx` resolve, macOS Keychain login guard, doctor per-check isolation, secrets เขียน 0600 + ขยาย detection, `design_review_html` sanitize กัน HTML/JS injection, cli role-gate (`provision`/`migrate-skills`), provider `#N` shard normalize + auto-resume telemetry แบบ provider-gated, และ data-loss guards + atomic writes ใน issues/vault/config/skill-policy.
- **Multi-spawn submit reliability (v1.0.24)** — เปิด codex ≥3 pane พร้อมกันแล้ว task ถูกทิ้ง (submit CR โดนกลืนตอน MCP boot ช้าจน budget หมด); แยก **boot-retry budget (~90s) ออกจาก swallow budget** ให้ pane ที่ boot ช้าได้ CR ครบ พร้อม deterministic regression test.
- **Lead delivery/draft reliability** — แก้ draft-hold, split escape sequence, done-notice churn, duplicate bridge firing, swallowed paste/Enter และ stale project state หลายชุดที่พบจาก live repro.
- **Cross-platform Windows/macOS/Linux** — แยก PTY backend, path/process handling และ doctor checks ให้ headless/desktop ใช้ contract เดียวกัน.
- **Settings data integrity** — ป้องกัน masked secrets เขียนทับ credential จริง, rollback partial writes ของ role/skill/MCP, provider broadcast และ dirty-navigation data loss.
- **CI hermetic + version sync** — Plugins repository test ไม่พึ่ง marketplace registry ของเครื่อง dev อีกต่อไป และเพิ่ม gate ให้ `pyproject.toml`, `package.json`, `agent_takkub.__version__` ตรงกันเสมอ.
- **Hotfix Qt dependency resolution (v1.0.23)** — v1.0.22 ระบุช่วง `<6.12` กว้างเกินไปจน npm production install ดึง Qt 6.11 ซึ่ง doctor บล็อกเพราะ pane-teardown crash regression; pin ทั้ง PyQt6/WebEngine และ binary wheels ให้อยู่สาย 6.8 LTS (`>=6.8,<6.9`) พร้อมตรวจจาก registry install จริง.
- **Plugin half-clone self-repair (v1.0.25)** — plugin ที่ clone ค้างครึ่งทาง (registry บอก installed แต่ cache ไม่มีไฟล์ปลั๊กอินจริง) ทำให้ `claude plugin install` ตอบ "already installed" วนไม่จบ **restart ก็ไม่หาย** (เจอจริงกับ Claude Mem บน prod); ตอนนี้ตรวจเจอแล้วซ่อมเอง 1 รอบ (uninstall → purge cache แบบ read-only-safe → reinstall) แทนข้อความ "try restart".
- **npm private-registry รองรับ (v1.0.25)** — เครื่องที่ตั้ง npm registry เป็น private (Nexus/Artifactory) เดิม update Claude CLI/takkub ไม่ได้ (E404 เพราะ public package ไม่อยู่ใน registry ภายใน); เปลี่ยนเป็น pass `--registry` แบบ scoped เฉพาะคำสั่งที่ดึง public package (installer + Claude-CLI updater) โดย **ไม่แตะ global npm config** ของผู้ใช้ (default public · override ผ่าน `TAKKUB_NPM_REGISTRY`), และ `takkub doctor` เพิ่ม check เตือนแบบ read-only เมื่อ registry เป็น private.

## [1.0.17] - 2026-07-06

### Security (ความปลอดภัย)
- **ชุด security hardening ครบวงจร** (repo infra — ไม่กระทบ package ที่ผู้ใช้โหลด):
  - **SECURITY.md** — นโยบายแจ้งช่องโหว่ + threat model (ระบุชัด: loopback IPC socket
    + รัน shell = by design ไม่ใช่ช่องโหว่ · in-scope = secret leak/RCE/token bypass)
  - **Dependabot** — สแกน+อัปเดต dependency อัตโนมัติทุกจันทร์ (pip · npm · vscode-ext · github-actions)
  - **CodeQL** — code scanning Python + JS/TS (query `security-extended`) ทุก push/PR + weekly
  - **gitleaks** — secret scan ทั้ง git history (CI hard-fail) + pre-commit hook บล็อกก่อน commit
  - **pip-audit** — เช็ก CVE ใน Python deps (informational)
- **GitHub repo settings** (เปิดผ่าน gh api): vulnerability alerts + automated security fixes +
  private vulnerability reporting + **secret scanning & push protection** + **branch protection บน main** (solo-friendly)
- sync `__version__` ใน `__init__.py` (0.7.0 → ตรงกับ package version)

### Housekeeping
- เคลียร์ stale branches → เหลือ `main` อย่างเดียว (ลบ vscode-ide-migration + branch ที่ merge/superseded/obsolete แล้ว)

## [1.0.16] - 2026-07-05

### Added (ใหม่)
- **🌙 Auto-resume ข้าม usage limit** — pane ที่ชน quota ระหว่างมี task ค้าง จะถูก park
  แล้ว**ปลุกทำต่ออัตโนมัติตอน window reset จริง** (เวลาอ่านจาก usage API ไม่ใช่เดา):
  detection 2 ชั้น (banner บนจอ + usage API ยืนยัน) กัน false positive · cap ≤3 รอบ/task
  + ชน limit ซ้ำใน 10 นาทีหลังปลุก = หยุดถาวรคืนการตัดสินใจให้ Lead · ปลุกเฉพาะงานที่สั่ง
  ค้างไว้ ไม่รับงานใหม่เอง · **default OFF** — เปิดด้วย chip 🌙 ใน status bar (persist +
  broadcast [system] เหมือน exec-mode) · ตอน OFF ระบบ inert สนิท พฤติกรรมเดิม 100%
- **Skills เสริมจาก mattpocock/skills (คัด 4 จาก 38)** — `/grill-with-docs` + `/grilling`
  (interview เค้นแผนพร้อมสร้าง ADR/glossary), `/domain-modeling` (gemini/reviewer),
  `/codebase-design` (reviewer/codex) — ติดที่ user skills, role files ชี้การใช้แล้ว
- **ซูม font ใน pane ด้วยเมาส์** — Ctrl+scroll (Mac: Cmd+scroll) บน pane ไหน font pane นั้น
  ใหญ่/เล็กทันที (8–24pt) · Ctrl/Cmd+0 reset · ขนาดล่าสุด persist เป็น default ของ pane
  ใหม่ข้าม restart (ต่อ role) · scroll เปล่าเลื่อนปกติ + กัน Chromium page-zoom ซ้อน +
  แจ้ง PTY resize ให้ TUI ข้างใน reflow ถูก

### Changed (ปรับ)
- **Title bar สะอาดขึ้น** — แก้ identity ซ้ำ 2 รอบ (`agent-takkub [prod v..] — ... -
  agent-takkub [prod v..]`) · ตัดคำว่า `prod` เหลือ `agent-takkub v1.0.15 — dev team cockpit`
  (ฝั่ง dev ยังมี `[dev · <repo>]` ไว้แยก instance) · ถอด version chip + Changelog dialog
  ออกจาก status bar ล่าง (version มีบน title แล้ว · update chip npm/git ยังอยู่ครบ)

### Fixed (แก้)
- **first-boot ของ prod ค้าง 8+ นาที (หน้าต่างไม่ขึ้น นึกว่าแอปดับ) + โปรไฟล์ torn** — bootstrap
  clone ของ 1.0.13 `copytree` ทั้ง `~/.claude` (สนามจริง 2.9GB — ประวัติแชต 2.2GB) บน main
  thread ก่อน UI ขึ้น เปิดซ้ำก็ auto-kill ไม่ลง (ติด I/O) เจอ dialog "already running" และถ้าโดน
  kill กลางทางจะเหลือโปรไฟล์ครึ่งๆ ที่ `dest.exists()` เช็คผ่านตลอดไป. แก้ 3 ชั้น: **allowlist**
  (clone เฉพาะ CLAUDE.md/settings/keybindings/agents/commands/skills/plugins — ขยะ
  cache/security/snapshots ไม่มีทางหลุดมา) + **ประวัติแชตเอาเฉพาะ 10 session ล่าสุดต่อโปรเจค**
  (ข้ามไฟล์เดี่ยว >50MB) + **atomic** (build ใน `.partial` + marker + `os.replace` — kill
  กลางทางกี่รอบก็ไม่มี torn profile, โปรไฟล์ที่ login แล้วไม่มีวันถูกแตะ). boot แรกจบใน <10 วินาที
- **prod install ไม่มี playbook/role files เลย (Lead ไม่รู้จัก `takkub assign`)** — wheel ship แค่ `.py`
  ส่วน `REPO_ROOT` ของ installed build ชี้ `venv/Lib` ที่ว่างเปล่า → `render_lead_context()` คืน
  `None` เงียบๆ (Lead spawn มาแบบไม่มีคู่มือ), `AGENTS_DIR` ว่าง (role files teammate หายทุก role),
  `REPO_ROOT/bin` ไม่มีจริง (pane ตกไปใช้ takkub CLI ตัว dev จาก user PATH = code-version skew).
  แก้: `ASSETS_ROOT` (installed → `agent_takkub/_assets` ship ใน wheel ผ่าน setup.py build_py —
  root files ยังเป็น single source, build **fail ทันที**ถ้า assets หาย) + `CLI_BIN_DIR` prepend
  venv Scripts เข้า pane PATH + lead-context หายต้อง log event ไม่เงียบอีก
- **REPO_ROOT sweep 9 จุด (audit โดย gemini + adversarial check โดย codex — ดู `docs/audit/`)** —
  `boot.log`/`rtk_button.log`/`startup_pull.log` เขียนลง `venv/Lib` → ย้ายไป `RUNTIME_DIR` ·
  `takkub issue` fallback DB อยู่ใน `venv/Lib` โดนลบทุกครั้งที่ update → ย้ายไป DATA_HOME ·
  pane cwd fallback เลิก spawn ลง `venv/Lib` · `skill_audit`→`AGENTS_DIR` · doctor แนะ
  `npm update -g` ฝั่ง installed · `install.ps1` เลิก hardcode path · `takkub release` fail สวยๆ

### Added (ใหม่)
- **เปิด dev + prod คู่กันได้จริง** — single-instance lock เปลี่ยนจาก global ไฟล์เดียว (ที่ทำให้
  instance เปิดทีหลัง **auto-kill process tree ของตัวแรกทั้งยวง** — ต้นเหตุ "prod หายเอง" ที่ไล่กัน
  มาหลายรอบ) → lock **ต่อ DATA_HOME** · เปิดซ้ำ home เดิมยังกันเหมือนเดิม 100%
- **Instance identity** — window title/taskbar แยกชัด: `agent-takkub [prod v1.0.13]` vs
  `[dev · <repo>]` + breadcrumb `instance_boot` ลง events.log ทุก boot (DATA_HOME/ASSETS_ROOT/
  CLI_BIN_DIR/port/lock ครบ — debug "ตัวไหนเป็นตัวไหน" จาก log ได้เลย)
- **Prod Claude profile แยกจาก dev** — installed default `CLAUDE_CONFIG_DIR` =
  `~/.agent-takkub/claude-config` + first-boot **โคลนจาก `~/.claude` อัตโนมัติ** (ทุกอย่างรวม
  ประวัติแชต ยกเว้น `.credentials.json` — login ใหม่ครั้งเดียว, doctor เตือนถ้ายังไม่ login) ·
  `chatlog_scanner`/`takkub search`/resume-brief ตาม profile ของ instance · dev = `~/.claude` เดิมเป๊ะ
- **Installed-mode CI gate** — job ใหม่ build wheel → ติดตั้ง venv จริง → เทสจาก installed layout
  บน Windows+macOS ทุก commit (ปิดช่องโหว่ "dev test เขียวแต่ prod พัง" ที่ทำให้บั๊กชุดนี้รอดมา
  ถึง 1.0.12) + doctor หมวด `[installed]` + `docs/release-checklist.md`
- **prod cockpit spawn teammate ไม่ได้ (connection refused ทั้งที่ cockpit เปิดอยู่)** — pane ของ
  cockpit ที่ติดตั้งผ่าน npm/pip (single-instance, DATA_HOME=`~/.agent-takkub`) resolve `takkub`
  บน PATH ไปเจอ CLI ของ **dev checkout** (repo/bin มาก่อนใน user PATH) → CLI อ่าน port file
  ของ DATA_HOME **ตัวเอง** (`repo/runtime/port` — port เก่าที่ตายแล้ว) แทนของ cockpit ที่ spawn
  มัน → `takkub assign/send/done` โดน `WinError 10061` ทุกครั้ง Lead เลย spawn role อื่นไม่ได้เลย
  (ถ้า dev cockpit เปิดคู่กัน อาการยิ่งแย่กว่า: คำสั่งวิ่งเข้า **ผิด instance**). Root cause:
  `TAKKUB_PORT_FILE` ถูก set เฉพาะ multi-instance mode (`app.py`) — single-instance ไม่มี env นี้
  ให้ forward. แก้: `_apply_port_file()` ใน `pane_env.py` stamp `config._get_port_file()` เข้า env
  ของ**ทุก pane ทุก role รวม Lead เสมอ** (ทั้ง single/multi-instance) → CLI copy ไหนบน PATH ก็
  dial cockpit ที่ spawn ตัวเองถูกตัว. + 8 tests (`test_pane_port_file.py`) + integration verify
  3 scenario (single/multi/CLI-side read).
  npm/pip (ไม่ใช่ git checkout) เข้า branch `not_repo` ของ `_refresh_update_button` ที่
  **hardcode เขียวตลอด** — ไม่เคย query npm registry ว่ามี version ใหม่ไหม เลยเขียวแม้มีอัพเดต
  จริง (ฝั่ง git checkout ยังเปลี่ยน เขียว→น้ำเงิน ตามปกติ). แก้: poll npm registry เบื้องหลังทุก
  5 นาที (ผ่าน `_NpmUpdateThread("check")` แบบ modal-free — sibling ของ click path) → cache
  latest เทียบ current → chip เปลี่ยนเป็น **น้ำเงิน "📦 Update available (vX)"** เมื่อมีของใหม่
  (สีเดียวกับ git behind-state) และเขียว "🔄 Update via npm" เมื่อ up-to-date / ยังไม่ได้เช็ค /
  เช็คไม่ผ่าน (ไม่ false-alarm). + 8 tests.
- **self-update ค้างที่ `git fetch/pull` → restart storm + false "cockpit ไม่ได้เปิด"** —
  pane เจอ `connection refused (10061)` ทั้งที่ cockpit เปิดอยู่ — สืบจาก stack trace ใน
  `boot.log` เจอ **2 บั๊กซ้อน**: **(1)** `update_helper._git` เรียก git โดยไม่ปิด interactive
  credential prompt → บน Windows `git fetch/pull` ผ่าน HTTPS spawn `git-credential-manager`
  ที่ **สืบทอด (inherit) pipe stdout/stderr** ไปด้วย พอ `timeout=` เตะ มันฆ่าแค่ process `git`
  แต่ไม่ฆ่า credential helper ที่แยกตัวไป → pipe ไม่มีวันถึง EOF → `communicate()` join
  reader-thread **ค้างนิรันดร์** = timeout เป็นหมัน → updater thread + main thread ค้าง →
  watchdog wedge → launch ใหม่ไล่ auto-kill ตัวเก่ารัวๆ (restart storm) → pane ที่ spawn ตอน
  cockpit ครึ่งตายเลยต่อ cli_server ไม่ติด. `try_silent_self_update` (pre-UI, ก่อน
  single-instance lock) ก็ค้างท่าเดียวกัน → เหลือซาก process 1-thread/5MB ค้างเครื่อง.
  **(2)** `update_worker` `emit()` `finished` signal หลัง `_WorkerSignals` ถูก Qt ลบทิ้งตอน
  restart → `RuntimeError: wrapped C/C++ object ... has been deleted` (unhandled) รก boot.log.
  **แก้:** เพิ่ม `git_env()` (`GIT_TERMINAL_PROMPT=0` + `GCM_INTERACTIVE=never`) + ใส่
  `-c credential.helper=` ทุก git call ผ่าน `_git` → ไม่มี credential helper ถูก spawn เลย =
  ไม่มี grandchild มาถือ pipe ค้าง → timeout ทำงานจริง; route git call ตรงๆ ใน
  `try_silent_self_update` (rev-parse/pull) ให้ผ่าน `_git` ด้วย (เดิมเป็น `subprocess.run`
  เปล่า ไม่ได้ hardening) → ตัดต้นตอ husk; ครอบ `emit()` ด้วย `_safe_emit` กลืนเฉพาะ
  `RuntimeError` ของ receiver ที่ถูกลบ. + 9 tests (git_env / `-c credential.helper=` / `_safe_emit`).
- **pane ต่อ cli_server ผิด instance — `TAKKUB_PORT_FILE` ตกจาก env allowlist** —
  `_build_pane_env()`/`_build_lead_env()` กรอง env ของ pane ด้วย allowlist แต่ **ไม่มี
  `TAKKUB_PORT_FILE`** ในลิสต์ → ตอน multi-instance mode ที่ `app.py` ตั้ง per-PID port file
  ไว้ (`agent-takkub-port.<pid>`) มันโดนตัดทิ้งตอน spawn → pane ไม่เคยได้ port file ของ
  cockpit ตัวเอง เลย fall back ไปอ่าน `runtime/port` ที่เป็นซากของ instance เก่าที่ตายแล้ว →
  `takkub list/assign` เจอ `connection refused` ทั้งที่ cockpit เปิดอยู่ (comment ที่ `app.py`
  ว่า "panes inherit this env" เป็นเท็จมาตลอด — allowlist ตัดทิ้ง). แก้: เพิ่ม
  `TAKKUB_PORT_FILE` เข้า `_PANE_ENV_ALLOWLIST` (ไม่ใช่ secret — เป็น path temp) → pane ต่อ
  cli_server ถูก instance ทั้ง single mode (fall back `runtime/port` ที่ server เดียวเป็นเจ้าของ)
  และ multi mode (per-PID file). + 2 regression tests.
- **delivery self-heal กู้ swallowed paste ได้ — pane ไม่ค้าง empty อีก (#79, follow-up #26)** —
  `⚠️ [delivery-unconfirmed]` ยิงซ้ำ ~16 ครั้ง/2 สัปดาห์ ทุก role/provider (qa/backend/
  frontend/devops/codex/gemini) เพราะ task paste โดน swallow ตอน race กับ TUI render →
  pane ค้าง empty ไม่เคย report done (อาการเดิม #26). root cause: self-heal
  (`_delayed_enter_verified`) กู้ได้แค่ **Enter ที่หาย** (resend CR) — ครอบ #22 (input box มี
  `[Pasted text]` placeholder ค้าง) แต่ **กู้ paste ที่หายไม่ได้** เพราะ input ว่าง resend CR
  ลงไปก็ไม่มีอะไร submit. แก้: ทำ self-heal ให้ **paste-aware** — ตอน verify ถ้า pane ยังอยู่
  ที่ ready prompt ให้เช็คว่า input box มี content จริงไหม (`PtySession.shows_pending_input`:
  หา `[Pasted text` placeholder หรือ fragment ของ task ใน bottom region, scoped กัน body
  poison เหมือน `is_at_ready_prompt`): มี content → resend CR (#22), **input ว่าง → re-paste
  payload แล้วค่อย submit (#26)**. ครอบทุก paste+submit path (task deliver, lead-notify pump,
  force-deliver, peer send) + log event `*_repaste`. backward-compatible (ไม่ส่ง payload =
  พฤติกรรมเดิม). + 6 tests.

## [v0.9.0] - 2026-06-22

### Added (เพิ่ม)
- **`takkub doctor` รายงาน version-behind** (`check_version`) — เดิม "ตามหลัง main กี่ commit"
  อยู่แค่ใน GUI update chip → user ที่ใช้ CLI ล้วน (เพื่อนที่เพิ่ง install) ไม่เห็น. เพิ่ม check
  ใน doctor: โชว์ version (`git describe`) + behind count เทียบ origin/main + hint วิธีอัพ
  (`git pull --ff-only` + `pip install -e .` ถ้า deps เปลี่ยน) + เตือน local edits. เป็น check
  เดียวที่แตะ network (best-effort `git fetch` timeout สั้น, offline → ใช้ ref ล่าสุด + บอกว่า
  offline). ไม่ใช่ git repo → INFO บอกแปลงเป็น checkout ก่อน. + tests.

### Changed (เปลี่ยน)
- **self-update sync deps อัตโนมัติ — เครื่องอื่นอัพแล้ว "เท่า main" จริง (ไม่ใช่แค่ code)** —
  self-update chip ที่มีอยู่ (poll `origin/main` ทุก 5 นาที → tray balloon + ปุ่มกะพริบเตือน
  เมื่อ behind → คลิก pull + auto-restart; ZIP install แปลงเป็น git checkout ได้) เดิม**แค่เตือน**
  ให้ user รัน `pip install -e .` เองเมื่อ pull แล้ว `pyproject.toml` เปลี่ยน → ถ้าข้าม =
  boot ทับ deps เก่า (ของไม่ครบ เครื่องอื่นไม่เท่า main จริง). แก้: เมื่อ pull เปลี่ยน deps →
  `_restart_with_pip_sync()` spawn detached script (`build_pip_sync_script`, pattern เดียวกับ
  Claude-CLI updater): รอ cockpit ตาย → `pip install -e .` ใน venv → relaunch (relaunch แม้ pip
  fail = ไม่ brick, fallback เป็น restart ปกติถ้า spawn fail). + unit tests. → one-click update
  ลง **code + deps ครบ** อัตโนมัติ ไม่ต้องทำ manual step.
- **Vault knowledge refactor (3-tier): แยก log ออกจาก knowledge** — vault เดิม 2,232
  notes แต่ลิงก์แค่ 53 target (86% ชี้ project hub, 0.86 link/note) เพราะทุก `takkub done`
  ฝัง `[[01-Projects/<p>]]` backlink ปลอมตัวเดียว → graph เป็น hub-and-spoke ไร้ประโยชน์
  (log archive ปลอมตัวเป็น second brain). แก้เป็น 3-tier: 🟢 **knowledge**
  (`02-Areas/` MOC + `01-Projects/<p>.md`, ลิงก์จริง, อยู่ใน graph), 🟡 **log**
  (`99-Logs/`, ซ่อนจาก graph, prune), 🔴 **session** (14 วัน เก็บ last 5/project).
  เปลี่ยน: session log → `99-Logs/sessions/`, brief → `99-Logs/briefs/`, **เลิกฝัง
  backlink ปลอมบน log** (`_render_decision_note`), auto-prune (session 14d / brief 30d),
  strengthen junk filter + dedup, **distill layer** (`distill_session_facts()` —
  session จบ → สกัด durable fact → append `## Decisions & Learnings` + MOC scaffolding,
  best-effort), Obsidian graph filter ซ่อน log/orphans. + migration script
  `scripts/migrate_vault_logs.py` (move <14d → 99-Logs, delete >14d) + 64 tests.
  design: `docs/design/vault-knowledge-refactor.md` · guide: `docs/guides/2026-06-22-vault-second-brain.md`.
- **Verify flow ใหม่: DEV เสร็จทุกอย่าง → devops ยก stack ขึ้น (port-safe) → QA ท้ายสุด** —
  เดิม impl done → fire qa+reviewer คู่ขนานทันที ตอนนี้ QA เป็น "ปุ่มจบ" รันท้ายสุด
  ต่อเมื่อ DEV งานหลักเสร็จหมด **และ** (ถ้าโปรเจคมี docker compose) devops ยก stack
  ขึ้น local บน **port ที่ไม่ชนกับ docker ที่รันอยู่** ก่อน (devops เช็ค `docker ps`
  เลือก port ว่าง / offset + unique project name, `up -d --wait`, report URLs ให้ QA).
  reviewer ย้ายเป็น gate ตอน PR (qa-only mid-cycle). กระทบ: auto-chain handoff prompt,
  CLAUDE.md playbook, devops role file, built-in "feature" pipeline template
  (hop: impl → devops → qa). โปรเจคที่ไม่มี compose ข้าม devops ตรงไป QA.
- **role `gemini` เปลี่ยนเครื่องยนต์จาก Gemini CLI → Antigravity CLI (`agy`)** —
  Google ปิดบริการ Gemini CLI standalone เมื่อ 2026-06-18 แทนที่ด้วย Antigravity
  CLI (binary ชื่อ `agy`, ติดตั้งเป็น native installer จาก antigravity.google ลง
  `%LOCALAPPDATA%\agy\bin`, auth = Google Sign-In / `ANTIGRAVITY_API_KEY`).
  **role/provider ยังชื่อ `gemini` เหมือนเดิม** — เปลี่ยนแค่ binary ที่อยู่เบื้องหลัง:
  one-shot `takkub gemini` ใช้ `agy -p` (เดิม `gemini -p`), pane spawn ใช้
  `agy --dangerously-skip-permissions` (เดิม `gemini -y`), helper เปลี่ยนเป็น
  `find_agy_executable()` (`which("agy")`). substitution (Claude รับตำแหน่งแทนเมื่อ
  ไม่มี binary), routing, สี, grid, toggle ทั้งหมดทำงานเหมือนเดิม.

### Removed (ลบ)
- **`gemini_md.py` (auto-plant GEMINI.md) ถูกลบ** — `agy` auto-discover `AGENTS.md`/
  `.agents/` ไม่ใช่ `GEMINI.md` แล้ว → gemini(agy) pane ใช้ `codex_agents_md.ensure_agents_md`
  ร่วมกับ codex (AGENTS.md เดียว, marker เดียว, idempotent ไม่ชน race เมื่อ codex +
  gemini แชร์ cwd เดียวกัน). cheatsheet กลางเปลี่ยน title เป็น "agent-takkub Teammate"
  (เลิกผูกกับ codex) + เพิ่มกฎ "ใช้ path รูปตรงๆ ห้าม recursive grep หา .png".

### Fixed (แก้)
- **structural stale-marker detection — silent break ของ ready-detection ให้ดังขึ้น (#20)** —
  marker ทั้งหมดเป็น natural-language text ของ upstream CLI → reword เมื่อไหร่ detection
  พังเงียบ (idle watchdog stall). full structural rewrite (exit code/ANSI) ทำไม่ได้ (CLI เป็น
  interactive TUI long-lived ไม่มี exit code ตอนรัน, raw mode เสมอ) → ใช้ layered mitigation
  แทน: เพิ่ม **output-quiescence primitive** (`PtySession.seconds_since_output()` — structural
  signal ไม่พึ่ง text: CLI ที่ generate จะ stream ตลอด) + **stale-marker detector**
  (`Orchestrator._check_stale_markers`): pane alive + quiet เกิน `STALE_MARKER_QUIET_S` (20s) +
  ไม่ match marker ใดเลย → log `ready_marker_possibly_stale` พร้อม footer text จริง (rate-limit
  10 นาที/pane) → operator เห็น prompt ที่ reword แล้ว rescue ด้วย `TAKKUB_EXTRA_READY_MARKERS`
  ได้ (silent stall → loud diagnostic). + version-dependence registry (เอกสาร marker ไหน
  เปราะ blast radius เท่าไหร่) ใน pty_session. + tests. ปิด #20 (mitigation ครบ 4 ชั้น:
  footer-scope + env-override + doctor selftest + field detector).
- **ready-detection scope ทั้งจอ → conversation body poison ได้ (#20 ราก, ต่อจาก #70)** —
  `is_at_ready_prompt()` / `is_at_update_splash()` match marker (`bypass permissions`,
  `esc to interrupt`, `update available!` ฯลฯ) ทั่ว **ทั้งจอ** → text ใน conversation body
  ที่บังเอิญมี marker string (เช่น Lead ที่กำลังคุยเรื่อง marker เอง) ทำ verdict ผิด —
  เป็น root cause ของ #70 false-busy stall. แก้: scope detection เฉพาะ **bottom region**
  (`_ready_region`, bottom 6 non-blank rows = footer/status/input chrome) เหมือนที่
  `_TTY_PROMPT_RE` anchor bottom rows อยู่แล้ว → body text ที่ scroll เหนือ region ไม่ poison.
  `is_at_trust_prompt` คง full-screen (modal ต้องการ 2 marker พร้อมกัน, poison แทบเป็นไปไม่ได้).
  + regression tests (body-quote ไม่ busy, real footer ยัง detect, short screen ไม่เปลี่ยน).
  หมายเหตุ: #20 ส่วน "structural signals (exit code/ANSI) แทน text" ยังเหลือ — fix นี้แก้ facet
  conversation-poison ที่กระทบจริง, ลด fragility มาก แต่ยังเป็น text-marker อยู่.
- **done-notice spill ไม่ถูก reap เมื่อมี >1 project active → Lead chain ค้าง (#70)** —
  teammate ทำเสร็จ + ส่ง `takkub done` จริง แต่ notice spill ลง durable แล้ว reaper
  (`_reap_pending_done_notices`) ไม่ flush กลับ Lead → autonomous/auto-chain run ค้างเงียบ
  (เจอตอน 2 project รันขนาน: tak-game flush ได้ แต่ agent-takkub starve ~10 นาที).
  **Root cause (พิสูจน์โดย elimination + repro):** reaper logic ถูกต้องทุก project แต่ gate
  ด้วย `is_at_ready_prompt()` ซึ่งเป็น false-negative ได้ (blocker marker ในจอ conversation
  ของ Lead เองอ่านเป็น busy — marker fragility #20) → Lead alive แต่ never-ready →
  reaper skip ถาวร ไม่มี escalation. **แก้:** staleness escalation — track
  `_pending_done_since` ต่อ project, ถ้า Lead alive-but-not-ready นานเกิน
  `_DONE_NOTICE_STALE_S` (60s) → `_force_deliver_done_notices()` paste รวมเป็นข้อความเดียว
  (1 paste + verified submit, ไม่ clobber) bypass ready gate + log `done_notice_force_flush`.
  guarantee delivery ไม่ค้างถาวรแม้ ready-detection พัง. + repro/regression tests.
- **teammate pane ค้างที่ codex `update available!` splash (#62)** — codex CLI splash
  modal ถือเป็น soft-block (`is_at_ready_prompt()` = False ถาวร) → orchestrator ไม่
  deliver task + idle watchdog ไม่เตือน → pane ค้างไม่จำกัดเวลา Lead รอ `takkub done`
  ที่ไม่มีวันมา. แก้: เพิ่ม `is_at_update_splash()` (detect splash, กัน false-trigger บน
  gemini passive footer), `_check_stuck_panes` ส่ง Enter (`b"\r"`) dismiss + cooldown 30s
  (`SPLASH_DISMISS_COOLDOWN_S`) → fall through ไป close→respawn ถ้ายังค้าง. Lead exempt.
- **log noise: `spawn_still_blocked` ยิงทุก 50ms tick (#64)** — spawn-gate retry log
  ทุก tick → 213 entries ที่เป็น normal flow ไม่ใช่ error. แก้: `_spawn_blocked_first_ts`
  dedup — log ครั้งเดียวตอนเริ่ม block + เตือนอีกเมื่อ block นานเกิน
  `SPAWN_BLOCK_WARN_AFTER_S` (5s), ลบ episode key เมื่อ gate เคลียร์.
- **error spam: `idle_watchdog_pane_error` (#64)** — `_check_idle_teammates` มี
  `except Exception` ที่กลืน error โดยไม่ log type/message + วนทุก 5s tick → events.log
  เดียวมี 3279 entries ที่วินิจฉัยอะไรไม่ได้ (3210 = pms Lead pane ตัวเดียว). แก้:
  capture `err=type+message` + rate-limit (log ครั้งเดียวต่อ error/pane, cooldown 5 นาที)
  → ครั้งหน้าเห็นสาเหตุจริง 1 บรรทัด แทน spam เปล่า.
- **docs-verify gate ครอบ point-in-time artifact ใน subdir ไม่ทั่ว** — `docs/code-review/*`
  ไม่ครอบ `docs/code-review/<subdir>/*.md` (PurePath `*` ไม่ข้าม `/`, Python 3.11 ไม่มี
  recursive `**`) → snapshot เก่าที่อ้างไฟล์ที่ลบแล้ว block commit. แก้: pattern `dir/*`
  ครอบ nested path ด้วย (prefix match).
- **error sources จาก runtime (#64)** — audit events.log: เพิ่มเอกสาร `main_thread_stall`
  (989×, UI freeze 1-2.6s, ไม่ใช่ตอน spawn), big-file cache-bloat + "Error writing file"
  retry-loop (แก้ด้วย BIG_FILE_GUARD/STALE_FILE_GUARD), delivery_unconfirmed (แก้ด้วย
  agy ready-wait 90s) — รายละเอียด + action items ใน issue #64.

## [v0.8.0] - 2026-06-16

### Added (เพิ่ม)
- **`takkub goal "<objective>"`** (#50) — Lead ตั้งเป้าหมาย session ก่อน fan-out
  parallel; orchestrator prepend goal block เข้าทุก `assign` task หลังจากนั้น
  อัตโนมัติ → ทุก role เห็น big picture เดียวกัน กัน scope drift เก็บแบบ volatile
  ราย project (ไม่ persist, แต่ละ tab ไม่ leak กัน), prepend แบบ idempotent (กัน
  double บน auto-respawn replay), ride ไปกับ task replay ด้วย `takkub goal` โชว์
  goal ปัจจุบัน, `--clear` ล้าง lead-only (gate ทั้ง CLI + server).

### Fixed (แก้)
- **#51 gemini ไม่ส่ง report กลับ Lead หลัง CLI update** — gemini 0.46.0 ที่มีรุ่น
  ใหม่กว่า upstream โชว์ footer `"Gemini CLI update available! …"` ค้างถาวร (passive,
  prompt ใช้ได้ปกติ) แต่ `is_at_ready_prompt()` ดัน block บน substring
  `"update available!"` (ตั้งใจไว้สำหรับ codex splash modal) → gemini ถูกอ่านว่า
  "ทำงานอยู่ตลอด" → idle watchdog ไม่เคยถึง threshold nudge `takkub done` → report
  ไม่ถึง Lead แก้โดยเช็ค gemini ready marker (`type your message or`) **ก่อน**
  generic update blocker (codex splash ยัง block เหมือนเดิม) + regression test.
- **context % ไม่ขึ้นบน tab ที่ใช้ user profile อื่น** — `token_meter` hardcode
  `~/.claude/projects` ตายตัว แต่ pane ที่รันใต้ profile อื่น (`CLAUDE_CONFIG_DIR`
  ต่าง) เก็บ session JSONL ที่ `<config_dir>/projects/` → `find_latest_session`
  หาไม่เจอ → badge ไม่โผล่ แก้: `PtySession` จำ `CLAUDE_CONFIG_DIR` จาก spawn env,
  `find_latest_session(config_dir=...)` scope ตาม config home ของ pane นั้น
  (None = default `~/.claude` เหมือนเดิม) + regression test.

### Added (เพิ่ม · instrumentation)
- **main-thread stall logging** — dead-man watchdog เก็บ event `main_thread_stall`
  ลง `events.log` ทุก freeze > 0.75s (peak duration + `spawn_in_progress`) +
  heartbeat ถี่ขึ้น 1s→250ms, soft stack-dump 3s→1.5s เพื่อจับ UI freeze สั้นๆ
  ตอนพิมพ์ (ไว้ยืนยันว่า freeze เกิดตอน pane spawn จริงไหม). ปรับ threshold ผ่าน
  env `TAKKUB_STALL_LOG_S` / `TAKKUB_WATCHDOG_SOFT_STALL_S` / `TAKKUB_WATCHDOG_POLL_S`.

### Security (ความปลอดภัย · vNEXT-hardening)
- **one-click exec hardening** (M3#13) — คลิก path ที่ pane print แล้วเปิดผ่าน OS
  default app เดิมไม่มี guard เลย ปิด 3 ช่อง: (1) **exec extension** (`.bat/.cmd/.ps1/
  .exe/.hta/.lnk/.vbs/.msi/…`) เดิมคลิก = **รันทันที** → เปลี่ยนเป็น reveal-in-folder,
  (2) **path confinement** — absolute path ที่ไหนก็ได้ (หรือ `../` traversal) เปิดได้ →
  จำกัดให้อยู่ใต้ cwd/repo เท่านั้น, (3) **drop `file://`** จาก clickable URL (bypass
  guard ทั้งหมด). logic เป็น pure helper + 13 test.
- **OSC 52 clipboard-set strip** (M3#14) — PTY output ไหลเข้า `term.write` ตรงๆ; pane
  ส่ง `ESC]52;c;<base64>BEL` เขียน system clipboard เงียบๆ ได้ → filter ที่ render
  boundary (split-across-batch carry) เป็น defense-in-depth.
- **gate `status` transcript + screenshot** (M3#16) — `takkub status` เดิมคืน
  transcript tail + screenshot path ของ **ทุก pane ให้ caller ใดก็ได้** (teammate/manual
  อ่าน secret ใน transcript คนอื่นได้) → redact 2 field นี้เว้นแต่ caller ถือ Lead token
  (state/stall ยังเห็นได้ปกติ; status bar ใน UI อ่าน method ตรง ไม่กระทบ).
- **bracketed-paste breakout** (M6#28) — content ที่ inject เข้า pane ถ้าฝัง `ESC]201~`
  จะจบ paste mode ก่อน แล้ว byte ที่เหลือ (รวม `\r`) รันเป็น keystroke จริง = auto-submit
  คำสั่งที่ถูกแทรก → strip paste marker ออกก่อน wrap เสมอ.
- **vault decision-note scrub** (sec-w1) — strip control byte + neutralize frontmatter
  dash นำ + cap length ก่อนเขียน Obsidian.

### Performance / Token diet (vNEXT-hardening)
- **ไม่ freeze GUI ตอน `--requires-commit` done** (M2) — `git status --porcelain` เดิม
  รัน sync timeout 10s บน Qt main thread (จอค้างทั้งตัว) → ย้ายไป **QProcess** (event-loop
  driven = non-blocking + single-thread ไม่มี worker race); done notice ออกทันที, warning
  uncommitted ตามมาเป็น follow-up.
- **ลด token ต่อ spawn** — skip inject project CLAUDE.md ซ้ำเมื่อ claude auto-discover จาก
  cwd อยู่แล้ว (~750 tok/Lead spawn, tok-4); ไม่ dump role-memory skeleton ว่าง → ชี้ทาง
  บรรทัดเดียว (~100-150 tok/teammate, tok-5); cap session goal ที่ set-time (กัน 64KiB
  re-paste, tok-3).
- **bounded transcript tail-read** (M4#22) — `takkub status` เดิมอ่าน transcript ทั้งไฟล์
  (MB) เข้า memory แค่เอา 5 บรรทัดท้าย → seek อ่านแค่ 64KiB ท้าย.

### Fixed (แก้ robustness · vNEXT-hardening)
- **`takkub harvest` dead-on-arrival** (M0#1) — payload ไม่มี `from` stamp → server role-gate
  ปัดทิ้ง; เพิ่ม stamp ทั้ง 2 จุด.
- **C0/C1 control-byte scrub** (M0#2) — sanitizer strip 8-bit C1 + DEL เพิ่มจาก C0.
- **central ready-prompt marker table** (M4#17) — marker detection เดิม hardcode อังกฤษ
  กระจาย; upstream reword = provider stall (เกิด 3 ครั้ง) → รวมเป็น `_READY_RULES` ตัวเดียว
  (first-match-wins, faithful) + env override `TAKKUB_EXTRA_READY_MARKERS` (กู้ reword โดยไม่
  แก้ code) + `takkub doctor` self-test จับ marker เสีย.
- **pipeline-run pre-check** (bug-1) — เดิมตอบ `ok=true` เสมอทิ้ง error message →
  validate template+hops ก่อน schedule.
- **auto-chain handoff release** (bug-1) — blocker ตัวสุดท้ายตายโดยไม่ส่ง done →
  chain deadlock; release ที่ crash-cap + stuck-give-up ครบ 4 จุด.
- **CC flush durability** (M4#22) — เดิม pop+persist-empty ก่อน write → write fail กลางทาง
  = message ที่เหลือหาย → deliver-then-dequeue.
- **brick-guard updater** (M4#21) — รอ cockpit PID exit จริงแทน `sleep 3s` (race) + capture
  install exit code → sentinel `.failed`.
- **Windows key ถูกกลืนตอน cockpit focus** — Chromium (QtWebEngine) MediaKeysListener ลง
  low-level keyboard hook บน Windows → `--disable-features=HardwareMediaKeyHandling,
  GlobalMediaControls` (cockpit ไม่ใช้ media key).

### Changed (internal refactor · vNEXT-hardening)
- **แตก spawn() 3 branch** — extract `_launch_session` (shell/gemini/codex) drift เป็น param
  ชัด (M5#23) + `_mint_pane_token` (M5#24) + named pane-geometry const (M5#25).
- **🐴 ponytail minimal-code rules** — ดูด ruleset "lazy senior dev" (MIT) เข้า role file
  `frontend/backend/mobile/devops` + `reviewer` (over-engineering lens) ไม่ลง Node-hook (กัน brick).

## [v0.7.0] - 2026-06-06

### Added (เพิ่ม)
- **Per-project pipeline + role→CLI settings** — pipeline templates และ per-role
  CLI mapping เก็บแยกราย project (`~/.takkub/projects/<slug>/`) แต่ละ tab ไม่ชนกัน;
  provider on/off (`disabled-providers.json`) ยัง global (เป็น machine capability).
  `load/save/provider_for/effective_provider_for` รับ `project=None` (None = global +
  fallback ที่ project ใหม่ inherit จนกว่าจะ save เอง). แก้กับดัก "แก้ built-in
  pipeline → save → reverted" (built-in identity ล็อค แต่ override hops ได้;
  save() ข้าม built-in ที่ไม่ถูกแตะให้ track code ต่อ).
- **Inline learned-notes content เข้า spawn prompt** — เดิม inject แค่ *pointer*
  ให้ pane Read() เอง (มักข้ามตอนงานด่วน → ค้นความรู้เดิมซ้ำทุก spawn). ตอนนี้ฝัง
  content ตรงๆ ใน `<learned-notes>` block (cap 200 บรรทัดท้ายสุด + truncation notice)
  ให้ pane เห็นความรู้ของ project ตั้งแต่ token 0. concat ไม่ f-string (กัน literal
  braces เช่น Go templates `{{.x}}`), read มี try/except OSError.
- **Per-role × project learned memory + QA จำ login** (`role_memory.py`) — แต่ละ
  teammate role สะสมความรู้ของ project ข้ามรอบงานใน
  `runtime/role-memory/<project>/<role>.md` (conventions/gotchas/decisions; qa: test
  login/flow) อ่านตอน spawn + append เมื่อเจอของไม่ obvious.

### Changed (เปลี่ยน)
- **ซ่อนปุ่ม ▶ Run pipeline** จาก status bar (handlers เก็บไว้ตาม pattern ปุ่มที่
  ซ่อน restore ได้ง่าย); pipeline backend + CLI ใช้งานปกติ.
- **role-memory curation** — เก็บ learned notes ไม่ให้บวมเกิน: dedup bullet
  (เก็บอันใหม่สุด) + size-cap (16 KB / 120 entries, ตัดเก่าสุด) ตอนอ่าน โดยคง
  header + seeded skeleton, best-effort ไม่ raise, atomic write (#43).

### Fixed (แก้)
- **#44 parallel spawn ชน ConPTY** — ยิง `takkub assign` หลาย role พร้อมกัน /
  shard fan-out / pipeline hop spawn บน tick เดียว → ConPTY COM call ตัวหลังชน
  input-sync dispatch ของตัวก่อน (`RPC_E_CANTCALLOUT`) → `spawn_failed_warned`.
  เพิ่ม **non-blocking stagger** (QTimer slot-reservation ใน cli_server + `_defer`
  seam ใน pipeline hop; env `TAKKUB_SPAWN_STAGGER_MS` default 400ms) ครอบทุก spawn
  path. assign แรก delay 0 (ของเดิมไม่เปลี่ยน). **ไม่แตะ ConPTY/main-thread** กัน
  freeze RCA (ไม่มี `time.sleep`).
- **#38 codex npm self-update ชน EBUSY** (mitigated) — codex 2 ตัว spawn พร้อมกัน
  รัน `npm i -g @openai/codex` ทับกัน. stagger codex ด้วย gap ใหญ่กว่า (env
  `TAKKUB_CODEX_SPAWN_STAGGER_MS` default 10s) + detect ผ่าน `effective_provider_for`
  (ครอบ role ที่ remap→codex, ไม่ stagger ผิดให้ codex ที่ degrade เป็น claude).
  codex ไม่มี update off-switch จึงเป็น mitigation ไม่ใช่ full prevention.
- **#41 stuck-recovery loop ไม่มี max-attempts cap** — pane ที่ค้างแต่ยังไม่ตาย
  (wedged-alive) ถูก close→respawn วนไม่จบ → pipeline ค้างถาวร. เพิ่ม
  `STUCK_RECOVER_MAX=3` + counter ที่ survive close-pop → ครบแล้วเลิก recover +
  fail/advance pipeline hop + เตือน Lead (one-shot).
- **#42 prune `runtime/browser-profiles/`** — per-shard Chrome profile สะสม
  ไม่จำกัด → age-prune (>14 วัน, env-tunable) ตอน startup เก็บ login profile
  ที่เพิ่งใช้.
- **#40 stray 'M' ในทุก pane shell** — pin `.bat`/`.cmd` เป็น CRLF verbatim
  (`-text` ใน `.gitattributes`) แก้ cmd.exe parse bug ที่ทำให้มีตัว `M`/REM
  fragment โผล่ทุก pane.
- **Cockpit freeze hardening (RCA 2026-06-04)** — `boot.log` rotation (>256 KB) +
  soft-stall watchdog dump main-thread stack ก่อน kill; แยก RCA Issue A (CLI
  pile-up, fixed) vs Issue B (ConPTY GIL freeze, open) ใน
  `docs/cockpit-freeze-rca-2026-06-04.md`.
- **Audit Wave 1** — shard lifecycle (stale-timer guard, spawn-fail bookkeeping),
  watchdog durability, doctor UI.

### Notes
- Issue B (single-spawn ~12s GIL-hold GUI freeze) ยัง **open** — ConPTY spawn ยัง
  sync บน Qt main thread (remedies off-thread + WinPTY backend ถูก revert ก่อนหน้า
  เพราะ GIL-starve / live-typing lag). v0.7.0 stagger เฉพาะ **parallel** spawn
  collision (spawn_failed) เท่านั้น ไม่ได้แก้ single-spawn freeze.

## [v0.6.0] - 2026-06-03

### Added (เพิ่ม)
- **QA shard fan-out** — `takkub assign --role qa --shards N` spawn QA หลาย pane
  (qa#1..qa#N) แชร์ base role `qa` รัน UI smoke คู่ขนาน. แต่ละ shard แยก Chrome
  port + user-data-dir ของตัวเอง (ไม่ชนกัน), ผลรวมเป็น Lead handoff ก้อนเดียว
  พร้อม timeout 45 นาที. cross-check โดย gemini (design) + codex (21 side-effects)
  ก่อน ship.
- **Pipeline Settings dialog** — ปุ่ม **⚙ Pipelines** ใน status bar เปิดหน้า
  ตั้งค่า dev pipeline ผ่าน UI ไม่ต้องแก้ code: (1) drag-drop hop builder (role
  ใน hop เดียว = parallel, ระหว่าง hop = sequential; ตั้ง cwd/requires-commit/
  auto-chain รายตัวใน Inspector), (2) custom templates (สร้าง/rename/duplicate/
  delete; built-in 3 ตัวล็อกแก้ไม่ได้ + ปุ่ม ↺ Restore defaults), (3) Provider &
  Role toggles เปิด/ปิด codex/gemini + per-role enable. เพิ่ม `pipeline_config.py`
  (store `~/.takkub/pipelines.json` + self-heal) + `pipeline_dialog.py` (QWebChannel
  bridge) + `static/pipeline_settings.html`.
- **Edit project config ผ่าน right-click tab** (#32) — เมนู "Edit project…" แก้
  description + path mapping แล้ว save+reload **ไม่ต้อง restart** (atomic write +
  refresh list, validate path มีจริงก่อน save, preserve presets เดิม).
- **GENERATE_GUIDE_HTML routing** (#30) — เอกสาร user-facing (setup guide / how-to /
  checklist / คู่มือ / วิธีตั้งค่า) route ไปผลิต md source + แปลงเป็น HTML ผ่าน
  `design_review_html` converter อัตโนมัติ. เช็คก่อน EXPLAIN_SYSTEM กัน precedence ชน
  + กัน false-positive (setup docker→devops, checklist component→frontend).
- **AI-generated project rules** — เพิ่ม project ใหม่ผ่านปุ่ม **＋ Add Project** เลือก
  "New project (AI rules)" → cockpit รัน Claude Code headless สร้าง `<project>/CLAUDE.md`
  ให้อัตโนมัติ (ใช้เวลา ~15–60 วินาที). preview + แก้ใน editor dialog ก่อน save
  หรือกด 🔄 Regenerate ถ้าไม่พอใจ. แก้ทีหลังได้ผ่านปุ่ม **✏ Rules**. Lead pane
  ทุก spawn โหลด rules เข้า context อัตโนมัติ (cap 3000 chars) ผ่าน `lead_context.py`.
  เพิ่ม `project_rules.py` (read/write helpers) + `_RulesGeneratorThread` (headless
  claude QThread worker) + `_generate_rules_with_ui()` / `_show_rules_editor_dialog()`
  ใน `main_window.py`.

### Changed (เปลี่ยน)
- **รวม role→CLI provider mapping เข้า Pipeline Settings** — ลบปุ่ม **🤖 Providers**
  + `provider_dialog.py` (dead code หลังย้าย); ตั้ง provider ต่อ role (claude/codex/
  gemini) ในแท็บ Providers & Roles ของ ⚙ Pipelines แทน. team/provider/role config
  รวมจบที่ปุ่มเดียว.
- **ยุบ ~14 per-pane state dict เป็น `PaneState` dataclass** — แก้ root cause ของ
  lifecycle bug class: teardown เคยต้อง pop ~14 dict แยกกัน (diverge ง่าย → state-loss/
  leak) เหลือ `_pane_state.pop(key)` ครั้งเดียว. ~60 call sites migrated. pure refactor
  (1430 tests pass, 2 independent reviews).
- **รวม `_show_rules_preview_dialog` กับ `_show_rules_editor_dialog` เหลือเมธอดเดียว** —
  ทั้งสองทำงานเหมือนกัน, ลบ `_show_rules_preview_dialog` (dead duplicate)
- **ลบ `MainWindow._rebalance_teammates` สองอัน** (dead code) — caller จริงใช้
  `tab.rebalance_teammates()` ใน `project_tab.py` โดยตรงอยู่แล้ว
- **เพิ่มปุ่ม `?` ใน status bar** + `QShortcut(F1)` ระดับ window สำหรับ help dialog
  (เดิม F1 ใช้ได้แค่ตอน main window focused — ตอนนี้ทำงานแม้ pane terminal focused)

### Fixed (แก้)
- **กัน main-thread freeze / zombie orchestrator / memory drop** (#33 #34 #35) — มาจาก
  freeze incident จริง (teammate pane พ่น output ต่อเนื่อง): (#35) coalesce bytesIn เป็น
  buffer ~16ms/จำกัดขนาด แทน render ทีละ chunk, (#34) single-instance QLockFile guard +
  dead-man watchdog (1s heartbeat), (#33) inject MEMORY.md pointer เข้า teammate spawn
  prompt.
- **gap-audit lifecycle/routing fixes** — stuck-recover snapshot/restore (uuid/task/
  auto-chain/commit-gate) + rollback on spawn fail; gate multi-role UI+API ด้วย impl-verb
  (review/test/refactor ไม่โดน shadow); แยก provider toggle-off vs not-installed
  (Claude-on-Claude); กัน save-empty/preset-loss.

## [v0.5.2] - 2026-06-01

### Added (เพิ่ม)
- **`takkub release` สร้าง GitHub Release ให้อัตโนมัติ** — เดิมทำแค่ commit + git
  tag, `git push --follow-tags` ดัน tag ขึ้นแต่หน้า Releases ว่าง (เหตุที่
  v0.4.0–v0.5.1 ไม่โผล่). ตอนนี้ step สุดท้าย: push + `gh release create` โดยใช้
  section ของ version นั้นใน CHANGELOG เป็น notes → changelog โชว์บนหน้า Releases
  ทันที. best-effort (gh ไม่มี / offline → เตือนแต่ไม่ fail release เพราะ commit+tag
  ในเครื่องสำเร็จแล้ว). ปิดด้วย `--no-github-release` (กลับไป commit+tag เฉยๆ ไม่ push).
  เพิ่ม `extract_release_notes()` + `create_github_release()` ใน `release.py`.

## [v0.5.1] - 2026-06-01

### Fixed (แก้)
- **issue ไม่รั่วไป repo ของโปรเจคอื่นแล้ว** — `new_issue` เปลี่ยน default เป็น
  `cockpit_bug=True` → `takkub issue new` ลง **agent-takkub repo เสมอ** ไม่ว่าจะ
  สั่งจาก pane ของโปรเจคไหน (cockpit tracker มีไว้สำหรับบั๊กของ cockpit/orchestrator/
  CLI/UI). เดิม bug-check prompt *ขอให้* ใส่ `--cockpit-bug` แต่พอ agent ลืม issue
  ก็หลุดไปลง repo ของโปรเจคที่ active อยู่ (เช่น pms-api) เพิ่ม `--no-cockpit-bug`
  เป็น opt-out ไว้ตั้งใจลง repo ของโปรเจค active. อัพเดต bug-check prompts; เพิ่ม
  `.takkub_issues.json` (local fallback) ใน gitignore.

### Added (ปุ่มอัพเดต Claude CLI)
- **ปุ่ม `⬆ Claude CLI` ใน status bar** — เช็คว่ามี Claude Code CLI
  (`@anthropic-ai/claude-code`, npm global) version ใหม่ไหม ถ้ามีจะ **วิเคราะห์
  ความเข้ากันได้ด้วย AI** ก่อนอัพเดต: ดึง CHANGELOG ของ upstream, ตัดเฉพาะส่วนที่
  ใหม่กว่าที่ติดตั้ง, แล้วให้ headless `claude -p` ประเมินเทียบกับ flags ที่ cockpit
  พึ่งพา (`--append-system-prompt-file`, `--resume`/`--session-id`, `--mcp-config`,
  `--plugin-dir`, `--fallback-model`, `--disallowed-tools`, รูปแบบ JSONL ของ
  token-meter ฯลฯ) → report ภาษาไทย (กระทบ / เอามาใช้ได้ / ปลอดภัย + คำแนะนำ).
  เพิ่ม `claude_update.py` + `ClaudeUpdateCheckWorker` (รัน version/network/analysis
  นอก Qt thread).
- **อัพเดตปลอดภัยบน Windows** — ตอน apply จะเขียน detached updater script, ปิด
  cockpit (Lead + claude pane ทุกตัวปล่อย binary), รัน
  `npm install -g @anthropic-ai/claude-code@latest` ตอนไม่มีอะไรจับไฟล์อยู่, แล้ว
  เปิด cockpit ใหม่. เลี่ยง file-lock ที่เคยทำ CLI พัง (เหตุผลที่ปิด autoupdate).
  popup ยืนยันบอกจำนวน claude pane ที่จะถูกปิด.
- **เจอว่าต้องแก้ → เปิด GitHub issue อัตโนมัติ** — ผลวิเคราะห์จบด้วย machine-readable
  verdict `<<<TAKKUB>>>` (`ACTION_REQUIRED`/`SEVERITY`/`ISSUE_TITLE`). ถ้า version
  ใหม่หมายความว่า agent-takkub ต้องแก้ระบบ → cockpit เปิด GitHub issue เข้า repo
  ตัวเองให้ (`new_issue(cockpit_bug=True)`, tag `claude-update`), dedup ตาม version
  range กันสแปมเวลากดเช็คซ้ำ. dialog โชว์เลข issue + URL → งานความเข้ากันได้ไม่หาย
  ตอนปิด dialog ผู้ใช้มาแก้ทีหลังตามจังหวะตัวเองได้.

## [v0.5.0] - 2026-06-01

### Added (provider substitution — Claude รับตำแหน่งแทน)
- **role codex/gemini ที่ใช้ไม่ได้ ตกมาเป็น Claude แทน** แทนที่จะปฏิเสธ. 2 กรณีที่
  provider ใช้ไม่ได้ — **ปิดผ่าน toggle** ใน status bar หรือ **ยังไม่ได้ติดตั้ง CLI**
  — รวมจัดการที่ spawn layer: `provider_config.effective_provider_for()` (runtime
  "ตอนนี้ CLI ไหนใช้ได้" ต่างจาก `provider_for()` ที่บอก "ตั้งค่าไว้เป็นอะไร") จะ
  degrade role codex/gemini ที่ใช้ไม่ได้ → `claude`. `orchestrator._spawn` gate
  branch codex/gemini ด้วยค่านี้ → provider ที่ใช้ไม่ได้ไหลลง branch claude **โดยคง
  ชื่อ role เดิม** → pane "gemini"/"codex" ยังอยู่ตำแหน่ง/slot เดิม แต่รันด้วย
  `claude.exe`.
- **stand-in role prompts** `.claude/agents/{gemini,codex}.md` — อ่านเฉพาะตอน
  substitute; บอก claude pane ว่ากำลังรับบทแทน (report ขึ้นต้น `[claude-substitute
  for <role>]`) และเตือนว่าเสีย model diversity.

### Changed (เปลี่ยน)
- **routing ไม่ปฏิเสธ codex/gemini ที่ถูกปิดอีกแล้ว** — `routing_planner.classify()`
  route ตามปกติ (ไม่มี `ASK_CLARIFY`, ไม่ strip cross_check) + ใส่ substitution note
  ใน `reason`; one-shot ที่ถูกปิด degrade เป็น `FIRE_ASSIGN` (pane ที่ backed ด้วย
  claude — one-shot ไม่มี substitute path). Lead spawn context (`lead_context.py`),
  toggle broadcast notice, และ `CLAUDE.md` เปลี่ยนเป็นบอก Lead ให้ propose/fire role
  แล้วหมายเหตุเรื่อง substitution แทนที่จะบอกผู้ใช้ให้ไปเปิด provider ก่อน.

## [v0.4.0] - 2026-05-31

### Added (terminal UX + review/release tooling)
- **Clickable URLs & file paths in panes** — click a link or path in any pane
  to open it: URLs go to the OS browser (`QDesktopServices`, since QtWebEngine
  blocks `window.open`), file paths open in the OS default app (resolved against
  the pane cwd, then repo root). `terminal_widget.py` + `static/terminal.html`
  (WebLinksAddon handler + a custom xterm link provider).
- **Self-contained HTML design reviews** — `design_review_html.py` renders a
  review `.md` → portable `.html` (screenshots from front-matter `shots:`
  inlined as base64, `*impact: …*` tags → colored badge cards via CSS `:has()`).
  `critic.md` runs the converter after writing the markdown and reports both paths.
- **`EXPLAIN_SYSTEM` routing intent** — "รีวิวระบบ / อธิบายระบบ / explain
  architecture / system overview" classifies as `ActionKind.EXPLAIN_SYSTEM` and
  produces an HTML system explainer for the project instead of a chat answer;
  normal work tasks stay markdown. `routing_planner.py`.
- **Changelog viewer** — clicking the status-bar version chip opens CHANGELOG.md
  rendered in an in-app dialog (`QTextBrowser.setMarkdown`); copy-version moved
  inside it. `main_window.py`.
- **`takkub release`** — one-shot version bump (major/minor/patch or `--version`)
  + CHANGELOG `[vNEXT]` roll + git commit & annotated tag; push left to the user.
  Guards (run before any write, so `--dry-run` is a real preflight): empty
  changelog, downgrade/same/malformed version, duplicate tag. `release.py`.

### Changed (status bar visual cleanup)
- **Neutralized the status bar** (design-review findings) — action buttons
  dropped their per-button rainbow fills for a quiet ghost style; only End
  Session (closes all panes = destructive) keeps a restrained red accent.
  Provider/plan chips became outline + status dot (codex/gemini stay clickable
  toggles). Token meter de-duplicated: the tab shows `%` only, the status-bar Σ
  shows only with 2+ panes, and the pane header stays the canonical per-pane
  meter. `main_window.py`.

### Changed (per-role model tiers)
- **Teammate model is now picked per role instead of one flat Sonnet-medium
  tier.** The cockpit owner runs on Claude Max (per-token cost irrelevant), so
  model choice trades latency for quality, not dollars — spend the bigger tier
  where a miss is expensive, stay snappy where it isn't:
  - **reviewer, critic** → Opus 4.8 high effort (gate roles: last line before
    ship, run infrequently at verify/pre-ship hops where the user already
    waits). Fallback degrades only to Sonnet.
  - **backend, devops** → Sonnet 4.6 **high** effort (API contracts, schema,
    migrations, irreversible deploy/infra — high frequency, so keep Sonnet for
    turn speed but raise effort to cut subtle-bug rework).
  - **frontend, mobile, qa, designer** → Sonnet 4.6 medium (unchanged default
    — high-frequency execution, low blast radius, latency matters).
  - `_ROLE_MODEL_TIERS` / `_teammate_tier()` in `orchestrator.py`. The global
    `TAKKUB_TEAMMATE_MODEL` / `_EFFORT` / `_FALLBACK` env vars still override
    every role at once when explicitly set.

### Added (graceful model fallback under load)
- **`--fallback-model` on every spawned claude pane.** When a pane's model is
  overloaded (HTTP 529) or not found, claude now switches to a fallback model
  for the rest of the session instead of hard-failing the turn (CC 2.1.152
  made the switch session-wide; 2.1.144 made it survive `/bg`+detach). In a
  multi-pane cockpit, 4-8 panes can hit the Max rate ceiling at the same
  instant — a falling-back pane keeps working rather than erroring mid-task
  and forcing a respawn. Defaults: teammates → `claude-haiku-4-5`,
  Lead → `claude-sonnet-4-6`. Override with `TAKKUB_TEAMMATE_FALLBACK` /
  `TAKKUB_LEAD_FALLBACK` (set to `""` to disable). `orchestrator.py` spawn argv.

### Added (user-level plugin + MCP inheritance)
- **User MCP allowlist-merge**: `ensure_user_mcps()` in `shared_dev_tools.py`
  reads `~/.claude.json` top-level `mcpServers` at cockpit boot and merges a
  curated allowlist into `runtime/shared-mcp.json`. Included by default:
  `obsidian-vault` and `postgres-pms` (stdio, no credentials). Skipped by
  default: `pms` (HTTP + bearer token — security regression risk); any entry
  with `headers.Authorization` or env vars matching TOKEN/KEY/SECRET. Set
  `TAKKUB_INCLUDE_PMS=1` to opt pms back in. Browser MCPs (playwright,
  chrome-devtools) always win on name collision. Authorization header values
  are never logged.
- **`ecc` plugin** added to `_SAFE_PLUGINS` — ECC tools available in panes;
  noisy hooks remain muted via `ECC_GATEGUARD=off` + `ECC_DISABLED_HOOKS`.
- **`claude-obsidian-marketplace` intentionally NOT added** — cached 1.4.3
  still ships a `SessionStart` prompt-hook that crashed all panes in v0.2.0.
  Gated on a manual spawn smoke-test before enabling.

## [0.3.8] — 2026-05-12

### Added (token usage meter)
- **Per-pane token badge** ("สรุปการใช้งาน token"): each pane header now shows
  `<prompt> / <limit> · <pct>%` derived from the active claude session's JSONL
  on disk (`~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`). Polls every 5s
  by reading the last assistant turn's `usage` block. Hover for full
  breakdown (input + cache create + cache read + output, model name, limit).
  Colour ramps: grey < 50% → yellow 50-80% → orange 80-95% → red ≥ 95%.
- **Aggregate status-bar meter**: shows `Σ <total> · max <pct>%` summing
  prompt tokens across every active pane. Tooltip lists per-role usage so
  the user can spot which pane is bumping the cap. Headline percentage is
  the **largest single pane's ratio**, not a sum — each pane has its own
  context window so the team-wide ratio is "closest pane to its cap".
- New `token_meter.py` module: `encode_path_for_claude`, `find_latest_session`,
  `read_last_usage`, `format_tokens`, `usage_color`, `context_limit_for_model`.
  Default limit 200k; override via `TAKKUB_CONTEXT_LIMIT` env var for the
  Opus 4.7 [1m] mode.

### Fixed
- **UI freeze during typing** ("อาการ ค้างของการพิมพ์"): Typing while Claude was busy printing large amounts of text caused the entire cockpit UI to freeze. This happened because `winpty.write()` is a blocking call. Fixed by moving `PtySession.write()` to a background `_WriterThread` with a non-blocking queue. Input keystrokes are now immediately queued and the UI remains responsive.
- **Typing delay and ghost characters** ("พิมแล้วดีเลย์/ตัวหนังสือโดนแทนที่"): Switched the PTY backend from WinPTY to ConPTY. WinPTY operates by scraping the hidden console screen buffer on an interval and generating ANSI diffs, which introduced a ~50-150ms roundtrip delay and caused characters to appear out of order or replace each other during rapid typing. ConPTY provides a direct, native ANSI rendering pipeline (same as VS Code and Windows Terminal), resulting in a "super real-time" typing experience.

## [0.3.7] — 2026-05-12

### Added (Lead hybrid policy)
- **Lead direct-edit hybrid policy.** Old guidance was a single soft bullet
  ("Lead ห้ามทำงานเอง") which Lead ignored under pressure — user saw Lead
  doing direct multi-file refactors in pms-web (i18n locales + workload
  page tsx + CSS) instead of delegating to `frontend`. New policy keeps
  flexibility for *meta* work (cockpit config, planning, task specs) but
  draws a hard line for *project* work.
- New decision matrix in `CLAUDE.md` (cockpit):
  - ✅ Lead may edit: cockpit files, plan-time Read/Grep/Glob, single-line
    typos at user-pinned paths, task-spec markdown.
  - 🚫 Lead must delegate: anything under a project path, >1 file,
    >30-line edits in a round, specialist-context work (CSS, API
    contracts, schemas, infra), explicit user assignment.
- **Auto-injected `BLOCKED_DIRS` at every Lead spawn**
  (`orchestrator._render_lead_context`): renders cockpit `CLAUDE.md`
  plus a dynamic section listing the active project's `paths` so Lead
  starts each session knowing the *exact* off-limits directories. Tracks
  `projects.json` so switching projects updates the policy automatically.
- Tools are *not* hard-locked (`--disallowed-tools` unused) — Lead keeps
  Edit/Write for cockpit-side work. The hybrid relies on a sharp,
  spawn-time injected rule rather than coarse tool removal.

### Fixed (stalled-frame bug)
- **Idle pane no longer holds a stale frame.** Symptom the user saw: a
  teammate finishes its turn, the *final* batch of PTY output reaches
  xterm.js, but the DOM paint never happens — the pane sits stuck on
  the second-to-last frame until you press a key or click into it.
  `term.write` had already run; the render simply wasn't painted.
- Root cause: Chromium aggressively pauses requestAnimationFrame and
  paint scheduling for any view that isn't the foreground tab. A
  multi-pane cockpit always has N−1 panes in that state.
- Fix is three-pronged so a single layer failing won't bring the bug
  back:
  1. **Chromium flags** (`app.py`, set before QtWebEngine boots):
     `--disable-background-timer-throttling`,
     `--disable-renderer-backgrounding`,
     `--disable-backgrounding-occluded-windows`,
     `--disable-features=CalculateNativeWinOcclusion`.
  2. **In-page RAF self-loop** (`terminal.html`): a one-line
     `requestAnimationFrame(pulse)` recursive scheduler keeps xterm.js's
     render service warm at the page's native refresh rate.
  3. **Python heartbeat** (`terminal_widget.py`): a 250 ms `QTimer`
     fires `runJavaScript("void 0;")` to force a JS task-queue tick if
     the RAF loop is ever paused for any reason. Cheap on capable
     hardware, harmless on weak.
- User intent for this fix: *"เครื่องฉันแรง อยากให้มันตื่นตัวอยู่ตลอดเวลา"*
  — render service is always on.

[0.3.7]: https://github.com/takkub/agent-takkub/releases/tag/v0.3.7

## [0.3.6] — 2026-05-12

### Removed (final word on local echo)
- **All local echo logic** — for real this time. v0.3.0..v0.3.5 kept
  flip-flopping between "echo locally for snappiness" and "pass-through
  for correctness". Under fast input, claude's TUI renders arrive out
  of order (e.g. a delayed render of `"กพ"` replays *after* the user
  backspaces it away), so a smart-echo gate is not enough — the
  symptom we keep hitting is "I deleted everything, but `กพ` is stuck
  on screen until I press another key".
- xterm.js is now a pure pass-through, same as iTerm / Windows
  Terminal / wezterm. claude is the only writer to the screen. When
  claude is busy, the user perceives a roundtrip of latency per
  keystroke — that is the *correct* terminal behaviour for an
  unresponsive program. The display will never be stuck or desynced.

### Kept
- `window.termSetIdle()` remains as a no-op so the Python-side wiring
  (`AgentPane._sync_idle_flag`, `TerminalWidget.set_idle`) doesn't
  have to be ripped out in lock-step. Reintroducing optimistic
  rendering later just needs to replace the function body.

[0.3.6]: https://github.com/takkub/agent-takkub/releases/tag/v0.3.6

## [0.3.5] — 2026-05-12

### Hardened
- **Idle-flag poll throttled to 150 ms** so the smart-local-echo gate
  doesn't fire 50+ times per second on chatty TUI output. Pyte's
  `is_at_ready_prompt()` scans every line of the screen on each call;
  combined with `outputUpdated` firing per byte chunk, the original
  v0.3.4 wiring was wasting real CPU.
- **Initial idle state forced to `False`** on every pane attach.
  Previously we left `_last_idle = None` and waited for the first state
  flip — meaning a race-condition early keystroke could see the JS
  default (which is whatever the previous pane left there) and local-
  echo into a not-yet-ready terminal.
- **`set_idle()` swallows JS bridge exceptions** so a single
  `runJavaScript` hiccup can't tear the whole `outputUpdated` signal
  chain down.
- **`_sync_idle_flag()` swallows pyte exceptions too** — pyte
  occasionally throws on malformed escape sequences, and we never
  want that to disable the idle gate.

[0.3.5]: https://github.com/takkub/agent-takkub/releases/tag/v0.3.5

## [0.3.4] — 2026-05-12

### Added
- **Smart local echo** — re-introduces optimistic local rendering, but
  only when claude is sitting at the `❯` ready prompt (`is_at_ready_prompt`
  returns true). At that point ink.js re-renders synchronously on every
  keystroke, so local echo + claude's redraw match cell-for-cell and the
  user gets instant feedback again.
- When claude is busy ("Sautéed for 17s") the path collapses to pure
  pass-through, so the v0.3.2-era ghost-character desync can't happen.

### Wiring
- `TerminalWidget.set_idle(bool)` exposes the flag to the JS side via a
  new `window.termSetIdle()` JS function.
- `AgentPane._sync_idle_flag()` listens to `PtySession.outputUpdated`,
  reads `is_at_ready_prompt()` from the pyte screen, and pushes the
  flag whenever it flips. Only edge-triggered updates cross the bridge
  to keep IPC chatter low.

[0.3.4]: https://github.com/takkub/agent-takkub/releases/tag/v0.3.4

## [0.3.3] — 2026-05-12

### Removed
- **All local echo / local backspace handling.** v0.3.0–0.3.2 tried to
  mask the round-trip latency of "type → JS → Python → PTY → claude →
  PTY → JS → render" by writing keystrokes to xterm.js immediately,
  but ink.js TUI input boxes batch their re-renders while claude is
  busy and our stale local state ended up fighting claude's delayed
  redraws. Symptom: typing a char then backspacing repeatedly left a
  ghost char on screen until the user pressed an unrelated key, which
  triggered claude to finally redraw and "consume" the buffered
  backspaces in one go.
- Now xterm.js is a pure pass-through: every keystroke goes straight
  to the PTY and claude is the only source of truth for the input
  area's display. Worst-case latency per keystroke matches every other
  terminal emulator (~roundtrip when claude is busy), but the display
  never desyncs.

[0.3.3]: https://github.com/takkub/agent-takkub/releases/tag/v0.3.3

## [0.3.2] — 2026-05-12

### Fixed
- **Backspace ค้าง** — v0.3.0 local echo wrote each typed char to xterm.js
  instantly but never erased on backspace, so typing "[backend" then
  hitting backspace 8 times left the chars visibly stuck until claude
  caught up and redrew the input area. Local echo now writes `\b \b`
  (erase last cell) when the user presses Backspace/DEL, keeping the
  display in sync with the user's intent even when claude is mid-think.
- **Local-echo filter tightened** — previously `\r`, `\n`, `\t` were
  treated as printable and got written locally, which could nudge the
  cursor in ways that conflicted with claude's redraw. Now only
  0x20..0x7e + non-control multi-byte (Thai, CJK) get local echo;
  everything else passes through to claude untouched.

[0.3.2]: https://github.com/takkub/agent-takkub/releases/tag/v0.3.2

## [0.3.1] — 2026-05-12

### Added
- **`agent-takkub.bat` at repo root** — single-file launcher that newcomers
  can double-click. Checks Python 3.11+ on PATH, checks `claude` CLI on
  PATH, creates `.venv` + installs deps on first run, copies
  `projects.json.example` to `projects.json` and opens it in Notepad if
  missing, then launches the cockpit detached.
- **Quick start** section in `README.md` — 3-step setup with the exact
  commands a fresh user needs (install Python + Claude CLI + clone +
  double-click the launcher).
- **Troubleshooting** table in `README.md` covering the seven most likely
  setup snags (missing Python / claude, sub-window dying, missing
  takkub shim, Thai diacritics, hook errors, wrong Lead cwd).

### Changed
- `scripts/run.bat` is now a thin one-line wrapper that delegates to
  the root `agent-takkub.bat`. Kept for backward compat with existing
  shortcuts / muscle memory.

### Fixed
- `agent-takkub.bat` initial drafts had unescaped `)` inside `echo`
  text blocks (e.g. `echo Log in: claude (one-time)`), which closed
  the surrounding `if` block early and caused unconditional `goto :fail`.
  Replaced with `--` separators.

[0.3.1]: https://github.com/takkub/agent-takkub/releases/tag/v0.3.1

## [0.3.0] — 2026-05-12

### Changed (breaking architecture)

The terminal rendering layer is now **xterm.js running inside a
QWebEngineView**, the same emulator VS Code / Hyper / GitHub Codespaces
ship with. The Iter 1–9 QPlainTextEdit + pyte rebuild pipeline was a
"fake terminal" that hit hard walls on Thai/CJK shaping, alt-screen
scrollback, and TUI form alignment — every "สระหาย / กระตุก / ลบไม่หมด"
report v0.2.x couldn't fully solve.

xterm.js handles these natively: browser layout engine for complex
script shaping (Thai combining marks, BiDi, CJK width), built-in 10k
scrollback, proper mouse modes, and first-class selection/copy/paste.

### Added
- `src/agent_takkub/static/` bundle: `terminal.html`, `xterm.js` 5.5.0,
  `xterm.css`, `addon-fit`, `addon-web-links` — shipped in the package
  via `package_data` so the app works offline.
- `TerminalWidget` rewritten as `QWebEngineView` + `QWebChannel` bridge:
  - `bridge.sendInput(str)` → `inputBytes` signal → PTY
  - `bridge.resize(cols, rows)` → `resized` signal → `PtySession.resize()`
  - `bridge.ready()` → flush bytes queued during boot
- `PtySession.bytesIn(bytes)` signal emitting raw PTY chunks for xterm.js
  to consume directly (no pyte → rich rebuild).
- **Local echo** for printable input in xterm.js so each typed character
  appears the moment the key is pressed instead of waiting for claude's
  ink.js TUI to redraw on the *next* keystroke. Control sequences (Esc,
  arrows, Ctrl-keys, DEL) still go untouched to claude.
- Batched output writes: multiple `write_bytes()` calls within the same
  Qt event-loop tick coalesce into a single `runJavaScript` IPC hop
  (0 ms QTimer). Chatty TUI frames now cost one round trip instead of
  dozens.
- `PyQt6-WebEngine>=6.6` dependency (~150 MB Chromium bundle).

### Kept
- `pyte.Screen` still lives in `PtySession` purely for state-detection
  helpers (`is_at_trust_prompt`, `is_at_ready_prompt`, and `display_lines`
  for export). The double-parse cost buys us keeping every v0.2.x
  orchestrator behaviour — auto-trust, ready-detect, audit log, presets,
  session resume — unchanged.

### Migration
- `pip install -e .` (pulls PyQt6-WebEngine ~150 MB Chromium).
- Same `scripts\run.bat`, same `projects.json`, same `takkub` CLI.
- All v0.2.x behaviour preserved: Lead in project root, role-aware cwd,
  superpowers + agent-skills plugins, audit log, tray notifications,
  bash-friendly `takkub` shim.

### Known caveats
- Per-pane font size shortcut (Ctrl+= / Ctrl+-) wired but untested in the
  xterm.js context; xterm's own Ctrl+= / Ctrl+- works regardless.
- Export pane buffer still goes via pyte (`display_lines`) so it captures
  only the visible viewport. Future patch: switch to xterm.js's full
  buffer (`term.buffer.active`).
- The pyte-mode-detection mouse-wheel path from v0.2.2 is unused —
  xterm.js's built-in scroll handles wheel correctly.

[0.3.0]: https://github.com/takkub/agent-takkub/releases/tag/v0.3.0

## [0.2.4] — 2026-05-12

### Fixed
- **Lead was working on agent-takkub itself, not on the user's project.**
  Lead spawned in `REPO_ROOT` (the cockpit source tree), so its Read/Grep/
  Bash tools all landed in cockpit files instead of the active project's
  code. Lead now spawns in the project root (common parent of all
  `paths`, or first listed path), and the cockpit's `CLAUDE.md` is passed
  via `--append-system-prompt-file` so Lead still knows the `takkub`
  cheatsheet without losing project context.
- `config.lead_cwd()` helper resolves the right directory:
  - `projects.json → projects.<name>.lead` explicit key, if set
  - else the common parent of all `paths` (e.g. `pms/` for `pms-web` + `pms-api`)
  - else the first listed path

### Changed
- Render debounce 20 ms → 0 ms (next-tick coalesce). Qt still batches
  many `outputUpdated` emits within a single event-loop tick into one
  redraw, so we don't thrash, but we also never artificially hold a
  frame back. IME echo and TUI form navigation feel live now.

[0.2.4]: https://github.com/takkub/agent-takkub/releases/tag/v0.2.4

## [0.2.3] — 2026-05-12

### Fixed
- **`takkub: command not found` from Lead's bash** — Lead's Bash tool spawns
  `/usr/bin/bash` (MSYS) which does not auto-append `.cmd` to commands, so
  `bin/takkub.cmd` was invisible to it. Added a POSIX shell shim at
  `bin/takkub` (no extension) that delegates to the same `.venv` Python
  module. cmd.exe/PowerShell still use `bin/takkub.cmd`.
- **UI felt stale ("ไม่ขยับ")** — the v0.2.2 `_last_rendered_rich` diff
  cache was skipping legitimate redraws when row tuples looked identical
  to the previous frame, even though pyte had mutated cursor state /
  refreshed a status line / pulsed a blink. Removed the cache entirely;
  every frame now redraws.
- Bumped debounce 33ms → 20ms (~50 fps) so typing echo feels live again
  while staying cheap enough that idle frames don't thrash.

[0.2.3]: https://github.com/takkub/agent-takkub/releases/tag/v0.2.3

## [0.2.2] — 2026-05-12

### Fixed
- **Thai diacritics rendering** — `QTextCharFormat.setFont(QFont(widget.font()))`
  was collapsing the families fallback chain in some Qt builds, so combining
  marks (◌ิ ◌ี ◌่ ◌้ ◌์ ฯลฯ) silently disappeared. Switched to
  `setFontFamilies(...)` + individual `setFontWeight/Italic/Underline` which
  preserves per-glyph fallback through Tahoma/Leelawadee UI.
- **Typing stutter** — added a `_last_rendered_rich` diff cache so identical
  screen states skip the full QTextDocument rebuild (~360 insertText calls).
  pyte fires `outputUpdated` for every byte chunk including no-op sequences
  (mouse-mode toggles, cursor save/restore), and the old path paid the rebuild
  on every keystroke.
- Bumped debounce 16ms→33ms (30fps) so typing storms collapse into fewer
  frames.
- Auto-scroll-to-bottom only fires when the user was already at the bottom
  before the refresh. Scrolling up to inspect history no longer gets yanked
  away by the next pyte update.

### Added
- **Smart mouse-wheel forwarding** — when claude has SGR mouse tracking on
  (mode 1006, the modern default), wheel events go out as proper
  `\x1b[<64;1;1M` / `\x1b[<65;1;1M` press events so claude scrolls its own
  buffer smoothly. Falls back to PgUp/PgDn when mouse tracking is off.
- `AgentPane._refresh_terminal` reads `screen.mode` and sets
  `TerminalWidget.mouse_tracking_on` accordingly on every frame.

[0.2.2]: https://github.com/takkub/agent-takkub/releases/tag/v0.2.2

## [0.2.1] — 2026-05-12

### Fixed
- Default `--setting-sources` reverted to `project,local`. The v0.2.0 switch to
  `user,project,local` re-exposed claude-obsidian 1.4.3's `SessionStart` hook
  bug (`ToolUseContext is required for prompt hooks. This is a bug.`) inside
  every spawned pane.
- Cleared `presets: ["frontend"]` from the shipped `projects.json`. Auto-spawn
  was firing on every cockpit launch regardless of whether the user wanted a
  frontend pane. Lead now stays alone until you `takkub assign` or click "+ pane".

### Added
- `_default_plugin_dirs()` + explicit `--plugin-dir` args so spawned agents
  still inherit **superpowers** and **agent-skills** even though user-level
  settings are skipped. claude-obsidian is intentionally excluded until its
  hook is fixed upstream.
- `TAKKUB_EXTRA_PLUGINS` env var (semicolon-separated paths) to override the
  default plugin allowlist — set to empty string to suppress, or point at
  custom plugin directories.

[0.2.1]: https://github.com/takkub/agent-takkub/releases/tag/v0.2.1

## [0.2.0] — 2026-05-12

### Changed
- `--setting-sources` default flipped from `project,local` to `user,project,local`
  so spawned agents inherit the user's installed Claude Code plugins (superpowers,
  agent-skills, claude-obsidian) and MCP servers. The original Iter 1 SessionStart
  hook bug that motivated the previous isolation appears resolved in claude-obsidian 1.4.3.

### Added
- `TAKKUB_SETTING_SOURCES` env var to override the default (e.g.
  `TAKKUB_SETTING_SOURCES=project,local` to fall back to the isolated v0.1 behaviour
  if a global plugin misbehaves).
- Orphan cleanup hook in `app.py`: atexit + SIGINT/SIGTERM/SIGBREAK handlers terminate
  every spawned claude/winpty-agent before the Qt process exits, so a crash or kill
  can't leave child processes pinned to the venv.
- Lead's `CLAUDE.md` now starts with a takkub quick-reference table + a "Tooling
  available to agents" section pointing at superpowers / agent-skills / MCP. Lead
  sees this on every session start, no more "what commands exist?".

[0.2.0]: https://github.com/takkub/agent-takkub/releases/tag/v0.2.0

## [0.1.0] — 2026-05-12

First release. Replaces the tmux-based `agent-teams` setup with a native Windows desktop cockpit. Built in 9 iterations on the same day.

### Added — Iter 1 (baseline)
- PyQt6 main window with 3-column splitter (Lead · middle · right)
- `pywinpty` PTY backend, `pyte` ANSI screen model
- TCP-based `takkub` CLI (list / spawn / assign / send / close / done) for agent-to-orchestrator IPC
- Initial migration of 7 role definitions from `agent-teams` (replaced tmux-send-keys with `takkub` CLI calls)
- `scripts/run.bat` launcher that creates the .venv on first run

### Fixed — Iter 1.5 (post-launch debugging)
- Hidden `cmd.exe`/`conhost.exe` console window after spawn (`ConsoleWindowClass` SW_HIDE diff)
- Use `pythonw.exe` + `start ""` in `run.bat` so the launcher batch exits immediately
- pywinpty `read(size=...)` signature fix (`num_bytes` kwarg was wrong)
- pywinpty `write()` expects `str` not `bytes` — silent TypeError was eating every keystroke
- EOFError handling: check `isalive()` before treating an empty read as termination
- Thai diacritic regression after rich rendering — preserve `QFont` family fallback chain inside `QTextCharFormat`

### Added — Iter 2
- Auto-trust folder prompt (poll for "trust this folder" modal → send Enter)
- Auto-detect idle `❯` prompt before pasting `assign` task (replaces 12s fixed wait)
- Mouse wheel forwarded as PgUp/PgDn so claude's alt-screen scroll works
- Pane fully removed from layout on close (was leaving an empty placeholder)

### Added — Iter 3
- ANSI colour rendering via `QTextCharFormat` cache + custom 16-colour palette (bold/italic/underline/reverse honoured)
- Spinner animation + elapsed-time counter on `working` panes
- Project switcher combo in status bar (writes back to `projects.json`)
- "+ pane" button to open a default or custom role

### Added — Iter 4
- Window geometry + splitter sizes persisted via `QSettings`
- Role-aware default cwd resolution (frontend→web, backend→api, ...)
- `--append-system-prompt-file <role.md>` so specialist override applies even when cwd is the project root
- Event audit log at `runtime/events.log` (JSONL: spawn/assign/send/close/done)
- Cleaned redundant 2.7s close path in main_window

### Added — Iter 5
- Crash recovery: `_expected_exit` flag distinguishes user-close from claude crash; crashed panes show orange "exited" state with respawn affordance
- Spawn errors surfaced in status bar
- Font-size shortcuts inside terminal (Ctrl+= / Ctrl+- / Ctrl+0)
- Lead pane shows active project name in header (`Lead · pms`)
- Verified `takkub done` end-to-end (done → 2.5s grace → orchestrator.close → pane removed)

### Added — Iter 6
- Bottom dock `LogsPanel` that tails `runtime/events.log` every 1s
- F1 / `?` help dialog with `takkub` cheatsheet + shortcuts
- "⟶ assign" quick-assign button (role picker + multi-line task input)
- `takkub close-all` command (closes every teammate, keeps Lead)

### Added — Iter 7
- Session resume: `claude --continue` passed automatically on respawn within 5min in the same cwd
- Desktop notification (`QSystemTrayIcon`) when an agent calls `takkub done`
- Export pane buffer to `.txt` via `⤓` button in the header (`runtime/exports/<role>-<ts>.txt`)
- Per-role font size persisted in `QSettings`

### Added — Iter 8
- Pane header shows cwd basename (`Frontend · pms-web`)
- Status bar live count: active panes + working panes (2s tick)
- Auto-spawn presets per project (`projects.json` → `presets: ["frontend", "backend"]`)
- Logs panel: filter by event type + role substring

### Added — Iter 9
- Pane minimise/restore toggle (`▾`/`▸` button collapses the body to the header strip)
- Logs panel text search (case-insensitive substring across rendered line)
- Custom-role colour picker via `QColorDialog` in the "+ pane → custom..." flow
- README rewritten to reflect all current features

### Verification — Iter 9 (final)
- End-to-end multi-agent flow tested live with the real PMS project:
  - backend created `pms-api/src/health/health.controller.ts` + module wiring
  - frontend waited for backend's `takkub send` message before implementing `pms-web/app/agent-takkub-test/page.tsx` with Ant Design (agent inspected project conventions instead of using the suggested shadcn)
  - both agents called `takkub done`; both panes auto-closed without manual intervention
- Multi-agent peer-to-peer comms + auto-close lifecycle verified against `runtime/events.log`

[0.1.0]: https://github.com/takkub/agent-takkub/releases/tag/v0.1.0
