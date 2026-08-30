# Changelog

All notable changes to agent-takkub. Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [SemVer](https://semver.org/).

## [vNEXT]

## [v1.6.22] - 2026-08-30

### Fixed (แก้)

- **remote ส่งข้อความไม่ได้ ("cockpit ออฟไลน์" / Cloudflare 502) เมื่อ cockpit ถูก start จาก shell ของ pane ของ cockpit อีกตัว (#354 follow-up)** — จำลองเป็น user ด้วย Playwright ผ่าน tunnel: login ผ่าน, อ่านได้, แต่ `POST /api/lead/say` = 502 · ต้นตอ: process cockpit สืบทอด `TAKKUB_PORT_FILE`/`TAKKUB_ROLE`/`TAKKUB_LEAD_TOKEN` ของ pane → #354 กัน "เขียน" port file ถูกตัวแล้ว แต่ *client read* ใน process (`read_port()` ที่ remote `_lead_frame` ใช้) ยังเชื่อ override → ส่ง `send` ไป cockpit อีก instance → "unauthorized: send requires a valid pane token" → 502 → Cloudflare แทนด้วยหน้า 502 ของตัวเอง → PWA ขึ้นออฟไลน์ · `config.reconcile_inherited_pane_env()` ตอน boot: ตั้ง `TAKKUB_PORT_FILE` ใน env ให้ตรง port file ของตัวเอง + ลบ marker ของ pane ทิ้ง + log `inherited_pane_env_reconciled`

## [v1.6.21] - 2026-08-30

### Fixed (แก้)

- **แถบ "cockpit ออฟไลน์" บอกแล้วว่า request ไหนล้ม** — ต่อท้าย route + status/content-type (edge 5xx) หรือ error ของ fetch (`api/lead/say: Load failed`) ให้ screenshot เดียววินิจฉัยได้ · แตะแถบ = ลองเชื่อมต่อทันที · bump SW cache v34

## [v1.6.20] - 2026-08-30

### Fixed (แก้)

- **เปิดลิงก์ URL-only บนมือถือแล้วยังเจอหน้า pairing** — PWA ใช้ `base` เก่าจาก localStorage (secret path จากการจับคู่รอบก่อน) ยิง `/api/bootstrap` → 404 → pairing ทั้งที่ลิงก์ที่เปิดถูกต้อง · ตอนนี้ path ที่หน้าถูกเสิร์ฟอยู่เป็น base เสมอ (ไม่มี token หรือ base ไม่ตรงกับที่เปิด) · bump SW cache v33

## [v1.6.19] - 2026-08-30

### Added (เพิ่ม)

- **โหมด URL-only สำหรับ remote — เปิดลิงก์เปล่าแล้วกรอกรหัสผ่านเลย ไม่ต้องมี `#token=` (user request 2026-08-30)** — `RemoteConfig.url_only_auth` (default off) · เปิดใน Remote settings: checkbox "URL-only access" · server ข้าม bearer check (secret path ยังต้องตรง, **รหัสผ่านยังบังคับเหมือนเดิม**) · `/api/bootstrap` ตอบ `url_only: true` ให้ PWA รู้ว่า link คือ credential — เปิดแอปไม่มี token จะถาม bootstrap ก่อน ไม่เด้งหน้า pairing ทันที; หน้า pairing รับลิงก์เปล่าได้ · `pairing_url()`/QR เป็นลิงก์เปล่าในโหมดนี้ · bump SW cache v32

## [v1.6.18] - 2026-08-30

### Added (เพิ่ม)

- **ปุ่ม "🧹 ล้างแคช & โหลดใหม่" บน remote PWA (#445 follow-up)** — อยู่ท้ายหน้ารายการโปรเจคและบนหน้า pairing · ลบ cache ของ service worker + unregister SW แล้วโหลดหน้าใหม่จาก cockpit — **ไม่ลบ token/session** ไม่ต้องสแกน QR ใหม่ · ใช้เมื่ออัป cockpit แล้วมือถือยังทำตัวเหมือนเดิม (เคส 1.6.15→1.6.16 ที่ shell เก่าค้างใน cache) · bump SW cache v31

## [v1.6.17] - 2026-08-30

### Fixed (แก้)

- **remote ยังเด้งไปหน้า token หลัง 1.6.16 — cockpit ไม่เคยบอกว่า 404 มาจาก route ไหน (#445 follow-up)** — จำลอง flow สลับโปรเจคของ PWA ยิง prod ครบ 26 projects ทุก route (history/sessions/activity/pulse/sse-ticket/image) ด้วย token+session ที่ถูก = 200 หมด แต่มือถือยังเด้ง → ไม่มีหลักฐานฝั่งไหนเลย · server: ทุก bare-404 reject เขียน `remote_reject` ลง events.log (`reason` = bad_secret_path/bad_token/no_bearer/locked_out/image_unservable/sse_bad_ticket/… + `route` หลัง secret segment ตัด query ทิ้ง — ไม่มี secret/token/ticket, throttle 60/นาที) · PWA: หน้า pairing บอก route ที่ 404 ต่อท้ายข้อความ (`(404: api/lead/history)`) ให้ screenshot เดียวพอวินิจฉัย · bump SW cache v30

## [v1.6.16] - 2026-08-30

### Fixed (แก้)

- **fix #445 (1.6.15) ไม่ถึงมือถือ — remote ยังเด้งไปหน้า token หลังอัป (#445 follow-up)** — service worker ของ PWA เป็น cache-first (`cached || network`) และ 1.6.15 แก้ `app.js` โดยไม่ bump `CACHE_NAME` (ค้าง v28 ตัวเดียวกับ build ที่พัง) → มือถือที่ติดตั้งไว้แล้วรัน app.js เก่าจาก cache ต่อไป · bump เป็น v29 ให้ SW install ใหม่ดึง shell สดจาก server + เพิ่ม test pin ว่า key ต้อง ≥ v29 · กฎ: แตะ `app.js`/`index.html` ต้อง bump `CACHE_NAME` ทุกครั้ง

## [v1.6.15] - 2026-08-30

### Fixed (แก้)

- **remote PWA หลุดไปหน้า pairing "session หมดอายุ" ทันทีที่สลับโปรเจค (#445)** — `apiFetch` ถือว่าทุก 404 บน `/api/*` ขณะถือ token = token ถูกเพิกถอน (ดีไซน์ zero-surface ตอบ auth ผิดเป็น 404 เปล่า) แต่ `/api/image` ก็ตอบ 404 เปล่าเวลารูปเสิร์ฟไม่ได้ (screenshot ที่ paste จาก desktop อยู่ใน image cache ของ CLI นอก project root, ไฟล์ถูกลบ) → history ที่มีรูปแบบนั้นสักรูปทำให้ `forgetToken()` ล้าง pairing ทั้งเครื่อง · `fetchImageBlob` ส่ง `allow404: true` — รูปหายเป็นแค่การ์ดหาย; route อื่นและ server ไม่เปลี่ยน · กระทบทุก install ตั้งแต่ #424 ไม่เกี่ยวกับ tunnel

## [v1.6.14] - 2026-08-30

### Fixed (แก้)

- **claude trust dialog default เป็น "No, exit" แล้ว auto-trust กด Enter ปิด pane ทิ้ง (#443)** — claude รุ่นใหม่ตั้ง cursor ที่ "No, exit" เมื่อโฟลเดอร์มี `.claude/settings.local.json` ที่ pre-approve permission (ทุกโปรเจคที่ import ใหม่) → `_auto_trust` เคยกด Enter เปล่าๆ = เลือก No แล้ว claude ปิดตัว หน้าจอค้างเป็นภาพ dialog กดอะไรไม่ไป · เพิ่ม `PtySession.trust_prompt_selects_no()` ตรวจแถวที่ cursor อยู่ แล้วกด ↓ ก่อน Enter ใน tick ถัดไป
- **done digest ค้างใน durable queue 7 ชม. จน user พิมพ์เอง (#440)** — draft-state ของ Lead pane ค้าง non-empty (Up/Down/paste ฝั่ง user ทำให้เข้า `unknown_nonempty` แล้วไม่มีทางออกจนกว่าจะมี Enter/Esc) → `_flush_pending_done_notices` ปฏิเสธตลอด · `_lead_can_accept_injection` reset state เมื่อหน้าจอแย้ง (claude composer ว่างจริงตาม #343 structural check) หรือค้างเกิน `DRAFT_HOLD_FORCE_RESET_S` (30 นาที) ที่ ready prompt + log `lead_draft_state_reset`
- **pane guard บล็อก push branch ของ worktree ตัวเอง + ตัดสิน isolation จาก cwd ของ hook อย่างเดียว (#438)** — worktree pane push ได้เฉพาะ `wt/<role>-<ts>` ของตัวเองแบบระบุชื่อ (`git push -u origin wt/...` ห้าม force/delete/`:`/`+`) เพื่อให้ CI ตรวจก่อน done · คำสั่งที่ระบุ `git -C <worktree>` หรือ `cd <worktree> &&` นับเป็น worktree แม้ cwd เป็น shared tree (pane ที่ respawn ไม่มี flag แต่ทำงานใน checkout เดิม)
- **`takkub worktree merge --role` เดา "ล่าสุด" เองเมื่อ role มีหลาย worktree (#439)** — ปฏิเสธแล้ว list `--branch` ให้เลือก (พร้อม dirty/commit ahead) · `--latest` ถ้าจะเอาใหม่สุดจริง · `takkub worktree clean --branch wt/...` ลบตัวเดียว
- **merge proposal ไม่เตือน conflict กับ base — Lead รู้ตอน merge ล้มแล้ว (#442)** — `merge_conflict_files()` (`git merge-tree --write-tree --name-only`) ใส่ชื่อไฟล์ที่จะชนใน proposal ตอน done · `takkub worktree merge` ปฏิเสธพร้อมชื่อไฟล์ก่อนแตะ tree · `--check` = dry-run
- **UI ค้าง 1.5–18 s จาก psutil บน Qt main thread (#437)** — console sweeper (ซ่อนหน้าต่าง console ของ process ลูก) เคยเป็น QTimer 250 ms บน main thread แล้วเดิน parent chain ผ่าน psutil ทีละ hop; ตอน process แตกเยอะ (codex เปิด pwsh ทุก tool call, pytest-xdist, pre-commit) = 5/8 stack dump ของ main_thread_stall → ย้ายเป็น daemon thread `console-sweeper` (หยุดที่ aboutToQuit) + จำกัด 16 หน้าต่างใหม่ต่อรอบ · resource governor เคยเรียก `psutil.cpu_percent`+`psutil.pids()` (เดิน process table ทั้งเครื่อง) ใน tick ของ GUI → `BackgroundSampler` อ่านบน worker thread ทุก 2 s, tick อ่านค่า cache อย่างเดียว

### Added (เพิ่ม)

- **redact secret ที่ cockpit ส่งต่อ (#441)** — module ใหม่ `secret_redact.py` (pure, ทุก provider): ค่าใน `KEY=value` ที่ชื่อ key เข้า pattern `*SECRET/*PASSWORD/*TOKEN/*API_KEY/*PRIVATE_KEY/*_KEY` และ literal ทรง JWT/`ghp_`/`sk-`/`xox?-`/`AKIA`/PEM ถูกแทนเป็น `<redacted:NAME>` (เก็บชื่อ key ไว้ debug) ก่อนเข้า done digest / merge proposal / `takkub send` / remote mirror · ต่อท้ายบรรทัดเตือน + event `forwarded_secret_redacted` (ไม่ log ค่า) · placeholder อย่าง `${VAR}`/`<paste-here>`/`changeme` ไม่โดน

## [v1.6.13] - 2026-08-29

### Added (เพิ่ม)

- **`takkub qa-gate --auto` เลือก tier จาก diff จริง (#436, user directive)** — อ่าน `git diff --name-only` (tree+index+untracked, tree สะอาด = HEAD~1..HEAD) แล้วพิมพ์ tier + เหตุผลเป็นแถวแรกของตาราง: แตะแค่ .css/asset/font/i18n json/md = `style` **ไม่รัน test suite เลย** (Node: typecheck/verify เท่านั้น, test/lint skip) · module logic = `targeted` pytest บนไฟล์ `tests/test_<name>*.py` ที่ map ได้ (Node: typecheck+test เพราะ narrow ไม่ได้ #368) · แตะ api/auth/schema/prisma/shared/lib/lockfile/tsconfig/ci/docker/config = `full` · source ที่ไม่มีเทส map → widen เป็น full ไม่เดา · CLAUDE.md + role frontend/mobile/qa + cli-reference บอกให้ specialist ใช้ `--auto` ก่อน `done` ทุก provider
- **`takkub ma` เช็ค GitHub code-scanning alert** — หัวข้อใหม่ดึง `gh api .../code-scanning/alerts?state=open` เรียงตาม severity แสดง path:line + URL; repo ไม่เปิด scanning = skip, `--no-net` = skip; แผน "ทำต่อ" เพิ่มข้อ fix/dismiss เฉพาะตอนมี alert (ก่อนหน้านี้ sweep บอก "พร้อม ship" ทับ alert high #43)

### Changed (เปลี่ยน)

- **qa-gate report ออกจาก repo** — full gate เขียน report ไป `<DATA_HOME>/runtime/qa-reports/` (ที่เดียวกับ events.log/qa-plans) แทน `docs/qa/` และ log ของแต่ละ step ไป `<DATA_HOME>/runtime/exports/` แทน `<repo>/runtime/` (ไม่สร้าง dir แปลกในโปรเจค Node) · ลบไฟล์ auto-generate `docs/qa/*-qa-gate*.md` 53 ไฟล์ — `docs/qa/` เหลือเฉพาะรายงานที่ qa pane/คนเขียน

### Fixed (แก้)

- **CodeQL #43 `py/overly-permissive-file`** — `resource_lock._write_exclusive` สร้าง lock file ด้วย mode 0o600 แทน 0o644 (lock มีแค่ pid+role ของ user เอง)

## [v1.6.12] - 2026-08-29

### Fixed (แก้)

- **gemini pane พิมพ์ `takkub done` เป็นข้อความแทนรันคำสั่ง → ledger ค้าง working (#435)** — idle watchdog อ่านจอ (provider-neutral): ถ้า pane idle แล้วบนจอมี `takkub done` เป็น text โดยไม่มี `ok:`/`err:` ตามหลัง → inject nudge ให้รันเป็น shell command จริง (cooldown 10 นาที, event `done_typed_as_text`)

## [v1.6.11] - 2026-08-29

### Added (เพิ่ม)

- **Remote: path รูปใน Lead reply แสดงเป็นรูปจริง + lightbox zoom (#434)** — path (absolute/relative/ใน quote/backtick · png/jpg/webp/gif) ที่ Lead พิมพ์ถึง กลายเป็น image card ใต้ข้อความ แตะ = lightbox บีบนิ้ว/ล้อ zoom · ลาก · double-tap · ไฟล์ส่งผ่าน `GET /api/image` (bearer+password, จำกัดใต้ project cwd / RUNTIME_DIR, เช็ค ext+magic+ขนาด, ทุก reject = 404 เปล่า) · compact summary ของ Claude Code แสดงเป็น pill บรรทัดเดียวแทนกำแพงข้อความ · resume picker กรอง session teammate ที่ spawn แบบ one-shot (first line = spawn trigger) เหลือแต่ Lead
- **`takkub spawn-service --name <n> -- <cmd>` / `--list` / `service-stop` (#429)** — service ที่ต้องอยู่รอด `done`/`close` (cloudflared, dev API) ให้ cockpit spawn นอก job/tree ของ pane (Windows DETACHED|BREAKAWAY_FROM_JOB · POSIX setsid) log ที่ `runtime/services/<project>/<n>.log` · single-instance kill ของ cockpit ข้าม PID เหล่านี้
- **`takkub lock <name> [--wait s]` / `unlock` / `lock --list` + `takkub kill --role <r> [--pid N]` (#430)** — advisory lock ต่อ project รอบ shared build dir (ไฟล์ใน `runtime/locks/` ใช้ได้แม้ cockpit ค้าง, ติด = exit 3 บอก role ที่ถือ, TTL 30 นาที) · Lead ฆ่า process ใต้ pane อื่นได้โดยไม่ต้องไล่ PID (audit `pane_children_killed`) · role docs devops/qa บังคับ wrap build
- **งาน UI จบในรอบเดียว (#433, user directive)** — frontend/mobile เป็น browser role แล้ว ต้อง self-verify ด้วย screenshot จริง (390px + 1440px, path ใน done note) · `done` ของ task ที่เป็น UI ถูกปฏิเสธถ้าไม่มี path ภาพจริง/note บอก "ยังไม่ได้เปิดจริง/route ไป qa" (`orchestrator_text.ui_evidence_gate`, opt-out `[no-ui]`, `--force`) · qa เหลือ regression/e2e/cross-model · role file + role-and-workflow.md อัปเดต ใช้ทุก provider

### Fixed (แก้)

- **codex pane: task ยาว ~1.5KB ถูกตัดกลางคัน ไม่กด Enter (#424)** — codex ไม่มี file-read tool จึงไม่ได้ pointer handoff (#273) ถูก paste ทั้งก้อน → writer แตก paste เป็นก้อนละ 300 ตัวอักษร ห่าง 60ms (`ProviderSpec.paste_chunk_chars/paste_chunk_delay_ms` เฉพาะ codex) ตัดที่ขอบ escape sequence/ตัวอักษร ไม่ตัดกลาง marker หรือ UTF-8
- **`takkub report publish --name <ไม่มีนามสกุล>` → 'unsupported extension' (#425)** — ใช้นามสกุลของไฟล์ต้นทางให้อัตโนมัติ
- **auto-issue `stuck_pane_recover` เปิดใบจาก chain เดียว (#427)** — devops ×3 ใน 20 นาที = close→respawn→ยังค้างจนถึง STUCK_RECOVER_MAX คือปัญหาเดียว → rule นับเป็น chain ต่อ pane (pane เดิมภายใน 15 นาที = 1)
- **`takkub wait` (#428 #431)** — `--role a,b,c` แตกเป็นหลาย role ทั้ง CLI/server · role ที่ไม่เคย spawn → ok=False exit 2 (เดิม "all roles resolved") · orchestrator bridge timeout (main_thread_stall 16.6s ตอน worktree merge) retry 3 ครั้งก่อนตาย · wait ที่พังเอง exit 2 แยกจาก exit 1 (role ยัง pending) · ESC-led chunk ภายใน 5s หลัง cockpit เขียน digest/notice เข้า Lead PTY = terminal reply ไม่ใช่ user input (`lead_user_input_suppressed_post_inject`) — ไม่ interrupt wait เพราะ notice ของ cockpit เองอีก
- **`takkub close --role X` หลัง worktree merge ตอบ 'no pane open' exit 2 บางครั้ง (#432)** — role ที่รู้จักแต่ไม่มี pane = ok no-op; ชื่อผิดยัง error

## [v1.6.10] - 2026-08-28

### Fixed (แก้)

- **Usage/limit panel: Gemini แสดง 'อัปเดตเมื่อ ~6 เดือนก่อน' ทั้งที่ pane gemini รันอยู่ (#423)** — ค่าที่อ่านได้คือ cache ของแอป Antigravity (`~/.antigravity_cockpit/cache/quota_api_v1_plugin`, updatedAt 27 ก.พ. จริง) เพราะ agy CLI ไม่มี usage API/ไม่เขียน cache นี้ — adapter รู้และส่ง `GEMINI_STALE_HINT` มาแล้ว แต่ Remote UI แสดง hint เฉพาะ status unsupported/error → การ์ด stale เห็นแค่เวลาเก่า · แก้: การ์ด stale แสดง hint ด้วย + เขียน hint ให้ตรง ("agy CLI ไม่มี usage API — ตัวเลขนี้คือ cache ของแอป Antigravity ครั้งล่าสุด")

## [v1.6.9] - 2026-08-28

### Added (เพิ่ม)

- **#422 reliability contract (cherry-pick จาก roadmap review)** —
  (1) ทุก `*_pane_recover` event มี `reason` เป็น enum ปิด (`orchestrator_text.RECOVERY_REASONS`: `content_static` · `idle_no_response` · `child_alive_grace_expired` · `no_first_content[_retry_failed]` · `auth_failed` · `account_pending`) + `snapshot` (5 บรรทัดท้ายจอหลังกรอง spinner, วินาทีตั้งแต่ byte/content/assign ล่าสุด, child process) + `recovery_id` ที่ event `*_recover_respawn`/`*_pane_respawned` ใช้ร่วม → `takkub ma` สรุป "เหตุผล recovery: idle_no_response ×2 · …" ได้เลย ไม่ต้องไล่ log มือเหมือนตอนปิด #418 ·
  (2) **ProviderSpec capability matrix** (`provider_spec.capability_matrix/capability_state`, 12 capability × supported/partial/unsupported/experimental) derive จาก flag ที่ engine branch จริง (ไม่พิมพ์มือ ไม่ drift) + `capability_overrides` ต่อ provider · `takkub doctor` เพิ่ม `[provider-capabilities]` INFO ต่อ provider · engine log `provider_capability_fallback` ทุกครั้งที่เดินทาง degraded (skills → instruction bridge บน provider ที่ไม่มี Skill tool, resume ถูกปฏิเสธ) — ห้าม fallback เงียบตามกฎ multi-provider ·
  (3) `done`/`close` event มี `project` + `session_uuid` ให้ correlate กับ recovery chain ·
  (4) **`takkub skills list [--global] [--project X]`** (ชื่อ · scope global/project/built-in/repo · path — resolve junction แล้ว, global ที่ยังไม่ถูก link ก็แสดง) และ **`takkub skills effective --role R [--provider P]`** (Skill Matrix × bridge ของ provider: native Skill tool vs instruction-only, ชี้ skill ที่ assign แต่หาไฟล์ไม่เจอซึ่งเดิมถูก drop เงียบตอน spawn) · tests 22 ตัว

- **Global skills — skill ส่วนกลางระดับ cockpit ที่ทุกโปรเจค + ทุก provider เห็น (#419)** — store ใหม่ `DATA_HOME/skills/<name>/SKILL.md` (`config.GLOBAL_SKILLS_HOME`) ใช้ surface เดียวกับ project-skills: `ensure_project_skill_links` junction/symlink ทุก skill กลางเข้า `<project>/.claude/skills/` ตอนเปิด tab **และตอน spawn** (skill ของ project ชื่อซ้ำชนะ ไม่ clobber) → claude/codex/agy ที่ discover จาก cwd เห็นเหมือน skill project, provider ที่ใช้ instruction-bridge (Skill Matrix appendix) ก็ scan เจอจาก root เดียวกัน · Settings › New Skill มี checkbox **Global skill** · `takkub migrate-skills --to-global <name>` ยก skill จาก central ของ project ขึ้นชั้นกลาง (refuse ถ้าชื่อซ้ำ/ยังไม่อยู่ใน central) · `takkub assign --cwd <GLOBAL_SKILLS_HOME>` ผ่าน allowlist แล้ว Lead delegate การเขียน skill กลางได้ตามกฎ (ไม่ต้อง Write เอง) · tests 10 ตัว

### Fixed (แก้)

- **พิมพ์ใน Lead pane แล้ว echo ช้า / กด Enter ไม่ขึ้นทันที (prod 1.6.8, saas_admin 5 pane)** — `py-spy record` ตัว cockpit ตอนอาการเกิด: 32% ของ sample ทั้งหมดอยู่ใน `graft_autobuild._sync_staging`/`_stat_or_none` — `resync_staging_only()` จาก idle-watchdog tick 5 วิ ของทุก pane (throttle 15 วิ/cwd) เดิน `git ls-files` + stat ไฟล์ทั้ง project (2,243 ไฟล์ ×2) + `os.walk` staging ใน Python thread ทุกรอบแม้ไม่มีอะไรเปลี่ยน → GIL convoy แบบเดียวกับ `ram_report` (1.6.4) main thread ตอบ keystroke ช้า · ไม่ใช่ main_thread_stall (≤1.7s ห่างๆ) ไม่ใช่ CPU peg
  → แก้: `_tree_fingerprint()` = sha256(`git rev-parse HEAD` + `git status --porcelain -z -uall`) ต่อ target (subprocess, ไม่ถือ GIL, ~50ms) — fingerprint เท่าเดิม + staging มีอยู่ → ข้ามทั้งรอบ; None (ไม่มี git/ไม่ใช่ repo) → เดินเต็มเหมือนเดิม · tests 3 ตัว
- **done digest ติดป้าย `⚠️ [unverified origin — <role> respawned since queued]` ทุกใบทั้งที่ pane ไม่ได้ respawn (#421)** — trace จาก events.log: `done 14:46:54 → close 14:46:56 → lead_inbox_digest 14:46:57` — `done()` ปิด pane อัตโนมัติก่อน digest debounce ยิง ทำให้ `_current_pane_identity` เป็น None แล้ว `_provenance_stale` fail-safe เป็น stale=True ทุกครั้ง → ตอนนี้ "ไม่มี pane live" + token เป็นตัวที่ cockpit นี้ mint เอง (`_pane_token_minted_at`) = pane ปิดตามปกติ ไม่ติดป้าย; token ที่ไม่เคย mint (replay/forge/cockpit restart) ยังติดป้ายเหมือนเดิม (#228 true-positive คงอยู่) · tests 2 ตัว
- **`takkub wait` จบด้วย "interrupted by user input" ซ้ำ 3 ครั้งติด (4s/16s/36s) ทั้งที่ inbox ว่างและไม่ได้พิมพ์ (#420)** — ทางเดียวที่ stamp `_lead_last_user_input_ts` ได้คือ byte จาก xterm.js ของ Lead pane; filter auto-reply เดิม (#357) รู้จักแค่ CPR/DA/DSR/OSC/focus/paste แต่ไม่รู้จัก reply ที่ TUI สมัยใหม่ query: DECRPM `ESC[?2026;2$y` (synchronized output), kitty keyboard `ESC[?0u`, XTWINOPS `ESC[8;r;ct`, DCS `ESC P … ESC \` (XTVERSION/DECRQSS) → เพิ่มทั้ง 4 token · เพิ่ม event `lead_user_input_stamp` (repr 64 byte แรก) เฉพาะ chunk ที่ขึ้นต้นด้วย ESC และผ่าน filter — รอบหน้าถ้ายังหลุดจะเห็นลำดับ byte ตัวจริงใน events.log แทนต้องเดา · tests +10 case

## [v1.6.8] - 2026-08-27

### Fixed (แก้)

- **codex pane ค้างหน้า `Starting MCP servers (0/3)` ~110 วิทุก spawn ทั้งที่ MCP server พร้อมใน 2-3 วิ (#416)** — log ของ codex เอง (`logs_2.sqlite`) ยืนยัน server ทุกตัวตอบ `initialize` + `startupStatus` ครบใน ~2 วิ แต่ TUI ใน pane ไม่ repaint/ไม่โชว์ prompt จนกว่าจะมี input ใดๆ เข้ามา (พิสูจน์สด: `takkub send "."` ที่ +30 วิ → "Working" ทันที ข้อความเข้า composer) → delivery รอ marker จน blind-paste 90 วิ / boot-stall 110 วิ; reproduce นอก cockpit (ConPTY เดียวกัน, argv/env/cwd เดียวกัน) ไม่ค้าง — trigger ใน pane ยังไม่ชี้ตัว
  → แก้: knob ใหม่ `ProviderSpec.boot_splash_paste_after_s` (codex 10 วิ, env `TAKKUB_BOOT_SPLASH_PASTE_AFTER_S_CODEX`, provider อื่น 0 = เดิม) — `_send_when_ready` เห็น MCP splash + ไม่มี modal + ผ่าน 10 วิหลัง session ขึ้น → paste+Enter เลย (event `task_deliver_boot_splash_paste`) แล้วเข้า acceptance-verify/re-deliver ปกติ · E2E บน dev: codex qa (3 MCP) task ถึงมือ **12 วิ** (เดิม 109-113 วิ), gemini ไม่กระทบ (21 วิ ตาม #404 เดิม)

## [v1.6.7] - 2026-08-27

### Fixed (แก้)

- **`takkub close --role <role>#N` ตอบ `unknown role` ปิด pane instance ไม่ได้ (#409)** — pane `frontend#4` ที่ยังค้างคิว resource governor (assign ตอบ ok แล้วแต่ spawn ยังไม่รัน) ไม่มี entry ใน pane map → close คืน unknown role ทั้งที่ `status` โชว์ queued อยู่
  → แก้: `close()` หา pane ไม่เจอ → fallback `_cancel_queued_resource_task()` (helper เดียวกับ `task cancel` #303) ยกเลิกคิว + ปิด ledger · role ที่ไม่รู้จักจริงยังได้ unknown role เหมือนเดิม + 4 tests
- **done digest หลัง Lead restart = `merge:ไม่ทราบ · snapshot ตอน assign ไม่ครบ` ไม่มี merge proposal (#410)** — `snapshot_state()/restore_teammates()` ไม่เคย persist `PaneState.worktree` / `assign_base_sha` / `assign_git_root` / `assign_dirty_snapshot` → restart คร่อม assign→done ทำ bookkeeping หายจาก memory
  → แก้ 3 ชั้น: persist+restore ครบใน session snapshot · `WorktreeManager.rediscover_worktree()` สร้าง branch/git_root/base_sha (merge-base กับ HEAD) จาก `git worktree list` เมื่อ bookkeeping หาย (รันใน `collect_done_git_facts` นอก Qt thread ตาม #408) · `takkub wait` ที่ตอบ "no longer active" หลัง restart แนบคำสั่ง `takkub wait --role …` ให้ resume (ไม่ auto re-arm เพราะแยกจาก `--cancel` ไม่ได้) + 15 tests
- **`takkub worktree clean` เก็บกวาดไม่จบในรอบเดียว (#411)** — ทิ้ง `.trash-*` (node_modules 518MB) ต้องรัน `--orphans` ซ้ำ · `--force` ลบ dir แต่ branch `wt/*` ค้าง · dir ว่างเหลือหลัง `--orphans`
  → แก้: `sweep_trash()` ลบ `.trash-*` ทุกรอบ clean (ของที่ผ่าน safety check มาแล้ว) · `branch -D` เช็คผล+retry · `--orphans` verify ซ้ำหลังลบ · `_rmtree_long_path_safe` retry 3× กัน Windows file-lock ชั่วคราว + 10 tests
- **watchdog เตือน Lead "ค้าง 1228s" ทั้งที่ idle รอ user · heavy_project_limit บล็อกเงียบ · done เตือน vite build ที่จบแล้วจะถูก kill (#412, รายงานจาก macOS/opencode)**
  → watchdog: Lead ได้ grace 30 นาที (`LEAD_DONE_IDLE_GRACE_S`) หลังทีมทุกคนเลิก working ก่อนนับ stale-marker (provider-agnostic ไม่พึ่ง marker text) · status/list: `display_state` tier ใหม่ `queued:<reason>` + บรรทัด `⏳ <เหตุผล>` ครอบ re-assign บน pane เดิมที่ถูก governor คิว (เดิมหายเงียบ) · shard fan-out โชว์ reason ของ shard ที่โดนบล็อก · `_live_non_scaffolding_children` ข้าม zombie (POSIX `STATUS_ZOMBIE`) / re-check `is_running()` (Windows) ก่อนเตือน + 14 tests
- **`takkub mcp deny --role qa chrome-devtools` ตอบ ok แต่ allowlist ไม่เปลี่ยน (#414, รายงานภายนอก)** — `deny_item()` บน role ที่ยังไม่มี override ถือว่า "ไม่มีอยู่แล้ว" คืน True โดยไม่เขียน ทั้งที่ effective list มาจาก built-in default
  → แก้: materialize default ก่อนลบ (เหมือน `allow_item`) · `cmd_mcp`/`cmd_plugins` verify effective list หลัง write — ไม่เปลี่ยน = `ok=False` บอกชัด · typo `denyed`→`denied` · ข้อความ deny บอกว่าบังคับได้เฉพาะ claude/codex (gemini/opencode/kimi/cursor ไม่อ่าน policy — gap เดิม #103/#121)
- **test hygiene** — suite รั่ว `context/last_context_trace.json` ลง repo root (`config.DATA_HOME == REPO_ROOT` บน dev checkout ไม่ถูก isolate) → conftest isolate `DATA_HOME`+`REPO_ROOT` ทั้ง suite + session guard fail ถ้ามี leak · `test_doctor` mock ขาด `check_rtk_ripgrep` แดงบนเครื่องไม่มี `rg` (#413) + drift guard derive จาก `run_all_checks` · `test_subprocess_no_window_guard` false-positive กับ GitRunner param ชื่อ `run` → rename `git_run` · `test_lead_project_rules` flaky บน windows CI (#415) = fixture ไม่ isolate `lead_context.RUNTIME_DIR` → xdist worker แย่งเขียน `lead-context.md` ไฟล์เดียวกัน

## [v1.6.6] - 2026-08-26

### Fixed (แก้)

- **codex / gemini / opencode ที่เปิดจาก IDE คิดว่าตัวเองเป็น pane ของ cockpit** — spawn ปลูก `AGENTS.md` (takkub-managed) ไว้ที่ root โปรเจคเพื่อให้ provider ที่ไม่ใช่ claude discover เอง แต่**ไม่เคยมีโค้ดลบ**
  → พบจริง 18 ไฟล์ค้างใน 14 โปรเจค (รวม `GEMINI.md` จากเวอร์ชันเก่า) CLI ที่เปิดเองในโปรเจคนั้นอ่านเจอ "You are running inside an agent-takkub pane" · config home แยก (`CODEX_HOME`/`CLAUDE_CONFIG_DIR`) ช่วยไม่ได้เพราะไฟล์อยู่ในโปรเจค
  → แก้: `codex_agents_md.remove_managed_context_files()` ลบเฉพาะไฟล์ที่บรรทัดแรกมี marker `takkub-managed` (ไฟล์ของ user ไม่แตะ) เรียกจาก `close()` / exit ไม่คาดคิด (เมื่อไม่มี pane อื่นใช้ cwd เดียวกัน) และตอนปิด cockpit (`release_all_planted_context`, รวม Lead) · spawn ครั้งหน้าปลูกใหม่เอง
  · **`takkub cleanup agents-md [--dry-run] [--yes] [--path DIR]`** กวาดของเก่าทุก project path ที่ลงทะเบียน — รันหลังอัป 1 ครั้ง

## [v1.6.5] - 2026-08-26

### Fixed (แก้)

- **Lead ค้าง 3–30 วิ ตอน `assign --isolation worktree` / `done` (#408)** — คลาสที่ 1.6.4 ยังไม่ได้แก้: `git worktree add` (assign) และ `merge-tree` / `diff --stat` / `status` (digest + merge proposal ตอน done)
  รันบน Qt main thread ตรงๆ (boot.log SOFT-stall dumps 10 ครั้ง) → `cli_server` ย้ายไป worker thread: `Orchestrator.worktree_assign_inputs()` / `done_git_inputs()` (อ่าน state เท่านั้น) → thread รัน
  `WorktreeManager.create()` / `collect_done_git_facts()` → ส่งกลับ main thread ผ่าน `_mainThreadCall` signal → `assign(worktree_prepared=…)` / `done(git_facts=…)` (`_compute_digest_facts` ข้าม git ทุก call เมื่อมี facts)
  · worktree ที่เตรียมไว้ตาม governor deferral ไปด้วย ไม่ถูกทิ้งเป็น orphan · orchestrator ที่ไม่มี hook (test fakes) / pane ไม่มี state → path เดิม synchronous ทุกอย่าง
- **QA/Codex pane ถูกตัดที่ 300s ทั้งที่ยังอยู่ใน MCP startup budget ของตัวเอง (#407)** — `BOOT_STALL_CEILING_SEC` 300 ถูกตั้งก่อนยุค MCP injection (startup 120s/server) → spawn จำ `PaneState.mcp_server_count`
  จาก argv จริง (`describe_mcp_handshake`) → ceiling = `max(300, 110 + 120 × N)` (`MCP_STARTUP_TIMEOUT_SEC`, env `TAKKUB_MCP_STARTUP_TIMEOUT_SEC`) ทุก provider เท่ากัน (#103) · pane ไม่มี MCP = 300 เท่าเดิม
  · notice boot-timeout FAILED บอกด้วยเมื่อ RAM ว่างต่ำกว่าเส้น governor (เคส #407 ว่าง 11%) — cold start ช้าเพราะเครื่อง ไม่ใช่ pane เสีย

## [v1.6.4] - 2026-08-26

### Fixed (แก้)

- **Lead pane พิมพ์แล้วไม่ขึ้น / ค้างเป็นวินาที (main_thread_stall 500–977 ครั้ง/วัน ตั้งแต่ ≤ 08-23, รวมค้าง 15–39 นาที/วัน)** — root cause ไม่ใช่เครื่องอืด (RAM ว่าง 11GB, disk idle) แต่เป็น **GIL starvation**:
  RAM chip (`ram_report.collect_ram_report`, #364 lever 6) เรียก `psutil.Process.ppid()` ทีละ descendant — บน Windows psutil สร้าง `ppid_map()` ของ**ทั้งเครื่อง**ใหม่ทุก call ใน C ext ที่ไม่ปล่อย GIL
  (28 descendants × 381 procs) → ถือ GIL 0.7–2 วิ ทุก 15 วิ (`py-spy --gil` = 51% ของ GIL samples; `py-spy dump` ระหว่าง stall เห็น thread นี้ถือ GIL คนเดียวขณะ main thread รออยู่ใน `EnumWindows`/`Path.stat`)
  — การย้ายไป QThreadPool worker ไม่ช่วยเพราะ C call ถือ GIL ข้าม thread → แก้: snapshot `ppid_map()` **ครั้งเดียว** (0.27s → 0.02s ต่อรอบ) · stall ยาว 3–30 วิ จาก `git` subprocess บน main thread (assign/done/diffstat) เป็นคนละคลาส ยังไม่แก้ในรอบนี้
- **Chrome ที่ cockpit เปิดให้ qa/critic/designer ค้าง ~500MB (8 process, about:blank) หลัง pane ปิด จนกว่าจะปิดแอป (#406)** — `close_native_chrome` เคยถูกเรียกแค่ตอน shutdown → เพิ่ม `_schedule_native_chrome_idle_release`:
  เมื่อ pane browser ตัวสุดท้าย (non-shard, #92) ปิดหรือ exit ไม่คาดคิด → รอ grace 60 วิ (กัน stuck-watchdog close→respawn 2 วิ / done→assign ถัดไป) → เช็คซ้ำว่ายังไม่มี pane ใช้ → `taskkill /T` บน daemon thread (ไม่บล็อก Qt) · spawn ใหม่ระหว่าง grace = ยกเลิกอัตโนมัติ
- **QA pane ค้างที่ graft MCP boot ไม่มี timeout/fallback (#405)** — claude `.mcp.json` ไม่มี `startup_timeout_sec` แบบ codex → ใส่ env `MCP_TIMEOUT=120000` (startup handshake ceiling ของ Claude Code) ให้ทุก pane ที่ operator ไม่ได้ตั้งเอง
  = ค่าเดียวกับ `_CODEX_DEFAULT_STARTUP_TIMEOUT_SEC` (#351) → server ที่ไม่ตอบใน 2 นาทีถูก mark failed แล้ว pane boot ต่อ · gemini ไม่มี startup knob (settings.json `timeout` = per-request) → gap #103

## [v1.6.3] - 2026-08-26

### Fixed (แก้)

- **watchdog เตือน "ไม่ active" / idle_reminder ทุก 90 วิ / harvest_hint +10 นาที ใส่ pane ที่กำลังทำงาน (#391 #394 #395 #398)** — regression จาก 1.6.2 (d047ab4):
  Claude Code รุ่นปัจจุบันวาง `esc to interrupt` บนบรรทัด footer **เสมอ** (busy หรือไม่ก็ตาม) และ spinner ไม่มี `(esc to interrupt)` อีกแล้ว → การตัดทั้งบรรทัด footer
  ออกจาก blocker scan ทำให้ไม่เหลือ busy indicator เลย → แก้: ตัดเฉพาะ substring `esc to interrupt` และเฉพาะเมื่อมีหลักฐาน background จริง (`← for agents` / `N shell` / `N agent`)
  + รู้จัก spinner line เปล่า (`✻ sock-hopping… 3`) เป็น busy + `PtySession.has_background_work()` แยกคำถาม "ready" กับ "ยังมีงาน background" · idle loop/harvest/compact เช็ค tool marker + progress ledger ก่อนเตือน
- **`takkub wait` ถูกตัดซ้ำด้วย user_input เดิม (#393 #396)** — `begin_wait` attach ใช้ `started_ts` เดิม → stamp เดียว interrupt ทุก 4 วิ → latch `_wait_user_input_ack_ts` ต่อ project
- **pane ตายเงียบ / boot ไม่ทัน (#387 #397)** — pane exit ไม่มี done-report แจ้ง Lead + snapshot 40 บรรทัดสุดท้ายไว้ `runtime/sessions/<date>/<project>/<role>-last-output.txt` · boot-ceiling reprobe เมื่อ main thread stall
- **gemini/agy รับ task ไม่ได้เพราะ paste เร็วกว่า account verification (#404 — user directive)** — `ProviderSpec.post_boot_settle_s` (gemini = 8 วิ, override `TAKKUB_POST_BOOT_SETTLE_S_GEMINI`) รอหลัง boot ก่อน paste+Enter, เห็น banner "verifying your account" = reset นับใหม่, paste แล้ว banner กลับมา = re-deliver อัตโนมัติสูงสุด 3 ครั้งแล้วแจ้ง Lead
- **pane_guard (#399 #400)** — rule ใหม่ `host_network` บล็อก `netsh wlan connect` / `ipconfig /release` / `route add` / `networksetup -set*` ฯลฯ (host network เป็นของ user) + แจ้ง Lead เมื่อบล็อก · git-block บอก pane ให้ `takkub done "พร้อม commit: …"` แทน retry วน · role files 16 ไฟล์ + docs อัปเดต
- **`takkub worktree merge --role backend` merge ของ `backend#3` แทน (#403)** — role match เป็น `^wt/{slug}-(\d+)$` แบบ exact
- **qa-gate / doctor / conftest (#388 #401 #402)** — status `ENV_GAP` (exit 78) แทน FAIL ปลอมเมื่อ env ขาด, fallback unittest, `--exec`, wheel-build fixture ล็อกร่วมกัน, `doctor` เช็ค `rtk`+`rg`
- **report แนบไฟล์ binary + ส่งเข้าห้องแชท (#389 #390 #392)** — `.docx .xlsx .pptx .zip .csv .txt` เป็น `Content-Disposition: attachment` (cap 50MB), `report publish --send` / `send --file` ส่งไฟล์ไป remote channel (fallback เป็นลิงก์)
- **CodeQL #41 `py/http-response-splitting`** — `Content-Disposition` ประกอบจาก `path.name` ที่ resolve แล้ว + whitelist `[A-Za-z0-9._-]` ใน helper เอง

## [v1.6.2] - 2026-08-25

### Fixed (แก้)

- **Lead ถูกอ่านว่า "กำลังทำงาน" ทั้งที่ว่าง ตลอดเวลาที่มี background agent/shell** (พบจริง 2026-08-25 — notice #343 "lead เงียบต่อเนื่อง 24s"
  เตือนผิดทุก cooldown) — Claude Code รุ่นใหม่โชว์ `· esc to interrupt · ← for agents` บน**บรรทัด footer** (`⏵⏵ bypass permissions on
  (shift+tab to cycle) · esc to interrupt · ← for agents` / `· 1 shell · esc to interrupt ·`) ขณะช่องพิมพ์ idle รับ input ได้ แต่ cockpit ใช้
  `esc to interrupt` เป็น hard blocker → Lead delivery รอทั้งที่ pane ว่าง, compact idle episode ถูกตัด, stale-marker เตือนผิด
  → แก้: hard-blocker scan ข้ามบรรทัด footer chrome (`shift+tab to cycle` / `bypass permissions`) — spinner จริง `✻ … (esc to interrupt)`
  อยู่คนละบรรทัดจึงยัง busy เหมือนเดิม · doctor `ready_marker_selftest` เพิ่ม 2 เคส + 3 tests (`test_pty_ready_prompt.py`)

## [v1.6.1] - 2026-08-25

### Fixed (แก้)

- **UI ค้างเป็นวินาทีทั้งที่ CPU ต่ำ (#386)** — `main_thread_stall` 45 ครั้ง/6 ชม. สูงสุด 18.8s → root cause (จาก watchdog stack dump
  ใน boot.log): main thread อ่านไฟล์ JSON เล็กๆ ซ้ำทุก tick — ledger ทุกโปรเจค (pending list ทุก 6s), `role-models`/`role-providers`
  (`effective_provider_for` ทุก idle-check + status header), projects registry, `role_messages` — ตอน disk ช้าแต่ละ read กิน 1–10s
  → แก้: `cached_read.read_cached` (stat เดียวต่อ poll, ใช้ค่า parse เดิมถ้า mtime/size/inode ไม่เปลี่ยน, ไฟล์ที่เพิ่งเขียน <2s
  อ่านใหม่เสมอ) ครอบ `legacy_reader.read_json` / `task_ledger._load_state` / `role_messages.read` · pending-projects scan
  ย้ายไป `QThreadPool` + 8 tests (`test_cached_read.py`, `test_project_nav.py`)
- **`/compact` ยิงซ้ำทั้งคืนบน pane ที่ไม่มีอะไรใหม่** (user report) — `proactive_idle_compact` ×18/2 วันบน Lead pane เดิม → root cause:
  idle episode จบทุกครั้งที่ ready-marker วูบ/hook session report → episode ใหม่ = compact ใหม่บน context ที่ compact ไปแล้ว
  → แก้: `PtySession.output_bytes_total` + gate `PROACTIVE_COMPACT_MIN_NEW_OUTPUT_BYTES` (8 KiB, env override, 0 = ปิด) — ไม่มี output
  ใหม่ตั้งแต่ compact ครั้งก่อน settle = ไม่ยิง (log `proactive_idle_compact_skipped`) + 1 test (`test_idle_watchdog.py`)
- **reviewer (codex) pane ค้างที่ TUI boot แล้ว respawn วน 3 รอบ (#380, #379)** — codex 0.149 วาด composer + `? for shortcuts`
  ก่อน init เสร็จ (banner ยัง `model: loading`) ทุก ready rule จึง match → cockpit paste task เข้า TUI ที่ยังโหลด → หาย → verify fail
  → auto-recover เข้า race เดิม → แก้: `model:/directory: loading` เป็น boot-phase marker ใน delivery window (`_BOOT_MARKER_TAIL_ROWS`)
  paste รอจน ready จริง + 1 test (`test_pty_ready_prompt.py`)
- **pane_guard บล็อก `git merge-base` (read-only) และบังคับ worktree pane วิ่งหา Lead ทุกครั้งที่ต้อง sync base (#385)** —
  `merge` match `merge-base` เพราะ `-` เป็น word boundary → แก้: subcommand จบด้วย `(?![\w-])` (merge-base/merge-tree/commit-tree/
  checkout-index ผ่าน) · `git merge <base>` **อนุญาตภายใน worktree ของ pane เอง** (branch ตัวเองเท่านั้นที่ขยับ; บน shared tree ยัง
  Lead-only, push/rebase/checkout ห้ามเหมือนเดิม) · rule text + worktree hint อัปเดต + 14 tests (`test_pane_guard.py`)
- **`takkub qa-gate` (full) ตายบน Windows กับ Node monorepo (#378)** — ชื่อ step `typecheck:apps/admin` ถูกใช้เป็นชื่อไฟล์ log ตรงๆ
  (`:` ผิดกฎ NTFS → Errno 22, `/` กลายเป็น dir ซ้อน) → แก้: `_log_stem` sanitize ทุกตัวนอก `[A-Za-z0-9._-]` เป็น `-` ทั้ง 2 OS
  + 8 tests (`test_qa_gate.py`)

### Changed (ปรับ)

- deps: ruff 0.16.4 (pin + pre-commit rev) · dependabot ignore PyQt6 ด้วย `versions: [">=6.9"]` (update-types กัน range widen
  ไม่ได้ — #138/#147/#384 ครั้งที่ 3) · actions: setup-node 7, upload-artifact 7, codeql-action 4.37.8

## [v1.6.0] - 2026-08-24

### Removed (ถอนออก)

- **OpenViking ถูกถอนออกจากผลิตภัณฑ์ทั้งหมด** (user decision — pack `docs/plans/remove-openviking-2026-08-24/`) — ฟีเจอร์บังคับให้มี
  AI embedding provider (จ่าย API หรือรัน Ollama) ขัดกับหลัก "ง่ายและฟรี" ของระบบ · ลบ: managed runtime ทั้ง package
  (`openviking/`), HTTP adapter/source/indexing, `merge_openviking*` ใน Context Builder, หน้า Settings + Setup Wizard,
  boot auto-start/shutdown wiring, CLI `takkub ov *`, doctor rows, secrets backend — **~7,600 บรรทัดออก** · เก็บครบ:
  Brain / Conversation / Graft / Obsidian local resources / context gate (small/medium/large) / generic trace / `apply_scope_and_trust`
  · env `TAKKUB_OPENVIKING_*` เก่าถูกเมินอย่างปลอดภัย ไม่ crash · regression suite ใหม่ (`test_remove_openviking_regression.py`):
  ไม่มี module/CLI/network/process ของ OpenViking เหลือใน boot/assign path
- **`takkub cleanup openviking`** (ใหม่, แทน `ov *` ทั้งหมด) — ล้าง runtime เก่าจาก 1.5.0 อย่างปลอดภัย: ตรวจ ownership ผ่าน PID file
  เดิม, โชว์ path+ขนาด, ขอ confirm (`--yes`), stop เฉพาะ process ที่ Takkub เปิดเอง (external ไม่แตะ), ลบ venv/log/state,
  `--purge-data` เท่านั้นถึงลบ config/data

### Changed (ปรับ)

- **Settings จัดใหม่ให้เรียบ** (user directive "หน้าไหน auto แล้ว เอาออก") — ลบหน้า: Role Overlap (read-only diagnostic ซ้ำ doctor),
  Core V2 Overview (flag default-on ตั้งแต่ 1.0.84, doctor โชว์เหมือนกัน), Core V2 Migration (auto-migrate ตอน boot #361 ทำแทนแล้ว)
  · Knowledge & Design 4 หน้า → 1 หน้า "Knowledge" (3 tab: Knowledge/Design Tools/Context Debug) · Accounts & Pools/Routing/Brain/
  Scheduler/Performance → section **ADVANCED** พับเก็บ default (จำสถานะกาง/พับ, เข้าหน้าใน section ที่พับ = auto-expand)
  · nav เห็นทันทีเหลือ 9 แถวจากเดิม 21 หน้า

## [v1.5.0] - 2026-08-24

### Added (เพิ่ม)

- **OpenViking แบบ Managed Local — ติดตั้ง/รันให้เองอัตโนมัติ ไม่ต้อง Docker ไม่ต้องเปิด terminal เอง** (pack
  `docs/plans/openviking-managed-local-2026-08-24/` · reviewer `docs/audit/2026-08-24-openviking-managed-review.md` SHIP หลังแก้ HIGH+MEDIUM ครบ) —
  ผู้ใช้แค่ Settings › Knowledge & Design › OpenViking → **[Install & Enable]** → Setup Wizard (provider/model ตาม upstream schema จริง:
  volcengine/openai/kimi/glm/ollama — ollama = local ล้วนไม่ต้องมี key) → Takkub สร้าง venv แยกที่ `~/.agent-takkub/services/openviking`,
  `pip install openviking`, เขียน ov.conf, start `openviking-server` ที่ 127.0.0.1 (port 1933 หรือ free port ถ้าชน — ถ้ามี OpenViking
  ตัวอื่นรันอยู่แล้วและ healthy จะ**ใช้ร่วมกันเลยไม่เปิดซ้ำ** เช่น dev+prod cockpit ใช้ตัวเดียวกัน) แล้ว poll health
  - เปิด Cockpit ครั้งถัดไป → auto-start ให้ (ถ้าติ๊ก Start automatically) · ปิด Cockpit → stop เฉพาะ process ที่ตัวเองเปิด
    (**external process ไม่โดนฆ่าเด็ดขาด** — owner_pid+create_time guard แบบเดียวกับ remote tunnel) · crash → bounded restart backoff
  - Settings: Status/Runtime/Address/Version · Start/Stop/Restart · Repair (venv `--clear` สร้างใหม่จริง) · Update (explicit เท่านั้น,
    เก็บ prior version) · Remove (ถามแยกว่าลบ data ไหม) · View Logs (redacted) · **[Open Studio]** เปิด Web Studio ของ OpenViking ใน browser
  - CLI: `takkub ov managed status|install|start|stop|restart|doctor|update|repair|remove|studio` · doctor section `[openviking]` managed runtime
  - Security (reviewer HIGH แก้แล้ว): API key เก็บใน SecretManager เท่านั้น — ov.conf มีแค่ placeholder `${OPENVIKING_API_KEY}`
    (env-substitution ของ upstream, ยืนยันจาก docs จริง) inject เป็น env ตอน spawn · log ผ่าน `redact()` ก่อนเขียน + View Logs redact ซ้ำ
  - fail-open ทุกจุด: ปิด/พัง → Cockpit ทำงานปกติ (Brain/Conversation/Graft ไม่เกี่ยว) · localhost เท่านั้น ไม่มี tunnel/0.0.0.0 ·
    ไม่ auto-install ตอน boot · docs: `docs/guides/2026-08-24-openviking-sidecar.md` (section "Managed local")

## [v1.4.1] - 2026-08-24

### Fixed (แก้)

- **OpenViking read/hybrid mode ค้นข้อมูลไม่เจอเลยจริง — search hit `uri` ไม่ตรงกับ local registry key เสมอ** (#377, follow-up จาก 1.4.0)
  — root cause ยืนยันจาก OpenViking API จริง (`docs/en/concepts/04-viking-uri.md`, `docs/en/api/02-resources.md`): ingest คืน
  `result.root_uri` (`viking://resources/...`) แต่โค้ดเดิม key local registry ด้วย path ในเครื่อง (`str(path)`) คนละ namespace กันสิ้นเชิง
  → `apply_scope_and_trust` reject ทุก hit จริงเป็น "missing project metadata" ตลอด (ปลอดภัย ไม่รั่วข้าม project แต่ใช้งานไม่ได้เลย)
  · แก้: `openviking_adapter.add_resource()` คืน `root_uri` ที่ sidecar ยืนยันจริง (ไม่ใช่ bool อีกต่อไป) แทนที่จะเดา · `indexing.py`
  ขอ uri แบบ deterministic ผ่าน `to=` (`viking://resources/<workspace>/<project>/<rel path>`, idempotent ต่อการ re-ingest) แต่ยึด
  `root_uri` จริงที่ sidecar คืนมาเป็น key เสมอถ้าไม่ตรงกับที่ขอ (log warning) · เทสเขียนใหม่ให้ mock ตาม response shape จริง
  (`root_uri` ต่างจาก local path) แทน pattern เดิมที่ doctor uri ให้ตรง key โดยบังเอิญ

## [v1.4.0] - 2026-08-24

Final closeout pack (after 1.3.0) — roadmap `docs/plans/final-closeout-after-1.3.0/` · reviewer `docs/audit/2026-08-24-closeout-review.md`
(ship ทั้งชุด, 1 HIGH follow-up ไม่บล็อก → #377) · Phase 10/V2 authority (#362) ไม่ถูกแตะ

### Added (เพิ่ม)

- **OpenViking strict project isolation** — ทุก resource ที่ index ติด `workspace_id/project_id/source/kind/resource_id/trust/updated_at`
  · defense-in-depth 3 ชั้น: source-level gate (`openviking_source`/`resource_source`) + `context_builder.merge_openviking_traced`
  re-check ก่อน inject เสมอ · fail-closed: metadata หาย/project อื่น → reject, มีแค่ project ตรง + `GLOBAL` เท่านั้นที่ผ่าน · trace โชว์
  `scope_rejects`/`trust_rejects` ต่อ source + ตัวอย่าง reject reason · **known gap** (#377): `read`/`hybrid` mode ยัง likely
  ไม่ได้ผลจริงกับ sidecar จริง (uri correlation ยังไม่ verify) — ปลอดภัย (fail-closed) แค่ยังไม่ทำงาน ไม่กระทบ default (`shadow`, ปิด)
- **Context/token gate ตามขนาดงาน** — `core/brain/context_gate.py`: small/medium/large classify จาก task text (+ override `flags={"context":...}`)
  · small = ไม่เรียก OpenViking/Resource เลย (ไม่ใช่แค่ trace บอก, ยืนยันด้วย call-spy test), budget ~2-4k · medium = Brain+Graft+files,
  ~4-8k · large = ทุก source adaptive ~6-12k · `TAKKUB_CONTEXT_GATE=0` คืนพฤติกรรมเดิมไบต์ต่อไบต์ (ยืนยันด้วย equality test ไม่ใช่แค่ claim)
  · trace เพิ่ม `task_size`/`inefficient` flag (small task เกิน 15k tokens → warn ใน doctor)
- **Settings UI: Knowledge & Design** — กลุ่มใหม่ใน Settings nav (`settings_knowledge_design.py`): **Knowledge** (Brain/Obsidian/Graft/
  OpenViking status รวม) · **OpenViking** (mode/strict-project/include-global/limit/timeout + Test/Sync/Re-index, env ยังชนะเสมอ
  ระบุใน UI) · **Design Tools** (Storybook/21st/Figma/Penpot status, credential เข้า SecretManager มาสก์เสมอ ไม่มี round-trip กลับ UI,
  Test/Permissions) · **Context Debug** (ตาราง SOURCE/ITEMS/TOKENS/TIME จาก trace ล่าสุด, scope/trust rejects, Total/budget, Copy Report)
  — network/subprocess ทุกจุดผ่าน worker thread จริง ไม่มีบน Qt main thread

## [v1.3.1] - 2026-08-24

### Fixed (แก้)

- **Delivery paste ทับ account-pending banner / trust modal ของ provider** (#376, severity high — เจอจริงกับ agy ใน wash-locker) —
  อาการ: spawn pane → ~7s ต่อมา task ถูก paste ลงหน้าจอ "Verifying your account…" (หรือ trust-folder modal ของ worktree ใหม่) → prompt หาย
  pane นั่งว่างที่ `>` และ `task_delivery_accepted` ถูก emit ทั้งที่ CLI ไม่เคยได้รับ → root cause: ready-gate เชื่อ `is_at_ready_prompt()`
  (footer chrome `>` + `? for shortcuts` ใน 6 แถว) อย่างเดียว ส่วน `account_pending_reason()` gate ด้วย grace 45s + streak 5 (ตอบคำถาม
  "ค้างถาวรไหม" ไม่ใช่ "ส่งได้ไหมตอนนี้") และ `_prompt_block_reason` แค่ warn Lead ครั้งเดียวไม่กั้น submit → แก้: `PtySession.
  shows_account_pending_marker(provider)` ungated (20 แถว, provider ไม่มี marker = no-op) + module ใหม่ `delivery_readiness.can_accept_input
  (is_ready, account_pending, prompt_blocked)` เป็น predicate เดียวก่อน submit ทุก path ใน `lead_inbox` (ready-streak reset ขณะ banner/modal
  อยู่ · `_delayed_enter_verified` ไม่ resend/repaste ทับ · blind-paste fallback defer เหมือน trust modal · `accepted` ไม่ผ่านถ้ายังเห็น
  banner/prompt) · `_prompt_block_reason` guard ชนิดค่าจริง (bool/str) กัน mock truthy · ทุก provider (pure text scan) ทั้ง Windows/macOS
  + tests `test_delivery_account_pending.py`, `test_delivery_prompt_blocked.py`, `test_auth_failure_detection.py` (+ fixture ใน
  `test_delivery_blocked_prompt.py` ให้ modal เคลียร์เอง) · gap: busy-marker จริงต่อ provider สำหรับ `accepted` → #103

## [v1.3.0] - 2026-08-24

Master Upgrade batch — roadmap `docs/plans/workspace-master-upgrade-2026-08-24/` (Phase 0 re-audit
`docs/audit/2026-08-24-master-upgrade-phase0.md` · reviewer `docs/audit/2026-08-24-master-upgrade-review.md` ·
QA `docs/audit/2026-08-24-master-upgrade-qa.md`) — Phase 10/V2 authority (#362) **ไม่ถูกแตะ**

### Added (เพิ่ม)

- **Explorer "Ask Agent" + Git-native ignore** (#374) — คลิกขวาไฟล์/โฟลเดอร์ใน Explorer → เลือก role ที่ live ใน project (ทุก provider)
  + พิมพ์คำถาม → ส่ง path (ไม่ส่งเนื้อไฟล์) ผ่าน `orch.send` เดิม · การซ่อนไฟล์ใช้ `git check-ignore --stdin -z` batch ต่อ directory
  (background, ผ่าน `_run_git` lock) fallback เป็น chain `.gitignore` เดิมเมื่อไม่มี git · placeholder Monaco สำหรับ encoding_unsupported
  แสดงข้อความของตัวเอง
- **Git Changes: rename/deleted/multi-root** (#375) — `FileChange` มี `old_path` + `repo_root` · diff rules M/A/D/R ตาม `06_GIT_FINAL_SPEC`
  (D ไม่ stat ไฟล์ที่หาย, R อ่าน `HEAD:old_path`) · `RepoDiscoveryService` resolve ทุก root → `git rev-parse --show-toplevel`
  (แก้ root ที่เป็น subdirectory ของ repo ซึ่งเดิมทิ้งทุก row เงียบๆ) · CHANGES จัดกลุ่มต่อ repo เมื่อ >1 repo
- **Design-tool integrations จริง: 21st.dev / Figma / Penpot** (#373) — `core/capabilities/design_clients.py` (stdlib urllib, fail-open,
  ทุกผลลัพธ์ติด `Provenance` = untrusted) · construct ได้ทางเดียวผ่าน `build_client` ที่เช็ค `PermissionEngine.mcp_allowed` (default-deny)
  + credential ใน `SecretManager` ทุกครั้ง · `takkub design integrations status|enable|disable|doctor` · doctor section `[design-integrations]`
  · Storybook ยัง priority 1 · 21st.dev ทางหลัก = official MCP (`register_twentyfirst_mcp`), REST ตรงเป็น opt-in ต้องใส่ base_url เอง
  (ไม่มี public search endpoint ยืนยัน 2026-08-24) · docs `docs/design-tool-integrations.md`
- **OpenViking optional sidecar + Context Sources** (#372) — `core/context_sources/` (base/brain/conversation/resource/openviking/
  indexing/trace_store/doctor_section) · `TAKKUB_OPENVIKING_ENABLED=0` default, `MODE=shadow|read|hybrid`, HTTP adapter เท่านั้น
  (ไม่ vendor AGPL) · Context Builder ยังเป็นผู้ merge/budget/dedup/provenance เดียว (`merge_openviking_traced` เป็น stage
  fail-open แยก — disabled = byte-identical) · Obsidian index allowlist 01-Projects/02-Areas ผ่าน `obsidian_boundary` · `takkub ov index|status`
  · doctor `[knowledge/context]` · docs `docs/guides/2026-08-24-openviking-sidecar.md`

### Fixed (แก้)

- **Preview: file:// normalization + project-aware + close cleanup** (#369, BUG-001/002/003) — `navigation_allowed` file mode เทียบ
  raw path กับ `file:///…` ที่ Chromium ส่งกลับ → block ไฟล์ตัวเองเสมอ → แก้ canonical `_local_file_path` (QUrl→toLocalFile→resolve,
  normcase, กัน `C:` ถูกอ่านเป็น scheme) · `PreviewHost` แสดงเฉพาะ project ที่ active (background publish = status bar notice,
  tab switch = show state/hide dock ไม่ destroy WebView) · ปิด tab → `preview_command("close")` ล้าง state + nav counters
- **Editor: strict UTF-8/BOM + POSIX mode** (#370, BUG-004/005) — `stat_snapshot` ไม่ใช้ `errors="replace"` อีก: invalid UTF-8 →
  `encoding_unsupported` เปิด read-only, `save_atomic` ปฏิเสธ · UTF-8/BOM ยัง editable round-trip · `_write_atomic_text` preserve
  `stat.S_IMODE` ลง temp ก่อน `os.replace` (POSIX; win32 skip ชัดเจน)
- **Revise → Designer** (#371, BUG-006) — `design_revise` ส่ง structured feedback (id/title/kind/target/feedback ไม่ส่ง HTML) ไปยัง
  pane ผู้สร้าง artifact หรือ `designer` ที่ live (provider-agnostic) · Lead ยังได้ audit notice ระบุ routed/fallback
- **Windows native abort (access violation) จาก QThreadPool worker** (#375 follow-up, reviewer HIGH ×2) — `RepoDiscoveryService`
  connect queued signal ตรงเข้า `signal.emit` ทำให้ receiver ที่ถูก destroy ยังโดนยิง → แก้เป็น bound slot · `SUBPROCESS_LOCK` +
  `RESOLVE_LOCK` (`project_file_index`) ครอบทุก `subprocess.run`/`Path.resolve()` ใน git_changes_service/project_file_index/
  project_explorer/preview_controller/editor_widget/file_watch_service — pair `test_project_explorer + test_project_file_index`
  เคย abort 3/3 → เขียว 3/3

## [v1.2.1] - 2026-08-24

### Added (เพิ่ม)

- **Remote Reports — แชร์รายงาน/dashboard ผ่าน tunnel domain ตัวเองด้วย share-token ต่อไฟล์** (#367) — `takkub report publish <file.html>
  [--name] [--project] [--expires 30d] [--label]` → URL `https://<public_url>/<secret>/r/<project>/<name>?k=<token>` ส่งให้ใครก็ได้
  โดยไม่ต้องมีรหัส Remote · `report list|revoke|rotate` (Lead-only) · route `GET /<secret>/r/...` ใน `remote/http_server.py` ไม่ผ่าน
  bearer/password แต่ตรวจ token (`hmac.compare_digest`) ผิด/หมดอายุ/ไม่มีไฟล์ → 404 เหมือนกันหมด + lockout แยก + CSP/no-store/noindex ·
  store `runtime/exports/<ns>/reports/` + `_shares.json` (`remote/reports.py`, standalone-HTML validator reject asset ภายนอก) · ลิงก์ใช้ได้
  เฉพาะตอนเปิด Remote + tunnel รัน (CLI พิมพ์สถานะทุกครั้ง) · reviewer MERGE-WITH-FIX (`docs/reviews/2026-08-23-367-remote-reports-review.md`)

### Fixed (แก้)

- **qa-gate (Node) รัน typecheck เสมอ — ปิด false-PASS ที่ไปแดง CI ด้วย TS2554** (#368) — เดิม Node gate รันแค่ `npm test` (+ `tsc` เฉพาะเมื่อ
  root มี tsconfig.json ซึ่ง monorepo/turbo ไม่มี) → vitest transpile ผ่าน esbuild ไม่เช็ค type → spec ที่ signature drift ผ่าน gate ทุก pane
  แล้วไปแดงที่ CI (lottery 2026-08-23, parallel worktree) · ตอนนี้ `verify.node_checks()` เลือกตามลำดับ `verify` script › `typecheck` script ›
  `tsc -p tsconfig.json --noEmit` (root ไม่มี → ทุก workspace package จาก `pnpm-workspace.yaml`/`workspaces` ที่มี tsconfig, รันใน cwd ของ
  package นั้น) › ไม่มี TS = `test` อย่างเดียว · typecheck มาก่อน `test` + fail-fast = typecheck แดง gate FAIL แม้ test เขียว · package manager
  จาก lockfile (`pnpm-lock.yaml`/`yarn.lock`/`bun.lock*`/`package-lock.json`) หรือ `packageManager` field — ไม่ hardcode npm, resolve `.cmd`
  shim บน Windows · step ชื่อ `verify`/`typecheck[:pkg]`/`test`/`lint` · `--targeted` บน Node ยังบอกว่าไม่ narrow และ typecheck รันเต็ม ·
  `Check.cwd` ใหม่ (verify.run_checks + qa_gate._run_step เคารพ) · tests 3 shape (verify / tsconfig / no-TS) + monorepo + เคส typecheck แดง
  test เขียว → FAIL · docs: cli-reference + CLAUDE.md บรรทัด Node gate
- **Sidebar Project Explorer เต็มถึงขอบล่าง** — tree ใต้ project card เคย fixed 260px เหลือแถบว่างใต้ tree บนจอสูง; ตอนนี้
  `ProjectNav._fit_explorer` คำนวณจาก viewport ของ list − header ทุก row (re-fit ตอน resize/splitter/เปลี่ยนโปรเจค/เพิ่ม-ปิด tab/chevron,
  coalesce `singleShot(0)`), floor `_EXPLORER_MIN_H=120` แล้ว list สกอลแทน
- **CodeQL 7 alerts (#31–#37)** — py/path-injection ×6 ใน `remote/reports.py`/`http_server.py`: เพิ่ม `os.path.normpath`+`startswith`
  barrier ที่ CodeQL รู้จัก (คง `resolve()+is_relative_to()` ไว้กัน symlink) · py/redos ×1 `_TERMINAL_AUTO_REPLY_RE` (#357) → token scanner
  forward-only + bounded quantifiers + max 512 bytes (adversarial 10k `ESC[` < 50 ms)

- **Project Explorer ย้ายเข้า sidebar PROJECTS ใต้การ์ดโปรเจค** (#365 feedback) — เดิมเป็น QSplitter panel แยกกลายเป็น 3 คอลัมน์ →
  ตอนนี้ tree ฝังใต้ row ของโปรเจคที่เลือก (chevron บนการ์ดยุบ/ขยาย, จำ state ต่อโปรเจค key เดิม) `project_nav.py` · `project_tab.py`
  เหลือ pane area อย่างเดียว · ADR `adr-workspace-shell.md` อัปเดต
- **พิมพ์ใน Monaco editor ไม่ได้** — คลิกทำให้ `QWebEngineView` ได้ Qt focus แต่ DOM focus ไม่เข้า Monaco (cursor ไม่กระพริบ คีย์หาย) →
  `_EditorWebView.focus()` forward ทั้ง Qt + `focusActiveEditor()` ใน page ตอน click/focusIn/เปิดไฟล์/โชว์ dock

## [v1.2.0] - 2026-08-23

### Added (เพิ่ม)

- **Workspace 1.2.0 เฟส 8 Obsidian hardening** (#365, `10_OBSIDIAN_HARDENING.md`) — `project_identity.resolve_project_id()` (project_id
  จาก `config.load_projects()` ตัวเดียว V2/V1 ไม่นิยามซ้ำ) · `obsidian_metadata.NoteMetadata` canonical frontmatter (knowledge_id/
  project_id/source/kind/trust/content_hash/created_at/updated_at) ใส่ทุก note ที่ cockpit เขียน (`vault_mirror.py`; page เก่าไม่แตะ
  อัตโนมัติ → `obsidian_backfill.py` opt-in) · `obsidian_dedup.DedupIndex` JSONL upsert-log ใต้ `core_home()` persistent ข้าม restart ·
  `obsidian_boundary.is_indexable()` default-deny (allow `01-Projects/02-Areas`, deny `99-Logs/.obsidian/runtime/secrets`/raw transcript —
  export ให้ OpenViking เฟส 9) · `takkub doctor --obsidian` (opt-in) + tests 5 ไฟล์ 90 เทส
- **Workspace 1.2.0 acceptance** (#365) — `docs/audit/2026-08-23-365-workspace-ram-acceptance.md`: เปิด editor+preview 3 โปรเจกต์จริง =
  **+155 MB** (budget +300) · `16_ACCEPTANCE_CRITERIA.md` 19 ข้อ ✅13 / ⏳6 (ต้อง browser จริง/CI) / ❌0 · `docs/audit/2026-08-23-365-webengine-soak.md`
  soak 25×3 ผ่าน · reviewer SHOULD ฝั่ง UI ปิดครบ (CHANGES row ผ่าน `resolve_and_contain`, `navigation_allowed` contract pin ลง docstring
  + เทส `_PreviewPage.acceptNavigationRequest` ตรงๆ, ask-agent 4000-char bound test) · follow-up: QtWebEngineProcess ไม่ถูก reap หลังปิด
  (offscreen, ต้อง verify จอจริง) → issue แยก **ปิดแล้ว, ดูล่าง #366**

- **#366 — QtWebEngineProcess reap fix: เป็นบั๊กของ soak harness ไม่ใช่แอป** —
  `docs/audit/2026-08-23-366-webengine-process-reap.md`: verify จอจริงก่อนพบว่ายังค้าง (5 cycles/3 projects → 4 process ค้าง,
  ไม่ลง 0 ใน 90s แม้จอจริง) ตรงข้ามกับสมมติฐาน offscreen-artifact เดิม — ไล่ root cause เจอว่า `tools/soak_workspace_webengine.py`'s
  `_pump()` เดิม (`processEvents()` ใน `time.sleep()` loop) ไม่เคย deliver `QEvent::DeferredDelete` ให้ `QWebEngineView`/`QWebEnginePage`
  เลย (`sip.isdeleted()` เป็น False ค้างตลอด 30s) — Chromium's IPC shutdown handshake ต้องการ real nested `QEventLoop`
  (`loop.exec()`) ถึงจะ deliver ได้ (bare-probe reap ใน ~1s ทันทีที่เปลี่ยนมาใช้) · `EditorHost`/`PreviewHost`'s teardown code
  **ไม่ต้องแก้เลย** — โปรดักชันรันใต้ `app.exec()` อยู่แล้ว จึงไม่เจอบั๊กนี้จริง · แก้ `_pump()` ให้ปั๊มผ่าน `QEventLoop` จริงแทน →
  reap 0 stray process ทั้ง real display (0.5–1.5s, 5/15 cycles) และ offscreen (0.5s) · เพิ่ม `webengine_process_count_after`
  assertion เข้า `tests/test_workspace_webengine_soak.py` (เดิมมองไม่เห็น bug นี้เพราะเช็คแค่ Python-level flag) ·
  acceptance doc §2 อัปเดตเป็น "ปิดแล้ว ≈0" แล้ว

- **Workspace 1.2.0 เฟส 7 design tool integrations** (#365) — `core/capabilities/design_integrations.py`: **Storybook detect จริง**
  (`.storybook/` หรือ `package.json` script · port จาก `-p/--port` · `preview_url` สำหรับ `takkub preview open-url`, ไม่รัน/ไม่แตะ
  network) + 21st.dev/Figma/Penpot = **registry stub opt-in only** (`enabled_for_role` อ่านจาก `pane_tools_policy.effective_mcps`
  เดียวกับ spawn — default OFF, ไม่มี bypass, secret ผ่าน `secret_ref` เท่านั้น) · `CapabilityRegistry.resolve_design_integrations` ·
  `takkub doctor` section `[design-integrations]` · `.claude/agents/designer.md` reference priority: Storybook → design system ของ
  โปรเจกต์ → external MCP (last resort) + anti-AI checklist
  + tests: `tests/test_core_capabilities_design_integrations.py` (detect/port/stub/policy) · `tests/test_doctor.py` (+section)

- **Workspace 1.2.0 เฟส 5 widget + เฟส 6 Design Director UI** (#365) — `preview_widget.py` PreviewHost: **QWebEngineView 1 ตัวทั้งแอป**
  (dock เหมือน editor) lazy create/destroy · URL (loopback-only) / file mode · device presets Desktop/Tablet/Mobile · Refresh/Open
  externally · **navigation policy gate ทุก request** ผ่าน `preview_controller.navigation_allowed` · **ไม่มี QWebChannel bridge** ·
  off-the-record profile · discard-on-hidden (#364 lever 1) · เฟส 6: artifact header (lookup by target) + Approve/Revise →
  `design_approve`/`design_revise` · `main_window.py` ต่อ `previewOpened/Updated/Closed` → widget (CLI `takkub preview` สั่งเปิดได้) ·
  RAM ad-hoc: เปิด preview ครั้งแรก ≈ +110–120 MB in-process (ตามคาด ~200–300 MB รวม Monaco+Preview) · full RAM acceptance 3 โปรเจกต์
  = งานเก็บตกถัดไป
  + tests: `tests/test_preview_widget.py` (36 — lazy/destroy · nav policy · no-bridge guard · presets · discard · approve/revise ผ่าน
  design_actions จริง · CLI→widget · opt-in real-QWebEngineView smoke)

- **Phase 10 ชิ้น 2 — readers สลับไปอ่าน `v2/` ภายใต้ `TAKKUB_V2_AUTHORITY` (default **OFF**)** (#362) — `core/storage/v2_authority.py`
  helper เดียว: flag ON + v2 target มี → อ่าน v2 (unwrap `.data`) ไม่งั้น V1 เดิม · fail-open กลับ V1 ทุกจุด (ไม่มี/พัง → V1 + log) ·
  ครอบทุก domain ที่ dual-write: role_models · provider_models · provider_state · pane_tools_policy · skill_policy · custom_roles ·
  provider_config routing (global+per-project) · `config.load_projects` · issues local-issues · auto_issue dedup · remote session_store ·
  `Router.effective_model_for` flip authority ตาม flag (ON = V2 authoritative, V1 เป็น shadow) · `doctor --storage-layout` เพิ่ม finding
  `authority` · **rollback = ปิด flag ทันที ไม่ต้องแตะดิสก์** · Settings Core V2 toggle · ไม่ wire exec_mode/auto_resume/rtk_helper
  (getter เป็น constant ไม่มี V1 read call site — บันทึกใน plan §3 Wave E) · **การ flip default เป็น ON = การตัดสินใจ release 2.0.0
  หลัง soak drift=0** ไม่ใช่ในรอบนี้
  + tests: `tests/test_core_storage_v2_authority.py` (44) · `test_core_routing.py` authority-flip (3) · parity OFF/ON ทุก domain
- **Workspace 1.2.0 เฟส 10 — `takkub doctor --workspace` diagnostics + WebEngine lifecycle soak** (#365, `13_PERFORMANCE_AND_QT_RULES.md`
  ข้อ 10) — เพิ่ม opt-in flag ตาม pattern `--ram`/`--live`: `[workspace] monaco-bundle` Finding (pure/local, เช็ค
  loader.js/editor.main.js/LICENSE ใต้ `static/editor/vendor/` + ขนาด) + IPC `workspace-status` (live editor host
  instance/tab count, preview state ต่อโปรเจกต์ + navigation-block counter ใหม่บน `PreviewController`, design artifact
  count/status, file watcher backlog/debounce, git_changes last-run ms+error, tree scan time) — เพิ่ม `diagnostics()`
  method เบาๆ (ไม่แตะ main thread) ใน `file_watch_service.py`/`git_changes_service.py`/`project_file_index.py`
  (`ProjectFileIndex`) ตัวที่ไม่เคยมี metric มาก่อน · `Orchestrator.set_editor_host`/`register_workspace_diag_source`
  ให้ main_window ผูก live object เข้ากับ diagnostics แบบ opt-in (ไม่มี = SKIP ไม่ใช่ FAIL) · `tools/soak_workspace_webengine.py`
  (standalone script ตาม pattern `spike_pane_discard_ram.py`) — วน editor open/close จริง (`EditorHost` + real
  `QWebEngineView`) + preview state machine + pane discard/reattach ข้ามโปรเจกต์ N รอบ วัด RSS/object count ก่อน-หลัง,
  proof "ไม่ reparent crash" คือ exit code 0 หลัง N รอบ (ไม่ใช่ identity check ที่ CPython id() recycle หลอกได้) —
  เทส wrapper opt-in `AGENT_TAKKUB_QT_WEBENGINE_SMOKE=1` (`test_workspace_webengine_soak.py`) · เพิ่ม release gate
  1.2.0 checklist ใน `docs/release-checklist.md` (acceptance 19 ข้อ + RAM +≤300 MB/ปิดแล้ว 0 + soak ผ่าน + Monaco
  bundle ยืนยันอยู่ในตัว wheel จริง — ตรวจแล้วด้วย `python -m build` จริง 1 รอบ)

- **#364 lever 5 — profile main process (pythonw) ด้วยตัวเลขจริง: ไม่พบ leak, ไม่มีอะไรให้ cap** — spike 5 pane จริง (offscreen):
  RSS โต **13.16 MB/pane**, ปิด pane แล้ว TerminalWidget/QWebEngineView object count กลับเป็น 0 ทั้งคู่ → ไม่เสนอ cap ·
  เพิ่ม `takkub doctor --ram --ram-profile` (opt-in, tracemalloc on-demand ผ่าน IPC `ram-profile` ไม่เปิดค้าง) เป็น visibility tool +
  `docs/audit/2026-08-23-364-lever5-main-process-profile.md` · `tools/spike_main_process_ram_profile.py` + spike test opt-in
  (`AGENT_TAKKUB_QT_WEBENGINE_SMOKE=1`) · **#364 ทุก lever ปิดครบ**: 6 วัด → 4 MCP (stale variant bug) → 2 subagent → 3 cap →
  1 discard (~65 MB/pane) → 5 profile (ไม่มี leak)

- **Workspace 1.2.0 เฟส 5 backend — Live Preview controller + Design artifact registry + CLI** (#365) — `preview_controller.py`
  (state ต่อโปรเจกต์, **URL loopback-only**, file gate = containment + `.html` เท่านั้น, `navigation_allowed` policy fn — QObject เดียว
  ทั้งแอปตาม RAM rule) · `design_actions.py` (DesignArtifact registry JSONL latest-per-id ใต้ `storage_layout_v2` project artifacts
  ตาม `schemas/design_artifact.schema.json` — publish/approve/request_revision, approved = terminal) · Orchestrator
  `preview_command`/`design_publish`/`design_approve`/`design_revise` + signals · CLI `takkub preview open-url|open-file|close|status`
  และ `takkub design publish|approve|revise` (nested subcommand ตาม convention เดิม, trust-local tier scope ด้วย from_project) ·
  widget จริง (`preview_widget.py`) = frontend ถัดไป
  + tests: `test_preview_controller.py` · `test_design_actions.py` · `test_workspace_preview_design_cli.py` (argparse + CliServer
  round-trip) · ผ่าน repo guard

- **#364 lever 1 — discard renderer ของ pane ที่ซ่อน (วัดจริง: คืน ~65 MB/pane ที่ 4 pane)** — `TerminalWidget.set_keepalive(False)`
  ตั้ง debounce 25 s (`TAKKUB_PANE_DISCARD_DEBOUNCE_MS`) → snapshot buffer (`termGetBufferText`, cap 5,000 บรรทัด) →
  `QWebEnginePage.LifecycleState.Discarded` · `set_keepalive(True)` ยกเลิก timer หรือ re-attach (Active + reload, replay snapshot
  ก่อน flush `_pending_writes` — ส่วนที่เขียนระหว่าง discarded ได้ ANSI เต็ม ส่วนก่อนหน้าเป็น plain text) · **veto**: Lead pane ของ
  project active · pane ที่ PTY output < 10 s · race ที่สลับกลับกลางทาง snapshot async → abort discard · `_page_ready` gate ครอบ
  "discarded" ด้วย (write/IPC ไม่ทิ้ง) · toggle Settings Performance `pane_discard_enabled` + `TAKKUB_PANE_DISCARD=0/1` ชนะ ·
  `doctor --ram` แสดง `[discarded]` ต่อ pane · re-attach 375–420 ms · 6 pane/5 hidden เหลือ ~40 MB/pane (เกิน
  `--renderer-process-limit=4` — ถ้าเปลี่ยน pane policy ต้องวัดใหม่) · ผลวัดจริง `runtime/exports/.../lever1-impl-discard-{4,6}pane.json`
  + tests: `test_agent_pane_discard_eligibility.py` (ใหม่) · `test_terminal_widget.py` · ram/doctor/orchestrator passthrough ·
  `test_pane_discard_spike.py` (scrollback_lost → False)

- **#365 เฟส 3+4 UI — editable Monaco + save/conflict flow + CHANGES panel + Ask Agent** (ต่อยอด
  service layer `7e88fd2`) — เฟส 3: Monaco เขียนได้ (`readOnly:false`) · dirty marker (`●`) บน tab ·
  Ctrl+S → `bridge.saveFile` → `editor_service.save_atomic` บน worker thread (baseline tracked
  server-side ใน `EditorHost._file_states`, ไม่เชื่อ version จาก JS) · conflict → `[Compare]`
  (diff editor ใช้ `tab.model` ตัวจริงเป็น modified side ไม่ใช่ copy — diff ตามการพิมพ์สด) /
  `[Reload disk]` / `[Keep mine]` (re-snapshot disk ณ เวลาที่กด ก่อนบังคับ overwrite) ·
  `file_watch_service.FileWatchService` (roots สะสมได้ผ่าน `add_roots()` ใหม่ — 1 instance ครอบทุก
  project) ต่อไฟล์ที่เปิด → banner "changed/deleted on disk" (ไม่ reload เงียบแม้ buffer clean) —
  แยกแยะ external edit vs. self-save echo ด้วย mtime_ns/size/sha256 diff กับ baseline · binary/
  large ยัง read-only fallback เดิม · เฟส 4: Explorer section "CHANGES (n)" จาก
  `git_changes_service.GitChangesService` (background + debounce, ไม่ eager-refresh ตอน construct
  กัน QTimer leak) · badge M/A/D/R (R ไม่มีใน GitStatusService เดิม) · คลิกแถว → เปิดไฟล์ + diff
  vs HEAD ในรอบเดียว (`EditorHost.open_file(..., show_diff=True)`) · refresh debounce หลัง save/
  disk_changed ผ่าน `EditorHost.gitRefreshNeeded` → `ProjectExplorer.refresh_changes()` · Ask Agent
  (context menu Monaco + ปุ่ม "?" ต่อ tab) ส่ง selection ที่ bound (≤4000 ตัวอักษร) + request ไป
  Lead ผ่าน `Orchestrator.send()` เดียวกับ `takkub send` — ไม่ยัดทั้งไฟล์, ไม่เดา agent attribution ·
  ADR อัปเดต (`docs/architecture/adr-workspace-shell.md` phase 3+4) · tests ใหม่/ขยาย:
  `test_editor_widget.py` (save/conflict/keep-mine/reload/disk-watch/open-with-diff) ·
  `test_project_explorer.py`/`test_project_tab_explorer.py` (CHANGES panel + signal forwarding) ·
  ผ่าน repo guard subprocess no-window/encoding + keepalive + editor_widget targeted รอบเดียว

### Fixed (แก้)

- **task ledger: `os.replace` retry บน Windows** — `_atomic_write` เจอ `WinError 5` ชั่วคราว (handle อื่นอ่านไฟล์อยู่/AV) แล้ว
  เด้ง warning "[ledger] เขียน INDEX.md ไม่สำเร็จ" แทน notice จริง (CI Windows flake `test_orchestrator_shard`) → bounded retry 8 ครั้ง
  แล้วค่อย raise · POSIX ไม่กระทบ

- **agy "Verifying your account" ใบงานหายเงียบ (regression #346)** (#363) — root cause: `account_pending_reason()` และ
  `is_at_ready_prompt()` สแกน `_ready_region` 6 แถวเท่ากัน → footer chrome ดัน banner 3 บรรทัดหลุดหน้าต่าง, ready อ่านผิดเป็น READY แล้ว
  reset streak ทุก poll → ไม่เคยเข้า `blocked:provider-account` ไม่เตือน Lead paste ทิ้ง · แก้: `account_pending_reason()` สแกน
  `_BOOT_MARKER_TAIL_ROWS` (20 แถว, แบบเดียวกับ #284) + `lead_inbox` ไม่ให้ ready verdict กด account-pending check (ใช้กับทุก provider ที่มี
  `account_pending_markers`) → recovery เดิมของ #346 (close+respawn+degrade) ทำงาน · tests fixture จาก banner จริง

- **Editor save size cap + JS-string invariant** (#365 reviewer SHOULD-2/3) — `editor_service.save_atomic` reject save ที่ encode
  เกิน `max_bytes` (cap เดียวกับ read-side) ก่อนเขียน → `SaveResult(error=...)` ไม่แตะไฟล์เดิม (JS→Python `saveFile` ไม่ trust ความยาว
  จาก page อีกต่อไป) · `editor_widget._js_str()` = `json.dumps(..., ensure_ascii=True)` helper เดียวทุก `run_js` call site (กัน U+2028/
  U+2029 ตัด JS string) + tests

- **`takkub preview`/`design` IPC ต้องมี pane token** (#365 reviewer MUST-FIX) — เดิม `cli_server` เชื่อ `from_project` ที่ caller
  ส่งมาตรงๆ (process ใดก็ได้ในเครื่องเปิด/ปิด Preview หรือ publish/approve/revise artifact แทนโปรเจกต์อื่นได้) → ตอนนี้ `design` ทุก
  action + `preview` ทุก action ยกเว้น `status` อยู่ชั้นเดียวกับ `done/progress/send` (token ผูก project+role) ·
  `tests/test_workspace_preview_design_cli.py` (+ spoof cases)

- **CI batch หลัง merge 1.2.0 ชุดแรก (run 32633203191)** — 4 ต้นเหตุ: (1) `test_pane_discard_spike` ×4 segfault บน macos
  offscreen — เป็นเครื่องมือวัด RAM ไม่ใช่ regression gate → opt-in `AGENT_TAKKUB_QT_WEBENGINE_SMOKE=1` (2) debounce test ของ
  `git_changes_service` ใช้ `qWait(150)` คงที่ → bounded `_wait_until` (3) ERROR `test_win_console_sweeper` = QTimer ของ (2) ค้าง
  ตอน teardown โดน guard #344 จับที่เทสถัดไป — หายเองเมื่อแก้ (2) (4) `test_fifo_queue_drains_three_claude_assigns` —
  `processEvents()` 20 รอบไม่มีเวลาจริง → `_pump_until` **และ** จุดเทียบ `seconds_since_output < stall_threshold` ใน
  `_send_when_ready` ที่ `ea4aa62` wrap ไม่ครบ (MagicMock → TypeError ใน QTimer slot) → `_timing_or_none()` ครบทุกจุดแล้ว
  (grep ทั้ง repo: จุดอื่นมี guard อยู่แล้ว)

- **module ใหม่ของ workspace ชน repo guard** — `editor_widget.py` / `project_explorer.py` / `project_file_index.py` ทุก subprocess call
  ใส่ `creationflags=SUBPROCESS_NO_WINDOW` + text-mode `encoding="utf-8", errors="replace"` (guard
  `test_subprocess_no_window_guard` / `test_subprocess_text_encoding_guard` ที่ทำ CI แดงหลัง `7120b90`)

- **Workspace 1.2.0 เฟส 3+4 service layer (ยังไม่มี UI)** (#365) — `editor_service.py` (pure Python: `read_for_edit` →
  state mtime_ns/size/sha256/language + binary/size guard · `save_atomic` same-dir temp + `os.replace` long-path-safe · **ห้าม
  overwrite เงียบ**: disk state ≠ expected → `Conflict` ไม่เขียน · containment reuse `project_file_index`) · `file_watch_service.py`
  (QFileSystemWatcher debounced เฉพาะไฟล์ที่เปิด → `workspace.file.disk_changed`) · `git_changes_service.py` (`git status
  --porcelain=v2 -z` บน worker + unified diff เทียบ HEAD — baseline policy เดียวชัด, ไม่เดา agent attribution)
  + tests 54 (`test_editor_service.py` · `test_file_watch_service.py` · `test_git_changes_service.py`) · ผ่าน repo guard
  subprocess no-window/encoding

### Fixed (แก้)

- **#359 follow-up: delivery progress check โยน `TypeError` เมื่อ session เป็น fake/MagicMock → ERROR รั่วไปเทสข้างๆ + delivery
  chain ของ shard ขาดกลางทาง (CI แดง 3 OS หลัง `9723002`)** — `lead_inbox.py` เพิ่ม `_timing_or_none()` แล้ว route 4 จุดเทียบ
  `seconds_since_output()`/`last_output_monotonic()` ผ่านมัน (`_pane_shows_real_progress`, `_on_settled`, 2 จุดใน
  `_delayed_enter_verified._verify`) — ค่าไม่ใช่ตัวเลข = ไม่มี progress, ไม่ raise ออกจาก slot · เทส governor ที่ไม่ได้ pin
  `slot_policy` แก้ให้ตั้งชัด (default `max_panes_global` กลายเป็น RAM-derived โดยเจตนา) · regression: fake session คืน MagicMock
  → ไม่ raise · 5 ไฟล์เดิมรันรวม invocation เดียวเขียว

- **#364 lever 1 — spike: discard renderer ของ pane ที่ซ่อน วัดจริงแล้ว GO** — `QWebEnginePage.setLifecycleState(Discarded)`
  บน pane ที่ไม่ใช่ current tab จริงๆ (isVisible()=False, ตรงกับ `ProjectTab._apply_pane_keepalive` เดิมเป๊ะ) คืน renderer
  process **ทั้งตัว** ไม่ใช่แค่ heap ว่าง — วัดได้ **~65 MB/pane** ที่ 4 pane รวม (Lead + teammate ≤3 ตามนโยบายวันนี้ ชนพอดี
  `--renderer-process-limit=4`) ผ่านเกณฑ์ ≥60 MB/pane ในแผน · แต่เหลือแค่ **~39 MB/pane** ถ้า pane เกิน 4 (Chromium แชร์
  renderer process ข้าม limit) — **ต้องวัดใหม่ถ้าจะขยับ pane cap** · re-attach (setLifecycleState(Active) → รอ pageReady
  เดิม → เขียนซ้ำ) ~300–390ms รวม ไม่พัง QWebChannel bridge · **ข้อเสียจริง**: xterm.js scrollback หายหมดตอน re-attach —
  `PtySession.screen` ก็กู้ให้ไม่ได้เพราะเป็น `pyte.Screen` เปล่า (ไม่ใช่ `HistoryScreen`) เก็บแค่ตาราง visible ปัจจุบัน ไม่มี
  scrollback มาตั้งแต่ต้น · Frozen ไม่ช่วยเรื่อง RAM เลย (~0, ตามดีไซน์ Chromium) ไม่ต้องใช้ fallback · ยืนยันด้วยโค้ด+รันจริงว่า
  ไม่ชน ready-marker/idle-detection/delivery-verify (`pty_session.py`/`task_delivery.py` ไม่ import QtWebEngineWidgets เลย)
  · **side-finding**: เจอ root cause ของ "QWebEngineView จริงชน pytest abort แม้ offscreen" ที่บันทึกไว้ใน
  test_terminal_widget.py/test_editor_widget.py — `QApplication([])` (argv ว่าง) ทำให้ Chromium's base::CommandLine
  พังตอน renderer spawn (native abort, exit -1073740791) ส่วน `QApplication(sys.argv)` บูตได้ปกติ — ยังไม่ได้แก้ที่ต้นตอ
  (pytest fixture เอง argv ไม่ sanitize เท่าสคริปต์เดี่ยว) flag ไว้เป็น follow-up · รายงานเต็ม +
  ดีไซน์ implementation ร่าง (ยังไม่ทำ): `docs/audit/2026-08-23-364-lever1-pane-discard-spike.md`
  + tools: `tools/spike_pane_discard_ram.py` (สคริปต์เดี่ยว วัดซ้ำได้ ห้ามรันใน pytest process เอง) +
  tests: `tests/test_pane_discard_spike.py` (shell ออก subprocess กัน hard-abort ชน pytest)

- **#364 lever 4 — node/MCP ต่อ pane: วัดจริงแล้วไม่ใช่ leak แต่เจอบั๊ก stale MCP variant** — วัดจาก process tree:
  dev instance ทุก pane node/mcp = 0 MB (role policy + graft worktree-exclusion ทำงานถูก) · pane โหมด browser จริง =
  playwright (~980 MB รวม chrome tree) + chrome-devtools (~369 MB) + graft (~182 MB) ≈ 1.6 GB **legit** และหายสะอาดเมื่อปิด pane
  · graft-only baseline ≈ 180 MB · **บั๊กจริง**: `pane_tools_policy.save_policy()` ไม่เคย regen `shared-mcp-<role>.json`
  (ไฟล์ที่ `--mcp-config` ของ claude spawn อ่านจริง) ต้องพึ่ง caller เรียก `regen_role_variants()` เองทุกจุด — เจอ drift คาดิสก์:
  policy บอก context7 แต่ variant ยังเป็น graft เดิม; role ที่ถูก revoke browser MCP จะยัง spawn node/chrome ทิ้งไว้ 350 MB–1 GB+
  ทุก pane จนกว่าจะมีอะไรมา regen → `save_policy()` regen เองที่ต้นทาง + regression test ที่ fail ก่อนแก้ · `doctor --ram`
  เพิ่ม `check_ram_node_children` WARN เมื่อ pane ไหน node/mcp > 350 MB บอก role/project/pid · lazy-start MCP (3ข) พิสูจน์จาก
  ข้อมูลจริงไม่ได้ (claude CLI closed binary) → flag เป็น follow-up ใน #103 ไม่ฟันธง
  + tests: `test_pane_tools_policy.py::test_save_policy_regenerates_stale_variant` · `test_doctor_ram_live.py`
- **RAM diet lever 2+3 — auto `--mode subagent` + proactive `max_panes_global`** (#364) —
  `orchestrator.resolve_auto_assign_mode()`: เมื่อ `takkub assign` ไม่ระบุ `--mode` เอง ระบบเลือก `subagent`
  อัตโนมัติให้เมื่อ task สั้น (< 400 ตัวอักษร) + role ไม่ใช่ reviewer/critic + ไม่มี `#N` shard suffix/`--isolation
  worktree`/`--plan`/`--model`/`--provider`/`--effort` + role's effective provider เป็น claude (provider อื่น
  fallback เป็น pane เสมอ พร้อม log อ้าง #103 gap — native subagent รองรับ claude เท่านั้น) · `--mode pane`/`--mode
  subagent` ที่ระบุเองชนะเสมอไม่มีเงื่อนไข · ผลลัพธ์ auto-select สะท้อนกลับใน assign ack message
  (`cli_server.py`) ไม่ใช่ตัดสินใจเงียบๆ · `--mode` CLI default เปลี่ยนจาก `"pane"` เป็น `None` (unset = auto)
  — `core.scheduling.facade.effective_slot_policy()`: `max_panes_global` เมื่อ Settings ไม่ได้ตั้งไว้ (`None` —
  รวมกรณีไม่มีไฟล์ core-v2-settings.json เลย, แก้บั๊กเดิมที่ไฟล์หายทำให้ทั้ง policy fail-open ทิ้งไม่เคยอ่าน
  scheduler_policy จริง) คำนวณ default จาก RAM จริงแทนการไม่มี cap: `floor((available − reserve) / 650MB)`,
  reserve = `performance_settings`'s `min_available_ram_percent` × total RAM (สัดส่วนเดียวกับที่ reactive
  governor latch สำรองไว้อยู่แล้ว) · sample สดทุกครั้ง (ไม่ผูกกับ settings-file mtime cache) · ค่าที่ user ตั้งเอง
  ใน Settings → Scheduler ชนะเสมอ · spawn ที่เกิน cap ยัง**คิว**ผ่าน `ResourceGovernor` เดิม (ไม่ปฏิเสธเงียบ) —
  `_describe_resource_wait` เพิ่มข้อความเฉพาะ `global_panes_limit` บอก RAM free % ตรงๆ แทนโค้ด reason เปล่าๆ
  + วัดบนเครื่องจริง (39.64GB total / 11.23GB available วันนี้): cap คำนวณได้ 5 panes
  + tests: `test_orchestrator_auto_assign_mode.py` (ทุกเงื่อนไข exclude + provider-gap fallback) ·
  `test_core_scheduling.py` (RAM-derived default/floor-at-1/fail-open/explicit-value-wins/missing-file) ·
  `test_cli_server.py` stagger tests ปักหมุด `mode: pane` ชัดเจนเมื่อทดสอบ pane-specific dispatch

- **Phase 10 ชิ้น 1c — ปิดช่องว่าง dual-write step 1 + mechanical audit กัน drift** (#362) — พบและปิด 3
  mapping ใน `build_readonly_registries_step` ที่ไม่มี `dual_write_*` hook: `disabled-providers.json`
  (`provider_state.py`), `exec-mode.json` (`exec_mode.py`), `rtk-enabled.json` (`rtk_helper.py`) — ตามแพทเทิร์น
  เดิมทุกอย่าง (เขียน V1 ก่อน แล้ว mirror payload เดิมเข้า v2/ แบบ best-effort) · เพิ่ม
  `test_every_ladder_mapping_has_dual_write_or_documented_exception`: ไล่ mapping จริงจาก step 1/3/5 ทุกตัว
  เทียบกับตารางที่ประกาศไว้ — fail ถ้ามี mapping ใหม่ในอนาคตไม่มี hook/exception กัน gap แบบนี้เงียบๆ อีก
  (step 2/4 fan-out + step 6/7/8 exception เดิม ยืนยันแยกอีก 2 เทส) + audit table เต็มเป็น comment ในไฟล์เทส

- **Phase 10 ชิ้น 1b — dual-write state writers ตาม step 5 mapping จริง** (#362) — local-issues · issue-dedup ·
  autoresume · remote-sessions (`auto_issue_capture`/`issues`/`auto_resume`/`remote/session_store`) ผ่าน helper
  `dual_write.py` เดิม · **ตัดสินใจบันทึกไว้**: runtime fan-out dirs (sessions/tasks/role-memory/knowledge — step 8) **ไม่
  dual-write ต่อไฟล์** (ขนาดไม่มีเพดาน) พึ่ง `apply_pending()` ตอน boot sync เป็นรอบแทน · ไฟล์ที่ไม่อยู่ใน ladder mapping
  (plan.json/claude_auth_config/graft_store) ไม่ dual-write โดยเจตนา — mapping ของ ladder คือ source of truth
  + tests: mirror match / skip เมื่อไม่มี v2 / V2 write fail ไม่ raise / `migrate validate` ผ่านหลังเขียน

- **Workspace 1.2.0 เฟส 0–1 — Workspace Shell + Project Explorer** (#365, แผน `docs/plans/workspace-1.2.0-design/` +
  ข้อแก้ 3 ข้อใน `docs/plans/2026-08-23-master-dev-plan.md` §4) — `project_explorer.py` (view, `QTreeView` native —
  **ไม่มี WebEngine ใหม่**) + `project_file_index.py` (service: lazy dir listing บน worker thread, ignore policy
  `.git node_modules .next dist build coverage runtime venv .venv __pycache__` + เคารพ `.gitignore`, canonical-path
  containment ใต้ project roots, ปฏิเสธ traversal/symlink) · QSplitter ซ้ายใน `ProjectTab` collapse/expand จำ
  width/collapsed ต่อโปรเจกต์ (QSettings ตัวเดียวกับ window geometry) · context menu Open externally / Reveal / Copy path
  (Open in Takkub + Ask Agent = เฟส 2) · git status badge เป็น skeleton (`GitStatusService`, debounced) ต่อจริงเฟส 4 ·
  **`main_window.py` ไม่แตะเลย** (ลด surface ชน PR ตามกฎ) · ADR `docs/architecture/adr-workspace-shell.md` ·
  **บั๊กความปลอดภัยที่เจอระหว่างเขียน**: NTFS junction บน Windows รายงาน `is_symlink()=False` แต่ `resolve()` ทะลุ →
  junction ที่ชี้ออกนอก root หลุดผ่าน containment เงียบๆ ถ้า gate เฉพาะ symlink-flagged → re-verify containment ทุก entry
  (พิสูจน์ด้วย junction จริงผ่าน `worktree_manager._make_link`) · keepalive/project-switch เดิมไม่เปลี่ยน
  + tests: `tests/test_project_file_index.py` · `tests/test_project_explorer.py` · `tests/test_project_tab_explorer.py`
- **Workspace 1.2.0 เฟส 2 — Monaco read-only** (#365) — `editor_widget.py` (`EditorHost`) + `static/editor/index.html`:
  Monaco WebView **1 ตัวทั้งแอป** ใน `QDockWidget` นอก `ProjectTab` (RAM hard rule §4 override ต่อแผนภายนอกที่ขอ 1
  ตัวต่อโปรเจกต์) — lazy create ตอนเปิดไฟล์แรก, `deleteLater` เต็มรูปแบบเมื่อปิด tab ครบ (ไม่ใช้ = +0) · bundle Monaco
  local ไม่มี CDN (`static/editor/vendor/` ว่างในรอบนี้ รอ devops packaging, degrade เป็น read-only `<pre>` viewer
  ถ้ายังไม่มี bundle) · QWebChannel bridge: `requestDiff`/`openExternally`/`revealInExplorer`/`notifyTabClosed`/
  `askAgent` (placeholder เฟส 3+) · internal Monaco tabs ในตัวเดียว + diff-vs-HEAD เป็น per-tab view toggle (ไม่ใช่
  tab แยก) · read-only fallback ไฟล์ binary/เกิน 2MB (cap) · ทุก path ผ่าน `project_file_index.resolve_and_contain`
  เดิม, file read + git diff รันบน worker thread เสมอ · Explorer "Open in Takkub" (เปิดใช้จากเฟส 1 placeholder) +
  double-click → editor · terminal path-click เปลี่ยนจากเปิดด้วย OS app เป็น "Open in Takkub" (exec-extension guard
  เดิมไม่เปลี่ยน) ผ่าน `AgentPane.openInEditorRequested` → `Orchestrator.openFileInEditorRequested` · ยังไม่เขียนไฟล์
  (ไม่มี `saveFile` slot, Monaco `readOnly: true` ทุก model — Ctrl+S โชว์ toast แทน) เฟส 3 ค่อยเพิ่ม
  + tests: `tests/test_editor_widget.py` (pure read/diff helpers + `EditorHost` lazy-create/single-instance/destroy
  lifecycle ผ่าน stub `view_factory` — QWebEngineView จริงชน pytest abort บนเครื่องนี้แม้ offscreen)

## [v1.1.0] - 2026-08-23

### Fixed (แก้)

- **auto-migrate ตอน boot บนเครื่องที่ migrate ไปแล้ว (`layout=mixed`) รันแค่ version-marker ทุกครั้ง → ladder
  step ใหม่ที่เพิ่มทีหลัง (#360's `core-internal-store` และทุก step ในอนาคต) ไม่มีวันถูก apply เอง** (#362,
  ต่อจาก #360/#361) — `MigrationEngine.apply_pending()` ใหม่: รันเฉพาะ step ที่ journal ยังไม่มี entry
  `applied` หรือ `validate()` ของ step นั้นบอกว่ายังไม่ครบ (เช่น version-marker หลังอัปเวอร์ชัน) ตามลำดับ
  ladder จริง — step ที่ applied แล้วและ validate ผ่านถูกข้ามไปเลย ไม่เรียก `apply()` ซ้ำ · boot บน `mixed`
  เปลี่ยนจาก "step 1 เท่านั้น" → `apply_pending()` · failure handling แยกตามชนิด step: step ใหม่ที่ไม่เคย
  apply มาก่อนพัง → `MigrationEngine.rollback_step()` เฉพาะตัวนั้น + event `auto_migrate_rolled_back(step_id)`
  + retry-guard คู่ `(version, step_id)` กันรันซ้ำทุก boot จนกว่าเวอร์ชันจะเปลี่ยน ส่วน step เก่าที่เคย apply
  สำเร็จแต่ validate พังตอนนี้ **ไม่ auto-rollback** (จะทำลาย state ที่เคยดีอยู่) — แค่ log +
  `takkub doctor --storage-layout` WARN ใหม่ 2 อัน (`auto-migrate-stale`, `auto-migrate-pending-rollback`)
  ให้คนตรวจเอง · เทสจำลอง "prod วันนี้" จริง: 8 step เดิม apply สำเร็จ (pre-#360 ladder) แล้ว boot
  บน ladder 9 step วันนี้ → apply เฉพาะ `core-internal-store` ตัวเดียว
- **เทสเขียนไฟล์ลง `~/.takkub` จริงของเครื่องที่รัน → xdist worker ชนกัน `WinError 5` บน Windows CI (ตัวจริง
  ตัวสุดท้ายของ #349)** — `tests/conftest.py` `_isolate_runtime` redirect `role_models._PATH`/`provider_config`
  ไว้แล้ว แต่ไม่เคย redirect `config.SETTINGS_HOME` เอง และ module-level `_PATH = SETTINGS_HOME / ...` ของ
  `provider_models` (ตัวที่พัง: `test_save_apply_preserves_out_of_scope_role_override` → `os.replace` ชน →
  `except OSError` → `QMessageBox.critical` → modal guard จับได้ดังๆ) · `auto_resume` · `exec_mode` ·
  `plan_tier` · `provider_state` · `remote.config` · `pane_tools_policy` · `skill_policy` · `custom_roles`
  (+`CUSTOM_AGENTS_DIR`) ยังชี้ home จริง → isolate ทั้งหมด + patch `config.SETTINGS_HOME` ไปที่ tmp ทั้งก้อน
  (ปลอดภัย — ไม่มีโค้ด branch บน `SETTINGS_HOME == …` ต่างจาก `DATA_HOME`) · **guard กันกลับมาอีก**: autouse
  teardown snapshot ไฟล์ top-level ใต้ SETTINGS_HOME จริงก่อน/หลังทุกเทส — ถ้าอะไรเปลี่ยน = fail ดังทันที
  ไม่ต้องรอ CI แดงถึงรู้ว่ามีเทสหลุด

- **`takkub wait` ถูกตัดด้วย "interrupted by user input" ทั้งที่ user ไม่ได้พิมพ์** (#357) — ต้นเหตุ:
  xterm.js ส่ง terminal auto-reply (CPR `ESC[r;cR` · DA `ESC[?..c` · DSR · OSC reply · focus in/out)
  ตอน Lead TUI redraw หลัง cockpit inject digest/banner ผ่าน `onData` choke point เดียวกับ keystroke จริง
  แยกไม่ได้ด้วย API สาธารณะ (`wasUserInput` ไม่ expose) → แยกด้วย **byte-pattern** ที่ `_on_pane_input`:
  chunk ที่เป็น escape-reply ล้วน (ไม่มี printable/CR/LF) ไม่ stamp `user_input`/draft-tracker — คนพิมพ์จริง
  ไม่มีทางพิมพ์สิ่งเหล่านี้ · chunk ผสมยังถือเป็น user input (conservative) · เพิ่ม `takkub wait
  --no-interrupt` เป็น belt-and-suspenders: ride-out เฉพาะ reason=user_input โดย re-attach role ที่ยัง
  pending ไม่กระทบ interrupt จริงจาก role อื่น (#253)
- **merge proposal ข้อ cleanup ยังแนะ `git worktree remove` ดิบที่พัง "Filename too long" บน Windows** (#358) —
  fix ของ #226 อยู่ใน `takkub worktree merge/clean` เท่านั้น แต่ข้อความ proposal พา Lead ไปทางที่พัง
  (เกิดจริง 3/3 worktree ของ saas_admin วันนี้) → proposal แนะ `takkub worktree merge --role <r>` /
  `takkub worktree clean` แทน + regression test
- **delivery-uncertain / delivery-stale-reap false positive บนใบงานยาว → เสี่ยง assign ซ้ำ** (#359) —
  TTL 120s + resend budget คงที่ ไม่สเกลตามขนาด paste: pane รับใบงาน ~5KB จริงแต่ verify chain settle
  เป็น UNCERTAIN/expired ก่อน CLI จะโชว์ progress → เพิ่ม `Orchestrator._pane_shows_real_progress()`
  (state=working + output สดใหม่) เช็คก่อนแจ้ง Lead ทั้ง 2 จุด (`_warn_lead_delivery_uncertain`,
  `_reap_stale_deliveries`) — ไม่มี notice ถ้า pane ทำงานจริง (state ยัง reap ตามเดิม แค่ไม่ cry-wolf) ·
  **แก้ที่จุดกำเนิดด้วย**: `_on_settled` เทียบ `last_output_monotonic()` หลัง write กับ baseline ก่อน write
  — มี output ใหม่ = accepted แม้ `is_at_ready_prompt()` ยัง True · notice ที่เหลือบอกชัด "ถ้า status ยัง
  working ไม่ต้อง assign ใหม่" · ไม่เปลี่ยน policy ของ #255/#336/#339
- **`delivery_boot_timeout_failed` false positive ตอนเครื่องโหลดหนัก** (#356, auto-captured ×2) —
  `elapsed[0]` ใน `_send_when_ready._check()` ถูกใช้ร่วม 2 เฟส (รอ session เกิด — ไม่มี ceiling เพราะ
  spawn-gate defer ได้ไม่จำกัด / รอ boot marker — ceiling 300s) **ไม่เคย reset ข้ามเฟส** → เวลาที่ governor
  กัก pane ก่อน spawn (memory_low) รั่วเข้า budget ของ boot-marker ทำให้ล้ม ceiling ตั้งแต่ poll แรกหลัง
  session alive ทั้งที่ pane เพิ่งเริ่ม boot → capture `elapsed_at_session_alive` แล้ววัด ceiling จากจุดนั้น
  (ยัง tick-based ไม่ใช่ `time.monotonic()` เพราะเทสทั้งชุดพึ่ง synchronous QTimer mock)

- **Lead pane ค้าง "unrecognised" 9 ชม. ไม่มี auto-recovery (#343)** — ต้นเหตุจาก transcript ของ pane
  ตัวนั้น: hint line ของ claude (`⏵⏵ bypass permissions on (shift+tab to cycle)`) ที่ marker table ใช้จับ
  "ready" ถูกวาด**ครั้งเดียวตอน boot** แล้วไม่วาดซ้ำ (ink diff-render) พอ `/compact` redraw ทั้งจอ
  hint หาย เหลือกล่อง `❯` เปล่าๆ · events.log ยืนยัน 134 ครั้งติดกันจอเป็น `border | ❯ | border`
  เป๊ะทุกครั้ง และ pane เดียวกันช่วงเดียวกันยังโชว์ hint อีกแบบ (`… · esc to interrupt · ← for
  agents`) = claude หมุน hint หลายแบบ ไม่ใช่ string คงที่ — ไล่ wording เป็นเกม fragile ที่ #20 เคยเล่น
  → (1) **auto-recovery probe** ใน `_escalate_stale_marker`: resize-nudge (cols+1 แล้วคืน — ไม่ส่ง
  keystroke เพราะ keystroke เปลี่ยน state ของ CLI ได้ resize ไม่ได้ ปลอดภัยทุก provider) ยิงครั้งเดียว
  ต่อ escalation แล้ว**ดูจอใน sweep tick ถัดไป** (2 เฟส — child ต้องมีเวลา repaint จริงก่อน ไม่ใช่
  อ่าน screen ต่อทันทีหลัง resize ซึ่ง pyte ยังไม่มี byte ใหม่) · log `ready_marker_nudge` ·
  (2) **structural fallback เฉพาะ claude** `PtySession.is_at_claude_empty_composer()`: จับ 3 แถว
  ติดกัน border / `❯` ล้วน / border เท่านั้น — ไม่รับ `>` (กันชน shell prompt), มีข้อความค้างใน input
  ไม่ match, **ไม่ผูกเข้า `is_at_ready_prompt()`/marker table** เรียกเฉพาะจุดที่รู้ provider แล้ว —
  shell prompt หลัง claude ตายไม่มีทางวาดกรอบ unicode + `❯` จึงไม่กลบสัญญาณ crash ตามที่ใบเตือน ·
  หลัง nudge ถ้ารู้จัก/ตรง structural → เคลียร์ streak เงียบ ไม่ page Lead · ไม่งั้นดังทันที (🔴 +
  `_notify_lead`) ในรอบนั้น ไม่รอ streak อีก 3 รอบ
  + tests: `tests/test_stale_marker_detector.py` (+8: nudge ครั้งเดียว / recovered ไม่ดัง / provider
  อื่นดัง / จอเปลี่ยนระหว่างเฟสดัง / detector match-reject 4 เคส)

- **`worktrees/` สะสมซากที่ git ไม่รู้จักจนหลายสิบ GB — `takkub worktree clean` มองไม่เห็น** (#355) —
  วัดจากเครื่องจริง: `~/.agent-takkub/worktrees/TK-ERP` มี 44 โฟลเดอร์ 31.7 GB แต่ `git worktree list`
  รู้จักแค่ 2 · ซากไม่มี `.git` ไม่มี branch · 99% ของขนาดคือ `node_modules` ตัวละ ~770 MB ·
  `clean_isolated()`/`list_isolated()` iterate จาก `git worktree list` อย่างเดียว ไม่เคยมองดิสก์ จึง
  ไม่มีวันเห็น — ผู้ใช้รัน `clean` ได้ "ok: 2 worktree(s)" ก็นึกว่าสะอาดแล้ว · **ต้นเหตุที่ทำให้ซากเกิด
  ปิดไปแล้วตั้งแต่ #226/#227 (`9bd1a10`, 2026-08-15)** — `git worktree remove` เดิมเดินพาธธรรมดาไม่ใช่
  extended-length บน Windows พังกลางทางที่ `node_modules` ลึกๆ แต่ registration/branch ถูกลบต่อไปแล้ว
  (ตรงกับหลักฐานเป๊ะ: ไม่มี `.git` ไม่มี branch) ซากที่เจอสร้างเมื่อ 2026-07-28 = ก่อนไฟล์นั้น
  → เพิ่ม `WorktreeManager.list_orphans()` สแกน `<DATA_HOME>/worktrees/<project>/*` บนดิสก์เทียบกับ
  `git worktree list --porcelain` (จำกัดเฉพาะใต้ managed root — anchor ผิดตัวบน Windows อาจ resolve
  ไปถึง drive root แล้วเดินทั้ง `C:\` · live-pane guard #187 ใช้ร่วมกัน dir ที่ pane ถืออยู่ไม่ถูกนับ)
  · `takkub worktree clean` **รายงาน** orphan + ขนาดรวมเสมอแต่**ไม่ลบ** (อาจมีงานที่ git ไม่รู้จักค้าง)
  · `--orphans` ลบทั้ง dir ผ่าน `remove_worktree_tree()` เดิม (long-path/readonly-safe) ·
  `--orphans-node-modules-only` ลบแค่ `node_modules/` คืน ~99% ของพื้นที่แต่เก็บซอร์สไว้ — ทางที่
  ปลอดภัยกว่าสำหรับคนไม่แน่ใจ · regression test ยืนยันว่า flow ปัจจุบันที่ rmtree ได้บางส่วนยัง
  รายงาน leftover ดังๆ ไม่เงียบ · `dir_stats()` ย้ายจาก `disk_usage` มา `worktree_manager`
  (ทิศทาง import เดิมผิด contract — `disk_usage` import จาก `worktree_manager` อยู่แล้ว)
  + tests: `TestListOrphans` 6 เคส (`tests/test_worktree_manager.py`), CLI 6 เคส (`tests/test_cli.py`)

### Added (เพิ่ม)

- **Phase 10 ชิ้น 1 — dual-write ทุก V1 config writer เข้า `v2/`** (#362) — เหตุผลที่ต้องมาใน 1.1.0 พร้อม auto-migrate:
  ถ้า `v2/` ถูกสร้างแล้วแต่ Settings ยังเขียน V1 อย่างเดียว การเปลี่ยน model pin ครั้งแรกจะทำให้ `model_pin_v2_drift`
  ฟ้อง (และ auto-issue) ทั้งที่ไม่ใช่บั๊ก — dual-write คือสิ่งที่ทำให้ telemetry นั้นมีความหมาย · helper กลาง
  `core/storage/dual_write.py`: **V1 ยัง authoritative** — writer เขียน V1 atomic ก่อนเสมอ แล้ว V2 best-effort
  (`OSError` → log `v2_write_failed` ไม่ raise ไม่ทำ V1 พัง) · skip เงียบบนเครื่องที่ยังไม่มี `v2/` (สร้างเป็นงาน #361) ·
  caller ส่ง payload ที่เพิ่งเขียนมาเลย ไม่ re-read จาก path · V2 target path **reuse `RegistryMapping`/target-path
  methods ของ ladder จริง** ไม่คำนวณซ้ำ (ladder กับ dual-write ไม่ drift กันเอง) · writer ที่ครอบ: `role_models` ·
  `provider_models` · `pane_tools_policy` · `skill_policy` · `provider_config` routing (global + per-project merge) ·
  `custom_roles` · projects registry · state writers ไว้ชิ้น 1b
  + tests: `tests/test_core_storage_dual_write.py` (13) · `test_core_routing.py::test_no_drift_event_after_dual_write_
  when_pin_changed_via_role_models_save` — เกณฑ์ผ่านตรงตัว: แก้ pin ผ่าน `role_models.save` บน fixture ที่ migrate แล้ว →
  Router ไม่ log drift อีก · `test_provider_config.py` integration

- **`takkub doctor --ram` — RAM ต่อ pane แยก claude CLI / node·MCP / QtWebEngine** (#364 lever 6, วัดก่อนทุกอย่าง) —
  `ram_report.py` pure leaf (psutil ที่เป็น dep อยู่แล้ว, ไม่มี Qt/orchestrator import — contract `ram-report-layer`)
  เดิน process tree จาก pythonw ลง descendant ของแต่ละ pane pid · QtWebEngineProcess ที่ map ไม่ได้ **รายงานเป็น shared
  ไม่เดา** · `--json` เป็น baseline ก่อน/หลังของ lever อื่น · performance chip เพิ่ม "RAM top 3 (pane)" ใน tooltip ผ่าน
  QRunnable worker — ไม่เดิน tree บน Qt main thread · `PtySession.pid` public · `Orchestrator.pane_ram_specs()`/
  `ram_status()` + IPC `ram-status` · ตัวเลขจริงเครื่อง dev: claude ~566–637 MB/pane · **node/MCP ของ pane เดียว 717 MB**
  (→ lever 4 สำคัญกว่าที่คิด) · main pythonw 479 MB · shared QtWebEngine 4 process 449 MB · total cockpit 4.66 GB
  + tests: `test_ram_report.py` · `test_doctor_ram_live.py` · `test_orchestrator_ram_status.py` · `test_ram_chip.py` ·
  `test_pty_session_pid_property.py`

- **ladder step ใหม่ `core-internal-store` (step 8) — ย้าย Core V2 internal store `core_home()` → `v2/system/`** (#360) —
  gap ที่ phase8b ระบุไว้: 8 step เดิมย้ายเฉพาะ V1 config แต่ store ที่ Core V2 เขียนเองตั้งแต่ Phase 1–8a
  (`version.json`, accounts/model_catalog JSONL, secrets, conversations) ยังอยู่ที่ `RUNTIME_DIR/core` ·
  `CoreInternalStoreStep`: copy-never-move · **first apply = staging + `os.replace` atomic swap** เพราะ
  `paths.core_home()` fallback จะสลับไปอ่าน `v2/system/` ทันทีที่ dir มี — ห้ามให้เห็นครึ่งเดียว · re-apply =
  merge-in-place · **ยกเว้น `migration_journal.jsonl` + `migration_backups/` ของ ladder เอง** (ผ่าน
  `journal.store_path`/`backups.root` property ใหม่ ไม่ hardcode ชื่อซ้ำ) — journal ที่กำลังเขียนตัวเองอยู่
  ห้ามย้าย · validate เทียบไฟล์ต่อไฟล์ + dir presence · rollback ลบ/restore · `LEGACY_MAPPING` เพิ่ม
  `CORE_INTERNAL` ให้ `doctor --storage-layout` นับ step ถูก
  · **2 บั๊กที่ qa ดักได้ก่อน release** (เกิดบนเครื่องจริงแน่นอนเพราะ apply/validate/rollback เป็นคนละ process เสมอ): (1) `_excluded_names()` เทียบ parent ของ journal ที่ resolve แบบ dynamic — พอ `core_home()` สลับไป `v2/system/` หลัง apply, engine ใหม่ตอน `validate` มองไม่เห็นว่า journal ที่ค้างใน source ต้องยกเว้น → false mismatch → แก้เป็น exclude ตาม**ชื่อ**เสมอ · (2) journal/backups ของ ladder เองอยู่ *ข้างใน* target ที่ step นี้ rmtree ตอน rollback → `journal.record()` mkdir target กลับมาทั้งที่ว่างเปล่า → `core_home()` ค้างชี้ target แอปมองไม่เห็น accounts/conversations ทั้งที่ข้อมูลอยู่ครบใน source → เพิ่ม `paths.migration_home()` = path คงที่ `RUNTIME_DIR/core` (ไม่ flip) ให้ journal/backup ใช้เป็น default — ladder's own bookkeeping ต้องไม่อยู่ในสิ่งที่ ladder ย้าย/ลบ (engine.py ระบุเจตนานี้อยู่แล้ว) · เครื่องเดิมไม่ต้อง migrate journal (migration_home == pre-flip core_home) · regression test จำลอง engine คนละตัว (apply ด้วย A → validate+rollback ด้วย B)
  + tests: happy path · exclude journal/backups · fallback สลับ**หลัง rename เท่านั้น** (จำลอง copy fail
  กลางทาง) · rollback fresh/re-apply · parity กับ `AccountRegistry`/`ModelRegistry` จริง · default ladder = 9

- **auto `migrate apply` ตอน boot — ทุก device ได้ storage layout เดียวกันโดยไม่ต้องพิมพ์เอง** (#361, user
  directive: "auto ให้เพื่อนด้วยเลยตอนเปิด จะได้เหมือนกันทุก device") — `auto_migrate_boot.py` (pure Python
  ไม่มี Qt, เทส headless ได้) รันเป็น stage ที่ 2 ใน boot splash เดิม (`boot_update_window.py`) **หลัง
  provider-update ก่อน MainWindow/spawn pane เสมอ** (= จังหวะ "cockpit ปิด" ที่แผน §2.3 ต้องการ — ไม่มีใคร
  เขียนไฟล์ที่ ladder อ่าน; ordering test คุมไว้) · **pre-flight gate ต้องผ่านครบ**ถึงจะรัน ไม่งั้น skip + log:
  `layout_state() == "v1"` เท่านั้น (ห้ามทับ `mixed`) · ไม่ใช่ dev checkout (`DATA_HOME == REPO_ROOT`) ·
  ดิสก์ว่าง ≥ 2× ประมาณการ · เคย rollback แล้วไม่ลองซ้ำจนกว่า app version เปลี่ยน (กัน loop) · ปิดได้ด้วย
  `TAKKUB_AUTO_MIGRATE=0` (ชนะเสมอ) หรือ Settings → Core V2 toggle `auto_migrate` (default ON) · รัน
  `MigrationEngine.apply()` → `validate()` ตัวเดียวกับ CLI (ไม่มี ladder ใหม่) → ไม่ผ่าน = **rollback
  อัตโนมัติ** + event `auto_migrate_rolled_back` + doctor WARN · สำเร็จ = `auto_migrate_applied` · state ที่
  `SETTINGS_HOME/auto-migrate-state.json` · เกิน 20s splash ขึ้น "still working" ไม่มี timeout (ห้าม spawn
  ก่อนจบ) · **ทุก boot ถัดไปรัน `MigrationEngine.apply_pending()`** — เฉพาะ step ที่ journal ยังไม่ applied หรือ validate ไม่ผ่าน (รวม version-marker) → เครื่องที่ migrate แล้ว (`mixed`) ยังได้ ladder step ใหม่ที่เพิ่มทีหลัง เช่น step 8 `core-internal-store` โดยไม่ต้องพิมพ์เอง · step ใหม่พัง → `rollback_step()` เฉพาะตัว + retry-guard `(version, step_id)` · step เก่าที่เคยสำเร็จแต่ validate พังตอนนี้ → ไม่ auto-rollback แค่ log + doctor WARN — ปิดอาการ
  `version.json` ค้างเวอร์ชันเก่าหลังอัป · headless fallback เมื่อ `TAKKUB_BOOT_UPDATE=0` ·
  `doctor --storage-layout` บอก auto-migrate last run/result + เตือนเมื่อมี local-issue backlog ยังไม่ได้ส่ง
  (เครื่องที่ไม่มี `gh`) · **auto-issue signal ใหม่ 2 ตัว**: `auto_migrate_rolled_back` และ
  `model_pin_v2_drift` (≥1/24h) — เครื่องเพื่อนที่พังหรือ V2 ไม่ตรง V1 จะเปิด issue รายงานกลับมาเอง
  = 1.1.0 บนเครื่องเพื่อนเป็น "ตัวทดลองของเครื่องตัวเอง" ที่ฟ้องเองเมื่อไม่ตรง
  + tests: `tests/test_auto_migrate_boot.py` (24 — gate ทุกข้อ/happy/rollback+event/dev skip/mixed skip/
  toggle off/retry-guard/disk), `tests/test_boot_update_window.py` (worker + ordering),
  `tests/test_doctor_auto_migrate.py`, `tests/test_auto_issue_signals.py`, `tests/test_core_migration.py`

- **Core V2 Wave C ชิ้นที่ 2 — PermissionEngine rewire เข้า `cli.cmd_guard` (epic #309, แผน §1.2)** —
  `PermissionEngine` สร้างเสร็จตั้งแต่ Phase 5c แต่ `cmd_guard` (PreToolUse/Bash hook ที่ยิงทุก Bash call
  ของทุก claude pane) ยังเรียก `pane_guard.classify` ตรง → swap ให้ผ่าน
  `PermissionEngine().evaluate_shell_command()` · **กับดักที่แผน §1.2 ไม่ได้บอก**: `cmd_guard` ส่ง
  `mb_fallback_check` (#304 mb-shard escape hatch) และ `cwd` เข้า `classify()` แต่ `evaluate_shell_command`
  เดิมรับแค่ `(command, role)` — swap ตรงๆ จะทำ mb fallback กับ cwd rule พังเงียบทั้งที่เคลม neutral
  → ขยาย signature ให้ pass-through ทั้ง 2 kwargs verbatim ก่อน · รักษาสัญญา "Never raises. Any
  unexpected failure allows the command (exit 0)" ของ `cmd_guard` — engine import/construct/audit พัง
  → exit 0 ไม่ใช่ exit 2 · audit `capability.shell_command_denied` เฉพาะ DENIED ตามเดิม
  + tests: parity ทุก verdict ของ `pane_guard` ผ่านเส้นทางใหม่ + mb_fallback_check/cwd ถูกส่งจริง
  + fail-open · manual smoke ผ่าน hook จริงบน dev cockpit (DENIED → exit 2 + stderr + audit event)

- **Core V2 Wave C ชิ้นที่ 1 — model resolver wiring เข้า `core/routing/` (epic #309, แผน §1.3)** —
  `Router.effective_model_for(role, provider, project)` + façade `effective_model_for_v2` (flag-off =
  เรียก V1 ตรง / flag-on = ผ่าน Router / exception = fail-open กลับ V1 — รูปเดียวกับ
  `effective_provider_for_v2`) wire เข้า **ทั้ง 3 call site** ใน `spawn_engine.py` (generic-provider
  branch, claude-teammate branch, claude-lead branch) แทน `role_models.model_for(role, provider) or
  provider_models.model_for(provider)` — precedence/env override/`--model` override ไม่เปลี่ยน ·
  **behavior-neutral by construction**: V2 catalog คือสำเนา verbatim ของไฟล์ V1 เดียวกัน จึงต้องคืน
  ค่าเท่ากันทุกกรณี และพิสูจน์ด้วย parity matrix ที่รัน migrate step 1 จริง (ไม่ใช่ fixture มือ) —
  7 เคส precedence × provider + not-migrated + V2 JSON พัง + flag-off + Router raise ·
  รักษาเงื่อนไข **provider-match ของ role pin** (`role_models.model_for` คืนค่าเฉพาะเมื่อ entry.provider
  == provider) ผ่าน helper ใหม่ใน `model_catalog/legacy.py` ที่อ่าน `(provider, model)` ดิบจาก
  `aliases.json` — `ModelProfile` ไม่มี field provider ถ้าใช้มันจะทำ role ที่ pin model ของ codex
  หลุดไปใส่ claude · เครื่องที่ยังไม่ migrate (ทุกเครื่องที่ไม่ใช่ prod) → V1 byte-identical ·
  **V1 ยัง authoritative — V2 เป็น shadow-read**: เมื่อค่าต่างกัน (เช่น user แก้ pin ใน Settings
  หลัง migrate ซึ่งเขียน V1 อย่างเดียว ไม่ sync เข้า `v2/`) คืน V1 + log `model_pin_v2_drift`
  เป็นหลักฐานบน production ว่า V2 พร้อมเป็น source of truth หรือยังก่อน Phase 10 สลับ authority ·
  `tests/conftest.py` isolate `storage_layout_v2()` แบบแคบ (ไม่ repoint `config.DATA_HOME` ซึ่งจะพัง
  `provider_home_env` ที่เช็ค `DATA_HOME == REPO_ROOT`) — เพราะ dev checkout นี้มี `v2/models/*.json`
  จริงจากการซ้อม migrate หลุดเข้าเทส
  + tests: `tests/test_core_routing.py`, `tests/test_core_model_catalog.py` · เทส spawn เดิมทั้งหมด
  เขียวโดยไม่แก้ expected values

## [v1.0.87] - 2026-08-23

### Fixed (แก้)

- **#349 ปิดแล้ว — "full pytest ตายเงียบ exit 127" ไม่ใช่ native crash แต่เป็น modal dialog ที่บล็อกค้าง** —
  CI macOS จับได้ครั้งแรกด้วย instrumentation ที่ใส่ไว้ใน 1.0.86 (`--max-worker-restart=0` +
  `faulthandler_timeout=280`): worker `gw7` ค้างที่ `settings_window.py:1238` ซึ่งคือ
  `QMessageBox.critical()` ใน `except (OSError, ValueError)` ของ `_on_save_apply_clicked`
  — modal dialog บล็อก event loop รอคนกด OK ที่ไม่มีทางมีใครกดใน headless test จน faulthandler
  ฆ่าโปรเซสที่ 280 วินาที แล้ว xdist รายงานเป็น `worker crashed` ที่แยกไม่ออกจาก native abort ·
  thread อื่นว่างหมด มีแต่ main thread ค้างบรรทัดเดียว · ที่ **"1 ใน 3 รอบ"** และ reproduce
  ในเครื่องไม่ได้ เพราะเส้นทางนี้เดินก็ต่อเมื่อการเขียนไฟล์ raise `OSError` จริง ซึ่งเกิดเฉพาะตอน
  ดิสก์/สิทธิ์แกว่งบน runner
  → เพิ่ม autouse fixture `_block_qt_modals` ใน `tests/conftest.py`: patch static ของ modal
  **ทุกตัว** (`QMessageBox.{critical,warning,information,question,about}` ·
  `QInputDialog.{getItem,getMultiLineText,getText,getInt,getDouble}` ·
  `QFileDialog.{getExistingDirectory,getOpenFileName,getSaveFileName}`) ให้ **raise
  `UnexpectedModalDialogError` ทันทีพร้อมบอกว่า dialog ไหนถูกเปิด** แทนที่จะบล็อก — เทสที่เผลอ
  เดินเข้า error path จึงตกดังๆ ใน 0 วินาที แทนแขวน 280 วินาทีแล้วตายเงียบ · เทสที่ตั้งใจทดสอบ
  dialog ยัง `monkeypatch` ทับเองได้เหมือนเดิม (แพทเทิร์นที่ suite ใช้อยู่แล้ว) ·
  **ไม่แตะ production** — dialog ในโค้ดจริงถูกต้องแล้ว user ควรเห็น

- **cockpit ตัวที่สองที่เปิดจาก pane เขียนทับไฟล์ `port` ของตัวแรก → `takkub` ทุกคำสั่ง route ผิด instance** (#354) —
  pane ทุกตัวถูก stamp `TAKKUB_PORT_FILE` ของ cockpit ที่ spawn มัน (ตั้งใจ เพื่อ multi-instance)
  แต่ค่านั้น **inherit ต่อไปยัง process ลูก** — ถ้าลูกเป็น cockpit อีกตัว มันจะเอา path ของ
  instance แรกมาเป็นที่เขียนพอร์ต**ของตัวเอง** ทั้งที่ `DATA_HOME` คนละที่ ผลคือทุกคำสั่ง `takkub`
  ที่ยิงไป instance แรกถูก route ไปหา instance ที่สองแทน · อาการที่เห็นไม่บอกใบ้เลยว่าเป็นเรื่องพอร์ต:
  `unauthorized: lead-only command` / `unauthorized: ... requires a valid pane token` ทั้งที่
  `takkub list`/`doctor` ยังทำงานปกติ (แค่ตอบจาก cockpit ผิดตัว) — pane ถึงกับสรุปเองว่าเป็น
  stale pane token ฝั่ง server ซึ่งผิดทาง · แถมค่าที่ผิดยัง**ถูกอบเข้า env ของทุก pane ที่
  cockpit นั้น spawn ต่อ** แก้ไฟล์ `port` เฉยๆ จึงไม่พอ ต้อง restart cockpit
  → แยกการ resolve เป็น 2 ทาง: `config._get_port_file()` (**client-side** — pane/CLI ยัง honor
  override ทุกกรณีเหมือนเดิม สัญญาเดิมไม่เสีย) กับ `config._effective_port_file_for_app()`
  (**app-side** — ใช้ตอนเขียนพอร์ตและตอน stamp env ให้ pane) ซึ่งเชื่อ override เฉพาะเมื่อเป็น
  ตัวที่ process นี้ derive เองสำหรับ multi-instance, หรืออยู่ใต้ `DATA_HOME` ของตัวเอง,
  **หรือไม่มี pane marker (`TAKKUB_ROLE`) ใน env** — เงื่อนไขสุดท้ายสำคัญ เพราะ
  `_restart_env` ระบุสัญญาไว้ว่า "user override จาก shell ต้องอยู่รอดข้าม restart" ซึ่ง
  หน้าตาเหมือน leak ทุกประการถ้าดูแค่ path · `.resolve()` ทั้งสองฝั่งก่อนเทียบ (symlink/case
  บน Windows) · error `unauthorized` แนบ port + `DATA_HOME` ของ instance ที่ตอบ ·
  `takkub doctor` เพิ่มหมวด `[port] instance-match` ผ่าน command ใหม่ `instance-identity`
  + tests: `tests/test_config.py`, `tests/test_pane_port_file.py`, `tests/test_cli_server_auth.py`,
  `tests/test_doctor_port_identity_live.py` + sync-guard กัน literal ที่ duplicate ข้ามโมดูล
  (`config.py` ต้องคง `leaf-modules-pure` จึง import `_restart_env` ไม่ได้) drift เงียบ

- **flaky test ตัวที่ 5: `proc.wait(timeout=5)` รอ Windows Job Object ฆ่า process** —
  `TestKillOnCloseJob` ปิด job handle แล้วรอ `ping` ตาย ภายใน 5 วินาที ปกติเสี้ยววินาที แต่ตอน
  เครื่องโหลดหนัก (xdist 8 worker) kernel teardown เกิน 5 วินาทีได้ → ขยายเป็น 30 วินาที ·
  **ต่างจาก 4 ตัวก่อนหน้า**: timeout ตรงนี้ไม่ใช่ assertion ว่า "ต้องเร็วแค่ไหน" แต่เป็น safety
  bound กันเทสแขวนถาวร การขยายจึงไม่ทำให้เทสอ่อนลง — ถ้า Job Object ไม่ฆ่า process จริง
  เทสยังตกเหมือนเดิม แค่ตกช้าลง

- **flaky test ตัวที่ 4: assert thread set ทันทีหลัง `stop()`** —
  `test_enabled_starts_a_real_server_and_stop_tears_it_down` assert
  `set(threading.enumerate()) == before` ทันทีที่ `rc.stop()` คืนค่า — แต่ `stop()` join แค่
  accept-loop thread ของตัวเอง ส่วน handler thread ที่ `ThreadingMixIn` แตกออกมาตอน `_start()`
  ยิง loopback probe (`diagnostics.probe_local`) ไม่มีใคร track/join มันจบทีหลังเสี้ยววินาที
  ปกติไม่ทัน แต่บน windows CI ที่โหลดหนักมันโผล่ → bounded poll รอ thread set converge (5s)
  แล้วค่อย assert — leak จริงยังตกเหมือนเดิม (ไม่มีวัน converge) แค่ตกช้าลง 5 วินาที ·
  ข้อความ assert บอก `leaked=` / `missing=` แทน repr ทึบๆ ที่อ่านไม่ออกว่า thread ไหนเกิน
- **sweep แพทเทิร์น "assert post-condition ของงาน async ทันที" ทั้ง `tests/`** — flaky ทั้ง 4 ตัว
  ของวันนี้เป็นโรคเดียวกัน (thread เริ่มไม่ทัน / ตายไม่ทัน / นาฬิกาช้ากว่าที่คิด) จึงไล่หา
  `threading.enumerate()`/`active_count()`/`is_alive()` ที่ assert ทันทีหลัง
  `stop()`/`close()`/`terminate()`/`quit()`/`shutdown()` ทั้งหมด — **ที่เหลือปลอดภัยแล้วทุกตัว**
  (มี `join(timeout=)` ก่อน assert, ใช้ `_pump_until` เดิม, หรือ assert ค่าที่ capture ไว้ก่อน teardown)
  ไม่พบตัวใหม่

- **ไล่เก็บ assert เชิงเวลาที่เหลือทั้ง `tests/`** — sweep หา `assert elapsed < <ตัวเลข>` ทั้งหมด
  เจออีก 3 จุด แก้ 2 ปล่อย 1: `test_remote_http_server.py:1107` (`elapsed < 1.0` — **ซ้ำซ้อน**
  เพราะบรรทัดถัดไป `assert pending.reply.empty()` พิสูจน์สัญญาเดียวกันแบบ deterministic อยู่แล้ว →
  ลบทิ้งพร้อม `start`/`elapsed` ที่ไม่ได้ใช้) · `test_pty_spawn_timeout.py:57` (`elapsed < 3.0` →
  `assert not release.is_set()` ใช้ Event ที่มีอยู่แล้วพิสูจน์ว่า timeout ยิงตอน native call
  ยังค้างอยู่จริง) · `test_pty_backend_missing_cwd.py:33` **ไม่แตะ** — guard raise แบบ synchronous
  ไม่มี thread/sleep เข้ามาเกี่ยว ไม่ใช่ flaky-shaped

- **flaky test ตัวที่ 3: assert เวลาบนนาฬิกาจริง** — `test_timeout_path_leaves_task_unchanged_and_does_not_block_the_caller`
  วัด wall clock ว่า hook ต้องคืนค่าใน `< 1.0` วินาที (timeout ของ hook เอง 300ms) — บน windows
  runner ที่โหลดหนักโปรเซสถูก starve จน 300ms กลายเป็น 1.23s แล้วตก (production ถูกต้องแล้ว —
  `assert out == _TASK` ผ่าน แปลว่าเดินเส้น timeout จริง) → เปลี่ยนไปพิสูจน์สัญญาแบบ deterministic
  ด้วย `threading.Event` คู่ (`entered`/`release`): worker บล็อกค้างที่ `release.wait()` แล้ว assert
  `not release.is_set()` หลัง hook คืนค่า = พิสูจน์ว่าไม่ได้รอผลของ worker โดยไม่มีตัวเลขเวลาสักตัว
  · แข็งแรงกว่าเดิมด้วย: `assert entered.wait()` พิสูจน์ว่า worker ถูกเรียกจริง ซึ่งของเดิมไม่เคยเช็ค
  · `try/finally` ปล่อย thread เสมอ ไม่ทิ้งค้าง


### Fixed (แก้)

- **TOCTOU ใน `pythonLooksExecutable()` — CodeQL alert #29 (`js/file-system-race`)** —
  `npm/scripts/lib.js` เช็ค `statSync(py)` (isFile + size) แล้วค่อย `openSync(py)` อีกครั้งเพื่ออ่าน
  PE header = check-then-use บน **ชื่อไฟล์** คนละจังหวะ ไฟล์ที่ตรวจกับไฟล์ที่อ่านจริงอาจไม่ใช่ตัวเดียวกัน
  → เปลี่ยนเป็น `openSync()` ครั้งเดียวแล้ว `fstatSync(fd)` + `readSync(fd)` จาก descriptor เดิมตลอด
  (`closeSync` ใน `finally` ครอบทุกเส้นทางรวม early-return) ผลลัพธ์เหมือนเดิมทุกเคส ยังเป็น static
  check ล้วนไม่มี spawn/exec ตามเจตนาเดิมของ #341 · rule เดียวกับ alert #4 (`pathfix.js`) ที่เคยแก้ไปแล้ว
  + tests: 6 เคสใน `npm/scripts/lib.test.js` (ไม่มีไฟล์ / เล็กกว่า min size / เป็นไดเรกทอรี /
  win32 header ไม่ใช่ MZ / win32 header MZ / non-Windows) mock `process.platform` ทั้งสองฝั่ง

- **flaky test: `test_get_build_status_distinguishes_queued_from_in_flight`** — เทส start 5 thread
  แต่ sync แค่ 2 (`entered.acquire()` ×2 = รอเฉพาะตัวที่เข้า `_run_build`) แล้ว assert
  `len(_building) == 5` ทันที ทั้งที่อีก 3 ตัวแค่ต้องไปถึงบรรทัด `_building.add()` ซึ่งอยู่**ก่อน**
  semaphore (`graft_autobuild.py:615`) — บน windows runner ที่โหลดหนัก + xdist 8 worker
  thread ที่ 5 ยังไม่ทัน register → `assert 4 == 5` (production ถูกต้องแล้ว ไม่แตะ) → เพิ่ม
  bounded poll `_wait_until()` รอให้ครบก่อน assert (ไม่ใช่ sleep ตายตัว, ไม่ xfail/skip)
  assert เดิมครบทั้ง 3 ข้อ · flake ตัวนี้ซ่อนมานาน — xdist ที่เพิ่งเปิดใน 1.0.86 แค่ทำให้มันโผล่

- **`.gitignore` กฎ `v2/` กลืน `docs/v2/` ทั้งโฟลเดอร์** — กฎที่ตั้งไว้กัน output ของ
  `migrate apply` บน dev checkout (`<repo>/v2/`) ไม่ได้ anchor ไว้ที่ root มันเลย match
  `docs/v2/` ด้วย ซึ่งเก็บแผน/รายงาน Core V2 ทั้งชุด → `git add docs/v2/<ไฟล์>` fail
  และ `git add -A` ข้ามไฟล์ V2 ใหม่แบบเงียบๆ (ไฟล์ที่ track อยู่แล้วรอดมาได้เพราะ gitignore
  ไม่มีผลกับไฟล์ที่ track แล้ว จึงไม่มีใครเห็นปัญหาจนกว่าจะเพิ่มไฟล์ใหม่) → เปลี่ยนเป็น `/v2/`

- **flaky test ตัวที่ 2: `TestPermissionErrorRetry` patch `pathlib.Path.stat` ทั้งโปรเซส** —
  เทส monkeypatch `Path.stat` ที่ระดับคลาส แล้วนับ call ด้วย counter ที่ raise `PermissionError`
  2 ครั้งแรก — ระหว่างที่ patch ค้าง โค้ดอื่นในโปรเซสเดียวกัน (thread เบื้องหลัง) เรียก
  `mkdir()`/`is_dir()` ไปกิน quota ของ counter แล้ว `PermissionError` โผล่นอกเทสตัวเอง
  (traceback ชี้ `pathlib.py:1250 is_dir()` ไม่ใช่ assert ของเทส) → ให้ patch delegate กลับไป
  `real_stat` ทันทีถ้า path ไม่ใช่ไฟล์เป้าหมายของเทส ทำทั้ง `test_stat_retries_then_succeeds`
  และ `test_stat_gives_up_after_max_retries` (ตัวหลังเดิม raise ทุก path = อันตรายกว่า)

- **Wave B: `takkub migrate apply` รันจริงบน prod ครั้งแรก (epic #309)** — ก่อนหน้านี้ ladder
  ถูกพิสูจน์แค่กับ fixture V1 home และ dry-run แบบ read-only บน dev checkout เท่านั้น รอบนี้รันจริง
  บน `installed_merged` (`~/.agent-takkub`) → apply 8/8 + validate 8/8,
  `doctor --storage-layout` = `mixed` (คือสำเร็จ ไม่ใช่ `v2` — copy-never-move ทำให้ V1 ยังอยู่ครบ),
  `v2/` = 696.9 MB, ไม่มีไฟล์ V1 ไหนถูกลบหรือแก้ · รายงานเต็ม + ตัวเลข pre-flight ที่ต่างจาก
  baseline (อธิบายได้ทั้ง 4 จุด: คนละ home shape) อยู่ที่ `docs/v2/wave-b-apply-report.md`
  · ผลพลอยได้: model registry ของ Wave A ได้ทดสอบกับข้อมูลจริงครั้งแรก (แผน §1.3 ระบุเองว่า
  ทำไม่ได้จนกว่า apply จะรัน) — composite id อ่านข้อมูลจริงถูกต้อง

### Added (เพิ่ม)

- **Core V2 model registry store (`core/model_catalog/`) — epic #309 Wave A ชิ้นสุดท้าย** —
  `core/models/` เดิมมีแต่ dataclass และไม่มีใคร construct เลยนอกจากเทส → เพิ่ม `ModelRegistry` /
  `ModelProfileRegistry` (JSONL upsert-log + tombstone แพทเทิร์นเดียวกับ `core/accounts/registry.py`)
  และ `legacy.py` ที่แกะ blob ของ ladder step 1 (`{"schema","migrated_from","migrated_at","data"}`)
  จาก `models/registry.json` / `models/aliases.json` เป็น `ModelDefinition`/`ModelProfile` จริง
  fail-open ทุกทาง (ไฟล์ไม่มี/JSON พัง/`data` ผิดรูป → `[]` ไม่ raise — ซึ่งคือสภาพของทุกเครื่อง
  วันนี้ เพราะยังไม่มีใครรัน `migrate apply`) · แผน §1.3 เดิมระบุให้วางที่ `core/models/registry.py`
  ซึ่ง**ทำไม่ได้** — contract `core-models-pure` ห้าม `core.models.*` import `core.storage` เด็ดขาด
  จึงแยกเป็น package ใหม่แบบเดียวกับที่ `core/accounts/` ทำกับ `core/models/account.py`
  (แก้เอกสาร §1.3 บันทึกเหตุผลไว้แล้ว) · **ยังไม่ wire เข้า `core/routing/`** — resolver คือ Wave C
  ที่ถูกล็อกไว้หลัง `migrate apply` โดยตั้งใจ
  + 20 tests (`tests/test_core_model_catalog.py`)
  - **กันข้อมูลหายเงียบ:** `ModelDefinition.id` ใช้ composite `provider:model` ไม่ใช่ model id เปล่า —
    `provider-models.json` ให้ 2 provider ชี้ model เดียวกันได้ (claude กับ cursor ต่างก็ `claude-sonnet-5`)
    แต่ registry key ด้วย `id` ตัวหลังจะทับตัวหน้าทิ้ง แล้ว `for_provider("claude")` คืน `[]`
    ทั้งที่มี — bug class เดียวกับ #340/#341/#346/#350


- **CI job `npm-wrapper` — เทส JS ของ npm wrapper ไม่เคยรันใน CI มาก่อนเลย** — `.github/workflows/ci.yml`
  ไม่มี Node step สักบรรทัด (`grep 'npm test' / 'node --test' / 'setup-node'` = 0 matches) เทสทั้ง 15 ตัว
  ใน `npm/scripts/*.test.js` จึงรันเฉพาะตอนมีคนพิมพ์ `npm test` เอง — รวมถึง guard ของ #340
  (กัน publish wheel ผิดเวอร์ชันเงียบๆ อย่างที่เกิดกับ 1.0.84) และ guard ของ #341/#29 →
  เพิ่ม job `npm-wrapper` (ubuntu-latest, `timeout-minutes: 5`, `permissions: contents: read`,
  `setup-node@v4` lts/*, รัน `npm test` ตรงๆ — `package.json` ไม่มี dependency จึงไม่ต้อง `npm ci`)

## [v1.0.86] - 2026-08-23

### Fixed (แก้)

- **`migrate apply` step `runtime-triage` ทับผลของ step `state` แล้วรายงาน `ok` ทั้งคู่ + `rollback` ไม่คืน storage layout เป็น `v1`** (#350) —
  step 5 (`state`) เขียน `autoresume.json`/`remote.json` ตรงเข้า `v2/state/sessions/`; step 8
  (`runtime-triage`) copy `RUNTIME_DIR/sessions` **ทั้งโฟลเดอร์** เข้า V2 target เดียวกันด้วย
  `rmtree` แล้ว `copytree` ทับ — ลบไฟล์ของ step 5 ทิ้งเงียบๆ ทั้งที่ทั้งสอง step รายงาน `ok: true`
  (เจอเฉพาะตอนรัน `validate` แยกต่างหากทันทีหลัง apply) แก้โดยเปลี่ยนเป็น
  `copytree(..., dirs_exist_ok=True)` (merge เข้า target แทนการทับทั้งโฟลเดอร์) และเพิ่ม
  `MigrationEngine.apply()` ให้ re-validate ทุก step หลัง ladder รันจบ ดาวน์เกรด report เป็น
  `ok: False` ทันทีถ้า on-disk state จริงไม่ตรงกับที่ step อ้าง (กัน silent-success ธีมเดียวกับ
  #340/#341/#346) — อีกด้าน `migrate rollback` เดิม restore ทีละไฟล์แต่ไม่เคยลบ `v2/` root
  ทั้งก้อน ทำให้ `doctor --storage-layout` ค้าง `mixed` ตลอดไปหลัง apply ครั้งแรก (ชน pre-flight
  ของแผนเองที่ห้าม `apply` ทับสภาพ `mixed`) แก้โดยให้ `rollback()` ลบ `v2/` root ทิ้งทั้งก้อนเมื่อ
  ทุก step คืนสำเร็จครบ (ปลอดภัยตามดีไซน์ copy-never-move — V2 ไม่เคยเป็นที่เก็บข้อมูลตัวจริง)

- **QTimer.singleShot ของ spawn-stagger รั่วทิ้งไว้ 5 ตัวต่อรอบ spawn** (#345) — `QTimer.singleShot`
  แบบ static ผูก lifetime ไว้กับ receiver ที่ไม่มีใครถือ ทำให้ timer ค้างใน event loop ต่อให้ pane
  ถูกปิดไปแล้ว (spawn 20 panes = 100 timer ค้าง) → แก้เป็น instance-owned `QTimer` ที่ owner ถือ
  reference จริงและ `stop()`+`deleteLater()` ตอน teardown; ไล่ปิดจนเหลือ **0** ทุกเส้นทาง
  (`f17fe1d` + `aac5dd8`)

- **argv assembly ที่ extract ออกมาแล้วไม่เคยถูกเรียกใช้จริง** (#347) — `assemble_generic_argv` /
  `assemble_claude_argv` ถูกแยกออกมาเป็นฟังก์ชันบริสุทธิ์ (Wave A, #309) แต่ `spawn_engine` ยัง
  ประกอบ argv ด้วยโค้ดเดิมคู่ขนาน — เท่ากับมี logic 2 ชุดที่ drift จากกันได้เงียบๆ → wire เข้า
  call site จริงทั้งสองเส้น (`spawn_engine.py:2188`, `:2971`)

- **transcript parser ระเบิดเมื่อ schema ของทั้งไฟล์เปลี่ยน** (#348) — parser เดิม guard เฉพาะราย
  record แต่ไม่ได้ guard กรณี provider เปลี่ยนโครงทั้งไฟล์ (top-level ไม่ใช่ list/dict ที่คาด) →
  เพิ่ม whole-file schema guard คืน result ว่างแทนการโยน exception ขึ้นไปถึง UI

- **codex pane ค้างที่ `Starting MCP servers` ไม่มีวันจบ** (#351) — `_CODEX_SERVER_KEYS` ไม่เคย
  forward `startup_timeout_sec`/`tool_timeout_sec` ลง config ที่ generate ให้ codex ทำให้ MCP server
  ที่ handshake ไม่ตอบค้างแบบไม่มี timeout → forward ทั้งสองคีย์ + synthesize default
  `startup_timeout_sec=120` เมื่อ user ไม่ได้ตั้ง

- **deny-by-default พังบน codex 0.149** (#352) — เดิมมีแค่ `_CODEX_RESOLVE_SAFE_MIN_VERSION`
  (floor เปิดปลาย) เท่ากับเคลมว่า codex **ทุกเวอร์ชันในอนาคต** ปลอดภัย ทั้งที่ verify มาถึงแค่
  0.146 → เพิ่ม `_CODEX_RESOLVE_SAFE_MAX_VERSION = (0, 146, 0)` ปิดช่วงเป็น range ที่ทดสอบจริง
  เวอร์ชันนอกช่วงตกกลับเส้นทาง conservative แทนที่จะเชื่อเงียบๆ

- **shell pane กับ teammate pane ใช้ `CODEX_HOME` คนละที่** (#353) — `doctor.check_provider_isolation`
  คาด provider home ที่ถูก inject ไว้ แต่เส้นทาง shell pane ใน `spawn_engine` ไม่ได้เรียก
  `inject_provider_home_env` เลย codex/opencode ที่เปิดจาก shell pane จึงไปอ่าน config คนละชุดกับ
  ที่ cockpit เตรียมไว้ → เรียก `inject_provider_home_env` ในเส้นทาง shell pane ด้วย
  (`CODEX_HOME`, `XDG_DATA_HOME`/`XDG_CONFIG_HOME`)

- **CI แขวนเงียบได้ถึง 6 ชั่วโมง + xdist restart worker ที่ตายแล้วรันต่อโดยข้ามเทสไป ~174 ตัว** —
  `.github/workflows/ci.yml` ไม่เคยมี `timeout-minutes` เลยสักจ็อบ (เจอจริง: ubuntu ค้าง 24+ นาที
  ขณะที่ windows จบใน 3 นาที) และ xdist default จะ restart worker ที่ตายแล้วเดินหน้าต่อ ทำให้ run
  จบด้วยสถานะ "ผ่าน" ทั้งที่นับได้ 8442 จาก 8616 → ใส่ `timeout-minutes: 20` ทั้งสองจ็อบ,
  `--max-worker-restart=0` (worker ตาย = fail ดังๆ พร้อมชื่อเทส), `--timeout` ราย test และ
  `faulthandler_timeout = 280` ที่ยิงก่อน pytest-timeout — สำคัญเพราะ xdist ต่อเฉพาะ **stdout** ของ
  worker กลับมาที่ controller (stderr ปล่อย inherit) dump ของ pytest-timeout จึงหายไปกับ
  `os._exit()` ส่วน faulthandler เขียนลง stderr fd ที่ dup ไว้จึงรอด

### Changed (เปลี่ยน)

- **full test suite รันขนานด้วย pytest-xdist — 357s → 113s** — `takkub qa-gate` (full) ใช้
  `-n <workers> --dist loadscope` (cap 8 worker, override ด้วย `TAKKUB_QA_XDIST_WORKERS`);
  `--targeted` ยังรัน serial เหมือนเดิม
- **`docs-verify` เร็วขึ้น 390 เท่า — 93s → 0.24s** — `verify_symbol()` เดิม glob + อ่านไฟล์ `.py`
  ทั้ง 277 ไฟล์ **ใหม่ทุกครั้ง** ต่อ doc symbol 1 ตัว (337 ตัว) = O(refs × files) → เปลี่ยนเป็น
  index รอบเดียวแล้ว cache (`_build_symbol_index`, `functools.cache`) แล้วเช็ค membership O(1)
  ผลลัพธ์เท่าเดิมทุกประการ (44 results) · `docs-verify` เป็น pre-commit hook ด้วย ทุก commit
  ที่ผ่านมาจ่ายค่านี้อยู่

### Fixed (test infra)

- **wheel build ชนกันข้าม xdist worker** — `tests/test_installed_mode_gate.py` ให้ทุก worker
  `python -m build` เข้า `build/` เดียวกันพร้อมกัน (3 worker = 3 error) → build **ครั้งเดียวต่อ
  pytest run** หลัง cross-process lock (`os.O_CREAT|os.O_EXCL`) + shared wheel cache ที่
  `tmp_path_factory.getbasetemp().parent` — เร็วขึ้นอีก ~60s ด้วย
- **`_main_thread_heartbeat_age` รั่วข้ามเทส** — `test_single_instance_watchdog` เขียน module
  global ทิ้งไว้ ทำให้ `test_no_content_watchdog_cap` `RecursionError` เมื่อ xdist จับคู่ไฟล์คนละ
  ลำดับ → reset ใน autouse fixture ของ `tests/conftest.py`
- **`terminate()` race ไม่ใช่ state รั่ว** — `terminate()` default `wait=False` ทำให้
  `_writer.quit()`/`_reader.quit()` วิ่งบน daemon thread ขณะที่ `request_stop()` ทำแบบ sync เทส
  ที่ assert ผลของ teardown จึงตกเฉพาะตอนรันเต็ม → เทสเรียก `terminate(wait=True)` (โค้ด
  production ไม่แตะ — async teardown ตั้งใจให้ Qt main thread ไม่ค้างตอน `taskkill /T`)
- **assert ที่ผูกกับความยาว tmp path** — `test_done_note_symmetrize` ตกเฉพาะ macOS
  (`/private/var/folders/...` + `popen-gwN` ของ xdist ยาวเกิน) → assert สัญญาการย่อโน้ตจริง
  แทนความยาวรวม

## [v1.0.85] - 2026-08-22

### Fixed (แก้)

- **`takkub release` ไม่ build wheel ใหม่ → npm publish แนบ wheel เวอร์ชันเก่าไปเงียบๆ** (#340) —
  `dist/` เป็น gitignore ไม่มีใครเห็นว่าค้าง wheel เวอร์ชันก่อนอยู่ ตอน publish 1.0.84 tarball
  แนบ `1.0.82-py3-none-any.whl` ไปแทน ผู้ใช้ `npm i -g agent-takkub@latest` ได้โค้ดเก่าโดยไม่มี
  อะไรเตือน → root cause: 3 จุดพร้อมกัน (1) `release()` bump เวอร์ชันแต่ไม่เคย build wheel เอง
  (2) ไม่มี guard เทียบเวอร์ชันก่อน publish (3) `postinstall.js`'s `findWheel()` เลือกด้วย
  `readdirSync().sort()` ตัวสุดท้าย — string sort ทำให้ "1.0.9" > "1.0.10" → แก้ทั้ง 3: `release()`
  เรียก `build_wheel()` (`python -m build --wheel`, ลบ `dist/*.whl` เก่าทิ้งก่อนเสมอ) หลัง commit+tag
  ของจริง; `npm publish` มี `prepublishOnly` (`npm/scripts/preflight-publish.js`) เทียบเวอร์ชันใน
  `package.json` กับชื่อไฟล์ wheel ใน `dist/` ตายทันทีถ้าไม่ตรง/ไม่มี/มีมากกว่า 1 ไฟล์; `postinstall.js`
  เลือก wheel จากเวอร์ชันที่ประกาศใน `package.json` ตรงๆ (`findWheelForVersion`, shared ใน
  `npm/scripts/lib.js`)
  + tests: `TestBuildWheel`/`TestReleaseWheelStep` (`tests/test_release.py`),
  `npm/scripts/lib.test.js`, `npm/scripts/preflight-publish.test.js`

- **takkub CLI ตายเงียบทั้งระบบเมื่อ venv interpreter ถูกเขียนทับ** (#341) —
  `venv/Scripts/python.exe` ถูกโปรเซสภายนอกเขียนทับด้วยไฟล์ 29 ไบต์ (ไม่ใช่ PE จริง)
  ระหว่างเซสชัน แต่ launcher ทุกตัว spawn มันตรงๆ โดยไม่เช็คก่อน → CreateProcess ล้ม
  แบบเงียบหรือ hang กู้คืนยาก ผู้ใช้เห็นแค่ "ไม่มีอะไรเกิดขึ้น" ไม่มี diagnostic เลย →
  แก้ 3 จุด: (1) launcher ทั้ง 4 ตัว (`npm/bin/takkub.js`, `npm/bin/agent-takkub.js`,
  `bin/takkub.cmd`, `bin/takkub`) ตรวจสุขภาพ interpreter แบบ **static** (ขนาดไฟล์ +
  PE magic bytes บน Windows) ก่อน spawn ทุกครั้ง — จงใจไม่ execute เพื่อทดสอบ เพราะ
  การ spawn ไฟล์ที่พังคือขั้นที่ทำให้ระบบ lock/hang จนกู้คืนยาก พร้อม diagnostic
  + วิธีกู้คืนแทนที่จะ exit เงียบ (2) `cli.py` main(): write-path commands (send/
  done/issue/assign/goal/…) ที่ได้ `ok=True` แต่ไม่มี `msg` ยืนยันจาก daemon ถูก
  treat เป็น failed (exit non-zero) แทนอ่านเป็น success เงียบๆ — defense-in-depth
  เผื่อกรณีอื่นที่ไม่ใช่ interpreter พัง (3) `doctor.py::check_installed_integrity()`
  เพิ่มเช็ค venv-python โดยอ่านไฟล์บนดิสก์ตรงๆ (ไม่ใช่ image ที่โหลดอยู่ใน memory)
  ทำให้ cockpit ที่กำลังรันอยู่ (image เก่ายังดี) ตรวจจับความเสียหายกลางเซสชันได้
  แม้ subprocess ใหม่จะรันไม่ได้แล้วก็ตาม
  + follow-up: (2) เองก็มี false-failure — `takkub issue show` fail ทุกครั้งแม้
  สำเร็จจริง เพราะ `cmd_issue_show` คืน `{ok: True, msg: ""}` เสมอ (เนื้อหา print
  ตรงไปยัง stdout แยกจาก `msg` field) ถูก write-confirmation guard ใหม่ override
  เป็น False → แก้ด้วยตั้ง `quiet=True` เพราะ confirmation จริงอยู่ที่เนื้อหาที่
  print ไปแล้ว
  + tests: `tests/test_cli_write_confirmation.py`, `tests/test_doctor.py`,
  `tests/test_issues.py`

- **`main_thread_stall` เกิดซ้ำทุก 5s tick** (#342) — ยืนยันจาก main-thread stack
  trace 2 dump ตรงกันใน boot.log: `Orchestrator._idle_watchdog` (5s tick) →
  `_check_idle_teammates` → `_check_stuck_tool_panes` → `effective_provider_for`
  → `_provider_available` เรียก `shutil.which()` เดิน PATH สดบน **Qt main thread**
  ทุก tick ทุก pane ที่ working อยู่บน codex/gemini-agy ข้ามทุกโปรเจกต์ที่เปิด →
  แก้ด้วย TTL cache 15s เฉพาะ CLI-installed probe (`shutil.which`) ตัวที่แพงจริง
  + regression ที่ตามมาในรอบเดียวกัน: cache รอบแรกครอบ `is_disabled()` ด้วย ทำให้
  `settings_window._sync_role_provider_badge()` (ต้องอ่าน provider_state สดทันที
  ทุกครั้งที่เปลี่ยน combo) เห็นค่า disabled ค้างได้นานถึง 15s — module-global
  cache ไม่มี reset hook เลย ทำให้ค้างข้ามเทสด้วย (root cause ของ
  `test_substitute_badge_shown_when_selected_provider_unavailable` ที่ qa เจอ
  flaky) → แยก cache ให้ครอบเฉพาะ CLI-installed probe ส่วน `is_disabled()` อ่านสด
  เสมอ (เป็น JSON-file read เล็กๆ ไม่ใช่ตัวที่แพง) เพิ่ม
  `reset_provider_available_cache()` ให้เทส reset ได้ เรียกจาก conftest.py
  autouse fixture กันค้างข้ามเทสไฟล์อื่น
  + ค้างเป็น follow-up: #343 (Lead pane sat unrecognized ~9 ชม. — ไม่ใช่ signal
  เดียวกับ main_thread_stall, temporal correlation อ่อน จึงเปิดเป็น issue แยก
  รอ live reproduction)
  + tests: `tests/test_provider_config.py`

- **pytest suite ตายเงียบแบบ native abort กลาง CI windows — ไม่มี traceback ไม่มี
  summary** (#344) — จุดตายขยับทุกรอบ (35% ใน repro venv, 46%/51%/77% ข้าม CI run)
  = timing-dependent ไม่ใช่บั๊กของเทสตัวใดตัวหนึ่ง root cause: QTimer ที่รั่วจาก
  เทสก่อนหน้ายังยิงต่อใต้ QApplication เดียวที่ `conftest.py::_qt_session_app`
  ถือไว้ทั้ง session — เมื่อ exception หลุดจาก slot ที่ Qt เรียกจาก C++ โดยไม่มี
  `sys.excepthook` มารับ (ติดตั้งเฉพาะ GUI entrypoint จริงใน app.py) PyQt6 default
  คือ qFatal() abort ทั้งโปรเซสทันที (บั๊กคลาสเดียวกับ console-sweeper ที่แก้ไป
  แล้วใน 33dcf5d) → แก้ 2 ชั้น: (1) `conftest.py` ติดตั้ง sys.excepthook/
  threading.excepthook/unraisablehook แบบเดียวกับ app.py แปลง hard-abort ให้
  กลายเป็น traceback ที่อ่านได้ ส่งตรง stderr จริง (bypass capsys) สะสม fail ที่
  session end แทนกลบเงียบ + generic QTimer leak tracker (audit พบ Orchestrator
  arm timer จริง 3 ตัวไม่ใช่ 1 ตามที่ issue ระบุตอนแรก — `_idle_watchdog`/
  `_resource_timer`/`_hot_md_timer` รั่วอยู่ ~15 ไฟล์) track ทุก QTimer ที่ถูก
  สร้าง stop ตัวที่ยัง active หลังเทสจบ report แบบ non-fatal ผ่าน
  `pytest_terminal_summary` (2) `Orchestrator.shutdown_timers()` เป็น
  stop-everything call ตัวเดียว (test-only — Orchestrator สร้างครั้งเดียวต่อ
  process ไม่เคยถูกแทนที่ระหว่างรัน ส่วน teardown จริงจบด้วย process exit เสมอ
  ไม่ใช่ hook ที่ตกหล่นในโปรดักชัน) สลับ 66 ไฟล์เทสจาก ad-hoc partial stop มา
  เรียกตัวเดียว ปิด leak เดียวกันใน `CliServer._reaper`/`ProjectNav._pending_timer`/
  `ProjectTab._tab_status_timer`/`LeadNotifier._timer` ผ่าน shared fixture helper
  `tests/_qt_timer_leak_guard.py`
  + verify: full suite exit 0 ไม่มี native abort (ก่อนแก้ตาย exit 139 ที่ ~35%)
  + ค้างเป็น follow-up: #345
  + tests: `tests/conftest.py` (excepthook guard + leak tracker),
  `tests/test_provider_toggle_orchestrator.py`,
  `src/agent_takkub/orchestrator.py::shutdown_timers()`,
  `tests/_qt_timer_leak_guard.py` (+ 68 ไฟล์เทสสลับมาเรียก `shutdown_timers()`)

- **agy/Antigravity ค้างที่ banner "Verifying your account" แล้ว cockpit อ่านผิดว่า
  ready → blind-paste ใบงานทิ้ง หายเงียบ** (#346) — ตอนเปิด issue เข้าใจผิดว่า root
  cause เป็น `is_at_trust_prompt` (สืบแล้วคืน False ตลอด ไม่เกี่ยว) ตัวจริงคือ
  `is_at_ready_prompt()` คืน True ผิด มาจาก `ReadyRule("please try again shortly",
  True)` ใน `gemini_spec` ที่ใส่ไว้เมื่อ 2026-07-25 บนสมมติฐานว่าวลีนี้แปลว่า
  account check ล้มแล้วตกกลับมาที่ composer ปกติ — หลักฐานสด 2026-08-22 พิสูจน์ว่า
  ผิด: CLI แสดงวลีเดียวกันได้ทั้งที่ค้างสนิทไม่รับ input เลย ผลคือ delivery loop
  เห็นว่า ready แล้ว blind-paste ใบงานลงไปในตัว banner ใบงานหายเงียบ แล้วรายงานเป็น
  `delivery-uncertain` ให้ Lead ไปเดาเอง ผลกระทบจริง: พา user ไปไล่แก้ผิดคลาส (คิดว่า
  เป็นบั๊ก paste timing แทนที่จะเป็น false-ready classification) → แก้: ลบ ReadyRule +
  bypass ที่ผิดออกจาก `provider_spec.py` และ `pty_session.py::_classify_ready`
  (พร้อม self-test case ที่เคย expect ผิดด้วย) เพิ่ม tier ใหม่
  `account_pending_markers` แยกจาก `auth_transient_markers` เพราะ "sign in ใหม่"
  กับ "รอ Google verify account" ต้องบอก user คนละแบบ (อันแรกแก้ได้เดี๋ยวนี้ อันหลัง
  ทำได้แค่รอหรือสลับ provider) เพิ่ม state ใหม่ `blocked:provider-account` พร้อม
  auto-recover (close + respawn + degrade เป็น claude) และไม่ยิง auto-answer Enter
  ใส่ state นี้เลยเพราะเป็น banner ไม่ใช่ prompt + ตรวจ multi-provider ครบ:
  claude/codex/opencode/cursor ไม่มี marker ทำนองนี้, kimi's "send /login to login"
  เป็น login failure จริงคนละเคสจึงไม่ย้าย, ปล่อย tuple ว่างไว้จนกว่าจะเห็นจอจริงของ
  provider นั้นไม่เดาจาก docs
  + audit doc: `docs/audit/2026-08-22-346-agy-account-pending-misclassified-as-ready.md`
  + tests: `tests/test_delivery_account_pending.py` (ใหม่),
  `tests/test_pty_ready_prompt.py` (ใหม่) + แก้ของเดิม 4 ไฟล์

## [v1.0.84] - 2026-08-21

### Changed (เปลี่ยน)

- **Core V2 เปิดเป็นค่าเริ่มต้นแล้วทั้ง 5 ตัว** (#309 — ขั้นสุดท้ายก่อน 2.0.0) —
  `TAKKUB_V2_ROUTER` / `_CONVERSATION` / `_CONTEXT` / `_BRAIN` / `_SCHEDULER` เคยปิดหมดตลอด
  Phase 1–9 แล้วเปิดใช้จริงทั้ง 5 ตัวบน cockpit prod เต็มวันทำงาน ซึ่งเป็นสิ่งที่ทำให้เจอ
  #332–#337 · แก้ครบแล้วใน 1.0.83 → "V2 เปิด" กลายเป็นตัวโปรแกรมจริง ส่วน `TAKKUB_V2_*=0`
  กลายเป็นทางออกฉุกเฉินแทน (plan §0 rule 3 "ปิด flag = พฤติกรรมเดิมเป๊ะ" ยังใช้ได้เหมือนเดิม
  แค่กลับด้านกัน)

  **เครื่องที่เคยตั้งค่าไว้แล้วไม่ถูกทับ** — `core_v2_settings.load()` วางค่าที่บันทึกไว้ทับ
  default เสมอ ดังนั้น `false` ที่อยู่ในไฟล์คือการตัดสินใจของผู้ใช้ ไม่ใช่ช่องว่าง มีแต่เครื่องที่
  ยังไม่มีไฟล์ (หรือ key ที่ไม่เคยมี) เท่านั้นที่รับค่าใหม่ · เทสใหม่ 2 ตัว pin ทั้งสองทาง

  เทสที่เคยพึ่ง "default = ปิด" ถูกแก้ให้**บอกเจตนาตรงๆ** (`setenv(..., "0")` แทน `delenv`)
  19 ตัวใน 10 ไฟล์ — เพราะหลังจากนี้ "ไม่ได้ตั้ง env" ไม่ได้แปลว่าปิดอีกต่อไป


## [v1.0.83] - 2026-08-21

### Fixed (แก้)

- **`takkub send` ตามหลัง `assign` ทิ้งใบงานที่ยังไม่ยืนยันว่าถึงมือ แล้วรายงานว่า "ปลอดภัย"** (#336) —
  `#295` แยก "ยกเลิกได้/ไม่ได้" ไว้ถูกแล้ว แต่ตารางสถานะไม่ครบ: `_UNDELIVERED_STATES` มีแค่
  `QUEUED`/`WAITING_RESOURCE` ทำให้ `UNCERTAIN` ถูกนับเป็น "ถึงมือแล้ว" ทั้งที่มันแปลตรงตัวว่า
  **ไม่รู้ว่าถึงหรือไม่ถึง** (สัญญาณ "ready + ช่องพิมพ์ว่าง" แยกไม่ออกว่า submit ไปแล้วหรือค้างอยู่
  ในช่อง — ความกำกวมตัวเดียวกับที่ทำให้ auto-repaste ถูกปิดไว้ตั้งแต่ #134/#328) · ของจริง: gemini
  UNCERTAIN 15:48:33 → โดน supersede 15:49:14 → transcript ไม่มีใบงานเลย ขณะที่ `takkub status`
  ยังบอก `working` ต่ออีก 4 นาที → เพิ่ม `_UNCONFIRMED_STATES` (สถานะกำกวมต้องตอบ "ไม่" เมื่อคำถามคือ
  "ทิ้งได้ไหม") + แยกข้อความเป็น `⚠️ [delivery-pending]` (ยังไม่ถึงมือ เดี๋ยวส่งให้เอง) กับ
  `🚨 [delivery-unconfirmed]` (ไม่ยืนยัน จะไม่ paste ซ้ำให้ ต้องเช็ค transcript แล้ว assign ใหม่)

- **Context Builder ตัด record project-scoped ของ role อื่นทิ้งทั้งหมด** (#335) — filter เขียนว่า
  `agent_id in (None, role)` พร้อม comment ว่า "PROJECT-scoped มี agent_id=None" ซึ่งไม่จริง:
  `digest_facts_source.from_digest_facts` stamp role ลงไปด้วย ผลคือ record `COCKPIT_MEASURED`
  (trust สูงสุดใน store) ของทุก role อื่นหายหมด — วัดจริงบน store 34 records: `recall 7 → filter 2`
  frontend ไม่มีทางเห็นสิ่งที่ qa เพิ่ง verify ให้ · แก้เป็นกรองด้วย **scope** (จำกัดเฉพาะ
  `Scope.AGENT` ซึ่งเป็นเจตนาเดิม) + เทียบ base role เพื่อให้ shard pane (`frontend#3`) อ่าน memory
  ของ role ตัวเองได้

- **`backpressure` rung QUEUE ทริกที่ backlog=2 ทุกเครื่อง** (#334) — `resource_governor` ไม่เคยส่ง
  `active_capacity_hint` ให้ `BackpressureSignal` เลย จึงตกไปใช้ default `1` ทำให้
  `backlog_threshold = 1 x 2.0 = 2` คือพอ overload latch ติด + มีงานรอ 2 ชิ้น ก็กระโดดข้าม
  `PAUSE_BACKGROUND` ไป rung แรงสุด (หยุดรับงาน NORMAL ทั้งหมด) ไม่ว่าเครื่องจะรัน 1 หรือ 10 pane ·
  ส่ง `max(1, len(self._tokens))` = งานที่ถือ token อยู่จริง (ตัวเดียวกับที่ `_active_counts()`
  รายงานเป็น `agents_global`)

- **`recall()` ไม่มี relevance floor — assign ที่ไม่เกี่ยวก็โดน inject memory ทุกครั้ง** (#333) —
  BM25 บน corpus ระดับไม่กี่สิบ document ใช้เป็นสัญญาณเดี่ยวไม่ได้: idf ทำให้ token ที่บังเอิญหายาก
  ดูมีความหมาย (วัดจริง "add pagination to the users table API endpoint" ได้ 4.53 จาก record เรื่อง
  git conflict ที่ overlap กับ query แค่คำว่า "the") ซ้ำร้าย normalisation เดิม `bm25 / max(bm25)`
  ดัน hit ที่ดีที่สุดเป็น 1.0 เสมอไม่ว่าจะอ่อนแค่ไหน และ signal ที่ไม่เกี่ยวกับ query
  (`scope+recency+confidence+trust` = 1.4) สูงกว่า `_W_BM25` เต็มสเกล = ของใหม่+trust สูงชนะของที่ตรง
  query เสมอ → เปลี่ยนเป็น `coverage x saturating(bm25)` + floor ที่ coverage 0.2 · ข้อจำกัดที่ยัง
  เหลือเขียนไว้ใน docstring: query ไทยสั้นที่มีแต่คำเชื่อมยังผ่าน floor ได้จาก trigram (ต้องใช้ word
  segmentation ถึงจะแก้)

- **1 `done()` เขียน memory 2 record เนื้อเดียวกัน → inject ซ้ำ กิน budget ครึ่งหนึ่ง** (#332) —
  `facade.on_pane_done` submit ทั้ง headline ของ agent (`scope=AGENT`) และ digest ที่ฝัง headline
  เดียวกันไว้ท้าย git facts (`scope=PROJECT`) และ dedup ของ `pipeline` จับไม่ได้ **โดยดีไซน์** เพราะ
  เทียบเฉพาะใน bucket `(kind, scope, project_id, agent_id)` เดียวกัน · แก้ที่ฝั่งอ่าน (ได้ผลกับ
  record ที่อยู่บนดิสก์แล้วด้วย) ด้วย token containment ≥ 0.7 เก็บตัวที่ข้อมูลมากกว่า — เกณฑ์มาจาก
  การวัด: คู่ note/digest ของ done เดียวกัน = 0.73–1.00, คู่ที่เป็นคนละ event จริง = 0.61

- **console เด้งเป็นพักๆ บน Windows 11 ทั้งที่ sweeper ทำงานอยู่** (#337) — sweeper พังสองชั้น:
  (1) `snapshot_console_hwnds()` แมตช์ชื่อคลาสเดียว `ConsoleWindowClass` ซึ่งเป็นของยุค conhost —
  บน Win11 default terminal คือ Windows Terminal ที่ Windows **COM-activate** ขึ้นมา host console
  ใหม่ โผล่เป็น `CASCADIA_HOSTING_WINDOW_CLASS` + `PseudoConsoleWindow` จึงไม่เคยเข้า snapshot เลย
  (2) `hide_own_console_windows()` พิสูจน์เจ้าของด้วยการเดิน parent chain หา cockpit แต่ COM
  activation ทำให้ parent เป็น `svchost.exe` → `services.exe` ไม่มีทางย้อนกลับมา · แก้: ขยายเป็น 3
  คลาส (spawn-time path พิสูจน์เจ้าของด้วย "ใหม่กว่า snapshot ก่อน spawn" ไม่พึ่ง parentage),
  กัน periodic sweeper ไม่ให้แตะ `CASCADIA_*` เด็ดขาด (นั่นคือ UI ของ Terminal ที่ user เปิดเอง) และ
  ลด `_CONSOLE_SWEEP_MS` 2000 → 250 เพราะ sweeper ซ่อน**ทีหลัง** ค่า interval จึง**คือ**ระยะแวบที่
  ผู้ใช้เห็น (วัดต้นทุนจริง 0.53 ms/sweep = 0.2% ของ 1 core)


## [v1.0.82] - 2026-08-21

### Fixed (แก้)

- **CI windows ตายเงียบเพราะ console sweeper ที่เพิ่งเพิ่มใน 1.0.81** — timer กวาดหน้าต่าง console
  ถูกสร้างเป็น QTimer ระดับ module **ไม่มี parent** แล้วสตาร์ตตอน spawn PTY พอเทสตัวหนึ่ง spawn
  PTY จริง timer ก็ยิงทุก 2 วิ ข้ามไปทุกเทสที่เหลือ ข้าม QApplication ที่ teardown ไปแล้ว →
  pytest abort ที่ 78% ไม่มี traceback ไม่มี summary (ตายที่
  `tests/test_settings_management_ui_phase4.py` ซึ่งเป็นเหยื่อ ไม่ใช่ต้นเหตุ) · หลักฐานที่ชี้ตัว:
  macOS + ubuntu เขียว มีแต่ windows ที่ตาย และของใหม่ที่รันเฉพาะ `win32` รอบนั้นมีตัวเดียวคือ
  sweeper นี้ → ไม่สตาร์ตเลยเมื่อ QPA เป็น `offscreen` (conftest ตั้งไว้ทุกเทส — headless ไม่มี
  หน้าต่าง OS ให้ซ่อนอยู่แล้ว timer จึงมีแต่โทษ) + `QTimer(app)` ผูกเจ้าของกับ application ให้ตายไป
  พร้อมแอป ไม่เป็น QObject ลอยข้าม teardown + 3 tests
- **release commit timeout 30 วิ สั้นกว่า pre-commit ของ repo นี้** — git commit ในขั้น release
  รัน hook ทั้งชุด (ruff · docs-verify · import-linter 25 contracts · depgraph freshness) ซึ่งเกิน
  30 วิได้ง่ายตอน cache เย็น แล้ว release ก็ abort + rollback พร้อมข้อความ "timed out" เปล่าๆ ที่อ่าน
  เหมือน git ค้างรอ credential ทั้งที่ hook แค่ทำงานตามปกติ (เจอตอน cut 1.0.82 ติดกัน 2 ครั้ง) →
  ขั้น commit ใช้ timeout 600 วิ + 1 test

## [v1.0.81] - 2026-08-21

### Fixed (แก้)

- **`takkub release` bump version ไม่ครบ — v1.0.80 ออกไปพร้อม `__version__` ค้างที่ 1.0.79** —
  เลข version อยู่ 4 ที่ (pyproject.toml · package.json · `agent_takkub.__version__` · CHANGELOG)
  และ `tests/test_version_sync.py` บังคับให้ตรงกันทั้งหมด แต่ `release()` เขียนแค่ 2 ที่ ที่ผ่านมา
  รอดมาได้เพราะคนที่ cut release แก้อีก 2 ไฟล์ด้วยมือก่อนรันคำสั่ง พอปล่อยด้วยคำสั่งล้วนๆ ก็ได้ tag
  ที่ version ไม่ตรงกันและ CI แดง → `release()` เขียน+stage ครบทั้ง 4 ไฟล์ และ rollback ครบทั้ง 4
  เมื่อล้มกลางทาง (v1.0.80 ที่ตกค้างถูกซิงก์ให้ตรงในคอมมิตนี้ — tag ที่ push ไปแล้วไม่แตะ) + 5 tests

- **RuntimeError จาก token-meter เมื่อปิด pane ระหว่างอ่าน** (#327) — worker thread ชื่อ
  `token-meter` emit `_tokenMeterReady` ใส่ `AgentPane` ที่ C++ object ถูกลบไปแล้ว ("wrapped C/C++
  object of type AgentPane has been deleted") — เป็น thread ธรรมดา ไม่ใช่ Qt thread exception เลย
  หลุดเป็น unhandled จนถึงตัว auto-capture → ครอบ emit ด้วย `try/except RuntimeError` ใน
  `_emit_token_meter()` (เช็ค "ถูกลบยัง" ก่อน emit ชนะ race ไม่ได้ — pane ตายได้ทุกจังหวะรวมถึง
  ระหว่างเช็คกับ emit) exception ชนิดอื่นยัง propagate ตามเดิม + 4 tests
- **auto-issue เปิดบั๊กจากเรื่องปกติ — `task_delivery_failed` เหมารวมของที่ไม่ใช่ความผิดพลาด** (#331) —
  state FAILED มาจาก 3 ทางที่ไม่เหมือนกันเลย: ใบงานไปไม่ถึง pane (พังจริง), ปิด pane ทั้งที่ยังมี
  delivery ค้าง (ปกติ), และ teammate รายงาน `takkub done --failed` (ปกติ — delivery สำเร็จด้วยซ้ำ)
  ทั้งสามนับรวมเป็นตัวเลขเดียว #331 จึงถูกเปิดจาก done --failed ธรรมดา 4 ใบ → `mark_failed(reason=…)`
  ทุก call site, auto-issue นับเฉพาะ reason ที่พังจริง, และ reason โผล่ในตัวอย่างใน issue body
  (เดิมบอกแค่ "×4" อ่านแล้วทำอะไรต่อไม่ได้) + 6 tests
- **`takkub qa-gate` ใช้กับโปรเจคที่ไม่ใช่ Python ไม่ได้** (#329) — root CLAUDE.md บังคับว่า qa-gate
  คือ entrypoint เดียวและห้ามพิมพ์ pytest ดิบ แต่ gate รู้จักแค่ pytest/ruff/lint-imports พอเรียกใน
  monorepo Node มันตายที่ `No module named pytest` แล้ว `--targeted` ที่ชี้ไป path ของโปรเจคนั้นก็
  หายเงียบ → กฎกลางบังคับเครื่องมือที่ใช้ไม่ได้ แล้วความผิดไปตกที่ specialist → `detect_project_kind()`
  แยกชนิดโปรเจคก่อน: Node → delegate ไป `verify.detect_stack` (test script / `tsc --noEmit` /
  eslint) พร้อม fail-fast เหมือนเดิม · ไม่รู้จัก → refuse พร้อมบอกว่าหา marker อะไรไม่เจอ ·
  `--targeted` บน Node ขึ้นเป็น step ที่บอกตรงๆ ว่า path เหล่านั้นไม่ได้ narrow อะไร ·
  แก้ถ้อยคำใน CLAUDE.md ให้กฎ "ห้ามพิมพ์ pytest ดิบ" ผูกกับ repo นี้เท่านั้น + 10 tests
- **gemini pane ค้าง folder-trust ไม่หลุด — auto-answer กด Enter ครั้งเดียวแล้วเลิกเฝ้า** (#330,
  regression ของ #186) — `_auto_trust` เจอ modal → `write("\r")` → `return` จบการ poll ทันที ถ้า
  Enter นั้นตกกลาง render แล้วถูกกลืน (การกลืนแบบเดียวกับที่ทำให้ paste ใบงานไม่น่าเชื่อถือ) ก็ไม่มีใคร
  เฝ้าต่ออีกเลย pane ค้างยาว — ตรงกับที่รายงาน: fan-out 3 pane ผ่าน 2 ค้าง 1 → เฝ้าจนเห็น modal
  **หายจริง** เท่านั้นถึงจะถือว่าสำเร็จ, กดซ้ำได้สูงสุด 5 ครั้งห่างกัน 1.5s (cap เพราะ
  `is_at_trust_prompt()` เป็น screen-scrape — TUI อนาคตที่หน้าจอปกติดันแมตช์จะโดนยิง Enter ทุก 500ms
  ไม่รู้จบ) · หมดเวลาแล้วยังติด → แจ้ง Lead ว่า pane นี้ **ยังไม่เคยได้รับใบงาน** (ถอนคำว่า "cockpit
  กำลัง auto-answer อยู่" จาก #186 ที่อ่านแล้วนึกว่าจัดการอยู่) + 7 tests
- **หน้าต่าง PowerShell เปล่าเด้งขึ้นมาเรื่อยๆ ระหว่างใช้งาน** — `pwsh.exe` คือ shell-tool host ของ
  codex บน Windows (#286 จดไว้แล้วว่าเป็น scaffolding) แต่ตัวซ่อนหน้าต่าง console ทำงานเฉพาะ 3.5
  วินาทีรอบ spawn PTY เท่านั้น ส่วน pwsh เกิดทุกครั้งที่ agent สั่ง shell ซึ่งเป็นนาทีที่ 5/10/20 →
  ไม่มีใครกวาด → `hide_own_console_windows()` + timer ระดับ process ตัวเดียว (ไม่ใช่ต่อ pane —
  จะได้ไม่คูณ EnumWindows ตามจำนวน pane) กวาดทุก 2 วิ · **ซ่อนเฉพาะหน้าต่างที่เจ้าของ process เป็น
  ลูกหลานของ cockpit** — กวาดแบบไม่ดูเจ้าของจะไปซ่อน terminal ที่ user เปิดเองด้วย · เดินขึ้นหา
  parent (ไม่ enumerate ลูกทั้งหมด — เครื่องนี้มี 400+ process และนี่รันบน timer) และจำผลต่อ HWND
  ไว้ ไม่ตัดสินซ้ำ + 10 tests

## [v1.0.80] - 2026-08-21

### Fixed (แก้)

- **ปุ่ม `TAKKUB_V2_CONTEXT` ใน Settings กดไม่ได้ ทั้งที่ Context Builder มีจริงมาตั้งแต่ Phase 7c** (#309) —
  แถว context ใน `settings_core_v2._FLAG_ROWS` ยังค้าง `wired=False` จากตอนที่ยังไม่มี core module
  ค่านั้นวิ่งไปเข้า `toggle.setEnabled(wired)` → ปุ่มเทา เปิดจาก UI ไม่ได้เลย ต้องไปแก้
  `core-v2-settings.json` หรือตั้ง env เอง ทั้งที่ `core/brain/context_builder.py` + hook ใน
  `orchestrator._assign_dispatch` merge เข้ามานานแล้ว (คำอธิบายในแถวก็ยังบอกว่า "ยังไม่มี core.context
  module") → แก้เป็น `wired=True` + เขียนคำอธิบายใหม่ · test เดิมที่ assert ว่าปุ่มต้อง disabled
  เปลี่ยนเป็น guard วนทุกตัวใน `FLAG_NAMES` ว่าต้องกดได้หมด กันแถวใหม่หลุดซ้ำ
- **โควต้า gemini โชว์ "ยังไม่อัปเดต" ทั้งที่ช่องทางนั้นไม่มีวันอัปเดตเอง** — `fetch_gemini_usage` อ่าน
  cache ของ**แอป Antigravity** (`~/.antigravity_cockpit/cache/quota_api_v1_plugin/authorized/*.json`)
  ซึ่ง `agy` (CLI ที่ cockpit spawn จริง) ไม่ใช่คนเขียน — บนเครื่อง dev ไฟล์เก่า 5 เดือนทั้งที่ agy
  รันทุกวัน stale ตรงนี้จึงไม่ใช่ "poll ยังไม่ทัน" แต่คือ "ไม่มีอะไรมาอัปเดตจนกว่าจะเปิดแอป" →
  adapter แนบ hint ของตัวเองมากับ snapshot ที่ stale แล้ว `usage_meter` render ให้ (`status == "error"`
  return ก่อนแล้ว hint จึงติดได้เฉพาะกับ stale ที่ยังใช้ข้อมูลได้) + 3 tests
- **boot model-refresh มองไม่เห็น `role-models.json` — บั๊มโมเดลไม่เคยทำงานเลยบนคอนฟิกจริง** (#317) —
  `provider_model_refresh` อ่านแค่ `provider_models` (provider-models.json) ทั้งที่โมเดลที่ทีมวิ่งจริง
  พินอยู่ใน `role_models` (provider store เป็นแค่ fallback ของ role ที่ไม่ได้ override) → รายงาน
  `NO_PIN` ทุกบูต ไม่บั๊มอะไรเลย ขณะที่ role แช่แข็งอยู่กับ pin เดิม → `_pins_for()` รวมทั้งสอง store,
  bump กลับผ่าน setter ตัวเดียวกับที่ Settings ใช้ (effort ของ role ไม่หาย) + 13 tests
- **codex ไม่มี model discovery ทั้งที่ CLI มี cache ของตัวเอง** (#103) — เดิมอยู่ใน
  `NO_MODEL_DISCOVERY_GAPS` เพราะไม่มี models subcommand (จริง) แต่ codex maintain
  `~/.codex/models_cache.json` เอง และ `settings_window` ก็จดไว้อยู่แล้วว่าให้อ่าน `slug` จากไฟล์นี้
  มือๆ → wire เข้าเป็น channel จริง (จับ shape จาก codex 0.149.0, 2026-08-21): honour
  `visibility: "hide"` ไม่ให้ `gpt-reserve`/`codex-auto-review` กลายเป็นเป้า bump · เรียงตาม version
  token ใน slug **ไม่ใช่ `priority`** เพราะ priority = ความเด่นไม่ใช่ความใหม่ (sol/terra/luna = 1/2/3
  ในรุ่นเดียวกัน) เชื่อมันแล้ว pin จะถูกดันถอยหลังได้ · codex คือ provider เดียวที่ทีมนี้พิน id ตายตัวไว้
- **รายชื่อโมเดล gemini ใน Settings ตกรุ่น** — ยืนยันสดด้วย `agy --output-format json models`
  (2026-08-21) ว่า 3.7 ออกแล้ว แต่ `_MODELS_BY_PROVIDER` จบที่ 3.6 → เติม 3.7 ทั้งสาม tier ·
  picker ตัวนี้ไม่มีใคร refresh ให้ (ต่างจาก pin ที่ boot-refresh ดูแล) ต้องรันมือเอง — จดไว้ในคอมเมนต์แล้ว
- **หน้าต่าง Settings ใหญ่เกินจอจนกด "Save & Apply" ไม่ถึง** — dialog เปิดที่ขนาดตายตัว 1320x848
  พร้อมพื้นขั้นต่ำ 900x600 บนจอเล็ก/จอที่ scale ไว้ footer สูง 60px (ที่เดียวที่ปุ่ม Save อยู่) เลยไปอยู่
  ใต้ taskbar และย่อให้ถึงก็ไม่ได้เพราะติดพื้น → clamp ทั้งขนาดเปิดและขนาดต่ำสุดตาม
  `availableGeometry` ของจอที่ dialog จะโผล่จริง (จอใหญ่ได้เท่าเดิมเป๊ะ · ไม่มี QScreen บน
  offscreen/headless → คืนค่าเดิม) · ทุก view ห่อ `QScrollArea` อยู่แล้ว หน้าต่างเล็กลงจึงเสียแค่
  "เห็นได้ทีละเท่าไหร่" · label คำอธิบายในแถว provider/role เปิด `wordWrap` — เดิมความกว้างขั้นต่ำ
  ของมันเท่ากับความยาวข้อความทั้งบรรทัด ทั้ง view เลยย่อไม่ลงและงอก horizontal scrollbar แทนที่จะ
  reflow (เห็นได้แม้บนจอ 1920) + 5 tests
- **`task_delivery` state `UNCERTAIN` เป็นทางตัน — ใบงานหายเงียบ** — `_on_settled` เรียก
  `mark_uncertain()` แล้วจบ ไม่มี escalate ไม่มี reaper ไม่มีแจ้งใคร Lead จึงเชื่อว่า pane ทำงานอยู่
  (`takkub status` ก็โชว์ busy) ทั้งที่ใบงานอาจค้างในช่องพิมพ์หรือหายไปแล้ว — เจอกับ gemini pane
  2 ครั้งในรอบเดียว (gemini เจอบ่อยกว่า claude เพราะ preload งานเข้า spawn ไม่ได้
  `system_prompt_flag is None` เลยวิ่ง paste path ทุกครั้ง ไม่ใช่เพราะ claude มี recovery ดีกว่า) →
  `_warn_lead_delivery_uncertain()` แจ้ง Lead ทันทีที่ settle · เป็น warning ไม่ใช่ FAILED เพราะ
  สัญญาณ "ready + ช่องว่าง" แยกไม่ออกจริงๆ ว่า submit แล้วหรือยังค้าง — ความกำกวมตัวเดียวกับที่
  ทำให้ #134 ปิด repaste อัตโนมัติ cockpit จึงรายงานสิ่งที่เห็นแล้วให้คนตัดสิน + 5 tests
- **MCP warm กลืน error หมด — warm ที่โดนฆ่ากลางคันหน้าตาเหมือนสำเร็จ** — `warm_browser_mcps`/
  `warm_graft_mcp` รันด้วย timeout 30s + `except Exception: pass` ล้วน มองจาก cockpit ไม่มีทางรู้ว่า
  npx cache ร้อนหรือยัง (mac ของ user ค้างที่ "Starting MCP servers: graft" ซ้ำๆ) → รวมเป็น
  `_warm_mcp_process()` ตัวเดียว: cap 180s (ต้องพอให้โหลด+แตก tarball รอบแรกจบ ไม่ใช่แค่ให้ server
  บูต) + log แยก timeout/error/สำเร็จ · ยังไม่ raise เหมือนเดิม — warm พังคือ "ครั้งแรกช้า"
  ไม่ใช่ "cockpit พัง" + 5 tests

## [1.0.79] - 2026-08-20

### Fixed (แก้)

- **boot-update ยิง `npm install -g` ขนานกันจน install ชนกันเอง** (#326) — อาการบน macOS ของ user
  จริง: claude ขึ้น "updated to v2.1.237" แต่ binary เป็น placeholder, codex กับ opencode ตาย
  พร้อมกันด้วย npm EEXIST (npm debug log ห่างกัน 1 ms) → root cause: `boot_update_window.start()`
  ยิง 1 QThreadPool worker ต่อ 1 provider ขนานกันหมด แต่ npm **ไม่มี cross-process lock** สำหรับ
  global prefix — สาม `npm install -g` ชน bin-linking กันเอง (EEXIST ที่ `<prefix>/bin/<name>`)
  และตัด postinstall กลางทางจน native binary ของ claude ไม่ถูก copy ออกจาก optional dep ทั้งที่
  npm ยัง exit 0 (= อาการ #313 ซ้ำ) → แก้: `provider_update._NPM_LOCK` mutex ระดับ process ครอบ
  ทุก npm run ทั้ง `_update_generic` และ `_update_claude` (`npm view` ที่ read-only ไม่ถือ lock ·
  uv ยังขนานได้เหมือนเดิม) รอเกิน 180s รายงานตรงๆ แทนค้าง
  + 3 tests (`test_provider_update.py::TestNpmSerialisation` — ปิด mutex แล้วเทสแดงจริง)
- **hint ซ่อม claude บอก platform ผิดบนทุก OS ที่ไม่ใช่ Windows** (#326) — ข้อความตอน binary ไม่ผ่าน
  header check hardcode `@anthropic-ai/claude-code-win32-x64` ส่งคนใช้ mac/linux ไปลง binary ของ
  Windows → แก้: `provider_update._node_optional_dep_tag()` map `sys.platform` + `platform.machine()`
  ตาม naming ของ npm optional dep (`darwin-arm64` / `darwin-x64` / `linux-*` / `win32-x64`)
  + 5 tests
- **ข้อความ error ของ npm บน splash อ่านไม่รู้เรื่อง** (#326) — `splitlines()[-2:]` ได้แต่ boilerplate
  ท้าย log ของ npm ("A complete log of this run can be found in: …") สาเหตุจริงถูกตัดทิ้งทุกครั้ง →
  แก้: `_error_excerpt()` anchor ที่ `npm error code <CODE>` ซึ่ง npm พิมพ์**ก่อน** (updater อื่นที่
  สาเหตุอยู่บรรทัดท้ายจริงๆ ยังใช้ tail เหมือนเดิม)
  + 4 tests

## [1.0.78] - 2026-08-20

### Added (เพิ่ม)

- **Boot-update + splash** — ปิด self-update ของทุก provider ระหว่าง pane ทำงาน (claude
  `DISABLE_AUTOUPDATER` · gemini/kimi env ตามเอกสาร · opencode `autoupdate:false` ใน config ·
  codex/cursor ไม่มี knob → gap #103) แล้วย้ายการ update ไปรัน**ครั้งเดียวตอนเปิด cockpit** ผ่าน
  splash window (painted vector icons, layout-driven height, fade, aggregate progress) — เปิด
  MainWindow หลังทุก provider done/fail/timeout ครบเท่านั้น · verify binary ด้วย pre-flight check
  ก่อน mark ✓ (กัน placeholder 500B จาก postinstall ล้ม) · `TAKKUB_BOOT_UPDATE=0` ข้ามได้,
  `TAKKUB_BOOT_UPDATE_TIMEOUT_S` ปรับ timeout · model catalog refresh ในเฟสเดียวกัน: gemini
  bump เป็นรุ่นล่าสุดจริงผ่าน `agy --output-format json models` + text fallback (#317), provider
  อื่น gap พร้อมเหตุผล (#103)
- **`takkub qa-gate`** (#325) — entrypoint เทสเดียวทั้งทีม: venv-check → full pytest → `ruff check
  src/ tests/` → lint-imports, exit code จับตรงไม่ผ่าน pipe, ตารางสรุป + report ลง `docs/qa/`,
  `--targeted` (tier กลางทาง) + `--v2-flags` (ตรวจ V2 ladder) — CI/qa pane/มือ user เรียกตัวเดียวกัน
  พร้อม regression guard ห้าม inject `PYTHONPATH=src` ตลอดไป
- **`takkub assign --effort low|medium|high`** (#323) — per-assign effort override ครบ 3 provider
  ที่มี knob จริง (claude `--effort` · codex `model_reasoning_effort` · gemini `--effort`) +
  routing guidance ใน role-and-workflow
- **Claude pane pin model ผ่าน `ANTHROPIC_DEFAULT_MODEL`** (#318) — role/provider pin ไม่ทับ
  `/model` ที่ user เลือกอีกต่อไป (`--model` เหลือเฉพาะ override ที่เจตนา) + **Concise output
  style pilot** กับ role qa (`TAKKUB_CONCISE_ROLES` ขยาย/ปิดได้, Lead ไม่โดนเสมอ) — token diet wave 4
- **`CLAUDE_CODE_PROJECT_DIR_NAME`** (#321) — transcript dir ต่อ project เป็นชื่อ deterministic
  (`takkub-project-<ns>`) แก้ปัญหา encode/decode path lossy ที่ต้นตอ · resolver ทุกชั้น fallback
  อ่าน layout เก่า session เดิมไม่หาย · gate ที่ claude ≥2.1.234
- **Pane รอ limit reset เป็น state จริง** (#322) — `LIMIT_WAIT` first-class ใน core scheduler ·
  wake path re-check banner ก่อน inject (claude 2.1.234+ auto-continue เองได้ — ไม่ paste ทับ
  live generation) · usage meter บอก "pane จะทำงานต่อเอง"
- **`pane_guard` กติกา `git_lead_only`** (#314) — commit/push/rebase/merge/checkout เป็น hard-block
  ระดับ PreToolUse สำหรับ specialist (ยกเว้นเดียว: `git commit` ใน worktree isolation ตาม #81) +
  custom-role template มี version-control ban แล้ว
- **Remote/มือถือตอบ AskUserQuestion ได้จริง** — answer-picker endpoint เขียน digit key ตรงเข้า
  PTY + fresh-state guard (409 เมื่อ desktop ตอบไปแล้ว) + notify ส่งครบทุกคำถาม (เดิมส่งแค่ข้อแรก)
  · multiSelect/non-claude fallback ตอบบน desktop เหมือนเดิม (#103)

### Fixed (แก้)

- **Spawn ชน binary เสียแล้ว freeze ทั้ง cockpit เป็นชั่วโมง** (#313) — พิสูจน์แล้วว่า
  `winpty.PtyProcess.spawn()` ไม่ปล่อย GIL ระหว่าง block: `spawn_pty()` ตรวจ header PE/ELF/Mach-O
  ก่อนเรียก native เสมอ → `SpawnTargetCorrupt` retry ผ่าน deferred queue (backoff ~4.5s) ·
  HARD-stall dead-man switch ด้วย `faulthandler.dump_traceback_later` (C-thread, ยิงได้แม้ GIL
  ถูกยึด — boot.log ไม่เงียบอีก)
- **ป้าย "unverified origin" เด้งแทบทุก done** (#315) — เทียบ mint timestamp ของ token แทนการเทียบ
  token ดิบ: respawn หลัง queue = ปกติ, warn เฉพาะ phantom/replay จริง
- **`pty-teardown` thread ตายเงียบตอนปิดโปรแกรม** (#316) — guard `RuntimeError` รอบ
  `quit()/wait()` เมื่อ Qt ลบ `_WriterThread/_ReaderThread` ไปก่อน
- **codex 0.148 `archive` ทำ session หายจาก mirror/resume** (#319) — resolver ทุกชั้น fallback ไป
  `archived_sessions/` เฉพาะ exact-id lookup + fixture 0.148 กัน drift · `/export` ไม่มี headless
  surface → ไม่ adopt (gap #103)
- **`takkub assign --effort` ใช้ได้กับ gemini/agy จริงแล้ว** (#323 follow-up) — เดิม #125 ปิด `--effort`
  ไว้เพราะ agy 1.1.6 เจอ `--model`/`--effort` ชนกันแล้ว swap model เงียบ ตอนนี้ agy 1.1.10+ fix ต้นทาง
  แล้ว (ค่าคู่ที่ชนกัน hard-error ชัดแทนที่จะ swap เงียบ, verify สดกับ 1.1.15) → `gemini_spec.effort_flag`
  กลับมาเป็น `--effort` (low/medium/high, ตรง `agy --help`) ผ่าน path เดิมที่ claude/codex ใช้อยู่แล้ว
  ดู `docs/reviews/2026-08-20-323-agy-effort-restored.md`

### Docs

- **Spike SendMessage/ListAgents** (#320) — สรุป no-go สำหรับ topology ปัจจุบัน (SendMessage address
  ได้เฉพาะ agent ที่ session ตัวเอง dispatch) + design sketch เก็บเป็น input ของ conversation V2
- Audit docs ครบทุกงานวันนี้ใน `docs/audit/2026-08-20-*` + issue #146 ปิดหลัง repro ไม่ติด 3 shards

## [1.0.77] - 2026-08-19

### Added (เพิ่ม)

- **Remote/มือถือเห็นประวัติ pane ที่เป็น Cursor** — `cursor_helper.py` อ่าน transcript ของ Cursor
  (`~/.cursor/projects/<encoded-cwd>/agent-transcripts/`) + ลง scanner ใน remote/notify (#310, than-aa)
- **`takkub mcp-fallback request`** — shard (qa#N) ที่ Playwright MCP ต่อไม่ติดจริง ขอสิทธิ์ใช้ `mb`
  แบบ single-holder time-boxed (กันชน CDP 9222 #92) แทนทางตัน · `takkub doctor --pane <role>` โชว์
  MCP handshake ของ spawn ล่าสุด · event `mcp_handshake_argv` ทุก spawn · qa.md มีขั้นตอน (#304, #146)
- **Lead รู้ทันทีเมื่อ pane ชน quota/usage limit** — detector ต่อ provider (gemini "Individual quota
  reached … Resets in XhYm" field-verified · codex provisional · claude เดิม) → state `stalled:quota`
  ใน `takkub status/list` + `[system]` notice ถึง Lead + โชว์ model ปัจจุบัน (gemini Pro→Flash) (#301)
- **stuck-tool watchdog** — pane ค้างใน shell tool (`Running command...`) เกิน 3 นาทีโดยไม่มี output
  จริง → `stalled:tool` + notice ถึง Lead · auto-Esc 1 ครั้งเฉพาะ provider ที่ยืนยันอาการ (gemini)
  · `takkub status` tail โชว์บรรทัด tool แทน input box ว่าง · heartbeat ไม่นับ elapsed counter เป็น progress (#308)
- **task ที่ติด resource gate แก้/ยกเลิก/ต่อท้ายได้** — ใบงานเขียนลง `runtime/tasks/` ตั้งแต่ queue
  (status=queued) · `takkub task cancel` ยกเลิกใบที่ยังติด gate · `takkub send` ถึง role ที่ยังไม่ spawn
  = queue ไว้ส่งตอน spawn · `takkub status` บอกว่าใครถือ slot อยู่โปรเจกต์ไหน + จำนวนคิว (#303)

### Fixed (แก้)

- **main thread ค้าง 1–7 s เมื่อมีหลาย pane** — graft chip snapshot (resolve/stat ทุก project) ย้ายไป
  worker + cache · pyte `display_lines()` memo ต่อ output generation (orchestrator timers เรียก 3–4
  predicate ต่อ tick ไม่ render ซ้ำ) (#312)
- **overload latch ค้างถาวร** ในช่วง RAM 20–25% → auto-release หลังอยู่ใน dead-band 120 s (ตั้งได้
  `overload_deadband_timeout_s`) · status header บอกเหตุผลจริง "machine overloaded (CPU x% · RAM free y%)"
  แทน "waiting for browser slot" (#305)
- **ชื่อ project/role ภาษาไทย/reserved/ยาว** — `path_safe.safe_segment()` กลาง (hash suffix กันชน,
  กัน CON/NUL, cap 64) ใช้ใน role_memory · task_ledger · lead_context; ชื่อ ascii เดิมไม่เปลี่ยน (#294)
- opencode/codex ready detection เร็วขึ้น · ชื่อ app บน macOS menu bar = agent-takkub · gemini usage
  meter ไม่ขึ้น 100% ปลอมจาก cache เก่า (#310, than-aa)


## [1.0.76] - 2026-08-19

### Added (เพิ่ม) — Core V2 ชั้นล่างใหม่ ปิดด้วย feature flag ทั้งหมด (epic #309, PR #311)

**เวอร์ชันนี้ไม่เปลี่ยนพฤติกรรมที่ผู้ใช้เห็น** — โค้ด Core V2 ทั้งหมดติดมาแต่ flag ปิด
(`TAKKUB_V2_ROUTER / CONVERSATION / CONTEXT / BRAIN / SCHEDULER` = off) พิสูจน์ด้วย suite
เดิม 7,134 tests เขียวโดยไม่แก้ expected + e2e บน cockpit จริง 5 รอบ · ทุก hook fail-open
และรันใน thread แยก

สิ่งที่เพิ่มเข้ามา (package ใหม่ `src/agent_takkub/core/` 13 sub-package ~8,000 บรรทัด):

- **models / contracts / storage** — domain model แบบ frozen dataclass, contract เป็น Protocol,
  jsonl store append-only · import-linter 25 → 28 contracts บังคับว่า core ห้ามดึง Qt/orchestrator
- **Secret Manager** — อ่าน credential ทุก provider ผ่าน interface เดียว (file · macOS Keychain ·
  Windows Credential Manager) + `redact()` กลาง · `takkub doctor` บอก secret backend ต่อ provider
- **Provider Adapter + Account + Router** (flag `router`) — หลาย account ต่อ provider, selector
  5 แบบ, ชน limit แล้ว pane ใหม่สลับ account แทนจอดรอ
- **Version / Compatibility / Migration engine** — `version.json`, compat matrix + live-store probe
  (บทเรียน codex 0.147), `takkub migrate inspect|plan|dry-run|apply|validate|rollback`
  copy-never-move · `takkub doctor --core-version --storage-layout`
- **Capability Hub** — คลัง skill ย้ายจาก `.claude/skills` → `capabilities/skills/` (เป็นกลางทุก
  provider) และสร้าง `.claude/skills` เป็น junction/symlink ตอน spawn ให้ claude ยัง discover ได้
- **Conversation V2 + Checkpoint** (flag `conversation`) — บทสนทนาทุก pane เก็บ jsonl ของเรา +
  summary structured (decisions/lessons จาก `DECISION:`/`LESSON:` ใน done note) + checkpoint
  พร้อม provider-binding · ข้อความผ่าน redact ก่อนลงดิสก์
- **Second Brain + Context Builder** (flag `brain` / `context`) — ความจำ 5 ระดับ trust, reflection
  จาก done note, แนบบล็อก `## Context (Takkub brain)` ให้ task ตอน assign (งบ 12% ของ context
  window, timeout 300 ms)
- **Scheduler extend + Storage V2 layout** (flag `scheduler`) — slot policy ต่อ provider/account/
  project + backpressure · โครง `~/.agent-takkub` ใหม่ (config/providers/accounts/capabilities/
  agents/projects/<id>/brain/state/runtime/cache/secrets/system) ผ่าน migration ladder 7 ขั้น —
  **ยังไม่ apply** (รอ 2.0.0)
- **Settings → หน้า "Core V2"** — เปิด/ปิด flag ทั้ง 5 จากหน้าจอ (persist ที่
  `~/.takkub/core-v2-settings.json`, env ชนะ) + ตั้ง scheduler policy + ดู migration report

### Fixed (แก้)

- `core_v2_settings.load()` cache ด้วย (mtime, size) — flag check บน Qt main thread ไม่อ่านไฟล์ทุกครั้ง
  (0.62 → 0.20 ms median, worst 78 → 7 ms) ตามรีวิว PR #311
- `tests/conftest.py` isolate `core-v2-settings.json` ของเครื่อง dev ออกจาก flag-off tests

### Notes (หมายเหตุ)

- gap ที่ประกาศ (ไม่กระทบเพราะ flag ปิด): claude argv builder ยังอยู่ใน spawn_engine ·
  kimi/cursor ingest/secret · PermissionEngine ยังไม่ rewire cmd_guard · context_window default 200k ·
  native resume ไม่ต่อ spawn · storage apply รอ 2.0.0
- แผนเปิด flag ทีละตัวในเวอร์ชันถัดไป: router → conversation → brain+context → scheduler →
  2.0.0 `takkub migrate apply` · เอกสาร: `docs/v2/` (audit · matrix · plan · phase reports ·
  critic review · pr311-review)


## [1.0.75] - 2026-08-19

### Fixed (แก้)

**Remote บนมือถือไม่เห็นคำตอบของ Lead ที่เป็น opencode**

อาการ: ส่งข้อความจากมือถือถึง Lead ได้ Lead ตอบจริงบนเดสก์ท็อป แต่มือถือขึ้นแค่
ข้อความที่เราส่ง แล้วค้างที่ "OpenCode กำลังทำงาน..." ตลอดไป ไม่มี error สักบรรทัด
คนละสาเหตุกับ codex/agy ที่แก้ไปใน 1.0.74 — เป็นบั๊กเฉพาะทางอ่าน sqlite ของ opencode

- **opencode เขียนคำตอบแบบสตรีมลงแถวเดิม** — มันสร้าง part ชนิด `text` ทันทีที่โมเดล
  เริ่มพูด แล้วค่อยทยอย UPDATE ข้อความลงแถวนั้น วัดจาก store จริง: `step-start` /
  `step-finish` เขียนจบใน 1–2 ms แต่ `text` ใช้ 660–720 ms และ `reasoning` 850–1370 ms
  ส่วน notifier poll ทุก 200 ms จึงมี poll ตกกลางช่วงนั้นเสมอ
- cursor เดิมอ้างอิง `p.time_created` ซึ่ง **ไม่มีวันเปลี่ยน** และถูกเลื่อนข้ามทุกแถวที่
  มองเห็น รวมถึงแถวที่ข้อความยังว่าง พอ query รอบถัดไปกรอง `time_created > cursor`
  แถวนั้นก็หลุดออกไปถาวร → คำตอบหายทั้งก้อน ส่วนวงกลมหมุนที่ `step-start` จุดไว้
  ก็ค้างตลอดไป (ถ้า poll ตกตอนข้อความเขียนได้ครึ่งเดียว จะได้คำตอบที่ถูกตัดกลางคัน)
- แก้โดยใช้ `time.end` ที่ตัว part เองเป็นสัญญาณจบสตรีม (โผล่เมื่อเขียนเสร็จเท่านั้น
  ไม่ต้องเดาด้วยการรอเงียบกี่มิลลิวินาที) · cursor ย้ายไปใช้
  `COALESCE(time_updated, time_created)` และ **ห้ามข้าม part ที่ยังสตรีมอยู่** ·
  จำ part id ที่ยิงไปแล้วไว้ที่ tail เพื่อให้การอ่านซ้ำไม่ยิงซ้ำ
- นี่คือตัวกันแบบเดียวกับที่ฝั่ง JSONL (claude/codex/gemini) มีมาตลอดในชื่อ
  `_Tail.partial` (กันบรรทัดที่เขียนค้าง) — ฝั่ง sqlite ของ opencode ไม่เคยมี
  จึงเป็นเหตุผลที่ provider อื่นไม่โดนบั๊กนี้

**Remote mirror ค้างกับ session เก่าเมื่อ opencode เปิด session ใหม่**

`_OPENCODE_RESOLVE_CACHE` ไม่มีวันหมดอายุ ทั้งที่ key ของมัน (db, cwd, uuid, spawn_ts)
คงที่ตลอดอายุ pane แต่คำตอบ "session ล่าสุดของ cwd นี้" ไม่คงที่ — ถ้า opencode เปิด
session ใหม่โดยที่ pane ไม่ได้ respawn (เช่น `/new` ใน TUI) mirror จะชี้ไป session เก่า
จนกว่าจะปิด cockpit โดยไม่มีสัญญาณอะไรเลย · ใส่ TTL 10 วินาทีให้ผลแบบ "ล่าสุดตอนนี้"
ส่วนผลที่มาจาก session id ที่ระบุมาตรงๆ ไม่หมดอายุ (session ไม่ย้าย directory
การถามซ้ำจึงเป็นงานเปล่า)

### Changed (เปลี่ยน)

- fixture ของเทสต์ opencode เพิ่มคอลัมน์ `time_updated` ให้ตรงกับ schema จริงของ
  `opencode.db` — ของเดิมไม่มีคอลัมน์นี้ ซึ่งเป็นเหตุผลหนึ่งที่บั๊กสตรีมด้านบน
  ไม่มีทางถูกจับได้ด้วยเทสต์ชุดเดิม

### สถานะ Remote mirror ต่อ provider (หลัง 1.0.75)

| provider | history | คำตอบสด | ปุ่ม resume |
|---|---|---|---|
| claude | ✅ | ✅ | ✅ |
| codex | ✅ | ✅ | ✅ |
| gemini (agy) | ✅ | ✅ | ✅ |
| opencode | ✅ | ✅ | ✅ |
| kimi | ❌ | ❌ | ❌ |
| cursor | ❌ | ❌ | ❌ |

kimi/cursor ยังไม่มี adapter เลย (ไม่ใช่ของพัง — ยังไม่เคยทำ) เป็น gap ที่ประกาศไว้
ใน #103 มือถือจะขึ้นเหตุผล `provider_unsupported` แทนจอว่าง · ส่งข้อความเข้าไปยังได้
ปกติทุก provider เพราะทางส่งเป็น PTY เหมือนกันหมด


## [1.0.74] - 2026-08-19

### Fixed (แก้)

**Remote บนมือถือไม่เห็นคำตอบของ Lead — พังทุก provider ยกเว้น claude**

อาการ: พิมพ์จากมือถือ → ข้อความถึง Lead จริง Lead ตอบจริงบนเดสก์ท็อป แต่มือถือ
ขึ้น "ยังไม่เห็นคำตอบใน 30 วิ" แล้วเงียบ ทั้ง history และ resume picker ว่างเปล่า
สองสาเหตุ คนละตัวกัน แต่ตายเงียบเหมือนกัน (ไม่มี error สักบรรทัด CI เขียวตลอด)

- **codex 0.147 เปลี่ยน schema ของ rollout log** — จาก `event_msg.agent_message` /
  `.user_message` เป็น `event_msg.item_completed` ที่ห่อข้อความไว้ใน `item`
  (`AgentMessage`/`UserMessage` + `content[]`) parser เดิมอ่านได้ 0 ข้อความ
  ที่ไม่มีใครจับได้เพราะ `codex exec` (ตัวที่ doctor/probe/test ใช้) **ยังเขียน
  schema เก่าอยู่** บน 0.147 — เทสต์เลยเขียวหมดขณะที่ pane จริง (`codex-tui`)
  เงียบสนิท ตอนนี้อ่านได้ทั้งสอง schema
- **agy (Antigravity CLI) ย้ายที่เก็บ transcript** — จาก
  `~/.gemini/tmp/<x>/chats/session-*.jsonl` ไปเป็น
  `~/.gemini/antigravity-cli/conversations/<id>.db` + `brain/<id>/…/transcript.jsonl`
  พร้อม schema ใหม่ (`USER_INPUT` / `PLANNER_RESPONSE`) resolver เดิมไม่ error
  แต่ไป resolve ไฟล์เก่าอายุ 2 เดือนแทน → มือถือเห็นแชทว่างตลอด รองรับทั้ง
  store ใหม่และเก่าแล้ว · `thinking` ของโมเดลไม่ถูกส่งขึ้นมือถือ (text-only เหมือนเดิม)

**Resume picker บนมือถือโชว์ preview ซ้ำกันทั้งลิสต์**

ทุกใบขึ้นว่า "Start the current task from the one-shot system-prompt block now."
เพราะ preview ใช้บรรทัดแรกของ user ซึ่งเป็นประโยคที่ cockpit เขียนเองตอน spawn
ตอนนี้ใช้ `ai-title` ของ claude (ชื่อเดียวกับที่ picker บนเดสก์ท็อปโชว์ เช่น "โหลๆ")
ถ้าไม่มีจึงค่อยไล่หาบรรทัดที่คนพิมพ์จริง · ตัวกรอง session ของ teammate ถูกแยก
ออกมาอ่านบรรทัดแรกจริงๆ แทนการดูข้อความที่แสดง (ไม่งั้น session ของลูกทีมจะหลุด
กลับเข้า picker ของ Lead)

### Added (เพิ่ม)

**prod เก็บ state ของทุก provider ไว้ใน `~/.agent-takkub` (user directive)**

เดิมมีแค่ claude ที่ถูก isolate (`CLAUDE_CONFIG_DIR`) — codex/opencode ยังเขียนลง
`~/.codex` / `~/.local/share/opencode` แปลว่า cockpit prod ใช้ session, config และ
login ร่วมกับ dev checkout และกับ CLI ที่ user รันเอง

- `CODEX_HOME` → `~/.agent-takkub/codex-home` · opencode → `XDG_DATA_HOME` /
  `XDG_CONFIG_HOME` ชี้เข้า `~/.agent-takkub/opencode-home/{data,config}`
  (สโคปเฉพาะ pane ของ provider นั้น ไม่ได้ export ทั้ง cockpit ไม่งั้นเครื่องมืออื่น
  ที่อ่าน XDG เช่น gh/uv จะย้ายตามไปด้วย)
- **ฝั่งอ่านกับฝั่ง spawn ใช้ที่เดียวกันเสมอ** (`config.provider_home_env`) — ถ้าสองฝั่ง
  ไม่ตรงกัน mirror จะไปอ่านโฟลเดอร์ที่ไม่มีใครเขียน = จอมือถือว่างแบบไม่มี error
  ซึ่งคือคลาสบั๊กที่รีลีสนี้แก้อยู่พอดี
- **ย้ายของเดิมตามไปให้ครั้งแรกที่ spawn** (`provider_bootstrap.py`): auth + config +
  session ของ **Lead** ล่าสุด (กรอง `[ROLE:` ของลูกทีมออก) แบบมีเพดาน atomic และ
  **คัดลอก ไม่ย้าย** — ของเดิมใน `~/.codex` อยู่ครบ ไม่ต้อง login ใหม่
- gemini/kimi/cursor **ยังย้ายไม่ได้** — ไม่มี env knob (gemini-cli ต่อ `.gemini` เข้ากับ
  `os.homedir()` ตรงๆ) ประกาศไว้ใน `config.PROVIDER_ISOLATION_GAPS` และ
  `takkub doctor` พิมพ์ออกมาเป็น gap #103 แทนที่จะเงียบ

**เตือนดังๆ เมื่อ `~/.agent-takkub` เขียนไม่ได้**

field report จาก mac: โฟลเดอร์เขียนไม่ได้ แล้ว**ไม่มีอะไรบอกเลย** — cockpit เปิดติด
pane spawn ได้ ข้อความถึง Lead ได้ ขณะที่ prod profile ของ claude, port file,
events.log และ provider home ทุกตัวเขียนไม่ลง (boot.log เองก็อยู่ในนั้น เลยกลืน error
ตัวเอง) เจอได้ทาง `takkub doctor` ทางเดียว ตอนนี้เด้ง dialog ตอน boot พร้อมคำสั่งแก้

### Changed (เปลี่ยน)

- `takkub doctor` เพิ่มหมวด `[provider-isolation]` — บอกว่าแต่ละ provider เก็บ state
  ไว้ที่ไหนจริงๆ และตัวไหนยัง isolate ไม่ได้เพราะอะไร
- skill ใหม่ `provider-integration` (bundle กลางที่ ship ไปกับ package) — เช็กลิสต์ 6 ข้อ
  ที่ provider ใหม่ต้องผ่าน + กฎกัน schema drift ของ CLI ต้นทาง ซึ่งเป็นต้นเหตุของ
  ทั้ง 2 บั๊กในรีลีสนี้

## [1.0.73] - 2026-08-18

### Added (เพิ่ม)

**cockpit รายงานบั๊กของตัวเองอัตโนมัติได้จริงแล้ว — เปิดเป็นค่าเริ่มต้น (#297)**

> **ผู้ใช้ต้องรู้:** ตั้งแต่เวอร์ชันนี้ cockpit จะเปิด issue ที่ `takkub/agent-takkub` ให้อัตโนมัติเมื่อ **ตัว cockpit เอง** มีปัญหา — **ปิดได้ที่ Settings → Performance → "ส่งรายงานบั๊กของ cockpit อัตโนมัติ"** หรือ `TAKKUB_AUTO_ISSUE=0`
>
> ส่งเฉพาะ: ชนิดของ event + จำนวนครั้ง + เวอร์ชัน + platform · **ไม่ส่ง** เนื้อ task, path ของโปรเจกต์ หรือ token (ผ่าน `_scrub_home` + `_redact` ก่อนส่งทุกครั้ง) · จำกัด 5 ใบ/24 ชม. และหัวข้อเดิมซ้ำได้ไม่เกิน 1 ครั้ง/24 ชม.

สามช่องโหว่ที่ทำให้ของเดิมใช้ไม่ได้จริง แก้ครบทั้งสาม:

1. **ยิงเฉพาะตอน crash** → `auto_issue_signals` อ่านสัญญาณจาก `events.log` ตามเกณฑ์ที่**วัดจากข้อมูลจริง** ไม่ใช่เดา: watchdog respawn ≥3/6ชม. · ส่งใบงานไม่สำเร็จ ≥3/6ชม. · pane boot ตาย ≥2/6ชม. · UI ค้าง ≥40 ครั้งที่นานเกิน 5 วิ ใน 6 ชม. — เกณฑ์สุดท้ายเงียบกับสภาพหลังแก้ #291 (13 ครั้ง/ชม. สูงสุด 3.3 วิ) และดังกับสภาพก่อนแก้ (1,448 ครั้ง/6ชม. สูงสุด 21 วิ) ทั้งสองเคสมีเทสยืนยัน
2. **user ที่ลงผ่าน npm ยิงไม่ถึง GitHub** → repo ปลายทางเป็นค่าคงที่อยู่แล้ว (`takkub/agent-takkub`) จึงไม่ต้อง derive จาก git checkout อีก · checkout ในเครื่องยังชนะถ้ามี (คนที่ fork จะได้ยิงเข้า remote ของตัวเอง) · ตั้ง `AGENT_TAKKUB_COCKPIT_REPO_SLUG` เพื่อ retarget ได้
3. **ยิงไม่ได้แล้วเงียบ** → `takkub ma` เพิ่มหัวข้อ "Issue ที่ค้างในเครื่อง (ยังไม่ถึง GitHub)" และแผนทำต่อจะบอกให้ส่งขึ้นก่อน (เดิมเตือนที่ stderr ซึ่ง cockpit แบบ GUI ไม่มีใครเห็น)

พ่วง: ถ้า user ไม่มีสิทธิ์สร้าง label บน repo กลาง เดิม `gh issue create --label <ไม่มี>` จะพังทั้งใบ ตอนนี้ retry ใหม่โดยไม่ใส่ label — รายงานสำคัญกว่า label

**ปิดช่อง #103 ของ auto-issue** — `CODEX_AGENTS_MD` (ไฟล์ที่ plant ให้ pane codex/gemini/opencode) เดิม**ไม่มีคำว่า `issue` สักคำ** ตอนนี้มีทั้ง `takkub issue new` (พร้อมกฎว่าเฉพาะบั๊กของ cockpit เท่านั้น บั๊กของโปรเจกต์ user ให้ `takkub send` หา Lead) และ `takkub done --blocked`

### Fixed (แก้)

**เทสยิง issue จริงขึ้น repo สาธารณะได้ (พบตอนแก้ #297 — สร้างไปจริง 3 ใบ ลบแล้ว)**
- ก่อนหน้านี้เทสแตะ `gh issue create` จริงไม่ได้โดยบังเอิญ: ไม่มี checkout ที่ resolve ได้ ทุก cockpit-bug op เลยตกลง local store การทำให้ repo เป็นค่าคงที่ลบตาข่ายนิรภัยที่ไม่ได้ตั้งใจนั้นทิ้ง แล้ว suite รอบแรกก็ยิง issue จริง 3 ใบทันที
- `issues._gh` ปฏิเสธ subcommand ที่**เปลี่ยนของบน GitHub** (`issue create/close/reopen`, `label create`) เมื่ออยู่ใน process ของเทส/CI · คำสั่งอ่านอย่างเดียวยังผ่านได้ (ทำ tracker เสียไม่ได้) · เทสที่ patch `_gh` ไว้ไม่กระทบเลยเพราะไม่ได้แตะเน็ตอยู่แล้ว · `TAKKUB_ALLOW_REAL_ISSUE_WRITE=1` เป็นทางออกสำหรับ smoke test จริง
- guard วางที่ `_gh` ไม่ใช่ที่ `new_issue` เพราะเป็นจุดเดียวที่ spawn subprocess จริง และไม่ไปเปลี่ยนเส้นทางของเทสที่ mock ไว้แล้ว

### Added (เพิ่ม)

**`takkub ma` — maintenance sweep คำสั่งเดียวเดิน checklist ให้ครบ**
- เปิด cockpit dev แล้วพิมพ์ `takkub ma` จะไล่เช็คทีละข้อ: (1) issue ที่เปิดค้าง + ค้างมากี่วัน (2) PR ที่เปิดค้าง + สถานะ CI ต่อใบ (แดง/กำลังรัน/เขียว) + conflict (3) `events.log` ของ cockpit ที่รันอยู่ — แยก 🔴 หนัก / 🟡 เตือน พร้อมเรียก stall ที่นานเกิน 2 วิ ออกมาเป็นรายตัว (4) สภาพ repo: branch, ไฟล์ที่ยังไม่ commit, ahead/behind origin, CI ล่าสุดของ branch นี้
- ปิดท้ายด้วย **แผนทำต่อที่สร้างจากสิ่งที่เจอจริง** เท่านั้น — ไม่มีหัวข้อไหนเจอปัญหา ก็ไม่มีข้อนั้นในแผน และเลขข้อไม่กระโดด
- ธง: `--since-hours N` (default 24) · `--no-net` ดูเฉพาะ log ในเครื่องไม่แตะ `gh` · `--json`
- **อ่านอย่างเดียวโดยตั้งใจ** ขั้นที่ 4-5 ของ checklist (แก้ตามที่เจอ / push รอ CI แล้ว publish) เป็นงานตัดสินใจของ Lead ไม่ใช่ของสคริปต์ — คำสั่งนี้จึงส่งแผนให้ ไม่ใช่ลงมือแทน
- `maintenance.py` เป็น leaf module (พึ่งแค่ `config` + subprocess) เพื่อไม่ให้ `cli.py` ลาก orchestrator engine เข้า process ของ CLI ตาม contract `cli-ipc-boundary`
- รายชื่อ event ที่ถือว่าผิดปกติ อ้างจาก `events.log` จริงที่ cockpit เขียน ไม่ได้เดาจากฝั่งผู้ส่ง

**`takkub done --blocked` — รายงานว่า "ติด blocker" แยกจาก "เจอบั๊ก" (#296)**
- BLOCKED = งาน**รันไม่ได้**เพราะขาดของนอกระบบ (credential, บัญชีทดสอบ, สิทธิ์, ข้อมูล, บริการภายนอก) · FAILED = งานรันแล้ว**มีของพัง** — มีแค่อย่างหลังที่มี role ให้ route กลับ
- ยังนับเป็น "ไม่เสร็จ" ใน ledger เหมือนเดิม (ส่ง `failed=True` ไปด้วย) ต่างกันที่ **ใครถูกถามต่อ**

### Fixed (แก้)

**QA ที่ติด blocker ถูกตีเป็น FAIL แล้วเสนอ route กลับ backend (#296)**
- เคสจริง 2026-08-18: qa#1 และ qa#2 รายงานว่าสร้าง tenant ทดสอบไม่ได้เพราะไม่มีรหัสผ่าน super admin — ไม่มีโค้ดผิดสักบรรทัด แต่ signature matcher อ่านคำกลุ่ม credential เป็น "server/API signature" แล้วเสนอส่ง backend ไปแก้ระบบ auth ที่ไม่ได้พัง
- ความเสี่ยงจริงไม่ใช่แค่เสีย pane: backend อาจไป "แก้" ทางเข้าระบบ auth ของจริงเพื่อให้ qa ผ่าน
- `routing_planner.classify_blocked()` ตัดสินก่อน `classify_failure` โดยใช้ตัวแยกว่า **สิ่งที่ขาด ไม่ใช่สิ่งที่พัง** — ทุก pattern ต้องมีคำบอกการขาด (ไม่มี/ขาด/ต้องขอ/รอ/missing/no/waiting for) อยู่ติดกับสิ่งนั้น `401 unauthorized ทั้งที่ควรเข้าได้` จึงยัง route ไป backend ตามเดิม ส่วน `ไม่มีรหัสผ่าน super admin` ไม่ route ไปไหน
- เมื่อเป็น blocked, Lead ได้ handoff คนละแบบ: บอกว่าติด blocker ไม่ใช่บั๊ก · **ไม่เสนอ role ใดๆ** · สั่งให้สรุปสิ่งที่ขาดให้เจ้าของ · และห้าม propose ให้ใครไปแก้ระบบเพื่อให้ผ่าน (รีเซ็ต/เดารหัสผ่าน ปลดการยืนยันตัวตน)

**`delivery-superseded` ยกเลิกใบงานแรกที่ยังไม่ถึงมือได้ (#295)**
- เคสจริง: `assign --isolation worktree` 11:35:25 → `send` แก้ path 11:35:39 → cockpit ยกเลิก pending delivery แล้วรายงานด้วยข้อความเดียวกับกรณี "ยกเลิก re-paste ของงานเก่า" ซึ่งปลอดภัย — Lead แยกไม่ออกว่าเพื่อนร่วมทีมยังมีงานอยู่ไหม ต้องเสีย 2 รอบถามยืนยัน และถ้าเป็นโหมด unattended กลางคืน pane นั้นจะเงียบไปทั้งคืน
- เหตุผลของ #255 (กัน self-heal resend paste task เดิมทับสิ่งที่ Lead เพิ่งส่ง) ใช้ได้เฉพาะกับ delivery ที่ **เคย paste ไปแล้ว** — ตัวที่ยัง `QUEUED`/`WAITING_RESOURCE` ไม่มีอะไรให้ซ้ำ การยกเลิกมันคือการทำลายสำเนาเดียวที่มี
- `supersede_for_session()` (ใช้โดย `send`) ยกเลิกเฉพาะตัวที่ถึง pane แล้ว ส่วนตัวที่ยังไม่ถึง **ปล่อยไว้ให้ส่งต่อ** แล้วเตือน Lead แยกอีกข้อความว่าใบงานไหนยังไม่ถึงมือและข้อความที่เพิ่ง send อาจถึงก่อน
- `cancel_for_session()` เดิม (ใช้โดย verb `takkub cancel` และตอนปิด pane) **ไม่เปลี่ยน** — ตรงนั้นผู้เรียกบอกเจตนาชัดว่าจะยกเลิกทุกอย่าง

**worktree merge cleanup พังบน Windows 'Filename too long' (#226)** — แก้ไปแล้วตั้งแต่ 2026-08-15 คู่กับ #227 แต่ใบไม่ได้ปิด ตรวจซ้ำที่ HEAD แล้วว่า `_stage_for_delete` / `_rmtree_long_path_safe` / `remove_worktree_tree` ยังอยู่ครบและเทสเขียว

### Fixed (แก้)

**status-bar chip เขียน "RAM 69%" ทั้งที่หมายถึง RAM ว่าง — อ่านกลับด้านจาก Task Manager (#292)**
- chip ดึง `available_memory_percent` มาโชว์ แต่ติดป้ายแค่ `RAM` ซึ่งทุกเครื่องมือมาตรฐาน (Task Manager / htop / Activity Monitor) หมายถึง RAM **ที่ใช้ไป** → เครื่องที่ใช้จริง 31% แสดงเป็น "RAM 69%" อ่านแล้วนึกว่าใกล้เต็ม
- จุดที่แย่กว่าคือทิศทาง: ตอน RAM ใกล้หมดจริง chip จะโชว์เลข **ต่ำ** (เช่น "RAM 8%") — เตือนไม่ติดตอนที่ควรเตือนที่สุด และย้อนแย้งกับ `SYS OVERLOAD` ที่ trigger จาก available ต่ำ
- chip โชว์ RAM ที่ใช้ไปแล้ว (`100 − available`) ทิศเดียวกับ `CPU` ที่อยู่ติดกัน · tooltip เพิ่มบรรทัด `RAM used:` และคง `Available RAM:` ไว้ เพราะ threshold ของ governor นิยามบนฝั่ง available
- test กันถอย: RAM ว่าง 8% ต้องแสดง "RAM 92%" คู่กับ "SYS OVERLOAD"

**cockpit กระตุกเป็นวินาทีทั้งที่ CPU/RAM ต่ำ — git subprocess ยิงทีละ dir บน Qt main thread (#291)**
- `events.log` 2 วัน (2026-08-16 → 08-18) มี `main_thread_stall` **203 ครั้ง** — median 1.1 วิ, p90 2.3 วิ, สูงสุด 24.9 วิ · `spawn_in_progress=true` **0/203** · 64/203 เกิดตอน pane พ่น output น้อยกว่า 1KB/s · 12 ครั้งเกิดตอน **ไม่มี pane เปิดเลย**
- CPU ไม่ขึ้นเพราะ main thread ไม่ได้ compute — มันรอ process spawn + file I/O ซึ่งไม่นับเป็น CPU load
- stack dump ชี้ว่า `takkub done`/`assign` วิ่งเข้า `cli_server._dispatch` แบบ synchronous บน GUI thread แล้ว `snapshot_porcelain_paths` ยิง `git status` **หนึ่ง process ต่อ `?? dir/` หนึ่งอัน** เรียงกัน (วัดบนเครื่องอ้างอิง 29–42 ms ต่อครั้งแม้ tree สะอาด) — tree ที่มี untracked dir สิบกว่าอันจึงแช่แข็ง UI เกินวินาที
- `_expand_dir_entries()` รวบเป็น **git call เดียว** ไม่ว่าจะมีกี่ directory แล้วแบ่งผลกลับตาม path prefix (longest-prefix first เพื่อให้ `a/b/` ชนะ `a/`) · cap ยังคิดแยกต่อ directory ตัวที่ล้น cap จึงไม่ลากตัวอื่นตกไปด้วย · path ที่ไม่ตรง pathspec ไหนเลยถูกส่งกลับเข้า snapshot ไม่ถูกทิ้ง — การ batch ต้องไม่กลายเป็นการเปลี่ยน correctness
- event ที่มาก่อน stall บ่อยสุดคือ `assign` (27) · `done` (20) · `session_report` (8) · `spawn` (7) — ทั้งหมดคือ handler ของ `cli_server`
- เป็นของเก่าที่กลับมา: #194 กับ #229 ปิดไปโดยแก้ทีละจุด และข้อ 3 ของ #194 ("regression guard") ไม่เคยถูกทำ — รอบนี้มี test ที่ fail ถ้าจำนวน git process โตตามจำนวน directory

**Lead = codex/gemini ใช้ไม่ได้จริง: resolve_session กิน 1.3–2.7 วิ บน GUI thread ทุก 5 วิ (#293, #103)**
- provider ที่ `requires_session_uuid=False` (codex/gemini/opencode) ถูก resolve ใหม่ทุก `_UUIDLESS_RESYNC_THROTTLE_S` — ทางลัด "ข้าม glob เมื่อ tail มีอยู่แล้ว" ของ #229 ใช้กับมันไม่ได้ตามนิยาม
- วัดจริงบนเครื่องอ้างอิง (`~/.codex/sessions` 738 ไฟล์): **codex 2,716 ms** · gemini 1,278 ms · claude 62.8 ms — ทั้งหมดบน Qt main thread
- ต้นเหตุคือ `sorted(root.rglob("rollout-*.jsonl"), key=mtime)` ซึ่ง stat ทุกไฟล์ในคลังก่อนจะมองไฟล์แรกด้วยซ้ำ
- codex partition คลังเป็น `sessions/YYYY/MM/DD/` อยู่แล้ว — `_codex_rollout_candidates()` เดินไล่ทีละวันจากใหม่ไปเก่าแบบ lazy (ทุก caller return ที่ match แรก การเจอในวันนี้จึงไม่ต้องจ่ายค่าของเมื่อวาน) และ **bound ด้วยวันที่** จาก spawn timestamp แทน `break` ตาม mtime ที่จะตัดการค้นสั้นเกินไปถ้าไฟล์ถูกแตะหลังวันของตัวเอง (เผื่อ 1 วันเต็มสำหรับ session ที่เริ่มก่อนเที่ยงคืน)
- ผลวัดหลังแก้: **2,716 ms → 41 ms** (cold) · เทียบ apples-to-apples บน cache อุ่นเท่ากัน: whole-store sort 60.6 ms → first-candidate 4.3 ms
- คลังที่ไม่ได้ partition ตามวันที่ยัง fallback ไปเดินทั้งต้นไม้เหมือนเดิม — ไม่ใช่รายงานว่าไม่มี session

**watchdog ฆ่า pane ที่ยังทำงานอยู่ — stuck detection ดูแค่หน้าจอนิ่ง ไม่ดู child process (#288)**
- เคสจริง 2026-08-17: pane QA (gemini) เขียนสคริปต์ Playwright แล้วรัน `node <script>.js` ซึ่งไม่พ่น output ออกจอเป็นนาที watchdog อ่านจอที่นิ่งว่าค้าง แล้ว respawn แบบ `resumed: false` → งาน 15 นาทีหายหมด และ pane เริ่มใหม่ตั้งแต่ข้อ 1 **กดปุ่มจริงในระบบจริงซ้ำรอบสอง**
- event เดียวกันนั้นบันทึก `content_static_s: 600` คู่กับ `close_kills_live_children count: 8` (node.exe + chrome-headless-shell.exe 5 ตัว) — หลักฐานว่า pane ไม่ได้ค้างถูกเก็บ**ตอนกำลังฆ่า** ช้าไปหนึ่งก้าวจนเปลี่ยนการตัดสินใจไม่ได้
- ตัวนับ live children ถูกแยกออกมาเป็น `_live_non_scaffolding_children()` แล้วเรียก**ก่อน**ตัดสินใจ recover (ตัวกรอง scaffolding ของ #272/#286 ยังทำงานเหมือนเดิม) ราคาถูกเพราะไม่มีอะไรมาถึงบรรทัดนั้นจนกว่า pane จะนิ่งครบ `STUCK_THRESHOLD_S` แล้ว
- การเลื่อนมีเพดาน `STUCK_LIVE_CHILD_GRACE_S` (ค่าเริ่มต้น 60 นาที ปรับผ่าน `TAKKUB_STUCK_LIVE_CHILD_GRACE_S`) — child ที่ตัวเองค้างต้องไม่สามารถยึด pane ไว้ตลอดกาล
- Lead ได้ notice ครั้งเดียวต่อ episode (cooldown 15 นาที) ว่า watchdog เลื่อนการ respawn เพราะงานยังเดินอยู่ ไม่ใช่ทุก tick

**`/api/history` ของ OpenCode คืน transcript ของโปรเจกต์อื่น (#285 follow-up)**
- adapter opencode เลือก session id ด้วย `list(_LAST_OPENCODE_SESSION_BY_PROJECT.values())[-1]` = โปรเจกต์ที่ถูก**ใส่เข้า dict ล่าสุด** ไม่ใช่โปรเจกต์ที่ร้องขอ (dict ไม่ย้ายลำดับ key เดิมตอน re-assign) — เปิด A ก่อน B แล้วทุกการอ่านของ A จะได้ id ของ B
- opencode เก็บทุก session ไว้ใน sqlite **ก้อนเดียวร่วมกันทุกโปรเจกต์** session id จึงเป็นสิ่งเดียวที่แยกกันได้ → หยิบผิดคือเสิร์ฟ transcript ข้ามโปรเจกต์
- รากปัญหาคือ `_HistoryScanner.read_messages` ไม่มี project context ให้ส่ง — เติม `project_ns` เข้า signature แล้วให้ adapter ทุกตัวรับ (ตัวที่ resolve เป็นไฟล์ต่อโปรเจกต์อยู่แล้วก็แค่ไม่ใช้) แทนที่จะเลี่ยงด้วย global

**`test_missing_installable_provider_gets_auto_fix` แดงบนเครื่องที่ติดตั้ง opencode จริง**
- เทสอ้างในคอมเมนต์ว่า "machine-independent" แต่ neutralize discovery แค่ codex/gemini + stub `shutil.which` ส่วน `find_opencode_executable` มี fallback ไป `_default_opencode_paths()` แล้วเช็คด้วย `Path.is_file()` ซึ่งเดินผ่าน stub ไปเลย
- บนเครื่อง dev ที่มี opencode ติดตั้งอยู่จริง binary ตัวจริงจึงรั่วเข้ามา provider กลับมาเป็น INFO พร้อมเลขเวอร์ชันแทนที่จะเป็น SKIP-พร้อม-installer — CI (ไม่มี opencode) ไม่มีทางเห็น
- เติม patch seam ของ opencode ให้ครบเหมือน codex/gemini

### Changed (เปลี่ยน)

- ruff 0.16.2 → **0.16.3** ขยับพร้อมกันทั้ง `pyproject.toml` และ `.pre-commit-config.yaml` ตามที่ #246 บังคับ (PR #290 ของ dependabot ขยับแค่ไฟล์เดียวจึงทำ CI แดง และพ่วงการขยาย PyQt6 เป็น `<6.12` ที่ขัดกับ pin LTS ที่ตั้งใจ — ปิดใบนั้นไปแล้ว PyQt6 ยังอยู่ที่ 6.8 LTS)

### Docs (เอกสาร)

- `docs/audit/takkub-brain-v1-current-head.md` เพิ่มภาคผนวก re-verification ที่ HEAD 1.0.72 (#275): ตรวจ `file:line` ที่ audit เดิมอ้างครบทุกจุด (drift 2 จาก 15) · แก้ข้อสรุปเรื่อง common assign boundary (subagent แตกทางที่ `orchestrator.py:1537` ก่อนถึง `_assign_dispatch`) · วัด token ratio แบบ corpus ทั้ง store แทนไฟล์เดียว (2.91 chars/token ไม่ใช่ 3.573) · วัด MAX_PATH headroom จริง

## [1.0.72] - 2026-08-17

### Changed (เปลี่ยน)

**Lead ไม่ต้อง "เฝ้า" อีกต่อไป — ค่าเริ่มต้นคือยิงงานแล้วจบเทิร์น (#287)**
- Lead ถูกจับได้ว่าค้างใน foreground Bash call เดียว **4 นาที 53 วินาที** (เพดาน 13 นาที) วน `takkub list` ทุก 20 วินาทีรอ backend — ขณะที่ user มีข้อความพิมพ์ค้างไว้ 7 บรรทัดที่ Lead อ่านไม่ได้
- ต้นทุนจริงไม่ใช่ socket call ที่ยิงซ้ำ แต่คือ **turn ของ Lead ถูกบล็อกทั้งอัน** → อ่านอะไรไม่ได้เลยรวมถึงข้อความ user · และ delivery pipeline เห็น Lead busy ตลอดช่วงนั้น (เงื่อนไขเดียวกับที่ต้องคิด busy-deliver escalation ขึ้นมาแก้ใน #279)
- แม้แต่ `takkub wait` ที่เป็นวิธี "ถูกต้อง" ก็ยังบล็อก turn อยู่ดี แค่ถูกกว่า — **ค่าเริ่มต้นใหม่คือไม่ต้องรอเลย** จบเทิร์นไป รายงาน done/FAILED จะถูกส่งเข้า pane ของ Lead แล้วปลุกเทิร์นใหม่เอง (คือหน้าที่ของ delivery pipeline ทั้งอัน ซึ่งทนพอแล้วหลัง #276/#277/#279)
- `takkub wait` เหลือเป็นข้อยกเว้น: ไม่มีงานอื่นทำเลย **และ** ต้องขยับทันทีที่รายงานถึง

### Fixed (แก้)

**กฎห้าม loop เฝ้า pane มีแต่ prose มาตั้งแต่ #242 — บังคับใช้ไม่ได้เลย (#287)**
- `docs/lead/role-and-workflow.md` เขียนว่า "เป็นแพทเทิร์นต้องห้ามเด็ดขาด" มาตลอด แต่ `pane_guard._UNGUARDED_ROLES = {"lead","shell"}` ทำให้ `classify()` คืน allow ก่อนกฎใดๆ ได้รัน
- **role เดียวที่มี teammate ให้เฝ้า และเป็นเจ้าของ `takkub wait` คือ role เดียวที่ถูกยกเว้นจากชั้นบังคับใช้** — บทเรียนซ้ำกับ #202/browser rule เป๊ะ: prose ชั้นเดียวไม่เคยยึด
- เพิ่มกฎ `pane_poll_loop` เช็ค**ก่อน** `_UNGUARDED_ROLES` exit — deny เมื่อครบ 3 อย่างพร้อมกัน: loop construct + `takkub list|status|inbox|ledger` + `sleep`
- ต้องครบทั้ง 3 จึงไม่กิน: fan-out ครั้งเดียว (ไม่มี sleep), curl/healthcheck poll ที่ `docs/lead/patterns.md` แนะนำเอง (ไม่ใช่ takkub), `takkub wait` เอง · role ว่าง = คนพิมพ์ที่ terminal จริง ยัง fail-open

**heredoc body ไม่ใช่โค้ด (#287)**
- เจอทันทีที่กฎขึ้น: การสร้าง issue #287 เอง (ซึ่ง quote loop ไว้เป็นหลักฐาน) ถูก deny โดยกฎที่มันกำลังอธิบาย — guard ที่ทำให้เขียนรายงานตัวเองไม่ได้ จะสอนให้ pane หาทางอ้อม guard
- strip heredoc body ก่อนแมตช์ · ยกเว้น heredoc ที่ป้อนให้ shell (`bash <<'EOF'`) ซึ่ง body ถูกรันจริง — ไม่งั้นเป็นทางอ้อมกฎแบบบรรทัดเดียว

## [1.0.71] - 2026-08-17

### Fixed (แก้)

**ทุกครั้งที่ pane เสร็จงาน มีข้อความที่ 2 ตามมาซ้ำเสมอ — "1 subprocess(es) still running… (pwsh.exe)" (#286)**
- warning นี้ (#234) มีไว้บอก Lead ว่า close กำลังจะฆ่างานที่ยังรันค้าง เช่น `docker compose build` · #272 เคยกรอง scaffolding ของแต่ละ provider ออกไปแล้วรอบหนึ่ง แต่ **`pwsh.exe` ของ codex หลุดตะแกรง**
- codex บน Windows รัน shell tool ผ่าน PowerShell และ**ถือ host process นั้นค้างตลอดอายุ pane** → ยืนอยู่ในทรีทุกครั้งที่ปิด
- หลักฐาน (prod events.log 2026-08-17): pane codex ปิด **10 ครั้ง ยิง warning 10 ครั้ง** ทุกครั้ง `count:1 names:["pwsh.exe"]` เป๊ะ ทั้งที่งานคนละเรื่องกันหมด (pg_dump / รัน test suite / build frontend) — งานค้างจริงต้องผันแปร ไม่ใช่ค่าคงที่ · เมื่อวานเจอคู่กับ `codex-code-mode-host.exe` เสมอ พอ #283 ใส่ `--disable apps` ตัว host หายไป เหลือ pwsh ตัวเดียว
- แก้: `pwsh/powershell` เข้า `codex_spec.scaffolding_process_names` (ใส่ `powershell.exe` ด้วยเพราะ spawn_engine fallback ไปตัวนี้เมื่อไม่มี PowerShell 7)

**`takkub done` ของ pane เอง ถูกนับเป็น "งานที่กำลังจะถูกฆ่า" (#286)**
- close ที่ warning นี้ยิงออกมา คือ auto-close 2.5 วิที่ `done()` ตั้งไว้ — และ process `takkub done` ที่ขอ close นั้น **ยังยืนอยู่ในทรีแน่นอนโดยนิยาม** ตอนที่เช็ค
- เตือนว่ากำลังจะฆ่า "การเรียกที่ทริกเกอร์คำเตือนนี้เอง" = self-reference ล้วนๆ และเป็นลูกตัวเดียวที่การันตีว่าอยู่ครบทุก done-close
- แก้: เพิ่ม `GENERIC_SCAFFOLDING_PROCESS_NAMES` (ข้ามทุก platform ทุก provider) = `takkub`

**ยังเตือนเหมือนเดิมถ้ามีงานจริงค้าง** — filter เป็นการ "ลบออก" ไม่ใช่ปิดเสียง: pane codex ที่ปิดตอน `docker` ยังรันอยู่ ยังได้ warning ครบ (มีเทสพินไว้)

## [1.0.70] - 2026-08-16

### Fixed (แก้)

**codex pane ค้าง booting 342-388 วินาที — `codex_apps` โชว์ `esc to interrupt` ทั้งที่ composer พร้อมแล้ว (#283)**
- `codex_apps` = MCP ในตัวของ codex (feature `apps`, stable, เปิดเป็นค่าเริ่มต้น) — บูตนานหลายนาที และระหว่างนั้นแสดงคำว่า `esc to interrupt` ในแถบสถานะ **ทั้งที่ composer รับ input ได้แล้ว**
- คำนั้นคือ `ready_hard_blockers` ของ codex_spec เอง → cockpit อ่านว่า pane ยุ่งตลอด ไม่ยอมส่ง task จน timeout
- วัดจริง เครื่องเดียว task เดียว: **apps เปิด** → ready 342s/388s, task ไม่เคยส่งสำเร็จ · **apps ปิด** → ready **0s**, task ถึงใน **1s**
- แก้ด้วย `--disable apps` ใน `autonomy_flags` ของ codex_spec (session-scoped ไม่แตะ `~/.codex/config.toml` ของผู้ใช้) — ถอดผ่าน `shared-mcp-*.json` ไม่ได้เพราะ `codex_apps` อยู่ข้างใน codex

**cockpit ส่ง task เร็วเกินไปตอน pane ยังบูตไม่เสร็จ (#284)**
- `is_at_ready_prompt()` สแกนแค่ 6 แถวล่างสุด (จูนมาสำหรับ footer บรรทัดเดียวของ claude) · codex มีกรอบ composer + status bar → บรรทัด boot หลุดกรอบทันทีที่ composer สูงขึ้นแค่แถวเดียว → เห็นแค่ `Fast off` → อ่านว่าพร้อม
- และสาขา ready-streak → `_deliver()` **ไม่เคยเช็ค boot marker เลย** (เช็คเฉพาะตอน timeout จะ blind-paste)
- แก้: เงื่อนไขส่งงานเป็น `ready AND not booting` · boot probe มีกรอบสแกนของตัวเอง (20 แถว) เฉพาะตอนตัดสินส่ง — ที่อื่นยังใช้กรอบแคบกัน boot line เก่าใน scrollback ค้าง


## [1.0.69] - 2026-08-16

### Changed (เปลี่ยน)

**codex ค้าง boot 90-150 วินาทีทุก spawn เพราะ MCP ที่ cockpit ฉีดใช้ `npx -y` แบบไม่ pin version (#281)**
- `npx` ไปถาม npm registry ทุกครั้งที่รัน แม้แพ็กเกจอยู่ในแคชแล้ว · cockpit รู้เรื่องนี้อยู่แล้วจึง **pin version** ให้ MCP ที่ตัวเองใส่ (playwright/chrome-devtools/graft) แต่ entry ที่ผู้ใช้เพิ่มเอง (context7, figma) ไม่มี version → จ่ายค่า network ทุก spawn (วัดจริง: 23.1s + 9.9s รอบแรก, 11.4s + 28.0s รอบถัดมา)
- **codex เจ็บที่สุดเพราะมันบล็อกรอ MCP โหลดเสร็จก่อนรับ input** (composer โชว์ `esc to interrupt` ตลอด) ส่วน claude ไม่บล็อก — config ชุดเดียวกันจึงดูปกติดีฝั่ง claude
- แก้: ใส่ `--prefer-offline` ให้ npx entry ตอน**เขียนไฟล์ที่ pane โหลดจริง** (ไม่แตะ master config ที่เป็นของผู้ใช้) — ใช้แคชก่อน ยิง network เฉพาะตอนไม่มีจริงๆ · ไม่ใช้ `--offline` เพราะรอบแรกสุดจะพังทันที
- `[delivery-boot-stall]` บอกด้วยว่า**ค้างที่ MCP ตัวไหน** — ดึงบรรทัด `Starting MCP servers (0/3): …` จากจอ pane เอง
- แก้คอมเมนต์ใน `provider_spec.py` ที่แนะนำให้ใช้ `codex mcp list` วินิจฉัย — **มันมองไม่เห็น MCP ที่ cockpit ฉีด** (session-scoped ผ่าน `-c mcp_servers.…` ไม่แตะ `~/.codex/config.toml`) จึงตอบ "No MCP servers configured yet" ทั้งที่มี 3 ตัวกำลังโหลด

**`tab to queue message` ถูกนับเป็น boot marker (#282)**
- marker เดียวถูกใช้ตอบ 2 คำถามที่ต่างกัน: "ไม่ใช่ turn ทำงานจริง" (idle watchdog — ถูก) กับ "ยังบูตอยู่" (delivery / boot-stall / `takkub list` — ผิด) · codex โชว์ `tab to queue message` **ตลอดเวลาที่ทำงาน** pane ที่ทำงานอยู่จึงอ่านว่ากำลังบูต
- แยกเป็น `_BOOT_PHASE_MARKERS` กับ `_QUEUED_MESSAGE_MARKERS` แล้วให้ delivery/boot-stall/display-state ใช้ boot-phase อย่างเดียว · idle watchdog ใช้ union เหมือนเดิม
- ถ้าไม่แยก boot ceiling ของ #276 (300s → ปิด pane + FAILED) จะไปฆ่า pane ที่ทำงานอยู่จริง

**ระบบเฝ้า pane: เฝ้าเงียบๆ แล้วรายงานตอนปิด pane แทนการรายงานสด (#280)**
- เดิม watchdog เห็นอะไรก็ยิง notice หา Lead ทันที — `[delivery-busy-wait]` · `[delivery-boot-stall]` · `[delivery-unconfirmed]` · `[no-content-retry]`/`[no-content-degrade]` · `[auth-failure-degrade]` — ทั้งหมดเป็น status update ของ pane ที่ยังทำงานอยู่และส่วนใหญ่อีกแป๊บก็จบปกติ · Lead pane เลยเต็มไปด้วยเรื่องที่ cockpit พูดถึงตัวเอง ไม่ใช่ผลงานของทีม (และเก่าตั้งแต่ก่อนถึงมือ จนต้องมี `_revalidate_system_notice` มาไล่ตรวจ — สัญญาณว่าออกแบบผิดตั้งแต่แรก)
- **การเฝ้ายังอยู่ครบ** (ยังกู้ pane, ยัง fail task ที่ไปต่อไม่ได้ตาม #276) แต่สิ่งที่เห็นถูกสะสมไว้ต่อ pane lifecycle แล้วแนบไปกับ report ตอน `done` / `done --fail` / `close` เป็นบรรทัดเดียว: `🩺 [pane health] boot ช้า 110s · task ถูก paste แบบ blind ×2`
- **pane ที่ตายโดยไม่รายงาน** (ถูก close / crash) ยังพูดแทนตัวเองตอนปิด — ไม่งั้น "เงียบ" จะแย่กว่า "หนวกหู"
- ยิงทันทีเหมือนเดิมเฉพาะเคสที่รอรายงานตอนจบไปไม่ถึง: **ไม่มีตอนจบ** (`spawn-failed` ไม่มี pane เลย · `spawn-stuck` · `respawn-capped`) และ **pane ไปต่อไม่ได้จนกว่าคนจะมาทำอะไร** (auth wall · ติด trust/permission prompt)
- สลับได้ด้วย env `TAKKUB_PANE_WATCH_NOTICES`: `terminal` (ค่าเริ่มต้น = ใหม่) · `live` (พฤติกรรมเดิม) · `off` (ไม่เก็บไม่รายงานเลย)

### Fixed (แก้)

**pane รายงาน `done` ทั้งที่ไม่เคยได้รับ task (#278, #276)**
- หลักฐาน (#278): `codex exec "reply with the single word: ok"` ยิง `takkub done "Acknowledged user request"` ออกมาเองตั้งแต่ turn แรก — มันอ่านคำสั่ง orchestrator ที่ถูก inject แล้วตีความว่า "ตอบคำถามจบ = งานเสร็จ" · ผลจริงในงาน: Lead ได้ report ที่อ่านดูสมบูรณ์ แต่เปิดไฟล์จริงแล้วโค้ดเดิมอยู่ครบ ไม่มีอะไรเปลี่ยนเลย
- `done()` ตอนนี้**ปฏิเสธ**รายงานจาก pane ที่มี task assign ไว้ แต่ task นั้น**ไม่เคยถูกส่งถึง pane เลย** (ไม่มี delivery เขียนลง PTY · ไม่มี preload ตอน spawn · ไม่มี `takkub send` · cockpit ไม่เคย mark ว่า working) — ทุกสัญญาณเป็น bookkeeping ของ cockpit เอง จึงใช้ได้กับทุก provider (#103) ไม่ได้อ่านจอ CLI ตัวไหนเป็นพิเศษ
- เกณฑ์แคบโดยตั้งใจ: pane ที่**ไม่มี assign อยู่เลย** (spawn แล้วสั่งด้วยมือในจอ) ปล่อยผ่าน — ไม่มี task ค้างอยู่ให้ done ปลอมไปปิด และการห้ามจะพังงานที่ทำอยู่จริง · เคสที่ cockpit ตามไม่ทันจริงๆ ใช้ `takkub done --force`
- เพิ่ม flag `⚠️ ไฟล์ที่แตะ:0 — ยังไม่มีอะไรเปลี่ยน` ใน digest ของ Lead เมื่อวัดได้จริงว่าเป็นศูนย์ (ไม่ใช่ "ตรวจไม่ได้") — เตือน ไม่บล็อก เพราะงาน review/QA/research จบโดยไม่แตะไฟล์ได้ตามปกติ
- แก้ถ้อยคำ AGENTS.md ที่ codex อ่าน: `takkub done` ใช้รายงาน **task ที่ได้รับมอบหมาย** เท่านั้น ไม่ใช่ "ตอบคำถามจบ" และห้ามเรียกคำสั่ง cockpit เมื่อรันนอก pane (ไฟล์นี้วางที่ root โปรเจกต์ `codex exec` ที่ผู้ใช้รันเองจึงอ่านเจอด้วย)

**pane ค้าง boot แล้ว task หายเงียบ (#276)**
- เดิม: pane ที่ค้างอยู่ที่ boot phase จะถูกรอจนถึง `BUSY_WAIT_CEILING_SEC` (**30 นาที**) แล้วค่อย blind-paste ลงหน้า boot ที่ไม่มีช่องรับข้อความ — task จึงไม่ถูกส่ง และไม่ถูก fail มันแค่หายไป
- ตอนนี้มี `BOOT_STALL_CEILING_SEC` (ค่าเริ่มต้น **300s**, override ด้วย env ได้) ครบแล้ว cockpit จะ **fail task นั้นชัดๆ**: ledger flip เป็น fail · Lead ได้ notice แบบ blocking พร้อมคำสั่งกู้ · ปิด pane ทิ้ง — ผลลัพธ์อาจไม่ดี แต่ไม่เงียบ
- notice `[delivery-boot-stall]` เลิกอ้างว่า "กำลังโหลด MCP server" — #278 วัดแล้วว่า codex ใช้ 61 วินาทีกับ prompt เปล่าทั้งที่ `codex mcp list` บอกว่า**ไม่มี MCP server ตั้งไว้เลย** · ตอนนี้รายงานเฉพาะสิ่งที่เห็นจริง (startup marker ยังค้างอยู่) และบอกด้วยว่าอีกกี่วินาที cockpit จะจัดการเอง เพื่อให้ Lead เลิกนั่งเฝ้า

**`takkub send` หายเงียบเมื่อ pane respawn (#277)**
- เดิม `send` ตอบ `ok: sent` เสมอ ไม่เก็บอะไรไว้เลย — pane respawn เมื่อไหร่ข้อความหายไปพร้อม process และ**ตรวจย้อนหลังไม่ได้แม้แต่ในทางทฤษฎี** · เคสจริง: คำสั่งแก้สเปกเรื่องวิธีเก็บรหัสผ่านหายไป agent เลยเดินหน้าสร้างตามสเปกที่ถูกยกเลิกแล้ว (respawn 4 ครั้ง หาย 3 ข้อความ)
- เพิ่ม message log ถาวรต่อ role (`runtime/messages/<project>.jsonl`) — บันทึกทุกข้อความพร้อม session generation ที่เขียนลงไป
- **ส่งซ้ำอัตโนมัติ**: ข้อความที่เขียนลง generation เก่าและยังไม่ยืนยันว่าถึงมือ = โดน respawn กลืน → cockpit ส่งซ้ำผ่านทาง delivery ปกติ (จำกัด 3 ครั้ง กัน pane ที่ crash วนไม่ให้โดน paste ซ้ำไม่รู้จบ) แล้วแจ้ง Lead ว่าเกิดขึ้น
- **`ok: sent` เลิกโกหก** → ตอบ `queued to <role> (id ...)` พร้อมบอกวิธีตรวจ
- คำสั่งใหม่ **`takkub messages --role <role>`** — อ่านย้อนหลังได้ว่าข้อความไหน ✅ ถึงแล้ว / ⏳ ยังไม่ยืนยัน / ⛔ ส่งไม่สำเร็จ และโดนส่งซ้ำกี่รอบ

**รายงานของ teammate ถึง Lead ช้า ~90 วินาที แล้วมากองพร้อมกันทีเดียว (#279)**
- อาการที่เห็น: Lead รอ `takkub wait` นานเกินจำเป็น แล้ว notice ก็โผล่มาเป็นพรืดพร้อมกันหลายอัน ทั้งที่ teammate ปิด pane ไปตั้งนานแล้ว
- สาเหตุจริง (นับจาก `events.log` ของ saas_admin วันที่ 2026-08-16 ทั้งวัน): `done` 36 ครั้ง แต่มี `lead_notify_spill` 52 + `done_notice_force_flush` 32 — แปลว่า**แทบทุกรายงาน**เดินสายยาวสุด คือ retry 75 รอบ (~30s) → spill ลง durable → รอ reaper อีก 60s → ค่อย force-flush
- ต้นตอ: `_pump_lead_notify` รอ `is_at_ready_prompt()` ของ Lead ก่อนวาง ซึ่งเขียนไว้สมัย Lead มีคนนั่งเฝ้า — Lead ที่ทำงานเองแทบไม่เคยว่าง ยิ่งกว่านั้น `takkub wait` เองจะค้างจนกว่ารายงานจะออกจาก pipeline (#163) **การรอนั้นเองคือสิ่งที่ทำให้ Lead ไม่ว่าง** → เงื่อนไขคลายได้ทางเดียวคือหมดเวลา
- แก้: ready prompt กลายเป็น "ถ้าได้ก็ดี" ไม่ใช่เงื่อนไขบังคับ — รอ Lead ว่าง 5 วินาที แล้ววางเลยแม้ Lead ยุ่ง (ข้อความไปนอนใน composer เป็น queued message เหมือนที่ force-flush ทำอยู่แล้ว) ยกเว้น 2 กรณีที่ยังห้ามวางเหมือนเดิม: **ผู้ใช้พิมพ์ค้างไว้** (draft guard #3) และ **Lead ติด trust/permission/tty prompt** (ตัวอักษรจะไปตอบ modal แทนที่จะเข้า composer — ของเดิมทาง force-flush ไม่เคยเช็คข้อนี้ ตอนนี้เช็คแล้ว)
- แก้เพิ่ม: role ที่ `takkub wait` กำลังรออยู่ **ข้าม digest window 15 วินาที** ทันที — การหน่วงเพื่อรวมเป็นชุดไม่มีประโยชน์กับ role ที่ Lead บล็อกรออยู่แล้ว
- ลดเสียงรบกวน: notice ตระกูล delivery-health ที่ pane เจ้าของปิดไปแล้วก่อนข้อความจะถึง Lead จะถูก**ย่อเหลือบรรทัดเดียว** แทนที่จะแปะ banner ทับข้อความเต็ม 8-10 บรรทัด (เนื้อความคือวิธีแก้ ซึ่งพอ pane หายไปแล้วก็ห้ามทำตาม) · `[spawn-failed]` ถูกกันออกจากการ re-check นี้ เพราะ "ไม่มี pane" คือการ**ยืนยัน**ข้อกล่าวหาของมัน ไม่ใช่การหักล้าง (บั๊กเดิมที่บอกว่า spawn ล้มเหลว "คลี่คลายแล้ว")
- ผลลัพธ์: รายงานถึง Lead ภายใน ~5 วินาทีแทน ~90-105 วินาที และมาทีละใบตามจังหวะจริง ไม่กองรวมกันอีก

## [1.0.68] - 2026-08-16

### Fixed (แก้)

**Performance Health บอกสาเหตุผิด (#274)**
- หน้าต่าง Performance Health และ chip บน status bar รายงาน `cpu_high` ทั้งที่ CPU อยู่แค่ 29% — `_denial_reason()` เช็คแค่ว่า RAM ต่ำกว่าเกณฑ์ pause ไหม ถ้าไม่ใช่ก็ **ตกไปคืน `cpu_high` เสมอโดยไม่เคยอ่านค่า CPU จริง**
- ความจริงของสถานะนั้นคือ latch ยังค้างอยู่เพราะ hysteresis: เข้าโหมด overload เมื่อ CPU ≥ 85% **หรือ** RAM ว่าง < 20% แต่ออกได้เมื่อ CPU ≤ 65% **และ** RAM ว่าง ≥ 25% — latch จึงมักอยู่นานกว่าตัวชี้วัดที่จุดชนวนมันตั้งแต่แรก
- ตอนนี้รายงานตามค่าจริง: `cpu_high` เมื่อ CPU เกินเกณฑ์ของตัวเอง · `memory_low` เมื่อ RAM ต่ำจริง · เพิ่มสถานะใหม่ **`waiting_resume`** สำหรับเคสที่ทั้งคู่ผ่านเกณฑ์ pause แล้วแต่ยังไม่ถึงเกณฑ์ resume พร้อมบอกว่าขาดอีกเท่าไหร่ · หน้าต่างแสดงเกณฑ์ที่ใช้อยู่ให้เห็นด้วย
- **ไม่มีการเปลี่ยนค่าเกณฑ์ใดๆ** — กลไกตัดสินใจเดิมถูกต้องแล้ว งานนี้แก้เฉพาะการรายงาน


## [1.0.67] - 2026-08-16

รอบ **"ลดค่าใช้จ่ายต่อ pane + Lead ต้องมีทางออกเสมอ"**

### Fixed (แก้)

**codex/gemini บูตไม่ทันแล้วงานหาย (#271)**
- `codex_spec.ready_wait_ms` ตั้งไว้ 90 วินาที แต่เวลาบูตจริงบนเครื่องทดสอบคือ 90-150 วินาที (วัดจาก 4 spawn ในวันเดียว ค้างเกิน 90 วิ ทุกครั้ง) → นาฬิกาหมดตอนยังบูตไม่เสร็จ → ระบบ paste งานแบบ blind เข้า composer ที่ยังไม่พร้อม → **งานตกหล่น 3 ใน 4 รอบ**
- แก้ที่ต้นเหตุ: **ห้าม blind-paste ขณะที่จอยังแสดง boot marker** (ใช้ `shows_startup_marker()` เดิม) ให้ยืดการรอแทน มีเพดานที่ `BUSY_WAIT_CEILING_SEC` · เป็น provider-agnostic จึงช่วย gemini ที่ค้างเฟส sign-in ด้วย · และยก `ready_wait_ms` ของ codex เป็น 180 วินาที

**notice ตอนปิด pane เตือนทุกครั้งจนไร้ความหมาย (#272)**
- เดิมเช็คแค่ "มี child process ไหม" แต่ทุก pane มี scaffolding ของ CLI ติดอยู่เสมอ (cmd.exe/conhost.exe/node.exe/pwsh.exe/codex-code-mode-host/python) → **เตือน 100% ของทุกการปิด** กิน context ของ Lead ฟรีวันละหลายสิบใบ
- เพิ่ม `ProviderSpec.scaffolding_process_names` แยกตาม provider แล้วกรองออกก่อนตัดสินใจ — เตือนเฉพาะเมื่อเหลือ process งานจริง (docker/pytest/build tooling) เจตนาเดิมของ #234 ยังอยู่ครบ

**Lead ไม่มีทางออกเมื่อ provider ของ role พัง (#270)**
- `--model` ปฏิเสธ claude model id สำหรับ role ที่ map ไป codex ทั้งที่ `--help` เขียนว่า "takes precedence over role/provider defaults" — พอ codex บูตไม่ผ่าน งานทั้งสายค้างโดยไม่มีทางแก้
- เพิ่ม **`takkub assign --provider <name>`** เป็นทางออกที่แสดงเจตนาชัด · `--model` ตรวจความเข้ากันได้กับ provider **หลัง override** · error message ชี้ทางแก้ · boot-stall notice บอกคำสั่งที่ใช้ได้จริง
- **ตัดสินใจไม่ auto-degrade ตอน boot-stall** (ต่างจาก auth-failure/no-content ที่ตายสนิท) เพราะ boot-stall ยังกู้เองได้ — เหตุผลเต็มใน `docs/audit/2026-08-16-270-provider-override.md`

**task ยาวส่งไม่ถึง pane ที่อ่านไฟล์ไม่ได้ (#273)**
- task ยาวถูกแปลงเป็น "ไปอ่าน spec จากไฟล์" แต่ pane ที่ไม่มี file-read tool เปิดไม่ได้ → ตอบ FAILED ทันที เสีย spawn ฟรี และยังไป trigger fix-loop ให้ Lead ไปหา root cause ของงานที่ไม่เคยเริ่ม
- เพิ่ม `ProviderSpec.supports_agent_file_read` → provider ที่อ่านไฟล์เองไม่ได้จะได้ task แบบ inline แทน pointer · แยก **delivery-failure ออกจาก task-failure** ไม่ให้ปนกัน
- boot-stall notice แนบ **error จริงจาก provider** (เช่น `codex mcp list`) ต่อท้าย แทนข้อความ generic — ดึงแบบ async ไม่บล็อก Qt thread

**CodeQL 15 alert**
- แก้จริง 6: `permissions:` ใน ci.yml (2), temp file ไม่ปลอดภัยใน shortcut.js, TOCTOU ใน pathfix.js (เจอบั๊ก EBADF จริงระหว่างแก้), ลบไฟล์ HTML กำพร้าที่ไม่มีใครอ้างถึง
- dismiss 9 พร้อมหลักฐานและ `codeql[...]` annotation ในโค้ด: hash ใน session_store เป็น session token ไม่ใช่รหัสผ่าน · path guard ของ `_serve_static` ถูกต้องอยู่แล้ว (เขียนใหม่เป็น `is_relative_to()` ให้อ่านง่ายและทดสอบกับ symlink/`..`/drive-letter override) · `state.token` ใน app.js เลือกแค่หน้าจอ ไม่ใช่สิทธิ์ · 5 ใบที่เหลืออยู่ในไฟล์เทส

### Changed (เปลี่ยน)

**token diet รอบ 2 (#267)**
- แปลกฎที่โมเดลอ่านอย่างเดียวใน role file ทั้ง 16 ใบเป็นภาษาอังกฤษ — staged context **78,657 → 52,053 token (−33.8%)** วัดด้วย tiktoken บนไฟล์ที่ stage แล้วจริง
- **ข้อความที่ผู้ใช้ต้องอ่านยังเป็นภาษาไทยทั้งหมด** (placeholder ของ `takkub done`, ข้อความตอน blocked, output template ของ qa) — cross-check โดย codex ทีละบรรทัดครบ 16 ไฟล์ พบจุดที่แปลเกิน 26 จุดแล้วคืนกลับเป็นไทย พร้อมแก้คำแปลที่ทำให้ความหมายเพี้ยน 2 จุด


## [1.0.66] - 2026-08-16

### Fixed (แก้)

**pane ที่ auth ไม่ผ่านไม่เคย degrade เป็น claude (#269)**
- role ที่ provider ล็อกอินไม่ผ่าน (เคสจริง: qa/gemini-agy ล้ม 3 รอบติด) **ไม่เคยถูก degrade เป็น claude substitute เลย** ทั้งที่ backend/frontend ในเซสชันเดียวกันบนเครื่องเดียวกัน degrade ได้ปกติ → QA gate ค้างทั้งที่งาน dev เสร็จหมด และไม่มี workaround นอกจากให้ผู้ใช้ไปล็อกอินเอง
- **root cause:** จุดเดียวในระบบที่แตะ `provider_override` (คือ degrade) คือ no-content watchdog ที่เฝ้าว่า pane ไม่พ่นอะไรออกมาเลย — แต่ pane ที่ติด auth **พ่นข้อความ error ออกมา** ซึ่งนับเป็น "content" watchdog จึงไม่มีวันเจอเคสนี้ และไม่มีทางไปถึงโค้ด degrade · backend/frontend ที่ degrade ได้เพราะล้มด้วยอาการ no-content จริงๆ คนละอาการกัน
- แยก `_recover_auth_failed_pane` ออกมา **degrade ทันทีตั้งแต่ครั้งแรกโดยไม่ retry ก่อน** (ต่างจาก no-content ที่ retry ก่อน) เพราะกำแพง login ไม่หายเอง · close+respawn+degrade ถูก extract เป็น `_recover_broken_pane` ให้ทั้งสอง path ใช้ร่วมกัน
- ครึ่งหลังของ #269 (`takkub list` โชว์ working ทั้งที่ pane ไม่เคยถึง ready prompt) ถูกคุมแล้วโดย `login-required` tier ของ #263 ใน 1.0.65


## [1.0.65] - 2026-08-16

รอบต่อเนื่องจาก 1.0.64 ในวันเดียวกัน — ปิดสองใบที่ค้างไว้เพราะรอบก่อนทำได้ไม่ครบ

### Fixed (แก้)

**สถานะ pane มีแหล่งความจริงเดียว (#263)**
- เดิม `takkub list`/`status` เอาสถานะมาจาก 3 ที่ที่เดินแยกกัน: `pane.state` ที่ orchestrator ประกาศเองตอน dispatch · ready marker ที่ scrape จากหน้าจอ · progress clock — ทั้งสามขัดกันเองได้และขัดจริง (gemini ขึ้น "working" ทั้งที่ค้าง "Signing in..." และ task ไม่เคยถูกส่ง · codex ขึ้น "active" ทั้งที่จอโชว์ "Working (esc to interrupt)" · kimi ขึ้น "active" ทั้งที่ค้างหน้า `/login`)
- เพิ่ม `display_state` ที่ derive จากทั้งสามแหล่งด้วยลำดับความสำคัญที่เขียนไว้ชัด (จอชนะ label ที่ประกาศเองเสมอ): **login-required → booting → waiting-delivery → busy → ค่าเดิม** · เป็น key ใหม่แบบ additive ของเดิมทุกที่ที่อ่าน `state` จึงไม่พัง
- provider ที่ marker ยัง calibrate ไม่ได้ แสดง unknown อย่างซื่อสัตย์ ไม่ fallback เป็น label ที่ดูมั่นใจ

### Added (ของใหม่)

**subagent mode (#268)**
- `takkub assign --mode pane|subagent` — งานรูปทรง scan/audit/หาของ/fan-out เยอะๆ รันเป็น subagent ในโปรเซสเดิมได้ ไม่ต้องเปิด pane ใหม่ จึงไม่ต้องรอ provider บูต และไม่เจอบั๊กตระกูล delivery เลย (#254/#255/#256/#257/#26/#144)
- **default = `pane` พฤติกรรมเดิมไม่เปลี่ยน** · `--mode subagent` ใช้ร่วมกับ `--model` ไม่ได้ (subagent ใช้ provider/model ของ parent เสมอ จึงแทน cross-check ต่างโมเดลไม่ได้ — เขียนข้อจำกัดนี้ไว้ในเอกสารแล้ว) และใช้กับ `--plan` ไม่ได้
- shard cap: 20 สำหรับ subagent (pane ยังคง 8) · ผลลัพธ์เข้าท่อ `takkub subagent-done` เพื่อให้ ledger/inbox/wait เห็นเหมือน pane ปกติ
- `routing_planner` แนะนำโหมดให้เมื่อเจอรูปทรงงานที่เข้าข่าย (เป็นข้อเสนอ Lead ตัดสินเอง)
- ปรับกฎ "ห้าม spawn subagent" ทั้งระบบให้เป็นเงื่อนไขเดียวกัน — **"ห้าม spawn เอง เว้นแต่ Lead สั่งด้วย `--mode subagent`"** ครบทุกจุด (role file ทุกใบ, prompt builder, guard, เอกสาร) เพื่อไม่ให้ teammate ได้รับคำสั่งขัดกันเองในเทิร์นเดียว


### Added

- `takkub assign --mode pane|subagent` (#268): ค่าเริ่มต้นยังเป็น pane; งาน scan/audit/search/triage/fan-out เลือก native subagent provider เดียวกับ Lead ได้โดยไม่เปิด pane และปิดงานผ่าน `subagent-done` เข้าสู่ ledger/inbox/wait เดิม. โหมดนี้ไม่ใช่ cross-model/cross-provider verification.

## [1.0.64] - 2026-08-16

รอบ **"cockpit เลิกโกหก Lead + ลดค่า token ต่อ pane"** — วันเดียวปิด 16 issue (#251-#266) ที่เกือบทั้งหมดโผล่มาจากการใช้งานจริงเช้าวันนั้น เริ่มจาก remote-control ที่ดับเองทุกคืน แล้วสาวไปเจอตระกูลบั๊ก "สัญญาณที่ดูน่าเชื่อถือแต่ไม่จริง" อีกเป็นพวง

### Fixed (แก้)

**remote-control ตายทุกเช้า (#252, #260)**
- idle-expire (4 ชม.) เคย `stop()` เซิร์ฟเวอร์+tunnel แล้ว **เขียน `enabled=false` ลงดิสก์** ทำให้เปิด cockpit ใหม่ remote ไม่กลับมาเอง และ re-enable ยัง mint `secret_path`/`token` ใหม่ทุกครั้ง = ต้องสแกน QR ใหม่ทุกเช้า · ตอนนี้แยก **auto-suspend** (พักเอง เก็บ pairing เดิม กลับมาเองตอนเปิด cockpit ใหม่) ออกจาก **Disable ที่ผู้ใช้กดเอง** (revoke pairing ทิ้ง เพื่อเคสมือถือหาย) + `idle_expire_min` ตั้งค่าได้ใน Settings (0 = ปิด)
- PWA เคยขึ้น chip **"Online" ทั้งที่ tunnel ล่ม** เพราะ `apiFetch` ถือว่า response ที่ resolve = ติดต่อได้ · ตอนนี้แยก 5xx ที่เป็น error page ของ edge (Cloudflare 530 → Offline + บอกว่า cockpit ปิด remote อยู่) ออกจาก 504 JSON ของ cockpit เอง (bridge timeout → ยัง Online แต่บอกว่าตอบช้า)

**สัญญาณที่โกหก Lead (#253, #258, #259, #262, #263, #265, #266)**
- `takkub wait` **resolve ด้วย done event เก่า** ทำให้ Lead เชื่อว่างานเสร็จแล้วทั้งที่ pane ยังทำอยู่ → commit/สรุปงานที่ยังไม่เสร็จ · แก้โดยยกพื้นความสดด้วย `assign_ts` ของ role นั้น event เก่าจึงชนะ assignment ปัจจุบันไม่ได้อีก (#262)
- `wait` เคยหูหนวกกับทุกอย่างที่ไม่ใช่ role ที่ตัวเองเฝ้า ตอนนี้ตื่นเมื่อมี blocking report จาก role อื่น (#253) · จาก role ที่เฝ้าเองแต่ไม่ใช่ done/FAILED เช่น `[delivery-unconfirmed]` (#259) · และ **เมื่อเจ้าของพิมพ์ข้อความใหม่เข้ามา** (#265) ซึ่งเดิมต้องรอจนครบ timeout 30 นาที
- delivery ที่ถูก cancel แล้วยัง **paste ซ้ำทับ composer** ได้ เพราะ path repaste ไม่ได้แนบ validator (#258)
- notice ถูกส่งถึง Lead โดยไม่เช็คว่ายังจริงอยู่ไหม → Lead ได้แต่ "ข่าวเก่า" (worktree ที่ commit ไปแล้ว, pane ที่ปิดไปแล้ว) ตอนนี้ re-validate ก่อน flush + ติดเวลาเกิดเหตุ (#266)
- สถานะ pane เคยมาจาก 3 แหล่งที่ขัดกันเอง — เพิ่ม flag `delivery_unconfirmed` ให้ `takkub list` บอกได้ว่า "ยังส่งงานไม่ถึง" แทนที่จะขึ้น working ลอยๆ (#263 บางส่วน)

**งานส่งไม่ถึง pane (#254, #255, #256, #257)**
- pane ที่ค้างเฟส boot (codex โหลด MCP) เคยเงียบยาวถึง 30 นาทีเพราะ spinner ทำให้ busy-wait ต่ออายุตัวเอง ตอนนี้ยิง `[delivery-boot-stall]` ที่ ~110 วินาที พร้อมบอกวิธีแก้ (#254)
- เพิ่ม `takkub task cancel --role` + `takkub send` ยกเลิก delivery ค้างให้อัตโนมัติ + `expire_stale()` ที่เดิมเป็น dead code และ scope ผิด (จะกวาด delivery ที่ teammate กำลังทำงานจริงอยู่) (#255)
- gemini/agy ขึ้น auth-failure หลอกทุก cold start เพราะ banner ของมันพิมพ์ "You are currently not signed in." เอง — ย้ายไปเป็น transient marker ที่มี grace คุม (#256)
- kimi ไม่มี ready marker เลย (`ready_rules=()`) ทำให้ task **ไม่เคยถูกส่งถึง** — ใส่ marker ที่ capture จาก TUI จริง (#257)

**รายงาน "ไฟล์ที่แตะ" ผิด (#251, #261)**
- โหมด shared tree เคยรายงาน dirty ทั้งทรี รวมงานของ pane อื่นและไฟล์ค้างเก่า · ตอนนี้ snapshot dirty path + mtime/size ตอน assign แล้วเทียบตอน done · probe ล้มเหลว = แสดง "ตรวจไม่ได้" ไม่ใช่แกล้งบอกว่าสะอาด (#251)
- `?? dir/` ที่ git ยุบมา ถูกขยายเป็นระดับไฟล์ด้วย `-uall` ที่ scope เฉพาะโฟลเดอร์นั้น (cap 2000 entry + fallback) เพื่อไม่ให้ไฟล์ที่แก้ลึกในโฟลเดอร์ untracked หลุดรายงาน (#261)

### Added (ของใหม่)

- **กันกดปิด pane ผิด** — กด X บนหัว pane หรือบนแท็บ ต้องยืนยันก่อน โดยบอกว่า role ไหน กำลังทำงานอยู่ไหม และมีไฟล์ที่ยังไม่ commit / commit ที่ยังไม่ merge อยู่เท่าไหร่ · default = ยกเลิก · **เส้นทางปิดอัตโนมัติทุกทางไม่ถูกแตะ** (CLI close, auto-close หลัง done, close-all, shutdown) เพื่อไม่ให้ automation ค้างรอคนกด
- **หลอดแสดงโควต้าในป็อปอัพ usage** — Claude 2 หลอด (5h/7d), Codex 1 หลอด · **provider ที่ไม่มีโควต้าจริง (OpenCode) ไม่มีหลอด** เพราะการวาดหลอดโดยไม่มีตัวหารคือการกุตัวเลข · ข้อมูลเก่า (stale) แสดงต่างจากข้อมูลสด

### Changed (เปลี่ยน)

- **token diet (#267)** — วัดจริงจาก session JSONL พบว่า `CLAUDE.md` ที่ root ถูก Claude Code auto-load เข้า**ทุก pane ทุก role ทุกเทิร์น** (6,728 token) ทั้งที่เนื้อหาส่วนใหญ่เป็นคู่มือ Lead เท่านั้น · ย้ายเนื้อหา Lead-only ไป `docs/lead/role-and-workflow.md` (ครบทุกหัวข้อ ไม่ลดเนื้อหา) เหลือ root CLAUDE.md **948 token** = ลด 5,780 token ต่อ pane ต่อเทิร์น
- `takkub wait` ลด cap `--timeout` จาก 2 ชั่วโมงเหลือ 30 นาที เพื่อไม่ให้ Lead เผลอ park ยาวโดยไม่มีจุดเช็คอิน (#253)
- installed build: rewrite path ของเอกสาร `docs/lead/*.md` ตามเข้าไปในตัวไฟล์ที่ ship ไปด้วย ไม่ใช่แค่ระดับบนสุด


## [1.0.63] - 2026-08-15

รอบ **"pane ค้างต้องรู้ตัวใน 1-2 นาที ไม่ใช่ 30 นาที"** — ปิด #247, #248, #249, #250 ต่อจากรอบ 1.0.62 เน้นเรื่องเดียว: สัญญาณที่ cockpit ใช้ตัดสินว่า pane "ยังทำงานอยู่" ต้องเป็นความคืบหน้าจริง ไม่ใช่แค่มีไบต์ไหลออกมา

### Fixed (แก้)

**สัญญาณ liveness ปลอม (#248, #247)**
- **pane ที่ค้างตั้งแต่วินาทีแรกยังถูกนับว่า "กำลังทำงาน" จนชน ceiling 1800 วินาที** — ต้นเหตุ: `_last_output_ts` ถูกประทับเวลาทุกครั้งที่มี **raw byte** เข้ามา ซึ่งรวม escape sequence ตอน terminal init กับ spinner ที่หมุนอยู่กับที่ด้วย pane ที่ค้างหน้า login หรือหน้าจอว่างจึงดู "มี output ตลอด" ตอนนี้แยก 2 สัญญาณออกจากกัน: `seconds_since_byte()` = มีไบต์ไหม กับ `seconds_since_output()` = **เนื้อหาบนจอเปลี่ยนจริงไหม** โดยเทียบ fingerprint ที่ตัด spinner glyph (braille / `◐◑◒◓` / `|/\-` เดี่ยวๆ) กับจุดไข่ปลาท้ายบรรทัดออกก่อน
- **auth fail-fast** — pane ที่ตกหล่นการล็อกอินเดิมต้องรอ busy-wait ceiling เต็ม 30 นาทีถึงจะรู้ ตอนนี้ตรวจได้ในไม่กี่วินาที แยก marker เป็น 2 ชั้น: **ชั้น error** (`not signed in`, `please sign in again`, …) กับ **ชั้น transient** (`signing in`, `verifying your account` — ของ gemini) ที่ต้องเงียบครบ grace 45 วินาทีก่อนถึงจะนับ · ทุกการตรวจ scope อยู่แค่ footer region ของจอ ไม่ใช่ทั้งหน้า เนื้อบทสนทนาจึงปนไม่ได้
- **ready prompt ชนะ auth marker เสมอ + ต้องเจอติดกัน 5 poll ถึงจะสรุป** — กันเคสที่คำว่า auth โผล่ในผลลัพธ์งานปกติ (เช่น log ของ API 401) แล้วถูกตัดสินว่า pane พัง · ด้วยเหตุผลเดียวกัน generic marker ถูกตัดจาก 12 เหลือ 4 ตัว ที่ตัดทิ้งคือคำที่ชนกับ output ของงาน dev ตรงๆ โดยเฉพาะ `not authenticated` ซึ่งเป็นข้อความ 401 default ของ FastAPI
- **no-content watchdog** — pane ที่ spawn แล้วไม่เคยมีเนื้อหาขึ้นจอเลยภายใน 75 วินาที (`TAKKUB_NO_CONTENT_WATCHDOG_SEC`) จะถูกกู้อัตโนมัติ **สูงสุด 2 ครั้ง** แล้วหยุด ไม่วน respawn ไม่รู้จบ · provider ที่ตายซ้ำจะ degrade ผ่าน `provider_override` (ค้างไว้ข้าม auto-respawn แต่ล้างเมื่อ spawn ใหม่จริง)
- **`takkub status` แยก `spawning` / `active` / `ready`** — เดิมทุกอย่างเป็น `active` ก้อนเดียว มองไม่ออกว่า pane เพิ่งเปิดหรือรอ input อยู่ (state อื่นไม่ถูกแตะ และตัวเทียบ `== "working"` ที่มีอยู่ยังทำงานเหมือนเดิม)

**`takkub wait` รอไม่จบ (#249)**
- **role ที่ไม่เคยถูก spawn ทำให้ `wait` ค้างจนหมด timeout** — เดิมคืน `pending` ตลอดเพราะหา pane ไม่เจอ ตอนนี้มี grace 15 วินาที แล้วสรุปเป็น verdict ใหม่ `gone` · pane ที่อยู่ในสถานะจบแล้ว (`empty`/`done`/`exited`/`error`) ก็ตัดสินทันทีไม่ต้องรอ + มี `cancel_wait()`
- **race 1: `wait` ตอบ `done` ให้กับงานที่ยังไม่เริ่ม** — `_wait_done_events` ไม่เคยถูกล้าง และ pane เพิ่งพลิกเป็น `working` ตอน **delivery** ไม่ใช่ตอน assign ลำดับ `assign` → `wait` จึงไปเจอ event ของรอบก่อนแล้วตอบว่าเสร็จแล้ว ตอนนี้ event ต้องมี `ts >= assign_ts` ของรอบปัจจุบันเท่านั้นถึงจะนับ
- **race 2: waiter ตัวที่ attach ได้ `err: wait session no longer active` แทนผลลัพธ์** — เจอจากของจริงตอนรันคืนนั้นเอง ตอนนี้ผลที่ resolve แล้วถูก echo ค้างไว้ 30 วินาทีให้ตัวที่มาทีหลังอ่านได้

**context ตอน spawn (#250)**
- **ทุก role ได้ guard block เหมือนกันหมดทั้งที่ใช้ไม่ได้** — dev-server hygiene กับ stale-file guard ถูกยัดเข้าไฟล์ role ของ `reviewer`/`gemini`/`codex`/`opencode`/`kimi`/`cursor`/`docs`/`analyst`/`security` ด้วย ทั้งที่ role พวกนี้ไม่ยกเซิร์ฟเวอร์และบางตัวไม่แก้ไฟล์เลย ตอนนี้ inject ตาม capability ผ่าน `role_needs_dev_server_guard()` / `role_needs_stale_file_guard()` · บล็อกที่เหมือนกันทุก role รวม 4,353 ตัวอักษร = 20-27% ของไฟล์ role ที่ staged

### Notes
- QA gate ผ่านแบบ **GO**: pytest 6527 passed / 7 skipped / 0 failed (670s), ruff check + format, `lint-imports` 25/25 contracts, depgraph fresh — รายงานเต็มที่ `docs/qa/gate-248-247-249-250.md`
- ต้อง **restart cockpit** ถึงจะได้ของทั้งหมดนี้
- detector ทั้งหมดในรอบนี้ทำงานกับทุก provider ผ่าน `provider_spec` (`auth_error_markers` / `auth_transient_markers` ต่อ provider) — ไม่มี claude-only shortcut เพิ่มใหม่

## [1.0.62] - 2026-08-15

รอบ **"cockpit หยุดโกหก Lead"** — ปิดครบ #225–#245 (15 ใบ) เน้นสามเรื่อง: สถานะที่รายงานต้องตรงกับความจริง, Lead ต้องมีเครื่องมือรอที่ใช้ได้จริง, และ pane ต้องไม่ค้างรอคนกดโดยไม่มีใครรู้

### Fixed (แก้)

**สถานะที่รายงานไม่ตรงความจริง**
- **`takkub status` บอก "working, progress 0s ago" ทั้งที่ pane ค้างรอคนกด permission มา 3 ชม.** (#236) — นาฬิกา progress นับ spinner frame กับการ redraw ของ dialog เป็น "ความคืบหน้า" และไม่มี detector สำหรับ approval dialog ของ Claude Code เอง (`1. Yes / 2. Yes, and don't ask again / 3. No` ไม่มี `[y/N]` เลยไม่ match pattern ไหน) ตอนนี้ progress อ่านจากสัญญาณที่กรอง spinner ออกแล้ว และ `takkub status` ขึ้นบรรทัด `⛔ blocked:permission-prompt` แยกจาก state ปกติ · ⚠️ detector ยืนยันเฉพาะ Claude Code — provider อื่นยังเป็น known gap (#103)
- **pane หายจาก `takkub list` ก่อน report ถูกส่ง** (#225) — ฝั่ง desktop แก้ไปแล้วตั้งแต่ #163 แต่ `/api/activity` ของมือถืออ่าน pane ตรงๆ ไม่ผ่านทางเดียวกัน มือถือจึงยังเห็น pane หายก่อนเวลา ตอนนี้ไปทางเดียวกันแล้ว
- **delivery บอก "task ยังไม่ถึงมือ" ขัดกับ status ที่บอกว่า working** (#235) — assign ซ้ำสำหรับ role เดิมทำให้เกิด poll loop คู่ขนาน ตัวเก่าที่ถูกแทนที่แล้วยังยิงคำเตือนของตัวเองเข้า Lead ตอนนี้ loop ที่ถูก supersede จะเงียบทันที
- **worktree notice ประกาศ "N commit พร้อม merge" ทั้งที่งานจริงยังไม่ commit** (#244) — เกือบทำให้ merge ของเก่าแล้วปิด issue ทั้งที่ fix ยังไม่เข้า 2 ครั้งในคืนเดียว ตอนนี้เช็ค `git status --porcelain` + `merge-tree` เทียบ base ปัจจุบันก่อน ถ้าสกปรกจะขึ้น `⚠ ยังมี N ไฟล์ที่ยังไม่ commit` และไม่แสดงคำสั่ง merge ให้กดตาม
- **เลข issue ใน notice มาจากที่ agent พิมพ์เอง ซึ่งพิมพ์ผิดได้** (#244) — เคสจริง: report พาดหัวว่า "#234" ทั้งที่แก้ #229 ตอนนี้ `[ref #N]` คำนวณจาก assign spec ต้นฉบับที่ Lead ส่งไปเอง
- **done report ถูกส่งไปโผล่ผิด pane หลัง respawn** (#228) — queue เก็บ notice เป็น string เปล่าไม่มี pane identity ผูกไว้ ตอนนี้ผูก pane token กับทุก tier แล้ว re-check ตอนส่งจริง mismatch จะขึ้น banner ⚠ unverified origin แทนที่จะส่งเงียบๆ

**Lead ไม่มีเครื่องมือที่ควรมี**
- **`takkub wait [--role R] [--timeout S]`** (#242) — ของใหม่ บล็อกจนกว่า done/FAILED report **ถึง Lead จริง** ไม่ใช่แค่ pane หายจาก list · timeout บังคับเสมอ + ตอน timeout บอกเหตุผลรายตัว (working / stalled / ติด tty prompt / report ค้างคิว / ไม่เคย spawn) · waiter ตัวที่ 2 จะ attach เข้าตัวเดิม ไม่มีทางกองซ้อน (เคสจริง: loop เขียนมือค้างสะสม 6 ตัวพร้อมกัน)
- **`takkub inbox [--role R]`** (#231) — อ่านเนื้อ report ที่ค้างคิวได้จริง ไม่ใช่เห็นแค่ป้าย "queued"
- **`takkub progress "<msg>"`** (#234) — รายงานความคืบหน้ากลางงานโดยไม่ปิด pane (เดิมมีแต่ `done` ซึ่งจบ pane ทิ้ง) + `close()` เตือน Lead ก่อนฆ่า subprocess ที่ยังทำงานอยู่
- **digest เป็นตาราง fact ที่ cockpit คำนวณเอง** (#245) — verdict · ref · branch · commit ที่นำหน้า base · ไฟล์ที่ยังไม่ commit · merge สะอาดไหม · ไฟล์ที่แตะ · path ไฟล์เต็ม · คำนวณไม่ได้จะบอก "ตรวจไม่ได้" ไม่ใช่ `0` ที่หลอกตา
- **digest ส่งซ้ำ/เวลาเพี้ยน/ตัดกลางประโยค** (#241) — dedup กับ tier อื่นแล้ว, ใช้เวลาที่ report เกิดจริง, ตัดที่ขอบคำ

**pane ค้างโดยไม่มีใครรู้**
- **teammate เดินไปชนคำสั่งที่ติด permission gate แล้วค้างรอคนกด** (#243) — โหมดปล่อยรันข้ามคืนจะหยุดทั้ง wave โดยไม่มีสัญญาณ ตอนนี้อ่าน `.claude/settings.json` → `permissions.ask` **สดตอน spawn** แล้วฉีดรายการคำสั่งที่ติด gate + ทางเลือกที่ไม่ติด gate + คำสั่งให้รายงาน FAILED แทนการรอ เข้าไปในทุก pane (ไม่ hardcode ลงไฟล์ role เพราะจะ drift) · provider อื่นได้หมายเหตุระบุ gap แทน (#103)
- **assign ค้างรอ response ไม่มีขอบเขต + resend งานที่ทำเสร็จไปแล้วตอน restart** (#233, #230)

**ประสิทธิภาพ / ความถูกต้อง**
- **UI ค้างเป็นจังหวะ 1.5-1.7 วินาที** (#229) — `notify._resync()` ยิง `pathlib.glob` แบบ recursive stat ของทุก project **ทุก tick 200ms** บน Qt main thread · วัดจริง 20 project/50 tick: claude 1000 calls/1229ms → **0 calls/4.71ms** · provider ที่ไม่มี session uuid (gemini/codex) ยัง re-resolve ต่อแบบ throttle 5s ไม่ใช่หยุดถาวร (ไม่งั้น Lead ที่เป็น gemini จะ tail ไฟล์แรกค้างตลอด)
- **cockpit issue ไม่เคยขึ้น GitHub เลยสักใบ** (#237) — `REPO_ROOT` ชี้ไปโฟลเดอร์ติดตั้ง wheel (`venv\Lib`) ที่ไม่มี `.git` แล้ว fallback ลง local store เงียบ 100% กระทบทุกคนที่ติดตั้งแบบปกติ · ตอนนี้ไล่หา git checkout จริง (dev checkout → `AGENT_TAKKUB_COCKPIT_REPO` → `projects.json`) หาไม่เจอจะเตือนเสียงดังพร้อมวิธีแก้ และติดป้าย `(LOCAL ONLY — did not reach GitHub)`
- **resource classifier ตัดสินผิดเพราะอ่านประโยคห้ามเป็นคำสั่ง** (#240) — task spec ที่เขียนว่า "ห้ามรัน pip install" ถูกจัดเป็นงาน package-install แล้วโดนคิวจำกัดจนงานคู่ขนานกลายเป็นเรียงทีละตัว ตอนนี้ดู negation cue รายบรรทัด + Lead เห็นได้ว่าติดคิวเพราะอะไร (#232)
- **ลบ worktree ไม่สำเร็จบน Windows path ยาว** (#226, #227) — long-path-safe delete + ไม่รายงานว่า "เก็บไว้" ทั้งที่ลบไปครึ่งทางแล้ว
- **guard test มองไม่เห็น subprocess ที่ import แบบ alias** (#238) + **CI แดงจาก hardcode path** (#239)

### Notes
- ต้อง **restart cockpit** ถึงจะได้ของทั้งหมดนี้ — โค้ดใหม่ไม่มีผลกับ instance ที่รันค้างอยู่

## [1.0.61] - 2026-08-15

รอบเก็บบั๊กใหญ่ของ **remote/PWA บนมือถือ** + เสถียรภาพ cockpit ตามที่ user รายงานหลังปล่อย 1.0.60 (ปิดครบ #193–#206)

### Fixed (แก้)

**Remote / PWA บนมือถือ**
- **dev กับ prod cockpit แย่ง config remote กันจนต่อไม่ติด** (#193) — ทั้งสอง instance เขียน/อ่าน `remote.json` ตัวเดียวกันและชน port 9999 ทำให้ token/secret path ของอีกฝั่งถูกทับ มือถือจึงต่อไปเจอ instance ผิดตัวหรือไม่ติดเลย ตอนนี้แยก config ตาม cockpit จริง (dev/prod คนละไฟล์คนละ port) พร้อมโชว์ port/hostname ที่ใช้อยู่ให้เห็นชัด
- **ปิด cockpit แล้ว tunnel ยังไม่ดับ** (#197) — cloudflared ค้างเป็น orphan ทำให้ URL เดิมยังตอบอยู่ทั้งที่แอปปิดไปแล้ว และรอบถัดไป spawn ทับกันจนสับสน ตอนนี้ปิดโปรแกรม = เก็บ tunnel ให้ตายตามเสมอ พร้อม reap ซากที่ค้างจากรอบก่อน
- **ต้องกรอกรหัสใหม่ทุกครั้ง** (#196) — session ของรหัสผ่านเก็บใน memory อย่างเดียว restart cockpit ทีเดียวหลุดหมด ตอนนี้ persist session (เก็บเฉพาะ hash ไม่เก็บรหัสจริง) มือถือจึงจำการล็อกอินข้ามการ restart ได้
- **หน้าแชทเด้งลงล่างสุดตลอดเวลาไล่อ่านโค้ด** (#198) — `showThinking()` บังคับ scroll ทุกครั้งที่มีข้อมูลใหม่ ตอนนี้ pin เฉพาะตอนที่อยู่ใกล้ล่างสุดจริง (ระยะ 48px) ถ้ากำลังอ่านย้อนอยู่จะไม่ถูกดึงลง และมีปุ่ม "ข้อความใหม่" ให้กดลงเองเมื่อพร้อม
- **หน้า Pulse ไม่บอกว่ามี pane ไหนเปิด/ทำงานอยู่** (#200) — แยก `PULSE_SHOW_TEAM` (ฝั่ง pull) ออกจาก `LEAD_ONLY_STREAM` (ฝั่ง push) หน้า Pulse จึงเห็นทุก pane ที่เปิดอยู่พร้อมสถานะ working/idle และ runtime รวม teammate ที่ idle ด้วย (provider-neutral ไม่ผูก claude)
- **หน้า usage ค้างไม่อัปเดต** (#203) — provider store อ่าน `~/.claude` ซึ่งเป็น snapshot เก่าค้างมา 23 วัน แทนที่จะอ่าน config dir ของ cockpit ที่รันอยู่จริง ตอนนี้ resolve ผ่าน active project + มี staleness badge บอกเมื่อข้อมูลเก่าเกิน 24 ชม.
- **usage แสดงแค่หน้าต่างเดียว** (#204) — ตอนนี้โชว์ครบทั้ง **5 ชม.** และ **7 วัน** เป็นคนละแถว แต่ละแถวมี % และเวลานับถอยหลังจน reset ของตัวเอง · window ที่ไม่มีข้อมูลแสดง `—` (ไม่ใช่ `0%`) · codex แสดง primary/secondary แยกกัน

**เสถียรภาพ cockpit**
- **main thread ค้าง 0.9–2.5 วินาทีซ้ำๆ** (#194) — อ่านไฟล์/สถานะแบบ blocking บน GUI thread ทำให้หน้าจอสะดุดเป็นจังหวะ ย้ายออกจาก main thread แล้ว
- **`resource_gate_block` ยิงซ้ำทุกวินาที** (#195) — heavy_project_limit log ท่วม `events.log` 1 Hz ตอนนี้ throttle แล้ว
- **backoff กินสล็อตที่ว่างแล้ว** (#201) — ปล่อย slot ไปแล้วแต่ task ที่รออยู่ยังติด backoff schedule เดิม (1/2/5/15 วิ) จึงไม่ยอมเข้าทันที แก้ด้วย capacity-epoch invalidation: พอมี capacity เพิ่ม ให้ล้าง backoff ที่ค้างอยู่ทิ้ง
- **tripwire "Open-With dialog" ตรวจจับตัวเอง** (#199) — pane ที่พิมพ์ชื่อ dialog ลงใน output ทำให้ tripwire เข้าใจผิดว่าเจอ dialog จริง ตอนนี้กัน self-match แล้ว
- **worktree pane ไป repoint editable install ของ repo หลัก** (#202) — pane ใน worktree รัน `pip install -e .` ทับ `.pth` ของ venv ที่ใช้ร่วมกัน ทำให้ instance อื่นชี้ไป worktree ที่กำลังจะถูกลบ แก้สองชั้น: `pane_guard` บล็อก `pip install -e` ทุก role + role file เขียนกำกับ และตัวซ่อม `.pth` ที่ค้างใช้ interpreter ของ venv repo เองเสมอ (ไม่ใช่ `sys.executable`)
- **`UnicodeDecodeError` ฆ่า reader thread เงียบๆ บน Windows locale ไทย** (#205) — `subprocess` text mode ไม่ระบุ `encoding=` จึงตกไปใช้ cp874 ของเครื่อง เจอ byte ที่ decode ไม่ได้แล้ว thread ตายทิ้ง output หายเงียบ (ไม่ raise ให้ caller จับได้) แก้ครบ 23 จุด (`encoding="utf-8", errors="replace"`) + guard test กันย้อนกลับ
- **guard test ของ #205 หลวมเกินไป** (#206) — เดิมเช็คแค่ว่ามี `encoding=`/`errors=` ไม่ได้เช็คค่า ทำให้ `errors="strict"` ผ่านฉลุยทั้งที่เป็นเคสที่ห้ามใช้ ตอนนี้บังคับค่าตรงตัว (`"utf-8"` / `"replace"`) · พร้อมแก้ต้นเหตุจริงของเทสต์ socket ที่ flaky: `/api/lead/upload` ตอบ 403 โดยไม่อ่าน body ทิ้งก่อนปิด socket ทำให้ client ฝั่ง Windows โดน RST เป็น `WinError 10053` — เพิ่ม `_drain_request_body()` แก้ที่โค้ดจริง ไม่ใช่ skip เทสต์

### Docs (เอกสาร)
- gate report 3 รอบ: `docs/qa/2026-08-14-gate-remote-perf-wave.md` (#193–#201, รอบแรก NO-GO แล้วแก้จน GO), `docs/qa/2026-08-14-gate-wave2-202-203-204.md`, `docs/qa/2026-08-14-gate-wave3-205.md`
- audit เชิงลึกรายเรื่องใน `docs/audit/2026-08-14-*.md`

ยืนยันด้วย `pytest` เต็มชุด (6189 passed, 7 skipped) ที่ gate สุดท้าย + targeted 213 tests หลัง merge #206

## [1.0.60] - 2026-08-14

### Added (เพิ่ม)
- **Performance & Reliability v2** — ชุดปรับความเสถียร/ประสิทธิภาพรอบใหญ่ของ cockpit เอง:
  - **task-delivery guard รอบใหม่** — ผูก identity/session ของ task ให้แน่นขึ้นพร้อม TTL + single-flight กันส่งงานซ้ำซ้อนหรือส่งผิด pane
  - **resource governor** — รับงานใหม่โดยดู CPU/RAM ของเครื่องจริงก่อน (admission control) พร้อมคิวรอที่ยุติธรรม ไม่ปล่อยให้ pane ใหม่แย่งทรัพยากรจน pane เดิมสะดุด
  - **PTY writer คิวเขียนแบบมี priority + batch การ render terminal** — ลดอาการหน้าจอกระตุกเวลามีหลาย pane เขียน output พร้อมกัน
  - **Windows Job Object** — ผูก process ownership ของแต่ละ pane เข้ากับ job object จริง ปิด pane หรือปิดแอปแล้ว process ลูกที่ค้างถูก kill ตามไปด้วยเสมอ ไม่ทิ้งซาก
  - **Performance Settings** — โปรไฟล์ให้เลือก Safe / Balanced / Maximum ปรับพฤติกรรม resource governor ตามเครื่อง
  - **Health UI chip** แบบ live แสดงสถานะ resource governor ให้เห็นสดๆ
  - **completion-notice dedupe** ที่ทนต่อการ restart/crash (durable) กันแจ้งเตือนงานเสร็จซ้ำ
  - **Token Meter รู้จัก Lead จริง** — resolve provider จาก pane Lead ที่ active จริงตอนนั้น แทนที่จะเดาจาก config เฉยๆ
  - เอกสารประกอบอยู่ที่ `docs/performance-reliability.md` (คู่มือใช้งานจริง), `docs/performance-reliability-v2-implementation-report.md`, `docs/performance-reliability-v2-traceability.md`, `docs/performance-reliability-v2-adversarial-audit.md`

### Fixed (แก้)
- **stats chip ของ Performance Health ไปนั่งอยู่บน header กลางจอ** — ย้ายมาไว้มุมของแต่ละ tab เคียงข้าง Token Meter แทน (container เดียวกัน mount/remount/teardown พร้อมกันตอนสลับ/ปิด tab) ไม่ปะปนกับ project อื่นที่เปิดอยู่คนละ tab
- **Token Meter โหมดย่อของ Claude อ่านค่าเปอร์เซ็นต์ผิดหน้าต่าง** — เดิมเลือกโชว์ตัวไหนก็ได้ระหว่าง 5 ชั่วโมงกับ 7 วันแล้วแต่ว่าตัวไหนเปอร์เซ็นต์สูงกว่า ทำให้บางทีโชว์ 7 วันทั้งที่ user อยากรู้ short window ตอนนี้ fix ให้อ่านเฉพาะหน้าต่าง 5 ชั่วโมงเสมอ (ทั้งเปอร์เซ็นต์และเวลานับถอยหลังจน reset)

ยืนยันด้วย `pytest` เต็มชุด (5820 passed, 7 skipped, 0 failed) และ `takkub doctor --live` เขียวทั้งหมด

## [1.0.59] - 2026-08-14

### Fixed (แก้)
- **proactive idle compact ยิง `/compact` ซ้ำทุก ~27 นาทีบน pane ที่ idle จริง** — compaction ที่ watchdog ยิงเองทำให้ pane หลุดออกจาก ready prompt ชั่วคราว ซึ่งถูกตีความว่า "มีงานใหม่เข้ามา" แล้วรีเซ็ตตัวจับเวลา idle พอ compact เสร็จกลับมา ready จึงเริ่มนับ idle episode ใหม่ทั้งที่ transcript ไม่มีอะไรเพิ่มเลย วนซ้ำแบบนี้ไม่รู้จบ (หลักฐาน `runtime/events.log` ห่างกันพอดี ~27 นาทีซ้ำหลายรอบ) — กลับหัวเจตนาของ #161 เดิม (ตั้งใจจะประหยัด token ตอน resume แต่กลายเป็นเผาเงินซ้ำด้วยการ compact เปล่าๆ ทุกครั้ง) ตอนนี้ทำให้ busy stretch ของการ compact ตัวเองโปร่งใสต่อ idle episode เดิม พร้อม ceiling กันค้างกรณีงานจริงแทรกเข้ามาก่อน (#190)
- **`auto_issue_capture` ยิง GitHub issue ขึ้น repo สาธารณะจาก process ที่ไม่ใช่ cockpit จริงได้** — `app.py` ติดตั้ง exception hook ทันทีที่ import module ซึ่งครอบคลุมทุก process ที่ import `agent_takkub` รวมถึง `pytest` ด้วย เกิดขึ้นจริงแล้วครั้งหนึ่ง (background pytest โดน kill ระหว่าง flush stdout แล้วโดน route เข้า capture) เพิ่ม guard ปิดการยิง issue อัตโนมัติเมื่อรันอยู่ใน pytest/CI (#188)
- **`takkub worktree clean --force` ลบ branch ของ pane ที่ยังมีชีวิต + ลบ branch ทั้งที่ลบ worktree ไม่สำเร็จ (ไม่ atomic)** — เกิดขึ้นจริงแล้วครั้งหนึ่ง: pane ที่เพิ่ง spawn ยังถือ worktree อยู่ Windows file lock ทำให้ `git worktree remove` ล้มเหลว แต่ `branch -D` รันต่อไปอีก 2 บรรทัดถัดมาโดยไม่เช็คผลลัพธ์ก่อน branch จึงหายไปทั้งที่ worktree ยังลบไม่สำเร็จ ตอนนี้ `branch -D` รันเฉพาะตอน remove สำเร็จเท่านั้น (ตรงกับ pattern ที่ `safe_remove()`/`merge_isolated()` ใช้อยู่แล้ว) และเพิ่ม live-pane guard: worktree ที่ pane ที่ยังมีชีวิตถืออยู่จะถูกข้ามเสมอไม่ว่าจะใส่ `--force` หรือไม่ ไม่มี bypass flag ใดๆ (#187)
- **gemini(agy) trust modal ทำให้ task หายเงียบ** — สาเหตุจริง (ไม่ใช่สมมติฐานเดิมเรื่อง worktree path) คือถ้อยคำ: agy เขียนปุ่มยืนยันว่า `enter Confirm` โดยไม่มีคำว่า `to` แต่ตัวตรวจจับเดิม match แบบ exact substring `"enter to confirm"` เป๊ะๆ จึงไม่เคย fire ให้ agy เลย `_auto_trust` เลยไม่เคยกด Enter — แก้ด้วย regex ที่ยอมรับได้ทั้ง 2 แบบ พร้อมเพิ่ม state ใหม่ `delivery-blocked-prompt` ที่เตือน Lead ทันทีตั้งแต่ poll แรกที่เจอ แทนที่จะเงียบไปได้นานถึง 30 นาทีเหมือนเดิม (ถูกกลืนเข้า busy-wait ปกติ) และเลิก blind-paste งานลงบน modal ที่ยังค้างอยู่ (#186)
- **`takkub list` โชว์ชื่อ project เป็น basename ของ worktree** — pane ที่ spawn เข้า git worktree เห็นหัวข้อเป็นชื่อ worktree เอง (เช่น `dev · frontend-1786615682`) แทนที่จะเป็นชื่อ project จริง (`dev · agent-takkub`) ทำให้ Lead เข้าใจผิดว่าคุมได้แค่บางส่วนของทีม ตอนนี้ `instance_identity_label()` ใช้ `TAKKUB_PROJECT` (env ที่ orchestrator stamp ให้ทุก pane) ก่อนเสมอ เหลือ fallback ไปที่ basename เฉพาะ terminal ที่เรียกเองนอกระบบเท่านั้น (#185)
- **มือถือเปิดแชท Lead แล้วเจอจอว่างเงียบๆ ไม่บอกสาเหตุ** — ตอนนี้แยกแยะได้ 3 กรณีและโชว์เหตุผลเป็นข้อความแทนคำว่า "ยังไม่มีข้อความ" เฉยๆ: provider ไม่รองรับ remote history (opencode/kimi/cursor), Lead ยังไม่มี session_uuid บันทึกไว้, และมี session_uuid แต่หา transcript ไม่เจอ (เช่น session drift จากการ `/resume` บนเดสก์ท็อปเอง) — ครอบคลุมทุก provider ด้วย classifier ตัวเดียวกันที่ `takkub doctor --live` ใช้อยู่แล้ว ไม่มี shortcut เฉพาะ claude และไม่ได้กลับไปเดา transcript ล่าสุดแบบเดิม (ตั้งใจตัดออกไปแล้วเพราะเคยทำให้ resume ผิด session) พร้อมโชว์ version ของ cockpit บนหน้าจอ usage ของมือถือ เพื่อให้เช็คได้เองว่าติด build เก่าอยู่หรือเปล่า (#192)
- **#177 ปิดแล้ว** — เดิมสงสัยว่า Playwright MCP ต่อไม่ติดบน pane ที่ spawn ผ่าน `qa --plan --shards` เพราะ policy deny-by-default บน role ที่มี suffix ตัด hypothesis นั้นทิ้งได้ขาด (ทุก call site ส่ง base role เสมอ เป็นไปไม่ได้ทาง architecture ที่จะโดน deny) ยืนยันด้วย live repro จริง 2 รอบ — `--shards 2` (2/2 connect) และ `--shards 3` (3/3 connect) รวม 5 shard pane ต่อ Playwright MCP ได้ครบทุกตัว เปิดหน้าเว็บอ่าน title สำเร็จ ไม่มี error เลยสักตัว

### Changed (เปลี่ยน)
- **architecture guardrail (`lint-imports` + depgraph freshness) บังคับใน CI ทั้ง 3 OS แล้ว** — เดิมมีแค่ pre-commit hook ฝั่ง local เท่านั้นที่เช็ค ทำให้ commit ที่ใช้ `--no-verify` หรือ merge ผ่านหน้าเว็บ GitHub ตรงๆ ลอดผ่านไปได้โดยไม่มีใครเช็ค architecture boundary เลย พ่วงแก้ pre-commit hook ทั้ง 2 ตัวที่ hardcode path แบบ Windows-only ให้ทำงานบน macOS/Linux venv ได้ด้วย
- **depgraph freshness เปลี่ยนเป็นโหมด `--check` เทียบใน memory** — เดิม regenerate ไฟล์จริงแล้ว `git diff --exit-code` ซึ่งจะแดงพร้อมกันทั้ง 3 OS ทันทีที่ `grimp` ออกเวอร์ชันใหม่ (provenance field เปลี่ยนแม้ graph จริงไม่เปลี่ยน) และมี CRLF flicker กวนใจ qa ตอนนี้ diff เทียบเฉพาะ field ที่มีความหมายจริง ไม่แตะไฟล์บน disk เลย พร้อม pin `grimp>=3.14,<4` กันเวอร์ชันใหม่เปลี่ยน semantics แบบเงียบๆ อีก
- **security hardening 3 จุด** — `alt` attribute ของรูปที่แปะผ่าน markdown ไม่ escape มาก่อน ทำให้ `"` หลุดออกไปแทรก attribute เพิ่มใน `<img>` ได้ (defense-in-depth เพราะ `style-src` ยัง allow unsafe-inline อยู่), caption จากรูปที่อัปโหลดผ่าน remote ไม่กรอง control byte ทำให้ `\x03`/ANSI escape หลุดเข้าไปถึง PTY ของ Lead ได้ผ่าน `lead_say`, และไฟล์ manifest (`package.json`, `pyproject.toml`, lockfile ฯลฯ) ใน staging mirror ของ autoskills เคย hardlink ไปด้วย ทำให้แก้ไฟล์ staging เผลอไปเขียนทับไฟล์จริงของโปรเจคได้ — ทั้ง 3 จุดแก้แล้วพร้อม regression test
- **dead-code sweep 8 รายการ ~230 LOC** — รวม chip helper 4 ตัวที่กำพร้าตั้งแต่ toggle ถูกถอดไปเมื่อ 2026-08-13, compat shim/DTO ที่ไม่มีใครเรียกอีก 3 ตัว และ**โมดูล `claude_auth_dialog.py` ทั้งไฟล์** (ถูกแทนที่ด้วย Settings → Users tab ไปแล้ว ไม่มีใครเรียกที่ไหนเลย) ยืนยันทุกรายการด้วย grep ทั้ง repo ใหม่ก่อนลบ
- **README** — ตรวจทุกคำสั่ง/flag/ลิงก์เทียบกับ `cli.py` จริง ไม่พบ command หรือ flag ที่ตายแล้ว แก้ 1 จุดที่พลาด: ภาพหน้าจอ cockpit เคย pin อยู่ที่ tag `v1.0.5` เก่า (ปัจจุบันคือ 1.0.58) เปลี่ยนให้ชี้ `main` ให้ตรงกับภาพอื่นที่ใช้ `main` อยู่แล้ว

## [1.0.58] - 2026-08-13

### Added (เพิ่ม)
- **usage/limit ของทุก provider** — `provider_usage.py` เป็น abstraction กลาง + endpoint `GET /api/usage` + แถบบนหัวจอเดสก์ท็อป + การ์ดบน PWA มือถือ · claude/codex/gemini ดึงได้จริง (codex ยืนยันด้วยการยิง JSON-RPC สด) · opencode แยกเป็น `spend` คนละ field เพราะเป็นยอดที่มันนับเอง **ไม่ใช่โควต้า** · kimi/cursor แสดงว่าไม่รองรับตรงๆ · **ไม่มีสถานะไหนถูกแสดงเป็น 0%** ค่าที่ไม่มีคือ "—" เสมอ · remote อ่านจาก cache ที่เดสก์ท็อปดึงมาแล้วเท่านั้น ไม่ยิง provider เอง เพื่อไม่ให้มือถือกลายเป็นตัวเร่ง rate limit
- **autoskills bridge** — ปุ่ม "ดึง skill ตาม stack" ในหน้า Skill Catalog: สแกน stack ด้วย `autoskills --dry-run` → ให้ผู้ใช้ติ๊กเลือกเอง → ติดตั้งเฉพาะที่เลือก ทำงานบน worker thread ไม่บล็อก UI · ติดตั้งใน staging mirror ก่อน **skill ที่ไม่ได้เลือกไม่แตะดิสก์จริงเลย** · ตรวจจับ+กู้คืน skill เดิมที่ถูกเขียนทับ · ตรวจ symlink หลบหนีแบบ nested · **แสดงธงเตือนของ CLI เอง** (เช่น `(security check ⚠)`) และ skill ที่ติดธงจะไม่ถูกติ๊กไว้ล่วงหน้า
- **หน้า New Role ใหม่** — จัดเป็น 5 การ์ด (Identity/Placement/Tools/Skills/Instructions) + ช่องค้นหา skill + ป้ายบอกที่มา + hint ตำแหน่งบนกริด + ปุ่ม "เริ่มจากเทมเพลต" ที่ดึง `analyst`/`designer`/`docs`/`security` มาใช้ได้จริง (4 ไฟล์นี้เคยมีแต่ doc ไม่เคยถูกใช้)
- **knowledge base ต่อโปรเจค** — กลั่นข้อสรุปที่ใช้ซ้ำได้จาก done note ลง `runtime/knowledge/<project>.md` มีเพดาน FIFO (12KB/150 entry) ปิดได้ด้วย `TAKKUB_KNOWLEDGE_BASE=0` (#168)
- **`takkub task reconcile` / `task close`** — ล้าง ledger row ที่ค้างได้เอง พร้อม dry-run
- **`takkub doctor --live` หมวด `[remote-mirror]`** — วินิจฉัยทีละชั้นว่าทำไมมือถือไม่ขึ้นคำตอบ (provider / scanner / session_uuid / ไฟล์ transcript)

### Fixed (แก้)
- **หน้า New Role ล้นแนวนอนจนกรอกไม่ครบ** — `QCheckBox` ที่ยัด description ยาวลงไปโดยไม่ wrap ดันความกว้างฟอร์มไป 2405px ทำให้ช่อง **Label** และสวิตช์ **MCP+Plugins** ถูกดันหลุดจอ ผู้ใช้จึงเห็นหน้าที่ใช้งานไม่ได้จริง (วัดหลังแก้เหลือ 554px) · ตัวหนังสือซ้อนกันเพราะ `deleteLater()` ลบแบบ deferred แล้วชน reload รอบสองที่ยังไม่ทันถึง event loop
- **แถบ usage บนเดสก์ท็อปเคยแสดงตัวเลขที่กุขึ้นมา** สำหรับ codex/gemini/opencode/kimi/cursor — ลบทิ้งแล้วอ่านจาก store จริง พร้อมเทสกันของปลอมกลับมา
- **มือถือค้างสปินเนอร์เงียบๆ เมื่อ Lead ไม่ใช่ claude** — คำสั่งส่งถึงและ Lead ตอบแล้ว แต่ mirror อ่าน transcript ของ claude อย่างเดียว ตอนนี้ตอบกลับพร้อมเหตุผลทันที และติดธง gap ของ opencode/kimi/cursor ไว้ที่ `provider_spec.py` (#103)
- **ledger row ค้าง `working` ถาวร** เมื่อ cockpit ปิดทั้ง process → reconcile ตอน startup โดยไม่แตะ row ของ pane ที่ยังมีชีวิต (#166)
- **`takkub send --to <role>` ตอบ `unknown role` ทั้งที่ role มีจริง** แค่ pane ปิดไปแล้ว → แยกข้อความ 2 กรณีพร้อมบอกวิธีแก้ (#164)
- **done report แนบ evidence ของ role อื่น** — shared-dir scan รันให้ทุก role โดยไม่ scope ตอนนี้จำกัดเฉพาะ role ที่ทำงานกับภาพ (#165)
- **pane หายจาก `takkub list` ก่อน done report ถึง Lead** — `list/status` แสดงสถานะ `report queued` ให้เห็นแทนที่จะเงียบ (#163)
- **`takkub issue list` ทิ้ง backlog ในไฟล์ local** ทันทีที่ `gh` ใช้ได้ ทำให้ issue ที่บันทึกไว้หายจากสายตาทั้งหมด — ตอนนี้อ่านรวมทั้ง 2 แหล่งพร้อมเตือน (#174)
- **teammate pane รันคำสั่งฆ่าโปรเซสข้ามเครื่องได้** (`taskkill /IM`, `pkill`, `killall`, `Stop-Process -Name`) ซึ่งฆ่า pane อื่นทั้งเครื่อง → กัน 2 ชั้นทั้ง `pane_guard.py` และ prose ใน role file ครบทุกไฟล์ พร้อมเทสที่จับได้ถ้ามี role ใหม่ลืมใส่ (#169)
- **AttributeError ที่ `pty_session.py`** — race ระหว่าง reader thread กับ teardown thread บน `self._proc` (#179)
- **Playwright MCP ไม่ได้ตั้ง `--output-dir`** ทำให้ screenshot ไปตกใน temp dir ของ MCP (#178)
- **screenshot ที่ถ่ายพลาดไม่ถูกตรวจจับ** — เพิ่มการจับภาพซ้ำด้วย md5 นอกเหนือจากการเช็คขนาด/header เดิม (#182)
- **เทส 3 ตัวแดงเฉพาะบนเครื่อง dev** เพราะไปอ่าน `~/.takkub` จริงของคนรัน — isolate แล้ว

## [1.0.56] - 2026-08-13

### Fixed (แก้)
- **`assign --isolation worktree` ซ้ำที่ role name เดิม (ไม่ใส่ `#N`) ชนกันเงียบๆ** — pane identity ผูกกับ bare role name เฉยๆ ทำให้ call ที่สองไม่ได้ pane อิสระใหม่ แต่ไปทับ/ชน pane เดิม worktree ใหม่ที่สร้างไว้ไม่มีใครใช้จริง ตอนนี้ reject ทันทีทั้งใน `assign()` และ synchronous pre-check ก่อน ack (เดิม assign() reject แต่ return value ไม่เคยถูกส่งกลับ caller เลย) พร้อมบอกให้ใช้ `role#N` แทน (#162)
- **worktree ที่ pane รายงาน done โดยไม่มี commit เลยถูกลบทิ้งอัตโนมัติทันที** — งานสูญหายจริงแม้ไม่มีทางกู้คืน ตอนนี้ไม่ auto-delete อีกต่อไปไม่ว่า dirty หรือ clean แจ้ง Lead ดังๆ แทน cleanup ต้องสั่งเองผ่าน `takkub worktree clean` (#161)
- **role prompt ไม่เตือนต้นทุนโทเคนของรูปภาพ** — รูปถูกชาร์จตาม resolution (vision tiling) ไม่ใช่ byte เหมือนไฟล์ข้อความ เปิดรูปเดิมซ้ำหลายรอบ (mockup review) เคยทำให้ 2 pane ชน usage limit พร้อมกันในเทิร์นเดียว — เพิ่มคำเตือน + threshold ชัดเจน (>300KB หรือด้านยาว >1500px) ในทุก role prompt (#157)

## [1.0.55] - 2026-08-13

### Fixed (แก้)
- **gemini/agy pane spawn เด้ง Windows "Select an app to open 'mb'"** — npm ติดตั้ง mini-browser shim (`mb`) แบบ extensionless คู่กับ `mb.cmd`; Win32 SearchPathW เจอตัว extensionless ก่อนแล้วไม่มี associated app จึงเด้ง dialog — sanitize shim (rename เป็น `mb.sh`) + จัดลำดับ PATH ให้ `mb.cmd` ชนะเสมอ (#156)
- **Named tunnel (remote-control) เงียบ ไม่บอกสาเหตุตอนเชื่อมไม่ติด** — cloudflared เด้งตายทันทีถ้า credentials ผิด/ก็อปมาจากเครื่องอื่น แต่ระบบไม่เคยเช็คว่ารอดจริงไหม เห็น pairing URL เหมือนสำเร็จทั้งที่ tunnel ตายแล้ว — เพิ่ม post-spawn liveness check + capture error message จริงมาโชว์ พร้อมแนะนำสลับ Quick tunnel ได้ทันที; แก้คู่มือ headless-docker ที่สอน copy credentials ข้ามเครื่องแบบไม่ปลอดภัยด้วย
- **auto-resume ยอมแพ้แล้วทิ้งงานค้างเงียบๆ** — pane ชน usage limit ซ้ำจน auto-resume หยุดช่วย ข้อความเดิมบอกแค่ "หยุดแล้ว" ทั้งที่งานอาจเสร็จสมบูรณ์แล้วจริง — ตอนนี้ dump task ค้าง/output ท้าย pane/git-status ให้ดูก่อนตัดสินใจ discard หรือมอบงานใหม่ พร้อมเขียน recovery snapshot ลงดิสก์กันงานหายแม้ pane crash (#158)
- **shard fan-out ของ QA เขียนไฟล์รายงานทับกัน** — หลาย shard ที่ output path เดียวกันจากงาน `--shards N` จะทับกันเอง เหลือแค่ shard สุดท้าย — บังคับ suffix `.shard{N}` ต่อ shard อัตโนมัติ พร้อม post-hoc collision detector เตือนถ้า agent ไม่ทำตาม (#160)
- **evidence screenshot เสีย (ว่าง/เล็กผิดปกติ) หลุดผ่านไปได้โดยไม่มีใครรู้** — role รายงานว่าเก็บหลักฐานครบทั้งที่ไฟล์ภาพบางไฟล์ว่าง/ถ่ายพลาด — เพิ่ม size check + magic-byte sniff ตอน scan evidence, แสดงขนาดไฟล์ + flag ให้เห็นชัด (#159)
- **role list ไม่ตรงกันระหว่างหน้า "Providers & Roles" กับ "Role Overlap"** — `opencode`/`kimi`/`cursor` ไม่เคยมี Role registry entry ทั้งที่ provider config บังคับใช้จริงอยู่แล้ว และ custom role ที่มีแค่ไฟล์ `.md` ไม่มี registry row ก็หลุดจากหน้า Providers & Roles ไปด้วย — เพิ่ม entry ที่ขาด + self-heal orphan doc ตอน boot (#162)

### Added (เพิ่ม)
- **ลด token cost ตอน resume จาก prompt-cache TTL หมดอายุ** — pane ที่ idle นานข้าม TTL boundary (~1 ชม. ปกติ, ร่นเหลือ ~5 นาทีตอน account เข้า usage-overage) ต้องจ่าย cache-write เต็มก้อนของ context ที่สะสมไว้ทั้งหมด — เพิ่ม proactive `/compact` อัตโนมัติเมื่อ pane idle เกิน 25 นาที (Claude เท่านั้นตอนนี้) และ status-bar chip เตือนเมื่อ account เข้า usage-overage (#161)
- **Right-click "Restart Lead" บน project tab** — คลิกขวา tab ไหนก็ได้ (ไม่ต้อง active) แล้ว restart Lead ของ project นั้นได้เลย พร้อม confirm dialog ที่เตือนถ้ามี pane กำลังทำงานอยู่
- **วัดและลด boot-time token cost ต่อ pane spawn จริง** — พบ graft-usage-caveats block ซ้ำแบบ policy-blind ใน 7 ไฟล์ role .md แม้ role ที่ MCP policy ไม่มี graft ก็ยังจ่าย token เต็ม — extract เป็น block เดียว gate ด้วย MCP allowlist จริง ลดได้ -637 token/spawn สำหรับ backend/frontend/mobile พร้อมเพิ่มกฎวินัยโทเคนสั้นๆ (skip redundant reads/no speculative calls/route large output ออกจาก context) ในทุก role prompt

### Changed (เปลี่ยน)
- **บังคับปิด toggle Multi/rtk/Auto-resume** — เอา chip ออกจาก status bar แล้วตั้งเป็นค่าเปิดถาวรทุกเครื่อง (Plan Pro/Max ยังเลือกได้ตามเดิม เพราะ Pro-tier ต้อง pin โมเดล standard-context)

## [1.0.54] - 2026-08-12

### Fixed (แก้)
- **วางข้อความซ้ำ (double-paste) ใน terminal** — browser กับ xterm.js เขียนทับ helper textarea พร้อมกันตอน paste ทำให้ข้อความถูกส่งซ้ำ 2 รอบ (ขอบคุณ contributor `than-aa`, PR #153)
- **ชื่อ project/role ที่มีจุดเดียวใช้ไม่ได้** — เช่น `www.abc.com` ตอนนี้อนุญาตแล้ว ยังกัน path traversal เต็ม (`..`, จุดขึ้นต้น/ลงท้าย) เหมือนเดิม (PR #153)

### Chore
- Bump `ruff` 0.16.1 → 0.16.2 (bugfix, dependabot #155) — ปฏิเสธการขยาย Qt6 pin ที่ dependabot เสนอเพราะ Qt 6.11+ มี crash regression ที่ทราบอยู่แล้ว (`doctor.py::check_qt()`)
- Bump `github/codeql-action` 4.37.4 → 4.37.6 (dependabot #154)

## [1.0.53] - 2026-08-12

### Added (เพิ่ม)
- **Auto-issue-capture สำหรับ cockpit crash** — unhandled exception ในตัว cockpit เอง (sys.excepthook/threading.excepthook/unraisablehook) auto เปิด GitHub issue เข้า repo agent-takkub ให้อัตโนมัติ พร้อม dedup ต่อ signature 24 ชม. + rate-cap 5 อีชชู่/24 ชม. กัน crash-loop สแปม และ scrub home path + redact secret-shaped string ก่อนส่งเพราะ repo เป็น public
- **นโยบาย Lead auto-open cockpit self-bug** — Lead ทุกโปรเจคที่เจอ error ที่เป็นตัว cockpit เอง (ไม่ใช่ bug โปรเจค user) เปิด issue อัตโนมัติได้เลยไม่ต้องรอสั่ง (`CLAUDE.md`)

### Fixed (แก้)
- **Remote/mobile chat เด้งไปบนสุดแทนที่จะโชว์ข้อความล่าสุด** — `renderSelectedProject()` rebuild log ทั้งหมดตอนโหลด history แต่พึ่ง atBottom heuristic ที่ออกแบบมาสำหรับ incremental streaming ล้วนๆ ซึ่ง fail เงียบๆ ถ้า container ยังไม่ visible ตอน render — เพิ่ม force scroll-to-bottom หลัง full-rebuild

## [1.0.52] - 2026-08-11

### Fixed (แก้)
- **ข้อความที่พิมพ์จาก Desktop แสดงบน Remote Mobile ทันที** — live transcript adapters ของ Claude, Codex และ Gemini ส่ง user turn ผ่าน SSE แยกจากคำตอบ Lead พร้อมระบุข้อความที่มาจาก Remote เพื่อกัน optimistic echo ซ้ำ; เพิ่ม `user` และ `session_changed` เข้า SSE allowlist จึงไม่ต้องกด Refresh เพื่อเห็นข้อความหรือ session ที่เปลี่ยน
- **สถานะ “OpenAI กำลังทำงาน…” ไม่ค้างหลัง turn จบ** — JSONL activity edge บันทึกสถานะเดียวกับ pane-state transition ทำให้ idle edge ไม่ถูก dedupe ทิ้ง; history API ส่งสถานะ working จริง และ PWA มี confirmed state พร้อม optimistic timeout 30 วินาทีสำหรับ turn ที่จบเร็วระหว่าง notifier polls
- **Header ของ Remote ไม่หายบน iPhone Safari** — ตรึง body/app shell กับ dynamic viewport, จำกัด scroll ไว้ใน chat log และกำหนดความสูงขั้นต่ำ/z-index ให้ header ป้องกัน Safari restore หรือ pan layout viewport จนหัวหน้าหลุดออกนอกจอ; bump service-worker shell cache เป็น v27

## [1.0.51] - 2026-08-11

### Added (เพิ่ม)
- **Remote Mobile Resume ใช้ session จริงของ provider** — มีรายการ session แยกตามโปรเจกต์พร้อม preview/provider; Codex resume ด้วย UUID และ cwd ที่ตรงกับ transcript ส่วน Gemini/Claude และ provider ที่รองรับใช้ adapter เดียวกัน โดย response ส่ง history ของ session ที่เลือกกลับมาก่อนต่อ SSE จึงไม่เห็นหน้าว่างหลัง resume
- **แนบรูปจากมือถือถึง Lead ได้โดยตรง** — ปุ่ม 📎 เลือกรูปจากกล้องหรือคลังรูป รองรับ PNG/JPEG/WebP/GIF สูงสุด 8 MB; ตรวจ magic bytes, บันทึกด้วยชื่อสุ่มใต้ artifacts กลางของโปรเจกต์ และส่ง absolute path ให้ Lead ทุก provider เปิดดู พร้อม preview แบบ data URL ที่ปลอดภัยบนมือถือ

### Changed (เปลี่ยน)
- **สลับหลายโปรเจกต์แบบทันทีและทำงานพร้อมกัน** — Remote เก็บ history/SSE แยกต่อโปรเจกต์และเปิด stream พร้อมกันสูงสุด 4 โปรเจกต์; การสลับใช้ memory cache โดยไม่ยิง history API หรือ reconnect ใหม่ ขณะที่เครื่อง/แท็บใหม่ยัง bootstrap history สดหนึ่งครั้งเสมอ
- **ซิงก์ข้ามอุปกรณ์โดยไม่ย้อนกลับไปโหลดทุกครั้ง** — เพิ่มปุ่ม Refresh, `Cache-Control: private, no-store` สำหรับ JSON API และรีซิงก์โปรเจกต์ที่เปิดเมื่อมือถือกลับจาก background เกิน 30 วินาที เพื่อเก็บข้อความที่พลาดระหว่าง browser ระงับ SSE

### Fixed (แก้)
- **Remote ไม่ค้างหรือตายหลัง network หลุด** — SSE retry ต่อเนื่องด้วย backoff สูงสุด 15 วินาทีแทนการหยุดถาวรหลังครบจำนวนครั้ง และรักษาสถานะ turn ที่กำลังทำงานระหว่าง reconnect
- **หน้า Remote ไม่มี console error แดงจาก bootstrap/Cloudflare อีก** — password gate ใช้ bootstrap preflight เดียวก่อนเรียก API ที่ถูก gate และ static response ส่ง `no-transform` เพื่อไม่ให้ edge inject analytics script ที่ขัดกับ CSP
- **ข้อความ Lead รองรับ provider ทุกตัว** — parser มี provider-neutral terminal fallback สำหรับ Codex, Gemini, Claude, OpenCode, Kimi, Cursor และ provider ใหม่ โดยยังคง structured adapters เมื่อมีข้อมูล native

## [1.0.50] - 2026-08-10

### Changed (เปลี่ยน)
- **กฎมอบหมายงานของ Lead เป็น provider-neutral** — Claude, Codex, Gemini/agy, OpenCode, Kimi, Cursor, provider ใหม่ และ provider substitution ใช้นโยบายเดียวกัน: งาน source/tests/provider behavior ต้องส่งให้ specialist เสมอ รวมถึงงานใน cockpit เอง; Lead ทำตรงได้เฉพาะ inspection หรือแก้ non-source เล็กมากตามเกณฑ์ที่ระบุไว้ใน `CLAUDE.md` และ context/`AGENTS.md` ที่สร้างตอน spawn
- **สลับ provider/profile แล้ว UI และ runtime ตรงกับความจริง** — เมนู Accounts แยก profile ต่อ provider, normalize ชื่อเก่า `openai` เป็น `codex`, บันทึก provider เริ่มต้นพร้อม Lead override แล้ว restart pane ของโปรเจกต์; status header แสดง effective provider จริงหลัง substitution แทนค่าที่ตั้งไว้แต่ใช้งานไม่ได้

### Fixed (แก้)
- **Gemini history/resume ใช้งานได้ครบเส้นทาง** — หา chat store จาก `.project_root`, อ่าน Gemini JSONL และแสดง remote history/session picker ตาม provider จริง; ตรวจ UUID กับ cwd ใน store ก่อนปิด pane แล้ว resume agy ด้วย `--conversation` ขณะที่ provider ที่ยังไม่มี capability ถูกปฏิเสธอย่างชัดเจนโดยไม่แตะ Lead ที่กำลังรัน

## [1.0.49] - 2026-08-06

รอบนี้มาจากการใช้งานจริง — user เปิด cockpit แล้วเจอ dialog ขึ้นว่า **"Failed: 15 projects"** พร้อม chip แดง `Graft: 15 failed` แล้วเข้าใจว่าระบบพัง · ไล่ดูแล้ว**ไม่มีอะไรพังเลยสักตัว**

### Fixed (แก้)
- **🔴 "ข้าม" ถูกรายงานเป็น "ล้มเหลว"** — `_run_build` คืน `ok=False` สำหรับโฟลเดอร์ที่ไม่ใช่ git repo ทั้งที่ข้อความในโค้ดเขียนเองว่า `— skipped` · เครื่อง user มี **15 โฟลเดอร์ที่ไม่ใช่ git repo** (prod 3) ในรายการโปรเจค → ขึ้น failed ทุกครั้งที่เปิด cockpit ทั้งที่ระบบทำงานถูกต้อง
  - `_run_build` เป็น tri-state (`True` = สร้างแล้ว / `False` = ล้มเหลวจริง / `None` = ไม่เข้าเกณฑ์) · `get_build_status()` เพิ่ม `skipped: list[str]` แยกจาก `failed`
  - **แยก "ไม่มี git ในเครื่อง" (ปัญหาจริง ต้องเตือน) ออกจาก "โฟลเดอร์นี้ไม่ใช่ git repo" (เรื่องปกติ)** — เดิมข้อความเดียวรวม 3 กรณี
  - **ทำไมไม่ใช่แค่เรื่องคำพูด:** ของที่พังจริงจะจมอยู่ในกอง 15 ตัวที่ไม่ใช่ปัญหา · ค้าง 24 ชม. ตาม TTL · และข้อความบอกให้ "ดู log" ซึ่ง**ไม่มีอะไรใน log เลยเพราะมันไม่ได้ fail** → พา user ไปทางตัน
  - chip ไม่ขึ้นสีเตือนถ้าเป็นแค่ skip · ตัวหารใช้เฉพาะที่เข้าเกณฑ์ (ไม่ใช่ `30/46` อีกแล้ว) · dialog แยก section `Skipped` / `Failed` และ**ไม่มีคำว่า Failed ถ้าไม่มีอะไรล้มเหลวจริง**
- **เทสเขียนไฟล์จริงลง `runtime/role-memory/` ของ repo** — `ROLE_MEMORY_DIR` ไม่ได้อยู่ใน `_isolate_runtime` → ขยะค้างจริง 9 โฟลเดอร์ (`proj`, `proj_a`, `spawn-task-test`, `gatetest`, …) · ปิดต้นเหตุที่ conftest + ลบของที่ค้าง (ยืนยันทีละตัวจาก `TEST_PROJECT` constant ใน source ก่อนลบ ไม่ใช่เดา — โฟลเดอร์ที่มีเนื้อหาจริงไม่ถูกแตะ)
- `subprocess.Popen` ของ graft viewer ส่ง `creationflags` ผ่าน `**kwargs` → guard test (AST checker) มองไม่เห็น · แก้ที่โค้ดให้ส่ง literal ตาม convention ของโปรเจค (`creationflags` เป็น 0 บน non-Windows อยู่แล้ว จึงส่งตรง ๆ ได้เสมอ) — **ไม่ได้แก้ที่ guard และไม่ใส่ `# subprocess-console-ok:`** เพราะทั้งสองทางจะทำให้ guard จับของจริงไม่ได้อีก

### Added (เพิ่ม)
- **ปุ่มเปิด Graph Viewer** ในกล่องของ chip — `graft viz` เสิร์ฟ graph แบบโต้ตอบได้ · ยืนยันด้วย browser จริงแล้ว: แท็บ **Code = 10,772 nodes / 21,209 links** (file 407 · function 2,724 · class 1,290 · method 6,351) เห็น symbol จริงของเรา
  - **แท็บ default ของ graft คือ `context` ซึ่งว่างเปล่าเสมอสำหรับเรา** (concept map มาจากชั้น LLM `--deep` ที่เราตั้งใจไม่ใช้) · อ่าน bundle ของ graft แล้วยืนยันว่า hardcode ไว้ เปลี่ยนผ่าน URL/hash/localStorage ไม่ได้ → บอก user ในกล่องแทนว่าให้ดูแท็บ Code · **ไม่เปิด `--deep`** (ต้องมี API key = เสียเงิน)
- **self-distill nudge** — เดิมความจำของ role เต็มงบ 6KB แล้ว `_trim_oldest_bullet` ดันของเก่าสุดออก archive · ค้นด้วย `takkub search` ได้ **แต่ agent ไม่เห็นตอน spawn อีกเลย** (เครื่องนี้มี ~14KB/role ที่หลุดไปแล้ว) — **การตัดตามอายุคือการเดาว่าอะไรสำคัญ**
  - ตอนนี้เมื่อ curation ต้อง archive จริง → ตั้ง flag → **spawn ครั้งถัดไปของ role นั้น** ได้ nudge 1 บรรทัดให้กลั่นความจำตัวเองก่อน append ใหม่ (รวม bullet เรื่องเดียวกัน ตัดคำฟุ่มเฟือย เก็บใจความ) · pane มี context โปรเจคอยู่แล้วจึงรู้ว่าอะไรสำคัญ
  - **งบเท่าเดิม 6,000 bytes — ไม่ได้ลด token แต่บรรจุความรู้ได้มากขึ้นต่อ token เท่าเดิม**
  - nudge ขึ้น**เฉพาะตอน flag ถูกตั้งจริง** ไม่ใช่ทุก spawn · ทำที่ spawn ไม่ใช่ `done()` เพราะ `done()` ปิด pane ใน 2.5 วิ เสี่ยง race · archive ยังเก็บฉบับเต็มเหมือนเดิม · ทุก provider
  - แนวคิดจาก TencentDB-Agent-Memory (L0→L3) — **เอาแค่แนวคิด ไม่ลง Docker ไม่ใช้ API key เพิ่ม** แบบเดียวกับ ReflexionMemory จาก SuperClaude ใน 1.0.46

### Decisions (ตัดสินใจไม่ทำ พร้อมเหตุผล)
- **ไม่ index โฟลเดอร์ที่ไม่ใช่ git repo** แม้จะมีถึง 15 อัน — ไม่มี `.gitignore` ให้กรอง noise จะย้อนกลับไปเจอบั๊กคลาสเดียวกับ H1 ใน 1.0.48 (index venv/`node_modules` จนบวม 463MB) และโฟลเดอร์เหล่านั้นส่วนใหญ่ไม่ใช่โค้ดอยู่แล้ว

## [1.0.48] - 2026-08-06

รอบนี้มาจาก user directive "อุดให้หมด อย่ารอให้ถามแล้วค่อยแก้" — cross-OS audit + final review เจอ **1 blocker วนไม่รู้จบ + 6 medium** ที่ CI จับไม่ได้เลย เพราะ repo ทดสอบไม่มี git submodule และไม่มี path ยาวเกิน MAX_PATH

### Added (เพิ่ม)
- **🧠 chip Graft บน status bar** — 4 สถานะ: พร้อมใช้ / กำลัง build (X/Y) / **ยังไม่ได้ลง** (บอกให้รัน `takkub doctor --fix`) / **build ล้ม N อัน** · คลิกดูรายละเอียด · ไม่มี popup ขวางทาง · เดิม cockpit build graph เบื้องหลังเงียบสนิท user ไม่มีทางรู้ว่าเกิดอะไรหรือทำไม graft ตอบว่าง
- **`graft_autobuild.get_build_status()`** — snapshot thread-safe `{'building': int, 'failed': list[str]}` เบาพอให้ UI poll ทุก 2 วินาที · แยก **queued** ออกจาก **in-flight** (semaphore cap 3) จึงไม่โชว์ "Building 46" ตอน boot ทั้งที่ build จริง 3
- **trigger ที่ 4: resync ระหว่างทำงาน** — เดิม graph อัปเดตแค่ตอน boot / สลับแท็บ / หลัง pane `done` → **ระหว่างที่ pane กำลังแก้โค้ด graph ยังเป็นภาพก่อนแก้** ถาม graft ได้คำตอบเก่า = มั่นใจแล้วผิด · ตอนนี้ resync staging บน idle-watchdog tick (throttle 15s/dir, ไม่ใช่ `graft build` เต็ม)
- `takkub doctor` รายงานขนาด store · `takkub prune --help` ลิสต์ `graft-graphs` ครบ

### Fixed (แก้)
- **🔴 วนไม่รู้จบบน repo ที่มี git submodule** — `git ls-files` รายงาน submodule เป็น 1 entry แต่บนดิสก์เป็น **directory** → stage ไม่ได้ → ระบบคิดว่า "ยังมีไฟล์ใหม่" → `_spawn_build` ใหม่ **ทุก ~15 วินาทีตลอดอายุ pane** กิน semaphore + rewrite store ทั้งก้อนไม่หยุด · broken symlink / dir symlink / non-utf8 เข้าทางเดียวกัน
  - **รอบสอง (ฝั่งปลายทาง):** แก้ครั้งแรกเช็คแค่ฝั่ง source แต่ **MAX_PATH fail ที่ปลายทาง** (staging prefix คงที่ 77 ตัวอักษร → rel path เกิน ~182 พังตอนเขียน) loop จึงกลับมา · ปิดด้วย escalation memory ที่ยืนยัน convergence **หลัง build จบ** ก่อนเลิก escalate + log ครั้งเดียวบอกชื่อไฟล์ แทนที่จะเงียบหรือสแปมทุกรอบ
- **🔴 cross-device inode collision (regression จากการ optimize รอบเดียวกัน)** — `_stage_files` เทียบ `st_ino` โดยไม่เทียบ `st_dev` · inode เป็น per-volume → ถ้า `AGENT_TAKKUB_HOME` อยู่คนละไดรฟ์ (use case ที่ document ไว้เอง) inode ชนกันได้ = **ข้าม sync ถาวรแบบเงียบ** ไฟล์ไม่อัปเดตอีกเลย → `os.path.samestat` (st_dev + st_ino)
- **staging ก๊อปใหม่ทั้งหมดทุกรอบ** ทั้งที่ไม่มีอะไรเปลี่ยน — 739 ไฟล์ = 264ms ทุก 15 วินาทีต่อ pane → เทียบ inode ก่อน relink เหลือ ~70ms · *(ตัดข้อเสนอ `(mtime,size)` ทิ้งหลังพิสูจน์ว่าบน Windows การเขียน 2 ครั้งติดกันได้ `st_mtime_ns` เท่ากันจริง)*
- **chip ถ่วง UI** — `_graft_progress_snapshot` วิ่งบน Qt main thread ทุก 2 วินาที สแกน PATH 69 entries = 16.3ms/รอบ · network path offline = จอค้างทุก 2 วิ → cache แยกส่วน (แพง=cache, `building`/`failed`=เรียกสด) **16.3ms → 0.0002ms** และไม่โชว์ "0/46" เทา ๆ ตอน boot ทั้งที่ build อยู่จริงอีกต่อไป
- **casing ผิดด้าน 2 จุดที่เหลือ** (`_dirs_for_project`, `_graft_progress_snapshot`) ยังใช้ `os.name == "nt"` → **บน macOS path ต่างตัวพิมพ์ = 2 build ยิงใส่ store เดียวกันพร้อมกัน** คือ corruption ที่ docstring ตัวเองห้ามไว้ · ทั้งคู่เปลี่ยนไปใช้ `graft_store.graph_key()` ตัวเดียวกับที่ store ใช้ จึง drift กันไม่ได้อีก
- `resync_staging_only` ไม่เช็ค graft CLI → เครื่องที่ **ไม่มี graft** ยิง git subprocess + 2 threads ทุก 15 วิ/pane ฟรี ๆ
- `qa.md` + `critic.md` ขาดกฎ new-file ทั้งที่ทั้งคู่ได้ graft — **qa สร้างไฟล์เทสใหม่ตลอด คือ role ที่ต้องการกฎนี้ที่สุด**
- **failed ค้างถาวร** — dir ที่ถูกลบจาก `projects.json` หลัง build ล้ม ค้างบน chip ตลอดไป → TTL 24 ชม. lazy-prune (ไม่เพิ่ม I/O ให้ getter ที่ UI poll ทุก 2 วิ)
- `~` เขียนไม่ได้ (เครื่ององค์กร) เดิมเงียบสนิท → ขึ้นใน `get_build_status()['failed']` · `GRAFT_STORE_ROOT`/`STAGING_ROOT` lazy ผ่าน module `__getattr__` (เดิม freeze ตอน import) · cache `codex --version` keyed by (bin, mtime) — วัดจริง cold 125-200ms → warm ~0ms

### Fixed — test quality (เจอเพราะ verify ไม่ใช่เพราะ CI)
- **เทสที่หลอกตัวเอง 3 กลุ่ม** — (1) `test_graft_store_root_never_under_data_home` อ่านค่าที่ conftest patch ทับ ผ่านบน Windows เพราะ tmp บังเอิญอยู่ใต้ `~` แต่ **ไม่เคยทดสอบสูตรจริงบนแพลตฟอร์มไหนเลย** (2) 4 เทสของ `resync_staging_only` ผ่านแบบ vacuous บน CI เพราะ CI ไม่ได้ลง graft — พิสูจน์ด้วยการลบ precondition ทิ้งแล้วยังผ่าน (3) module-global state ของ `graft_autobuild` รั่วข้ามเทส → `test_graft_chip` พังเฉพาะตอนรัน full suite (targeted จับไม่ได้)
- ทุกเทสที่แก้ **พิสูจน์แล้วว่าจะ fail จริงถ้าทำลาย logic ที่มันควรคุม** ไม่ใช่แค่ทำให้เขียว

### Known gaps (ยังเปิดอยู่ — ไม่ block)
- ยังไม่ได้ smoke pane codex ตัวจริง (ค้างจาก 1.0.46 — codex ติด token limit) · unit test คุมไว้
- dev กับ prod cockpit build graph คนละชุด (ตั้งใจ — จะ share ต้องมี cross-process file lock ก่อน ไม่งั้น 2 โปรเซสเขียนทับกัน)
- LOW ที่เหลือจาก audit ไม่มีอันไหนกระทบการใช้งาน

### Fixed — mid-task staleness follow-up
- **ไฟล์ที่สร้างใหม่ระหว่าง task (ไม่เคยอยู่ใน graph มาก่อน) ยัง invisible แม้ `resync_staging_only` วิ่งแล้ว** — ต่างจากไฟล์ที่แก้ (modify) ซึ่งปิดไปแล้วใน 1.0.47 (freshness gate ของ graft เอง refresh ให้ถูกต้อง) ไฟล์ใหม่กลับไม่ถูก refresh: พิสูจน์ว่า `resync_staging_only` คัดลอกไฟล์เข้า staging mirror ถูกต้องแล้ว แต่ `graft ask` ยังตอบว่างเงียบๆ (ไม่มีบรรทัด `[graft] refreshed...`) ต้องรอ `_build_one` เต็มรอบถัดไปถึงจะเห็น
  - แก้โดยให้ `resync_staging_only` เทียบรายชื่อไฟล์ที่ git เห็นตอนนี้กับที่ staging mirror มีอยู่จริง (`_has_new_files`) — ถ้าเจอไฟล์ที่ staging ยังไม่มี ถือว่าเป็นไฟล์ใหม่ → escalate เป็น `_spawn_build` เต็มรูป (ใช้ single-flight/semaphore/throttle เดิมทั้งหมด) แทนการ sync เฉยๆ
  - พิสูจน์ end-to-end กับ `graft` CLI จริง (ไม่ mock): build baseline → สร้างไฟล์ใหม่พร้อม symbol ใหม่ → เรียก `resync_staging_only()` ของจริง → รอ build จบ → `graft ask` เจอ symbol ใหม่ทันที ไม่ต้องรอรอบ build ปกติถัดไป

### Known behavior (พฤติกรรมที่รู้ไว้ตั้งใจ — ไม่ใช่ gap ที่ปล่อยเงียบ)
- **สาเหตุที่แท้จริงฝั่ง `@nanonets/graft` เองไม่ได้ไล่ลึกลงไป** — อ่านซอร์สที่ shipped มา (`probeDrift`/`ensureFreshGraph` ใน `graph/fingerprint.js`/`graph/refresh.js`) แล้วดูเหมือนไฟล์ใหม่ควรถูกจับผ่าน `drift.added` เหมือนไฟล์ที่แก้ผ่าน `drift.changed` แต่ทดสอบจริงกลับไม่เป็นแบบนั้น — เลือก workaround ที่ควบคุมได้เองในฝั่ง `graft_autobuild.py` (escalate เป็น build เต็มเมื่อเจอไฟล์ใหม่) แทนการ debug เข้าไปใน dependency ภายนอก เพราะพิสูจน์แล้วว่าปิดช่องได้จริงและความเสี่ยง regression ต่ำ (ใช้ build path เดิมที่ผ่าน test อยู่แล้ว)
  - เอกสารกฎไว้ใน `.claude/agents/*.md` (backend/devops/frontend/mobile/reviewer) + `codex_agents_md.py` ด้วยเป็น defense-in-depth เผื่อ agent ถามคำถามเร็วกว่า throttle (15s) + เวลา build จริงจะทำเสร็จ — "ไฟล์ที่เพิ่งสร้างใหม่ graft อาจยังไม่เห็น ห้ามสรุปว่าไม่มี ให้ fallback ไป Glob/Grep"

## [1.0.47] - 2026-08-05

### Added (เพิ่ม)
- **`graft build` รันเองอัตโนมัติแล้ว** — 1.0.46 เสียบ graft MCP ไว้แต่ไม่มีอะไรสร้าง graph ให้ ฟีเจอร์เลยเงียบไปเฉย ๆ จนกว่า user จะรู้เองว่าต้องรันคำสั่ง · ตอนนี้ `graft_autobuild.py` ยิงให้ 3 จังหวะ: **ตอน cockpit boot** (ทุกโปรเจกต์ใน `projects.json`), **ตอนเปิด/สลับ project tab** (เฉพาะที่ยังไม่มี graph), **หลัง pane รายงาน `done`** (debounce 20s กัน shard เสร็จพร้อมกันแล้วยิงซ้ำ)
  - background thread ล้วน + timeout 600s/build · semaphore คุม concurrency 3 · single-flight ต่อ directory · **boot ไม่หน่วง** (วัดจริง 300-460ms เท่า baseline)
  - rebuild ที่มี cache แล้ว = **1.02s** (141 ไฟล์, 136 replay จาก cache) → ไม่ต้องเพิ่ม git-diff gating
  - skip เงียบถ้าไม่มี graft CLI · skip worktree (#81) · **structural only ไม่มี `--deep`** (ไม่ต้องมี API key ไม่มีค่าใช้จ่าย) · ไม่แตะ `graft init`
  - kill switch `TAKKUB_SKIP_GRAFT_BUILD` (เหมือน `TAKKUB_SKIP_MCP_WARM`) — conftest ตั้งให้ทุก test run อยู่แล้ว
- **`takkub disk` เห็น graph แล้ว** — category `graft-graphs` แยกออกมา, ตัวที่ path หายจาก `projects.json` แล้วขึ้นเป็น orphan ให้ `prune` ลบได้ (ไม่แตะตัวที่ยัง live)

### Fixed (แก้)
- **graft เขียนไฟล์ลง repo ที่ไม่ใช่ของ cockpit** — auto-build sweep วิ่งข้ามทุก path ที่ตั้งค่าไว้ (เครื่องทดสอบ: **46 โฟลเดอร์ จาก 27 โปรเจกต์**) ซึ่งส่วนใหญ่เป็น git repo ของ user เอง · `graft build` แบบไม่มี `--dir` เขียน `graft/`, `.gitignore`, `.ignore` ลง target ตรง ๆ → tree ของ user เลิกสะอาดทันทีที่เปิด cockpit และ `git add -A` เผลอ ๆ ก็ commit ไฟล์ของ cockpit ติดไป
  - `graft_store.py` (ใหม่) — graph ย้ายไปเก็บนอก target ทั้งหมดที่ `~/.agent-takkub/graft-graphs/<instance-hash>/<target-hash>/` ผ่าน `--dir` · MCP inject `--dir` ต่อ pane ให้ตรงกัน (ผ่าน `mcp_bridge` ครบทั้ง claude `strict` และ codex `session_override`)
  - key = **SHA-256 ของ absolute path ที่ normalize แล้ว** ไม่ใช้ `decode_project_dir` หรือ encoding แบบ lossy (โปรเจกต์ที่ชื่อมี `-` `_` `.` หรือเว้นวรรค จะ round-trip ผิดแล้วชี้ graph ผิดตัวแบบเงียบ ๆ) · บน Windows case-fold ก่อน hash ด้วย ไม่งั้น path เดียวกันคนละตัวพิมพ์จะแตกเป็น 2 store · มี `source.json` manifest ให้ย้อนดูได้ว่า hash ไหนคือโปรเจกต์ไหน
  - **แยกต่อ cockpit instance โดยตั้งใจ** (`<instance-hash>`) — single-flight ปัจจุบันเป็น `threading.Lock` ระดับโปรเซส ถ้า dev กับ prod cockpit (รันพร้อมกันได้บนเครื่องเดียว) ใช้ store ร่วมกันจะเขียนทับกันโดยไม่มีอะไรกัน แล้ว agent จะเชื่อ graph ที่พังครึ่ง ๆ · จะ share ต้องทำ **cross-process file lock** ก่อน — เขียนเตือนไว้ใน `graft_store.py` แล้ว
- **cockpit เขียนไฟล์ลง repo ตัวเอง** — `GRAFT_STORE_ROOT` เดิมอิง `DATA_HOME` ซึ่งใน dev checkout `DATA_HOME == REPO_ROOT` → store ตกในรีโปตัวเอง → `graft build` เขียน `.ignore` ที่ราก **และ append เข้า `.gitignore` ที่ track อยู่** ทุก boot · ย้าย store ออกนอกรีโปเสมอ (ไม่อิง `DATA_HOME` อีก) แก้ทั้งสองอาการที่ต้นทาง

### Fixed — จาก cross-OS audit ก่อนปล่อย (`docs/reviews/2026-08-05-graft-crossos-audit.md`)
รอบ audit เต็มก่อน publish เจอ 3 blocker + 6 medium · แก้ครบก่อนขึ้น npm — **1.0.47 ไม่เคยถูก publish ในสภาพที่มีปัญหาเหล่านี้**

- **graph บวมหลาย GB เพราะ index ของที่ `.gitignore` ไว้** — พิสูจน์จากการอ่าน source ของ graft เอง: **มันไม่อ่าน `.gitignore` เลย** มีแค่ skip-list 9 ชื่อที่ hardcode ไว้ · repo cockpit เอง = **463MB / 4,005 ไฟล์** ซึ่ง 72% เป็น venv ใต้ `runtime/` (source จริง 143 การ์ด) × 46 dirs × 2 cockpit instance
  - build จาก **staging mirror ที่กรองด้วย `git ls-files --cached --others --exclude-standard`** แทนการชี้ที่ target ตรง ๆ → **463MB → 43MB (−91%)**, path ยาวสุด 308 → 139 ตัวอักษร
  - staging เป็น **hardlink** (ไม่ใช่สำเนาจริง) เมื่ออยู่ volume เดียวกัน → ต้นทุนดิสก์จริงเกือบศูนย์ · re-sync ทุก build (ลบไฟล์ที่หายไป, re-link ไฟล์ที่เปลี่ยน)
  - **รอบสองของบั๊กเดียวกัน:** staging แรกเป็น tempdir ที่ถูกลบหลัง build → graft ทำ freshness check ทุก query แล้ว **re-index จาก cwd จริงเงียบ ๆ** ทำให้บวมกลับเป็น 435MB ทันทีที่ pane ถามคำถามแรก (`graft mcp` ไม่มี `--no-refresh` ให้ปิด) · แก้เป็น staging ถาวร + ส่งเป็น positional `dir` ทั้งตอน build และตอน inject MCP → **วัดแล้ว store/staging ไม่ขยับเลยหลัง query**
  - skip target ที่ไม่ใช่ git work-tree (โฟลเดอร์เอกสาร/รูปใน `projects.json` ไม่ถูก index อีก)
  - `takkub prune` ลบ live store ได้ (escape hatch) + เตือนเมื่อ store ใหญ่เกินเกณฑ์
- **Windows MAX_PATH** — 3,080 ไฟล์ยาวเกิน 259 ตัวอักษร · เครื่อง dev รอดเพราะเปิด `LongPathsEnabled` ไว้ซึ่ง **ไม่ใช่ default ของ Win11** → hash key 64 → 16 hex (คืนมา 129 ตัวอักษร) + `built.json` marker แทนการเช็คแค่ว่า `.graph` มีอยู่ (build ที่ค้างครึ่งทางไม่ถูกนับว่าเสร็จอีกต่อไป) + legacy-store detection สำหรับ key 64 ตัวเก่าที่ scan/prune เดิมมองไม่เห็น
- **MCP บอก agent ว่า repo index แล้วทั้งที่ graph ว่าง** — เดิม inject ให้ทุก role โดยไม่เช็ค แต่ graft CLI ลงให้เฉพาะ `doctor --fix` → user ใหม่ได้เครื่องมือที่บอกให้ "ใช้แทน grep" ทั้งที่ไม่มีข้อมูล · ตอนนี้ inject เฉพาะ pane ที่ store build เสร็จจริง (มี CLI + มี marker + ไม่ใช่ worktree cwd)
- **case-fold ผิดด้าน** — เดิมทำเฉพาะ `os.name == "nt"` ซึ่ง `Path.resolve()` บน Windows แก้ case ให้อยู่แล้ว **แต่ darwin ที่ APFS case-insensitive จริงกลับไม่ได้ทำ** → path เดียวกันคนละตัวพิมพ์บน Mac จะแตกเป็น 2 store · กลับด้านให้ถูก
- worktree pane ไม่ inject graft (เดิม build side skip แต่ MCP side ไม่ skip → ได้ graph ว่างถาวร) · `expanduser` รวมจุดเดียวใน `_normalize_for_key` · timeout ใช้ `Popen` + `taskkill /T` กัน `node.exe` ค้างบน Windows (เดิม `subprocess.run` เก็บ pid ไม่ได้)

### Known gaps (ยังปิดไม่ได้)
- **ยังไม่ได้ smoke pane codex ตัวจริง** (ค้างจาก 1.0.46) — qa ไม่มีสิทธิ์ `takkub assign` และ codex ติด token limit · unit test คุมไว้
- dev กับ prod cockpit ยัง build graph คนละชุด (ตั้งใจ — ดูเหตุผลด้านบน) กินดิสก์ 2 เท่า
- `engines: node >= 18` ใน `package.json` แต่ graft ต้อง Node ≥ 20 (doctor เตือนให้แล้ว)

ปิดแล้ว (2026-08-06 — ดู `docs/reviews/2026-08-05-graft-crossos-audit.md` สำหรับที่มา):
- ~~`codex --version` ยิงทุก spawn (363ms) ไม่ cache~~ — cache ระดับ process แล้ว คีย์ด้วย `(provider_bin, mtime ของไฟล์ binary)` ให้ codex-cli อัปเกรดที่ path เดิมแล้วยัง re-probe ถูก (`mcp_bridge._codex_cli_version_cached`) · วัดจริงกับ binary จริง: cold call ~125-200ms, warm call ~0ms
- ~~`GRAFT_STORE_ROOT` คำนวณตอน import ไม่ lazy~~ — ย้ายเป็น module-level `__getattr__` (PEP 562) คำนวณสดทุกครั้งที่อ่าน ตอบสนอง `AGENT_TAKKUB_HOME`/`DATA_HOME` ที่ patch หลัง import ได้จริง (พิสูจน์ด้วยเทส patch DATA_HOME หลัง import แล้วอ่านค่าใหม่)
- ~~`~` เขียนไม่ได้แล้วเงียบ ไม่มีสัญญาณบน UI~~ — build ที่ mkdir store ล้ม (unwritable home) ตอนนี้ถูกบันทึกเป็น failure ใน `graft_autobuild.get_build_status()` เหมือน build failure อื่นๆ ทุกตัว ไม่ใช่ silent path พิเศษอีกต่อไป
- ~~first-run ไม่มีตัวบอกสถานะ (M6)~~ — `graft_autobuild.get_build_status()` (thread-safe `{'building': int, 'failed': list[str]}`, ไม่แตะ filesystem หนัก ปลอดภัยให้ poll ถี่) + status-bar chip ฝั่ง frontend อ่านค่านี้แล้ว
- ~~mid-task staleness: pane query graft ระหว่างที่ตัวเองกำลังแก้โค้ดอยู่ยังได้คำตอบเก่า (ไม่มี trigger คลุมช่วงนี้เลย นอกจาก boot/tab-switch/done)~~ — เพิ่ม trigger ที่ 4: `resync_staging_only()` วิ่งบน idle-watchdog tick ทุก pane ที่ `state=="working"` (throttle 15s/dir, ไม่ใช่ `graft build` เต็มรูป) sync แค่ staging mirror แล้วปล่อยให้ freshness gate ของ graft เองรีเฟรช graph แบบ incremental บน query ถัดไป · พิสูจน์ end-to-end จริงด้วย rename-over-write edit (ให้ inode ใหม่ ไม่ผูกกับ hardlink เดิม): query ก่อน resync ได้ symbol เก่าเงียบๆ (ไม่มี refresh message เพราะ staging ยังไม่เปลี่ยน) · หลังเรียก `resync_staging_only()` แล้ว query ซ้ำ ได้ `[graft] refreshed the graph (1 file changed)` แล้วตอบ symbol ใหม่ทันที
- ~~`disk_report` ปนสอง root เข้าด้วยกันใต้ label `data_home` เดียว (L2)~~ — เพิ่มฟิลด์ `graft_store_root` + `graft_store_root_outside_data_home` ในผลลัพธ์ + `takkub disk` พิมพ์หมายเหตุเมื่อ graft-graphs อยู่นอก DATA_HOME จริง (กรณี dev checkout ที่ `DATA_HOME == REPO_ROOT`)

## [1.0.46] - 2026-08-05

### Added (เพิ่ม)
- **graft = แผนที่โค้ดให้ทุก pane** — เสียบ [`@nanonets/graft`](https://github.com/NanoNets/Graft) `0.8.2` เป็น shared MCP ตัวที่ 5 ต่อจาก playwright/chrome-devtools/context7/notebooklm · pane ที่ต้องอ่านโค้ด (frontend/backend/mobile/devops/qa/reviewer/critic) ถาม graph ได้แทนการ grep แล้วเดา — `ask` (ค้น symbol), `skeleton` (API surface ของไฟล์), `callers` (ใครเรียกใคร)
  - **ใช้เฉพาะชั้น structural** (tree-sitter) — ไม่ใช้ `--deep` จึง**ไม่ต้องมี API key และไม่มีค่าใช้จ่ายเพิ่ม**
  - วัดจริงบน `src/agent_takkub` (`docs/audit/2026-08-05-graft-pilot.md`): parse Python **136/136 ไฟล์ (100%)** · build 4.68s · `skeleton orchestrator.py` = 12.5KB จากไฟล์จริง 213KB → **ลด token ~94%**
  - **macOS ใช้ได้ไม่ต้อง compile** — tree-sitter มี prebuild ครบ darwin-arm64/x64 + win32-x64 · ขอแค่ Node ≥ 20 · `takkub doctor` เช็ค/ลงให้ (opt-in ด้วย `--fix`)
  - **ไม่รัน `graft init`** เด็ดขาด — มันเขียนทับ `.claude/settings.json` + statusline ที่ cockpit จัดการเอง
  - `graft/` ถูก gitignore (regenerate ได้) — โค้ดซิงค์ข้ามเครื่องผ่าน git แต่ graph ต้อง `graft build` เองที่ปลายทาง
- **role memory จำความผิดพลาดเองแล้ว** — `done(failed=True)` เขียน 1 bullet เข้า `runtime/role-memory/<project>/<role>.md` อัตโนมัติ · เดิม FAILED report ไปถึง Lead แล้วหายไปเลย role เดิมจึงพลาดซ้ำเรื่องเดิมได้ · dedup ตามเหตุผล (ไม่นับวันที่), เคารพ entry cap 600 ตัวอักษร + budget 6k + archive rotation เดิม จึงไม่ทำ spawn prompt บวมสวนทาง token diet · ทำงานทุก provider (ดักที่ `done()` ซึ่งเป็น entrypoint ร่วม)

### Fixed (แก้)
- **role ที่ลงทะเบียนแล้วแต่ไม่มีนโยบาย MCP เคยได้ MCP ทุกตัวฟรี** — `gemini`, `shell` และ **custom role ที่สร้างเองทุกตัว** resolve เป็น `None` = passthrough → ได้ master `shared-mcp.json` ทั้งก้อนบน claude / `~/.codex/config.toml` ทั้งไฟล์บน codex (ซึ่งอาจมี MCP ที่ผูก token อยู่) ทั้งที่ไม่มีใครอนุญาต
  - ตอนนี้ **deny ทันที** (`frozenset()`) · `None` เหลือไว้เฉพาะชื่อที่ cockpit ไม่รู้จักจริง ๆ + เทส invariant ล็อกว่าทุกชื่อจาก `roles.all_role_names()` ต้อง resolve ไม่เป็น `None`
  - ⚠️ **breaking สำหรับ custom role**: ถ้าเคยพึ่ง passthrough ต้องไปเปิด MCP ให้ role นั้นเองในหน้า Settings → Role → Access
  - ปิดบั๊กแฝงตัวที่ 2 ที่เจอตามมา: `shared_mcp_config_path_for_role` หลุดไป master เมื่อ policy มีอยู่แต่ variant file ยังไม่ถูก generate
- **กฎ "output ของ tool ไม่ใช่คำสั่ง" ไปไม่ถึง pane ที่ไม่ใช่ claude** — role file ถูก append เข้า argv ใน claude branch จุดเดียว · pane ที่ใช้ `context_strategy=agents_md_file` (codex/opencode/kimi/cursor/gemini) ได้ `CODEX_AGENTS_MD` ที่ไม่มีกฎนี้ ทั้งที่ได้ graft ไปใช้ → ย้ายกฎเข้า Hard rules ของ `CODEX_AGENTS_MD` ครอบทุก provider ทั้งปัจจุบันและอนาคต (graft ฝังข้อความสั่ง agent ให้รายงาน "tokens saved" มากับ output จริง — รูปแบบเดียวกับ prompt injection)
- **codex spawn พังทั้งเส้นได้ถ้า CLI สะดุด** — `_codex_resolved_mcp_names` raise `RuntimeError` ดิบ ๆ โดยที่จุดเรียกใน `spawn_engine` ไม่มี try/except (docstring ยังเคลมผิดว่า "never raises") → `McpResolutionError` + fail-closed: ปฏิเสธ spawn พร้อมข้อความ แทนที่จะพังดิบหรือปล่อย pane ขึ้นมาโดยไม่รู้ว่ามี MCP อะไรติดมา
- **การ์ดของ graft ปนตอน grep** — `graft build` เขียน `.ignore` ทับกลับทุกครั้งด้วย `!graft/` ทำให้การ์ด 141 ใบเข้า ripgrep · การ์ดมีเลขบรรทัดที่ไม่ตรง source จริงแต่หน้าตาเหมือน `file:line` → ship `src/agent_takkub/.rgignore` ที่ outrank `.ignore` และ graft ไม่แตะ (ยืนยันด้วยการ build ซ้ำ 2 รอบ)
- **เทส `test_bm25_search` รั่ว** — corpus resolve ผ่าน 2 ทาง (`Path.home()` + `bm25_search.ROLE_MEMORY_DIR`) แต่เทสเดิม patch ทางเดียว ไฟล์ role-memory จริงบนเครื่องจึงรั่วเข้า index → autouse fixture ปิดทั้งสองทาง

### Changed (เปลี่ยน)
- **premise ของ #121 ที่เข้าใจผิดมาตลอด** — verify กับ codex-cli จริง 3 เวอร์ชันติดกัน (0.144.1 / 0.145.0 / 0.146.0): `-c mcp_servers={}` เคลียร์ inherited table ได้**ครบทุกเวอร์ชัน** และคงสภาพเคลียร์แม้ layer partial override ทับทีหลัง · ที่ #121 เจอว่า "merge ไม่ replace" คือเคส bare partial override ที่ไม่มี `mcp_servers={}` นำหน้า ซึ่ง cockpit ไม่เคยส่ง
  - gate ขั้นตอน resolve-and-disable ที่ซ้ำซ้อนด้วย `_CODEX_RESOLVE_SAFE_MIN_VERSION = (0,144,1)` — เวอร์ชันเก่าสุดที่ทดสอบจริง ไม่ใช่ 0.146.0 · ต่ำกว่านั้นหรืออ่านเวอร์ชันไม่ออก = คงพฤติกรรมระวังตัวแบบเดิม (เราไม่ได้ทดสอบทุก build ที่เคยออกมา จึงไม่เหมาว่า "codex ไม่เคย merge")
  - ผลข้างเคียง: spawn ของ codex ประหยัดไป ~80ms (เลิกยิง `codex mcp list` 440ms เหลือ `codex --version` 363ms)

### Known gaps (ยังปิดไม่ได้)
- **ยังไม่ได้ smoke pane codex ตัวจริง** — qa ไม่มีสิทธิ์ `takkub assign` (CLI gate กันไว้เอง) · unit test คุมไว้ 32 ตัว แต่ไม่เท่าของจริง
- `codex --version` ยังยิงทุก spawn (363ms) ยังไม่ cache ระดับ session
- `graft ask` เป็น lexical (BM25) ล้วนเพราะไม่ใช้ `--deep` — query ที่ไม่มีคำร่วมกับโค้ดเป้าหมายจะหาไม่เจอ ให้ fallback ไป grep
- `graft callers` under-report ได้ (เจอเคส type-annotated instantiation) — ผลลัพธ์ "no callers" **ห้ามใช้เป็นหลักฐานว่าโค้ดตายแล้ว**

## [1.0.45] - 2026-08-05

### Added (เพิ่ม)
- **`takkub search` เป็น BM25 ranking แล้ว** (#152) — เดิมเป็น grep ต้องเดาคำเป๊ะ · ตอนนี้ค้นแบบจัดอันดับความเกี่ยวข้อง (Okapi BM25) ข้าม session logs ทุกโปรเจค + role-memory archives พร้อม score/บรรทัดที่เจอ · **รองรับภาษาไทย**ด้วย character trigram (ไทยไม่มีวรรค — ไม่ต้องพึ่ง segmenter ภายนอก) · เขียนเองทั้งตัว **ไม่มี dependency ใหม่** · query สั้นเกิน/index พัง → ตกกลับ grep เดิมอัตโนมัติ, flag `--grep` บังคับโหมดเก่าได้
- **role-memory L2 archive** (#151) — เดิม entry ที่โดนตัด/ทิ้งตอน curation (token diet) หายถาวร · ตอนนี้เนื้อเต็มถูกเก็บลง `<role>-archive.md` ข้างไฟล์เดิม (ประทับวันที่ ISO, cap 200k แบบ rotate ตัวเก่าสุดออก) — **ไม่ถูก inject เข้า pane** จึงไม่กระทบ token budget แต่ค้นกลับได้ผ่าน `takkub search` · header ของไฟล์ memory ชี้ archive ให้ agent รู้
- **Evidence-cite check** — done note ของ verify roles (qa/reviewer/critic/designer) ที่สรุปลอยๆ โดยไม่อ้างหลักฐานเลย (ไม่มี path ของ report/shots/log และไม่มีผลรันจริงอย่าง "N passed"/"exit 0") จะถูก tag **`⚠ no evidence cited`** ให้ Lead เห็นใน done notice — เตือนอย่างเดียวไม่ block · เช็คที่ engine จึงไม่กิน token ของ pane, role files แตะเพียง 1–2 บรรทัด · reviewer ถูกเพิ่มเข้ากลุ่มที่ตรวจด้วย

### Fixed (แก้)
- **เครื่องที่ติดตั้งผ่าน npm ได้ CLAUDE.md ที่ pointer ด้วน** — CLAUDE.md หลัง token diet (1.0.44) อ้าง `docs/lead/cli-reference.md` / `docs/lead/patterns.md` รวม 6 จุด แต่ wheel ไม่เคย ship โฟลเดอร์ `docs/` เลย และต่อให้ ship มา path แบบ relative ก็ resolve ไม่ได้จาก cwd ของโปรเจค user (เครื่อง dev ไม่เจออาการเพราะ cwd คือ repo เอง)
  - `setup.py` stage `docs/lead/*.md` เข้า wheel แล้ว + **build จะ fail ทันที**ถ้า CLAUDE.md อ้างไฟล์ docs/lead ที่ไม่มีจริง (กัน pointer ด้วนตั้งแต่ต้นทางทุก release ต่อไป)
  - เครื่องติดตั้ง: ตอน render Lead context ระบบ rewrite `docs/lead/` เป็น absolute path ของสำเนาที่ ship มา — Lead เปิดอ่านได้จริง · เครื่อง dev พฤติกรรมเดิมไม่เปลี่ยน
  - เพิ่ม assert เข้า installed-mode gate ใน CI (build + ติดตั้ง wheel จริงทั้ง Windows/macOS ทุก commit) — ช่องโหว่แบบนี้จะไม่หลุดอีก

### Changed (เปลี่ยน)
- **README ใหม่** — เพิ่มกลุ่ม feature "Token efficiency & memory" + "Reliability & diagnostics" (pull-on-demand context, role memory + archive, BM25 search, evidence-gated QA, doctor --live, Shift+Enter multiline พร้อมเครดิต [@than-aa](https://github.com/than-aa)) + อัปเดตตารางคำสั่ง — หน้าเดียวกันนี้แสดงทั้ง GitHub และ npm

## [1.0.44] - 2026-08-05

### Added (เพิ่ม)
- **Shift+Enter / Alt+Enter ขึ้นบรรทัดใหม่ได้ในช่องพิมพ์ของ pane** (PR #149 โดย [@than-aa](https://github.com/than-aa) ทดสอบบน Mac จริง) — เดิมกดแล้ว submit ทันทีเหมือน Enter ธรรมดา ต้องพิมพ์ multiline ผ่านวิธีอ้อม
  - ทำงานแบบ **provider-aware** ผ่าน field ใหม่ `ProviderSpec.multiline_newline_seq`: claude / gemini (Ink TUI — ESC เปล่าเป็น no-op) ได้ `ESC+CR` · **codex ไม่ intercept** (ratatui ตีความ ESC เป็น interrupt/ล้างช่องพิมพ์ — จะพังแทน) · opencode/kimi/cursor ยังไม่ยืนยัน toolkit → ไม่ intercept เช่นกัน = พฤติกรรมเดิม 100%
  - Ctrl+Enter / Cmd+Enter **ไม่ถูกแตะ** — ยัง submit ตามเดิมทุก pane
- **แก้ Mac IME/ภาษาไทยกินตัวอักษรหลังสลับภาษา** (PR #149) — เพิ่ม fallback ผ่าน helper textarea จับตัวอักษรที่ xterm.js ทิ้งหลัง CapsLock/สลับ layout (Cocoa NSTextInput) พร้อม guard ครบ: ไม่ flush ระหว่าง IME composition (จีน/ญี่ปุ่น/เกาหลี/ไทย), CapsLock intercept เฉพาะ macOS, ตัวอักษรที่พิมพ์ก่อน bridge ต่อเสร็จถูก queue ไว้ flush ตามลำดับ (เดิมหายเงียบ)

### Fixed (แก้)
- **แก้ 3 blocker ที่เจอตอนรีวิว PR #149 ก่อน merge** (รีวิวเต็ม: `docs/reviews/2026-08-04-pr149-mac-ime-shift-enter.md`) — พิมพ์ backquote/`~` ไม่ได้ทุก OS (xterm custom handler คืน false โดยไม่ `preventDefault` ตัวอักษรค้างใน textarea), IME ส่งข้อความที่ compose ไม่เสร็จเข้า PTY, Shift+Enter ทิ้ง `\n` ค้างจนปิด fallback ของตัวเอง · และ blocker รอบสอง: method ใหม่ถูกแทรกกลาง `__init__` ของ `terminal_widget.py` ทำ pane เปิดมาจอว่าง (CI จับไม่ได้เพราะ GUI ไม่ construct ใน headless — เพิ่ม structural test แบบ AST กันซ้ำแล้ว)

### Changed (เปลี่ยน) — token reduction wave 3 (ลดต้นทุนเปิด pane)
- **Lead context ลด −53%** (~15.4k → ~7.2k token โดยประมาณ) — cockpit CLAUDE.md ตัดจาก ~12.1k เหลือ ~4k โดยย้ายตัวอย่าง/reference ทั้งหมดไป `docs/lead/cli-reference.md` + `docs/lead/patterns.md` ให้อ่าน on-demand · กฎ/สัญญาพฤติกรรมอยู่ครบ
- **role memory ต่อ pane โดนคุมขนาดจริงจัง** — entry ที่ agent เขียนยาวเกิน 600 ตัวอักษร (เคยเจอ paste done-report ทั้งย่อหน้า ~1,500 token ต่อ bullet) ถูกตัดเหลือประโยคแรกอัตโนมัติตอน spawn · budget ต่อไฟล์ลด 16k → 6k bytes · header ระบุกฎ "entry ละไม่เกิน 2-3 บรรทัด" กันที่ต้นทาง
- **`.claude/agents/qa.md` ตัดจาก ~4.7k → ~2.6k token** (ยาวผิดปกติเกือบ 2 เท่าของ role อื่น) — พฤติกรรมครบ: verdict rubric, mb surface, shard mode, blocked protocol
- **snapshot `CLAUDE.spawn-*.md` เก่าถูกเก็บกวาดอัตโนมัติ** ตอน spawn (เก็บ 3 ตัวล่าสุดของ pane ที่ปิดแล้ว ไม่แตะ pane ที่เปิดอยู่)
- **ถอด notebooklm MCP ออกจาก Lead** (แทบไม่ได้ใช้ — ลด ~20 tools + instruction block ทุก session)

## [1.0.43] - 2026-08-04

### Fixed (แก้)
- **ข้อความสั้นที่มีหลายบรรทัดถูกตัดกลางทาง** (จาก PR #149 โดย [@than-aa](https://github.com/than-aa)) — LF ดิบที่เขียนลง PTY **คือปุ่ม Enter** ที่ชั้น TUI (`lead_draft_state._ENTER_BYTES` นับ `0x0A` เป็น submit) และ `_sanitize_pane_text` ก็จงใจเก็บ `\n` ไว้สำหรับ task หลายบรรทัด · ผลคือ payload ที่ **สั้นกว่า 200 ตัวอักษรแต่มีหลายบรรทัด** จะถูก submit ตั้งแต่ `\n` แรก แล้วมาถึงแบบขาดเป็นท่อน — เคสที่เจอจริงคือ done-notice หลายอันที่ถูกรวมเป็นก้อนเดียว (`lead_inbox.py:1669`) และข้อความจาก `takkub send`
  - ตอนนี้ payload ที่มีหลายบรรทัด **ห่อ bracketed paste เสมอไม่ว่าสั้นแค่ไหน** → ทั้งก้อนถึงปลายทางพร้อมกัน
  - ผลข้างเคียงที่ตั้งใจ: notice สั้นหลายบรรทัดเปลี่ยนไปใช้ `_PASTE_ENTER_DELAY_MS` (200 ms → 800 ms) มี test pin ไว้แล้วว่าเป็นความตั้งใจ ไม่ใช่ drift
  - ไม่กระทบ provider ตัวไหน — `_paste_payload` ไม่อ่าน provider spec เลย ทุก provider ได้ bracketed paste กับ task spec ปกติอยู่แล้ว
- **มือถือเห็นจำนวน pane ของทั้งทีม ทั้งที่ตั้งให้ mirror เฉพาะ Lead** — `api.pulse()` เป็น reader ตัวเดียวที่ **ตกหล่น** ตอนเพิ่ม `LEAD_ONLY_STREAM` เมื่อ 2026-07-23 (`api.activity` กับ `notify.LeadNotifier` ถูก gate ไปแล้ว) → `total` ยังนับทุก pane ที่เปิดอยู่ และ `working` ยังสะท้อนว่า teammate กำลังทำงาน · ตอนนี้ scope ลงเหลือเฉพาะ entry ของ Lead (`total` เป็น 1 หรือ 0 เท่านั้น ไม่บอกขนาดทีม) · PWA ที่ ship อยู่ไม่ได้เรียก endpoint นี้แล้ว (ใช้ `/api/activity`) แต่ route ยัง live + authenticated จึงยังเป็นช่องรั่วจริงของ API surface
- **error ตอน `--cwd` ผิด โชว์ path ซ้ำสองรอบ** (#150) — โปรเจคที่ตั้ง path ไว้ตัวเดียว (เช่น cockpit เอง) จะได้ "project root" เป็นค่าเดียวกับ configured path อยู่แล้ว เพราะ common parent ของ path เดียวคือตัวมันเอง · ตอนนี้ติดป้าย `(project root)` ที่บรรทัดเดิมแทนการต่อท้ายซ้ำ · โปรเจคหลาย path ยังเห็น root เป็นบรรทัดเพิ่มเหมือนเดิม

### Notes (หมายเหตุ)
- **PR #149 ยังไม่ merge ทั้งก้อน** — รับมาเฉพาะฝั่ง Python (ข้อแรกด้านบน) ส่วน `terminal.html` ติด 3 blocker ที่กระทบผู้ใช้ทุก OS ไม่ใช่แค่ Mac: พิมพ์ `` ` ``/`~` ไม่ได้ (xterm `attachCustomKeyEventHandler` คืน `false` ไม่ได้เรียก `preventDefault` ตัวอักษรเลยค้างใน helper textarea), CJK IME ส่งข้อความที่ยัง compose ไม่เสร็จเข้า PTY, และ Shift+Enter ทิ้ง `\n` ค้างจนปิด fallback ของตัวเอง · รายละเอียดเต็ม: `docs/reviews/2026-08-04-pr149-mac-ime-shift-enter.md`
- **#146 (Playwright MCP บน `qa --plan --shards`) ยังเปิดอยู่** — repro รอบใหม่บนเครื่อง dev: 3 shard แยก config + แยก browser profile จริง แล้ว **connect ได้ทั้ง 3** → per-shard isolation ไม่ใช่สาเหตุ · ระหว่างทางเจอ false lead ที่บันทึกไว้กันคนหลังหลงซ้ำ: `RUNTIME_DIR` ของ dev checkout (`<repo>/runtime/`) กับ installed (`~/.agent-takkub/runtime/`) เป็นคนละที่ ทำให้ดูเผินๆ เหมือนไฟล์ per-shard ไม่ถูกสร้าง

## [1.0.42] - 2026-08-04

รอบนี้เก็บบั๊กจาก issue queue ที่ค้างอยู่ทั้งหมด **8 เรื่อง** (#127, #139–#145) — ส่วนใหญ่เจอตอนใช้งานจริงกับโปรเจค wash-locker เมื่อ 4 ส.ค.

### Fixed (แก้)
- **งานทั้งระบบค้าง 47 นาที — assign แล้วไม่มี pane ขึ้นเลย ต้อง `takkub restart` อย่างเดียวถึงหาย** (#139) — อาการ: สั่ง `takkub assign` 6 รอบ pane โชว์ `empty` ตลอด → root cause: การเรียก **native PTY spawn** (pywinpty/ConPTY) บล็อกยาว **56.9 นาที** (`spawn_native_ms=3,412,178`) โดยถือธง `_spawn_in_progress` ของ FIFO arbiter ไว้ตลอดช่วงนั้น · คิว spawn ถูกระบายได้จาก `finally` ของ spawn เท่านั้น ไม่มีทางเข้าอื่นเลย และ watchdog เป็น diagnostic-only โดยเจตนา → ไม่มีอะไรกู้ได้เอง
  - แก้: หุ้ม native call ด้วย `spawn_pty_bounded()` + `PtySpawnTimeout` (worker thread + join แบบมี timeout · default **30 วินาที** ปรับได้ด้วย `TAKKUB_PTY_SPAWN_TIMEOUT_SEC`) — ครอบทั้ง **Windows (pywinpty) และ macOS/Linux (ptyprocess)** เหมือนกัน · ถ้า timeout แล้ว native call เสร็จช้าตามมาทีหลัง ตัว worker จะ `terminate(force=True)` process นั้นทิ้งเอง ไม่ปล่อยเป็น ghost
  - เพิ่ม **escape hatch ของคิว**: งานที่ค้างหัวคิวเกิน **120 วินาที** (`TAKKUB_SPAWN_QUEUE_STUCK_SEC`) จะถูกปล่อย arbiter + drain คิวอัตโนมัติ โดยเช็คบน tick 5 วินาทีของ idle watchdog (ไม่ต้องรอ spawn ตัวใหม่มาสะดุด)
  - log ที่เคยจมอยู่ใน `events.log`: เพิ่ม `spawn_native_slow` (เกิน 5 วินาที) และ `spawn_native_failed` (พร้อมเวลาที่ใช้จริง) แยกออกมาให้ grep เจอ
  - \+ 3 ไฟล์เทสใหม่ (`test_pty_spawn_timeout.py`, `test_pty_session_spawn_timeout.py`, `test_spawn_queue_stuck.py`)
- **assign ที่ตกเข้าคิวแล้วค้าง — Lead ไม่ได้รับสัญญาณอะไรเลย** (#140) — เดิมแจ้ง Lead เฉพาะตอน spawn **fail** (`[spawn-failed]`) ส่วนทางที่เข้าคิวคืนค่า "queued" แบบเงียบๆ ไม่มี notice ผูกอยู่เลย → Lead ต้องไปไล่อ่าน `runtime/events.log` เองถึงรู้ว่างานไม่ได้ออก · ตอนนี้มี **`[spawn-stuck]`** เด้งเข้า Lead (นับเป็น blocking notice แบบเดียวกับ `[spawn-failed]` — ข้ามคิว digest) พร้อมบอกว่า retry ให้อัตโนมัติแล้ว
- **งานที่ส่งให้ pane ที่กำลังยุ่ง เงียบไป 30 นาทีก่อนจะแจ้ง** (#144) — เดิมตอนเข้าโหมดรอ pane ว่าง ระบบ log แค่ event ไม่แจ้ง Lead แล้วเงียบยาวจนชนเพดาน `BUSY_WAIT_CEILING_SEC` (30 นาที) ถึงค่อยเด้ง `[delivery-unconfirmed]` → เพิ่ม **`[delivery-busy-wait]`** แจ้งครั้งเดียวตั้งแต่วินาทีที่เริ่มรอ (ไม่ flood — ครั้งเดียวต่อการส่ง 1 ครั้ง) ส่วน notice ตอนชนเพดานยังอยู่เหมือนเดิม
- **`takkub assign --cwd <path ผิด>` ตอบ `ok: task queued` ก่อน แล้วค่อย fail ทีหลัง** (#143) — validation อยู่ใน `spawn()` ซึ่งรัน async **หลัง** CLI ตอบ ok ไปแล้ว → Lead เข้าใจว่างานออกไปแล้ว · ตอนนี้ตรวจ cwd **แบบ sync ที่ `cli_server` ก่อน ack** — ผิดคือ exit non-zero ทันที และ error บอก **valid paths ของโปรเจคนั้นทั้งหมด** (ไม่ใช่แค่บอกว่าผิด) · เพิ่มด้วยว่า **root ของโปรเจคเองใช้เป็น cwd ได้แล้ว** (เป็น parent ของทุก path ที่ตั้งไว้อยู่แล้ว) · ตัว check ใน `spawn()` ยังอยู่เป็น backstop สำหรับ caller ที่ไม่ผ่าน socket
- **`takkub issue list` บอกว่าไม่มี issue ทั้งที่เพิ่งสร้างสำเร็จ** (#142) — `issue new` (default = ลง repo agent-takkub) เขียนลง `~/.agent-takkub/.takkub_issues.json` แต่ `issue list` อ่านจาก cwd ของ pane → คนละ store กัน เกือบวินิจฉัยผิดเป็น "ข้อมูลหายหลัง restart" (จริงๆ มีครบ 11 รายการ) · แก้ให้ `list` / `close` / `show` รับ `--cockpit-bug` / `--no-cockpit-bug` แบบเดียวกับ `new` (default ตรงกัน) + **แสดงบรรทัด `scope:` บอกว่ากำลังอ่าน store ไหน** เพื่อไม่ให้ "(no issues)" เปล่าๆ อ่านเหมือนข้อมูลหาย
- **`takkub status` มี ANSI escape ดิบปนจนอ่านไม่ออก** (#145) — regex เดิมครอบไม่ครบ 3 แบบที่เจอจริง: `[?25h`/`[?25l` (private-mode `?`), `[3G` (final byte `G`), และ `]0;…` (OSC — คนละตระกูลกับ CSI) · เปลี่ยนเป็น stripper เต็มสเปค ECMA-48 ครอบทั้ง CSI + OSC
- **`--model` ไม่เช็คว่า model id เป็นของ provider นั้นจริง** (#127) — เคสจริง: ส่ง `--model claude-haiku-4-5` ให้ role ที่ map เป็น gemini → CLI เตือนแล้ว fallback ไป default เงียบๆ · ตอนนี้ **บล็อกตั้งแต่ CLI เมื่อผิด provider ชัดเจน** (เช่น `claude-*` เข้า agy) พร้อม error ที่ชี้ mapping `role → provider` ให้เห็น · ส่วน id ที่ไม่รู้จักแต่ไม่ขัดกับใคร = **เตือนเฉยๆ ไม่บล็อก** (กัน model รุ่นใหม่ที่เพิ่งออกโดนบล็อกผิด) · provider แบบ router (opencode/cursor) ที่รับ model ได้หลายเจ้าโดยออกแบบ = ไม่แตะเลย

### Added (เพิ่ม)
- **`takkub doctor --live`** (#141) — `doctor` ปกติเป็น pure-logic ไม่คุยกับ cockpit **โดยเจตนา** จึงมองไม่เห็น state ใน memory ของ orchestrator: ตอนคิว spawn ค้างอยู่จริง 4 งาน doctor ยังรายงาน "all checks passed — 31 ok" · เพิ่ม endpoint `spawn-queue-status` (read-only) + check `[spawn-queue]` ที่เรียก endpoint นั้นเฉพาะเมื่อใส่ `--live` — คิวค้างเกิน 60 วินาที = FAIL พร้อมบอกให้ `takkub restart` · **`takkub doctor` เปล่าๆ ยังไม่ต้องมี cockpit รันเหมือนเดิม 100%** และถ้า cockpit ปิดอยู่ `--live` จะขึ้น SKIP ไม่ใช่ FAIL

### Changed (เปลี่ยน)
- ruff 0.16.0 → **0.16.1** (ไม่มีโค้ดต้องแก้ตาม) และ **กัน dependabot เสนอ PyQt6 ข้าม minor/major** — PyQt6 ถูก pin ที่ซีรีส์ **6.8 LTS โดยเจตนา** (ผูกกับ check ของ `takkub doctor` ผ่าน `test_version_sync.py`) · PR #138 ที่ขยายเป็น `<6.12` ทำ CI แดงทั้ง 3 OS จึงปิดไป และแทนที่ด้วยกฎ `ignore` ใน `.github/dependabot.yml` (security update ภายใน 6.8.x ยังผ่านได้ปกติ)
- อัป GitHub Actions: `github/codeql-action` 4 → 4.37.4 (PR #137) · `actions/setup-python` 6 → 7 (PR #122)

### Notes (หมายเหตุ)
- **#146 (Playwright MCP ไม่ connect บน `qa --plan --shards`) ยังไม่ปิด** — ไล่โค้ดแบบ static ครบทั้ง argv, provider resolution, env injection และ policy lookup แล้ว **ไม่พบบั๊กเชิงโครงสร้าง** (`--mcp-config` ถูกส่งเข้า shard pane ถูกต้อง, per-shard config ถูกสร้างครบ) · สาเหตุที่เหลือเป็นเรื่อง **timing/contention ตอน runtime** ซึ่งพิสูจน์ได้เฉพาะ repro สดเท่านั้น — บันทึกหลักฐาน + repro plan ไว้ที่ `docs/audit/2026-08-04-issue-146-playwright-shards.md`
- **ต้อง restart cockpit** ถึงจะได้ engine fix ของ #139/#140/#143/#144 (โค้ดที่รันอยู่เป็นตัวเก่าใน memory)

## [1.0.41] - 2026-08-03

### Added (เพิ่ม)
- **ตั้งระดับ Effort (ความลึกในการคิด) ได้เองต่อ role** — Settings → Providers & Roles มีช่อง **Effort** เพิ่มมาข้างช่อง model ของแต่ละ role · เดิมค่านี้ hardcode อยู่ในโค้ด (`_ROLE_MODEL_TIERS`) ปรับได้ทางเดียวคือ env `TAKKUB_TEAMMATE_EFFORT` ซึ่งมีผลทั้งระบบพร้อมกัน
  - เก็บใน `role-models.json` ไฟล์เดิม (ที่เก็บ provider+model อยู่แล้ว) แบบ sparse — เลือก "(ตามค่าเริ่มต้นของ role)" = ไม่เขียนลงไฟล์ · ไฟล์เก่าที่ไม่มีค่านี้ยังโหลดได้ปกติ
  - **รู้ว่า CLI ไหนรับอะไรได้จริง**: claude รับ `low/medium/high/xhigh/max` · codex รับ `low/medium/high` (ผ่าน `-c model_reasoning_effort=`) · agy/opencode/kimi/cursor **ไม่รองรับ** (agy ฝัง effort ไว้ในชื่อ model อยู่แล้ว เช่น `gemini-3.1-pro-high`) → ช่องจะ disable พร้อมบอกเหตุผล
  - **`claude-haiku-4-5` ไม่รองรับ effort** (CLI จะ error ถ้าส่งไป) → เลือก model นี้เมื่อไหร่ ช่อง Effort ปิดทันทีและไม่ส่ง flag แม้เคยตั้งค่าไว้
  - ลำดับความสำคัญ: **ค่าที่ตั้งใน Settings > env `TAKKUB_TEAMMATE_EFFORT` > ค่าเริ่มต้นของ role**
  - เปลี่ยน provider/model แล้วรายการอัปเดตทันที · ค่าที่ใช้ไม่ได้จะไม่ถูกลบเงียบ — คงไว้จนกด Save & Apply แล้วค่อยตัดพร้อมแจ้ง

### Fixed (แก้)
- **macOS: เปิดแอปจาก Finder/Launchpad/Dock แล้วหา CLI ไม่เจอ** (จาก PR #136 โดย [@than-aa](https://github.com/than-aa) ทดสอบบน Mac Intel i7) — แอป GUI บน mac **ไม่ได้รับ PATH ของ shell** ทำให้ตอน boot มองไม่เห็น `codex` / `agy` / `opencode` / `npm` → provider ที่ตั้งไว้ถูกมองว่า "ใช้ไม่ได้" แล้ว degrade กลับเป็น claude **ทุกครั้งที่เปิดใหม่** (อาการที่ผู้ใช้เห็นคือ "ตั้ง Lead เป็น codex แล้วไม่จำค่า")
  - `ensure_gui_path()` เติม path มาตรฐานเข้า PATH ตอน boot: Homebrew ทั้ง **Apple Silicon (`/opt/homebrew/bin`) และ Intel (`/usr/local/bin`)**, nvm, fnm, n, volta, asdf, `~/.local/bin`
  - หา `npm` เจอแม้ไม่อยู่ใน PATH — ครอบทั้ง Windows (`%APPDATA%\npm`, nodejs, `npm.cmd/.exe`), macOS และ Linux
  - **แก้บั๊กเลือก Node ผิดเวอร์ชัน**: การเรียงโฟลเดอร์ nvm แบบข้อความทำให้ `v9` มาก่อน `v20` (เพราะ `'9' > '2'`) เลยได้ Node เก่ากว่า — เปลี่ยนเป็นเรียงตามเลขเวอร์ชันจริง
  - รวมโค้ดค้นหาที่เคยซ้ำกัน 3 ที่ (`claude_update.py`, `update_panel.py`, `config.py`) ให้เหลือที่เดียว
  - เอาการเรียก PATH-bootstrap ออกจาก `_provider_available()` (ถูกเรียกทุกครั้งที่ spawn/เปิด Settings) + memoize ให้ทำงานจริงครั้งเดียวต่อการเปิดแอป
- **macOS: icon ไม่ขึ้นบน Dock / App Switcher** และเพิ่ม launcher ลง `~/Applications` ให้เปิดจาก Launchpad ได้ (PR #136)
- **`_ROLE_MODEL_TIERS` ยังชี้ `claude-opus-4-8`** — ตารางนี้อยู่คนละที่กับ dropdown ใน Settings เลยตกหล่นตอนอัปรายชื่อ model รอบที่แล้ว (1.0.40) · อัปเป็น `claude-opus-5` แล้ว พร้อมคอมเมนต์ชี้จุดที่ต้องแก้คู่กันเวลามีรุ่นใหม่

### Notes (หมายเหตุ)
- เครื่อง dev และ CI (`macos-latest`) เป็น **Apple Silicon** ทั้งคู่ — path ของ Homebrew ฝั่ง **Intel (`/usr/local/bin`) ยืนยันด้วยเทสที่จำลอง filesystem เท่านั้น** ยังไม่ได้รันบนเครื่อง Intel จริง (ขอให้ผู้ส่ง PR ช่วยยืนยันไว้แล้ว)
- ส่วนที่ PR #136 แถมมาแต่ไม่รับเข้า: การแก้ `save_role_overrides()` (ตรวจแล้วไม่ได้เปลี่ยนพฤติกรรมจริง + เสี่ยงลบค่าที่สัญญาว่าจะเก็บ) — รายละเอียดในรีวิว `docs/reviews/2026-08-03-pr136-mac-intel.md`

## [1.0.40] - 2026-08-03

### Fixed (แก้)
- **หน้าต่างดำเด้งแว๊บๆ ตอนเปิด cockpit (user รายงานว่า "รู้สึกเหมือนโดนไวรัส")** — cockpit รันใต้ `pythonw.exe` ซึ่งเป็น GUI process ที่ไม่มี console ของตัวเอง · บน Windows เมื่อ process แบบนี้เรียกโปรแกรม console (git.exe / npm / node) โดยไม่ใส่ `creationflags=CREATE_NO_WINDOW` ระบบจะ **สร้าง console หน้าต่างใหม่ให้ลูกทุกครั้ง** = หน้าต่างดำแว๊บแล้วหาย (การ redirect `capture_output` ไม่ช่วย — คนละเรื่องกัน)
  - พิสูจน์ด้วยตัวเลขจริง (harness รันใต้ `pythonw`): ลูกที่ไม่มี flag → `GetConsoleWindow()` = `396036` (มี console จริง) · ใส่ flag → `0`
  - จุดที่ทำให้เห็นตอน boot: **git ของระบบ worktree** (`prune_orphan_worktrees_boot()` รันทุกครั้งที่เปิดแอป), `git ls-files` ตอนเปิดโปรเจค, เช็ค npm อัปเดตหลัง boot, และตอน spawn pane ของ codex
  - แก้ **13 จุด** ที่ขาด flag (worktree · skill scan · update panel/worker · mcp bridge · issues · browser · limit status · remote settings/tunnel · skills page) — ค่าเป็น `0` บน macOS จึงไม่กระทบฝั่ง mac
  - เหลือ **1 จุดที่ตั้งใจไม่แก้**: `takkub issue new` ที่เปิด `$EDITOR` (vim/nano/notepad) แบบพิมพ์โต้ตอบ — ต้องใช้ console จริง ถ้าซ่อนจะพิมพ์ไม่ได้ · กำกับด้วยคอมเมนต์ `# subprocess-console-ok:`
  - **กันกลับมาเป็นซ้ำ**: เพิ่มเทสที่เดินอ่าน AST ทุกไฟล์ใต้ `src/agent_takkub/` (136 ไฟล์) แล้วฟ้องทันทีถ้ามี subprocess ใหม่ที่ลืม `creationflags` (ยกเว้นได้ด้วยคอมเมนต์ระบุเหตุผล) · QA ทดสอบแล้วว่าฟ้องแดงจริงเมื่อจงใจใส่โค้ดผิด
- **แถบนับ token คิดเปอร์เซ็นต์ผิดสำหรับ model รุ่นใหม่** — ตารางขนาด context ผูกกับชื่อที่มี `[1m]` ต่อท้ายเท่านั้น ทำให้ `claude-opus-5` / `claude-sonnet-5` / `claude-fable-5` / opus-4.x / sonnet-4.6 (ซึ่ง context **1M เป็นค่า default อยู่แล้ว**) ถูกคิดเป็น 200k → pane ที่ใช้ไป 150k โชว์ ~75% ทั้งที่จริง ~15% · ตอนนี้ผูกกับชื่อ model ตรงๆ และจับ `[1m]` ด้วย regex (ครอบคลุมรุ่นใหม่ที่ยังไม่อยู่ในตารางด้วย)

### Changed (เปลี่ยน)
- **รายชื่อ model ใน Settings → Providers & Roles เป็นรุ่นล่าสุด** — ของเดิมเป็น snapshot เดือน ก.ค. และหลายตัวใช้ไม่ได้จริง · รอบนี้ดึงจาก CLI ที่ติดตั้งอยู่บนเครื่องจริง ไม่ได้เดา:
  - **claude**: `claude-opus-5` ขึ้นเป็นตัวหลัก (เดิมสุดที่ `claude-opus-4-8`) · มี `claude-sonnet-5` / `claude-haiku-4-5` / `claude-fable-5` · เก็บ `claude-opus-4-8` ไว้เป็นรุ่นเก่าที่ยังใช้ได้
  - **codex**: `gpt-5.6` เดี่ยวไม่มีแล้ว กลายเป็น `gpt-5.6-sol` / `-terra` / `-luna` (ยืนยันจาก cache ของ codex เอง) · ตัด `gpt-5.3-codex` ที่หายไปแล้วออก
  - **gemini (agy)**: เปลี่ยนจากชื่อโชว์ (`"Gemini 3.5 Flash"` ซึ่งใส่แล้วใช้ไม่ได้) เป็น token จริงที่ `--model` รับ เช่น `gemini-3.6-flash-high`, `gemini-3.1-pro-high`
  - **opencode**: `anthropic/claude-opus-5`, `anthropic/claude-sonnet-5`
  - **kimi / cursor**: คงค่าเดิม + คอมเมนต์ระบุชัดว่า **ยังยืนยันไม่ได้** (kimi ไม่มีคำสั่งลิสต์ model, cursor CLI ไม่ได้ติดตั้งบนเครื่องนี้) — ไม่เดาให้
  - ทุกช่องยังพิมพ์เองได้เหมือนเดิม (CLI ออกรุ่นใหม่เร็วกว่ารอบ release)
- `plan_tier.PRO_LEAD_MODEL` → `claude-opus-5` (เจตนาเดิมคือ "Opus ตัวล่าสุดที่ไม่ใช่ 1M variant")

## [1.0.39] - 2026-07-28

### Added (เพิ่ม)
- **แท็บ 🌿 Git ใน Task dock — ดูสถานะ git ของโปรเจคที่เปิดอยู่ได้ในที่เดียว** · dock ขวา (chip `📋 Tasks` / Ctrl+Shift+T) แยกเป็น 2 แท็บ `📋 Tasks` / `🌿 Git`
  - tree แบบเดียวกับ task list: 1 แถวต่อ repo → **branch ปัจจุบัน · ↑ahead ↓behind เทียบ upstream · ●modified +untracked (หรือ `clean`)** กางออกเห็น **commit ล่าสุด 8 ตัว** (sha สั้น · subject · เวลาแบบ "2 hours ago") และหมวด **`wt/*` worktrees** ที่ pane isolation สร้างไว้ (commits ahead + dirty)
  - **โปรเจคที่มีหลาย repo รองรับเต็ม** — path key ทุกอันใน `projects.json` ถูกอ่านแยกกัน แล้ว **ยุบ key ที่ชี้ repo เดียวกัน** ด้วย `git rev-parse --show-toplevel` (monorepo ไม่โชว์ซ้ำ · repo แยกจริงโชว์ครบทุกตัว) · path ที่ไม่ใช่ git repo โชว์เป็นแถวสีเทาบอกเหตุ ไม่ใช่หายเงียบ
  - engine ใหม่ `git_status.py` (ไม่มี PyQt — เทสแยกได้) เรียก git ผ่าน subprocess ที่มี timeout, ไม่เด้งหน้าต่าง console บน Windows, และ**ไม่ raise ทุกกรณี** (git หาย / timeout / repo พัง → แถว error)
  - อ่าน git ใน **worker thread** เสมอ ไม่บล็อก UI · refresh เมื่อสลับโปรเจค/สลับมาแท็บ Git/กดปุ่ม ↻ และทุก 30 วิ **เฉพาะตอน dock เปิดและแท็บ Git ถูกเลือกอยู่** (ยุบ dock หรือสลับแท็บ = หยุด poll ทันที)

### Changed (เปลี่ยน)
- **Task List โชว์เฉพาะโปรเจคของ tab ที่เปิดอยู่** (ตามที่ user ขอ) — เดิมไล่ทุกโปรเจคใน `projects.json` มากองรวมกันในลิสต์เดียว ทำให้หางานของโปรเจคตัวเองไม่เจอ · ตอนนี้ผูกกับ tab ที่ active: สลับโปรเจค card เปลี่ยนตาม, card ของโปรเจคอื่นถูก**เอาออกจริง** (รวม avatar ใน rail ตอนยุบ) และ event `ledgerChanged` ของโปรเจคอื่นถูกข้าม

### Fixed (แก้)
- **#134 (HIGH) งานที่เพิ่ง spawn ถูก paste ซ้ำ 2 ก้อนติดกันจนกลายเป็นข้อความเพี้ยน** — pane ที่ preload task ผ่าน system-prompt จะได้ trigger สั้นๆ 1 บรรทัด แต่ตัวกู้ paste มองว่าหาย เลย paste ซ้ำ กลายเป็น `...block now.Start the current task from the one-shot system-prompt block now.` แล้ว submit ทั้งก้อน (หลักฐาน: `task_deliver_repaste` 2 ครั้ง 10:24:34 / 10:32:18 ตามหลัง `spawn_initial_task_preloaded` ทันที บน 1.0.38 ที่มี fix #133 ครบแล้ว)
  - สาเหตุ: ข้อความสั้นบรรทัดเดียว **ไม่มี `[Pasted text]` placeholder** ให้ตรวจ พอ CR ลงไปแล้วช่องพิมพ์ก็ว่าง → สัญญาณ "ready + ช่องว่าง" แยกไม่ออกระหว่าง *ส่งไปแล้ว* กับ *ยังไม่ได้ส่ง* → เดาผิดข้างเดียวก็ paste ทับทันที
  - trigger ตัวนี้เปลี่ยนไปใช้โหมด **CR-only** (`allow_repaste=False`) — กด Enter ซ้ำได้ แต่**ห้าม paste ซ้ำเด็ดขาด** · ถ้า trigger หายจริง idle watchdog (`[auto-reminder]`) ยังกู้ให้เหมือนเดิม · **การส่ง task ปกติไม่เปลี่ยน** (ยังกู้ paste ที่ถูกกลืนจริงตาม #26)
  - เพิ่ม log `task_deliver_verify_decision` ที่จุดตัดสินใจกู้ทั้ง 4 จุด (บันทึก ready / มีข้อความค้าง / มี output ใหม่ / เงียบมากี่วินาที) — ครั้งหน้ามีหลักฐานตรงๆ ไม่ต้องเดาจากจำนวนครั้งที่ retry
- **#135 hook ของ plugin หมดเวลาแล้วผลถูกทิ้งตอนเปิดหลาย pane พร้อมกัน** — ขึ้นข้อความแดงในจอ pane ว่า `UserPromptSubmit hook timed out after 5s — output discarded` · ต้นเหตุคือ plugin ตั้ง timeout ของตัวเองไว้ต่ำ (pordee = 5 วินาที ทั้ง `SessionStart` และ `UserPromptSubmit`) ซึ่ง node เย็นๆ ตอน fan-out หลาย pane ทำไม่ทัน
  - ก่อน inject plugin เข้า pane cockpit จะ **ยก timeout ที่ต่ำกว่า 30 วินาทีขึ้นเป็น 30** ให้อัตโนมัติ (ปรับได้ด้วย env `TAKKUB_PLUGIN_HOOK_TIMEOUT_FLOOR`) ทั้งใน `.claude-plugin/plugin.json` และ `hooks/hooks.json` · เขียนแบบ atomic และ**เขียนเฉพาะตอนค่าเปลี่ยนจริง** · ทำใหม่ทุกครั้งที่ spawn เพราะ plugin update ทับค่ากลับ · manifest พังหรือเขียนไม่ได้ = ข้ามเงียบ ไม่ทำให้ spawn ล้ม · มี log `plugin_hook_timeout_raised` ตอนยกจริง

## [1.0.38] - 2026-07-28

### Fixed (แก้)
- **#133 (HIGH) เปิดหลาย pane พร้อมกันแล้ว pane ค้างไม่เริ่มงาน** — fan-out จาก Multi mode ทำให้ trigger `Start the current task from the one-shot system-prompt block now.` ถูก **paste ทับซ้อน 4 ก้อนในช่องพิมพ์และไม่เคยถูก submit** · Lead เองก็ได้ notice ซ้ำและ **ตัวอักษรเพี้ยน** (`isolawted`, `fkontend-2`, `Imerge`) จากการเขียนชนกันใน PTY เดียว · เกิดทุกครั้งที่ fan-out
  - **สาเหตุที่ 1 — ตัดสินด้วยนาฬิกาที่หยุดเดิน:** ตัวตรวจว่า paste หายหรือไม่ อ่านจากสถานะที่ Qt main thread เป็นคนอัปเดต (จอที่ render แล้ว, timestamp ของ output ล่าสุด, ช่วง grace) · fan-out ทำให้ event loop ค้างทีละ ~1 วินาที แล้ว timer ของ verify ที่ค้างคิวไว้จะ **ยิงพรวดพร้อมกันทันทีที่คิวคลาย** ทำให้ grace ที่ตั้งใจให้กินเวลาจริงหลายวินาทียุบเหลือหลักมิลลิวินาที — paste ที่แค่ยังวาดไม่เสร็จจึงถูกตัดสินว่าหาย แล้วโดน repaste ทับ (หลักฐานจริง: `task_deliver_repaste` 3 ครั้ง/pane ห่างกัน ~1 วินาที ขนาบด้วย `main_thread_stall` 938ms / 1125ms / 1141ms)
  - ตอนนี้เช็ค heartbeat ของ main thread ก่อนทุกครั้งที่จะตัดสิน ถ้าเพิ่งค้างจะ **เลื่อนการตัดสินโดยไม่กินโควตา repaste** (จำกัด 4 ครั้ง) — paste ที่หายจริงยังกู้ได้เหมือนเดิม
  - **สาเหตุที่ 2 — เขียนชนกัน:** log มี `remaining:3` ซ้ำ = มี verify chain 2 เส้นเขียนเข้า Lead พร้อมกัน · `_pump_lead_notify` กับ `_force_deliver_done_notices` ใช้ guard ร่วมกันแล้ว ปล่อยคืนผ่าน callback ตอน chain จบ ทำให้เขียนได้ทีละเส้น · เคสที่ถูกกันไว้จะไม่ดึงคิวออก ของยังอยู่ครบให้รอบถัดไปส่ง

## [1.0.37] - 2026-07-27

### Fixed (แก้)
- **#132 (HIGH · แก้ของที่หลุดไปกับ 1.0.36) boot sweep อาจลบงานที่ยังไม่ commit** — `prune_orphan_worktrees_boot()` รันเองทุกครั้งที่เปิด cockpit แล้วลบ orphan worktree **ทั้งโฟลเดอร์** โดยตัดสินจาก metadata ล้วน (ไม่มี `.git` / resolve repo ไม่ได้ / ไม่อยู่ใน `git worktree list`) ซึ่งไม่ได้แปลว่าไม่มีอะไรจะเสีย — worktree ที่ `.git` pointer หายแต่ยังมีไฟล์ที่ไม่เคย commit จะหายถาวรโดยไม่มีใครสั่ง · เคสจริง: ตอนเก็บกวาดพื้นที่พบ orphan 3 ตัว โดยตัวหนึ่งมีไฟล์ที่ไม่ใช่ node_modules อยู่ ~96 MB
  - sweep อัตโนมัติลบได้เฉพาะสิ่งที่ **สร้างใหม่ได้แน่นอน**: โฟลเดอร์ว่าง หรือมีแต่ `node_modules` และต้อง clean + ไม่มี commit ค้างบน branch
  - orphan ที่ยังมีไฟล์ source ถูกเลื่อนไปหมวดใหม่ `orphan-worktrees-review` (ระดับ review) ต้องสั่งเองด้วย `takkub prune --level review --category orphan-worktrees-review --yes` และ CLI **พิมพ์ path ที่จะหายให้เห็นก่อนลบเสมอ**
  - `classify_worktree()` เติม `dirty`/`branch`/`ahead` ให้เคสที่ยังอ่าน git ได้ แทนที่จะ return ทันทีที่ path หลุดจาก `git worktree list`
- **#130 `delivery-unconfirmed` เตือนผิดทุกครั้งที่ pane กำลังทำงาน** — เกณฑ์เดิมคือ "ไม่ถึง ready prompt ใน 90 วินาที" ซึ่ง pane ที่กำลังรันงานยาว (เช่น full test suite 6 นาที) ก็เข้าเงื่อนไขเดียวกัน · วันที่ 2026-07-27 เตือนผิด **6 จาก 6 ครั้ง** (ทุกเคส `takkub status` แสดง state=working และจบด้วย done ปกติ) ทำให้ notice นี้ไร้ความหมาย และมันยังสั่งให้ Lead re-assign ซึ่งจะส่งงานซ้ำให้ pane ที่ทำอยู่
  - แยก busy ออกจาก stuck ด้วยสัญญาณ PTY output (provider-agnostic — codex/agy เหมือน claude ไม่ต้อง parse ข้อความ) เตือนเฉพาะเมื่อ pane เงียบต่อเนื่องเกิน `STALL_THRESHOLD_SEC` ซึ่งเป็นเกณฑ์เดียวกับที่ `takkub status` ใช้บอกว่า pane stalled
  - มีเพดานรวม `BUSY_WAIT_CEILING_SEC` (default 1800s · env `TAKKUB_BUSY_WAIT_CEILING_SEC`) กัน pane ที่ **ค้างแบบมีเสียง** (TUI วน redraw แต่ไม่รับ input) รอไม่รู้จบจนไม่มีใครรู้ — ครบเพดานจะเตือนด้วยข้อความและ log event **คนละแบบ** กับเคสเงียบ จะได้ไม่วินิจฉัยผิดทาง
- **#131 เทส watchdog flaky บน CI** — `test_single_instance_watchdog.py` ใช้ `sleep()` เวลาคงที่แล้ว assert ผลของ daemon thread ทันที ซึ่งแพ้ runner ที่ CPU ถูกแชร์ (แดงแล้วผ่านเองตอน re-run โค้ดชุดเดิม) · เปลี่ยนเป็น poll-until-condition (เพดาน 5 วินาที) 3 จุด และขยาย margin ให้เคสที่พิสูจน์ว่า "ไม่เกิด" ซึ่ง poll ไม่ได้ · ยืนยันด้วยการรัน 15 รอบ รวม 5 รอบภายใต้ CPU stress

## [1.0.36] - 2026-07-27

### Added (ใหม่)
- **`takkub disk` / `takkub prune` — ดูว่าอะไรกินพื้นที่ แล้วเลือกลบได้** (`disk_usage.py`) · `disk` รายงาน DATA_HOME แยกหมวดพร้อมป้ายความปลอดภัย **safe / review / never** (+`--json`) · `prune` **dry-run เป็นค่าเริ่มต้น** ต้อง `--yes` ถึงลบจริง เลือกได้ด้วย `--category` / `--level` / `--older-than` · หมวด `never` (venv, config, tasks, worktree ที่ยังใช้งาน) ถูกปฏิเสธเสมอแม้สั่งเอง · ทุกเป้าหมายถูก resolve แล้วเช็คว่าอยู่ใต้ DATA_HOME จริง กัน path escape
  - **หมวด `node-modules`** — สแกน `node_modules` ทุกชั้นใต้ `worktrees/` แยก **orphan** (worktree ที่ git ไม่รู้จักแล้ว = ลบได้เลย) ออกจากตัวที่ยังใช้งาน (ข้ามโดยอัตโนมัติ ต้อง `--include-live` เอง) — เคสจริงจากเครื่อง user: `~/.agent-takkub` บวมถึง **14.5 GB / 472,973 ไฟล์** โดย 8.1 GB เป็น worktree ซาก
  - **Windows long-path** — ลบผ่าน `\\?\` extended-length prefix + ปลด read-only แล้วลองซ้ำ และ **รายงานไฟล์ที่ลบไม่ออกเสมอ ไม่เงียบแล้วบอกว่าสำเร็จ**
  - boot auto-prune กวาด orphan worktree (เฉพาะระดับ safe) ต่อจาก transcript/browser-profile prune เดิม
- **agy conversation แยกตามโปรเจคแล้ว (#132)** — เดิม cockpit ไม่เคยส่ง `--project` ให้ agy ทำให้ทุกบทสนทนาตกลงถัง `default-cli-project` ถังเดียว (วัดจริง: **212 conversation จาก 11+ โปรเจคปนกัน**) กด resume ทีเดียวเห็นทุกโปรเจค · ตอนนี้ resolve project id จาก registry ของ agy เองแล้วส่ง `--project <id>` (ไม่เจอ → `--new-project` ซึ่ง agy จะจดโฟลเดอร์ให้เอง = สร้างครั้งเดียวต่อ cwd) · กันสร้างซ้ำตอน spawn ขนานด้วย in-process claim + poll registry แบบมี timeout · ทำผ่าน `ProviderSpec` (`project_scope_flag/_resolver/_new_flag`) ไม่ hardcode ชื่อ provider ใน spawn path

### Changed (ปรับ)
- **เตือน context น้อยลงมาก** — เดิมมี 3 ช่องซ้อนกัน (badge 80% + session cap + **paste ข้อความเตือนเข้า pane**) และ cap เป็นเลขตายตัว 180k ไม่ผูกกับ context window จริง ทำให้ session 1M โดนเตือนตั้งแต่ใช้ไปราว 18% (พบใน log 21 ครั้ง prompt 503k–538k) · ตอนนี้ **cap = สัดส่วนของ context window จริง** (default 0.85 · `0` = ปิด · เลขเดิมแบบ token ยังใช้ได้) · **ตัด tray toast ทั้ง 2 จุด** · **ตัดการ paste advisory เข้า pane ทิ้ง** (CLI auto-compact จัดการเองอยู่แล้ว) เหลือ status bar บรรทัดเดียวตอนข้าม cap · badge %/สีบน pane header + tab ยังอยู่ครบ

### Fixed (แก้)
- **#129 token meter อ่าน session ของ pane อื่น เมื่อหลาย role ใช้ cwd เดียวกัน** — โค้ดเลือก "ไฟล์ JSONL ใหม่สุดใน cwd" แทนที่จะยึด session uuid ของ pane ตัวเอง โดย docstring เขียนสมมติฐานไว้ว่า "one-pane-per-cwd ทำให้ปนกันไม่ได้" ซึ่งไม่จริงกับโปรเจค single-repo · เห็นสดๆ ตอน qa pane เพิ่ง spawn 2 วินาทีแล้วถูกรายงานว่าใช้ 185k tokens (เลขของ Lead) · ตอนนี้ยึด `<uuid>.jsonl` ตรงๆ ตามกติกา exact-uuid-หรือไม่แสดงเลย (ไม่เดา)
- **CI แดงมาตั้งแต่ 2026-07-24 โดยไม่มีใครเห็น** — `pyproject` ขอ `ruff>=0.7` แบบลอย ทำให้ CI ลง **0.16.0** ขณะที่เครื่อง dev อยู่ 0.15.12 และ pre-commit ปักไว้ 0.15.13 → gate ในเครื่องผ่าน แต่ CI ล้มที่ `ruff check` **ก่อนได้รัน pytest เลย** (รวมถึง commit ที่ release 1.0.32/1.0.33/1.0.35) · ตรึง `ruff==0.16.0` ให้ตรงกันทั้ง 3 ที่ + กัน ruff ไปไล่ `worktrees/**` (เครื่อง dev lint 503 ไฟล์ ขณะที่ CI เห็น 362) และ `*.md`
- **เทสที่ผูกกับสภาพเครื่องที่รัน** — พอ CI รัน pytest ได้อีกครั้งจึงโผล่มา: path แบบ Windows ที่ต้อง lowercase (แดงบน Linux ซึ่ง case-sensitive ถูกต้องแล้ว), เทสที่เรียก binary `codex` จริง, `TERM` ที่รั่วจาก shell ของ host, และ macOS runner ที่มี Chrome จริงที่ `/Applications` ชนะ fixture เสมอ (แยก path ออกเป็น const ให้ monkeypatch ได้ ค่า default ไม่เปลี่ยน) · รวมถึง `~/.gemini` จริงที่หลุดเข้าเทส 6 จุด
- **PtySession re-export หลุดหาย** ทำให้เทส 112 ตัวพัง — `orchestrator.py` เป็น re-export façade โดยเจตนา (มี per-file F401 ignore กำกับ) แต่ import ถูกลบเพราะดูเหมือนไม่ถูกใช้ · คืนกลับ + เพิ่มเทสที่ทำให้การลบ re-export แดงที่จุดเดียวแทนที่จะพัง 112 จุดกระจาย

## [1.0.32] - 2026-07-24

### Fixed (แก้)
- **#126 agy pane "ตายเงียบ" — task ค้างใน composer ไม่ถูก submit** — ระหว่าง agy ขึ้น "Signing in / Verifying your account" (Google eligibility check ฝั่ง server) จอยังโชว์ idle footer ทำ cockpit เข้าใจว่า ready → paste แล้ว Enter โดน swallow → pane นั่งเงียบจน user ต้องสั่งซ้ำเอง (เจอ 3 เคสใน 1 วัน) · ตอนนี้ marker ทั้งสองเป็น **ready hard-blocker**: cockpit รอให้พ้น check ก่อนส่ง + submit verification/resend หลังพ้น (calibrate จาก transcript จริง แนวเดียวกับที่เคยแก้ codex #99)

## [1.0.31] - 2026-07-24

### Fixed (แก้)
- **#125 (HIGH) agy pane เปลี่ยน model เองเมื่อเจอ `--effort`** — regression จาก 1.0.29: role tier effort ที่ inject ให้ agy ชนกับ model ที่ user ตั้งไว้ (slug ของ agy encode effort อยู่แล้ว เช่น `gemini-3.1-pro-low`) แล้ว agy ตอบโต้ด้วยการ**ทิ้ง model ไปใช้ default เงียบๆ** · ตัด `effort_flag` ฝั่ง agy ทิ้ง (หลักการ: flag เสริมห้ามทำ model เปลี่ยน) — claude/codex ยังได้ effort ตาม tier ปกติ
- **`browser_chrome` อ่าน env จริงของ host ทั้งที่ caller ส่ง dict ว่าง** — `env or os.environ` ทำ `{}` (falsy) fallback ไป host env → `CHROME_BIN` รั่วเข้า resolution · แก้เป็น None-check + regression test

## [1.0.30] - 2026-07-24

### Added (ใหม่)
- **Lead Inbox Digest** — done notices + peer CC ที่มาเป็น burst ถูกรวมส่งเข้า Lead เป็น**ข้อความเดียว** (window 60 วิ · `TAKKUB_INBOX_DIGEST_MS`, `0` = ปิดกลับพฤติกรรมเดิม) ลดการปลุก Lead ให้ลาก context เต็มซ้ำต่อ notice (~5.1M tokens/7วันจาก audit) · `[FAILED]` / spawn-failed / delivery-unconfirmed **ไม่เข้า digest** ส่งทันทีและแซงคิว · auto-chain handoff ถูกแนบต่อท้าย digest ใน turn เดียวกัน — ลำดับ "เห็น done ก่อน act" คงเดิม
- **เลือก model ต่อ assign ได้** (`takkub assign --model <id>`) — ยิงงาน scan/audit รอบแรกด้วยรุ่นถูก (haiku/flash) แล้ว escalate เป็นรุ่นใหญ่เฉพาะรอบ final ตามกลยุทธ์ Hybrid Tiered Scanning · precedence: **assign > role > provider > CLI default** (ชนะ `TAKKUB_TEAMMATE_MODEL` — แคบสุดชนะ) · provider ที่ไม่มี `model_flag` = error ชัดตอน assign ไม่เงียบ · มีผลเฉพาะ pane ที่ spawn ใหม่
- **Browser QA ผ่าน `mb` ใช้ได้ทุก provider บน Windows (#123)** — cockpit จัดการ Chrome lifecycle เอง (module ใหม่ `browser_chrome.py`: launch native / reuse / cleanup ผ่าน CDP 9222 ตามทางที่พิสูจน์ empirical ใน issue) · `takkub doctor --fix` ติดตั้ง mb ให้ (run ปกติ = read-only) · #92 (mb+shard ชน CDP) บังคับที่ `pane_guard` แล้ว ไม่ใช่แค่ prose

### Changed (ปรับ)
- **Idle reminder ไม่ปลุก model แล้ว** — เตือน pane ที่ลืม `takkub done` ผ่าน **UI notice (status bar + tray)** แทนการ write+Enter เข้า PTY (เดิมปลุก model turn เต็ม ~296k tokens/ครั้ง) ใช้ได้ทุก provider (#103) · ยังมี escalation: idle ต่อเนื่องเกิน N รอบ (`TAKKUB_IDLE_REMIND_ESCALATE_ROUNDS`, default 3, `0` = UI-only ตลอด) ค่อย inject PTY หนเดียว — pane ที่เสร็จจริงแล้วเงียบยังถูกดันให้รายงาน

### Fixed (แก้)
- **#121 codex pane ค้าง "Starting MCP servers" ทั้งที่ policy ไม่ให้ MCP** — MCP รั่วจาก `~/.codex/config.toml` ระดับ user เพราะ codex merge config table · ตอนนี้ role ที่ policy deny-all จะถูกปิดรายตัวผ่าน `codex mcp list --json` + `features.plugins=false` (session-scoped, fail-closed, ไม่แตะไฟล์ user) · provider อื่นยังไม่มี surface เทียบเท่า — ระบุ gap ใน spec (#103)
- **`$TAKKUB_ARTIFACTS_DIR` หายจาก pane gemini** — ย้ายการ stamp เข้า env builder (`pane_env`) โดยตรง provider branch ที่ early-return ก็ไม่ทำหลุดอีก
- **#124 ปิดแบบ invalid diagnosis** — "one-shot delivery ไม่ทำงาน" ที่รายงานไว้ แท้จริง pointer เป็น by-design ของ role ที่ map เป็น codex/agy (ไม่มี system-prompt-file) — claude pane preload ปกติมาตลอด · ได้ของแถม: assign event มี `initial_delivery_reason` แยกเหตุชัด (provider-unsupported / fallback-after-fail / preloaded) + retry/queue path hardening + integration test 3-pane ผ่าน QTimer จริง

## [1.0.29] - 2026-07-24

### Added (ใหม่)
- **ลด token ชุดแรก (จาก audit ข้อมูลจริง 7 วัน = 4.34B tokens, 98.25% เป็น cache reads)** — report เต็มที่ `docs/qa-reports/2026-07-24-token-audit.md`:
  - **session-cap watchdog** (`session_cap.py`) — pane ที่ prompt ทะลุ cap (default 180k · `TAKKUB_SESSION_CAP_TOKENS` / QSettings) เตือนแบบ edge-trigger: teammate ได้ advisory ให้จบงานก่อนแล้ว `/compact` (รอ ready prompt เสมอ ไม่ตัดกลางงาน) · Lead ได้แค่ UI notice ไม่ auto-compact เด็ดขาด · เฉพาะ claude pane (provider อื่นไม่มี JSONL ให้อ่าน — #103 gap ระบุแล้ว)
  - **one-shot spawn task delivery** — assign ที่ spawn pane ใหม่ฝัง task เต็มเข้า `--append-system-prompt-file` + trigger สั้นๆ แทน pointer→Read round-trip (~60k tokens/spawn) · pane รันอยู่/provider อื่น = pointer เดิม · **known gap #124: ยังไม่ engage ตอน staggered fan-out**
- **reasoning effort ต่อ provider** — role tier effort มีผลกับ pane ที่ไม่ใช่ claude แล้ว: agy `--effort` (1.1.5+) · codex `-c model_reasoning_effort=` · opencode/kimi/cursor ไม่มี surface = ระบุ gap ใน spec (#103)
- **Team Settings redesign** (👥 Team) ตาม control-plane mockup — ธีมดำ+gold เดิม: sidebar หมวด+SVG icons · heading kicker · card rows · matrix legend/sublabel/separator/empty-state · sticky footer พร้อม dirty dot · ผ่าน qa 3 รอบ + critic verify SHIP
- **Thai font fallback ทั้งแอป** — bundle Noto Sans Thai (OFL) + per-OS stack (Win: Leelawadee UI · mac: Thonburi) แก้ข้อความไทย/glyph เป็นกล่อง tofu จากการประกาศ font family เดียว

### Fixed (แก้)
- **#120 restart port-file drift** — successor ของ `takkub restart` เคยสืบทอด `TAKKUB_PORT_FILE` ของ PID เก่า ทำ CLI วิ่งผิด instance ("unauthorized: lead-only") · ตอนนี้ค่าที่ app ตั้งเองถูก strip (provenance marker) ส่วน override จริงจาก shell ยังรอด
- **#117 over-capacity advisory เตือนเกินจริง** — budget ลดจาก 2GB → 0.5GB/pane (วัดจริง ~350MB) + ฐาน RAM ใช้ `max(available, 25% ของ total)` กัน sample แกว่ง
- **#118 delivery-unconfirmed false positive** (ส่วนที่เหลือจาก 1.0.26) — claude pane ที่ policy ให้ MCP (qa/critic/designer) ได้ ready-wait 90s เท่า provider ช้า + ข้อความเตือนรายงานเลข wait จริง
- **ลูกศร dropdown ของ QComboBox กลับมาแสดง** — สไตล์ `::down-arrow` โดยไม่มี `image:` ทำ Qt ลบลูกศร native ทิ้ง ใส่ SVG glyph ตรงๆ (พร้อม state เปิด/disabled)

## [1.0.28] - 2026-07-23

### Fixed (แก้)
- **pane เดินอ้อม tool policy ผ่าน shell ไม่ได้อีกแล้ว** — `pane_tools_policy` คุมแค่ **MCP** แต่ทุก pane spawn ด้วย `--dangerously-skip-permissions` แปลว่า **Bash ไม่มี gate เลย**. pane `frontend` ที่ถูกปฏิเสธ browser MCP จึงติดตั้ง browser เองด้วย `npx --yes playwright` แล้วขับ Chromium จาก ad-hoc script (จับได้สดๆ 2026-07-23 พร้อม `find / -maxdepth 6 -iname playwright` ที่สแกนทั้งไดรฟ์จนเครื่องกระตุก). ปิดทางที่ถูกต้องโดยไม่ปิดทางอ้อม = agent เดินอ้อมเอง.
  - **module ใหม่ `pane_guard.py`** (pure leaf, stdlib ล้วน) — 2 rule: `browser_driver` (playwright / puppeteer / selenium / headless chrome ทุกช่องทาง: `npx`, `npm|pnpm|yarn|bun install|add|dlx`, `pip install`, `python -m`, bare invoke, inline `require()`/`import`, `chrome --headless`) และ `disk_scan` (`find /`, `find C:\`, `Get-ChildItem <root> -Recurse`)
  - **บังคับจริงที่ hook** — `takkub _guard` ต่อเป็น `PreToolUse`/`Bash` ให้ทุก claude pane (unconditional, เรียงก่อน rtk) · deny ด้วย **exit code 2 + เหตุผลทาง stderr** ซึ่งเป็น contract ที่ Claude Code ทุก build เข้าใจ (JSON `permissionDecision` เสี่ยง fail-open เงียบ)
  - **`BROWSER_ROLES` = qa / critic / designer** เท่านั้นที่ขับ browser ได้ — shard (`qa#3`) ได้สิทธิ์ตาม role แม่ · `lead` / `shell` ไม่โดน guard (user พิมพ์เอง)
  - **fail-open ทุกทาง** — role ว่าง / command ว่าง / payload พัง / guard เองพัง = ปล่อยผ่านเสมอ. hook นี้ยิงทุก Bash call จึงห้ามทำ pane ค้างเด็ดขาด
  - **อ่านยังได้หมด** — `grep playwright`, `ls ~/AppData/Local/ms-playwright`, `cat package.json` ไม่โดนบล็อก บล็อกเฉพาะ **ติดตั้ง/รัน**
- **นับ "ตำแหน่งกำลังทำงาน" บนมือถือถูกต้อง** — `renderPulse` เดิมนับจาก `roles` อย่างเดียว พอ `roles` ว่างจะขึ้น "0 ตำแหน่งกำลังทำงาน" ทั้งที่ chip Lead หมุนอยู่ตรงนั้น ตอนนี้นับ Lead ที่ working ด้วย และ Lead ที่ idle ยังได้ card ("Lead ว่าง")

### Changed (ปรับ)
- **remote-control mirror เฉพาะ Lead** (`remote/config.py: LEAD_ONLY_STREAM = True`) — เดิม teammate ทุกตัวไปโผล่บนมือถือ 2 ทาง: เป็นแถวใน `/api/activity` และเป็น `done` SSE ต่อ 1 งาน. fan-out ปกติ (frontend + backend + qa + reviewer บางทีมี shard) = notification ระเบิดเรื่องงานที่ user มอบหมายไปแล้วเพื่อจะได้ไม่ต้องนั่งดู. Lead สรุปงานทีมให้อยู่แล้ว traffic ของ teammate จึงเป็นของซ้ำล้วนๆ บนจอ 6 นิ้ว
  - `api.activity()` ยัง emit key `roles` แต่**ว่างเสมอ** (ลบ key ทิ้งจะพัง PWA build เก่าที่อ่าน `p.roles.length`)
  - `notify._on_done` drop done ของ teammate, ของ Lead ยังผ่าน
  - เปลี่ยน `LEAD_ONLY_STREAM = False` = ทีมทั้งหมดกลับมาเหมือนเดิม (มีเทสกันไว้ทั้ง 2 ทิศ)
- **role file 16 ไฟล์เพิ่มหมวด "Browser & เครื่องมือหนัก (บังคับ)"** — 13 role ได้ข้อห้าม + ชี้ทางส่งต่อให้ qa, ส่วน qa/critic/designer ได้ข้อความอนุญาต + เตือนอย่าลง browser ซ้ำ (cache บนเครื่อง dev บวมถึง 2.88 GB / chromium 4 builds). **#103:** Claude Code hook มีเฉพาะ claude — pane ที่รัน codex / gemini-agy / opencode / kimi / cursor บังคับด้วย prose ตรงนี้เท่านั้น

### Added (ใหม่)
- **เทสใหม่** — `test_pane_guard.py` (87 เคส รวม false-positive ทั้งชุด), `test_cli_guard.py` (18 เคส wiring + fail-open), `test_agent_role_files_have_browser_guard.py` (กัน prose ↔ `BROWSER_ROLES` drift), `test_hook_wiring.py::TestGuardInjection`
- **`docs/architecture/godfile-map.md`** เพิ่ม hidden edge 3 เส้น — guard เชื่อมด้วย **string ในไฟล์ settings + PATH** ไม่มี import edge เลย, tool policy 2 ชั้นคนละกลไก, และ `BROWSER_ROLES` ↔ role-file prose

## [1.0.27] - 2026-07-22

### Added (ใหม่)
- **เตือนทันทีถ้าติดตั้งแบบไม่ใส่ `-g`** — postinstall ตรวจว่าเป็น local install แล้วบอกตรงๆ ให้ลงใหม่ด้วย `npm install -g agent-takkub`. เดิมการลงแบบ local จะ "สำเร็จ" เงียบๆ แต่คำสั่ง `takkub` ไม่เคยขึ้น PATH เลย (bin shim กับ PATH provisioning ผูกกับ npm global bin dir) — ผู้ใช้เจอแค่ "command not found" โดยไม่รู้สาเหตุ.

### Changed (ปรับ)
- **`description` ของแพ็กเกจบอกวิธีติดตั้งแบบ global แล้ว** — หน้า npm และผลค้นหาแสดง `description` จาก package.json ซึ่งเดิมไม่ได้บอกว่าต้องใส่ `-g` คนที่เห็นจาก npm โดยไม่เปิด README จึงพลาดได้ง่าย (README บอกไว้ครบอยู่แล้ว).

### Fixed (แก้)
- **role file ที่ขาดของ kimi / cursor** — ทั้งคู่ถูกเพิ่มเป็น forced role ใน 1.0.26 แต่ไม่มี `.claude/agents/<role>.md` เลย เมื่อ CLI ยังไม่ได้ติดตั้ง (สถานะปกติของเครื่องส่วนใหญ่) pane จะ degrade เป็น claude substitute แล้วหาไฟล์ role ไม่เจอ → ไม่ได้รับ SPECIALIST OVERRIDE → เสี่ยงอ่าน CLAUDE.md ของโปรเจคแล้วทำตัวเป็น Lead แทนที่จะเป็น teammate ที่ทำงานเองแล้วรายงาน. เพิ่ม stand-in role file ครบทั้ง kimi/cursor และ track `opencode.md` ที่ค้างอยู่.

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
