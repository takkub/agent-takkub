"""Tests for QA shard fan-out (--shards N) feature.

Covers:
  - _split_shard helper
  - validate_name shard suffix
  - Shard pane spawn (qa#1, qa#2 independent, not overwriting each other)
  - TAKKUB_BASE_ROLE / TAKKUB_SHARD / TAKKUB_SHARD_TOTAL env injection
  - agent_role_dir uses base role, not shard key
  - done() aggregate: all N done → consolidated handoff
  - done() partial-pass: 1 done + 1 crashed → still fires handoff
  - _warn_lead_respawn_capped → shard fail detection
  - validate_name rejects bad shard suffixes
  - cli --shards sends per-shard requests with shard_total
"""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.config import validate_name
from agent_takkub.orchestrator import (
    Orchestrator,
    ShardGroup,
    _exit_key,
    _split_shard,
)

TEST_PROJECT = "shardtest"


# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────


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
    o._idle_watchdog.stop()
    return o


def _make_pane(role_name: str = "qa") -> MagicMock:
    pane = MagicMock()
    pane.role = MagicMock()
    pane.role.name = role_name
    pane.state = "working"
    pane.session = MagicMock()
    pane.session.is_alive = True
    pane._session_cwd = "/project/web"
    pane._transcript_path = None
    return pane


def _make_lead() -> MagicMock:
    lead = MagicMock()
    lead.session = MagicMock()
    lead.session.is_alive = True
    return lead


# ──────────────────────────────────────────────────────────────
# _split_shard helper
# ──────────────────────────────────────────────────────────────


class TestSplitShard:
    def test_plain_role(self) -> None:
        assert _split_shard("qa") == ("qa", None)

    def test_shard_key(self) -> None:
        assert _split_shard("qa#1") == ("qa", 1)
        assert _split_shard("qa#2") == ("qa", 2)
        assert _split_shard("frontend#10") == ("frontend", 10)

    def test_non_qa_role(self) -> None:
        assert _split_shard("backend") == ("backend", None)
        assert _split_shard("backend#3") == ("backend", 3)


# ──────────────────────────────────────────────────────────────
# validate_name shard suffix
# ──────────────────────────────────────────────────────────────


class TestValidateNameShard:
    def test_accepts_shard_key(self) -> None:
        assert validate_name("qa#1", "role") == "qa#1"
        assert validate_name("qa#99", "role") == "qa#99"
        assert validate_name("QA#1", "role") == "qa#1"  # lowercases

    def test_rejects_zero_shard(self) -> None:
        with pytest.raises(ValueError):
            validate_name("qa#0", "role")

    def test_rejects_negative_shard(self) -> None:
        with pytest.raises(ValueError):
            validate_name("qa#-1", "role")

    def test_rejects_non_numeric_shard(self) -> None:
        with pytest.raises(ValueError):
            validate_name("qa#abc", "role")

    def test_rejects_empty_shard(self) -> None:
        with pytest.raises(ValueError):
            validate_name("qa#", "role")

    def test_rejects_bad_base(self) -> None:
        with pytest.raises(ValueError):
            validate_name("#1", "role")

    def test_plain_still_works(self) -> None:
        assert validate_name("backend", "role") == "backend"

    def test_rejects_traversal(self) -> None:
        with pytest.raises(ValueError):
            validate_name("../etc#1", "role")


# ──────────────────────────────────────────────────────────────
# ShardGroup dataclass
# ──────────────────────────────────────────────────────────────


class TestShardGroup:
    def test_defaults(self) -> None:
        g = ShardGroup(base_role="qa", total=3)
        assert g.done == {}
        assert g.failed == set()
        assert g.closed is False

    def test_aggregate_done(self) -> None:
        g = ShardGroup(base_role="qa", total=2)
        g.done["qa#1"] = "shard 1 ok"
        g.done["qa#2"] = "shard 2 ok"
        assert len(g.done) == 2


# ──────────────────────────────────────────────────────────────
# Shard pane independence
# ──────────────────────────────────────────────────────────────


