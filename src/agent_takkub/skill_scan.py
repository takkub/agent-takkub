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
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import config
from .worktree_manager import _make_link, _remove_link

_log = logging.getLogger(__name__)

# `git ls-files` here is local + fast; bound it so a wedged git can never
# freeze the project-open path (the migration runs on the Qt main thread).
_GIT_LS_TIMEOUT_S = 15

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


def _is_reparse_point(p: Path) -> bool:
    """True when `p` is a symlink OR a Windows junction (a directory reparse
    point that `Path.is_symlink()` does NOT flag). Used to tell an already-
    migrated/linked skill (leave it alone) from a real on-disk directory (a
    migration candidate). Never raises."""
    try:
        if p.is_symlink():
            return True
    except OSError:
        return False
    # A junction resolves to somewhere other than `<realpath(parent)>/<name>`;
    # a real directory resolves to exactly that. Comparing against the parent's
    # realpath keeps a link *above* `p` from producing a false positive.
    try:
        real = os.path.realpath(str(p))
        expected = os.path.join(os.path.realpath(str(p.parent)), p.name)
    except OSError:
        return False
    return os.path.normcase(real) != os.path.normcase(expected)


def _git_tracked_skill_names(project_root: Path) -> set[str] | None:
    """Names of skills under `<project_root>/.claude/skills/` that git tracks
    (i.e. the user committed them → user-owned, never migrate).

    Returns None when `project_root` is not a git work tree (or git is
    unavailable) — the caller treats that as "cannot prove ownership, leave
    everything alone". An empty set means "git repo, but nothing tracked".
    Never raises."""
    try:
        res = subprocess.run(
            ["git", "-C", str(project_root), "ls-files", "-z", "--", ".claude/skills"],
            capture_output=True,
            timeout=_GIT_LS_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:  # 128 = not a git repository
        return None
    out = res.stdout.decode("utf-8", errors="replace")
    names: set[str] = set()
    for entry in out.split("\0"):
        entry = entry.strip()
        if not entry:
            continue
        # git always emits forward slashes: ".claude/skills/<name>/SKILL.md".
        parts = entry.split("/")
        if len(parts) >= 3 and parts[0] == ".claude" and parts[1] == "skills":
            names.add(parts[2])
    return names


@dataclass(frozen=True)
class SkillMigration:
    """One entry in a legacy-skill migration report (see
    `migrate_legacy_project_skills`). `action` is one of: ``migrated`` /
    ``would-migrate`` (dry-run) / ``skipped-tracked`` / ``skipped-linked`` /
    ``skipped-conflict`` / ``skipped-non-git`` / ``error``."""

    name: str
    action: str
    detail: str = ""


def _migrate_one_skill(
    src_dir: Path, central: Path, project_root: Path, project_ns: str, name: str
) -> str | None:
    """Move one real skill dir `src_dir` → `central`, then junction/symlink it
    back to `src_dir`'s path. Returns None on success, an error string
    otherwise. On a link failure AFTER the move, the central dir is moved back
    to the project so the skill is never lost."""
    link_path = project_root / ".claude" / "skills" / name
    try:
        central.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_dir), str(central))
    except OSError as e:
        return f"move failed: {e}"
    err = _link_skill_into_project(project_root, project_ns, name)
    if err:
        # Roll back so the skill stays discoverable at its original path.
        try:
            _remove_link(link_path)
            if not link_path.exists() and central.is_dir():
                shutil.move(str(central), str(link_path))
        except OSError:
            pass
        return f"link failed after move (rolled back): {err}"
    return None


