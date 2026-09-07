"""#512 team preset model/storage/guard tests."""

from __future__ import annotations

import pytest

from agent_takkub import team_preset


@pytest.fixture(autouse=True)
def _isolate_base_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(team_preset, "_BASE_DIR", tmp_path)
    yield


def test_default_project_is_auto():
    cfg = team_preset.current("proj")
    assert cfg["preset"] == "auto"


def test_solo_lead_disables_all_positions_and_checker():
    team_preset.set_current("solo-lead", "proj")
    cfg = team_preset.current("proj")
    assert cfg["lead_may_implement"] is True
    assert cfg["checker"] is None
    assert all(v is False for v in cfg["roles"].values())
    assert team_preset.verify_mode(cfg) == "self"


def test_pair_checker_is_reviewer():
    team_preset.set_current("pair", "proj")
    cfg = team_preset.current("proj")
    assert cfg["checker"] == "reviewer"
    assert cfg["lead_may_implement"] is True
    assert team_preset.verify_mode(cfg) == "reviewer"


def test_full_enables_positions_and_qa_checker():
    team_preset.set_current("full", "proj")
    cfg = team_preset.current("proj")
    assert all(v is True for v in cfg["roles"].values())
    assert cfg["checker"] == "qa"
    assert cfg["lead_may_implement"] is False


def test_set_current_rejects_unknown_preset():
    with pytest.raises(ValueError):
        team_preset.set_current("nope", "proj")


def test_custom_requires_no_special_validation_and_round_trips():
    team_preset.set_current(
        "custom",
        "proj",
        custom={
            "roles": {"frontend": True, "backend": False, "mobile": False, "devops": False},
            "checker": "reviewer",
            "lead_may_implement": True,
            "template": "quickfix",
            "exec_mode": "solo",
        },
    )
    cfg = team_preset.current("proj")
    assert cfg["preset"] == "custom"
    assert cfg["roles"]["frontend"] is True
    assert cfg["roles"]["backend"] is False
    assert cfg["checker"] == "reviewer"


# ── per-task override ──────────────────────────────────────────────────


def test_override_wins_over_standing_preset():
    team_preset.set_current("full", "proj")
    team_preset.set_override("solo-lead", "proj")
    cfg = team_preset.current("proj")
    assert cfg["preset"] == "solo-lead"
    # standing preset unaffected
    assert team_preset.current_preset_id("proj") == "full"


def test_override_rejects_custom():
    with pytest.raises(ValueError):
        team_preset.set_override("custom", "proj")


def test_clear_override_restores_standing_preset():
    team_preset.set_current("full", "proj")
    team_preset.set_override("solo-lead", "proj")
    team_preset.clear_override("proj")
    assert team_preset.current("proj")["preset"] == "full"


# ── manual-toggle -> custom flip (#512 acceptance) ─────────────────────


def test_manual_role_toggle_flips_fixed_preset_to_custom():
    team_preset.set_current("solo-lead", "proj")
    flipped = team_preset.note_manual_roles_change(
        {"frontend": True, "backend": False, "mobile": False, "devops": False}, "proj"
    )
    assert flipped is True
    cfg = team_preset.current("proj")
    assert cfg["preset"] == "custom"
    assert cfg["roles"]["frontend"] is True
    # lead_may_implement carried over from the preset it flipped from
    assert cfg["lead_may_implement"] is True


def test_manual_toggle_matching_preset_does_not_flip():
    team_preset.set_current("full", "proj")
    flipped = team_preset.note_manual_roles_change(
        {"frontend": True, "backend": True, "mobile": True, "devops": True, "qa": True}, "proj"
    )
    assert flipped is False
    assert team_preset.current_preset_id("proj") == "full"


def test_manual_toggle_ignored_under_custom_and_auto():
    team_preset.set_current("custom", "proj", custom={"roles": {}, "checker": None})
    assert team_preset.note_manual_roles_change({"frontend": True}, "proj") is False
    team_preset.set_current("auto", "proj")
    assert team_preset.note_manual_roles_change({"frontend": True}, "proj") is False


