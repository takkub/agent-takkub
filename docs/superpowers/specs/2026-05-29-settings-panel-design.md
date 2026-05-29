# Unified Settings panel — design

**Date:** 2026-05-29
**Owner:** Lead
**Status:** spec, ready for implementation plan
**Estimated effort:** ~4–6 ชม. (single PR)

---

## 1. Goal

ยุบ config controls ที่กระจายอยู่ใน status bar ให้เหลือ **ประตูเดียว** — ปุ่ม `⚙` ตัวเดียวเปิด `SettingsDialog` ที่รวมทุก config ของ cockpit ไว้ในหน้าเดียว และเปลี่ยนกลไกการ "แจ้ง Lead ที่รันอยู่" จากการ broadcast `[system]` message (soft enforcement — พึ่ง AI เชื่อฟัง) ไปเป็นการ **restart Lead pane จริง** (deterministic — model pin ใหม่มีผลทันที)

**ที่มา:** user สับสนว่าทำไม plan/provider toggle ต้อง "ส่งข้อความไปหา AI" (เห็น `[system] account plan set to ...` เด้งใน Lead pane) คำตอบคือ broadcast เป็นแค่การแจ้ง live session เพราะ model pin มีผลแค่ตอน spawn — แต่ user เห็นว่ามันงงและเปราะ จึงเลือกแก้เป็น settings panel + restart-on-change แทน

**Non-goals:**
- ไม่ rebuild `RoleProviderDialog` / `ClaudeAuthDialog` เป็น inline widget — เปิดเป็น sub-dialog ผ่านปุ่ม (reuse code เดิม risk ต่ำ); inline เป็น follow-up ถ้าต้องการ
- ไม่ restart teammate pane (plan pin มีผลแค่ Lead — teammate ใช้ plain model id ไม่มี `[1m]`)
- ไม่ทำ per-project / per-tab override (config ทั้งหมด global เหมือนเดิม)
- ไม่แตะ logic การ pin model ตอน spawn (orchestrator.spawn อ่าน plan_tier อยู่แล้ว — คงไว้)

---

## 2. Design decisions (จาก brainstorm 2026-05-29)

| Dimension | Decision | Rationale |
|---|---|---|
| Scope ของ panel | รวมทุก config: plan + providers + role-providers + claude-auth | ลด config ที่กระจาย 5 จุดให้เหลือประตูเดียว |
| Status bar | เหลือปุ่ม `⚙ <plan>` ตัวเดียว (label โชว์ Max/Pro, badge ⚠ ถ้ามี provider ปิด) | declutter แต่คง glanceability ของ plan (เหตุผลหลักของ feature คือกัน 1M error) |
| role-providers / claude-auth sections | ปุ่ม "Configure…" เปิด dialog เดิมเป็น sub-dialog | reuse code ที่ test แล้ว ไม่ refactor |
| Live-session sync | **restart Lead pane** แทน `[system]` broadcast | deterministic — ไม่พึ่ง AI เชื่อฟัง notice |
| Restart scope | Lead ของทุก project tab | plan/provider เป็น global; teammate ไม่กระทบ plan |
| Context preservation | restart spawn ด้วย `--resume <uuid>` (auto ภายใน RESUME_WINDOW_SEC) | Lead จำ conversation เดิม ไม่เริ่มจากศูนย์ |
| Restart trigger | เฉพาะเมื่อ plan หรือ provider เปลี่ยน | role-providers/claude-auth apply ตอน spawn teammate ถัดไป ไม่ต้อง restart Lead |
| Cancel restart popup | revert เฉพาะ field plan/provider (ไม่ persist การเปลี่ยนนั้น) | กัน state ไม่ตรงระหว่างไฟล์กับ running Lead (เช่น Pro persisted แต่ Lead ยัง 1M → ตรงกับ bug ที่ feature นี้พยายามเลี่ยง) |

---

## 3. Components

### 3.1 `settings_dialog.py` (ไฟล์ใหม่)

`SettingsDialog(QDialog)` — 4 sections เรียงบนลงล่าง:

1. **Account plan** — `QRadioButton` 2 ตัว (Max / Pro) ใน `QButtonGroup`
   - initial: `plan_tier.current()`
2. **Brains (2nd/3rd opinion)** — `QCheckBox` 2 ตัว (Codex / Gemini)
   - checked = enabled; initial: `not provider_state.is_disabled(p)`
