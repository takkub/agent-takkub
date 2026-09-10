"""Boot-context token audit — issue #516.

Measures what a `claude` teammate spawn actually injects at boot, WITHOUT
spawning a real pane, by re-reading the exact same files/functions
`spawn_engine.spawn()`'s non-Lead branch reads when it builds `role_md_file`
(see that file's ~2477-2735 block). Kept here — instead of imported from
`spawn_engine` — because `spawn_engine.py` imports `PyQt6.QtCore.QTimer` at
module level; this module (like `doctor.py`, which is its only caller) must
stay importable from a bare CLI process. If spawn_engine's assembly sequence
changes, update this module's `measure_role_appendix` to match.

Every number here is a real byte count of a real file/render call — never a
guess. Where a real Anthropic-billed ground truth exists (see
`docs/audit/2026-09-07-boot-context.md`, captured from this project's own
`.claude-work` transcript), even the Thai-weighted `token_estimate` (#516 F2)
stays well below the real token count for this codebase's Thai+English mix —
the categories this module measures are not the whole boot payload (the
per-spawn task block, git status, env block, and Claude's own dynamic
system-prompt sections are real contributors this module does not read yet,
tracked as a gap in the audit doc, not silently patched over with an
inflated constant). Treat every `est_tokens` value in this module as a
LOWER BOUND, not a prediction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# #516 F2: was a private chars/4 copy (same convention as core.context_
# sources.base / core.brain.context_builder / core.brain.retrieval had, each
# independently); all 4 now share `token_estimate.estimate_tokens`, which
# still stays a leaf import (stdlib-only, zero new coupling for this module).
from .token_estimate import estimate_tokens


@dataclass
class CategoryMeasurement:
    category: str
    chars: int
    est_tokens: int
    detail: str = ""


# #516b (addendum, Lead 2026-09-07): categories whose SIZE is per-machine
# runtime state accumulated by panes doing real work during the day
# (role_memory.py's learned-notes file, the Lead-memory pointer text, the
# native `/memory` file), not repo content a `git diff` can regress. A
# static baseline ceiling gated on these produces false "regressions" on
# whichever dev machine happens to have written more notes today while CI
# (a fresh checkout with none of that state) stays green — see
# tests/test_boot_context_ceiling.py's own docstring for the incident.
# Reported for visibility, excluded from the ceiling-ratchet sum.
#
# `graft_caveats` joined this set 2026-09-07 (#516 follow-up): whether a
# role gets it is decided by `shared_dev_tools.role_mcp_allowlist`, which
# merges the built-in `_ROLE_MCP_POLICY` default with a per-machine
# operator override at `SETTINGS_HOME/pane-tools.json` (`takkub mcp
# allow|deny --role`). That file is real mutable operator config, not repo
# content — a machine that has denied/granted graft for a role makes this
# module's report disagree with a machine that hasn't, even on the exact
# same commit. Confirmed live: this machine's real `pane-tools.json` denies
# graft to frontend/backend/mobile (so `doctor --boot-context` never shows
# it), while `tests/test_boot_context_ceiling.py` runs under conftest's
# autouse `_isolate_runtime` (which points `SETTINGS_HOME`/`PANE_TOOLS_
# POLICY_FILE` at an empty tmp dir for every test — a correct, load-bearing
# isolation, not something to work around here) and so always falls back to
# the built-in policy, which grants graft to those three roles — a fixed
# +631 tok gap between `doctor` and the test that has nothing to do with
# the repo. Same shape as `learned_notes` above: measured and reported,
# never gated.
DYNAMIC_STATE_CATEGORIES = frozenset(
    {
        "learned_notes",
        "learned_notes_empty_pointer",
        "project_memory_pointer",
        "native_project_memory",
        "native_project_memory_LEAD",
        "graft_caveats",
        # #516 follow-up: whatever the operator has personally installed
        # under CLAUDE_CONFIG_DIR/skills — per-machine, not repo content.
        "native_skill_catalog",
    }
)


@dataclass
class RoleBootReport:
    role: str
    project: str
    categories: list[CategoryMeasurement] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_chars(self) -> int:
        return sum(c.chars for c in self.categories)

    @property
    def total_est_tokens(self) -> int:
        return sum(c.est_tokens for c in self.categories)

    @property
    def repo_controlled_categories(self) -> list[CategoryMeasurement]:
        return [c for c in self.categories if c.category not in DYNAMIC_STATE_CATEGORIES]

    @property
    def repo_controlled_chars(self) -> int:
        return sum(c.chars for c in self.repo_controlled_categories)

    @property
    def repo_controlled_est_tokens(self) -> int:
        return sum(c.est_tokens for c in self.repo_controlled_categories)


def _cat(category: str, text: str, detail: str = "") -> CategoryMeasurement:
    return CategoryMeasurement(category, len(text), estimate_tokens(text), detail)


def measure_role_appendix(base_role: str, project_ns: str) -> list[CategoryMeasurement]:
    """Reconstruct spawn_engine's role_md_file content, category by category.

    Calls the SAME leaf functions production spawn uses (agent_role_dir,
    role_memory.*, skill_policy.render_skill_appendix, the BIG_FILE_GUARD /
    STALE_FILE_GUARD / GRAFT_TOOL_CAVEATS constants, permission_gates) — only
    the sequencing is duplicated, not the content logic, so this tracks real
    behaviour rather than a hand-written template.
    """
    from . import role_memory, skill_policy
    from .config import agent_role_dir, default_cwd_for_role, lead_cwd, role_needs_stale_file_guard
    from .lead_context import (
        BIG_FILE_GUARD,
        GRAFT_TOOL_CAVEATS,
        STALE_FILE_GUARD,
        _allowed_project_roots,
    )
    from .orchestrator_text import _resolve_project_memory
    from .permission_gates import render_claude_gate_appendix
    from .provider_spec import PROVIDER_REGISTRY
    from .shared_dev_tools import role_mcp_allowlist

    out: list[CategoryMeasurement] = []

    staging = agent_role_dir(base_role)
    role_md_path = staging / "CLAUDE.md"
    try:
        base_text = role_md_path.read_text(encoding="utf-8") if role_md_path.is_file() else ""
    except OSError:
        base_text = ""
    out.append(_cat("role_file_base", base_text, str(role_md_path)))

    spawn_cwd = default_cwd_for_role(base_role, project=project_ns) or str(staging)

    try:
        mem_path = _resolve_project_memory(lead_cwd(project_ns) or spawn_cwd)
    except Exception:
        mem_path = None
    if mem_path is not None:
        # Fixed-template pointer block — length dominated by the path string
        # itself, which we have exactly; matches spawn_engine's real text.
        pointer = (
            "\n\n---\n\n## 📋 Project memory (Lead's constraint registry)\n\n"
            "Lead ของ project นี้มี auto-memory ที่บันทึก domain rules ไว้ที่:\n\n"
            f"`{mem_path}`\n\n"
            "อ่านก่อนเริ่มงานที่แตะ: dependency, lockfile, docker, ports, vendor "
            f'pattern, หรือ tool ที่โปรเจ็คระบุว่าใช้:\n\n```\nRead("{mem_path}")\n```\n\n'
            "MEMORY.md เป็น index — แต่ละ entry ชี้ไปยัง memory file ที่อธิบาย rule "
            "นั้นๆ อ่านเฉพาะ file ที่เกี่ยวกับงานของคุณ ไม่ต้องอ่านทั้งหมด"
        )
        out.append(_cat("project_memory_pointer", pointer, str(mem_path)))

    try:
        role_mem = role_memory.ensure_role_memory(project_ns, base_role)
    except Exception:
        role_mem = None
    if role_mem is not None:
        try:
            mem_text = role_mem.read_text(encoding="utf-8", errors="replace")
        except OSError:
            mem_text = ""
        try:
            has_content = role_memory.has_learned_content(mem_text, project_ns, base_role)
        except Exception:
            has_content = False
        if has_content:
            shown = "\n".join(mem_text.splitlines()[-200:])
            out.append(_cat("learned_notes", shown, str(role_mem)))
        else:
            out.append(_cat("learned_notes_empty_pointer", "x" * 120, str(role_mem)))

    try:
        roots = list(_allowed_project_roots(project_ns)) if project_ns else []
    except Exception:
        roots = []
    try:
        roots = roots or []
        skill_appendix = skill_policy.render_skill_appendix(
            base_role, roots, PROVIDER_REGISTRY["claude"].context_strategy
        )
    except Exception:
        skill_appendix = ""
    out.append(_cat("skill_matrix_appendix", skill_appendix))

    guard_text = BIG_FILE_GUARD
    if role_needs_stale_file_guard(base_role):
        guard_text += STALE_FILE_GUARD
    out.append(_cat("guards_fixed", guard_text))

    try:
        grants_graft = "graft" in (role_mcp_allowlist(base_role) or frozenset())
    except Exception:
        grants_graft = False
    if grants_graft:
        out.append(_cat("graft_caveats", GRAFT_TOOL_CAVEATS))

    try:
        gate_appendix = render_claude_gate_appendix(spawn_cwd)
    except Exception:
        gate_appendix = ""
    out.append(_cat("permission_gate_appendix", gate_appendix))

    return out


def measure_repo_claude_md(project_ns: str, base_role: str) -> list[CategoryMeasurement]:
    """Claude's OWN auto-discovery of CLAUDE.md — separate mechanism from
    `--append-system-prompt-file` above, walked from the role's spawn cwd up
    to the nearest `.git`, plus the user's global `~/.claude/CLAUDE.md`.
    Real repo content, not a cockpit knob, but real boot bytes all the same.
    """
    from .config import default_cwd_for_role

    out: list[CategoryMeasurement] = []
    global_md = Path.home() / ".claude" / "CLAUDE.md"
    if global_md.is_file():
        try:
            out.append(
                _cat("global_claude_md", global_md.read_text(encoding="utf-8"), str(global_md))
            )
        except OSError:
            pass

    cwd = default_cwd_for_role(base_role, project=project_ns)
    if not cwd:
        return out
    cur = Path(cwd)
    seen: set[Path] = set()
    for _ in range(10):
        md = cur / "CLAUDE.md"
        if md.is_file() and md not in seen:
            seen.add(md)
            try:
                out.append(_cat("repo_claude_md", md.read_text(encoding="utf-8"), str(md)))
            except OSError:
                pass
        if (cur / ".git").exists() or cur.parent == cur:
            break
        cur = cur.parent
    return out


def measure_mcp_config(
    base_role: str, project_ns: str, shard_idx: int | None = None
) -> CategoryMeasurement:
    """Server COUNT only — tool-schema token cost lives entirely inside each
    MCP server's own `tools/list` response and cannot be read from the
    argv/config file, only from a live handshake. Flagged, not guessed; see
    the ~15k tok/browser-MCP figure already documented in
    spawn_engine.py's own comment for the one calibration point that exists.
    """
    from .config import default_cwd_for_role
    from .mcp_bridge import mcp_argv_for_provider

    spawn_cwd = default_cwd_for_role(base_role, project=project_ns)
    try:
        argv = mcp_argv_for_provider("claude", base_role, shard_idx, project_ns, cwd=spawn_cwd)
    except Exception as exc:
        return CategoryMeasurement("mcp_config", 0, 0, f"could not resolve: {exc}")
    if "--mcp-config" not in argv:
        return CategoryMeasurement("mcp_config", 0, 0, "no --mcp-config for this role")
    cfg_path = Path(argv[argv.index("--mcp-config") + 1])
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        servers = list((data.get("mcpServers") or {}).keys())
    except Exception as exc:
        return CategoryMeasurement("mcp_config", 0, 0, f"{cfg_path}: unreadable ({exc})")
    detail = f"{cfg_path} — {len(servers)} server(s): {', '.join(servers) or '(none)'}"
    return CategoryMeasurement("mcp_config", 0, 0, detail)


def measure_native_project_memory(
    project_ns: str, base_role: str | None = None
) -> CategoryMeasurement | None:
    """Claude Code's own built-in `/memory` auto-load — NOT cockpit's
    `role_memory.py` (that's `learned_notes` above).

    Before #516 F1: `CLAUDE_CODE_PROJECT_DIR_NAME` (`pane_env.
    claude_project_dir_name`) was the SAME value for every role of a
    project, so this file was identical and auto-loaded for EVERY role's
    pane, not role-specific (see docs/audit/2026-09-07-boot-context.md F1).
    After the fix, `base_role` selects which pane's memory file this reads —
    `None`/`"lead"` for Lead's own (unsuffixed, unchanged) directory, any
    other role for that role's own `<project>-<role>` directory. A non-Lead
    role returns `None` here until that role has actually been spawned under
    the new naming (no file exists yet at the new path) — that is the
    expected, correct post-fix state, not a measurement bug.
    """
    from .pane_env import claude_project_dir_name
    from .user_profile import config_dir_for

    try:
        config_dir = config_dir_for(project_ns)
    except Exception:
        config_dir = Path.home() / ".claude"
    dirname = claude_project_dir_name(project_ns, base_role)
    mem_path = config_dir / "projects" / dirname / "memory" / "MEMORY.md"
    if not mem_path.is_file():
        return None
    try:
        text = mem_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    category = (
        "native_project_memory"
        if base_role and base_role != "lead"
        else "native_project_memory_LEAD"
    )
    return _cat(category, text, str(mem_path))


def measure_native_skill_catalog(project_ns: str) -> CategoryMeasurement | None:
    """Claude Code's OWN native skill-discovery catalog (the `skill_listing`
    system-prompt attachment — see docs/audit/2026-09-07-boot-context.md
    finding F2), a DIFFERENT mechanism from `skill_matrix_appendix` above
    (cockpit's `skill_policy.py` proactive-reference block, which only
    covers this PROJECT's own `.claude/skills/`). This one is scanned
    directly from `CLAUDE_CONFIG_DIR/skills/` (`user_profile.config_dir_for`
    — the exact directory every claude pane of this project reads its
    global skill catalog from, real files, not a guess) and is SHARED by
    every role of the project (profiles are per-project, not per-role — no
    per-role split to measure here, unlike F1's `native_project_memory`).

    Cross-referenced against this project's OWN skills (`skill_scan.
    scan_skills` over `_allowed_project_roots`) so the detail string can
    separate "this repo's own skill" from "an unrelated global skill riding
    along on every pane for free" — the #516 follow-up gap F2 flagged as
    unmeasured. Measuring this does NOT imply a safe way to gate it yet:
    `--disable-slash-commands` is the only discovered CLI lever and it is
    all-or-nothing (would also remove this project's own skills, and real
    30-day usage data showed teammates using unrelated catalog skills like
    `superpowers-dev`'s `test-driven-development` — see the audit doc's F3
    section for why that was deliberately not wired blind).

    Returns None when `CLAUDE_CONFIG_DIR/skills/` doesn't exist or has no
    skill files — nothing to report. Never raises."""
    from . import skill_scan
    from .lead_context import _allowed_project_roots
    from .user_profile import config_dir_for

    try:
        config_dir = config_dir_for(project_ns)
    except Exception:
        return None
    skills_dir = config_dir / "skills"
    files = skill_scan._skill_files(skills_dir)
    if not files:
        return None

    try:
        project_roots = _allowed_project_roots(project_ns)
    except Exception:
        project_roots = []
    project_names = {s.name for s in skill_scan.scan_skills(project_roots)}

    texts: list[str] = []
    names: list[str] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        texts.append(text)
        fm = skill_scan._parse_frontmatter(text)
        name = fm.get("name")
        if not isinstance(name, str) or not name.strip():
            name = f.parent.name if f.name == "SKILL.md" else f.stem
        names.append(name.strip())

    unrelated = [n for n in names if n not in project_names]
    detail = (
        f"{skills_dir} — {len(names)} skill(s), {len(unrelated)} not part of this "
        f"project's own .claude/skills"
        + (
            f" ({', '.join(unrelated[:8])}{'...' if len(unrelated) > 8 else ''})"
            if unrelated
            else ""
        )
    )
    return _cat("native_skill_catalog", "".join(texts), detail)


def build_report(base_role: str, project_ns: str) -> RoleBootReport:
    report = RoleBootReport(role=base_role, project=project_ns)
    report.categories.extend(measure_role_appendix(base_role, project_ns))
    report.categories.extend(measure_repo_claude_md(project_ns, base_role))
    report.categories.append(measure_mcp_config(base_role, project_ns))
    own_memory = measure_native_project_memory(project_ns, base_role)
    if own_memory is not None:
        report.categories.append(own_memory)
    return report


def format_report(
    reports: list[RoleBootReport],
    native_memory: CategoryMeasurement | None,
    native_skills: CategoryMeasurement | None = None,
) -> str:
    lines: list[str] = []
    if native_memory is not None:
        lines.append(
            f"[Lead only, post-#516-F1] native_project_memory: {native_memory.chars} chars, "
            f"~{native_memory.est_tokens} tok (lower bound) — {native_memory.detail}"
        )
        lines.append("")
    if native_skills is not None:
        lines.append(
            f"[shared across every role] native_skill_catalog: {native_skills.chars} chars, "
            f"~{native_skills.est_tokens} tok (lower bound) — {native_skills.detail} "
            "[dynamic-state, not gated]"
        )
        lines.append("")
    for r in reports:
        lines.append(
            f"=== {r.role} ({r.project}) — total est. {r.total_est_tokens} tok "
            f"(repo-controlled: {r.repo_controlled_est_tokens} tok — the ceiling-gated "
            "subset; the rest is per-machine dynamic state) ==="
        )
        for c in r.categories:
            tag = " [dynamic-state, not gated]" if c.category in DYNAMIC_STATE_CATEGORIES else ""
            lines.append(
                f"  {c.category:32s} {c.chars:7d} chars  ~{c.est_tokens:6d} tok  {c.detail}{tag}"
            )
        lines.append("")
    return "\n".join(lines)
