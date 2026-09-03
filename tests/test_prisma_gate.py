"""Tests for prisma_gate.py — schema/migration drift + checksum-drift checks
(#469). `check_schema_drift` always fakes `subprocess.run` (never a real
`prisma` CLI on CI); `check_migration_integrity`/`find_prisma_roots` use a
real tmp git repo since git plumbing is cheap and is exactly what's under
test there."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_takkub import prisma_gate


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def prisma_repo(tmp_path: Path) -> Path:
    """A Node+Prisma repo on a branch literally named `main` (deterministic
    regardless of the machine's `init.defaultBranch`), with one already-
    "applied" migration committed."""
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t.test", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}))
    (root / "prisma").mkdir()
    (root / "prisma" / "schema.prisma").write_text("// v1\n", encoding="utf-8")
    mig_dir = root / "prisma" / "migrations" / "20260101000000_init"
    mig_dir.mkdir(parents=True)
    (mig_dir / "migration.sql").write_text("CREATE TABLE x (id INT);\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-q", "-m", "init", cwd=root)
    return root


# ---------------------------------------------------------------------------
# find_prisma_roots
# ---------------------------------------------------------------------------


def test_find_prisma_roots_root_only(prisma_repo: Path) -> None:
    assert prisma_gate.find_prisma_roots(prisma_repo, {}) == [prisma_repo]


def test_find_prisma_roots_none_when_absent(tmp_path: Path) -> None:
    root = tmp_path / "bare"
    root.mkdir()
    (root / "package.json").write_text("{}", encoding="utf-8")
    assert prisma_gate.find_prisma_roots(root, {}) == []


def test_find_prisma_roots_monorepo_workspace_package(tmp_path: Path) -> None:
    root = tmp_path / "mono"
    root.mkdir()
    (root / "pnpm-workspace.yaml").write_text("packages:\n  - 'apps/*'\n", encoding="utf-8")
    api = root / "apps" / "api"
    api.mkdir(parents=True)
    (api / "prisma").mkdir()
    (api / "prisma" / "schema.prisma").write_text("", encoding="utf-8")
    web = root / "apps" / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text("{}", encoding="utf-8")

    assert prisma_gate.find_prisma_roots(root, {}) == [api]


# ---------------------------------------------------------------------------
# check_schema_drift — subprocess always faked, never a real prisma CLI
# ---------------------------------------------------------------------------


def test_schema_drift_fails_when_prisma_reports_a_diff(prisma_repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        prisma_gate.subprocess,
        "run",
        lambda cmd, **kw: _FakeCompleted(2, stderr="[+] Added column y\n"),
    )
    finding = prisma_gate.check_schema_drift(prisma_repo, "npm", os.environ.copy())
    assert finding.ok is False
    assert finding.skipped is False


def test_schema_drift_passes_when_no_diff(prisma_repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(prisma_gate.subprocess, "run", lambda cmd, **kw: _FakeCompleted(0))
    finding = prisma_gate.check_schema_drift(prisma_repo, "npm", os.environ.copy())
    assert finding.ok is True
    assert finding.skipped is False


def test_schema_drift_skips_visibly_when_env_missing(prisma_repo: Path, monkeypatch) -> None:
    """exit 1 = prisma couldn't evaluate the diff (no shadow DB/DATABASE_URL)
    — must surface as a visible skip, never a silent FAIL (#469 explicit
    ask)."""
    monkeypatch.setattr(
        prisma_gate.subprocess,
        "run",
        lambda cmd, **kw: _FakeCompleted(
            1, stderr="Error: Environment variable not found: DATABASE_URL"
        ),
    )
    finding = prisma_gate.check_schema_drift(prisma_repo, "npm", os.environ.copy())
    assert finding.ok is True
    assert finding.skipped is True
    assert "skip" in finding.detail.lower()


def test_schema_drift_skips_when_no_migrations_dir_yet(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "fresh"
    root.mkdir()
    (root / "prisma").mkdir()
    (root / "prisma" / "schema.prisma").write_text("", encoding="utf-8")
    called = []
    monkeypatch.setattr(
        prisma_gate.subprocess, "run", lambda cmd, **kw: called.append(cmd) or _FakeCompleted(0)
    )
    finding = prisma_gate.check_schema_drift(root, "npm", os.environ.copy())
    assert finding.skipped is True
    assert not called, "no migrations dir — must not even shell out"


# ---------------------------------------------------------------------------
# check_migration_integrity — real tmp git repo, no prisma CLI involved
# ---------------------------------------------------------------------------


def test_migration_integrity_flags_modified_already_applied_migration(
    prisma_repo: Path,
) -> None:
    mig_file = prisma_repo / "prisma" / "migrations" / "20260101000000_init" / "migration.sql"
    mig_file.write_text("CREATE TABLE x (id INT, extra TEXT);\n", encoding="utf-8")

    finding = prisma_gate.check_migration_integrity(prisma_repo)

    assert finding.ok is False
    assert finding.skipped is False
    assert "migration.sql" in finding.detail


def test_migration_integrity_passes_for_a_newly_added_migration(prisma_repo: Path) -> None:
    new_dir = prisma_repo / "prisma" / "migrations" / "20260201000000_add_col"
    new_dir.mkdir()
    (new_dir / "migration.sql").write_text("ALTER TABLE x ADD COLUMN y INT;\n", encoding="utf-8")

    finding = prisma_gate.check_migration_integrity(prisma_repo)

    assert finding.ok is True
    assert finding.skipped is False


def test_migration_integrity_passes_when_nothing_changed(prisma_repo: Path) -> None:
    finding = prisma_gate.check_migration_integrity(prisma_repo)
    assert finding.ok is True
    assert finding.skipped is False


def test_migration_integrity_skips_when_no_migrations_dir(tmp_path: Path) -> None:
    root = tmp_path / "bare"
    root.mkdir()
    finding = prisma_gate.check_migration_integrity(root)
    assert finding.skipped is True


def test_migration_integrity_skips_when_no_base_ref_resolves(tmp_path: Path) -> None:
    root = tmp_path / "no-main"
    root.mkdir()
    _git("init", "-q", "-b", "feature-only", cwd=root)
    _git("config", "user.email", "t@t.test", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    mig_dir = root / "prisma" / "migrations" / "20260101000000_init"
    mig_dir.mkdir(parents=True)
    (mig_dir / "migration.sql").write_text("x", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-q", "-m", "init", cwd=root)

    finding = prisma_gate.check_migration_integrity(root)

    assert finding.skipped is True
