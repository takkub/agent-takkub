"""`orchestrator.resolve_auto_assign_mode` (#364 lever 2) — auto-route a
short, no-frills assign to a native subagent instead of a full pane when the
caller left `--mode` unset; an explicit `requested_mode` always wins
unconditionally."""

from __future__ import annotations

import pytest

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import resolve_auto_assign_mode


def _resolve(role="backend", task="short task", **overrides):
    kwargs = dict(
        requested_mode=None,
        isolation="shared",
        model=None,
        provider=None,
        effort=None,
        plan=False,
        shard_total=0,
        project=None,
    )
    kwargs.update(overrides)
    return resolve_auto_assign_mode(role, task, **kwargs)


@pytest.fixture(autouse=True)
def _stub_claude_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every role resolves to claude unless a test overrides this — isolates
    these tests from the real ~/.takkub role-providers config."""
    import agent_takkub.provider_config as pc

    monkeypatch.setattr(pc, "effective_provider_for", lambda role, project=None: pc.CLAUDE)


def test_explicit_mode_always_wins_unconditionally():
    mode, note = _resolve(requested_mode="pane", task="x" * 1000)
    assert (mode, note) == ("pane", None)
    mode, note = _resolve(requested_mode="subagent", isolation="worktree")
    assert (mode, note) == ("subagent", None)


def test_short_plain_task_auto_selects_subagent():
    mode, note = _resolve()
    assert mode == "subagent"
    assert note and "auto-selected" in note


def test_long_task_falls_back_to_pane():
    mode, note = _resolve(task="x" * (orch_mod.AUTO_SUBAGENT_MAX_TASK_CHARS + 1))
    assert (mode, note) == ("pane", None)


def test_plan_falls_back_to_pane():
    mode, note = _resolve(plan=True)
    assert (mode, note) == ("pane", None)


def test_shard_fanout_falls_back_to_pane():
    mode, note = _resolve(shard_total=2)
    assert (mode, note) == ("pane", None)


def test_shard_suffixed_role_falls_back_to_pane_even_without_shard_total():
    """A direct `role#N` assign outside `--shards` (shard_total left 0) still
    must not auto-subagent — keep it in a pane alongside its siblings."""
    mode, note = _resolve(role="backend#2")
    assert (mode, note) == ("pane", None)


def test_worktree_isolation_falls_back_to_pane():
    mode, note = _resolve(isolation="worktree")
    assert (mode, note) == ("pane", None)


@pytest.mark.parametrize("override", ["model", "provider", "effort"])
def test_any_override_falls_back_to_pane(override):
    mode, note = _resolve(**{override: "something"})
    assert (mode, note) == ("pane", None)


@pytest.mark.parametrize("role", sorted(orch_mod.AUTO_SUBAGENT_EXCLUDED_ROLES))
def test_excluded_roles_fall_back_to_pane(role):
    mode, note = _resolve(role=role)
    assert (mode, note) == ("pane", None)


def test_non_claude_effective_provider_falls_back_to_pane_with_gap_note(
    monkeypatch: pytest.MonkeyPatch,
):
    import agent_takkub.provider_config as pc

    monkeypatch.setattr(pc, "effective_provider_for", lambda role, project=None: pc.CODEX)
    mode, note = _resolve(role="frontend")
    assert mode == "pane"
    assert note and "#103" in note and "codex" in note
