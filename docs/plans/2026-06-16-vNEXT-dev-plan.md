# agent-takkub — Dev Plan สู่ vNEXT (v0.8.0)

**ที่มา:** `docs/reviews/2026-06-16-full-system-review.md` (66 confirmed findings, multi-agent + adversarial verify)
**วันที่:** 2026-06-16 · **หลักการจัดลำดับ:** quick-win/high-value ก่อน → แก้ตาม root-cause cluster → arch debt → verify gaps → release
**Owner key:** `L` = Lead-direct (cockpit/doc/เล็ก) · `PR` = teammate + PR (>1 ไฟล์ หรือ >30 บรรทัด หรือ specialist context)
**กฎ verify:** จบแต่ละ milestone รัน `pytest tests/ -q` ทีเดียว (batch verify), reviewer ตอน PR time

---

## 🌙 Overnight progress — 2026-06-16 (branch `feat/vnext-hardening`)

**✅ M0#1 + M0#2 เสร็จ + test ผ่าน (2008 passed) + commit แล้ว** บน branch `feat/vnext-hardening` (ยังไม่ merge main รอ restart-verify)
- M0#1 harvest `from`-stamp fix + 2 e2e regression test
- M0#2 sanitizer C0/C1/DEL strip + 4 test

**⚠️ M0#3 (CLAUDE.md split) — DEFER + แก้ความเข้าใจผิด:** verifier บอก tok-1 ว่า cockpit CLAUDE.md "double-loaded" → **เป็น FALSE POSITIVE** อ่าน code จริงพบ `orchestrator.py:1822` guard: `_render_lead_context` (ตัว inject `base`) ถูกเรียก**เฉพาะตอน Lead cwd ≠ REPO_ROOT** (ทำ project อื่น) ซึ่ง Claude Code auto-load **project CLAUDE.md** (ไม่ใช่ cockpit) → การ inject cockpit CLAUDE.md **จำเป็น ไม่ใช่ของซ้ำ**. tok-1 ที่แท้จริง = CLAUDE.md ตัวมันใหญ่ (6.8k) ควร restructure core/lazy — แต่ต้องใช้ **วิจารณญาณคุณ**ว่าอะไร load-bearing → defer

