# Gemini CLI Role / Provider — Design Spec

**Date:** 2026-05-19
**Status:** Approved (pending implementation plan)
**Author:** Lead (agent-takkub cockpit)

## Goal

เพิ่ม Google Gemini CLI เป็น **third provider** ในรายชื่อ pane backends ของ cockpit (ปัจจุบันมี `claude` กับ `codex` เท่านั้น) พร้อมเพิ่ม role ใหม่ชื่อ `gemini` ที่แทน `designer` ใน default grid layout

ใช้งานเหมือน codex pane: เป็น generalist / second-opinion specialist ไม่ใช่ specialized role ที่ผูกกับ domain (frontend/backend/mobile/...)

## Non-Goals

- ไม่ทำ Gemini-specific MCP config (มี `gemini mcp` subcommand แยก เก็บไว้ phase 2)
- ไม่ทำ session resume (`gemini --resume`) — pane เริ่ม stateless เหมือน codex
- ไม่ลบไฟล์ `.claude/agents/designer.md` — เก็บไว้ให้ user add เป็น custom slot ทีหลังได้
- ไม่ migrate `~/.takkub/role-providers.json` เดิม — sanitizer drop entry ที่ provider invalid อยู่แล้ว

## Architecture — Mirror Codex Pattern

โครงสร้างทุก concept ของ codex มี counterpart ฝั่ง gemini แบบ 1-to-1:

| Concept | Codex (existing) | Gemini (new) |
|---|---|---|
| Role entry | `Role("codex", col=1, row=4, "#10a37f")` | `Role("gemini", col=2, row=0, "#4285f4")` (แทน designer slot) |
| Provider constant | `CODEX = "codex"` | `GEMINI = "gemini"` |
| Forced provider | `"codex" → CODEX` | `"gemini" → GEMINI` |
| Helper module | `codex_helper.py` (find binary + one-shot exec) | `gemini_helper.py` (ใหม่ — mirror shape) |
| Context file planter | `codex_agents_md.py` plants `AGENTS.md` | `gemini_md.py` (ใหม่) plants `GEMINI.md` |
| Pane spawn branch | `orchestrator.py` line ~711-772 | branch ใหม่ใต้ codex branch |
| One-shot CLI | `takkub codex "<prompt>"` (uses `codex exec`) | `takkub gemini "<prompt>"` (uses `gemini -p`) |
| Provider dialog row | locked "codex" row | locked "gemini" row + dropdown 3-way ทุก row อื่น |

## Slot Decision

```
col 0    col 1        col 2
─────    ─────────    ──────────
[Lead]   frontend     [GEMINI]    ← row 0 (was designer)
         backend      qa
         mobile       reviewer
         devops
         codex
```

- ลบ `Role("designer", ...)` จาก `DEFAULT_TEAMMATES`
- เพิ่ม `Role("gemini", "Gemini", "#4285f4", column=2, row=0)`
- เก็บ `.claude/agents/designer.md` ไว้ (user เพิ่มเป็น custom slot ภายหลังได้)

## Component Details

### `src/agent_takkub/gemini_helper.py` (ใหม่)

โครงเหมือน `codex_helper.py`:

```python
def find_gemini_executable() -> str | None:
    return shutil.which("gemini")

def gemini_exec(prompt, *, cwd=None, timeout=120.0, model=None) -> tuple[bool, str]:
    binary = find_gemini_executable()
    if binary is None:
        return False, ("gemini binary not on PATH. Install with "
                       "`npm install -g @google/gemini-cli`.")
    if not (prompt or "").strip():
        return False, "empty prompt"
    argv = [binary, "-p", prompt]
    if model:
        argv = [binary, "-m", model, "-p", prompt]
    # subprocess.run(...) — same error handling as codex_exec
```

**ต่างจาก codex_helper:**
- Gemini ใช้ flag `-p`/`--prompt` ไม่ใช่ subcommand `exec`
- Model flag เป็น `-m` (codex เป็น `--model`)
- Install command ต่างกัน (`@google/gemini-cli` แทน `@openai/codex`)

### `src/agent_takkub/gemini_md.py` (ใหม่)

โครงเหมือน `codex_agents_md.py`:

- Marker (ขึ้นต้นไฟล์): `<!-- takkub-managed GEMINI.md · do not commit -->`
- Target: `GEMINI.md` ใน spawn cwd (Gemini CLI auto-discovers)
- เนื้อหา: mirror `CODEX_AGENTS_MD` — เปลี่ยน "Codex Teammate" → "Gemini Teammate", ส่วน takkub send/done cheatsheet เหมือนเดิม
- Safety rule: skip ถ้า user-owned `GEMINI.md` (ไม่มี marker) → return `(False, "user-owned")`
- Idempotent: ทับไฟล์ที่มี marker ของเราได้ (refresh)