class TestShardPaneIndependence:
    def test_two_shards_registered_separately(self, orch: Orchestrator) -> None:
        """qa#1 and qa#2 must coexist in the registry without overwriting."""
        pane1 = _make_pane("qa#1")
        pane2 = _make_pane("qa#2")

        orch._panes_by_project.setdefault(TEST_PROJECT, {})["qa#1"] = pane1
        orch._panes_by_project[TEST_PROJECT]["qa#2"] = pane2

        assert orch._panes_by_project[TEST_PROJECT]["qa#1"] is pane1
        assert orch._panes_by_project[TEST_PROJECT]["qa#2"] is pane2
        assert pane1 is not pane2

    def test_shards_have_independent_pane_state(self, orch: Orchestrator) -> None:
        """PaneState for qa#1 must not affect qa#2."""
        key1 = _exit_key(TEST_PROJECT, "qa#1")
        key2 = _exit_key(TEST_PROJECT, "qa#2")
        orch._ps(key1).shard_total = 3
        orch._ps(key2).shard_total = 3
        orch._ps(key1).last_assigned_task = "task A"
        orch._ps(key2).last_assigned_task = "task B"

        assert orch._pane_state[key1].last_assigned_task == "task A"
        assert orch._pane_state[key2].last_assigned_task == "task B"

    def test_done_for_qa1_does_not_pop_qa2_state(self, orch: Orchestrator) -> None:
        """done(qa#1) pops only qa#1 state, leaving qa#2 intact."""
        pane1 = _make_pane("qa#1")
        pane2 = _make_pane("qa#2")
        lead = _make_lead()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["qa#1"] = pane1
        orch._panes_by_project[TEST_PROJECT]["qa#2"] = pane2
        orch._panes_by_project[TEST_PROJECT]["lead"] = lead

        key1 = _exit_key(TEST_PROJECT, "qa#1")
        key2 = _exit_key(TEST_PROJECT, "qa#2")
        orch._ps(key1).shard_total = 2
        orch._ps(key2).shard_total = 2
        orch._shard_groups[f"{TEST_PROJECT}::qa"] = ShardGroup(base_role="qa", total=2)

        with patch("agent_takkub.orchestrator.subprocess.run"):
            orch.done("qa#1", note="shard 1 ok", project=TEST_PROJECT)

        # qa#1 state popped, qa#2 state intact
        assert orch._pane_state.get(key1) is None
        assert orch._pane_state.get(key2) is not None


# ──────────────────────────────────────────────────────────────
# Shard aggregate done → consolidated handoff
# ──────────────────────────────────────────────────────────────


def _written_str(mock_session: MagicMock) -> str:
    parts: list[str] = []
    for c in mock_session.write.call_args_list:
        arg = c.args[0] if c.args else b""
        if isinstance(arg, bytes):
            parts.append(arg.decode("utf-8", errors="replace"))
        elif isinstance(arg, str):
            parts.append(arg)
    return "".join(parts)


class TestShardAggregate:
    def _setup(self, orch: Orchestrator, total: int = 2):
        shards = {}
        for n in range(1, total + 1):
            key_n = f"qa#{n}"
            pane = _make_pane(key_n)
            orch._panes_by_project.setdefault(TEST_PROJECT, {})[key_n] = pane
            ek = _exit_key(TEST_PROJECT, key_n)
            orch._ps(ek).shard_total = total
            shards[key_n] = pane
        lead = _make_lead()
        orch._panes_by_project[TEST_PROJECT]["lead"] = lead
        orch._shard_groups[f"{TEST_PROJECT}::qa"] = ShardGroup(base_role="qa", total=total)
        return shards, lead

    def test_handoff_fires_after_all_shards_done(self, orch: Orchestrator) -> None:
        """Consolidated handoff injected to Lead only after all N shards done."""
        _shards, lead = self._setup(orch, total=2)

        with patch("agent_takkub.orchestrator.subprocess.run"):
            ok1, _ = orch.done("qa#1", note="shard 1 ok", project=TEST_PROJECT)
            assert ok1 is True
            # After first shard: group not yet closed, no handoff
            group = orch._shard_groups.get(f"{TEST_PROJECT}::qa")
            assert group is not None  # might be popped after 2nd done

            ok2, _ = orch.done("qa#2", note="shard 2 ok", project=TEST_PROJECT)
            assert ok2 is True

        injected = _written_str(lead.session)
        assert "qa fan-out complete" in injected
        assert "2/2" in injected
        assert "shard 1" in injected
        assert "shard 2" in injected

    def test_no_handoff_until_all_done(self, orch: Orchestrator) -> None:
        """With 3 shards, handoff fires only after the 3rd done."""
        _shards, lead = self._setup(orch, total=3)

        with patch("agent_takkub.orchestrator.subprocess.run"):
            orch.done("qa#1", note="s1", project=TEST_PROJECT)
            orch.done("qa#2", note="s2", project=TEST_PROJECT)
            # only 2 of 3 done — no handoff yet; per-shard notices suppressed in shard mode
            assert lead.session.write.call_count == 0

            orch.done("qa#3", note="s3", project=TEST_PROJECT)

        injected = _written_str(lead.session)
        assert "qa fan-out complete" in injected

    def test_group_removed_after_handoff(self, orch: Orchestrator) -> None:
        _shards, _lead = self._setup(orch, total=2)
        with patch("agent_takkub.orchestrator.subprocess.run"):
            orch.done("qa#1", note="a", project=TEST_PROJECT)
            orch.done("qa#2", note="b", project=TEST_PROJECT)
        assert orch._shard_groups.get(f"{TEST_PROJECT}::qa") is None


