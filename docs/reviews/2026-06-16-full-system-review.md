# agent-takkub — Full System Review (v0.7.0 → vNEXT)

**วันที่:** 2026-06-16 · **วิธี:** multi-agent workflow — 10 review lens × adversarial verify + completeness critic + targeted gap review
**สเกล:** 92 agents · 4.3M tokens · 840 tool-uses · ~56 นาที
**ผล:** 61 raw findings → **66 confirmed** (54 ผ่าน verify รอบแรก + 12 จาก gap follow-up)
**กระจาย:** robustness 23 · perf 13 · security 9 · arch 7 · token 7 · bug 4 · dx 3

> **บริบทที่ใช้ตัดสิน severity:** single-user desktop · Claude Max OAuth (ไม่ billing ต่อ token → "ประหยัด token" = ประหยัด **context window** ไม่ใช่เงิน) · single-tenant local-trust (security = กัน pane ที่ถูก confuse/compromise + local malicious process ไม่ใช่ remote attacker). ทุก finding ถูก verifier เปิด code อ่านที่จุดจริงก่อนยืนยัน.

---

## 1. Executive summary

มี **2 finding ระดับ HIGH ที่กระทบ user ตอนนี้จริง:**

1. **`takkub harvest` ใช้ไม่ได้เลย (dead on arrival)** — payload IPC ทั้ง 2 ตัวไม่ stamp `from` role → role gate reject ก่อนถึง token check (`cli.py:271,314` ↔ `cli_server.py:250-254`) ถูกบังด้วย test ที่ inject `from=lead` เอง เลยไม่เคยจับได้
2. **Terminal render เปิด/รันไฟล์ได้ด้วยคลิกเดียว** จาก output ที่ pane (agent) ควบคุม — ไม่มี filter นามสกุล executable, ไม่ confine path, ไม่มี dialog ยืนยัน (`terminal_widget.py:382-406`)

ที่เหลือเป็น med/low: robustness gaps, security defense-in-depth, token-injection hygiene, architecture debt

---

## 2. 🪙 TOKEN SAVINGS (dimension หลักตามที่ขอ — เรียงตาม impact)

ทุกตัววัดเป็น token/spawn (หรือ /respawn) คิดกับ **context window** (Max ไม่ billing):

| # | จุด | location | ประหยัด | fix |
|---|---|---|---|---|
| tok-1 | **cockpit CLAUDE.md (~6800 tok) ถูก append verbatim เข้า Lead system prompt ทุก spawn** | `lead_context.py:223,382` + `orchestrator.py:2093` | **~3000-4000 tok/spawn** | แยก core (lean) + lazy appendix |
| tok-4 | **project CLAUDE.md ถูก inject ซ้ำ 2 รอบ** (auto-discover จาก cwd + append เอง) | `lead_context.py:274-299` + `orchestrator.py:1812` | **~750 tok/spawn** | skip เมื่อ spawn cwd == project root |
| tok-3 | session goal ไม่ bound — re-paste เข้าทุก assign (worst 64KiB) | `orchestrator.py:2524-2561` | สูงสุดมหาศาล | bound 2-4KB |
| tok-5 | role-memory skeleton+wrapper inject แม้ว่าง | `orchestrator.py:1886-1910` | ~100-150 tok/teammate spawn | skip-on-empty |
| tok-6 | codex/gemini cheatsheet ซ้ำ prose dev-server ที่ env บังคับอยู่แล้ว | `gemini_md.py`/`codex_agents_md.py` | ~200 tok/spawn | dedup |
| tok-7 | hop-start boilerplate ซ้ำทุก hop | `orchestrator.py:3223-3234` | ~30-40 tok/hop | hoist |

**รวม quick-win: ~4000-5000 tok/Lead spawn + 100-350/teammate spawn**

> ⚠️ **tok-2 (git boilerplate ซ้ำ 8 role files) = ประหยัด 0 ต่อ spawn** เพราะ `agent_role_dir` อ่านเฉพาะ role file ของตัวที่ spawn → เป็น maintenance cleanup ไม่ใช่ token win (verifier ตัด claim ลง)

---

## 3. Correctness / Robustness / Security

