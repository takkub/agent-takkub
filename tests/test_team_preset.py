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


def test_full_enables_core_positions_and_reviewer_checker():
    team_preset.set_current("full", "proj")
    cfg = team_preset.current("proj")
    assert all(cfg["roles"][r] is True for r in team_preset.CORE_POSITION_ROLES)
    assert cfg["checker"] == "reviewer"
    assert cfg["lead_may_implement"] is False


def test_full_keeps_extra_positions_off_by_default():
    """2026-09-09: tester/analyst/designer/docs/security are toggleable like
    any other position, but no built-in preset — including `full` — turns
    one on for you; a project must opt in explicitly."""
    team_preset.set_current("full", "proj")
    cfg = team_preset.current("proj")
    assert all(cfg["roles"][r] is False for r in team_preset.EXTRA_POSITION_ROLES)


def test_solo_lead_and_pair_keep_extra_positions_off():
    for preset_id in ("solo-lead", "pair"):
        team_preset.set_current(preset_id, "proj")
        cfg = team_preset.current("proj")
        assert all(cfg["roles"][r] is False for r in team_preset.EXTRA_POSITION_ROLES)


def test_auto_enables_core_and_keeps_extra_positions_off():
    """#555: `_position_roles()` grew to include EXTRA_POSITION_ROLES, which
    made the `auto` branch's `dict.fromkeys(_position_roles(project), True)`
    turn every extra role on too. Extras must stay off under `auto`, same as
    every other preset."""
    cfg = team_preset.current("proj")
    assert cfg["preset"] == "auto"
    assert all(cfg["roles"][r] is True for r in team_preset.CORE_POSITION_ROLES)
    assert all(cfg["roles"][r] is False for r in team_preset.EXTRA_POSITION_ROLES)


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


def test_manual_toggle_noop_under_auto():
    team_preset.set_current("auto", "proj")
    assert team_preset.note_manual_roles_change({"frontend": True}, "proj") is False
    assert team_preset.current_preset_id("proj") == "auto"


def test_manual_toggle_under_custom_persists_without_flipping():
    """#556: toggling a role back OFF while already on `custom` used to hit
    an early return and never persist — Settings would read the stale True
    back on reopen. It must save the new value and keep reporting no-flip."""
    team_preset.set_current("custom", "proj", custom={"roles": {"tester": False}, "checker": None})
    flipped_on = team_preset.note_manual_roles_change({"tester": True}, "proj")
    assert flipped_on is False
    assert team_preset.current("proj")["roles"]["tester"] is True

    flipped_off = team_preset.note_manual_roles_change({"tester": False}, "proj")
    assert flipped_off is False
    cfg = team_preset.current("proj")
    assert cfg["preset"] == "custom"
    assert cfg["roles"]["tester"] is False


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


def test_pair_allows_reviewer_and_qa_alias_as_checker():
    """qa is #513's legacy alias for reviewer --mode e2e (resolve_role_alias) —
    with checker=reviewer (pair's default), spawning "qa" must still resolve
    through the alias and pass, same as spawning "reviewer" directly."""
    team_preset.set_current("pair", "proj")
    assert team_preset.can_spawn("reviewer", "proj")[0] is True
    assert team_preset.can_spawn("qa", "proj")[0] is True
    assert team_preset.can_spawn("backend", "proj")[0] is False


def test_full_allows_positions_reviewer_and_qa_alias():
    """Regression for the live-test repro: `takkub assign --role qa` on the
    default 'full' preset (checker=reviewer) must NOT be rejected — qa is
    reviewer's alias, not a separate ungoverned role."""
    team_preset.set_current("full", "proj")
    assert team_preset.can_spawn("backend", "proj")[0] is True
    assert team_preset.can_spawn("reviewer", "proj")[0] is True
    assert team_preset.can_spawn("qa", "proj")[0] is True


def test_full_never_blocks_critic():
    """critic was never added to CHECKER_ROLES, so it's ungoverned and always
    passes through _governed_roles — same alias-role status as qa, kept
    symmetric here so a future regression in either direction is caught."""
    team_preset.set_current("full", "proj")
    assert team_preset.can_spawn("critic", "proj")[0] is True


def test_custom_checker_qa_explicit_keeps_qa_allowed_and_reviewer_blocked():
    """A custom preset that picks checker="qa" directly (not via the #513
    alias) must keep working exactly as before this fix: qa passes because
    it *is* the checker, reviewer is blocked because it isn't (and isn't a
    plain position either)."""
    team_preset.set_current(
        "custom",
        "proj",
        custom={
            "roles": dict.fromkeys(team_preset.CORE_POSITION_ROLES, True),
            "checker": "qa",
            "lead_may_implement": False,
        },
    )
    assert team_preset.can_spawn("qa", "proj")[0] is True
    assert team_preset.can_spawn("reviewer", "proj")[0] is False


def test_full_blocks_extra_positions_until_toggled_on():
    team_preset.set_current("full", "proj")
    assert team_preset.can_spawn("tester", "proj")[0] is False
    team_preset.set_current(
        "custom",
        "proj",
        custom={
            "roles": {**dict.fromkeys(team_preset.CORE_POSITION_ROLES, True), "tester": True},
            "checker": "qa",
            "lead_may_implement": False,
        },
    )
    assert team_preset.can_spawn("tester", "proj")[0] is True
    assert team_preset.can_spawn("analyst", "proj")[0] is False


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