### `src/agent_takkub/orchestrator.py` — spawn branch

เพิ่ม branch ใต้ codex branch (line ~773):

```python
if provider_for(role_name) == GEMINI:
    from .gemini_md import ensure_gemini_md
    from .gemini_helper import find_gemini_executable

    gemini_bin = find_gemini_executable()
    if gemini_bin is None:
        return False, ("gemini binary not on PATH. Install with "
                       "`npm install -g @google/gemini-cli`.")
    spawn_cwd = cwd or default_cwd_for_role(role_name) or str(REPO_ROOT)
    ensure_gemini_md(spawn_cwd)
    env = os.environ.copy()
    env["TAKKUB_ROLE"] = role_name
    env["TAKKUB_PROJECT"] = project_ns
    bin_dir = str(REPO_ROOT / "bin")
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    # Yolo parity with codex's `--ask-for-approval never -s workspace-write`.
    # `-y` shortcut == `--approval-mode yolo`. Skips trust prompt + auto-approves
    # every tool call so the pane is unattended-runnable.
    gemini_argv = [gemini_bin, "-y"]
    session = PtySession(cols=110, rows=36, parent=self)
    try:
        session.spawn(argv=gemini_argv, cwd=spawn_cwd, env=env)
    except Exception as e:
        return False, f"failed to spawn gemini: {e}"
    pane.attach_session(session, cwd=spawn_cwd)
    session.processExited.connect(
        lambda _code, r=role_name, c=spawn_cwd, p=project_ns: self._on_session_exit(r, c, p)
    )
    if role_name in self._recent_exits:
        del self._recent_exits[role_name]
    self._auto_trust(role_name)
    self.statusChanged.emit()
    _log_event("spawn", role=role_name, cwd=spawn_cwd, resumed=False)
    return True, f"gemini spawned in {spawn_cwd}"
```

### `src/agent_takkub/provider_config.py`

- เพิ่ม `GEMINI = "gemini"`
- `VALID_PROVIDERS = frozenset({CLAUDE, CODEX, GEMINI})`
- `_FORCED_PROVIDER["gemini"] = GEMINI`
- Update docstring (3 providers + forced gemini)

### `src/agent_takkub/provider_dialog.py`

- import `GEMINI`
- dropdown items: `[CLAUDE, CODEX, GEMINI]`
- เพิ่ม locked row สำหรับ role `gemini` (เหมือน codex row) — display label `"gemini   (locked — role identity)"`

### `src/agent_takkub/cli.py`

- `cmd_gemini(args)`: mirror `cmd_codex` — call `gemini_exec(args.prompt, cwd=args.cwd, timeout=args.timeout, model=args.model)`, print output
- parser `takkub gemini`:
  - positional `prompt`
  - `--cwd` default None
  - `--model` default None
  - `--timeout` default 120.0
- ไม่อยู่ใน `LEAD_ONLY_COMMANDS` (one-shot, ใครเรียกก็ได้)

### `src/agent_takkub/roles.py`

- ลบ `Role("designer", "Designer", "#ec4899", column=2, row=0)` จาก `DEFAULT_TEAMMATES`
- เพิ่ม `Role("gemini", "Gemini", "#4285f4", column=2, row=0)` พร้อม comment อธิบายว่าเป็น non-claude pane (mirror codex comment block)
- คงลำดับ `DEFAULT_TEAMMATES`: frontend, backend, mobile, devops, **gemini**, qa, reviewer, codex (เรียงตาม column/row ที่ใช้จริง)

### `src/agent_takkub/main_window.py`

- update tooltip + label string ที่ hard-code `"claude / codex"` → `"claude / codex / gemini"` (1 จุดที่ line ~298)

### `CLAUDE.md` (project root)

- ใน teammate list ลบ `designer` ออก
- เพิ่ม `gemini` พร้อมคำอธิบาย:
  > **gemini** — Google Gemini CLI "สมองที่ 3" สำหรับ planning / second opinion / multi-perspective brainstorm — ใช้คู่ codex เพื่อเทียบมุมสามขั้ว (claude/codex/gemini)
- เพิ่ม section "เมื่อไหร่ควรเรียก gemini" (mirror codex section structure)

## Spawn Argv — Safety Profile

**A: `gemini -y` (yolo parity)** — เลือกตัวนี้

- `-y` = `--approval-mode yolo` = auto-accept ทุก action
- ใกล้ที่สุดกับ codex `--ask-for-approval never -s workspace-write`
- Blast radius = pane cwd เท่านั้น (ไม่ต่างจาก codex)
- User คุ้นกับ codex pane แล้ว behavior สอดคล้องกัน

