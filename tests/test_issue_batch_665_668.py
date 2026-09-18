"""2.1.19 batch — #665 (spawn-path stalls), #666 (Skill tool dropped for
skill-less panes), #667 (ledger close on idle pane + dropped-queue file),
#668/#664-follow-up (done-kept pane UX: no auto-clear, dead session reaped)."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import orchestrator as orch_mod
from agent_takkub import spawn_engine, user_profile
from agent_takkub.orchestrator import Orchestrator, _exit_key

TEST_PROJECT = "batch665proj"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda project: project or TEST_PROJECT),
    )
    o = Orchestrator()
    o.shutdown_timers()
    return o


def _pane(state: str, *, alive: bool = True, at_prompt: bool = False) -> MagicMock:
    pane = MagicMock()
    pane.state = state
    pane.session = MagicMock()
    pane.session.is_alive = alive
    pane.session.is_at_ready_prompt_cached.return_value = at_prompt
    pane.session.has_background_work.return_value = False
    return pane


# ── #666: Skill tool dropped only when provably unused ─────────────────────


class TestSkillCatalogUnused:
    def test_empty_curated_skills_dir_and_no_plugins_drops(self, tmp_path):
        (tmp_path / "skills").mkdir()
        assert spawn_engine._skill_catalog_unused({"CLAUDE_CONFIG_DIR": str(tmp_path)}, []) is True

    def test_missing_skills_dir_under_curated_config_drops(self, tmp_path):
        assert spawn_engine._skill_catalog_unused({"CLAUDE_CONFIG_DIR": str(tmp_path)}, []) is True

    def test_any_skill_keeps(self, tmp_path):
        (tmp_path / "skills" / "my-skill").mkdir(parents=True)
        assert spawn_engine._skill_catalog_unused({"CLAUDE_CONFIG_DIR": str(tmp_path)}, []) is False

    def test_plugin_dirs_keep(self, tmp_path):
        (tmp_path / "skills").mkdir()
        env = {"CLAUDE_CONFIG_DIR": str(tmp_path)}
        assert spawn_engine._skill_catalog_unused(env, ["--plugin-dir", "x"]) is False

    def test_no_config_dir_keeps(self):
        assert spawn_engine._skill_catalog_unused({}, []) is False


# ── #665: plugin-skills memoization ────────────────────────────────────────


class TestPluginSkillsMemo:
    def test_second_call_within_ttl_skips_the_walk(self, tmp_path, monkeypatch):
        user_profile._plugin_skills_cache.clear()
        calls: list[int] = []
        real = user_profile._extract_plugin_skills_uncached

        def counting(plugin_dir):
            calls.append(1)
            return real(plugin_dir)

        monkeypatch.setattr(user_profile, "_extract_plugin_skills_uncached", counting)
        sdir = tmp_path / "skills" / "alpha"
        sdir.mkdir(parents=True)
        (sdir / "SKILL.md").write_text("---\nname: alpha\n---\n", encoding="utf-8")
        first = user_profile._extract_plugin_skills(tmp_path)
        second = user_profile._extract_plugin_skills(tmp_path)
        assert "alpha" in first and first == second
        assert len(calls) == 1

    def test_expired_entry_is_rescanned(self, tmp_path, monkeypatch):
        user_profile._plugin_skills_cache.clear()
        user_profile._extract_plugin_skills(tmp_path)
        key = str(tmp_path)
        ts, val = user_profile._plugin_skills_cache[key]
        user_profile._plugin_skills_cache[key] = (
            ts - user_profile._PLUGIN_SKILLS_TTL_S - 1,
            val,
        )
        with patch.object(
            user_profile, "_extract_plugin_skills_uncached", return_value={"fresh"}
        ) as scan:
            assert user_profile._extract_plugin_skills(tmp_path) == {"fresh"}
        scan.assert_called_once()


# ── #667: busy_roles + ledger close on idle pane + dropped-queue file ──────


class TestBusyRolesAndLedgerClose:
    def test_busy_roles_excludes_idle_and_dead_panes(self, orch, monkeypatch):
        panes = orch._panes_by_project.setdefault(TEST_PROJECT, {})
        panes["working"] = _pane("working", at_prompt=False)
        panes["parked"] = _pane("working", at_prompt=True)  # progress-finished
        panes["done"] = _pane("done")
        panes["dead"] = _pane("working", alive=False)
        assert orch._busy_roles(TEST_PROJECT) == frozenset({"working"})
        # the original set still counts every live pane
        assert orch._live_roles(TEST_PROJECT) == frozenset({"working", "parked", "done"})

    def test_task_close_allows_live_idle_pane(self, orch, monkeypatch):
        from agent_takkub import task_ledger

        panes = orch._panes_by_project.setdefault(TEST_PROJECT, {})
        panes["devops"] = _pane("working", at_prompt=True)
        seen: dict = {}

        def fake_close(project, role, live_roles, force=False):
            seen["live_roles"] = live_roles
            return True, "closed ledger row for 'devops'"

        monkeypatch.setattr(task_ledger, "close_role", fake_close)
        monkeypatch.setattr(orch, "resolve_pane_role", lambda r, p: r)
        ok, _ = orch.task_close_role("devops", project=TEST_PROJECT)
        assert ok is True
        assert "devops" not in seen["live_roles"]

    def test_save_dropped_queue_writes_every_item(self, orch, monkeypatch, tmp_path):
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
        items = [
            {"_queued_task_id": "aaaa1111", "task": "งานที่หนึ่ง"},
            {"_queued_task_id": "bbbb2222", "task": "งานที่สอง"},
        ]
        path = orch._save_dropped_queue(TEST_PROJECT, "devops", items)
        assert path
        text = (tmp_path / "tasks" / TEST_PROJECT).glob("dropped-devops-*.md")
        content = next(iter(text)).read_text(encoding="utf-8")
        assert "งานที่หนึ่ง" in content and "งานที่สอง" in content
        assert "aaaa1111" in content and "bbbb2222" in content


# ── done-kept pane hygiene (#664 follow-up from the field) ─────────────────


class TestDoneKeptPaneHygiene:
    def test_reap_closes_kept_pane_whose_session_died_before_ttl(self, orch, monkeypatch):
        monkeypatch.setattr(orch_mod, "DONE_PANE_TTL_S", 1800.0)
        key = _exit_key(TEST_PROJECT, "backend")
        pane = _pane("done", alive=False)
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane
        now = time.time()
        orch._ps(key).done_kept_since = now - 5  # well inside the TTL
        with patch.object(orch, "close", return_value=(True, "closed")) as close:
            orch._reap_done_panes(now)
        close.assert_called_once()

    def test_done_state_does_not_arm_auto_clear_in_reuse_mode(self, qapp, monkeypatch):
        from agent_takkub import agent_pane as ap_mod

        monkeypatch.delenv("TAKKUB_CLOSE_ON_DONE", raising=False)
        assert ap_mod._close_on_done_env() is False
        monkeypatch.setenv("TAKKUB_CLOSE_ON_DONE", "1")
        assert ap_mod._close_on_done_env() is True


# ── #665: mcp handshake config read goes through the stat cache ────────────


def test_describe_mcp_handshake_uses_cached_read(tmp_path, monkeypatch):
    from agent_takkub import cached_read, mcp_bridge

    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"pw": {}}}), encoding="utf-8")
    out = mcp_bridge.describe_mcp_handshake("claude", ["--mcp-config", str(cfg)])
    assert out["server_names"] == ["pw"]
    # served from cache without re-reading (file older than the recent guard)
    import os as _os

    t = time.time() - 30
    _os.utime(cfg, (t, t))
    cached_read.invalidate(cfg)
    mcp_bridge.describe_mcp_handshake("claude", ["--mcp-config", str(cfg)])
    with patch("builtins.open", side_effect=AssertionError("must not re-read")):
        out2 = mcp_bridge.describe_mcp_handshake("claude", ["--mcp-config", str(cfg)])
    assert out2["server_names"] == ["pw"]
