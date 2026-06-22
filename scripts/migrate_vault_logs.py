"""One-shot migration: move vault session logs from the old 01-Projects layout
to the new 99-Logs/sessions/ layout introduced by the vault-knowledge-refactor.

Run once after deploying Phase A:

    python scripts/migrate_vault_logs.py [--vault PATH] [--dry-run]

Behaviour
---------
- For every ``<vault>/01-Projects/<project>/sessions/`` that exists:
    - Files older than 14 days → delete (beyond retention window anyway)
    - Remaining files → move to ``<vault>/99-Logs/sessions/<project>/``
    - If the sessions/ subdir is now empty, remove it (rmdir, not rmtree)
- Never touches the project page (``<vault>/01-Projects/<project>.md``)
- Idempotent: already-migrated vaults are no-ops
- Prints a one-line summary; use --dry-run to preview without writing
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import time

_DEFAULT_VAULT = pathlib.Path.home() / "WebstormProjects" / "second-brain"
_SESSION_MAX_AGE_S = 14 * 86400  # matches vault_mirror._SESSION_MAX_AGE_S


def _resolve_vault(override: str | None) -> pathlib.Path | None:
    candidates = []
    if override:
        candidates.append(pathlib.Path(override))
    env = os.environ.get("TAKKUB_VAULT_DIR", "").strip()
    if env:
        candidates.append(pathlib.Path(env))
    candidates.append(_DEFAULT_VAULT)
    for c in candidates:
        if (c / "01-Projects").is_dir():
            return c
    return None


def migrate(vault: pathlib.Path, dry_run: bool = False) -> tuple[int, int, int]:
    """Move session files from old to new layout.

    Returns ``(moved, deleted, errors)`` counts.
    """
    now = time.time()
    moved = deleted = errors = 0

    projects_root = vault / "01-Projects"
    for proj_dir in sorted(projects_root.iterdir()):
        if not proj_dir.is_dir():
            continue
        old_sessions = proj_dir / "sessions"
        if not old_sessions.is_dir():
            continue

        project = proj_dir.name
        new_sessions = vault / "99-Logs" / "sessions" / project

        for f in sorted(old_sessions.glob("*.md")):
            try:
                age = now - f.stat().st_mtime
            except OSError:
                errors += 1
                continue

            if age > _SESSION_MAX_AGE_S:
                print(f"  DELETE (expired)  {f.relative_to(vault)}")
                if not dry_run:
                    try:
                        f.unlink()
                        deleted += 1
                    except OSError as exc:
                        print(f"    ERROR: {exc}")
                        errors += 1
                else:
                    deleted += 1
            else:
                dest = new_sessions / f.name
                print(f"  MOVE  {f.relative_to(vault)}")
                print(f"     ->  {dest.relative_to(vault)}")
                if not dry_run:
                    try:
                        new_sessions.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(f), str(dest))
                        moved += 1
                    except OSError as exc:
                        print(f"    ERROR: {exc}")
                        errors += 1
                else:
                    moved += 1

        # Remove empty sessions dir.
        if not dry_run and old_sessions.is_dir():
            try:
                remaining = list(old_sessions.iterdir())
                if not remaining:
                    old_sessions.rmdir()
            except OSError:
                pass

    return moved, deleted, errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", help="Vault root path (default: auto-detect)")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without writing")
    args = parser.parse_args()

    vault = _resolve_vault(args.vault)
    if vault is None:
        print("ERROR: vault not found. Set $TAKKUB_VAULT_DIR or pass --vault PATH.")
        raise SystemExit(1)

    prefix = "[DRY-RUN] " if args.dry_run else ""
    print(f"{prefix}Migrating vault sessions: {vault}")

    moved, deleted, errors = migrate(vault, dry_run=args.dry_run)

    print(f"\n{prefix}Done — moved {moved}, deleted (expired) {deleted}, errors {errors}")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
