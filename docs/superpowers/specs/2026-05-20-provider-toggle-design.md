# Provider toggle (codex / gemini) — design

**Date:** 2026-05-20
**Owner:** Lead
**Status:** spec, ready for implementation plan
**Estimated effort:** ~4–6 ชม. (single PR)

---

## 1. Goal

ให้ user เปิด/ปิด codex และ gemini จาก UI status bar ได้ตามใจ ขณะที่ provider ถูกปิด Lead จะไม่ propose ใน routing table หรือ cross-check toggle เป็น manual general-purpose ไม่ผูก rate limit หรือเงื่อนไขใดๆ user flip เมื่อไรก็ได้ state persist ข้าม cockpit restart

**Non-goals:**
- ไม่ใช่ rate-limit detector (เลือก manual)
- ไม่ใช่ hard block (ไม่ block CLI `takkub codex/gemini` หรือ pane spawn ตรงๆ)
- ไม่ใช่ per-project / per-tab override (global ทั้ง cockpit)
- ไม่ kill pane ที่กำลังเปิดอยู่เมื่อ toggle off

---

## 2. Design decisions (จาก brainstorm 2026-05-20)

| Dimension | Decision | Rationale |
|---|---|---|
| Scope | Global ทั้ง cockpit | Rate-limit / preference ของ user ไม่ขึ้นกับ project |
| Trigger | UI status bar chip + click | เห็นตลอด ไม่ต้องเปิด dropdown คู่กับ chip RTK ที่มีอยู่ |
| Block | Soft — routing-only | Lead ไม่เสนอ แต่ user override manual ได้ถ้าจำเป็น |
| Detection | Manual ล้วน | ไม่ parse stderr ของ external CLI |
| Granularity | แยก toggle 2 ตัว | rate limit แต่ละตัวเกิดคนละเวลา |
| Persistence | Persist ข้าม restart | `~/.takkub/disabled-providers.json` |

---

## 3. Architecture

```
┌─────────────────────────────────────────────────┐
│  Status bar UI (main_window.py)                 │
│  [ Codex ●on ]  [ Gemini ●on ]   ← click toggle │
└──────────────────┬──────────────────────────────┘
                   │ orchestrator.toggle_provider("codex", True)
                   ▼
┌─────────────────────────────────────────────────┐
│  Orchestrator                                   │
│  - provider_state.set_disabled(...) → disk      │
│  - broadcast → ทุก Lead pane (multi-tab)        │
│  - emit Qt signal → main_window redraw chip     │
└──────────────────┬──────────────────────────────┘
        ┌──────────┴──────────────┐
        ▼                          ▼
┌────────────────┐         ┌──────────────────┐
│  Lead spawn    │         │  Runtime toggle  │
│  read file →   │         │  inject message  │
│  --append-     │         │  "[system] codex │
│  system-prompt │         │   DISABLED"      │
└────────────────┘         └──────────────────┘
                   │
                   ▼ Lead avoids codex in routing
```

**Single source of truth:** `~/.takkub/disabled-providers.json`
**Format:** `{"codex": true, "gemini": false}` (provider → disabled flag)
**Default:** ทุก provider enabled ถ้าไฟล์ไม่มี / corrupt → return empty mapping

---

## 4. Components

### 4.1 New file: `src/agent_takkub/provider_state.py` (~80 lines)

```python
"""Per-provider enable/disable state.

ปัจจุบัน provider_config.py ตอบ "role X ใช้ provider ไหน" (per-role mapping)
ส่วนไฟล์นี้ตอบ "provider Y พร้อมใช้ไหม" (per-provider gate) — boundary คนละแบบ
+ ค่า persist คนละไฟล์ + UI flow คนละทาง รวมจะ leaky จึงแยก module
"""

CODEX = "codex"
GEMINI = "gemini"
TOGGLABLE = frozenset({CODEX, GEMINI})  # ขยาย provider ใหม่เพิ่มที่นี่
_PATH = Path.home() / ".takkub" / "disabled-providers.json"

def path() -> Path: ...
def load() -> dict[str, bool]: ...           # missing → {}, corrupt → {}
def save(state: dict[str, bool]) -> None: ... # atomic write via .tmp
def is_disabled(provider: str) -> bool: ...
def set_disabled(provider: str, flag: bool) -> None: ...
def all_disabled() -> set[str]: ...
```

