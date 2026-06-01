# Code Review — PaneState refactor (reviewer)

- **Scope:** `git diff c2c71f8 -- src/agent_takkub/orchestrator.py` (+202 / −167, 1 file)
- **Nature:** pure refactor — รวม ~14 per-pane state dict เป็น `PaneState` dataclass เดียว (`_pane_state[key]`), helper `_ps(key)` (get-or-create), teardown เหลือ `_pane_state.pop(key)` ครั้งเดียว
- **เป้าหมาย review:** behavior preservation (ต้องเหมือนเดิมเป๊ะ)
- **Snyk:** ไม่ได้รัน — `snyk` ไม่ได้ติดตั้งใน PATH. Diff เพิ่ม import เดียว `from dataclasses import dataclass` (stdlib) ไม่มี dependency ใหม่ → ไม่มี supply-chain surface ใหม่
- **Test evidence:** `pytest` 13 ไฟล์ที่เกี่ยว (session_uuid, auto_respawn_replay, stuck_recover, lifecycle_recovery, rate_limit_watchdog, done_gate, auto_chain, peer_cc_durability, cross_tab_done, stall, project_scoping, session_resume, codex_crash) → **204 passed**. `test_delivery_unconfirmed.py` → **8 passed**

## สรุป: ✅ ไม่พบ blocker, ไม่พบ major

Refactor นี้ **faithful และระมัดระวังมาก** — ทุก field default, membership semantics, teardown ตรงกับ dict เดิม. รายงานราย point ด้านล่าง.

---

## รายละเอียดตาม review point

### 1. read ใช้ `.get()`+fallback / write ใช้ `_ps()` — มี read-via-`_ps()` leak ไหม? ✅ ไม่มี

ตรวจ `self._ps(` ทั้ง **15 จุด** — ทุกจุดเป็น **write path** (เขียน field ทันทีหรือเป็น get-or-create ที่ตามด้วยการเขียน):

| line | call | path |
|---|---|---|
| 1200 | `_ps(_ekey).codex_spawn_ts = …` | write |
| 1482 | `_ps_new = _ps(_ekey_spawn)` → set session_uuid/cwd | write |
| 1533 | `_ps(_ekey_spawn).last_spawn_resumed = …` | write |
| 1658 | `ps = _ps(key)` (auto_respawn) → set attempts/auto_chain/last_assigned_task | write |
| 1772 | `ps_assign = _ps(key)` (assign) → set last_assigned_task | write |
| 2146 | `_ps(…).last_send_ts = …` | write |
| 2181 | `_ps(…).blocked_on_lead_ts = …` | write |
| 3281 | `_ps(key).harvest_hint_ts = now` | write |
| 3319 | `ps_ck = _ps(key)` (stuck check) → set content_hash/change_ts/stuck_recover | write |
| 3388 | `_ps(key).last_stuck_recover = now` | write |
| 3410/3414/3416/3418 | `_do_respawn` restore | write |
| 3483 | `_ps(key).rate_limited_until = …` | write |

ทุก **read-only path** ใช้ `self._pane_state.get(key)` + guard None อย่างถูกต้อง (spawn:1010, idle watchdog:3227/3265, rate_limit:3466, compute_progress:2661, can_resume:1467). **ไม่มีจุดไหน read ผ่าน `_ps()` แล้วสร้าง entry เกินจำเป็น** → membership/leak ไม่เพี้ยน.

### 2. PaneState defaults (702–748) ตรงกับ `dict.get(key, default)` เดิมทุก field? ✅ ตรง

| field | default ใหม่ | default เดิมตอน read |
|---|---|---|
| session_uuid | None | `.get(key)`→None ✓ |
| session_uuid_cwd | "" | ใช้เฉพาะตอน entry absent; can_resume require `prior_uuid is not None` อยู่แล้ว ✓ |
| blocked_on_lead_ts | None | `.get(key)`→None ✓ |
| rate_limited_until | 0.0 | `.get(key, 0.0)` ✓ |
| auto_respawn_attempts | 0 | `.get(key, 0)` ✓ |
| last_assigned_task | None | `.get(key)`→None ✓ |
| requires_commit_on_done | False | `.get(key, False)` ✓ |
| auto_chain | False | `.get(key, False)` ✓ |
| last_stuck_recover | 0.0 | `.get(key, 0.0)` ✓ |
| codex_spawn_ts | None | `.pop(ekey, None)` ✓ |
| last_send_ts | 0.0 | `.get(key, 0.0)` ✓ |
| harvest_hint_ts | 0.0 | `.get(hint_key, 0.0)` ✓ |
| last_content_hash | None | `.get(key)`→None ✓ |
| last_content_change_ts | None | `.get(key, last_out)` → **จัดการแยก** (ดู point 3) |
| last_spawn_resumed | False | `.get(key, False)` ✓ |

