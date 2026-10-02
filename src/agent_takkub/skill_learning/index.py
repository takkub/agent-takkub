"""IDX — the learned-skill index injected into every pane's boot context.

Recall must not depend on a provider happening to surface a skill: claude
and codex discover SKILL.md natively, gemini/opencode/cursor don't, and even
native discovery only sees the description. So every spawn (teammate and
Lead, every provider) gets the same grouped one-line index; providers
without native discovery also get the file path to open.

Empty library → empty string (no context spent).
"""

from __future__ import annotations

from collections import defaultdict

from . import store
from .settings import load

_LINE_DESC = 80
_MAX_LINES = 60


def _native(provider: str) -> bool:
    try:
        from ..provider_spec import PROVIDER_REGISTRY

        spec = PROVIDER_REGISTRY.get(provider)
        return bool(spec and spec.native_skill_dir)
    except Exception:
        return False


def render(project_ns: str, provider: str = "claude", *, for_lead: bool = False) -> str:
    try:
        recs = store.list_skills(project_ns)
    except Exception:
        return ""
    if not recs:
        return ""
    # codex's native home only carries GLOBAL skills (native_skills.py), so a
    # project skill still needs its path there; claude links both layers.
    path_for = (
        (lambda r: False)
        if provider == "claude"
        else ((lambda r: r.layer == "project") if _native(provider) else (lambda r: True))
    )
    groups: dict[str, list[store.SkillRecord]] = defaultdict(list)
    for r in recs:
        groups[r.category].append(r)
    lines: list[str] = []
    for cat in sorted(groups):
        lines.append(f"**{cat}**")
        for r in sorted(groups[cat], key=lambda x: x.name):
            desc = (
                r.description
                if len(r.description) <= _LINE_DESC
                else r.description[: _LINE_DESC - 1] + "…"
            )
            tag = "" if r.layer == "project" else " · global"
            line = f"- `{r.name}`{tag} — {desc}"
            if path_for(r):
                line += f"  ↳ `{r.skill_file}`"
            lines.append(line)
    if len(lines) > _MAX_LINES:
        more = f"- …อีก {len(lines) - _MAX_LINES} บรรทัด (`takkub skills learned list`)"
        lines = [*lines[:_MAX_LINES], more]
    head = (
        "\n\n---\n\n## 🧠 Skills ที่ทีมเรียนรู้จากงานจริง (Skill Learning)\n\n"
        "บทเรียนที่ระบบกลั่นจากงานก่อนๆ ของโปรเจกต์นี้ — ถ้างานตรงกับ skill ไหน ให้เปิดอ่านก่อนลงมือ "
        "แล้ว **ใส่ `[skill: <name>]` ใน done note** ทุกตัวที่ทำตาม (ระบบใช้นับว่า skill มีประโยชน์จริง; "
        "skill ที่ไม่มีใครใช้จะถูก archive เอง)\n\n"
    )
    out = head + "\n".join(lines) + "\n"
    if for_lead:
        out += _last_run_line(project_ns)
    return out


def _last_run_line(project_ns: str) -> str:
    last = store.read_json(store.state_dir(project_ns) / "last_run.json", {})
    if not last:
        return ""
    mode = load().mode
    return f"\nรอบล่าสุด ({mode}): {last.get('summary', '-')}\n"
