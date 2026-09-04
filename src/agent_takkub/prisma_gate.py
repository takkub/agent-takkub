"""Prisma migration safety checks for the Node qa-gate (#469).

`qa-gate`/`done` used to prove nothing about schema↔migration parity: a
`schema.prisma` edit with no matching migration ran green (test suite hit a
different DB) and only broke prod when `migrate deploy` hit the missing
column; a migration file edited after it was already applied ran green too
and only broke on the next env that already had it applied (checksum drift).
Both are cheap to catch statically/via git — no running stack required. (A
smoke test against a real running stack, #469's proposal 3, is tracked
separately as #475.)
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ._win_console import gate_popen_kwargs
from .verify import pm_exec, workspace_dirs

_GIT_TIMEOUT_S = 15
_PRISMA_TIMEOUT_S = 120
_BASE_REF_CANDIDATES: tuple[str, ...] = ("main", "master", "origin/main", "origin/master")


@dataclass
class PrismaFinding:
    ok: bool
    skipped: bool
    detail: str


def _tail(text: str, lines: int = 15) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def find_prisma_roots(cwd: Path, pkg: dict) -> list[Path]:
    """Every directory (*cwd* itself, plus each workspace package) that
    carries `prisma/schema.prisma` — the unit each check below runs against."""
    roots: list[Path] = []
    if (cwd / "prisma" / "schema.prisma").exists():
        roots.append(cwd)
    for rel in workspace_dirs(cwd, pkg):
        d = cwd / rel
        if (d / "prisma" / "schema.prisma").exists():
            roots.append(d)
    return roots


def check_schema_drift(prisma_root: Path, pm: str, env: dict) -> PrismaFinding:
    """`prisma migrate diff --from-migrations ... --to-schema-datamodel ...
    --exit-code`: exit 0 = migration history matches the schema, exit 2 = it
    doesn't (a schema edit with no migration to match it), exit 1 = prisma
    couldn't evaluate the diff at all (most commonly: no shadow database /
    DATABASE_URL reachable in this environment) — that last case is a visible
    skip, never a silent FAIL, per #469's explicit ask."""
    migrations_dir = prisma_root / "prisma" / "migrations"
    if not migrations_dir.is_dir():
        return PrismaFinding(True, True, "no prisma/migrations directory yet — skip")

    cmd = pm_exec(
        pm,
        "prisma",
        "migrate",
        "diff",
        "--from-migrations",
        "prisma/migrations",
        "--to-schema-datamodel",
        "prisma/schema.prisma",
        "--exit-code",
    )
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(prisma_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=_PRISMA_TIMEOUT_S,
            **gate_popen_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return PrismaFinding(True, True, "prisma migrate diff timed out — skip (no reachable DB?)")
    except OSError as e:
        return PrismaFinding(True, True, f"prisma CLI not runnable ({e}) — skip")

    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        return PrismaFinding(True, False, "schema.prisma matches migration history")
    if proc.returncode == 2:
        return PrismaFinding(
            False,
            False,
            "schema.prisma เปลี่ยนแต่ไม่มี migration — รัน prisma migrate dev --name <x>: "
            + _tail(output),
        )
    return PrismaFinding(
        True,
        True,
        f"prisma migrate diff ไม่สามารถรันได้ (exit {proc.returncode}) — skip เพราะไม่มี "
        f"env/DB ชัดเจน ไม่ใช่ FAIL: {_tail(output)}",
    )


def _resolve_merge_base(cwd: Path) -> str | None:
    for ref in _BASE_REF_CANDIDATES:
        try:
            verified = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", ref],
                cwd=str(cwd),
                capture_output=True,
                timeout=_GIT_TIMEOUT_S,
                **gate_popen_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if verified.returncode != 0:
            continue
        try:
            merge_base = subprocess.run(
                ["git", "merge-base", "HEAD", ref],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_GIT_TIMEOUT_S,
                **gate_popen_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if merge_base.returncode == 0 and merge_base.stdout.strip():
            return merge_base.stdout.strip()
    return None


def check_migration_integrity(prisma_root: Path) -> PrismaFinding:
    """A `migration.sql` that already existed at the merge-base with main and
    is modified in the current diff (working tree + index, vs that base) is a
    migration checksum drift bomb: `migrate deploy` refuses it on any env
    that already applied it (e.g. the VPS). A newly added migration file
    never appears in this diff at all — only edits to a pre-existing one do."""
    migrations_dir = prisma_root / "prisma" / "migrations"
    if not migrations_dir.is_dir():
        return PrismaFinding(True, True, "no prisma/migrations directory — skip")

    base = _resolve_merge_base(prisma_root)
    if base is None:
        return PrismaFinding(
            True, True, "no main/master ref to diff against — skip migration-integrity check"
        )

    try:
        proc = subprocess.run(
            ["git", "diff", "--relative", "--name-status", base, "--", "prisma/migrations"],
            cwd=str(prisma_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_S,
            **gate_popen_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return PrismaFinding(True, True, "git diff failed — skip migration-integrity check")
    if proc.returncode != 0:
        return PrismaFinding(True, True, "git diff failed — skip migration-integrity check")

    modified = [
        line.split("\t", 1)[1]
        for line in proc.stdout.splitlines()
        if line.startswith("M\t") and line.endswith("migration.sql")
    ]
    if modified:
        return PrismaFinding(
            False,
            False,
            "แก้ migration ที่ apply แล้ว (checksum จะ drift) — สร้าง migration ใหม่แทน: "
            + " ".join(modified),
        )
    return PrismaFinding(True, False, "no already-applied migration.sql edited")
