"""Lead-spawn-time context rendering — system prompt, write-boundary
settings, and plugin discovery.

Three concerns live here, all aimed at building what Lead sees the
moment a Lead pane starts:

1. `_render_lead_context(project)` — assembles `runtime/lead-context.md`
   from the cockpit CLAUDE.md plus auto-injected BLOCKED_DIRS section,
   disabled-providers section, and recent-session brief. This is the
   file claude reads via `--append-system-prompt-file` on Lead spawn.

2. `render_lead_settings(project)` — generates
   `runtime/lead-guard-<project>.json` with permissions.deny rules that
   block Lead from editing files under the project's configured roots.
   Currently bypassed by --dangerously-skip-permissions but kept for
   reference / future re-enable.

3. `_default_plugin_dirs()` + `_SAFE_PLUGINS` — discover the on-disk
   `~/.claude/plugins/cache/...` directory for each plugin the cockpit
   wants spawned panes to inherit (skipping claude-obsidian's broken
   SessionStart hook). Returns the list passed to `--plugin-dir`.

Extracted from orchestrator.py to keep that file focused on pane
lifecycle. orchestrator.py re-exports the names so existing test
imports (test_session_brief, test_lead_write_guard) and other
modules (doctor.py imports _SAFE_PLUGINS) keep working without churn.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime

from .config import (
    REPO_ROOT,
    RUNTIME_DIR,
    active_project,
    ensure_runtime,
    load_projects,
)

# Plugins we want spawned agents to inherit *explicitly* (skipping user-level
# settings to avoid claude-obsidian's broken SessionStart hook). Each entry
# is a *marketplace name* under ~/.claude/plugins/cache/. We pick the highest
# semver-ish version directory found.
_SAFE_PLUGINS: tuple[str, ...] = (
    "superpowers-dev",
    "addy-agent-skills",
    "pordee",
    "ecc",
    # claude-obsidian-marketplace is intentionally excluded: the cached 1.4.3
    # build ships a SessionStart prompt-hook that crashed all panes in v0.2.0
    # (ToolUseContext required error). Until a spawn smoke-test under cockpit
    # flags confirms the hook no longer fires, do not add it here.
)


def _allowed_project_roots(project: str) -> list[pathlib.Path]:
    """Return resolved Path objects for every path configured in `project`."""
    data = load_projects()
    proj = (data.get("projects") or {}).get(project) or {}
    return [pathlib.Path(p).resolve() for p in (proj.get("paths") or {}).values()]


def _recent_session_brief(project: str) -> str | None:
    """Build a compact context-restoration brief for Lead's spawn-time prompt.

    Reads runtime/sessions/<date>/<project>/ for:
      * the newest `lead-*.md` (most recent `takkub end-session` summary)
      * today's teammate done-event filenames (`<role>-HHMMSS.md`)

    Returns formatted markdown, or None when no history exists. Output is
    capped at ~3KB so it can't blow up Lead's spawn-time context budget.

    Why: without this, every new Lead session forgets the previous one — user
    says "rebuild" and Lead has to ask "rebuild what?" because the
    end_session summary is on disk but unread.
    """
    sessions_root = RUNTIME_DIR / "sessions"
    if not sessions_root.is_dir():
        return None

    latest_lead: pathlib.Path | None = None
    for day_dir in sorted(sessions_root.iterdir(), reverse=True):
        if not day_dir.is_dir():
            continue
        proj_dir = day_dir / project
        if not proj_dir.is_dir():
            continue
        leads = sorted(proj_dir.glob("lead-*.md"), reverse=True)
        if leads:
            latest_lead = leads[0]
            break

    today = datetime.now().strftime("%Y-%m-%d")
    today_dir = sessions_root / today / project
    today_done: list[str] = []
    if today_dir.is_dir():
        for f in sorted(today_dir.iterdir()):
            if f.suffix == ".md" and not f.name.startswith("lead-"):
                today_done.append(f.stem)

    if latest_lead is None and not today_done:
        return None

    lines = [
        "",
        "---",
        "",
        "## 🧠 Recent session brief (auto-injected at spawn)",
        "",
        f"context จาก previous session ของ project **{project}** — ใช้เป็น starting point",
        "ก่อนถาม user ซ้ำในเรื่องที่ทำไปแล้ว",
        "",
    ]

    if latest_lead is not None:
        try:
            raw = latest_lead.read_text(encoding="utf-8")
        except OSError:
            raw = ""
        # Truncate per-summary to ~2KB so a giant note can't swallow the budget.
        if len(raw) > 2048:
            raw = raw[:2048] + "\n…(truncated)"
        rel = (
            latest_lead.relative_to(RUNTIME_DIR.parent).as_posix()
            if RUNTIME_DIR.parent in latest_lead.parents
            else latest_lead.name
        )
        lines.append(f"### latest lead end-session — `{rel}`")
        lines.append("")
        lines.append(raw.rstrip())
        lines.append("")

    if today_done:
        lines.append("### today's teammate done events")
        lines.append("")
        for stem in today_done[:20]:
            lines.append(f"- `{stem}`")
        if len(today_done) > 20:
            lines.append(f"- …และอีก {len(today_done) - 20} event")
        lines.append("")

    brief = "\n".join(lines)
    # Hard cap — keeps total injection under control even if upstream sizes drift.
    if len(brief) > 4000:
        brief = brief[:4000] + "\n…(truncated)\n"
    return brief


def _render_lead_context(
    project: str | None = None,
    post_compact_brief: str | None = None,
) -> str | None:
    """Render Lead's spawn-time system prompt: cockpit CLAUDE.md + an
    auto-injected `BLOCKED_DIRS` paragraph listing the active project's paths.

    Lead's hybrid policy (see cockpit CLAUDE.md "Lead direct-edit policy")
    forbids direct file edits inside project paths; this function bakes the
    *current* project's paths into the prompt every spawn so the rule has
    teeth even when projects.json switches between sessions.

    Returns the rendered file path (string), or None if there's no cockpit
    CLAUDE.md to render from.
    """
    cockpit_md = REPO_ROOT / "CLAUDE.md"
    if not cockpit_md.exists():
        return None

    base = cockpit_md.read_text(encoding="utf-8")
    if project is not None:
        data = load_projects()
        proj = (data.get("projects") or {}).get(project) or {}
        name = project
    else:
        name, proj = active_project()
    paths = list((proj.get("paths") or {}).values()) if proj else []

    if paths:
        blocked = "\n".join(f"- `{p}`" for p in paths)
        header = f"active project: **{name}**" if name else "active project:"
    else:
        blocked = "- (no active project — projects.json `active` not set)"
        header = "active project:"

    suffix = f"""