**`last_content_change_ts`** เป็น field เดียวที่ default ต่าง (None ใหม่ vs `last_out` ในการ read เดิม) — แต่ migrate **ถูกต้อง**: code อ่านแล้ว fallback `if last_content_ts is None: last_content_ts = last_out` (line 3346–3348) → semantics เท่าเดิม.

### 3. semantics ที่เปลี่ยนได้ (membership / pop / iterate / None vs 0.0/False) ✅ รักษาครบ

- **membership `key in dict`:** `blocked_on_lead_ts` default None ⟺ "absent" เดิม; check `if blocked_at is not None` (3229) คุมทั้งสองกรณี. ✓
- **`setdefault` → None-sentinel (stuck detect, 3333–3348):** เดิม `_last_content_change_ts.setdefault(key, last_out)` เขียนเฉพาะตอน key absent. ใหม่ใช้ `elif ps_ck.last_content_change_ts is None:` — เนื่องจากเดิม field นี้ถูก set เป็น `now`/`last_out` (non-None) เท่านั้น ไม่เคยเป็น None → `is None` ⟺ "absent" เป๊ะ. รวมถึง except-path ก็ map ถูก. ✓
- **iterate ทั้ง dict (auto_chain pending, 2430–2433):** เดิม iterate `_auto_chain_panes` (มีแต่ key auto-chain). ใหม่ iterate `_pane_state` ทั้งหมดแล้ว filter `and s.auto_chain` → ได้ set เดียวกัน. current key ถูก pop ที่ 2391 ก่อนถึงจุดนี้ → ไม่นับตัวเอง (เหมือนเดิมที่ `pop(key)` ก่อน iterate). ✓
- **`.pop(key, default)`:** spawn-clear (1010–1014) อ่าน get ก่อน set None/0 เฉพาะถ้า entry มี → เทียบเท่า pop-on-absent. ✓

### 4. close() (2224–2225) / done() (2390–2391) teardown ครบ? ✅ ครบ

- **close():** `_idle_state.pop(key)` + `getattr(self,"_pane_state",{}).pop(key)` → 13 dict pop เดิม → 1 pop เดียว. ไม่ตกfield (ทุก field อยู่ใน PaneState เดียว). ✓
- **done():** อ่าน `had_requires_commit`/`had_auto_chain` **ก่อน** pop (2349–2351), แล้ว pop `_idle_state`+`_pane_state` (2390–2391). ลำดับ read-before-pop ถูกต้อง. auto_chain pending check (2429) ใช้ state หลัง pop → current key ไม่ติดมา. ✓
- `getattr(self,"_pane_state",{})` ใน close/done = defensive สำหรับ `__new__` fixture. ✓

### 5. เว้น `_idle_state` + `_recent_exits` ไม่ merge — เหตุผลถูกไหม? `_recent_exits` รอด close() จริงไหม? ✅ ถูกทั้งคู่

- **`_idle_state` แยก:** ถูกต้อง — watchdog/tests พึ่ง key-presence (`pop` ต้องลบ key จริง ไม่ใช่ reset field). ถ้า merge เข้า PaneState การ pop ทั้ง state จะกระทบ field อื่น. เก็บแยกถูกแล้ว.
- **`_recent_exits` แยก:** ยืนยัน close() (2223–2225) **ไม่แตะ** `_recent_exits` → entry รอดจริง. m3 crash-resume (`_do_respawn` synthesise ที่ 3423–3427 + spawn can_resume ที่ 1465) อ่าน `_recent_exits` หลัง close() ได้ → เหตุผลใน docstring ตรงกับพฤติกรรมจริง. ✓

### 6. `_session_uuids` dict {uuid,cwd} → split `session_uuid` + `session_uuid_cwd` — call site แปลงถูกหมด? ✅ ถูก