# ──────────────────────────────────────────────────────────────
# Partial-pass: failed shard still fires handoff
# ──────────────────────────────────────────────────────────────


class TestShardPartialPass:
    def test_failed_shard_triggers_handoff(self, orch: Orchestrator) -> None:
        """When 1 shard done + 1 shard respawn-capped, consolidated handoff fires."""
        pane1 = _make_pane("qa#1")
        pane2 = _make_pane("qa#2")
        lead = _make_lead()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["qa#1"] = pane1
        orch._panes_by_project[TEST_PROJECT]["qa#2"] = pane2
        orch._panes_by_project[TEST_PROJECT]["lead"] = lead

        for n in (1, 2):
            ek = _exit_key(TEST_PROJECT, f"qa#{n}")
            orch._ps(ek).shard_total = 2
        orch._shard_groups[f"{TEST_PROJECT}::qa"] = ShardGroup(base_role="qa", total=2)

        with patch("agent_takkub.orchestrator.subprocess.run"):
            orch.done("qa#1", note="shard 1 done", project=TEST_PROJECT)

        # Simulate qa#2 respawn-capped
        ek2 = _exit_key(TEST_PROJECT, "qa#2")
        orch._ps(ek2).shard_total = 2  # still in pane_state (not yet closed)
        orch._warn_lead_respawn_capped("qa#2", TEST_PROJECT)

        injected = _written_str(lead.session)
        assert "qa fan-out complete" in injected
        assert "CRASHED" in injected


# ──────────────────────────────────────────────────────────────
# Timeout → partial handoff
# ──────────────────────────────────────────────────────────────


class TestShardGroupTimeout:
    def test_timeout_fires_partial_handoff(self, orch: Orchestrator) -> None:
        lead = _make_lead()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["lead"] = lead

        group = ShardGroup(base_role="qa", total=3)
        group.done["qa#1"] = "shard 1 ok"
        group_key = f"{TEST_PROJECT}::qa"
        orch._shard_groups[group_key] = group

        orch._check_shard_group_timeout(TEST_PROJECT, group_key)

        assert group.closed is True
        assert orch._shard_groups.get(group_key) is None
        injected = _written_str(lead.session)
        assert "timeout" in injected
        assert "NO RESPONSE" in injected  # shard 2 and 3 missing

    def test_timeout_noop_if_already_closed(self, orch: Orchestrator) -> None:
        lead = _make_lead()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["lead"] = lead

        group = ShardGroup(base_role="qa", total=2)
        group.closed = True
        group_key = f"{TEST_PROJECT}::qa"
        orch._shard_groups[group_key] = group

        orch._check_shard_group_timeout(TEST_PROJECT, group_key)

        # Lead not contacted (group already done)
        assert lead.session.write.call_count == 0


# ──────────────────────────────────────────────────────────────
# assign() creates shard group
# ──────────────────────────────────────────────────────────────