def migrate_legacy_project_skills(
    project_root: str | Path, project_ns: str, *, dry_run: bool = False
) -> list[SkillMigration]:
    """One-time (idempotent) migration of legacy cockpit-created skills sitting
    as REAL directories under `<project_root>/.claude/skills/` into the central
    `project_skills_dir(project_ns)`, leaving a junction/symlink behind.

    Safe by construction — only a skill that is BOTH:
      * a real directory (not already a junction/symlink to central), AND
      * git-UNtracked (cockpit wrote it; the user never committed it)
    is moved. A git-tracked skill is the user's own committed skill and is left
    untouched; a project that is not a git repo is skipped entirely (ownership
    can't be proven). A name that already exists centrally is skipped (no
    clobber). Nothing is ever deleted.

    `dry_run=True` reports what WOULD move (`action="would-migrate"`) without
    touching the filesystem. Returns one `SkillMigration` per skill directory
    inspected (skips are reported too, for a clear log); never raises.
    """
    project_root = Path(project_root)
    skills_dir = project_root / ".claude" / "skills"
    if not skills_dir.is_dir():
        return []
    try:
        central_base = config.project_skills_dir(project_ns)
    except ValueError:
        return []

    tracked = _git_tracked_skill_names(project_root)
    records: list[SkillMigration] = []
    for child in sorted(skills_dir.iterdir()):
        name = child.name
        if _is_reparse_point(child):
            records.append(SkillMigration(name, "skipped-linked", "already a link"))
            continue
        if not child.is_dir():
            continue  # flat `<name>.md` legacy layout — leave as-is
        if tracked is None:
            records.append(
                SkillMigration(
                    name,
                    "skipped-non-git",
                    "not a git repo — cannot prove ownership, left in place",
                )
            )
            continue
        if name in tracked:
            records.append(SkillMigration(name, "skipped-tracked", "git-tracked (user-owned)"))
            continue
        central = central_base / name
        if central.exists():
            records.append(
                SkillMigration(name, "skipped-conflict", f"central already has {central}")
            )
            continue
        if dry_run:
            records.append(SkillMigration(name, "would-migrate", f"→ {central}"))
            continue
        err = _migrate_one_skill(child, central, project_root, project_ns, name)
        if err:
            records.append(SkillMigration(name, "error", err))
        else:
            records.append(SkillMigration(name, "migrated", f"→ {central}"))
            _log.info("migrated legacy skill %r → central (%s)", name, central)
    return records


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
            tmp_path = Path(tmp.name)
            tmp.write(content)
    except OSError as e:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
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


def read_skill(path: Path) -> tuple[dict, str]:
    """Return ``(frontmatter, body)`` for an existing ``SKILL.md`` — the
    public counterpart of ``_parse_frontmatter`` that also hands back the
    body text after the ``---`` fence, so callers (the Settings UI's Skill
    detail pane) can show/edit instructions without re-implementing the
    split. Never raises: unreadable/malformed files return ``({}, "")``."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, ""
    fm = _parse_frontmatter(text)
    if not text.startswith("---"):
        return fm, text
    parts = text.split("---", 2)
    body = parts[2].lstrip("\n") if len(parts) == 3 else text
    return fm, body


def update_skill(path: Path, description: str, instructions: str) -> tuple[bool, str]:
    """Read-modify-write an existing ``SKILL.md`` in place, changing only
    ``description`` and the body — every OTHER frontmatter key (``name``,
    plus any hand-authored extra like ``license``) is preserved verbatim.
    ``name`` is immutable via this path (a skill's identity is its
    directory name; renaming means create+delete). Returns ``(ok, message)``
    — ``message`` is "" on success. Never raises."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, f"อ่าน {path} ไม่สำเร็จ"
    fm = _parse_frontmatter(text)
    if not fm:
        fm = {"name": path.parent.name if path.name == "SKILL.md" else path.stem}

    fm["description"] = (description or "").strip()
    body = (
        instructions or ""
    ).strip() or f"# {fm.get('name', path.parent.name)}\n\n_(เพิ่มเนื้อหา skill นี้)_\n"
    try:
        frontmatter_text = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip()
    except yaml.YAMLError as e:
        return False, f"เขียน frontmatter ไม่สำเร็จ: {e}"
    content = f"---\n{frontmatter_text}\n---\n\n{body}\n"

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(content)
        tmp_path.replace(path)
    except OSError as e:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        return False, f"เขียน SKILL.md ไม่สำเร็จ: {e}"
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
