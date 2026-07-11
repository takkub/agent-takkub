"""Scan `.claude/skills/` for real Claude Code skills (SKILL.md frontmatter).

Distinct from :mod:`skill_audit` (TF-IDF overlap of *role* docs under
`.claude/agents/`) — this scans actual *skill* files loaded via the Skill
tool, so the New Role form (`settings_window.py`) can let a role reference
skills that really exist instead of the user re-typing skill knowledge into
free-text Instructions.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

_log = logging.getLogger(__name__)

# Same charset as `custom_roles.validate_role_name`/`config.validate_name`
# (a-z0-9, -, _, 1-64 chars, must start alnum) — a skill name becomes a
# `.claude/skills/<name>/` path component, so it's held to the same
# traversal-safe rule as every other path-derived name in the cockpit.
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


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


def validate_skill_name(name: str, existing: Iterable[str] = ()) -> tuple[bool, str]:
    """Return (ok, error_message) — error_message is "" when ok. Mirrors
    `custom_roles.validate_role_name`'s shape/tone for the settings UI's New
    Skill form. `existing` is the set of skill names already visible in the
    current scan (case-insensitive collision check) — callers pass
    `{s.name for s in scan_skills(roots)}`."""
    name = (name or "").strip()
    if not name:
        return False, "ชื่อห้ามว่าง"
    if not _SKILL_NAME_RE.fullmatch(name.lower()):
        return False, "ชื่อต้องเป็น a-z0-9 กับ - _ เท่านั้น (เริ่มด้วยตัวอักษร/ตัวเลข, ยาวไม่เกิน 64)"
    if name.lower() in {e.lower() for e in existing}:
        return False, f"skill '{name}' มีอยู่แล้ว"
    return True, ""


def create_skill(
    root: Path,
    name: str,
    description: str,
    instructions: str,
    *,
    existing: Iterable[str] = (),
) -> tuple[bool, str]:
    """Validate + write `<root>/.claude/skills/<name>/SKILL.md`.

    Returns (ok, message) — message is "" on success, an error string
    otherwise. Never raises. Writes the frontmatter file to a temp path
    first, then creates the skill dir and renames it into place last — same
    ordering `custom_roles.create_role` uses so a partial failure never
    leaves an empty/broken skill folder behind.
    """
    ok, err = validate_skill_name(name, existing)
    if not ok:
        return False, err
    name = name.strip().lower()
    description = (description or "").strip()
    body = (instructions or "").strip() or f"# {name}\n\n_(เพิ่มเนื้อหา skill นี้)_\n"
    content = f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"

    skills_dir = root / ".claude" / "skills"
    skill_dir = skills_dir / name
    if skill_dir.exists():
        return False, f"skill '{name}' มีอยู่แล้วที่ {skill_dir}"

    tmp_path: Path | None = None
    try:
        skills_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=skills_dir, suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
    except OSError as e:
        return False, f"เขียน SKILL.md ไม่สำเร็จ: {e}"

    try:
        skill_dir.mkdir(parents=True, exist_ok=False)
        tmp_path.replace(skill_dir / "SKILL.md")
    except OSError as e:
        tmp_path.unlink(missing_ok=True)
        try:
            skill_dir.rmdir()
        except OSError:
            pass
        return False, f"สร้าง skill ไม่สำเร็จ: {e}"

    return True, ""


def delete_skill(path: Path) -> bool:
    """Remove a skill given the `SkillInfo.path` `scan_skills` returned for
    it. Nested layout (`.../<name>/SKILL.md`) removes the whole skill
    folder; flat layout (`.../<name>.md`) removes just that file. Never
    raises — an OSError just means the delete didn't happen."""
    try:
        if path.name == "SKILL.md":
            shutil.rmtree(path.parent, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        return True
    except OSError as e:
        _log.warning("delete_skill: could not remove %s: %s", path, e)
        return False


def is_writable_skill(path: Path, writable_roots: Iterable[Path]) -> bool:
    """Whether `path` (a `SkillInfo.path`) lives under one of `writable_roots`
    — used to gate the Settings UI's delete button so a bundled/cockpit-
    checkout skill (shipped, not a project's own) never gets one. Resolves
    both sides so symlinks/relative roots compare correctly; never raises."""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in writable_roots:
        try:
            if resolved.is_relative_to((root / ".claude" / "skills").resolve()):
                return True
        except OSError:
            continue
    return False
