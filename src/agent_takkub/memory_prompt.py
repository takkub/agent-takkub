"""The memory-pointer text every teammate pane gets, whatever its provider (#690).

Before #690 the project-memory pointer (#33/#687) and the role learned-notes
block were rendered inline inside `spawn_engine`'s claude-only appendix, so a
teammate on codex/gemini-agy/opencode/kimi/cursor never saw either — while
`orchestrator.done()` kept *writing* its failures into role memory that no
non-claude pane was ever pointed back at.

Pure text rendering, no I/O and no `agent_takkub` imports: callers resolve the
paths (`orchestrator_text._resolve_project_memory`,
`role_memory.ensure_role_memory`) and this module only words them, so the claude
and non-claude paths cannot drift apart again.

Why the shared-file variant names a DIRECTORY, not a file: non-claude providers
read one `AGENTS.md` per cwd (`codex_agents_md.ensure_agents_md`). Two different
roles sharing a cwd — backend on codex and frontend on gemini in the same repo
root — would otherwise read whichever role spawned last, i.e. another role's
lessons presented as their own. Every pane carries `TAKKUB_BASE_ROLE` in its
env, so the rule "<dir>/<your TAKKUB_BASE_ROLE>.md" resolves correctly per
pane from a single shared file.
"""

from __future__ import annotations

from pathlib import Path


def project_memory_block(mem_path: Path | str) -> str:
    """Pointer to the project-wide MEMORY.md index (Lead's constraint registry).

    Project-scoped, so it is safe in a per-cwd shared file as well as in a
    per-pane prompt.
    """
    return f"""

---

## 📋 Project memory (Lead's constraint registry)

Lead ของ project นี้มี auto-memory ที่บันทึก domain rules ไว้ที่:

`{mem_path}`

**อ่านก่อนเริ่มงานที่แตะ:** dependency, lockfile, docker, ports, vendor pattern, หรือ tool ที่โปรเจ็คระบุว่าใช้:

```
Read("{mem_path}")
```

MEMORY.md เป็น index — แต่ละ entry ชี้ไปยัง memory file ที่อธิบาย rule นั้นๆ อ่านเฉพาะ file ที่เกี่ยวกับงานของคุณ ไม่ต้องอ่านทั้งหมด
"""


def shared_role_memory_block(role_memory_dir: Path | str) -> str:
    """Role learned-notes rule for a file several roles may share (AGENTS.md).

    Names the directory plus the naming rule rather than one role's file — see
    the module docstring for why a concrete path would hand one role another
    role's notes.
    """
    return f"""

---

## 🧠 Your learned notes (role ของคุณ · โปรเจคนี้)

ความรู้ที่ role ของคุณสะสมไว้กับโปรเจคนี้ (pattern, pitfall, login/flow, decision, งานที่เคยล้ม) อยู่ที่:

`{role_memory_dir}/<role ของคุณ>.md`

role ของคุณ = ค่า env `TAKKUB_BASE_ROLE` (ไฟล์นี้ใช้ร่วมกันหลาย role — **อย่าเปิดไฟล์ของ role อื่น**)

- **อ่านไฟล์ของคุณก่อนเริ่มงาน** — ถ้ามีเนื้อหาแล้ว นั่นคือสิ่งที่คุณรู้เกี่ยวกับโปรเจคนี้แล้ว อย่าเดา/ค้นใหม่
- **เจอสิ่งที่ไม่ obvious ที่ยังไม่มีในไฟล์** → append สั้นๆ ลงไฟล์เดียวกัน เก็บเฉพาะของจริงที่มีค่า อย่าซ้ำ code/git
"""


def paste_memory_note(mem_path: Path | str | None, role_mem_path: Path | str | None) -> str:
    """Short per-pane note for when AGENTS.md is user-owned and the blocks above
    never land (same fallback channel as #621 M3's language directive).

    Per-pane, so the concrete role file is safe to name here. Empty when there
    is nothing to point at.
    """
    lines: list[str] = []
    if mem_path:
        lines.append(f"project memory (อ่าน index ก่อนเริ่มงาน): {mem_path}")
    if role_mem_path:
        lines.append(f"learned notes ของ role คุณ (อ่านก่อน / append เมื่อเจอสิ่งใหม่): {role_mem_path}")
    if not lines:
        return ""
    return "[ความจำของโปรเจค (#690)] " + " · ".join(lines)
