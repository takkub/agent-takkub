"""Core V2 storage: append/read/atomic-write/corruption-tolerance, plus the
subprocess proof that `agent_takkub.core` (and every submodule) never pulls
PyQt6 into `sys.modules` (docs/v2/V2_IMPLEMENTATION_PLAN.md Phase 1 DoD)."""

from __future__ import annotations

import json
import subprocess
import sys

from agent_takkub.core.storage.jsonl_store import JsonlStore
from agent_takkub.core.storage.legacy_reader import (
    read_json,
    read_role_provider_routing_policy,
)
from agent_takkub.core.storage.paths import core_home, core_store_path


def test_append_and_read_all_round_trip(tmp_path):
    store = JsonlStore(tmp_path / "records.jsonl")
    store.append({"id": "a", "n": 1})
    store.append({"id": "b", "n": 2})
    result = store.read_all()
    assert result.records == [{"id": "a", "n": 1}, {"id": "b", "n": 2}]
    assert result.corrupt_lines == 0


def test_read_all_on_missing_file_is_empty(tmp_path):
    store = JsonlStore(tmp_path / "does-not-exist.jsonl")
    result = store.read_all()
    assert result.records == []
    assert result.corrupt_lines == 0


def test_append_is_atomic_via_os_replace(tmp_path, monkeypatch):
    path = tmp_path / "records.jsonl"
    store = JsonlStore(path)
    store.append({"id": "a"})

    calls = []
    real_replace = __import__("os").replace

    def spy_replace(src, dst):
        calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr("agent_takkub.core.storage.jsonl_store.os.replace", spy_replace)
    store.append({"id": "b"})
    assert len(calls) == 1
    tmp_leftover = path.with_name(path.name + ".tmp")
    assert not tmp_leftover.exists()


def test_read_all_skips_corrupt_lines_and_counts_them(tmp_path):
    path = tmp_path / "records.jsonl"
    path.write_text(
        '{"id": "a"}\nnot json at all\n{"id": "b"}\n\n',
        encoding="utf-8",
    )
    store = JsonlStore(path)
    result = store.read_all()
    assert result.records == [{"id": "a"}, {"id": "b"}]
    assert result.corrupt_lines == 1


def test_core_store_path_lives_under_core_home():
    path = core_store_path("tasks")
    assert path == core_home() / "tasks.jsonl"
    assert path.parent.name == "core"


def test_read_json_missing_file_is_empty_dict(tmp_path):
    assert read_json(tmp_path / "missing.json") == {}


def test_read_json_corrupt_file_is_empty_dict(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    assert read_json(bad) == {}


def test_read_role_provider_routing_policy(tmp_path):
    path = tmp_path / "role-providers.json"
    path.write_text(json.dumps({"backend": "codex", "qa": "gemini"}), encoding="utf-8")
    policy = read_role_provider_routing_policy(path)
    assert policy.overrides == {"backend": "codex", "qa": "gemini"}


def test_read_role_provider_routing_policy_missing_file(tmp_path):
    policy = read_role_provider_routing_policy(tmp_path / "missing.json")
    assert policy.overrides == {}


_NO_QT_PROBE = """
import sys
import agent_takkub.core
import agent_takkub.core.models.provider
import agent_takkub.core.models.account
import agent_takkub.core.models.model
import agent_takkub.core.models.agent
import agent_takkub.core.models.task
import agent_takkub.core.models.conversation
import agent_takkub.core.models.capability
import agent_takkub.core.models.memory
import agent_takkub.core.models.version
import agent_takkub.core.contracts.provider_adapter
import agent_takkub.core.contracts.account_selector
import agent_takkub.core.contracts.brain_adapter
import agent_takkub.core.contracts.secret_manager
import agent_takkub.core.contracts.store
import agent_takkub.core.contracts.migration
import agent_takkub.core.contracts.routing_policy
import agent_takkub.core.storage.paths
import agent_takkub.core.storage.jsonl_store
import agent_takkub.core.storage.legacy_reader
import agent_takkub.core.providers
import agent_takkub.core.accounts
import agent_takkub.core.routing

qt_modules = [m for m in sys.modules if m.startswith("PyQt6")]
print(",".join(qt_modules))
"""


def test_importing_core_does_not_pull_in_pyqt6():
    proc = subprocess.run(
        [sys.executable, "-c", _NO_QT_PROBE],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", f"PyQt6 modules leaked in: {proc.stdout.strip()}"
