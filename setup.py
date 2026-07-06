"""Custom build step: stage app-shipped assets into the wheel.

The cockpit's ``CLAUDE.md`` (Lead playbook) and ``.claude/agents/*.md`` (role
files) live at the repo root as the single source of truth — they're also
what a dev checkout reads directly via ``config.ASSETS_ROOT`` (see
config.py's dev-checkout branch). An installed build can't read them from
the repo root (site-packages ships none of that), so they need to travel
*inside* the wheel too.

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
    """Refuse to ship any staged asset carrying a real home-dir username."""
    offenders: list[str] = []
    for path in sorted(_ASSETS.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for user in _HOME_PATH_RE.findall(line):
                if user.lower() not in _PLACEHOLDER_USERS:
                    rel = path.relative_to(_ASSETS)
                    offenders.append(f"  {rel}:{lineno}  →  {line.strip()}")
    if offenders:
        raise RuntimeError(
            "asset staging failed: real home-dir path(s) found in shipped assets "
            "— redact to `~/...` or a `<placeholder>` before building:\n" + "\n".join(offenders)
        )


def _stage_assets() -> None:
    if _ASSETS.exists():
        shutil.rmtree(_ASSETS)
    claude_md = _ROOT / "CLAUDE.md"
    agents_src = _ROOT / ".claude" / "agents"
    if not claude_md.is_file():
        raise RuntimeError(
            f"asset staging failed: {claude_md} is missing — refusing to build a "
            "wheel with no Lead playbook shipped inside it"
        )
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
    _assert_no_home_path_leak()


class build_py(_build_py):
    def run(self) -> None:
        _stage_assets()
        try:
            super().run()
        finally:
            shutil.rmtree(_ASSETS, ignore_errors=True)


setup(cmdclass={"build_py": build_py})