class TestAssignCreatesShardGroup:
    def test_assign_with_shard_total_creates_group(self, orch: Orchestrator) -> None:
        pane = _make_pane("qa#1")
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["qa#1"] = pane

        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready"),
        ):
            orch.assign("qa#1", cwd="/web", task="smoke", shard_total=3, project=TEST_PROJECT)

        group_key = f"{TEST_PROJECT}::qa"
        assert group_key in orch._shard_groups
        assert orch._shard_groups[group_key].total == 3

        ek = _exit_key(TEST_PROJECT, "qa#1")
        assert orch._pane_state[ek].shard_total == 3

    def test_assign_without_shard_no_group(self, orch: Orchestrator) -> None:
        pane = _make_pane("backend")
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane

        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready"),
        ):
            orch.assign("backend", cwd="/api", task="add endpoint", project=TEST_PROJECT)

        assert f"{TEST_PROJECT}::backend" not in orch._shard_groups


# ──────────────────────────────────────────────────────────────
# Edge cases not covered by the main 24-test suite
# ──────────────────────────────────────────────────────────────


class TestShardEdgeCases:
    def test_validate_name_rejects_double_shard_suffix(self) -> None:
        """qa#1#2 must be rejected — partition splits on first '#' giving shard='1#2'."""
        with pytest.raises(ValueError):
            validate_name("qa#1#2", "role")

    def test_shards_1_no_shard_group(self, orch: Orchestrator) -> None:
        """--shards 1 falls through to normal assign (no suffix, no shard group)."""
        pane = _make_pane("qa")
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["qa"] = pane

        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready"),
        ):
            # shard_total=0 mirrors what cmd_assign sends when shards==1
            orch.assign("qa", cwd="/web", task="smoke", shard_total=0, project=TEST_PROJECT)

        # No shard group created, pane key has no suffix
        assert f"{TEST_PROJECT}::qa" not in orch._shard_groups
        assert "qa#1" not in orch._panes_by_project.get(TEST_PROJECT, {})

    def test_split_shard_plain(self) -> None:
        assert _split_shard("qa") == ("qa", None)

    def test_split_shard_indexed(self) -> None:
        assert _split_shard("qa#2") == ("qa", 2)

    def test_split_shard_double_hash_raises(self) -> None:
        """_split_shard('qa#1#2') raises ValueError — int('1#2') is not parseable.

        validate_name() rejects 'qa#1#2' before _split_shard is ever called,
        but guard the helper directly too.
        """
        with pytest.raises(ValueError):
            _split_shard("qa#1#2")


# ──────────────────────────────────────────────────────────────
# CLI: --shards + --auto-chain rejection
# ──────────────────────────────────────────────────────────────


class TestShardAutoChainRejection:
    """--shards N and --auto-chain must not be used together:
    shard fan-out already emits a consolidated handoff;
    --auto-chain would fire a second one when the last shard finishes."""

    def _args(self, shards: int, auto_chain: bool) -> argparse.Namespace:
        return argparse.Namespace(
            role="qa",
            shards=shards,
            auto_chain=auto_chain,
            cwd=None,
            task="smoke",
            requires_commit=False,
        )

    def test_shards_and_auto_chain_rejected(self) -> None:
        from agent_takkub.cli import cmd_assign

        result = cmd_assign(self._args(shards=3, auto_chain=True))
        assert result["ok"] is False
        assert "--auto-chain" in result["msg"] or "auto-chain" in result["msg"].lower()
        assert "--shards" in result["msg"] or "shard" in result["msg"].lower()

    def test_shards_without_auto_chain_not_rejected(self) -> None:
        from unittest.mock import patch

        from agent_takkub.cli import cmd_assign

        with patch("agent_takkub.cli._request", return_value={"ok": True}):
            result = cmd_assign(self._args(shards=2, auto_chain=False))
        assert result["ok"] is True

    def test_auto_chain_without_shards_not_rejected(self) -> None:
        from unittest.mock import patch

        from agent_takkub.cli import cmd_assign

        with patch("agent_takkub.cli._request", return_value={"ok": True}):
            result = cmd_assign(self._args(shards=1, auto_chain=True))
        assert result["ok"] is True


# ──────────────────────────────────────────────────────────────
# #1 — --shards clamp (1–8)
# ──────────────────────────────────────────────────────────────


