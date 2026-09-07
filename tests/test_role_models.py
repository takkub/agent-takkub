"""Round-trip + provider-binding tests for the per-role model store.

The provider binding is the point of this module: a model id only means
something to the CLI it was chosen for, so a role whose provider changed (or
was substituted to claude because its own CLI is off/missing) must NOT inherit
the old model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub import role_models


@pytest.fixture(autouse=True)
def redirect_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(role_models, "_PATH", tmp_path / "role-models.json")
    yield tmp_path


def test_unset_role_returns_none() -> None:
    assert role_models.model_for("backend", "codex") is None


def test_set_get_roundtrip() -> None:
    role_models.set_model("backend", "codex", "gpt-5.6")
    assert role_models.model_for("backend", "codex") == "gpt-5.6"
    assert role_models.all_models() == {"backend": {"provider": "codex", "model": "gpt-5.6"}}


def test_model_not_returned_for_a_different_provider() -> None:
    # The core guard: role re-pointed at another CLI must not reuse the model.
    role_models.set_model("backend", "kimi", "k2.5")
    assert role_models.model_for("backend", "kimi") == "k2.5"
    assert role_models.model_for("backend", "codex") is None
    assert role_models.model_for("backend", "claude") is None


def test_substituted_role_does_not_inherit_model() -> None:
    # kimi role with k2.5 that degrades to a claude substitute must spawn
    # claude WITHOUT --model k2.5.
    role_models.set_model("kimi", "kimi", "k2.5")
    assert role_models.model_for("kimi", "claude") is None


def test_empty_provider_returns_none() -> None:
    role_models.set_model("backend", "codex", "gpt-5.6")
    assert role_models.model_for("backend", "") is None


def test_empty_value_clears() -> None:
    role_models.set_model("backend", "codex", "gpt-5.6")
    role_models.set_model("backend", "codex", "   ")
    assert role_models.model_for("backend", "codex") is None


def test_clear_model() -> None:
    role_models.set_model("backend", "codex", "gpt-5.6")
    role_models.clear_model("backend")
    assert role_models.model_for("backend", "codex") is None


def test_set_strips_whitespace() -> None:
    role_models.set_model("qa", "kimi", "  k3  ")
    assert role_models.model_for("qa", "kimi") == "k3"


def test_empty_role_name_rejected() -> None:
    with pytest.raises(ValueError):
        role_models.set_model("   ", "codex", "gpt-5.6")


def test_missing_provider_rejected_when_setting_a_model() -> None:
    with pytest.raises(ValueError):
        role_models.set_model("backend", "", "gpt-5.6")


def test_custom_role_names_allowed() -> None:
    role_models.set_model("maintainer", "claude", "sonnet")
    assert role_models.model_for("maintainer", "claude") == "sonnet"


def test_clearing_model_preserves_provider_only_entry() -> None:
    """MED-2 (round2 gemini review): a role that only carries a provider
    override (no model/effort pinned — the shape `provider_config.
    set_provider` writes) must survive `set_model(role, provider, "")`
    instead of being popped entirely, per this module's own docstring
    ("An entry is only dropped once it carries neither a provider nor a
    model nor an effort")."""
    role_models.set_provider("backend", "codex")
    assert role_models.all_models() == {"backend": {"provider": "codex"}}

    role_models.set_model("backend", "codex", "")

    assert role_models.all_models() == {"backend": {"provider": "codex"}}
    assert role_models.raw_model_for("backend") == ("codex", "")


def test_clearing_effort_preserves_provider_only_entry() -> None:
    role_models.set_provider("qa", "gemini")
    role_models.set_effort("qa", "gemini", "")
    assert role_models.all_models() == {"qa": {"provider": "gemini"}}


def test_raw_model_for_reports_binding() -> None:
    role_models.set_model("backend", "codex", "gpt-5.6")
    assert role_models.raw_model_for("backend") == ("codex", "gpt-5.6")
    assert role_models.raw_model_for("frontend") is None


def test_legacy_flat_string_entry_is_dropped() -> None:
    # A pre-binding entry carries no provider — honouring it is exactly the
    # wrong-model-to-wrong-CLI hazard, so it must be ignored, not guessed at.
    role_models._PATH.write_text('{"backend": "gpt-5.6"}', encoding="utf-8")
    assert role_models.all_models() == {}
    assert role_models.model_for("backend", "codex") is None


def test_corrupt_file_behaves_empty() -> None:
    role_models._PATH.write_text("{not json", encoding="utf-8")
    assert role_models.all_models() == {}


def test_non_dict_json_behaves_empty() -> None:
    role_models._PATH.write_text('["a", "b"]', encoding="utf-8")
    assert role_models.all_models() == {}


# ── effort field (#136 follow-up: per-role reasoning-effort override) ──────


def test_legacy_entry_without_effort_key_still_loads() -> None:
    # Backward compat: a role-models.json written before this field existed
    # must load unchanged, with effort simply absent.
    role_models._PATH.write_text(
        '{"backend": {"provider": "codex", "model": "gpt-5.6"}}', encoding="utf-8"
    )
    assert role_models.all_models() == {"backend": {"provider": "codex", "model": "gpt-5.6"}}
    assert role_models.effort_for("backend", "codex") is None


def test_set_get_effort_roundtrip() -> None:
    role_models.set_model("backend", "claude", "claude-sonnet-5")
    role_models.set_effort("backend", "claude", "high")
    assert role_models.effort_for("backend", "claude") == "high"
    assert role_models.all_models() == {
        "backend": {"provider": "claude", "model": "claude-sonnet-5", "effort": "high"}
    }


def test_effort_not_returned_for_a_different_provider() -> None:
    role_models.set_model("backend", "claude", "claude-sonnet-5")
    role_models.set_effort("backend", "claude", "high")
    assert role_models.effort_for("backend", "codex") is None


def test_invalid_effort_value_drops_only_that_field() -> None:
    # "ludicrous" is not one of claude's declared effort_levels — the
    # (provider, model) pair underneath must survive, only effort is dropped.
    role_models._PATH.write_text(
        '{"backend": {"provider": "claude", "model": "claude-sonnet-5", "effort": "ludicrous"}}',
        encoding="utf-8",
    )
    assert role_models.model_for("backend", "claude") == "claude-sonnet-5"
    assert role_models.effort_for("backend", "claude") is None


def test_effort_valid_for_unregistered_provider_is_kept() -> None:
    # A provider not in PROVIDER_REGISTRY (future/custom) can't be validated
    # against a known level set — trust it rather than guess.
    role_models._PATH.write_text(
        '{"backend": {"provider": "made-up", "model": "x", "effort": "whatever"}}',
        encoding="utf-8",
    )
    assert role_models.effort_for("backend", "made-up") == "whatever"


def test_empty_effort_clears_only_effort_keeps_model() -> None:
    role_models.set_model("backend", "claude", "claude-sonnet-5")
    role_models.set_effort("backend", "claude", "high")
    role_models.set_effort("backend", "claude", "")
    assert role_models.effort_for("backend", "claude") is None
    assert role_models.model_for("backend", "claude") == "claude-sonnet-5"


def test_empty_model_clears_only_model_keeps_effort() -> None:
    role_models.set_model("backend", "claude", "claude-sonnet-5")
    role_models.set_effort("backend", "claude", "high")
    role_models.set_model("backend", "claude", "")
    assert role_models.model_for("backend", "claude") is None
    assert role_models.effort_for("backend", "claude") == "high"


def test_effort_only_entry_persists_without_a_model() -> None:
    role_models.set_effort("reviewer", "claude", "max")
    assert role_models.effort_for("reviewer", "claude") == "max"
    assert role_models.model_for("reviewer", "claude") is None
    assert role_models.all_models() == {"reviewer": {"provider": "claude", "effort": "max"}}


def test_provider_switch_drops_stale_effort() -> None:
    # Same wrong-CLI hazard as model: an effort chosen for one provider must
    # not leak onto another when the role's provider changes.
    role_models.set_model("backend", "claude", "claude-sonnet-5")
    role_models.set_effort("backend", "claude", "high")
    role_models.set_model("backend", "codex", "gpt-5.6")
    assert role_models.effort_for("backend", "codex") is None
    assert role_models.effort_for("backend", "claude") is None


def test_clear_model_also_clears_effort() -> None:
    role_models.set_model("backend", "claude", "claude-sonnet-5")
    role_models.set_effort("backend", "claude", "high")
    role_models.clear_model("backend")
    assert role_models.model_for("backend", "claude") is None
    assert role_models.effort_for("backend", "claude") is None


# ── B-H2 (round2 review, docs/audit/2026-09-07-batch-2.0.x-review-round2.md):
# #515 folded the standalone global-routing file (role-providers.json) into
# this module, making `set_provider`/`_save` the ONLY writer of global
# routing — but it only ever mirrored `role-models.json` itself
# (`dual_write_role_models`), never `v2/config/routing.json`
# (`dual_write_routing`). A provider switch through the model picker (this
# module, not `provider_config.save_providers`) left the v2 mirror stale
# and invisible to `scan_v1_only_writes`'s exit-gate check. ────────────────


def test_set_provider_mirrors_v2_routing_global(monkeypatch, isolated_v2_data_home) -> None:
    (isolated_v2_data_home / "v2").mkdir(parents=True)
    settings_home = isolated_v2_data_home.parent / "settings"
    settings_home.mkdir()
    monkeypatch.setattr("agent_takkub.config.SETTINGS_HOME", settings_home)
    monkeypatch.setattr(role_models, "_PATH", settings_home / "role-models.json")

    role_models.set_provider("frontend", "codex")

    from agent_takkub.core.migration.steps_v1 import RoleAgentMigrationStep
    from agent_takkub.core.storage.legacy_reader import read_json

    routing_target = RoleAgentMigrationStep(data_home=isolated_v2_data_home)._routing_target()
    assert read_json(routing_target).get("global") == {"frontend": "codex"}


def test_set_provider_leaves_no_v1_only_write_hit(monkeypatch, isolated_v2_data_home) -> None:
    """The regression this bug produces at the exit gate: `scan_v1_only_writes`
    reading clean (0 hits) despite the v2 mirror never having been updated —
    the exact `probe_routing_source.py` scenario from the round2 review."""
    (isolated_v2_data_home / "v2").mkdir(parents=True)
    settings_home = isolated_v2_data_home.parent / "settings"
    settings_home.mkdir()
    monkeypatch.setattr("agent_takkub.config.SETTINGS_HOME", settings_home)
    monkeypatch.setattr(role_models, "_PATH", settings_home / "role-models.json")

    role_models.set_provider("frontend", "codex")

    from agent_takkub.core.storage.v1_only_write import scan_v1_only_writes

    hits = scan_v1_only_writes(data_home=isolated_v2_data_home)
    assert not any(h.name.startswith("role-providers") for h in hits)
