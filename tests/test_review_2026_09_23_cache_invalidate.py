"""System review 2026-09-23, group "cache-invalidate": writers that never
called `cached_read.invalidate()` so an in-process read inside the ≤3 s
stat-free TTL (#658, `cached_read._STAT_TTL_S`) returned the pre-write parse.

This file covers the shared migration writer (`registry_copy_step.
write_json_atomic`) and the V2 envelope reader (`v2_target.read_data`); the
per-module regressions live next to each module's own tests
(test_custom_roles / test_pane_tools_policy / test_role_messages /
test_task_ledger).

Every test primes the cache on a file whose mtime is pushed >2 s into the
past — the normal state of any file not written in the last moment — which
is exactly the case where the stat-free fast path serves a stale parse.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from agent_takkub import cached_read
from agent_takkub.core.migration.backup import BackupManager
from agent_takkub.core.migration.journal import MigrationJournal
from agent_takkub.core.migration.registry_copy_step import (
    RegistryCopyStep,
    RegistryMapping,
    write_json_atomic,
)
from agent_takkub.core.storage import v2_target
from agent_takkub.core.storage.jsonl_store import JsonlStore
from agent_takkub.core.storage.legacy_reader import read_json


@pytest.fixture(autouse=True)
def _real_ttl(monkeypatch: pytest.MonkeyPatch):
    """The bug only exists with the stat-skip TTL on; pin it so a later
    change to the default can't silently turn these into no-op tests."""
    monkeypatch.setattr(cached_read, "_STAT_TTL_S", 3.0)
    cached_read.invalidate()
    yield
    cached_read.invalidate()


def _old(path: Path, seconds: int = 60) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


class TestWriteJsonAtomicInvalidates:
    def test_read_after_write_sees_new_payload_inside_ttl(self, tmp_path: Path) -> None:
        p = tmp_path / "registry.json"
        p.write_text(json.dumps({"v": 1}), encoding="utf-8")
        _old(p)
        assert read_json(p) == {"v": 1}  # primes the cache with recent=False

        write_json_atomic(p, {"v": 2})

        assert read_json(p) == {"v": 2}

    def test_step_validate_is_green_right_after_apply(self, tmp_path: Path) -> None:
        """engine.apply_pending's 598 → 605 → 624 sequence: validate (primes),
        apply (rewrites the target), validate again milliseconds later — the
        second validate used to report a false 'mismatched'."""
        source = tmp_path / "v1" / "role-models.json"
        target = tmp_path / "v2" / "models" / "aliases.json"
        source.parent.mkdir(parents=True)
        target.parent.mkdir(parents=True)
        source.write_text(json.dumps({"backend": "opus"}), encoding="utf-8")
        target.write_text(
            json.dumps({"schema": 1, "data": {"backend": "sonnet"}}), encoding="utf-8"
        )
        _old(source)
        _old(target)

        step = RegistryCopyStep(
            "readonly-registries",
            (RegistryMapping("role-models", source, target),),
            journal=MigrationJournal(JsonlStore(tmp_path / "journal.jsonl")),
            backups=BackupManager(tmp_path / "backups"),
        )
        assert step.validate().ok is False
        assert step.apply().ok is True

        report = step.validate()

        assert report.ok is True, report.summary
        assert json.loads(target.read_text(encoding="utf-8"))["data"] == {"backend": "opus"}


class TestReadDataFresh:
    def test_fresh_read_bypasses_ttl_after_external_write(self, tmp_path: Path) -> None:
        """A `takkub` CLI process rewrote the target; the cockpit's own cache
        entry is still inside the TTL. A read-modify-write caller must be
        able to ask for the on-disk truth."""
        target = tmp_path / "state" / "issues" / "local.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps({"schema": 1, "data": [{"number": 1}]}), encoding="utf-8")
        _old(target)
        assert v2_target.read_data(target) == [{"number": 1}]

        # simulate the other process: plain rewrite, mtime kept "old" so the
        # recent-write guard cannot mask the TTL fast path
        target.write_text(
            json.dumps({"schema": 1, "data": [{"number": 1}, {"number": 2}]}), encoding="utf-8"
        )
        _old(target, 30)

        assert v2_target.read_data(target, fresh=True) == [{"number": 1}, {"number": 2}]

    def test_fresh_read_returns_a_private_copy(self, tmp_path: Path) -> None:
        target = tmp_path / "local.json"
        v2_target.write_data(target, [{"number": 1}])
        _old(target)

        first = v2_target.read_data(target, fresh=True)
        first.append({"number": 99})

        assert v2_target.read_data(target, fresh=True) == [{"number": 1}]
        assert v2_target.read_data(target) == [{"number": 1}]

    def test_default_read_keeps_the_cached_fast_path(self, tmp_path: Path) -> None:
        target = tmp_path / "aliases.json"
        v2_target.write_data(target, {"backend": "opus"})
        _old(target)
        assert v2_target.read_data(target) == {"backend": "opus"}
        stats: list[str] = []
        real_stat = os.stat

        def counting_stat(path, *a, **kw):
            stats.append(os.fspath(path))
            return real_stat(path, *a, **kw)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(cached_read.os, "stat", counting_stat)
            assert v2_target.read_data(target) == {"backend": "opus"}
        assert stats == []