class TestShardClamp:
    """cmd_assign must reject --shards values outside [1, 8]."""

    def _args(self, shards: int) -> argparse.Namespace:
        return argparse.Namespace(
            role="qa",
            shards=shards,
            auto_chain=False,
            cwd=None,
            task="smoke",
            requires_commit=False,
        )

    def test_shards_zero_rejected(self) -> None:
        from agent_takkub.cli import cmd_assign

        result = cmd_assign(self._args(shards=0))
        assert result["ok"] is False
        assert "1" in result["msg"] and "8" in result["msg"]

    def test_shards_negative_rejected(self) -> None:
        from agent_takkub.cli import cmd_assign

        result = cmd_assign(self._args(shards=-3))
        assert result["ok"] is False

    def test_shards_nine_rejected(self) -> None:
        from agent_takkub.cli import cmd_assign

        result = cmd_assign(self._args(shards=9))
        assert result["ok"] is False
        assert "8" in result["msg"]

    def test_shards_eight_accepted(self) -> None:
        from unittest.mock import patch

        from agent_takkub.cli import cmd_assign

        with patch("agent_takkub.cli._request", return_value={"ok": True}):
            result = cmd_assign(self._args(shards=8))
        assert result["ok"] is True

    def test_shards_one_accepted(self) -> None:
        from unittest.mock import patch

        from agent_takkub.cli import cmd_assign

        with patch("agent_takkub.cli._request", return_value={"ok": True}):
            result = cmd_assign(self._args(shards=1))
        assert result["ok"] is True


# ──────────────────────────────────────────────────────────────
# #2 — generation guard on shard-group timeout
# ──────────────────────────────────────────────────────────────


class TestShardGenerationGuard:
    def test_stale_timer_bails_on_generation_mismatch(self, orch: Orchestrator) -> None:
        """Timer from first fan-out must not close a newer group with same key."""
        lead = _make_lead()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["lead"] = lead

        # First fan-out — capture its generation
        old_group = ShardGroup(base_role="qa", total=2)
        old_gen = old_group.generation
        group_key = f"{TEST_PROJECT}::qa"
        orch._shard_groups[group_key] = old_group
        orch._check_shard_group_timeout(TEST_PROJECT, group_key)
        assert old_group.closed is True
        orch._shard_groups.pop(group_key, None)

        # Second fan-out with same key — different generation
        new_group = ShardGroup(base_role="qa", total=2)
        assert new_group.generation != old_gen, "generations must be unique"
        orch._shard_groups[group_key] = new_group

        # Stale timer fires with old generation — must bail
        orch._check_shard_group_timeout(TEST_PROJECT, group_key, generation=old_gen)
        assert new_group.closed is False  # not touched by stale timer
        assert lead.session.write.call_count == 1  # only from first timeout

    def test_matching_generation_fires_timeout(self, orch: Orchestrator) -> None:
        lead = _make_lead()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["lead"] = lead
        group = ShardGroup(base_role="qa", total=2)
        group_key = f"{TEST_PROJECT}::qa"
        orch._shard_groups[group_key] = group
        gen = group.generation

        orch._check_shard_group_timeout(TEST_PROJECT, group_key, generation=gen)
        assert group.closed is True

    def test_shard_group_has_unique_generations(self) -> None:
        g1 = ShardGroup(base_role="qa", total=2)
        g2 = ShardGroup(base_role="qa", total=2)
        assert g1.generation != g2.generation


# ──────────────────────────────────────────────────────────────
# #3 — late-complete notice when shard group already closed
# ──────────────────────────────────────────────────────────────


class TestShardLateComplete:
    def test_late_done_sends_notice_to_alive_lead(self, orch: Orchestrator) -> None:
        """done() after group already closed/popped must inject a notice, not drop."""
        pane = _make_pane("qa#1")
        lead = _make_lead()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["qa#1"] = pane
        orch._panes_by_project[TEST_PROJECT]["lead"] = lead

        # Set up pane_state so done() sees had_shard_total > 0
        ek = _exit_key(TEST_PROJECT, "qa#1")
        orch._ps(ek).shard_total = 2
        # No group registered (simulates timeout + pop)

        with patch("agent_takkub.orchestrator.subprocess.run"):
            ok, _ = orch.done("qa#1", note="arrived late", project=TEST_PROJECT)

        assert ok is True
        written = _written_str(lead.session)
        assert "late-complete" in written or "late" in written.lower()

    def test_late_done_queues_notice_when_lead_absent(self, orch: Orchestrator) -> None:
        """Late-complete notice goes to pending queue when Lead is offline."""
        pane = _make_pane("qa#1")
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["qa#1"] = pane
        # No lead registered

        ek = _exit_key(TEST_PROJECT, "qa#1")
        orch._ps(ek).shard_total = 2

        with patch("agent_takkub.orchestrator.subprocess.run"):
            orch.done("qa#1", note="late", project=TEST_PROJECT)

        queue = orch._pending_done_notices.get(TEST_PROJECT, [])
        assert any("late" in entry.get("note", "").lower() for entry in queue)


