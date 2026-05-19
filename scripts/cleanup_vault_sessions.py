"""One-shot cleanup for the Obsidian vault's session-mirror dumps.

Backstory:
- Pre-cleanup, the cockpit mirrored *every* `takkub done` call to
  `<vault>/01-Projects/<project>/sessions/*.md`, regardless of whether
  the note was substantive. Most files ended up as 3-line stubs with
  notes like "appended" / "wip" / "ok" — Obsidian saw 98 disconnected
  dots in graph view with no usable knowledge attached.
- A separate change in this commit teaches the orchestrator to skip
  junk notes / scratch projects at *write* time. This script does the
  retroactive cleanup: walks the existing mirror, archives files that
  match the same junk rule, and leaves substantive sessions intact.

Safety:
- Files are *moved*, not deleted. Destination:
    <vault>/04-Archive/old-sessions-<YYYY-MM-DD>/<project>/<file>.md
  Reversible if a session turns out to have been useful.
- Whole project subtrees whose name matches `_is_junk_project`
  (testproj, scratch-*, tmp-*, playground) are archived wholesale —
  individual files inside aren't inspected.
- Dry-run is the default. Pass `--apply` to actually move files.

Run from repo root:
    python scripts/cleanup_vault_sessions.py            # preview only
    python scripts/cleanup_vault_sessions.py --apply    # do it
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Re-use the same junk-detection rules the orchestrator now applies at
# write time. Importing keeps the cleanup definition in lock-step with
# the live filter — no risk of the script and runtime drifting apart.
from agent_takkub.orchestrator import (
    _is_junk_note,
    _is_junk_project,
    _render_decision_note,
    _resolve_vault_dir,
)

# Pattern matches the `## Note\n\n<body>\n` block written by
# `_render_decision_note`. Captures only the first paragraph — that's
# the actual note string we'd have passed to `_is_junk_note`.
_NOTE_RE = re.compile(r"## Note\n\n(?P<note>.+?)(?:\n\n|\Z)", re.DOTALL)

# Filenames written by `_save_decision_note` follow this exact pattern:
#   2026-05-17T143045-backend.md
# Stem split on the last "-" separates the ISO-basic timestamp from
# the role. Anything that doesn't match (user-renamed files,
# `.md.bak` copies, etc.) gets skipped from the migration pass.
_FILENAME_RE = re.compile(r"^(?P<stamp>\d{4}-\d{2}-\d{2}T\d{6})-(?P<role>[a-z0-9_-]+)$")


def _extract_note(text: str) -> str:
    """Return the substantive note body from a session file, or '' if
    the file doesn't match our expected layout."""
    m = _NOTE_RE.search(text)
    return m.group("note").strip() if m else ""


def _rewrite_to_new_format(f: Path, project: str, *, apply: bool) -> bool:
    """If `f` is an old-format session file (no YAML frontmatter),
    re-render it in the new wikilink + frontmatter layout while
    preserving the note text and timestamp. Returns True when the
    file is eligible for rewrite (whether or not `apply` actually
    wrote)."""
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if text.startswith("---\n"):
        return False  # already in the new format
    m = _FILENAME_RE.match(f.stem)
    if m is None:
        return False
    role = m.group("role")
    stamp = m.group("stamp")
    try:
        ts = datetime.strptime(stamp, "%Y-%m-%dT%H%M%S")
    except ValueError:
        return False
    note = _extract_note(text)
    if not note:
        return False
    new_body = _render_decision_note(project, role, note, ts)
    if apply:
        try:
            f.write_text(new_body, encoding="utf-8")
        except OSError:
            return False
    return True


def _archive_root(vault: Path) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d")
    return vault / "04-Archive" / f"old-sessions-{stamp}"


def _move(src: Path, dst: Path, *, apply: bool) -> None:
    if not apply:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually move files. Default is dry-run preview.",
    )
    args = p.parse_args()

    vault = _resolve_vault_dir()
    if vault is None:
        print("no vault configured (set TAKKUB_VAULT_DIR or use default layout).")
        return 1

    projects_root = vault / "01-Projects"
    if not projects_root.is_dir():
        print(f"no 01-Projects under {vault}")
        return 1

    archive_root = _archive_root(vault)
    print(f"vault:         {vault}")
    print(f"archive root:  {archive_root}")
    print(f"mode:          {'APPLY' if args.apply else 'dry-run'}")
    print()

    archived_projects: list[str] = []
    archived_files: list[Path] = []
    kept_files: list[Path] = []
    migrated_files: list[Path] = []

    for project_dir in sorted(projects_root.iterdir()):
        if not project_dir.is_dir():
            continue
        project = project_dir.name

        # Whole-project archive: scratch/test workspaces leave entirely.
        if _is_junk_project(project):
            dst = archive_root / project
            print(f"PROJECT JUNK: {project}/ -> {dst.relative_to(vault)}")
            _move(project_dir, dst, apply=args.apply)
            archived_projects.append(project)
            continue

        sessions = project_dir / "sessions"
        if not sessions.is_dir():
            continue

        for f in sorted(sessions.glob("*.md")):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            note = _extract_note(text)
            if _is_junk_note(note):
                dst = archive_root / project / f.name
                print(f"  junk note: {f.relative_to(vault)}  note={note!r}")
                _move(f, dst, apply=args.apply)
                archived_files.append(f)
                continue
            kept_files.append(f)
            # Substantive but old-format: re-render in place so the
            # graph view finally clusters this file under its project
            # page via the new `[[01-Projects/<project>|...]]` link.
            if _rewrite_to_new_format(f, project, apply=args.apply):
                migrated_files.append(f)
                print(f"  migrated:  {f.relative_to(vault)}")

    print()
    print("=== summary ===")
    print(f"  projects archived: {len(archived_projects)}")
    for proj in archived_projects:
        print(f"    - {proj}")
    print(f"  files archived:    {len(archived_files)}")
    print(f"  files kept:        {len(kept_files)}")
    print(f"  files migrated:    {len(migrated_files)}  (rewritten in new format)")
    if not args.apply:
        print()
        print("Run again with --apply to actually move the files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
