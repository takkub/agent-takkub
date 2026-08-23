"""Opt-in backfill: add canonical frontmatter to an EXISTING
``01-Projects/<project>.md`` page that predates issue #365 phase 8 —
never run automatically. `10_OBSIDIAN_HARDENING.md`'s constraint is
explicit: cockpit may only read + write NEW notes into a user's vault
under the existing contract, never rewrite/rename an existing one on its
own — `doctor --obsidian` only WARNS about pages missing canonical
metadata; this script is the human's opt-in fix, one project at a time.

Only adds a frontmatter block when the page has none — never touches the
body or any existing entry, and refuses (no-op) on a page that already
has frontmatter so it can't double-stamp one that a later cockpit version
already wrote.

Usage: ``python -m agent_takkub.obsidian_backfill <project>``
(same CLI shape as `vault_graph.py`'s own ``__main__`` entry point —
this is a script, not a `takkub` subcommand, matching that precedent).
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime

from .obsidian_metadata import TRUST_CURATED, NoteMetadata
from .project_identity import resolve_project_id
from .vault_mirror import _resolve_vault_dir


def backfill_project_page(
    vault: pathlib.Path, project: str, *, now: datetime | None = None
) -> bool:
    """Add a canonical frontmatter block to ``vault/01-Projects/<project>.md``.

    Returns ``True`` when the page was rewritten, ``False`` when there was
    nothing to do (page missing, or already has frontmatter).
    """
    page = vault / "01-Projects" / f"{project}.md"
    if not page.is_file():
        print(f"obsidian_backfill: no page at {page}")
        return False
    text = page.read_text(encoding="utf-8")
    if text.startswith("---\n"):
        print(f"obsidian_backfill: {page} already has frontmatter, skipping")
        return False
    try:
        project_id = resolve_project_id(project)
    except ValueError:
        project_id = project
    meta = NoteMetadata.new(
        project_id=project_id,
        source="backfill",
        kind="project",
        trust=TRUST_CURATED,
        text=text,
        now=now,
    )
    fm = "---\n" + "".join(f"{line}\n" for line in meta.frontmatter_lines()) + "---\n\n"
    page.write_text(fm + text, encoding="utf-8")
    print(f"OK {page}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent_takkub.obsidian_backfill",
        description="Opt-in: add canonical frontmatter to an existing "
        "01-Projects/<project>.md page (#365 phase 8). Never run automatically.",
    )
    parser.add_argument("project", help="Project name as used under 01-Projects/")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    vault = _resolve_vault_dir()
    if vault is None:
        print("obsidian_backfill: no vault configured ($TAKKUB_VAULT_DIR)")
        return 1
    return 0 if backfill_project_page(vault, args.project) else 1


if __name__ == "__main__":
    raise SystemExit(main())
