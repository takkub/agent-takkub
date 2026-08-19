"""Shipped-skill store surface (Phase 5a, epic #309 Capability Hub).

The shipped skill bundle now physically lives at
``config.ASSETS_ROOT/capabilities/skills`` — provider-neutral storage, no
longer nested under ``.claude/`` (see
``docs/v2/REUSE_VS_REWRITE_MATRIX.md`` §3 "Skill store path": REPLACE,
queued after 1.0.74; ``config._resolve_skills_dir`` is the read side).

Nothing that *reads* skills changed: `skill_scan.scan_skills`,
`skill_policy.render_skill_appendix`, and every caller that hands them a
root (`settings_window`, `spawn_engine._skill_roots_for_project`,
`settings_management.repositories.skills`) are untouched — they still
hard-code ``<root>/.claude/skills``. Claude's own Skill tool ALSO only
ever auto-discovers ``.claude/skills`` from cwd; it has no notion of a
``capabilities/`` dir. So all of those keep working only because
`ensure_shipped_skill_surface()` below (re)creates a per-skill junction
(Windows) / symlink (macOS/Linux) at ``<assets_root>/.claude/skills/<name>``
pointing at the real ``capabilities/skills/<name>`` — the exact same
primitive (`worktree_manager._make_link` / `_remove_link`) that
`skill_scan._link_skill_into_project` already uses for per-project custom
skills, just applied to the shipped bundle instead. Per-skill links (not
one link for the whole `skills/` directory), mirroring that function, so
``.claude/skills/`` itself stays a real, ordinary directory — never
itself a reparse point — which keeps `git status` legible and never
clobbers a foreign real directory a user happens to have at that name.

Idempotent + best-effort: safe to call on every spawn
(`spawn_engine._skill_roots_for_project`) and on every `doctor` run.
Never raises.
"""

from __future__ import annotations

import logging
from pathlib import Path

from agent_takkub import config
from agent_takkub.worktree_manager import _make_link, _remove_link

_log = logging.getLogger(__name__)


def shipped_skills_root() -> Path:
    """The real (non-surface) shipped skill storage dir — always
    ``ASSETS_ROOT/capabilities/skills``, regardless of whether
    `config.SKILLS_DIR` is currently falling back to the legacy
    ``.claude/skills`` path (a checkout that hasn't been migrated yet has
    no `capabilities/` dir at all, so there is nothing here to surface)."""
    return config.ASSETS_ROOT / "capabilities" / "skills"


def ensure_shipped_skill_surface() -> list[str]:
    """(Re)link every skill under the new `capabilities/skills` store into
    ``.claude/skills/<name>`` so every existing reader — claude's Skill
    tool included — keeps discovering the same skills at the old path.

    No-op (returns ``[]``) when the new store doesn't exist yet: a legacy
    layout means ``.claude/skills`` already holds the real files, nothing
    to link. Never clobbers a foreign real directory already sitting at a
    skill's name (mirrors `skill_scan._link_skill_into_project`'s
    never-clobber rule). Returns a list of error strings, one per skill
    whose link could not be (re)created; empty when everything is healthy.
    """
    real_root = shipped_skills_root()
    if not real_root.is_dir():
        return []
    surface_root = config.ASSETS_ROOT / ".claude" / "skills"
    errors: list[str] = []
    for skill_dir in sorted(p for p in real_root.iterdir() if p.is_dir()):
        dst = surface_root / skill_dir.name
        try:
            real_target = Path(skill_dir).resolve()
            real_dst = dst.resolve() if dst.exists() else None
        except OSError:
            real_dst = None
            real_target = skill_dir
        if dst.exists():
            if real_dst != real_target:
                _log.debug("skill_store: leaving foreign dir at %s alone (not our link)", dst)
            continue
        _remove_link(dst)  # clear a stale/dangling reparse point, if any
        err = _make_link(skill_dir, dst)
        if err:
            errors.append(f"{skill_dir.name}: {err}")
    return errors
