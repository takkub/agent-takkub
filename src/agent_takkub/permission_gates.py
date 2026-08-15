"""Permission-gate awareness for spawned teammate panes (issue #243).

2026-08-15 incident: backend#3 hit `.claude/settings.json`'s
`permissions.ask` rule for `git reset --hard` while clearing a merge
conflict. An `ask` rule survives `--dangerously-skip-permissions` by
design (it's the one override the CLI itself can't bypass), so the pane
just sat at an unanswered y/N prompt. In an attended session someone was
there to press it; in an unattended overnight run, nothing would have —
the whole wave stalls with no signal.

This module is the single source of truth for "what will this pane's Bash
gate on, and what should it do instead of typing the command and waiting":
never hardcode the `.claude/settings.json` rule list into the 16 role
`.md` files — it would drift the moment settings.json changes. Read live
at spawn time instead (mirrors `lead_context.py`'s BLOCKED_DIRS pattern:
render from source-of-truth config, not a copy).

Multi-provider note (#103): only claude has a persistent, bypass-proof
`permissions.ask` mechanism the cockpit can enumerate today. codex/gemini/
opencode/kimi/cursor are spawned with a full-autonomy flag (see each
provider's `autonomy_flags` in provider_spec.py) — none of it is
`permissions.ask`-equivalent, so there is nothing to enumerate for them
yet. That gap is real and stated explicitly below (`_render_generic_note`)
rather than silently only covering claude.
"""

from __future__ import annotations

import json
import pathlib

# Safety bound on the upward directory walk (matches other cwd-root-finding
# code in this codebase, e.g. codex_agents_md._ensure_git_excluded's walk).
_MAX_WALK_UP = 12

_SETTINGS_FILENAMES = ("settings.json", "settings.local.json")

# Known `.claude/settings.json` `permissions.ask` Bash patterns → the
# gate-free command(s) that achieve the same intent, written up during the
# #243 investigation from the cockpit's OWN current gate list (see
# `.claude/settings.json` at REPO_ROOT). Keyed by the command prefix with
# the trailing `:*` stripped (i.e. `Bash(git reset --hard:*)` -> the key
# `git reset --hard`). A pattern not in this table still gets listed with a
# generic "no known alternative, report FAILED" fallback instead of being
# dropped silently.
_KNOWN_ALTERNATIVES: dict[str, str] = {
    "git reset --hard": (
        "`git checkout -B <branch> <sha>` (ย้าย branch pointer แบบไม่ทำลาย history) · "
        "`git restore --source=<sha> --worktree --staged .` (sync working tree ไปยัง "
        "commit อื่นโดยไม่ reset) · ยกเลิก merge/rebase/cherry-pick ที่ค้างอยู่ → "
        "`git merge --abort` / `git rebase --abort` / `git cherry-pick --abort` · "
        "ยกเลิก staged file เดียว → `git restore --staged <path>`"
    ),
    "git push --force": (
        "ห้าม force-push อยู่แล้วตามนโยบาย role — commit ปกติบน branch ตัวเอง "
        "แล้วรายงาน Lead ให้ review/push แทน"
    ),
    "git push -f": (
        "ห้าม force-push อยู่แล้วตามนโยบาย role — commit ปกติบน branch ตัวเอง "
        "แล้วรายงาน Lead ให้ review/push แทน"
    ),
    "git push --force-with-lease": (
        "ห้าม force-push อยู่แล้วตามนโยบาย role — commit ปกติบน branch ตัวเอง "
        "แล้วรายงาน Lead ให้ review/push แทน"
    ),
    "npm install -g": (
        "ติดตั้งแบบ local แทน: `npm install <pkg>` (ลงใน package.json ของ project) "
        "หรือรันครั้งเดียวไม่ติดตั้งถาวรด้วย `npx --yes <pkg>`"
    ),
    "npm i -g": (
        "ติดตั้งแบบ local แทน: `npm install <pkg>` (ลงใน package.json ของ project) "
        "หรือรันครั้งเดียวไม่ติดตั้งถาวรด้วย `npx --yes <pkg>`"
    ),
}

_NO_KNOWN_ALTERNATIVE = (
    "ไม่มีทางเลือกที่ cockpit รู้จักสำหรับ pattern นี้ — ถ้าจนมุมจริงจนต้องใช้คำสั่งนี้ "
    "ห้ามรันแล้วรอ ให้รายงาน FAILED ทันที (ดูวิธีด้านล่าง)"
)


def _iter_claude_dirs(start: pathlib.Path) -> list[pathlib.Path]:
    """`.claude` dirs from *start* upward through parents, stopping after (and
    including) the first `.git` boundary found, or after `_MAX_WALK_UP`
    levels — whichever comes first. Nearest-first order.
    """
    found: list[pathlib.Path] = []
    cur = start
    for _ in range(_MAX_WALK_UP):
        candidate = cur / ".claude"
        if candidate.is_dir():
            found.append(candidate)
        if (cur / ".git").exists():
            break
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return found


def _read_ask_rules(settings_path: pathlib.Path) -> list[str]:
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    ask = (data.get("permissions") or {}).get("ask") or []
    return [r for r in ask if isinstance(r, str)]