Sandbox (`-s` boolean) ไม่เปิดเพราะ codex ไม่ได้เปิด full sandbox เช่นกัน — workspace-write ของ codex ≈ no-sandbox-but-cwd-scoped ของ gemini

## Testing Strategy

| Test file | What to verify |
|---|---|
| `tests/test_roles.py` (extend) | `by_name("gemini")` คืน Role; ไม่มี designer ใน `DEFAULT_TEAMMATES`; gemini col=2 row=0 |
| `tests/test_provider_config.py` (extend) | `provider_for("gemini") == GEMINI` (forced); save/load รับ gemini เป็น valid provider; typo provider → drop |
| `tests/test_gemini_helper.py` (ใหม่) | `find_gemini_executable` คืน path/None ตาม `shutil.which` monkeypatch; `gemini_exec` empty prompt → fail; binary missing → fail message; mock `subprocess.run` ยืนยัน argv ใช้ `-p`; timeout → `(False, "gemini exec timed out")` |
| `tests/test_gemini_md.py` (ใหม่) | empty dir → plant + marker ขึ้นต้น; ทับ marker ของเรา → refresh; ทับ user-owned → `(False, "user-owned")` + original untouched |
| `tests/test_cli.py` (extend) | parser route `takkub gemini "<prompt>"` ไป `cmd_gemini`; role gate ไม่บล็อก |

ไม่มี orchestrator spawn test สำหรับ codex อยู่แล้ว (PTY mocking ยาก) — gemini ก็ไม่ต้องเพิ่ม

## Auth / Runtime

- **Auth model**: Google login (`gemini` ครั้งแรก prompt browser) หรือ `GEMINI_API_KEY` env. Cockpit ไม่แตะ credentials. หาก user ยังไม่ login → ขึ้น browser prompt ครั้งแรกที่ pane spawn (ไม่ block — pane รออยู่จน user เสร็จ)
- **Auto-trust**: `-y` flag skip trust prompt อยู่แล้ว ไม่ต้องเพิ่ม PTY detection
- **Readiness**: ถ้า gemini banner ค้างหลัง `-y` → user กด Enter เอง 1 ครั้ง (ไม่ block functionality)
- **`takkub done/send`**: `GEMINI.md` cheatsheet สอน gemini ใช้ — mechanism เดียวกับ codex pane

## Pitfalls (จาก codex review + ตรวจ `gemini --help`)

1. **อย่า reuse codex flags** — gemini ไม่มี subcommand `exec`, ใช้ `-p` (flag); ไม่มี granular sandbox value, มีแค่ `-s` boolean
2. **อย่าส่ง Claude flags** — `--dangerously-skip-permissions`, MCP config, `--append-system-prompt`, `--continue` เป็น claude-only
3. **Context file ต่างกัน** — Codex → `AGENTS.md`, Gemini → `GEMINI.md` (อย่าเขียนทับไฟล์เดียวกัน)
4. **Safety marker แยก** — `<!-- takkub-managed GEMINI.md ... -->` ≠ codex marker (กันคนละ planter ชนกัน)
5. **Provider sanitizer** — ต้อง update `VALID_PROVIDERS` ก่อน ไม่งั้น entry `gemini` ใน JSON ถูก drop ทันที
6. **One-shot CLI return shape** — `cmd_gemini` return `{"ok": ..., "msg": ...}` แบบเดียวกับ `cmd_codex` เพื่อ CLI main() print logic ใช้ร่วมกันได้

## Acceptance Criteria

- [ ] `takkub assign --role gemini "<task>"` spawn pane ที่ `provider_for("gemini") == "gemini"` รัน `gemini -y` ใน project's cwd
- [ ] Pane spawn สำเร็จเมื่อ `gemini` binary บน PATH; fail message friendly เมื่อไม่มี
- [ ] `GEMINI.md` ถูก plant ใน cwd (เฉพาะถ้าไม่มีไฟล์ user-owned)
- [ ] `takkub gemini "<prompt>"` รัน `gemini -p "<prompt>"` แบบ one-shot ได้
- [ ] Provider dialog แสดง dropdown 3 ตัว (claude/codex/gemini) ทุก row ยกเว้น locked rows; locked rows: lead (claude), codex (codex), gemini (gemini)
- [ ] Designer ไม่อยู่ใน default grid อีกแล้ว แต่ `.claude/agents/designer.md` ยังอยู่
- [ ] Tests ทั้งหมดผ่าน (existing + new gemini_helper + gemini_md + extended provider/roles/cli)
- [ ] CLAUDE.md update รวมข้อมูล gemini role + ตัวอย่างใช้