- **spawn can_resume (1465–1483):** `prior_uuid` = `.session_uuid`, `prior_uuid_cwd` = `.session_uuid_cwd`; check `prior_uuid_cwd == spawn_cwd`; `--resume prior_uuid` (เดิม `prior_uuid["uuid"]`). write new: set ทั้ง 2 field. ✓
- **`_do_respawn` snapshot (3380–3385):** `snap_uuid` = `.session_uuid` (str), `snap_uuid_cwd` = `.session_uuid_cwd` แยกเก็บ. restore (3409–3412) set ทั้ง 2 field. ✓
- **`_do_respawn` synthesise (3423–3427):** จุดเสี่ยงสุด — เดิม `snap_uuid.get("cwd", cwd or "")`. ตอนนี้ `snap_uuid` เป็น str แล้ว → ใช้ `snap_uuid_cwd or cwd or ""` ถูกต้อง ไม่มี `.get()` ค้างบน str (ซึ่งจะ AttributeError). ✓
- **rollback on spawn fail (3433–3443):** reset `.session_uuid=None`, `.session_uuid_cwd=""`. ✓
- **startup `_resume_last_session` (2974–3002):** comment-only change; เขียนแค่ `_recent_exits` ไม่ตั้ง session_uuid → spawn ออก `--session-id` fresh (no bleed). ✓

---

## Findings (minor เท่านั้น)

### Minor 1 — test ตกหล่นจากการ migrate: `tests/test_delivery_unconfirmed.py:108,119`
```python
orch._blocked_on_lead = {}   # ← dead attribute หลัง refactor
```
`spawn()` ไม่อ่าน `_blocked_on_lead` แล้ว (ใช้ `_pane_state[key].blocked_on_lead_ts`). บรรทัดนี้กลายเป็น no-op ที่สร้าง attribute ตายทิ้งไว้ — **ไม่ทำให้ test fail** (test path return ที่ "could not create pane" ก่อนถึงจุดอ่าน state; และ test ไม่ assert อะไรเกี่ยวกับ blocked) แต่ misleading. ไฟล์นี้ไม่อยู่ใน git-status modified list = refactor พลาดอัปเดต.
**Fix:** ลบ 2 บรรทัดนั้น หรือเปลี่ยนเป็น `orch._pane_state = {}` ให้สื่อเจตนาตรง.

### Minor 2 — `_ps()` docstring over-claims robustness ของ `__new__` fixture
Docstring (722–724): *"Lazily initialises `_pane_state` so test fixtures that create a bare `Orchestrator.__new__` instance ... still work."* — จริงเฉพาะกับ `_ps()` เอง. แต่ **direct read** `self._pane_state.get(...)` ที่ spawn:1010, idle watchdog:3227/3265, rate_limit:3466, compute_progress:2661 **ไม่มี guard** (ต่างจาก close()/done() ที่ใช้ `getattr(...,{})`). `__new__` fixture (test_delivery_unconfirmed:44, test_rate_limit_watchdog:72, test_peer_cc_durability:72, test_cross_tab_done:65) ที่ไม่ตั้ง `_pane_state` แล้ว drive spawn ผ่าน pane-creation สำเร็จ หรือ trigger watchdog จะโดน `AttributeError`.
- **ไม่ใช่ bug ปัจจุบัน:** 212 tests ผ่านหมด — fixtures ปัจจุบันไม่แตะ path เหล่านั้นด้วย bare instance, และ production รัน `__init__` เสมอ (มี `_pane_state` แน่นอน).
- **เป็น latent fragility:** test `__new__` ในอนาคตที่ exercise spawn-to-success/watchdog จะ crash แบบ AttributeError แทนที่จะ degrade.
**Fix (เลือกอย่างใดอย่างหนึ่ง):** (ก) ใส่ guard `getattr(self,"_pane_state",{})` ที่ direct read 5 จุดให้สม่ำเสมอ หรือ (ข) ลด claim ใน docstring ให้ตรง (lazy-init คุ้มเฉพาะ `_ps()` write path).

---

## บทสรุป
Pure refactor นี้ **behavior-preserving** — ผ่านทั้ง manual trace ทุก call site และ test suite (212 passed). ไม่มี blocker/major. มีแค่ 2 minor เรื่อง test cleanliness + docstring accuracy ที่ไม่กระทบ runtime. **อนุมัติได้** (แก้ minor หรือไม่แก้ก็ ship ได้).
