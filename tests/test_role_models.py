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