class TestSettingsRoleFor:
    """#590: qa/critic must resolve provider/model/effort through whichever
    role the Settings roster actually renders a row for."""

    def test_qa_defers_to_reviewer_under_default_full_preset(self):
        team_preset.set_current("full", "proj")
        assert team_preset.settings_role_for("qa", "proj") == "reviewer"

    def test_critic_always_defers_to_reviewer(self):
        # critic has no CHECKER_ROLES entry — no preset can ever point the
        # roster at it, so it always defers regardless of checker choice.
        team_preset.set_current("full", "proj")
        assert team_preset.settings_role_for("critic", "proj") == "reviewer"
        team_preset.set_current(
            "custom",
            "proj",
            custom={"roles": dict.fromkeys(team_preset.CORE_POSITION_ROLES, True), "checker": "qa"},
        )
        assert team_preset.settings_role_for("critic", "proj") == "reviewer"

    def test_qa_keeps_own_row_when_checker_is_explicitly_qa(self):
        team_preset.set_current(
            "custom",
            "proj",
            custom={"roles": dict.fromkeys(team_preset.CORE_POSITION_ROLES, True), "checker": "qa"},
        )
        assert team_preset.settings_role_for("qa", "proj") == "qa"

    def test_reviewer_and_unrelated_roles_pass_through_unchanged(self):
        team_preset.set_current("full", "proj")
        assert team_preset.settings_role_for("reviewer", "proj") == "reviewer"
        assert team_preset.settings_role_for("backend", "proj") == "backend"
        assert team_preset.settings_role_for("lead", "proj") == "lead"

    def test_defers_under_auto_and_solo_lead_presets_too(self):
        # "auto" resolves checker="reviewer" (see _resolve); solo-lead has no
        # checker at all — neither equals "qa", so both defer to reviewer.
        team_preset.set_current("auto", "proj")
        assert team_preset.settings_role_for("qa", "proj") == "reviewer"
        team_preset.set_current("solo-lead", "proj")
        assert team_preset.settings_role_for("qa", "proj") == "reviewer"


class TestPaneDisplayLabel:
    """#590 item A: qa/critic panes label themselves after reviewer's mode
    instead of a bare "QA"/"Design Critic", unless checker=qa keeps qa's
    own row."""

    def test_qa_labels_as_reviewer_e2e_under_default_checker(self):
        team_preset.set_current("full", "proj")
        assert team_preset.pane_display_label("qa", "QA", "proj") == "Reviewer · e2e"

    def test_critic_labels_as_reviewer_ui_regardless_of_checker(self):
        team_preset.set_current("full", "proj")
        assert team_preset.pane_display_label("critic", "Design Critic", "proj") == "Reviewer · ui"

    def test_qa_keeps_own_label_when_checker_is_explicitly_qa(self):
        team_preset.set_current(
            "custom",
            "proj",
            custom={"roles": dict.fromkeys(team_preset.CORE_POSITION_ROLES, True), "checker": "qa"},
        )
        assert team_preset.pane_display_label("qa", "QA", "proj") == "QA"

    def test_other_roles_pass_through_unchanged(self):
        team_preset.set_current("full", "proj")
        assert team_preset.pane_display_label("backend", "Backend", "proj") == "Backend"
        assert team_preset.pane_display_label("reviewer", "Reviewer", "proj") == "Reviewer"


class TestStaleLegacyRoleConfigs:
    """#590 item C: qa/critic entries the roster no longer renders a row
    for, but are still on disk — surfaced for Settings, never deleted."""

    def test_reports_global_qa_entry_unused_under_default_checker(self):
        from agent_takkub import role_models

        role_models.set_provider("qa", "gemini")
        team_preset.set_current("full", "proj")

        stale = team_preset.stale_legacy_role_configs("proj")

        assert {"role": "qa", "provider": "gemini", "model": ""} in stale

    def test_project_override_wins_over_global_entry(self):
        from agent_takkub import provider_config, role_models

        role_models.set_provider("qa", "gemini")
        routing = provider_config._read_routing()
        routing["projects"]["proj"] = {"qa": "opencode"}
        provider_config._write_routing(routing["global"], routing["projects"])
        team_preset.set_current("full", "proj")

        stale = team_preset.stale_legacy_role_configs("proj")

        assert {"role": "qa", "provider": "opencode", "model": ""} in stale

    def test_nothing_stale_when_no_legacy_entry_exists(self):
        team_preset.set_current("full", "proj")
        assert team_preset.stale_legacy_role_configs("proj") == []

    def test_qa_entry_not_stale_when_checker_is_explicitly_qa(self):
        from agent_takkub import role_models

        role_models.set_provider("qa", "gemini")
        team_preset.set_current(
            "custom",
            "proj",
            custom={"roles": dict.fromkeys(team_preset.CORE_POSITION_ROLES, True), "checker": "qa"},
        )

        stale = team_preset.stale_legacy_role_configs("proj")

        assert stale == []

    def test_critic_entry_always_reportable_since_it_never_gets_its_own_row(self):
        from agent_takkub import role_models

        role_models.set_provider("critic", "gemini")
        team_preset.set_current(
            "custom",
            "proj",
            custom={"roles": dict.fromkeys(team_preset.CORE_POSITION_ROLES, True), "checker": "qa"},
        )

        stale = team_preset.stale_legacy_role_configs("proj")

        assert {"role": "critic", "provider": "gemini", "model": ""} in stale
