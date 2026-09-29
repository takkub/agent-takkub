"""Link the central skill store into a provider's *native* skill dir.

One source of truth (`config.global_skills_dir` / `project_skills_dir`);
providers that discover `SKILL.md` folders themselves (claude via
`<project>/.claude/skills`, codex via `$CODEX_HOME/skills`) get a
junction/symlink per skill — never a copy. Providers without a native dir
keep the AGENTS.md appendix and `takkub skills effective` shows the gap.

Prod-state safety: a home-relative dir is only touched when the cockpit
isolates that provider under DATA_HOME (or a chosen account dir). A dev
checkout has no isolated home → we return ``no_isolated_home`` and the caller
falls back to the appendix; the user's real ``~/.codex`` is never written.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import config
from .worktree_manager import _make_link, _remove_link

_log = logging.getLogger(__name__)

# Names we never link over: codex ships its own `.system` skill bundle.
_RESERVED = {".system"}


def native_skill_home(spec, project_ns: str = "") -> Path | None:
    """Directory holding the provider's native skills dir, for a
    home-relative spec (`native_skill_home_var`); None when unresolvable."""
    var = spec.native_skill_home_var
    if not var:
        return None
    env = dict(config.provider_home_env(spec.name))
    if project_ns:
        try:
            from .pane_env import _PROFILE_HOME_VAR
            from .user_profile import provider_config_dir_for

            if _PROFILE_HOME_VAR.get(spec.name) == var:
                acct = provider_config_dir_for(project_ns, spec.name)
                if acct is not None:
                    env[var] = str(acct)
        except Exception:
            pass
    val = env.get(var)
    return Path(val) if val else None


def _global_skill_dirs() -> list[Path]:
    store = config.global_skills_dir()
    if not store.is_dir():
        return []
    return [
        d
        for d in sorted(store.iterdir())
        if d.name not in _RESERVED
        and not d.name.startswith(".")
        and d.is_dir()
        and (d / "SKILL.md").is_file()
    ]


def project_has_skills(project_ns: str) -> bool:
    """True when the project store holds skills the native link does NOT
    carry (the caller must keep the AGENTS.md appendix for them)."""
    if not project_ns:
        return False
    try:
        store = config.project_skills_dir(project_ns)
    except ValueError:
        return False
    return store.is_dir() and any((d / "SKILL.md").is_file() for d in store.iterdir() if d.is_dir())


def _is_link(p: Path) -> bool:
    if p.is_symlink():
        return True
    try:  # py<3.12 has no is_junction: NTFS junction = reparse point
        attrs = getattr(p.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _is_ours(p: Path) -> bool:
    """A link (symlink/junction) resolving into a central skill store."""
    if not _is_link(p):
        return False
    try:
        tgt = p.resolve()
    except OSError:
        return True  # unresolvable link → can only be a dangling link
    roots = (config.global_skills_dir(), config.PROJECT_SKILLS_HOME.resolve())
    return any(tgt == r or r in tgt.parents for r in roots)


def link_native_skills(spec, project_ns: str = "") -> tuple[str, list[str]]:
    """Reconcile the provider's home-relative native skill dir with the
    *global* central store. Returns ``(status, errors)``: ``linked``,
    ``not_native`` (no such dir / project-relative), or ``no_isolated_home`` /
    ``failed`` (caller keeps the AGENTS.md appendix). Never raises.

    Only global skills go here: that home (e.g. npm-install ``codex-home``)
    is shared by every project, so a project skill linked in would leak to
    other projects and shadow same-named ones. Project skills stay in the
    per-project appendix (`project_has_skills`). Links we made earlier that
    are dangling, or no longer match the global store (incl. project links
    from older builds), are removed; real folders, foreign links and
    ``.system`` are never touched."""
    if not spec.native_skill_dir or not spec.native_skill_home_var:
        return "not_native", []
    home = native_skill_home(spec, project_ns)
    if home is None:
        return "no_isolated_home", []
    errors: list[str] = []
    try:
        target_root = home / spec.native_skill_dir
        want = {d.name: d for d in _global_skill_dirs()}
        if target_root.is_dir():
            for ex in target_root.iterdir():
                if ex.name in _RESERVED or not _is_ours(ex):
                    continue
                try:
                    ok = ex.name in want and ex.resolve() == want[ex.name].resolve()
                except OSError:
                    ok = False
                if not ok:
                    _remove_link(ex)
        for name, src in want.items():
            dst = target_root / name
            if dst.exists():
                continue  # already linked, or a real/foreign skill — leave it
            err = _make_link(src, dst)
            if err:
                errors.append(f"{name}: {err}")
    except Exception as exc:  # pragma: no cover - defensive
        errors.append(str(exc))
    if errors:
        _log.warning("native skill link failed for %s: %s", spec.name, errors)
        return "failed", errors
    return "linked", []


def native_skill_gap(spec) -> str | None:
    """Human-readable gap for `takkub skills effective`, or None if native."""
    if spec.native_skill_dir:
        return None
    return "no native skill dir — instruction appendix in AGENTS.md (no auto-trigger)"


__all__ = ["link_native_skills", "native_skill_gap", "native_skill_home", "project_has_skills"]
