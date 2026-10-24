"""`takkub backups list|prune` — user-triggered removal of old safety copies."""

from __future__ import annotations

import os
import time

import pytest

from agent_takkub import backup_prune, config


@pytest.fixture
def backups(tmp_path, monkeypatch):
    home = tmp_path / "data"
    b = home / "backups"
    old = time.time() - 30 * 86_400
    (b / "v1-archive-1").mkdir(parents=True)
    (b / "v1-archive-1" / "f.bin").write_bytes(b"x" * 1000)
    (b / "figma-removed-today").mkdir()
    (b / "promote-v2-root-manifest.json").write_text("{}", encoding="utf-8")
    for name in ("v1-archive-1", "promote-v2-root-manifest.json"):
        os.utime(b / name, (old, old))
    monkeypatch.setattr(config, "DATA_HOME", home)
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "repo")
    monkeypatch.setattr(backup_prune, "_migration_finished", lambda: True)
    return b


def test_only_old_non_kept_entries_are_prunable(backups):
    by_name = {e.name: e for e in backup_prune.list_backups()}
    assert by_name["v1-archive-1"].prunable
    assert by_name["v1-archive-1"].size == 1000
    assert not by_name["figma-removed-today"].prunable  # too new
    assert not by_name["promote-v2-root-manifest.json"].prunable  # read by doctor


def test_prune_is_a_dry_run_without_yes(backups):
    res = backup_prune.run_cli("prune", yes=False)
    assert res["dry_run"] is True
    assert (backups / "v1-archive-1").exists()


def test_prune_yes_deletes_only_prunable(backups):
    res = backup_prune.run_cli("prune", yes=True)
    assert res["freed_bytes"] == 1000
    assert not (backups / "v1-archive-1").exists()
    assert (backups / "figma-removed-today").exists()
    assert (backups / "promote-v2-root-manifest.json").exists()


def test_nothing_prunable_before_migration_finished(backups, monkeypatch):
    monkeypatch.setattr(backup_prune, "_migration_finished", lambda: False)
    assert not any(e.prunable for e in backup_prune.list_backups())
