"""Per-project rules file I/O — provider-neutral.

A project's rules live in ``AGENTS.md`` (codex, agy, opencode and cursor read
it natively); ``CLAUDE.md`` holds ``@AGENTS.md`` so claude reads the same file.
Older projects keep their rules in ``CLAUDE.md`` alone — those keep working.

The AI generator that used to live here (``claude -p`` writing CLAUDE.md for a
new project) was removed with the template picker (``project_template``): it
tied project setup to one provider.

``AGENTS.md`` files the cockpit itself plants for a pane carry
``codex_agents_md.TAKKUB_MARKER`` on line 1 — those are not project rules and
are never read or written here.
"""

from __future__ import annotations

from pathlib import Path

from .codex_agents_md import TAKKUB_MARKER

AGENTS_FILE = "AGENTS.md"
CLAUDE_FILE = "CLAUDE.md"
CLAUDE_IMPORT = "@AGENTS.md"


def _first_line(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.readline().strip()
    except OSError:
        return ""


def _user_agents_md(project_root: Path) -> Path | None:
    target = project_root / AGENTS_FILE
    if target.is_file() and TAKKUB_MARKER not in _first_line(target):
        return target
    return None


def _claude_is_import_only(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines == [CLAUDE_IMPORT]


def rules_path(project_root: Path) -> Path:
    """The file holding *project_root*'s rules: a user ``AGENTS.md``, else a
    legacy ``CLAUDE.md`` with real content, else ``AGENTS.md`` (new)."""
    project_root = Path(project_root)
    agents = _user_agents_md(project_root)
    if agents is not None:
        return agents
    claude = project_root / CLAUDE_FILE
    if claude.is_file() and not _claude_is_import_only(claude):
        return claude
    return project_root / AGENTS_FILE


def read_project_rules(project_root: Path) -> str | None:
    """Rules text of *project_root*, or None when it has none."""
    target = rules_path(project_root)
    if not target.is_file() or TAKKUB_MARKER in _first_line(target):
        return None  # absent, or the cockpit's own planted pane sheet
    return target.read_text(encoding="utf-8")


def write_project_rules(project_root: Path, content: str) -> Path:
    """Atomically write the rules to ``rules_path``; a brand-new ``AGENTS.md``
    also gets the ``@AGENTS.md`` CLAUDE.md so claude reads it too."""
    project_root = Path(project_root)
    target = rules_path(project_root)
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(target)
    claude = project_root / CLAUDE_FILE
    if target.name == AGENTS_FILE and not claude.exists():
        claude.write_text(CLAUDE_IMPORT + "\n", encoding="utf-8")
    return target