### Correctness
| sev | finding | location | fix |
|---|---|---|---|
| **HIGH** | `takkub harvest` dead — ไม่ stamp `from` | `cli.py:271,314` | เพิ่ม from stamp 2 จุด + e2e test ผ่าน dispatch จริง (Lead-direct) |
| MED (was HIGH) | auto-chain verify hop ไม่ fire ถ้า pane จบทาง crash-cap/give-up แทน done/close — clear flag แต่ไม่ recompute pending (degrade ไม่ hang เงียบ มี Lead warning) | `orchestrator.py:2434, 5089` vs `3808, 3584` | shared helper เรียกครบ 4 จุด |
| MED (was HIGH) | pipeline-run ตอบ `ok=true` เสมอ — schedule แล้วตอบทันที ทิ้ง false-with-message | `cli_server.py:482-491` | pre-check template+hops ก่อน schedule |

### Security
| sev | finding | location | fix |
|---|---|---|---|
| **HIGH** | render คลิกเดียวเปิด/รันไฟล์ (.bat/.cmd/.hta/.lnk/.ps1) จาก pane buffer ไม่ filter/confine/confirm | `terminal_widget.py:382-406` | ตัด file scheme, ปฏิเสธ exec ext (ใช้ reveal-in-folder), confine ใต้ cwd/repo, เพิ่ม confirm |
| MED (was HIGH) | outbound render ไม่ sanitize — `term.write` ตรง + proposed API เปิด → DCS/APC/OSC52 clipboard/title จาก pane ไม่น่าเชื่อถือถึง emulator (เป็น scraper handshake surface ด้วย) | `write_bytes`/`flush_writes` | filter outbound, drop OSC52, ปิด proposed API |
| MED | decision-note เขียนลง Obsidian vault verbatim (ต้องมี pane token + note ไม่ junk) → escape replay ตอน cat + corrupt ไฟล์เดียว | vault writer | scrub note+role, neutralize frontmatter dash นำ, cap length |
| MED | sanitizer strip แค่ ESC/CR/paste-marker ไม่ strip 8-bit C1; short message เขียน raw | sanitizer | 1 บรรทัด strip control range ยกเว้น newline (Lead-direct) |
| MED | `status` คืน transcript tail + screenshot path ทุก pane โดยไม่ auth (harvest lead-only แล้ว แต่ status/list เปิด) | `cli_server` status | gate transcript+screenshot หลัง token |

### Robustness (MED เด่น)
- `terminate` block GUI thread ได้ถึง **6 วินาที** (sync taskkill + 2 thread join) — `pty_session.py:427-478` → offload/detach
- readiness markers hardcode อังกฤษ ไม่มี override/central — `pty_session.py:580-621`; upstream reword → ทั้ง provider stall (เกิดมาแล้ว 3 ครั้ง) → central marker table + env override + doctor self-test
- shard fan-out group in-memory + QTimer → restart กลาง fan-out = orphan (per-shard warning ยังถึง Lead) → persist+rehydrate
- brick-guard updater `sleep 3s` แทนรอ process exit — `claude_update.py:336,343` → race brick CLI → ส่ง PID + poll exit
- updater install fail เงียบ ไม่เช็ค exit code/ไม่มี relaunch guard → capture exit code + sentinel
- done handler รัน sync `git status` timeout 10s บน main thread (requires-commit) → offload/ลด timeout
- Low: blind CC flush pop-before-write · double-done re-notify · pump-flag wedge · missing stuck liveness gate · unbounded in-session transcript

---

## 4. Architecture seams (incremental ปลอดภัย ไม่ rewrite)
- **arch-1 (HIGH-arch):** `spawn()` เป็น method **920 บรรทัด** 4 provider branch copy-paste มี drift จริง (codex มี exit handler เอง, shell ข้าม auto-trust) — `orchestrator.py:1371-2292` → extract `_launch_session` helper (fold arch-2/4/5 + tok-1 ได้)
- **arch-2 (MED):** token mint+revoke ritual ซ้ำ 4-5 จุด inline — `orchestrator.py:1516-1964`
- **LOW:** stale-guard lambda ซ้ำ 3x · `MainWindow` คลาสเดียว 3640 บรรทัด · pane-geometry magic number ซ้ำ 4x · install hint ซ้ำ · dead keep-alive import

---