# ──────────────────────────────────────────────────────────────
# #4 — respawn-capped: shard bookkeeping before Lead-alive gate
# ──────────────────────────────────────────────────────────────


class TestShardRespawnCappedLeadDown:
    def test_shard_bookkeeping_fires_even_when_lead_down(self, orch: Orchestrator) -> None:
        """Group must close and queue handoff even when Lead is absent."""
        pane1 = _make_pane("qa#1")
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["qa#1"] = pane1
        # No lead

        ek1 = _exit_key(TEST_PROJECT, "qa#1")
        orch._ps(ek1).shard_total = 2
        group = ShardGroup(base_role="qa", total=2)
        group.done["qa#2"] = "shard 2 ok"  # qa#2 already done
        orch._shard_groups[f"{TEST_PROJECT}::qa"] = group

        # qa#1 caps — Lead is not running
        orch._warn_lead_respawn_capped("qa#1", TEST_PROJECT)

        # Group must be closed and handoff queued
        assert group.closed is True
        assert orch._shard_groups.get(f"{TEST_PROJECT}::qa") is None
        queue = orch._pending_done_notices.get(TEST_PROJECT, [])
        assert any("fan-out" in entry.get("body", "") for entry in queue)


# ──────────────────────────────────────────────────────────────
# #5 — spawn-fail records into shard group
# ──────────────────────────────────────────────────────────────


class TestShardSpawnFail:
    def test_spawn_fail_records_failed_shard(self, orch: Orchestrator) -> None:
        """When assign() fails to spawn a shard, group.failed gets the shard key."""
        lead = _make_lead()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["lead"] = lead

        # Pre-create group with qa#1 already done
        group = ShardGroup(base_role="qa", total=2)
        group.done["qa#1"] = "shard 1 ok"
        orch._shard_groups[f"{TEST_PROJECT}::qa"] = group

        with patch.object(orch, "spawn", return_value=(False, "pty error")):
            ok, _ = orch.assign(
                "qa#2", cwd="/web", task="smoke", shard_total=2, project=TEST_PROJECT
            )

        assert ok is False
        # Group should now be closed (1 done + 1 failed = 2 = total)
        assert group.closed is True

    def test_spawn_fail_creates_group_if_missing(self, orch: Orchestrator) -> None:
        """If group doesn't exist yet when first shard spawn fails, create it."""
        lead = _make_lead()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["lead"] = lead

        with patch.object(orch, "spawn", return_value=(False, "pty error")):
            orch.assign("qa#1", cwd="/web", task="smoke", shard_total=1, project=TEST_PROJECT)

        # Group created and immediately closed (1 failed = 1 total)
        group_key = f"{TEST_PROJECT}::qa"
        assert orch._shard_groups.get(group_key) is None  # closed + popped


# ──────────────────────────────────────────────────────────────
# #8 — close() triggers auto-chain handoff
# ──────────────────────────────────────────────────────────────


