"""Scan `.claude/skills/` for real Claude Code skills (SKILL.md frontmatter).

Distinct from :mod:`skill_audit` (TF-IDF overlap of *role* docs under
`.claude/agents/`) — this scans actual *skill* files loaded via the Skill
tool, so the New Role form (`settings_window.py`) can let a role reference
skills that really exist instead of the user re-typing skill knowledge into
free-text Instructions.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str
    path: Path


def _parse_frontmatter(text: str) -> dict:
    """Same tolerant `---\\nyaml\\n---` split as design_review_html/vault_graph
    use for markdown front matter. Never raises — malformed YAML -> {}."""
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) != 3:
        return {}
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}
    return fm if isinstance(fm, dict) else {}


def _skill_files(skills_dir: Path) -> list[Path]:
    """`<skills_dir>/<name>/SKILL.md` (the real Claude Code layout) plus any
    flat `<skills_dir>/*.md`, for tolerance of ad-hoc layouts."""
    if not skills_dir.is_dir():
        return []
    nested = sorted(skills_dir.glob("*/SKILL.md"))
    flat = sorted(p for p in skills_dir.glob("*.md") if p.is_file())
    return nested + flat


def scan_skills(roots: Path | list[Path]) -> list[SkillInfo]:
    """Every skill found under `<root>/.claude/skills/` for each root in
    `roots` (a single Path is also accepted), deduped by skill name — first
    root wins. Never raises: a missing/unreadable skills dir just yields
    fewer results, mirroring `skill_audit.load_role_docs`'s tolerance."""
    if isinstance(roots, Path):
        roots = [roots]
    seen: dict[str, SkillInfo] = {}
    for root in roots:
        for f in _skill_files(root / ".claude" / "skills"):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fm = _parse_frontmatter(text)
            name = fm.get("name")
            if not isinstance(name, str) or not name.strip():
                name = f.parent.name if f.name == "SKILL.md" else f.stem
            name = name.strip()
            if not name or name in seen:
                continue
            description = fm.get("description")
            description = description.strip() if isinstance(description, str) else ""
            seen[name] = SkillInfo(name=name, description=description, path=f)
    return sorted(seen.values(), key=lambda s: s.name)
