"""Per-role skill injection policy — issue #103 phase 4.

Mirrors :mod:`pane_tools_policy`'s role→item allowlist pattern (same JSON
shape, same atomic tmp+replace save) but governs which real
``.claude/skills/*/SKILL.md`` entries get PROACTIVELY referenced in a
role's spawn-time context — a different concern from the New Role
picker's one-time embed (``settings_window._append_skill_references``,
which bakes a "## Skills ที่เกี่ยวข้อง" block into a *custom* role's saved
instructions text at creation time and never touches it again). This
policy instead applies to EVERY role (built-in + custom) and is resolved
fresh on every spawn, exactly like ``pane_tools_policy.effective_mcps``.

Schema (``~/.takkub/skill-policy.json``):
  {"version": 1, "roles": {"<role>": ["skill-name", ...]}}

A role with no entry gets NO injected skill references. Unlike
``pane_tools_policy``'s MCP/plugin defaults there is no built-in
skill-per-role default table to fall back to — every role starts
opted-out until an operator explicitly ticks it in the Skill Matrix
(Settings → SKILL section).

**Spawn-time rendering** (`render_skill_appendix`) is the other half:
turns a role's assigned skill names (filtered down to skills that
actually exist in the current project) into a markdown block, shaped by
the spawning provider's ``provider_spec.ProviderSpec.context_strategy``:

- ``"append_system_prompt_file"`` (claude): the pane's own Skill tool
  already auto-discovers ``.claude/skills/`` project-wide regardless of
  role — this policy doesn't grant access, it just names which skills
  matter for THIS role so the pane proactively reads them instead of
  waiting to stumble onto them.
- ``"agents_md_file"`` (codex/gemini): these CLIs have no Skill tool at
  all. The block instead points at each skill's real file path for the
  agent to ``Read()`` itself, and says explicitly that only the
  instruction *text* is bridged this way — a skill that depends on the
  Skill tool's own execution machinery (auto-trigger, bundled scripts the
  tool runs on the agent's behalf) cannot be bridged through a plain
  markdown file. This is a documented gap, not a silent degrade.

**Import constraint:** this module MUST NOT import ``app``, ``cli``, or
``orchestrator`` (mirrors ``pane_tools_policy``'s leaf-module contract).
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from pathlib import Path

from .config import SETTINGS_HOME
from .pane_tools_policy import known_roles

_log = logging.getLogger(__name__)

SKILL_POLICY_FILE = SETTINGS_HOME / "skill-policy.json"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$", re.IGNORECASE)

# Providers whose CLI has a native Skill tool that auto-discovers
# `.claude/skills/` on its own — the appendix for these is a proactive
# nudge, not a content bridge (see module docstring).
_EXECUTABLE_CONTEXT_STRATEGY = "append_system_prompt_file"
# Providers with no Skill tool at all — content must be bridged into their
# cheatsheet file (AGENTS.md) as plain instruction text.
_INSTRUCTION_CONTEXT_STRATEGY = "agents_md_file"


def skill_matrix_roles() -> tuple[str, ...]:
    """Every role that gets a Skill Matrix row.

    Unlike the MCP/Plugins matrices (`pane_tools_dialog.matrix_roles()`),
    codex and gemini ARE included here — they're the whole point of the
    AGENTS.md-bridge half of this policy. Only `shell` is excluded (a
    plain terminal, no agent present to read anything).
    """
    from . import roles as _roles_mod

    return tuple(r for r in _roles_mod.all_role_names() if r != "shell")


def _validate_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))


def load_policy() -> dict[str, list[str]]:
    """Load ``{role: [skill_name, ...]}``. Missing/corrupt/empty file -> {}.

    Never raises. Unknown roles and invalid names are silently filtered,
    mirroring `pane_tools_policy.load_policy`'s tolerance.
    """
    if not SKILL_POLICY_FILE.is_file():
        return {}
    try:
        data = json.loads(SKILL_POLICY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _log.debug("load_policy: could not read %s: %s", SKILL_POLICY_FILE, e)
        return {}
    if not isinstance(data, dict):
        return {}
    roles = data.get("roles")
    if not isinstance(roles, dict):
        return {}

    out: dict[str, list[str]] = {}
    for role, names in roles.items():
        if not isinstance(role, str) or role not in known_roles():
            continue
        if not isinstance(names, list):
            continue
        out[role] = [n for n in names if isinstance(n, str) and _validate_name(n)]
    return out


def save_policy(policy: dict[str, list[str]]) -> bool:
    """Atomically persist *policy* (tmp + replace). Never raises.

    Empty policy deletes the file (idempotent). Rejects the whole write
    (returns False, no partial write) if any role/name is invalid.
    """
    if not policy:
        try:
            SKILL_POLICY_FILE.unlink(missing_ok=True)
        except OSError as e:
            _log.warning("save_policy: could not delete %s: %s", SKILL_POLICY_FILE, e)
            return False
        return True

    for role, names in policy.items():
        if not isinstance(role, str) or role not in known_roles():
            _log.warning("save_policy: rejecting invalid role %r", role)
            return False
        if not isinstance(names, list) or not all(
            isinstance(n, str) and _validate_name(n) for n in names
        ):
            _log.warning("save_policy: role %r has invalid skill name(s)", role)
            return False

    payload = {"version": 1, "roles": policy}
    tmp_path: Path | None = None
    try:
        SKILL_POLICY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=SKILL_POLICY_FILE.parent,
            suffix=".json",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            json.dump(payload, tmp, indent=2, ensure_ascii=False)
            tmp.write("\n")
            tmp_path = Path(tmp.name)
        tmp_path.replace(SKILL_POLICY_FILE)
        return True
    except OSError as e:
        _log.warning("save_policy: could not write %s: %s", SKILL_POLICY_FILE, e)
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def effective_skills(role: str) -> list[str]:
    """Skill names assigned to `role`, or [] if none configured.

    Unlike `pane_tools_policy`'s MCP/plugin `effective_*` helpers there is
    no None-vs-empty distinction to preserve here: a role with no policy
    entry and a role explicitly emptied both mean "inject nothing" — there
    is no built-in default set this could fall back to.
    """
    return list(load_policy().get(role, ()))


def set_role_skills(role: str, names: list[str]) -> bool:
    """Update the skill allowlist for a role. Returns True on success."""
    if role not in known_roles():
        _log.warning("set_role_skills: invalid role %r", role)
        return False
    if not all(isinstance(n, str) and _validate_name(n) for n in names):
        _log.warning("set_role_skills: role %r has invalid skill name(s)", role)
        return False
    policy = load_policy()
    policy[role] = list(names)
    return save_policy(policy)


def render_skill_appendix(
    role: str,
    roots: list[Path],
    context_strategy: str,
) -> str:
    """Markdown appendix for the skills the Skill Matrix assigns to `role`.

    Filters the role's assigned skill names down to skills that actually
    exist under `roots`'s `.claude/skills/` (a policy entry naming a
    skill this project doesn't have is silently dropped, not surfaced as
    an error — same tolerance as every other policy loader in this
    codebase). Returns "" when the role has no assignment, none of the
    assigned skills resolve, or `context_strategy` isn't one this module
    knows how to bridge.

    See module docstring for what each `context_strategy` value means.
    """
    from . import skill_scan

    assigned = effective_skills(role)
    if not assigned:
        return ""
    available = {s.name: s for s in skill_scan.scan_skills(roots)}
    skills = [available[n] for n in assigned if n in available]
    if not skills:
        return ""

    if context_strategy == _EXECUTABLE_CONTEXT_STRATEGY:
        lines = "\n".join(
            f"- อ่าน skill: {s.name} — {s.description} ก่อนเริ่มงานที่เกี่ยวข้อง"
            if s.description
            else f"- อ่าน skill: {s.name} ก่อนเริ่มงานที่เกี่ยวข้อง"
            for s in skills
        )
        return f"\n\n---\n\n## 🧩 Skills ที่กำหนดให้ role นี้ (Skill Matrix)\n{lines}\n"

    if context_strategy == _INSTRUCTION_CONTEXT_STRATEGY:
        lines = "\n".join(
            f"- **{s.name}**" + (f" — {s.description}" if s.description else "") + "\n"
            f"  อ่านไฟล์เต็มก่อนเริ่มงานที่เกี่ยวข้อง: `{s.path}`"
            for s in skills
        )
        return (
            "\n\n---\n\n## 🧩 Skills ที่กำหนดให้ role นี้ (Skill Matrix)\n\n"
            "⚠️ CLI นี้ไม่มีระบบ Skill tool ของ Claude Code — เนื้อหาด้านล่างเป็น "
            "**instruction-style เท่านั้น** (เปิดไฟล์อ่านตรงๆ) ความสามารถที่ skill "
            "ต้องพึ่ง Skill tool จริง (auto-trigger ตาม description, bundled "
            "scripts ที่ tool รันเอง) **ข้ามผ่าน bridge นี้ไม่ได้**:\n\n"
            f"{lines}\n"
        )

    return ""
