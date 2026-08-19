"""Custom build step: stage app-shipped assets into the wheel.

The cockpit's ``CLAUDE.md`` (Lead playbook), ``.claude/agents/*.md`` (role
files), and ``.claude/skills/*/SKILL.md`` (default skill bundle) live at the
repo root as the single source of truth — they're also what a dev checkout
reads directly via ``config.ASSETS_ROOT`` (see config.py's dev-checkout
branch). An installed build can't read them from the repo root
(site-packages ships none of that), so they need to travel *inside* the
wheel too.

This stages them into ``src/agent_takkub/_assets/`` right before
setuptools' ``build_py`` collects ``package_data`` (see pyproject.toml), then
deletes the staged copy again so the dev source tree never carries a second,
driftable copy on disk between builds.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py

_ROOT = Path(__file__).resolve().parent
_ASSETS = _ROOT / "src" / "agent_takkub" / "_assets"
# remote/ (P0 scaffold) ships straight from the repo tree, no staging step
# (unlike _ASSETS) — scan it too so a future app.js/static asset can't leak a
# home-dir path into the wheel the same way the 1.0.13-1.0.17 leak did.
_REMOTE = _ROOT / "src" / "agent_takkub" / "remote"
_ASSET_PACKAGE_MARKER = "__init__.py"

# Personal home-path leak guard: staged assets ship inside the wheel to every
# installer, so a hardcoded `C:\Users\<me>\...` / `/Users/<me>/...` path would
# expose the maintainer's username. Match the home-dir segment and allow only
# obvious placeholders (docs legitimately use `~`, `<vault>`, `alice`, etc.).
# Regression cover for the 1.0.13–1.0.17 leak (redacted 2026-07-06).
_HOME_PATH_RE = re.compile(r"(?:[A-Za-z]:\\Users\\|/Users/|/home/)([^\\/\s\"'`<>|]+)")
_PLACEHOLDER_USERS = frozenset(
    {"user", "username", "name", "you", "youruser", "alice", "bob", "me", "home"}
)


def _assert_no_home_path_leak() -> None:
    """Refuse to ship any staged/shipped asset carrying a real home-dir username."""
    offenders: list[str] = []
    for root in (_ASSETS, _REMOTE):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                for user in _HOME_PATH_RE.findall(line):
                    if user.lower() not in _PLACEHOLDER_USERS:
                        rel = path.relative_to(root)
                        offenders.append(f"  {rel}:{lineno}  →  {line.strip()}")
    if offenders:
        raise RuntimeError(
            "asset staging failed: real home-dir path(s) found in shipped assets "
            "— redact to `~/...` or a `<placeholder>` before building:\n" + "\n".join(offenders)
        )


def _clear_staged_asset_payload() -> None:
    """Remove generated assets while preserving the package marker.

    Keeping ``_assets/__init__.py`` in the source tree makes setuptools treat
    the runtime asset root as an explicit package instead of an ambiguous
    importable data directory.  The generated CLAUDE/role/skill payload still
    exists only for the duration of a build.
    """
    _ASSETS.mkdir(parents=True, exist_ok=True)
    for path in _ASSETS.iterdir():
        if path.name == _ASSET_PACKAGE_MARKER:
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def _stage_assets() -> None:
    _clear_staged_asset_payload()
    claude_md = _ROOT / "CLAUDE.md"
    agents_src = _ROOT / ".claude" / "agents"
    if not claude_md.is_file():
        raise RuntimeError(
            f"asset staging failed: {claude_md} is missing — refusing to build a "
            "wheel with no Lead playbook shipped inside it"
        )
    claude_md_text = claude_md.read_text(encoding="utf-8")
    agent_files = sorted(agents_src.glob("*.md")) if agents_src.is_dir() else []
    if not agent_files:
        raise RuntimeError(
            f"asset staging failed: no *.md role files found under {agents_src} — "
            "refusing to build a wheel with an empty/missing .claude/agents"
        )
    agents_dst = _ASSETS / ".claude" / "agents"
    agents_dst.mkdir(parents=True)
    shutil.copy2(claude_md, _ASSETS / "CLAUDE.md")
    for f in agent_files:
        shutil.copy2(f, agents_dst / f.name)

    # Lead playbook doc bundle (docs/lead/*.md) — CLAUDE.md cross-references
    # these by relative path (`docs/lead/patterns.md`, `docs/lead/cli-
    # reference.md`, ...), which only resolves from a dev checkout's cwd.
    # Ship them alongside CLAUDE.md so an installed build has something for
    # lead_context.py to rewrite the references onto (see its installed-mode
    # rewrite) instead of shipping a CLAUDE.md full of dangling pointers.
    docs_lead_src = _ROOT / "docs" / "lead"
    docs_lead_files = sorted(docs_lead_src.glob("*.md")) if docs_lead_src.is_dir() else []
    if docs_lead_files:
        docs_lead_dst = _ASSETS / "docs" / "lead"
        docs_lead_dst.mkdir(parents=True)
        for f in docs_lead_files:
            shutil.copy2(f, docs_lead_dst / f.name)

    # Fail the build fast if CLAUDE.md references a docs/lead/*.md file that
    # isn't actually staged (missing on disk, or docs/lead/ absent entirely)
    # — catches a dangling pointer at build time instead of shipping it to
    # every installer (the bug this staging step exists to fix).
    referenced_docs = set(re.findall(r"docs/lead/([\w.-]+\.md)", claude_md_text))
    staged_names = {f.name for f in docs_lead_files}
    missing_docs = sorted(referenced_docs - staged_names)
    if missing_docs:
        raise RuntimeError(
            "asset staging failed: CLAUDE.md references docs/lead file(s) not found "
            f"under {docs_lead_src}: {', '.join(missing_docs)}"
        )

    # Default skill bundle — supplementary reference material for the New
    # Role / Skill Catalog pickers, unlike CLAUDE.md/agents it's not
    # required for a pane to spawn, so a missing/empty store degrades to
    # "no bundled skills shipped" instead of failing the build. Phase 5a
    # (epic #309 Capability Hub): real storage moved from
    # `.claude/skills/` to `capabilities/skills/` (provider-neutral, see
    # `config._resolve_skills_dir`) — `.claude/skills` at repo root is
    # kept only as a runtime junction/symlink surface, so staging must
    # read the NEW path; a legacy repo checkout that hasn't migrated yet
    # falls back to the old path so the build never silently ships empty.
    skills_src = _ROOT / "capabilities" / "skills"
    if not skills_src.is_dir():
        skills_src = _ROOT / ".claude" / "skills"
    if skills_src.is_dir():
        skill_files = sorted(skills_src.glob("*/SKILL.md"))
        if skill_files:
            skills_dst = _ASSETS / "capabilities" / "skills"
            for f in skill_files:
                dst = skills_dst / f.parent.name / f.name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst)

    _assert_no_home_path_leak()


class build_py(_build_py):
    def run(self) -> None:
        _stage_assets()
        try:
            super().run()
        finally:
            _clear_staged_asset_payload()


setup(cmdclass={"build_py": build_py})