**Atomic write:** write `<path>.tmp` → `tmp.replace(path)` (เหมือน `config._write_json_atomic`)
**Sanitization:** drop key ที่ไม่อยู่ใน `TOGGLABLE` (กัน manual edit ใส่ provider แปลก)

### 4.2 Modify: `src/agent_takkub/main_window.py`

- เพิ่ม 2 chip buttons ใน status bar layout (คู่กับ chip RTK install)
- Visual:
  - enabled = bright color (codex teal `#10a37f`, gemini blue `#4285f4`)
  - disabled = dim gray + strikethrough label
- Tooltip: `"Codex: enabled — click to disable"` / `"Codex: disabled — click to enable"`
- Wire click → `orchestrator.toggle_provider(name, flag)`
- Connect to orchestrator's `providerStateChanged` signal → redraw chip color

### 4.3 Modify: `src/agent_takkub/orchestrator.py`

```python
class Orchestrator:
    providerStateChanged = pyqtSignal(str, bool)  # (provider, disabled)

    def toggle_provider(self, provider: str, disabled: bool) -> None:
        provider_state.set_disabled(provider, disabled)
        word = "DISABLED" if disabled else "ENABLED"
        suffix = ("Do not propose this in routing." if disabled
                  else "Available again.")
        msg = f"[system] {provider} provider {word}. {suffix}\n"
        for tab in self._all_tabs():
            lead = tab.lead_pane()
            if lead is not None:
                lead.inject_text(msg)
        self.providerStateChanged.emit(provider, disabled)
```

- **Lead spawn:** อ่าน `provider_state.all_disabled()` ตอน build `--append-system-prompt` → ใส่ snippet (ดู section 5)

### 4.4 Modify: `src/agent_takkub/routing_planner.py`

```python
def classify(
    user_message: str,
    *,
    disabled_providers: set[str] | None = None,
) -> RoutingAction:
    disabled = disabled_providers or set()
    action = _classify_inner(user_message)
    # Strip codex/gemini from cross_check
    if action.cross_check:
        action.cross_check = [r for r in action.cross_check if r not in disabled]
    # FIRE_ONESHOT codex/gemini ที่ถูกปิด → switch เป็น ASK_CLARIFY
    if action.kind == ActionKind.FIRE_ONESHOT and action.role in disabled:
        return RoutingAction(
            kind=ActionKind.ASK_CLARIFY,
            reason=f"{action.role} provider is disabled — ask user to enable first",
        )
    return action
```

- Default `disabled_providers=None` = backward-compat ทุก test เดิมยัง pass
- Lead AI ไม่ได้รัน Python นี้ตรงๆ แต่ unit tests + future code-level integration ใช้

### 4.5 Modify: `CLAUDE.md`

เพิ่ม section หลัง "Auto-routing":

```markdown
## Disabled providers

Cockpit มี toggle ใน status bar ปิด/เปิด codex และ gemini ได้ตามใจ user

ขณะ provider ถูกปิด:
- ห้าม propose ใน routing table — ทั้ง primary และ cross-check
- ห้าม fire `takkub assign --role codex/gemini` หรือ `takkub codex/gemini`
- ถ้า user ขอตรงๆ → ตอบว่าปิดอยู่ ให้ user enable ก่อนค่อยใช้

Source of truth: ~/.takkub/disabled-providers.json
Orchestrator inject สถานะตอน Lead spawn (system prompt) และระหว่าง session ([system] message)
```

---

## 5. Lead awareness — ทั้ง 2 ทาง

**(1) Spawn time:** `--append-system-prompt` snippet

```
## Disabled providers (cockpit toggle)

ขณะนี้ provider ต่อไปนี้ถูกปิดโดย user: codex, gemini

**ห้าม** propose role เหล่านี้ใน routing table หรือ cross-check
**ห้าม** fire `takkub assign --role <disabled>` หรือ `takkub <disabled>`
ถ้า user ขอตรงๆ → ตอบว่า provider นั้นถูกปิดอยู่ ให้ user enable ก่อน

Status เปลี่ยนระหว่าง session: cockpit จะ inject [system] message
```

(ถ้า disabled set ว่าง → ไม่ append snippet — ประหยัด token)

**(2) Runtime toggle change:** inject `[system]` message เข้า Lead pane

ใช้ mechanism เดียวกับ `[<role> done]` ที่มีอยู่ — Lead เห็นข้อความใน input → จำ context

---

## 6. UI mockup