class TestCloseAutoChain:
    def test_close_last_auto_chain_pane_fires_handoff(self, orch: Orchestrator) -> None:
        """close() on the last auto-chain pane must inject the handoff prompt."""
        pane = _make_pane("frontend")
        lead = _make_lead()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["frontend"] = pane
        orch._panes_by_project[TEST_PROJECT]["lead"] = lead

        ek = _exit_key(TEST_PROJECT, "frontend")
        orch._ps(ek).auto_chain = True

        orch.close("frontend", project=TEST_PROJECT, force=True)

        written = _written_str(lead.session)
        assert "auto-chain handoff" in written

    def test_close_non_auto_chain_pane_no_handoff(self, orch: Orchestrator) -> None:
        """close() on a regular pane must NOT inject auto-chain handoff."""
        pane = _make_pane("frontend")
        lead = _make_lead()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["frontend"] = pane
        orch._panes_by_project[TEST_PROJECT]["lead"] = lead
        # auto_chain NOT set

        orch.close("frontend", project=TEST_PROJECT, force=True)

        written = _written_str(lead.session)
        assert "auto-chain handoff" not in written

    def test_close_non_last_auto_chain_no_handoff(self, orch: Orchestrator) -> None:
        """close() when another auto-chain pane is still pending must NOT fire."""
        pane1 = _make_pane("frontend")
        pane2 = _make_pane("backend")
        lead = _make_lead()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["frontend"] = pane1
        orch._panes_by_project[TEST_PROJECT]["backend"] = pane2
        orch._panes_by_project[TEST_PROJECT]["lead"] = lead

        ek1 = _exit_key(TEST_PROJECT, "frontend")
        ek2 = _exit_key(TEST_PROJECT, "backend")
        orch._ps(ek1).auto_chain = True
        orch._ps(ek2).auto_chain = True

        orch.close("frontend", project=TEST_PROJECT, force=True)
        # backend still pending → no handoff
        written = _written_str(lead.session)
        assert "auto-chain handoff" not in written


# ──────────────────────────────────────────────────────────────
# #9 — snapshot_state includes last_task + session_uuid;
#       restore_teammates re-pastes task
# ──────────────────────────────────────────────────────────────


class TestSnapshotAndRestore:
    def test_snapshot_includes_last_task_and_uuid(self, orch: Orchestrator) -> None:
        pane = _make_pane("frontend")
        pane.state = "working"
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["frontend"] = pane

        ek = _exit_key(TEST_PROJECT, "frontend")
        orch._ps(ek).last_assigned_task = "implement login form"
        orch._ps(ek).session_uuid = "uuid-abc"

        snap = orch.snapshot_state()
        entries = snap["projects"].get(TEST_PROJECT, [])
        assert len(entries) == 1
        assert entries[0]["last_task"] == "implement login form"
        assert entries[0]["session_uuid"] == "uuid-abc"

    def test_snapshot_empty_task_when_none(self, orch: Orchestrator) -> None:
        pane = _make_pane("backend")
        pane.state = "working"
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane
        # No pane_state set — last_task defaults to ""

        snap = orch.snapshot_state()
        entries = snap["projects"].get(TEST_PROJECT, [])
        assert any(e.get("last_task", None) == "" for e in entries)

    def test_restore_teammates_queues_notice_with_task(
        self, orch: Orchestrator, tmp_path: pytest.TempPathFactory
    ) -> None:
        """restore_teammates queues a Lead notice when last_task is present."""
        import json
        import pathlib

        from agent_takkub import orchestrator as orch_mod

        snap = {
            "saved_at": "2099-01-01T00:00:00",
            "projects": {
                TEST_PROJECT: [
                    {
                        "role": "frontend",
                        "cwd": "/web",
                        "state": "working",
                        "last_task": "add /login form",
                        "session_uuid": "",
                    }
                ]
            },
        }
        fake_file = pathlib.Path(str(id(self)) + "-last-session.json")
        # Patch _LAST_SESSION_FILE and _LAST_SESSION_MAX_AGE_SEC
        monkeypatch_ns = pytest.MonkeyPatch()
        monkeypatch_ns.setattr(orch_mod, "_LAST_SESSION_FILE", fake_file)
        monkeypatch_ns.setattr(orch_mod, "_LAST_SESSION_MAX_AGE_SEC", 10**9)
        fake_file.write_text(json.dumps(snap), encoding="utf-8")
        try:
            with (
                patch.object(orch, "spawn", return_value=(True, "ok")),
                patch.object(orch, "_send_when_ready"),
            ):
                count = orch.restore_teammates()

            assert count == 1
            queue = orch._pending_done_notices.get(TEST_PROJECT, [])
            assert any("restart" in entry.get("body", "").lower() for entry in queue)
        finally:
            fake_file.unlink(missing_ok=True)
            monkeypatch_ns.undo()