---

## 🚫 BLOCKED_DIRS (auto-injected at spawn)

{header}

ไดเรกทอรีต่อไปนี้คือ project code — Lead **ห้ามใช้ Edit / Write / MultiEdit / NotebookEdit** ในไฟล์ใต้ paths เหล่านี้เด็ดขาด:

{blocked}

ถ้างานต้องแก้ไฟล์ในเส้นทางข้างบน → ใช้ `takkub assign --role <role> --cwd <path> "<task>"` เสมอ

✅ ทำเองได้:
- Read / Grep / Glob ทุกที่ (สำหรับวางแผน + เขียน task spec)
- Edit / Write ใน cockpit ({REPO_ROOT}) เช่น CLAUDE.md, projects.json, .claude/agents/*
- `git status` / `git log` / `git diff` (inspection ไม่กระทบไฟล์)

❌ ห้ามทำเองแม้แค่บรรทัดเดียว:
- ทุกไฟล์ใต้ BLOCKED_DIRS ข้างบน
- งานที่ touch > 1 ไฟล์
- งานที่ edit > 30 บรรทัดในรอบเดียว

ละเมิดข้อใดข้อหนึ่ง → หยุดทันทีแล้ว delegate ผ่าน `takkub assign`
"""
    # Append disabled-providers section (only if any are disabled — saves tokens
    # when everything is enabled, which is the common case). Lead reads this on
    # spawn and treats codex/gemini in the list as forbidden in proposals.
    from .provider_state import all_disabled as _all_disabled

    disabled = _all_disabled()
    if disabled:
        disabled_list = ", ".join(sorted(disabled))
        suffix += f"""

---

## ⛔ Disabled providers (cockpit toggle)

ขณะนี้ provider ต่อไปนี้ถูกปิดโดย user: **{disabled_list}**

**ห้าม** propose role เหล่านี้ใน routing table หรือ cross-check
**ห้าม** fire `takkub assign --role <disabled>` หรือ `takkub <disabled>`
ถ้า user ขอตรงๆ → ตอบว่า provider นั้นถูกปิดอยู่ ให้ user enable ก่อน

Status เปลี่ยนระหว่าง session: cockpit จะ inject `[system] <provider> ENABLED/DISABLED` message
"""
    # Append account-plan note ONLY under Pro (Max is the default and behaves
    # exactly as before — emitting nothing there saves tokens on every spawn).
    # A Pro owner can't reach the 1M-context model variant (usage-credits
    # gated), so the Lead must not propose or assume it.
    from .plan_tier import is_pro as _plan_is_pro

    if _plan_is_pro():
        suffix += """

---

## 💳 Account plan: Pro (1M context unavailable)

User อยู่บน Pro plan — **1M-context model variant ใช้ไม่ได้** (usage-credits gated)
**ห้าม** propose หรือพึ่ง 1M context / `[1m]` model variant
Lead pane ของ session นี้ถูก pin ไว้ที่ standard-context model อยู่แล้ว

Status เปลี่ยนระหว่าง session: cockpit จะ inject `[system] account plan set to PRO/MAX` message
"""
    # Append recent-session brief so a fresh Lead pane inherits context from
    # the previous `takkub end-session` summary + today's teammate done events.
    # Without this the read half of the session-log loop is missing — files
    # get written by end_session() but never read back at spawn time.
    if project is not None:
        brief = _recent_session_brief(project)
        if brief:
            suffix += brief

    # Append post-compact pane status when the orchestrator detects a recent
    # cockpit restart with live teammates (session-compact scenario).
    if post_compact_brief:
        suffix += post_compact_brief

    ensure_runtime()
    out = RUNTIME_DIR / "lead-context.md"
    out.write_text(base + suffix, encoding="utf-8")
    return str(out)


_LEAD_GUARD_WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

# Tools Lead can use without a permission prompt. Read-only ops (Read/Grep/
# Glob), arbitrary Bash (git/ls/takkub CLI — Lead's daily bread), web reads,
# task tracking, plan-mode toggles, and every MCP tool. Edit/Write are
# deliberately omitted: they go through defaultMode=acceptEdits (auto-accept
# on cockpit files) AND the deny rules above (hard-block project paths) so
# the write-boundary survives even if a future allow pattern broadens.
_LEAD_GUARD_ALLOW_TOOLS = (
    "Bash",
    "Read",
    "Grep",
    "Glob",
    "WebFetch",
    "WebSearch",
    "TaskCreate",
    "TaskUpdate",
    "TaskGet",
    "TaskList",
    "TaskOutput",
    "TaskStop",
    "EnterPlanMode",
    "ExitPlanMode",
    "mcp__*",
)


def render_lead_settings(project: str) -> pathlib.Path:
    """Generate runtime/lead-guard-<project>.json with permissions.deny rules
    that block Lead from editing any path under the project's configured roots.

    Also sets defaultMode=acceptEdits so Lead auto-accepts edits to cockpit
    files without requiring --dangerously-skip-permissions, and injects an
    allow list for read-only / coordinator tools (Bash, Read, Grep, MCP, ...)
    so Lead doesn't get prompt-spammed for every git/ls/takkub call.

    Idempotent: regenerates the file on every call so path changes in
    projects.json are picked up on the next Lead spawn.
    """
    roots = _allowed_project_roots(project)
    deny_rules: list[str] = []
    for root in roots:
        # Use POSIX forward-slash path; Claude Code accepts both on Windows.
        path_str = root.as_posix()
        for tool in _LEAD_GUARD_WRITE_TOOLS:
            deny_rules.append(f"{tool}({path_str}/**)")

    settings: dict = {
        "permissions": {
            "allow": list(_LEAD_GUARD_ALLOW_TOOLS),
            "deny": deny_rules,
            "defaultMode": "acceptEdits",
        }
    }

    ensure_runtime()
    out = RUNTIME_DIR / f"lead-guard-{project}.json"
    out.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def _default_plugin_dirs() -> list[str]:
    """Resolve ~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/ for
    each plugin in `_SAFE_PLUGINS`, returning the directories that actually
    contain a `.claude-plugin/plugin.json`. Best-effort; never raises."""
    home = pathlib.Path.home()
    cache = home / ".claude" / "plugins" / "cache"
    out: list[str] = []
    if not cache.exists():
        return out
    for marketplace in _SAFE_PLUGINS:
        mp_dir = cache / marketplace
        if not mp_dir.is_dir():
            continue
        # one plugin per marketplace; one version per plugin (typically)
        for plugin in sorted(mp_dir.iterdir()):
            if not plugin.is_dir():
                continue
            versions = sorted((v for v in plugin.iterdir() if v.is_dir()), reverse=True)
            for v in versions:
                if (v / ".claude-plugin" / "plugin.json").exists():
                    out.append(str(v))
                    break
    return out