3. **Role → provider mapping** — `QLabel` + ปุ่ม "Configure…" → `RoleProviderDialog(self).exec()`
4. **Claude auth override** — `QLabel` + ปุ่ม "Configure…" → `ClaudeAuthDialog(self).exec()`

ปิดท้ายด้วย `[Cancel] [Save]` (QDialogButtonBox)

Dialog **ไม่** persist เอง — มันคืน intent (plan ที่เลือก, provider states ที่เลือก) ให้ caller (main_window) เป็นคนตัดสิน restart + persist ผ่าน orchestrator เพื่อให้ logic restart อยู่ที่เดียว (orchestrator/main_window) ทดสอบง่าย

**Pure helper (แยกจาก Qt เพื่อ test):**
```python
def restart_needed(before: SettingsSnapshot, after: SettingsSnapshot) -> bool:
    """True iff plan tier หรือ provider-disabled set เปลี่ยน."""
```
`SettingsSnapshot` = dataclass `{tier: str, disabled: frozenset[str]}`

### 3.2 `main_window.py` — status bar

**ลบ:**
- `_chip_plan`, `_chip_codex`, `_chip_gemini` (สร้าง + addPermanentWidget + style/tooltip helpers ที่ใช้เฉพาะ chip เหล่านี้: `_provider_chip_style`, `_plan_chip_style`, `_plan_chip_tooltip`, handler `_on_plan_chip_clicked`, `_on_provider_chip_clicked`)
- ปุ่ม `_btn_providers` (🤖 Providers) + `_btn_claude_auth` (Claude Auth) + handler `_on_providers_clicked` / `_on_claude_auth_clicked` — **ลบทั้งหมด** entry point ย้ายเข้า SettingsDialog (3.1 ปุ่ม Configure… ของ section 3/4 import `RoleProviderDialog` / `ClaudeAuthDialog` แล้ว `.exec()` เองตรงๆ — ไม่พึ่ง main_window handler)

**เพิ่ม:**
- `_btn_settings = QPushButton(self)` — label จาก pure function:
  ```python
  def settings_button_label(tier: str, any_provider_disabled: bool) -> str:
      base = "⚙ Pro" if tier == PRO else "⚙ Max"
      return f"{base} ⚠" if any_provider_disabled else base
  ```
  - stylesheet: สีเตือนถ้า Pro (เหมือน `_plan_chip_style` เดิม)
  - คลิก → `_on_settings_clicked()` เปิด `SettingsDialog`
- repaint ปุ่มเมื่อ `planTierChanged` / `providerStateChanged` emit (reuse `_on_plan_tier_changed` / `_on_provider_state_changed` → ชี้มา `_refresh_settings_button()`)

### 3.3 `orchestrator.py`

- **`set_plan_tier(tier)`** — ลบ broadcast block (`notice = "[system] account plan set to ..."` + loop ส่งเข้า Lead panes) เก็บ `plan_tier.set_current()` + `planTierChanged.emit()` + `_log_event`
- **`toggle_provider(provider, disabled)`** — ลบ broadcast block เช่นกัน เก็บ `set_disabled()` + `providerStateChanged.emit()` + `_log_event`
- **`restart_leads() -> tuple[int, list[str]]`** (method ใหม่) — สำหรับทุก project tab ที่มี Lead pane alive: `close(role="lead", project=p)` แล้ว `spawn(role="lead", project=p, ...)` (spawn หยิบ `--resume <uuid>` อัตโนมัติเพราะเพิ่ง exit ใน RESUME_WINDOW_SEC) คืนจำนวน + รายชื่อ project ที่ restart
  - ระวัง: ต้องไม่ชนกับ auto-respawn watcher — ใช้ path เดียวกับ manual close+spawn (auto_respawn_attempts ถูก reset ใน close() อยู่แล้ว)

### 3.4 Save flow (main_window `_on_settings_clicked`)

```
before = snapshot()                       # tier + disabled set ปัจจุบัน
dlg = SettingsDialog(self); ถ้า exec != Accepted: return
after = dlg.result_snapshot()
ถ้า restart_needed(before, after):
    popup = QMessageBox "จะ restart Lead ของทุก tab (เก็บ context ผ่าน --resume) ตกลงไหม?"
    ถ้า OK:
        ใช้ after → orch.set_plan_tier / orch.toggle_provider (persist)
        n, projs = orch.restart_leads()
        status: "restarted Lead ใน N tab"
    ถ้า Cancel:
        # revert: ไม่ persist plan/provider — config เดิมคงอยู่
        (role-providers/claude-auth ที่กดผ่าน sub-dialog persist ไปแล้วตอนกด Save ใน sub-dialog — ไม่ revert)
else:
    # ไม่มีอะไรที่ต้อง restart — role-providers/claude-auth persist ผ่าน sub-dialog แล้ว
    no-op (status: "settings saved")
```

