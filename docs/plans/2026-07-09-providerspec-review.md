# ProviderSpec design review — Wave 3 #6 Phase 0 (issue #103)

**Reviewer:** reviewer role · **Date:** 2026-07-09
**Doc reviewed:** `docs/plans/2026-07-09-providerspec-design.md`
**Verdict: APPROVE-with-changes** — ทิศทางถูกและ *ดีกว่า* ของเดิม (per-provider runtime marker dispatch แก้ปัญหา cross-provider collision ได้จริง) แต่ schema ยังไม่ครอบ spawn surface จริง + migration ยัง **ไม่ behavior-neutral** (มี silent behavior change) + doctor self-test/#100/#101 ยังไม่ครบ ต้องปิดก่อน implement

---

## สรุปผู้บริหาร (1 ย่อหน้า)

Registry ที่ declarative + `_classify_ready` อ่านจาก `self._provider_spec` เป็นแนวทางที่ถูก และ **ลบ collision จริง** (agy `gemini cli update available!` ⊃ codex `update available!` ที่ปัจจุบันต้อง order agy มาก่อนถึงจะไม่พัง — พอแยก per-provider แล้วปัญหานี้หายเอง = ได้กำไร) แต่ design มี 3 ปัญหาที่ต้องแก้ก่อนลงมือ: (1) **schema ตกหลาย knob จริงของ claude branch** (plugin-dir, disallowed-tools, model/effort tier, hook `--settings` flag, Lead-vs-teammate axis, `_ready_wait_ms` 90s) → เคลม "zero engine edit" เกินจริง; (2) **phase-0 ไม่ควรเปลี่ยน behavior** แต่ค่า enter-delay/self-heal ของ codex/gemini ใน spec ต่างจากที่โค้ดทำอยู่วันนี้ = regression risk (โดยเฉพาะขัดกับ #99); (3) `set()`-union backwards-compat ทำลาย order ของ `_READY_RULES` + doctor self-test ที่ case ข้าม provider ไม่ได้ระบุว่าจะ classify กับ spec ไหน. #100 (agy MCP) ถูก drop เป็น `none`, #101 (lead unlock) ตอบแค่ครึ่งเดียว (ไม่แตะ read-side JSONL coupling).

---

## Dimension 1 — schema ครอบ vendor quirks ครบไหม? (จุดที่ code มี แต่ design ไม่ mention)

Design §5 ระบุ call site 4 จุด (provider_config, spawn_engine, pty_session, orchestrator_text) แต่โค้ดจริงมี provider-specific behavior นอกนั้นอีก และ claude branch มี knob ที่ schema §2.1 ไม่มี field รองรับ:

### 1a. claude spawn branch — knob ที่ schema ตกหมด (`spawn_engine.py:1377-1575`)
- **`--plugin-dir` allowlist** (`_default_plugin_dirs(base_role)` + `TAKKUB_EXTRA_PLUGINS`, line 1470-1474) — ไม่มี field. superpowers/agent-skills ถูก hand ผ่าน `--plugin-dir` เพราะ `--setting-sources project,local` ตัด user settings ทิ้ง → เป็น knob บังคับของ claude
- **`--disallowed-tools Task,AskUserQuestion`** (line 1532-1546) — ไม่มี field. เป็น hard-enforce "ห้าม spawn subagent" + teammate ไม่ได้ AskUserQuestion. codex/gemini ใช้ bypass-approval แทน → ต่าง mechanism ต่อ provider จริงแต่ schema เงียบ
- **`--model` / `--effort` / `--fallback-model` per-role tier** (`_teammate_tier`, `_lead_model_override`, line 1416-1464) — ไม่มี field. codex/gemini ไม่รับ flag พวกนี้ = provider-specific แต่ไม่มีที่ให้ declare "provider นี้รับ model flag ไหม"
- **hook wiring `--settings <file>`** (`ensure_hook_settings_file()`, line 1391-1396) — schema มี `supports_hooks: bool` แต่ **ไม่มี flag name**. `--settings` เป็น path ไม่ใช่ inline JSON (Windows quoting). bool อย่างเดียว build argv ไม่ได้
- **`apply_claude_auth_overrides(env)`** (line 1360) — claude-only env, ไม่มี field

### 1b. **Lead-vs-teammate axis หายทั้งแกน** (สำคัญสุดของ dimension นี้)
ProviderSpec เป็น **per-provider** แต่ความแตกต่างครึ่งหนึ่งของ claude branch เป็น **per-role-class (Lead/teammate) ภายใน provider เดียว**: `_build_lead_env()` vs `_build_pane_env()`, `TAKKUB_LEAD_TOKEN` vs pane token, `_render_lead_context()` vs role .md appendix, cwd resolution (`lead_cwd` vs `default_cwd_for_role`), Lead ได้ AskUserQuestion / teammate โดน deny, Lead ไม่ default `--model`. Design จับแกน provider แกนเดียว → **claude spec เดียวอธิบาย behavior จริงของ claude ไม่ได้** ถ้าไม่ยอมรับตรงๆ ว่า "claude branch ยังมี logic hardcode เหลือหลัง spec" เคลม "Zero Engine Edits / no hardcoding" (§1) จะ **เกินจริง**

### 1c. `lead_inbox.py` — provider timing ที่ schema ไม่มี field
- **`_ready_wait_ms` (line 400-413):** codex/gemini รอ ready **90s** vs claude 45s — provider-specific timing ชัด แต่ schema **ไม่มี field `ready_wait_ms`** → ตกหล่น
- **`_send_when_ready` + `_delayed_enter_verified` (line 442-455):** self-heal ยิงให้ **ทุก** pane รวม gemini วันนี้ (ดู dimension 2-D)

### 1d. อื่นๆ
- **`shared_dev_tools.browser_profile_mcp_config_path(role, shard, project)`** — keyed role×shard×project จริง, `supports_browser_profiles: bool` หยาบเกิน (bool ไม่พอ build path)
- **`task_notice_preamble`** — §5.4 อ้างว่าเก็บใน ProviderSpec แต่ **field นี้ไม่มีใน §2.1 schema** = design ขัดกันเอง (call site จริง `orchestrator.py:816` gate ด้วย `effective_provider_for(...)==CODEX`)
- **substitution/degradation ไม่ mention:** `effective_provider_for` degrade codex/gemini→claude + อ่าน stand-in `.claude/agents/{role}.md`. provider ใหม่ (`opencode`) unavailable จะ degrade→claude เหมือนกันไหม? ต้องมี stand-in role file (.claude/agents/ ชื่อ opencode — ยังไม่มีจริง เป็นตัวอย่างสมมติ) ด้วยไหม? §5.1 ไม่พูด
- **`_FORCED_PROVIDER` (provider_config.py:45)** lead→claude — design ทำ `VALID_PROVIDERS` dynamic แต่ไม่แตะ forced-role table
- **`pane_env.py`** — task บอกให้เช็ค; design **ไม่ mention** เลย (passthrough allowlist ครอบ claude/codex/gemini, line 38) แต่กระทบน้อย (ส่วนใหญ่ provider-agnostic)

---

## Dimension 2 — migration ปลอดภัยจริงไหม?

### 2-A. `set()`-union ทำลาย order ของ `_READY_RULES` (blocker เชิง correctness ของ compat layer)
§5.5 โชว์:
```python
_READY_HARD_BLOCKERS = tuple(set(claude…) | set(codex…) | set(gemini…))
```
- **hard blockers:** order ไม่สำคัญ (any match → not ready) → `set()` OK ✅
- **`_READY_RULES` (ordered, first-match-wins):** ถ้าใช้ `set()` union แบบเดียวกัน **order ที่ encode precedence หาย**. มี substring collision จริง: gemini `(True,"gemini cli update available!")` **contains** codex `(False,"update available!")`. หน้าจอ agy idle "Gemini CLI update available!" → merged table ที่ codex marker มาก่อน = match `False` → gemini อ่าน **busy ตลอด** (นี่คือเหตุผลที่ comment `pty_session.py:216-223` สั่ง "keep the order")
- **ข่าวดี:** per-provider RUNTIME **ลบ collision นี้** (gemini spec ไม่มี bare `update available!`) = design ดีกว่าของเดิม ✅ **แต่** legacy compat table + doctor ที่รวม provider ยัง **ต้องคง order** → `set()` ใช้ไม่ได้กับ `_READY_RULES`. Design ต้องระบุ: compat `_READY_RULES` สร้างด้วย **ordered concat ตาม precedence เดิม** ไม่ใช่ set

### 2-B. `ready_marker_selftest()` / `_classify_ready` — migration ไม่ระบุ (verification vector #2 พัง)
`_READY_SELFTEST_CASES` (`pty_session.py:320-364`) มี case **คละ provider** (agy/gemini/codex/claude) รันผ่าน `_classify_ready` ตัวเดียว. §5.3 เปลี่ยน `_classify_ready` เป็น method อ่าน `self._provider_spec` → **doctor จะเลือก spec ไหน classify แต่ละ case?** Design ไม่พูด. ต้อง restructure: tag แต่ละ case ด้วย provider แล้ว classify กับ spec ของมัน (ไม่งั้น fall back merged table = เจอ 2-A). Verification vector #2 (`takkub doctor`) จะ **แดงหรือ degrade เงียบ** ถ้าไม่ทำ. Case line 363 ("cross-provider contamination... fast off") กลายเป็น ill-defined ต้องนิยามใหม่

### 2-C. Enter-delay = behavior change ที่ไม่ควรมีใน phase-0 (regression risk, ขัด #99)
วันนี้ `_enter_delay_ms` (`orchestrator_text.py:461`) **ไม่ parameterize per-provider** → codex/gemini ได้ **800/150/3000** เท่า claude (ยิงที่ `lead_inbox.py:454` ให้ทุก pane รวม codex task delivery). Design spec ตั้ง codex/gemini = **200/0/200** → **ลด enter delay ของ codex ลงจาก 800**
- **ไม่ถูก flag ว่าเป็น behavior change** — design เขียนเหมือนดึงค่าจากโค้ด แต่จริงๆ เป็นค่า **ใหม่ที่ต่างจากพฤติกรรมวันนี้**
- **ขัดกับ #99** (codex enter-swallow ที่อยู่ Wave 2 track เดียวกัน) — ลด delay ยิ่งเสี่ยง swallow. `[Pasted text]` render slowness เป็น claude quirk แต่ **ไม่มีหลักฐานในโค้ด/design ว่า codex render เร็วกว่า** → assumption ล้วน
- **แนะนำ:** phase-0 = pure refactor ต้อง **preserve ค่าปัจจุบัน** (ทุก provider 800/150/3000). การ tune ค่า per-provider ให้แยกเป็น change ต่างหากที่มี test/pty-capture รองรับ

### 2-D. gemini `input_swallow_recovery=False` = ถอด self-heal ที่มีอยู่ (regression risk)
`_send_when_ready` เรียก `_delayed_enter_verified` **ไม่มี provider gate** (`lead_inbox.py:451`) → gemini วันนี้ **ได้** enter-verify self-heal. Design ตั้ง gemini `input_swallow_recovery=False` = **ถอดออก**. เป็น behavior change อีกจุด ไม่ flag. phase-0 ควรคง `True` ทั้งหมด (หรือพิสูจน์ก่อนว่า gemini ไม่ต้องการ)

> **สรุป dimension 2:** direction ปลอดภัยและดีขึ้น แต่ตามที่เขียน **ไม่ behavior-neutral** (2-C, 2-D) + compat layer มี correctness bug (2-A) + verification path ไม่ระบุ (2-B). ต้องแก้ให้ phase-0 = strictly behavior-preserving ก่อน

---

## Dimension 3 — capability flags ตอบ #100 / #101 ครบไหม?

### #100 (MCP adapter) — ตอบครึ่งเดียว + abstraction รั่ว
- `mcp_adapter_variant: str` เป็น enum ปิด `{"strict","cmd_args","none"}`. **การ implement ของแต่ละ variant อยู่ใน engine code** (dispatch จาก string) → provider ใหม่ที่มี MCP mechanism แบบที่ 4 = **ต้องเขียน engine code ใหม่** = **ขัด goal "zero engine edits" โดยตรง**. bool/enum ให้ความรู้สึก pluggable แต่ behavior จริงยัง hardcode
- **agy ถูก drop:** spec ตั้ง gemini `mcp_adapter_variant="none"` แต่ plan §6 phase-0 ระบุชัด "agy plugin import — issue #100" → design **ทิ้ง agy MCP variant** ที่ #100 ต้องการ. ต้องมี variant `"plugin_import"` (agy) ไม่ใช่ `none`
- codex `"cmd_args"` = **feature ใหม่** (codex วันนี้ไม่มี MCP wiring เลย — `shared_dev_tools` line 23 "every *claude* spawn"; codex branch spawn_engine ไม่มี `--mcp-config`) → OK แต่ต้อง flag ว่า **new feature ไม่ใช่ refactor** (ต้อง test แยก)

### #101 (lead unlock) — ตอบแค่ spawn-side, ตก read-side coupling ทั้งหมด
`_FORCED_PROVIDER["lead"]=claude` เพราะ "claude-specific plumbing (CLAUDE.md, JSONL token meter, --continue resume)". Spec ครอบ spawn-side: `context_strategy`, `supports_resume`, `supports_hooks`, `supports_mirror`, `supports_slash_commands` ✅ **แต่ตก read-side ที่ผูก claude JSONL format:**
- `notify.py` (remote bridge, `_lead_user_text`, `resolve_lead_jsonl`) parse claude JSONL
- token meter (`limit_panel`) อ่าน claude session file
- remote `/api/lead/history`, resume picker (W3) — ทั้งหมดสมมติ claude transcript

Lead ที่ไม่ใช่ claude **ผลิต JSONL format นั้นไม่ได้** → subsystem พังเงียบ. Schema **ไม่มี capability สำหรับ "session-log format / parseable transcript"** → #101 **ตอบไม่ครบ**. อย่างน้อยต้อง flag ว่า lead-unlock เป็น phase หลัง + ต้องมี capability อธิบาย read-side ก่อน (หรือ note ว่า non-claude Lead จะเสีย token meter/remote history)
- `supports_slash_commands` (inject `/remote-control`) เป็น **Lead-only** behavior แต่ Lead ถูก force→claude อยู่ → flag นี้ **inert วันนี้** (premature จนกว่า #101 จะปลด lock จริง)

---

## Dimension 4 — จุด over-engineered ตัดได้

1. **JSON serialization format (§2.2) — ตัดได้** ✂️
   ไม่มี requirement ไหนขอ config-file-driven providers. Goal (§1) พูดแค่ "register a new ProviderSpec **declaration**" = Python literal พอ (ตาม §3). JSON layer เพิ่ม nested-dict schema คู่ขนาน + บังคับ `custom_discovery_fn` เป็น **string + lookup table** แทน callable ตรงๆ (`find_agy_executable` เป็นชื่อ string ต้อง resolve เอง). speculative generality → ตัดจนกว่ามี requirement user แก้ provider โดยไม่แตะ python

2. **`custom_discovery_fn: Optional[str]`** — ถ้าตัด JSON (ข้อ 1) เก็บเป็น `Callable` ตรงๆ ได้ ลบ indirection ทั้งชั้น

3. **`supports_slash_commands`** — inert (dimension 3, #101) → ใส่ตอนปลด lead lock จริงค่อยเพิ่ม ไม่ใช่ phase-0

4. **`mcp_adapter_variant` enum** — ไม่ใช่ over-engineer แต่ **under-abstract** (dimension 3 #100): ดูเหมือน declarative แต่ impl ยัง hardcode → อย่าเคลมว่า pluggable

> **ห้ามตัด (เป็นของจำเป็น ไม่ใช่ฟุ่มเฟือย):** `ready_hard_blockers`/`ready_rules` per-provider (แก้ collision จริง), `context_strategy`, capability flags ที่ gate behavior จริง — พวกนี้คือแกนที่ทำให้ refactor คุ้ม

---

## Action items ก่อน implement (เรียงตาม priority)

**ต้องแก้ (blocker):**
1. **[2-C/2-D] Phase-0 = behavior-preserving:** เก็บ enter-delay ทุก provider = 800/150/3000, `input_swallow_recovery=True` ทุกตัว (ค่าปัจจุบัน). tune per-provider แยก change ที่มี test
2. **[2-A] compat `_READY_RULES` ต้อง ordered concat** ตาม precedence เดิม — **ห้าม `set()`** (hard blockers `set()` ได้)
3. **[2-B] ระบุ migration ของ `ready_marker_selftest`:** tag selftest case ด้วย provider → classify กับ spec ของมัน (verification vector #2 ต้องเขียว)
4. **[Dim1-b] ยอมรับ Lead/teammate axis:** ระบุชัดว่า knob ไหนอยู่ใน spec / knob ไหนยัง hardcode ใน claude branch (Lead context, model tier, plugin-dir, disallowed-tools, `--settings` hook flag) — อย่าเคลม zero-hardcode เกินจริง; หรือเพิ่ม field: `plugin_dirs`, `disallowed_tools`, `settings_flag`, `model_flag`/`effort_flag`, `ready_wait_ms`, `task_notice_preamble` (ตัวหลังหายจาก §2.1)

**ควรแก้:**
5. **[Dim3 #100] agy variant = `plugin_import` ไม่ใช่ `none`**; ระบุว่า codex `cmd_args` MCP เป็น new feature (test แยก); ยอมรับว่า variant ใหม่ = engine edit (abstraction boundary)
6. **[Dim3 #101] flag read-side coupling:** non-claude Lead เสีย token meter/notify/remote history — lead-unlock ต้องมี capability อธิบาย transcript format ก่อน; `supports_slash_commands` เลื่อนไป phase นั้น
7. **[Dim1] mention call site ที่ตก:** `lead_inbox._ready_wait_ms` (90s), substitution/degradation + stand-in role file สำหรับ provider ใหม่, `_FORCED_PROVIDER`

**ตัดได้:**
8. **[Dim4] ตัด JSON serialization (§2.2)** + `custom_discovery_fn` เป็น callable ตรงๆ จนกว่ามี requirement config-driven

---

## Verdict: **APPROVE-with-changes**

Core architecture ถูกและ **ดีกว่า** ของเดิม (per-provider marker dispatch แก้ collision จริง, registry ลด coupling) — ไม่ถึงขั้น REDESIGN. แต่ต้องปิด action item 1-4 (blocker) ก่อน implement: phase-0 ต้อง **behavior-neutral จริง** (ตอนนี้ยังไม่ใช่), compat layer ต้องคง order, doctor self-test ต้องมี migration path, และ scope ของ schema ต้องซื่อสัตย์ว่า claude branch ยังเหลือ hardcode. #100 (agy) + #101 (read-side) ตอบไม่ครบ — flag เป็น follow-up phase ไม่ใช่ปิดใน phase-0