**✅ เพิ่มเติมที่ทำเสร็จ (safe + test-gated, commit แล้ว):**
- **tok-3** bound session goal ที่ set-time (cap 4000 chars, กัน 64KiB re-paste) + 2 test
- **bug-1 routing** pipeline-run pre-check ก่อน async ack (เลิกตอบ ok=true เสมอ) — `orch.pipeline_precheck()` + 3 test
- **sec-w1** scrub agent note ก่อนเขียน vault (strip C0/C1/DEL + defuse leading `---` + cap) + 4 test
- **bug-1 orch** auto-chain handoff release ตอน crash-cap + stuck-give-up (เดิม deadlock ถ้า blocker ตัวสุดท้ายตายไม่ส่ง done) — extract `_maybe_fire_auto_chain_handoff` เรียก 4 จุด + 6 test
- **M4#21 + bug-2** updater รอ cockpit PID exit จริง (แทน sleep 3s race) + capture install exit code → sentinel `.failed` (เดิมเงียบ) + tests
- **🐴 ponytail** (safe path — ไม่ลง plugin): ดูด rules จริงของ [ponytail](https://github.com/DietrichGebert/ponytail) (MIT) "lazy senior dev / minimal-code" ใส่ role file `frontend/backend/mobile/devops` + `reviewer` (over-engineering lens). ไม่แตะ Node-hook (กัน brick). per-pane โหลดแค่ role ตัวเอง → 0 token เพิ่ม

**⏸️ DEFER (ต้อง checkpoint คุณ):**
- **tok-4/5/6** + M0#3 — แตะ context-injection ที่ behavioral-sensitive, verify ไม่ได้ถ้าคุณไม่อยู่ (tok-5: role-memory มี seed skeleton เสมอ, detect "ว่างจริง" พลาด = suppress note จริง)
- **M2** (offload main-thread) — threading race ไม่โผล่ใน test เสมอ
- **M3 #13/#14/#16, M4 #17/#18/#20/#21, M5** — เสี่ยงสูง / blast radius กว้าง ต้อง restart-verify

**สรุป:** ทำ **10 work commit** ที่ปลอดภัย+test-gated (2023 passed) — harvest, sanitizer, ponytail, tok-3, pipeline pre-check, vault scrub, auto-chain release, updater PID-wait. หยุด autonomous ตรงนี้เพราะที่เหลือเข้าโซนที่ต้อง restart-verify หรือ refactor path sensitive (สะสมเสี่ยงถ้า stack blind)

**🔜 รอ restart-verify session (ทำกับคุณ ทีละ checkpoint):**
- **medium** (ทำได้ test-gated แต่แตะ path sensitive): tok-4/5 (context inject), M5#24 (token mint/revoke refactor in spawn), M4#20 (persist shard groups), M3#16 (status gate — ระวัง status bar), tok-7
- **🔴 high-risk** (verify เดี่ยว ห้าม stack): M4#17 marker-table, M5#23 spawn 920-บรรทัด refactor, M2 main-thread offload, M3#13 exec-hardening, M3#14 outbound filter, M0#3 CLAUDE.md restructure

---

## 🟢 M0 — Quick wins (Lead-direct, รอบเดียว จบไว)

1. **fix `takkub harvest` dead** — เพิ่ม `from` stamp ใน payload IPC ทั้ง 2 จุด (`cli.py:271,314`) + e2e test ผ่าน dispatch จริง · **HIGH bug** · `L`
2. **C0+C1 control-byte scrub** ใน sanitizer (strip 8-bit C1 + short-path raw write) ยกเว้น newline · **MED security** · `L`
3. **split cockpit CLAUDE.md → core (lean) + lazy appendix** ลด ~3,000-4,000 tok/Lead spawn (`lead_context.py:223,382` + `orchestrator.py:2093`) · **token #1** · `L`

> จบ M0 → รัน test → commit

## 🪙 M1 — Token diet (cluster E ที่เหลือ)

4. **skip double-inject project CLAUDE.md** — inject ซ้ำ 2 รอบ, skip เมื่อ spawn cwd == project root (`lead_context.py:274-299` + `orchestrator.py:1812`) ~750 tok/spawn · `PR backend`
5. **bound session goal + task** — re-paste ทุก assign worst 64KiB → cap 2-4KB (`orchestrator.py:2524-2561`) · `PR`
6. **skip-on-empty role memory** — เลิก inject skeleton+wrapper เมื่อว่าง (`orchestrator.py:1886-1910`) ~100-150 tok/teammate · `PR`
7. **dedup codex/gemini cheatsheet** — ตัด prose dev-server ที่ env บังคับอยู่แล้ว (`gemini_md.py`/`codex_agents_md.py`) ~200 tok/spawn · `PR`
8. **hoist hop-start boilerplate** — ซ้ำทุก hop (`orchestrator.py:3223-3234`) ~30-40 tok/hop · `PR`
9. **dedup git boilerplate ใน 8 role files** — ⚠️ ประหยัด 0 ต่อ spawn (อ่านแค่ role เดียว) = **maintenance cleanup เท่านั้น** จัด priority ต่ำสุด · `L`

## ⚡ M2 — De-block Qt main thread (cluster D = freeze pain อันดับ 1)

10. **offload `terminate`** — block GUI ได้ถึง 6s (sync taskkill + 2 thread join) → worker/detach (`pty_session.py:427-478`) · **MED robustness** · `PR`
11. **offload done-handler `git status`** — sync 10s timeout บน main thread (requires-commit) → QThreadPool worker หรือลด timeout (`bug-3`) · `PR`
12. **audit + offload main-thread blocker ที่เหลือ** — ตั้ง invariant "no blocking I/O on Qt main thread" + กวาด perf-3/perf-6 call site เป็น 1 workstream · `PR`

## 🛡️ M3 — Sanitization boundary (cluster B = security)

13. **one-click exec hardening** — ตัด `file` scheme, ปฏิเสธ exec ext (.bat/.cmd/.hta/.lnk/.ps1 → reveal-in-folder แทน), confine path ใต้ cwd/repo, เพิ่ม confirm dialog (`terminal_widget.py:382-406`) · **HIGH security** · `PR frontend`
14. **outbound render escape filter** — `write_bytes`/`flush_writes` ป้อน `term.write` ตรง → filter outbound, drop OSC52 clipboard, ปิด proposed API · **MED security** · `PR frontend`
15. **scrub vault decision-note** — scrub note+role, neutralize frontmatter dash นำ, cap length ก่อนเขียน Obsidian · **MED security** · `PR`
16. **gate `status` transcript+screenshot หลัง token** — ตอนนี้คืน transcript tail + screenshot path ทุก pane ไม่ auth · **MED security** · `PR`

## 🔁 M4 — Control-plane robustness (cluster A = marker open-loop)

17. **central provider marker table + env override + doctor self-test + bottom-row anchoring** — ตอนนี้ marker hardcode อังกฤษ กระจาย, upstream reword = ทั้ง provider stall (เกิด 3 ครั้ง) (`pty_session.py:580-621`) · **MED robustness, leverage สูง** · `PR`
18. **auto-chain handoff helper เรียกครบ 4 จุด** — crash-cap(2434)/give-up-stuck(5089) clear flag แต่ไม่ recompute pending (`orchestrator.py` vs `3808,3584`) · `PR`
19. **pipeline-run pre-check reply** — ตอบ `ok=true` เสมอ ทิ้ง false-with-message (`cli_server.py:482-491`) → pre-check template+hops ก่อน schedule · `PR`
20. **persist + rehydrate shard fan-out group** — in-memory + QTimer, restart กลาง fan-out = orphan (`robust-4`) · `PR`
21. **brick-guard updater รอ PID exit + capture exit code** — `sleep 3s` แทนรอ process (race brick) + install fail เงียบ (`claude_update.py:336,343`) · `PR devops`
22. **เก็บ low-robustness ชุด** — blind CC flush (pop-before-write) · double-done re-notify · pump-flag wedge · missing stuck liveness gate · unbounded in-session transcript · `PR`

## 🏗️ M5 — Architecture seams (cluster C — incremental, gated by ~2000 tests)

23. **extract `_launch_session` helper** — `spawn()` 920 บรรทัด 4 provider branch copy-paste + drift จริง (codex exit handler, shell auto-trust) (`orchestrator.py:1371-2292`) · fold arch-2/4/5 + tok dedup เข้าด้วย · **HIGH-arch** · `PR`
24. **extract token mint+revoke helper** — ซ้ำ 4-5 จุด inline (`orchestrator.py:1516-1964`) · `PR`
25. **low arch cleanup** — stale-guard lambda ซ้ำ 3x · pane-geometry magic number ซ้ำ 4x → named const · install hint ซ้ำ · dead keep-alive import · `L/PR`
26. **(ระยะยาว) แตก `MainWindow` 3640-บรรทัด** + orchestrator god-object เป็น collaborator (1 ต่อ PR ไม่ rewrite) · `PR` track แยก

## 🔍 M6 — Gap closure + cross-check (verify ที่ยังไม่ครอบคลุม)

27. **audit doctor/issues/services/verify/docs-verify** (~1850 บรรทัด 0 findings) — timeout mislabel healthy tool? · issue string → `gh` CLI ปลอดภัย? · verify/docs-verify รัน detected-stack command = **command injection** ถ้า stack/config ถูก influence? · `PR reviewer + qa`
28. **inbound bracketed-paste breakout** — paste terminator/newline หลุด paste mode → auto-submit attacker text เข้า pane รัน tool · `PR reviewer`
29. **vault write-side ที่เหลือ** — role-memory เก็บ plaintext test credential · project name เป็น path component ไม่ validate · `PR reviewer`
30. **model-diversity cross-check** — spawn codex pane review top-5 findings ซ้ำ (กัน Claude confirmation bias) หรือ `/code-review ultra` · `codex pane`

## 📦 M7 — Release vNEXT

31. **CHANGELOG (ไทย)** สรุป M0-M6 ลง `[vNEXT]` → bump version v0.8.0 → `takkub release` (push + gh release) · `L`

---

## 📋 Appendix — deferred / low-confidence (ไม่ทิ้ง แต่ไม่เร่ง)

ไม่ขึ้น milestone หลัก เพราะ verifier ตัด severity ลงหรือ guarded อยู่แล้ว — เก็บไว้รอบ debt:
- **sec-2** from-role header (ต้องมี lead token อยู่แล้ว) · **sec-w2/w4/w5** (guarded by validate-name) · **sec-render-3** termWrite marshal (UI-DoS only) · **ipc-1** single-secret auth (optional hardening, same-user trust)
- **perf-1/perf-2** (verdict ว่า overstated/throttled แล้ว) · **perf-4/perf-5** · cockpit-runtime FS sweep (off-thread แล้ว ไม่กิน model token)
- routing/CLI allowlist drift (ถูกต้อง ณ วันนี้) · double-done bug-4 · pump-flag wedge bug-5

---

## สรุปการพึ่งพา (dependency)
- M0 อิสระ ทำได้ทันที · M1 อิสระ (token) · M2 อิสระ (perf)
- **M5 #23 (`_launch_session`) ควรทำหลัง M1** (token dedup จะถูกดูดเข้า helper พอดี — ไม่ทำซ้ำงาน)
- M3 #13 (HIGH security) ทำได้ขนานกับทุกอย่าง · M4 #17 (marker table) ปลด root cause A ทั้งก้อน
- M6 = verify หลัง impl · M7 = ปิดท้าย