## 5. Root-cause clusters (leverage สูงสุด — แก้ที่ราก)
- **A — Open-loop control plane:** อาศัย scrape terminal text ล้วน ไม่มี machine handshake (robust-1,2,3,5,6) → **central marker table + bottom-row anchoring**
- **B — Untrusted bytes ข้าม boundary โดยไม่ sanitize สม่ำเสมอ:** (sec-1,2,render-1/2,w1/2) sanitizer กันแค่ paste-into-pane ส่วน render/disk/header sink ไม่กัน → **shared strict C0+C1 scrubber ที่ทุก sink**
- **C — spawn monolith → duplication+drift:** (arch-1,2,5,4,tok-1) → `_launch_session` helper
- **D — Blocking I/O + subprocess บน Qt main thread:** (bug-3, robustness-1, perf-1/6/3) = **freeze pain อันดับ 1** → offload ไป worker
- **E — Token injection ไม่มี lazy-load+dedup:** (tok-1,3,4,5 + robustness-1) → split core/appendix, skip-empty, bound size

---

## 6. Prioritized action plan (quick-win ก่อน)

| # | งาน | finding | impact/effort | ใคร | sev |
|---|---|---|---|---|---|
| 1 | stamp `from` ใน harvest payload + e2e test | bug-1 CLI | high/low | **Lead-direct** | HIGH |
| 2 | strict C0+C1 scrub ใน sanitizer | sec-1 | med/low | **Lead-direct** | MED |
| 3 | ตัด file scheme + ปฏิเสธ exec ext + confine ใน open-path | sec-render-2 | high/med | PR frontend | HIGH |
| 4 | split CLAUDE.md core + lazy appendix | tok-1 | high/med | **Lead-direct** | MED |
| 5 | skip project-rules inject เมื่อ cwd == project root | tok-4 | med/low | PR backend | LOW |
| 6 | gate transcript+screenshot ใน status หลัง token | sec-3 | med/med | PR | MED |
| 7 | scrub note+role ก่อนเขียน vault | sec-w1 | med/low | PR | MED |
| 8 | auto-chain handoff helper เรียกครบ 4 จุด | bug-1 orch | med/med | PR | MED |
| 9 | pre-check pipeline template+hops | bug-1 routing | med/low | PR | MED |
| 10 | outbound escape filter ก่อน term.write | sec-render-1 | med/med | PR frontend | MED |
| 11 | bound goal+task + skip-on-empty role memory | robustness-1, tok-5 | med/low | PR | MED-LOW |
| 12 | central provider marker + env override + anchoring | robust-1,2,3 | med/high | PR | MED |
| 13 | offload main-thread blockers | bug-3, robustness-1, perf-6 | med/med | PR | MED |
| 14 | brick-guard รอ PID exit + capture exit code | bug-1,2 update | med/med | PR devops | MED |
| 15 | persist+rehydrate shard groups | robust-4 | med/med | PR | MED |
| 16 | extract `_launch_session` helper | arch-1 | high/high | PR | HIGH-arch |

> ข้อ 1,2,4 = Lead-direct (อยู่ใน cockpit + เล็ก/doc) อะไรแตะ >1 ไฟล์ หรือ >30 บรรทัด → PR

---

## 7. Open gaps + next cross-check (ยังไม่ครอบคลุม)
1. **doctor/issues/services/verify/docs-verify** (~1850 บรรทัด, 0 findings, shell out หมด) — เช็ค: tool ช้าแต่ healthy ถูก mislabel ตอน timeout ไหม · issue string จาก agent ส่งเข้า `gh` CLI ปลอดภัยไหม · verify/docs-verify รัน detected-stack command = command injection ถ้า stack/config ถูก influence ไหม → **cross-check reviewer + qa**
2. **inbound bracketed-paste breakout** — paste terminator/newline หลุด paste mode → auto-submit attacker text เข้า pane ที่รัน tool → **cross-check reviewer**
3. **vault write-side ที่เหลือ** — role-memory เก็บ plaintext test credential, project name เป็น path component ไม่ validate → **cross-check reviewer**

**Honest uncertainty:** C1/OSC findings ขึ้นกับว่า terminal ปลายทาง honor sequence ไหม (defense-in-depth ไม่ใช่ exploit พิสูจน์แล้ว) · facet HIGH ของ robust-1 ถูก defuse ด้วย spinner gate + 45s idle streak เลยเหลือ low · bug-3 worst 10s ต้อง git tree ผิดปกติ · ตัวเลข 6800-tok ของ CLAUDE.md เป็น conservative (bytes/4, ภาษาไทย tokenize สูงกว่า 1 tok/char)

> **Cross-check note:** workflow นี้ verify ด้วย Claude (independent perspective) ไม่ใช่ model อื่น — สำหรับ model-diversity จริง แนะนำ spawn codex pane review top-5 ซ้ำ หรือ `/code-review ultra`