def resolve_claude_ask_rules(cwd: str) -> list[str]:
    """Merge `permissions.ask` from every `.claude/settings.json` +
    `.claude/settings.local.json` found walking up from *cwd* to its git
    root (inclusive). De-duplicated, nearest-cwd-first.

    Mirrors what claude itself actually merges: every teammate spawn passes
    `--setting-sources project,local` (see
    `provider_spec.claude_spec.extra_static_args`), so this reflects the
    same two sources claude reads — not a guess, not the user's `~/.claude`
    settings (deliberately excluded, same as the spawned pane).
    """
    try:
        start = pathlib.Path(cwd).resolve()
    except OSError:
        return []
    if not start.is_dir():
        start = start.parent
    seen: dict[str, None] = {}
    for claude_dir in _iter_claude_dirs(start):
        for fname in _SETTINGS_FILENAMES:
            fp = claude_dir / fname
            if fp.is_file():
                for rule in _read_ask_rules(fp):
                    seen.setdefault(rule, None)
    return list(seen)


def _extract_bash_prefix(rule: str) -> str | None:
    """`Bash(git reset --hard:*)` -> `git reset --hard`. `None` for a rule
    that isn't a `Bash(...)` pattern (e.g. an MCP tool rule) — still listed
    by the caller, just without a normalized alternative-lookup key."""
    if not (rule.startswith("Bash(") and rule.endswith(")")):
        return None
    inner = rule[len("Bash(") : -1]
    if inner.endswith(":*"):
        inner = inner[:-2]
    return inner.strip()


def _alternative_for(prefix: str | None) -> str:
    if prefix is not None and prefix in _KNOWN_ALTERNATIVES:
        return _KNOWN_ALTERNATIVES[prefix]
    return _NO_KNOWN_ALTERNATIVE


_FAILED_REPORT_HOWTO = (
    '```\ntakkub done --fail "ต้องใช้ <คำสั่ง> ซึ่งติด permission gate — <รายละเอียด/ทางที่ลองมาแล้ว>"\n```'
)


def render_claude_gate_appendix(cwd: str) -> str:
    """Spawn-time appendix for a claude-backed pane: every `permissions.ask`
    rule that actually applies to *cwd*, each with its gate-free
    alternative, plus the FAILED-report instruction. Empty string when the
    project sets no `ask` rules (keeps normal spawns token-free — same
    "only emit when non-empty" discipline as lead_context.py's substituted-
    providers section).
    """
    rules = resolve_claude_ask_rules(cwd)
    if not rules:
        return ""
    lines = [
        "",
        "",
        "---",
        "",
        "## 🚧 Permission gate ของ project นี้ (บังคับอ่านก่อนรัน Bash)",
        "",
        "project นี้ตั้ง `permissions.ask` ไว้ใน `.claude/settings.json` — คำสั่งด้านล่าง "
        "จะ**ค้างรอ user กด y/N** แม้ pane รันแบบ `--dangerously-skip-permissions` ก็ตาม "
        "(ask-rule override การ bypass เสมอ โดยดีไซน์) ถ้าไม่มีคนอยู่หน้าจอ (unattended) "
        "→ **pane จะค้างจนหมดเวลาโดยไม่มีสัญญาณ** ห้ามเดินชนคำสั่งเหล่านี้:",
        "",
    ]
    for rule in rules:
        prefix = _extract_bash_prefix(rule)
        label = f"`{prefix}`" if prefix is not None else f"`{rule}`"
        lines.append(f"- {label}")
        lines.append(f"  - ทางเลือกที่ไม่ติด gate: {_alternative_for(prefix)}")
    lines.append("")
    lines.append(
        "**จนมุมจริง (ไม่มีทางเลือกใช้ได้จริง) → ห้ามพิมพ์คำสั่งที่ติด gate แล้วรอ** "
        f"รายงานแทนด้วย:\n{_FAILED_REPORT_HOWTO}"
    )
    return "\n".join(lines)


def render_generic_gate_note(provider_display: str, autonomy_flags: list[str]) -> str:
    """Spawn-time appendix for a non-claude pane (#103 gap, stated
    explicitly rather than silently omitted). None of codex/gemini/
    opencode/kimi/cursor are currently spawned with a persistent,
    bypass-proof "ask" mechanism the cockpit can enumerate the way claude's
    `permissions.ask` works — each is launched with a full-autonomy flag
    instead (see the *autonomy_flags* passed in, sourced from
    `provider_spec.py`). Still tells the pane what to do if it hits an
    unanswered prompt anyway, instead of leaving it with no guidance at all.
    """
    flags = " ".join(autonomy_flags) or "(none)"
    return f"""

---

## 🚧 Permission gate awareness ({provider_display})

cockpit spawn pane นี้ด้วย autonomy flag ที่ bypass confirmation prompt เกือบทั้งหมด
(`{flags}`) — cockpit **ยังไม่มีกลไก resolve gate-list แบบ persistent สำหรับ provider นี้**
เหมือน claude's `.claude/settings.json → permissions.ask` (gap นี้เปิดเป็น #103, ไม่ใช่
claude-only แบบเงียบ — แค่ยังไม่มี mechanism ให้ resolve)

**ถึงอย่างนั้น ถ้าเจอ prompt ค้างจริง** (เช่น confirm ที่ autonomy flag ไม่ครอบคลุม)
**ห้ามรอเงียบ** — รายงานทันทีด้วย:
{_FAILED_REPORT_HOWTO}"""
