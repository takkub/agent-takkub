"""Scan `.claude/skills/` for real Claude Code skills (SKILL.md frontmatter).

Distinct from :mod:`skill_audit` (TF-IDF overlap of *role* docs under
`.claude/agents/`) — this scans actual *skill* files loaded via the Skill
tool, so the New Role form (`settings_window.py`) can let a role reference
skills that really exist instead of the user re-typing skill knowledge into
free-text Instructions.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import config
from .worktree_manager import _make_link, _remove_link

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


def _link_skill_into_project(project_root: Path, project_ns: str, name: str) -> str | None:
    """Ensure `<project_root>/.claude/skills/<name>` links to the central
    real skill dir `project_skills_dir(project_ns)/<name>`.

    Returns None on success (or when the link already points at the right
    target), an error string otherwise. Uses `worktree_manager._make_link`
    (Windows junction / POSIX symlink — both work without admin for a dir),
    so a central skill still shows up where every CLI discovers skills.
    Never clobbers a *real* directory already sitting at the project path
    (a user's own committed skill of the same name) — it's left untouched.
    """
    central = config.project_skills_dir(project_ns) / name
    if not central.is_dir():
        return f"central skill missing: {central}"
    dst = project_root / ".claude" / "skills" / name
    try:
        real_dst = os.path.realpath(str(dst))
    except OSError:
        real_dst = str(dst)
    if dst.exists():
        # Already present: only accept it if it resolves to our central dir.
        # A real user skill (or a link elsewhere) at this name is left alone.
        if real_dst == os.path.realpath(str(central)):
            return None
        return None  # foreign dir/link at this name — do not clobber
    # Missing (or a dangling reparse point): clear any stale link, then make.
    _remove_link(dst)
    return _make_link(central, dst)


def ensure_project_skill_links(project_root: str | Path, project_ns: str) -> list[str]:
    """(Re)create the junction/symlink for every central skill of
    `project_ns` into `<project_root>/.claude/skills/`. Idempotent — safe to
    call on every project open. Returns a list of error strings (empty when
    all links are healthy); never raises.

    This is the "repair on open" half of the central-skills design: a skill
    created in one session, or a link deleted/broken between sessions, is
    re-linked here so claude/codex/agy keep discovering it from cwd.
    """
    project_root = Path(project_root)
    try:
        central = config.project_skills_dir(project_ns)
    except ValueError:
        return []
    if not central.is_dir():
        return []
    errors: list[str] = []
    for skill_dir in sorted(central.iterdir()):
        if not skill_dir.is_dir():
            continue
        err = _link_skill_into_project(project_root, project_ns, skill_dir.name)
        if err:
            errors.append(f"{skill_dir.name}: {err}")
    return errors


def create_skill(
    root: Path,
    name: str,
    description: str,
    instructions: str,
    *,
    project_ns: str | None = None,
    existing: Iterable[str] = (),
) -> tuple[bool, str]:
    """Validate + write a new skill's `SKILL.md`, then make it discoverable
    from `<root>/.claude/skills/<name>`.

    When `project_ns` is given (the normal path from the Settings UI) the
    real file is written to the central `project_skills_dir(project_ns)/
    <name>/SKILL.md` and a junction/symlink links `<root>/.claude/skills/
    <name>` back to it — so the New-Skill button never dirties the user's
    repo. When `project_ns` is None (no active project, e.g. a bare test),
    it falls back to writing directly under `root` (legacy behaviour).

    Returns (ok, message) — message is "" on success, an error string
    otherwise. Never raises. Writes the frontmatter file to a temp path
    first, then renames it into the skill dir last — same ordering
    `custom_roles.create_role` uses so a partial failure never leaves an
    empty/broken skill folder behind.
    """
    ok, err = validate_skill_name(name, existing)
    if not ok:
        return False, err
    name = name.strip().lower()
    description = (description or "").strip()
    body = (instructions or "").strip() or f"# {name}\n\n_(เพิ่มเนื้อหา skill นี้)_\n"
    content = f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"

    if project_ns:
        try:
            store_dir = config.project_skills_dir(project_ns)
        except ValueError as e:
            return False, f"project ไม่ถูกต้อง: {e}"
    else:
        store_dir = root / ".claude" / "skills"
    skill_dir = store_dir / name
    link_path = root / ".claude" / "skills" / name
    if skill_dir.exists():
        return False, f"skill '{name}' มีอยู่แล้วที่ {skill_dir}"
    if project_ns and link_path.exists():
        return False, f"skill '{name}' มีอยู่แล้วที่ {link_path}"

    tmp_path: Path | None = None
    try:
        store_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=store_dir, suffix=".md", delete=False, encoding="utf-8"
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

    if project_ns:
        err = _link_skill_into_project(root, project_ns, name)
        if err:
            # Link failed — the central file exists but claude won't discover
            # it from cwd. Roll back so the UI's create/scan stays consistent
            # (no orphaned central skill that never shows up in the project).
            # Clear any partial reparse point first (junction-safe), then the
            # central real dir.
            _remove_link(root / ".claude" / "skills" / name)
            shutil.rmtree(skill_dir, ignore_errors=True)
            return False, f"สร้าง junction skill ไม่สำเร็จ: {err}"

    return True, ""


def delete_skill(path: Path) -> bool:
    """Remove a skill given the `SkillInfo.path` `scan_skills` returned for
    it. Nested layout (`.../<name>/SKILL.md`) removes the whole skill
    folder; flat layout (`.../<name>.md`) removes just that file. Never
    raises — an OSError just means the delete didn't happen.

    Junction-safe: when the skill dir is reached through a project-side
    junction/symlink pointing at the central store, the reparse point is
    unlinked FIRST (via `worktree_manager._remove_link`, which never
    recurses into a target) and then the real central dir is removed — so a
    naive `rmtree` can never delete through the link into the central store.
    A real (non-linked) skill dir is removed directly, as before.
    """
    try:
        if path.name == "SKILL.md":
            skill_dir = path.parent
            # Resolve the real target BEFORE touching the (possibly) link.
            try:
                real = Path(os.path.realpath(str(skill_dir)))
            except OSError:
                real = skill_dir
            # Remove the project-side link point (no-op / safe on a real dir).
            _remove_link(skill_dir)
            if real != skill_dir and real.exists():
                shutil.rmtree(real, ignore_errors=True)
            elif skill_dir.exists():
                shutil.rmtree(skill_dir, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        return True
    except OSError as e:
        _log.warning("delete_skill: could not remove %s: %s", path, e)
        return False


def is_writable_skill(
    path: Path,
    writable_roots: Iterable[Path],
    extra_dirs: Iterable[Path] = (),
) -> bool:
    """Whether `path` (a `SkillInfo.path`) is one the Settings UI may delete
    — used to gate the delete button so a bundled/cockpit-checkout skill
    (shipped, read-only) never gets one.

    True when the *resolved* path lives under either:
      - `<root>/.claude/skills/` for some `root` in `writable_roots` (a
        project's own on-disk skills), or
      - one of `extra_dirs` directly (the central `project_skills_dir`,
        where junctioned skills resolve to — passed by the Settings UI).
    Resolves both sides so junctions/symlinks compare correctly; never
    raises."""
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
    for d in extra_dirs:
        try:
            if resolved.is_relative_to(Path(d).resolve()):
                return True
        except OSError:
            continue
    return False