```
สถานะ normal (ทั้งคู่ enabled):
┌──────────────────────────────────────────────────────────────┐
│  ⚡ Install rtk   │  [ Codex ●on ]  [ Gemini ●on ]    │ ...  │
└──────────────────────────────────────────────────────────────┘

สถานะ codex ปิด:
┌──────────────────────────────────────────────────────────────┐
│  ⚡ Install rtk   │  [ Codex ○off ]  [ Gemini ●on ]   │ ...  │
└──────────────────────────────────────────────────────────────┘
                       ↑ dim gray
```

---

## 7. Error handling + edge cases

| Case | Behavior |
|---|---|
| Disk write fail (~/.takkub readonly / full) | Toast warning ใน status bar in-memory state ยัง flip revert ตอน restart ไม่ block UI |
| JSON file corrupt | `load()` catch JSONDecodeError → return `{}` (all enabled) log ลง `runtime/events.log` ไม่ crash |
| Toggle ขณะ pane ของ provider นั้นเปิดอยู่ | Pane เดิมทำงานต่อ (soft = อนาคต) Lead ตัดสินเองว่าจะ `takkub close --role X` ไหม |
| Multi-tab | Iterate ทุก Lead pane ของทุก tab → inject `[system]` Lead pane queue handle |
| Lead เผลอ propose codex ทั้งที่ปิด | ไม่มี hard enforcement (soft) User responsibility ตาม chip indicator |
| `routing_planner.classify(disabled_providers=None)` | Default empty set → backward-compat ทุก test เดิม |

---

## 8. Testing strategy

### 8.1 Unit tests

```python
# tests/test_provider_state.py (NEW)
- test_load_missing_file_returns_empty()
- test_save_then_load_roundtrip()
- test_atomic_write_via_temp_file()
- test_corrupt_json_returns_empty_without_crash()
- test_set_disabled_then_is_disabled()
- test_unknown_provider_dropped_on_save()

# tests/test_routing_planner.py (EXTEND)
- test_classify_with_disabled_codex_drops_from_cross_check()
- test_classify_with_disabled_gemini_drops_rollout_proposal()
- test_classify_with_both_disabled_no_codex_no_gemini()
- test_classify_oneshot_disabled_provider_becomes_ask_clarify()
- test_classify_with_none_disabled_unchanged()  # regression
```

### 8.2 Manual smoke test (acceptance)

1. เปิด cockpit → status bar เห็น chip `[Codex on] [Gemini on]`
2. คลิก Codex chip → flip → `~/.takkub/disabled-providers.json` เก็บ `{"codex": true}`
3. ปิด cockpit + เปิดใหม่ → Codex chip ยัง off (persist)
4. ใน Lead pane พิมพ์ `refactor X` → Lead propose โดย**ไม่มี** codex row ใน table
5. คลิก Codex chip กลับ on → Lead pane เห็น `[system] codex ENABLED Available again.`
6. พิมพ์ `refactor Y` → Lead propose codex cross-check กลับมา
7. Disabled ทั้งคู่ + Lead พิมพ์ `rollout plan deploy safely` → Lead **ไม่** propose gemini เสนอ role อื่นหรือตอบ text

---

## 9. Files

### New
```
src/agent_takkub/provider_state.py        (~80 lines)
tests/test_provider_state.py              (~120 lines)
docs/superpowers/specs/2026-05-20-provider-toggle-design.md  (this file)
```

### Modified
```
src/agent_takkub/main_window.py           (add 2 chips in status bar)
src/agent_takkub/orchestrator.py          (toggle_provider, broadcast, spawn prompt)
src/agent_takkub/routing_planner.py       (filter cross_check)
tests/test_routing_planner.py             (5 new test cases)
CLAUDE.md                                 (add "Disabled providers" section)
```

### Untouched (verify ไม่ break)
```
src/agent_takkub/codex_helper.py
src/agent_takkub/gemini_helper.py
src/agent_takkub/cli.py
src/agent_takkub/provider_config.py
src/agent_takkub/roles.py
```

---

## 10. Sequence: Mac port comes after

User confirm 2026-05-20: feature 1 (toggle) merge เข้า main ก่อน Mac port (`docs/MACOS_PORT_PLAN.md`) จะแยก branch ทำหลัง toggle feature เสร็จ Mac plan ที่มีอยู่ยังใช้ได้ — review ตอนเริ่ม Mac branch