# ── can_spawn guard ─────────────────────────────────────────────────────


def test_solo_lead_blocks_every_teammate_spawn():
    team_preset.set_current("solo-lead", "proj")
    ok, _ = team_preset.can_spawn("backend", "proj")
    assert ok is False
    ok, _ = team_preset.can_spawn("reviewer", "proj")
    assert ok is False
    ok, _ = team_preset.can_spawn("qa", "proj")
    assert ok is False


def test_solo_lead_never_blocks_lead_or_providers():
    team_preset.set_current("solo-lead", "proj")
    assert team_preset.can_spawn("lead", "proj")[0] is True
    # providers/shell/critic are outside the preset roster entirely
    for role in ("codex", "gemini", "opencode", "kimi", "cursor", "shell", "critic"):
        assert team_preset.can_spawn(role, "proj")[0] is True


def test_pair_allows_only_reviewer_as_checker():
    team_preset.set_current("pair", "proj")
    assert team_preset.can_spawn("reviewer", "proj")[0] is True
    assert team_preset.can_spawn("qa", "proj")[0] is False
    assert team_preset.can_spawn("backend", "proj")[0] is False


def test_full_allows_positions_and_qa_not_reviewer():
    team_preset.set_current("full", "proj")
    assert team_preset.can_spawn("backend", "proj")[0] is True
    assert team_preset.can_spawn("qa", "proj")[0] is True
    assert team_preset.can_spawn("reviewer", "proj")[0] is False


def test_auto_never_blocks():
    team_preset.set_current("auto", "proj")
    for role in ("backend", "qa", "reviewer", "frontend"):
        assert team_preset.can_spawn(role, "proj")[0] is True


def test_shard_suffix_stripped_before_lookup():
    team_preset.set_current("full", "proj")
    assert team_preset.can_spawn("backend#2", "proj")[0] is True
    team_preset.set_current("solo-lead", "proj")
    assert team_preset.can_spawn("backend#2", "proj")[0] is False


def test_lead_may_implement_reads_effective_preset():
    team_preset.set_current("full", "proj")
    assert team_preset.lead_may_implement("proj") is False
    team_preset.set_override("solo-lead", "proj")
    assert team_preset.lead_may_implement("proj") is True


def test_projects_are_isolated():
    team_preset.set_current("solo-lead", "proj-a")
    team_preset.set_current("full", "proj-b")
    assert team_preset.current("proj-a")["preset"] == "solo-lead"
    assert team_preset.current("proj-b")["preset"] == "full"


def test_corrupt_file_falls_back_to_default(tmp_path):
    p = team_preset.path("proj")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert team_preset.current("proj")["preset"] == "auto"


# ── #512 UI support: label/description/pane_note/resolve (Settings cards,
# status-bar chip menu, mobile drawer) ──────────────────────────────────


def test_quick_preset_ids_excludes_custom():
    assert "custom" not in team_preset.QUICK_PRESET_IDS
    assert set(team_preset.QUICK_PRESET_IDS) == {"solo-lead", "pair", "full", "auto"}


def test_description_and_pane_note_cover_every_preset_id():
    for pid in team_preset.PRESET_IDS:
        assert team_preset.description(pid)
        assert team_preset.pane_note(pid)


def test_description_and_pane_note_unknown_id_returns_empty_string():
    assert team_preset.description("nope") == ""
    assert team_preset.pane_note("nope") == ""


def test_resolve_ignores_active_override():
    team_preset.set_current("full", "proj")
    team_preset.set_override("solo-lead", "proj")
    # current() honors the override; resolve() previews the standing preset
    # a caller is ABOUT to write, so it must not read through it.
    assert team_preset.current("proj")["preset"] == "solo-lead"
    assert team_preset.resolve("full", "proj")["preset"] == "full"
    assert team_preset.resolve("pair", "proj")["checker"] == "reviewer"


def test_resolve_rejects_unknown_preset():
    with pytest.raises(ValueError):
        team_preset.resolve("nope", "proj")