> หมายเหตุ: role-providers / claude-auth persist ทันทีที่กด Save ใน sub-dialog ของมันเอง (พฤติกรรมเดิม) — มันไม่อยู่ใน before/after snapshot และไม่ trigger restart

---

## 4. Data flow

```
user คลิก ⚙
  → SettingsDialog เปิด (อ่าน plan_tier + provider_state เป็น initial)
  → user แก้ radio/checkbox / กด Configure… (sub-dialog persist เอง)
  → กด Save
  → main_window เทียบ before/after
     ├─ plan/provider เปลี่ยน → popup → OK → persist (orch) → orch.restart_leads()
     │                                   → close+spawn(--resume) ทุก Lead → context คงอยู่
     │                                   → planTierChanged/providerStateChanged → ปุ่ม ⚙ repaint
     └─ ไม่เปลี่ยน (หรือแก้แค่ sub-dialog) → no restart
```

State files (ไม่เปลี่ยน format): `~/.takkub/plan.json`, `~/.takkub/disabled-providers.json`, `~/.takkub/role-providers.json`, claude-auth state

---

## 5. Error handling

- `restart_leads()` ต่อ project: ถ้า `spawn()` คืน `(False, msg)` → log + แสดงใน status bar, ทำ project อื่นต่อ (ไม่ abort ทั้ง batch)
- ถ้า resume window พลาด (spawn เกิน 5 นาทีหลัง close — ไม่น่าเกิดเพราะ close→spawn ติดกัน) → Lead เริ่ม session ใหม่ (ยอมรับได้ ไม่ใช่ crash)
- ปุ่ม Configure… sub-dialog cancel → ไม่กระทบ Settings dialog (เหมือนเดิม)
- plan/provider persist สำเร็จแต่ restart บาง tab พลาด → state ไฟล์ถูกต้อง (apply ตอน spawn ถัดไปอยู่ดี); แค่ tab ที่พลาดยัง inherit model เดิมจน restart เอง — log เตือน

---

## 6. Testing

| Test | ระดับ |
|---|---|
| `restart_needed()` — plan เปลี่ยน → True; provider เปลี่ยน → True; ไม่เปลี่ยน → False; แก้ทั้งคู่ → True | pure unit (ไม่ต้อง Qt) |
| `settings_button_label()` — Max/Pro × provider-disabled → ป้ายถูก | pure unit |
| `orchestrator.set_plan_tier` — persist + emit + **ไม่** broadcast (assert ไม่มี `[system]` ส่งเข้า pane) | unit (update test เดิม) |
| `orchestrator.toggle_provider` — persist + emit + ไม่ broadcast | unit (update test เดิม) |
| `orchestrator.restart_leads` — เรียก close+spawn ต่อ Lead ทุก tab, นับถูก, error tab เดียวไม่ abort | unit (mock spawn/close) |
| `SettingsDialog` initial state ตรงกับ plan_tier/provider_state | Qt test (offscreen) ถ้า test infra รองรับ |

ลบ/อัปเดต test เดิมที่ assert broadcast wording (เช่น `test_provider_toggle_orchestrator.py`, `test_plan_tier.py` ส่วนที่ผูก broadcast)

---

## 7. Files touched

- **new:** `src/agent_takkub/settings_dialog.py`
- **edit:** `src/agent_takkub/main_window.py` (status bar — ลบ 3 chip + 2 ปุ่ม, เพิ่ม 1 ปุ่ม ⚙, save flow)
- **edit:** `src/agent_takkub/orchestrator.py` (ลบ broadcast ใน set_plan_tier/toggle_provider, เพิ่ม restart_leads)
- **edit/new tests:** `tests/test_settings_dialog.py` (new), อัปเดต `tests/test_plan_tier.py` / `tests/test_provider_toggle_orchestrator.py` ถ้าผูก broadcast
- **docs:** spec นี้ + update `CLAUDE.md` section "Account plan toggle" / "Disabled providers" ให้สะท้อนว่า toggle อยู่ใน Settings dialog + restart แทน broadcast
```
