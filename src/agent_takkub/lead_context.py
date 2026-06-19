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
import re

from .config import (
    REPO_ROOT,
    RUNTIME_DIR,
    active_project,
    ensure_runtime,
    lead_cwd,
    load_projects,
)
from .vault_mirror import _is_junk_note

# Recent-teammate-note injection budget. We surface the *body* of recent
# done notes (not just filenames) so a fresh Lead recalls what teammates
# actually did — but bounded so it can't swallow the spawn-time context.
_BRIEF_MAX_NOTES = 6  # newest-first across days
_BRIEF_NOTE_CAP = 240  # chars per note snippet
_LEAD_SUMMARY_CAP = 1600  # chars for the latest lead end-session note

# Captures the body of the `## Note` block written by `_render_decision_note`,
# stopping at the next `##` section (e.g. `## Transcript`) or end of file.
_NOTE_BODY_RE = re.compile(r"## Note\s*\n+(?P<note>.+?)(?:\n##\s|\Z)", re.DOTALL)


def _extract_note_body(text: str) -> str:
    """Pull the substantive note text out of a session-mirror markdown file.

    Prefers the `## Note` block; falls back to everything after the YAML
    frontmatter and heading lines so older / hand-written files still yield
    something useful. Returns '' when nothing meaningful remains.
    """
    m = _NOTE_BODY_RE.search(text)
    if m:
        return m.group("note").strip()
    body = text
    if body.startswith("---"):
        parts = body.split("---", 2)
        if len(parts) == 3:
            body = parts[2]
    kept = [ln for ln in body.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    return "\n".join(kept).strip()


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

    # Recent teammate done notes WITH their bodies, newest-first across days.
    # Injecting the note *content* (not just the filename) is what actually
    # lets a fresh Lead recall what teammates did — the old version listed
    # bare filenames, so the substantive work was stored but never recalled.
    recent_notes: list[tuple[str, str, str]] = []  # (day, stem, note body)
    for day_dir in sorted(sessions_root.iterdir(), reverse=True):
        if not day_dir.is_dir():
            continue
        proj_dir = day_dir / project
        if not proj_dir.is_dir():
            continue
        for f in sorted(proj_dir.iterdir(), reverse=True):
            if f.suffix != ".md" or f.name.startswith("lead-"):
                continue
            try:
                note = _extract_note_body(f.read_text(encoding="utf-8"))
            except OSError:
                continue
            if _is_junk_note(note):
                continue
            recent_notes.append((day_dir.name, f.stem, note))
            if len(recent_notes) >= _BRIEF_MAX_NOTES:
                break
        if len(recent_notes) >= _BRIEF_MAX_NOTES:
            break

    if latest_lead is None and not recent_notes:
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
        # Truncate per-summary so a giant note can't swallow the budget and
        # leaves room for the teammate-note section below.
        if len(raw) > _LEAD_SUMMARY_CAP:
            raw = raw[:_LEAD_SUMMARY_CAP] + "\n…(truncated)"
        rel = (
            latest_lead.relative_to(RUNTIME_DIR.parent).as_posix()
            if RUNTIME_DIR.parent in latest_lead.parents
            else latest_lead.name
        )
        lines.append(f"### latest lead end-session — `{rel}`")
        lines.append("")
        lines.append(raw.rstrip())
        lines.append("")

    if recent_notes:
        lines.append("### recent teammate done (newest first)")
        lines.append("")
        for day, stem, note in recent_notes:
            snippet = note if len(note) <= _BRIEF_NOTE_CAP else note[:_BRIEF_NOTE_CAP] + "…"
            # Collapse newlines/runs of whitespace so each note stays one line.
            snippet = " ".join(snippet.split())
            lines.append(f"- `{day}` **{stem}** — {snippet}")
        lines.append("")

    brief = "\n".join(lines)
    # Hard cap — keeps total injection under control even if upstream sizes drift.
    if len(brief) > 4000:
        brief = brief[:4000] + "\n…(truncated)\n"
    return brief


def _claude_autoloads(claude_cwd: pathlib.Path, md_dir: pathlib.Path) -> bool:
    """True if claude, started in ``claude_cwd``, will auto-discover the CLAUDE.md
    living in ``md_dir`` — i.e. ``md_dir`` is the cwd itself or an ancestor of it,
    since claude reads CLAUDE.md from cwd and walks UP the tree. A ``md_dir`` that
    is a *subdirectory* of cwd is NOT auto-loaded eagerly, so the caller must still
    inject it. Used by tok-4 to avoid double-injecting the project rules that
    claude already pulls in on its own.
    """
    try:
        claude_cwd = claude_cwd.resolve()
        md_dir = md_dir.resolve()
    except (OSError, ValueError):
        return False
    return md_dir == claude_cwd or md_dir in claude_cwd.parents


def _render_lead_context(
    project: str | None = None,
    post_compact_brief: str | None = None,
    claude_cwd: str | None = None,
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

    # Guard: if the active project IS the cockpit repo itself (agent-takkub),
    # skip BLOCKED_DIRS injection entirely — cockpit files are in the ✅ list
    # (Lead can edit CLAUDE.md, projects.json, .claude/agents/*) so blocking
    # the whole repo would contradict that policy.
    if paths:
        _proj_paths: dict = proj.get("paths") or {}
        _proj_root_str: str | None = _proj_paths.get("main") or next(
            iter(_proj_paths.values()), None
        )
        if _proj_root_str:
            _proj_root = pathlib.Path(_proj_root_str).resolve()
            if _proj_root == REPO_ROOT.resolve():
                # Active project = cockpit → no BLOCKED_DIRS
                paths = []

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

    # Inject project-specific CLAUDE.md so Lead knows the project's deploy
    # rules, stack constraints, and conventions at planning time — previously
    # Lead only saw the cockpit CLAUDE.md and had to infer project rules from
    # the task description alone.
    #
    # Guard: if the active project IS the cockpit repo itself (agent-takkub),
    # its project_root/CLAUDE.md == the cockpit CLAUDE.md already in `base`,
    # so we skip to avoid double-injecting the same content.
    _project_rules_text: str | None = None
    if proj and paths:
        # Resolve project_root: prefer path keyed "main", else first path.
        _proj_paths: dict = proj.get("paths") or {}
        _proj_root_str: str | None = _proj_paths.get("main") or next(
            iter(_proj_paths.values()), None
        )
        if _proj_root_str:
            _proj_root = pathlib.Path(_proj_root_str).resolve()
            if _proj_root != REPO_ROOT.resolve():
                # tok-4: skip injection when claude will auto-discover this same
                # CLAUDE.md from its cwd (Lead spawns at `claude_cwd`, defaulting to
                # lead_cwd(project)). Injecting it too would double the project rules
                # in context. Only inject when the file lives somewhere claude won't
                # auto-load (e.g. a project subdir while Lead sits at the parent).
                _lead_dir_str = claude_cwd or lead_cwd(project=name)
                _auto = bool(_lead_dir_str) and _claude_autoloads(
                    pathlib.Path(_lead_dir_str), _proj_root
                )
                _candidate = _proj_root / "CLAUDE.md"
                if _candidate.exists() and not _auto:
                    _raw = _candidate.read_text(encoding="utf-8")
                    _cap = 3000
                    if len(_raw) > _cap:
                        _raw = _raw[:_cap] + "\n…(truncated)\n"
                    _project_rules_text = _raw

    if _project_rules_text:
        suffix += f"""

---

## 📋 Project rules (auto-injected from {name}/CLAUDE.md)

{_project_rules_text}"""

    # Determine provider availability — two distinct reasons codex/gemini can be
    # unusable, each warranting different Lead messaging:
    #   "toggled off"   = user disabled via cockpit status bar (can re-enable)
    #   "not installed" = binary absent from PATH — cross-check is Claude-on-Claude
    # Section is suppressed entirely when all providers are available (saves tokens
    # on every normal spawn where nothing is substituted).
    from .provider_config import _provider_available as _check_available
    from .provider_state import TOGGLABLE as _TOGGLABLE
    from .provider_state import is_disabled as _is_disabled

    _toggled_off: list[str] = []
    _not_installed: list[str] = []
    for _prov in sorted(_TOGGLABLE):
        if _is_disabled(_prov):
            _toggled_off.append(_prov)
        elif not _check_available(_prov):
            _not_installed.append(_prov)

    if _toggled_off or _not_installed:
        _sub_parts: list[str] = []
        if _toggled_off:
            _sub_parts.append(f"**ปิดโดย user (toggle):** {', '.join(_toggled_off)}")
        if _not_installed:
            _sub_parts.append(
                f"**ไม่ได้ติดตั้ง (CLI ไม่พบใน PATH):** {', '.join(_not_installed)}"
                " — cross-check จะเป็น Claude-on-Claude"
            )
        _sub_note_lines = "\n".join("- " + p for p in _sub_parts)
        _all_unavailable = ", ".join(sorted(_toggled_off + _not_installed))
        suffix += f"""

---

## 🔄 Substituted providers (Claude รับแทน)

provider ต่อไปนี้ใช้ไม่ได้ → **Claude รับแทนอัตโนมัติ** (เสีย model diversity):

{_sub_note_lines}

**ไม่ต้อง refuse** — ตำแหน่งเหล่านี้ Claude รับแทนอัตโนมัติ:
- propose / fire role เหล่านี้ได้ตามปกติ (primary หรือ cross-check)
- orchestrator จะ spawn pane ชื่อ role เดิมแต่รันด้วย claude
- เวลา propose/fire ให้ **บอก user 1 บรรทัด** ว่า "{_all_unavailable} ใช้ไม่ได้ → Claude รับแทน (เสีย model diversity)" แล้วเดินงานต่อ ไม่ต้องหยุดรอ

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


# Role-scoped plugin injection. `--plugin-dir` loads each plugin's skill +
# agent descriptions into the pane's system prompt, so an unused plugin is
# pure per-pane context bloat. addy-agent-skills (44 skills + 7 agents,
# ~3-5k tokens) duplicates the role `.md` prompts and takkub's own role panes,
# so it's dropped for every role. superpowers-dev (TDD/debug/brainstorm/plans)
# stays for implementing teammates; Lead orchestrates and gets only pordee
# (Thai compression, used in conversation). This mirrors `_ROLE_MCP_POLICY` in
# shared_dev_tools.py: _SAFE_PLUGINS stays the full safe set (doctor validates
# all of it); this policy only decides what each pane actually receives.
#
# Roles NOT in this map fall back to the teammate set, NOT the full set — a
# future role should default to lean, not inherit addy-agent-skills by accident.
_TEAMMATE_PLUGINS: frozenset[str] = frozenset({"superpowers-dev", "pordee"})
_ROLE_PLUGIN_POLICY: dict[str, frozenset[str]] = {
    "lead": frozenset({"pordee"}),
}


def _default_plugin_dirs(role: str | None = None) -> list[str]:
    """Resolve ~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/ for
    each plugin in `_SAFE_PLUGINS`, returning the directories that actually
    contain a `.claude-plugin/plugin.json`. Best-effort; never raises.

    When `role` is given, the result is filtered through `_ROLE_PLUGIN_POLICY`
    (lead → pordee only; any other role → teammate set) so each pane only
    loads the plugins it actually uses. `role=None` keeps the full discovered
    set for back-compat with direct callers (e.g. doctor / smoke tests).
    """
    allowed = None if role is None else _ROLE_PLUGIN_POLICY.get(role, _TEAMMATE_PLUGINS)
    home = pathlib.Path.home()
    cache = home / ".claude" / "plugins" / "cache"
    out: list[str] = []
    if not cache.exists():
        return out
    for marketplace in _SAFE_PLUGINS:
        if allowed is not None and marketplace not in allowed:
            continue
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
