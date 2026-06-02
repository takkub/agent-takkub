"""Tests for pipeline_config — the ~/.takkub/pipelines.json store.

Mirrors the provider_state test convention: monkeypatch the module ``_PATH`` to
a tmp file, then exercise load/save/normalize for missing/corrupt/round-trip/
sanitize/seed behavior. Pure data layer — no Qt.
"""

from __future__ import annotations

import json

import pytest

from agent_takkub import pipeline_config, provider_state


@pytest.fixture
def tmp_path_json(tmp_path, monkeypatch):
    """Redirect pipeline_config to a tmp file so tests don't touch real config."""
    p = tmp_path / "pipelines.json"
    monkeypatch.setattr(pipeline_config, "_PATH", p)
    return p


# ── seed / load defaults ──────────────────────────────────────────────


def test_load_missing_file_returns_seed(tmp_path_json):
    state = pipeline_config.load()
    ids = [t["id"] for t in state["templates"]]
    assert ids == ["feature", "design", "quickfix"]
    assert state["activeTemplate"] == "feature"
    # every selectable role present and enabled by default
    assert set(state["rolesEnabled"]) == set(pipeline_config.VALID_ROLES)
    assert all(state["rolesEnabled"].values())


def test_seed_matches_load_on_missing(tmp_path_json):
    assert pipeline_config.seed() == pipeline_config.load()


def test_builtin_feature_pipeline_shape(tmp_path_json):
    feature = next(t for t in pipeline_config.load()["templates"] if t["id"] == "feature")
    assert feature["builtin"] is True
    # Hop 1 = frontend + backend (parallel), both auto-chained; Hop 2 = qa + reviewer
    assert [e["role"] for e in feature["hops"][0]] == ["frontend", "backend"]
    assert all(e["autoChain"] for e in feature["hops"][0])
    assert [e["role"] for e in feature["hops"][1]] == ["qa", "reviewer"]


def test_quickfix_requires_commit_flag(tmp_path_json):
    qf = next(t for t in pipeline_config.load()["templates"] if t["id"] == "quickfix")
    assert qf["hops"][0][0] == {
        "role": "backend",
        "cwd": "",
        "requiresCommit": True,
        "autoChain": False,
    }


# ── graceful degradation ──────────────────────────────────────────────


def test_corrupt_json_returns_seed(tmp_path_json):
    tmp_path_json.write_text("{not valid json", encoding="utf-8")
    assert pipeline_config.load() == pipeline_config.seed()


def test_non_dict_top_level_returns_seed(tmp_path_json):
    tmp_path_json.write_text("[1, 2, 3]", encoding="utf-8")
    assert pipeline_config.load() == pipeline_config.seed()


# ── round-trip + sanitization ─────────────────────────────────────────


def test_roundtrip_custom_template(tmp_path_json):
    payload = pipeline_config.seed()
    payload["templates"].append(
        {
            "id": "mine",
            "name": "My flow",
            "builtin": False,
            "hops": [[{"role": "backend"}], [{"role": "qa"}]],
        }
    )
    payload["activeTemplate"] = "mine"
    pipeline_config.save(payload)

    loaded = pipeline_config.load()
    ids = [t["id"] for t in loaded["templates"]]
    assert ids == ["feature", "design", "quickfix", "mine"]  # built-ins first
    assert loaded["activeTemplate"] == "mine"
    mine = next(t for t in loaded["templates"] if t["id"] == "mine")
    assert mine["builtin"] is False
    assert mine["hops"][0][0]["role"] == "backend"
    # entry got fully normalized with default flags
    assert mine["hops"][0][0] == {
        "role": "backend",
        "cwd": "",
        "requiresCommit": False,
        "autoChain": False,
    }


def test_save_writes_atomically_via_tmp_file(tmp_path_json):
    pipeline_config.save(pipeline_config.seed())
    assert tmp_path_json.exists()
    assert not tmp_path_json.with_suffix(tmp_path_json.suffix + ".tmp").exists()
    # file is valid, indented JSON with trailing newline
    raw = tmp_path_json.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    json.loads(raw)


def test_builtins_reasserted_even_if_file_tampers_them(tmp_path_json):
    # A tampered built-in (wrong hops, claims non-builtin) must be overwritten
    # by the canonical definition, and stay marked builtin.
    tmp_path_json.write_text(
        json.dumps(
            {
                "templates": [
                    {
                        "id": "feature",
                        "name": "HACKED",
                        "builtin": False,
                        "hops": [[{"role": "shell"}]],
                    }
                ],
                "activeTemplate": "feature",
            }
        ),
        encoding="utf-8",
    )
    feature = next(t for t in pipeline_config.load()["templates"] if t["id"] == "feature")
    assert feature["name"] == "Feature (UI+API)"
    assert feature["builtin"] is True
    assert [e["role"] for e in feature["hops"][0]] == ["frontend", "backend"]


def test_custom_template_claiming_builtin_id_is_dropped(tmp_path_json):
    pipeline_config.save(
        {
            "templates": [
                {"id": "design", "name": "fake", "builtin": False, "hops": [[{"role": "shell"}]]},
                {"id": "real", "name": "Real", "builtin": False, "hops": [[{"role": "qa"}]]},
            ]
        }
    )
    loaded = pipeline_config.load()
    ids = [t["id"] for t in loaded["templates"]]
    assert ids == ["feature", "design", "quickfix", "real"]
    design = next(t for t in loaded["templates"] if t["id"] == "design")
    assert design["name"] == "Design Review"  # canonical, not "fake"


def test_unknown_role_entry_dropped(tmp_path_json):
    pipeline_config.save(
        {
            "templates": [
                {
                    "id": "x",
                    "name": "X",
                    "hops": [[{"role": "frontend"}, {"role": "bogus"}, {"role": "qa"}]],
                }
            ]
        }
    )
    x = next(t for t in pipeline_config.load()["templates"] if t["id"] == "x")
    assert [e["role"] for e in x["hops"][0]] == ["frontend", "qa"]


def test_hop_dedups_repeated_role(tmp_path_json):
    pipeline_config.save(
        {
            "templates": [
                {
                    "id": "x",
                    "name": "X",
                    "hops": [[{"role": "qa"}, {"role": "qa"}, {"role": "backend"}]],
                }
            ]
        }
    )
    x = next(t for t in pipeline_config.load()["templates"] if t["id"] == "x")
    assert [e["role"] for e in x["hops"][0]] == ["qa", "backend"]


def test_roles_enabled_unknown_dropped_and_defaults_true(tmp_path_json):
    pipeline_config.save({"rolesEnabled": {"backend": False, "ghost": True}})
    roles = pipeline_config.load()["rolesEnabled"]
    assert set(roles) == set(pipeline_config.VALID_ROLES)  # ghost dropped
    assert roles["backend"] is False  # explicit False persists
    assert roles["frontend"] is True  # unspecified defaults True


def test_active_template_invalid_falls_back_to_first(tmp_path_json):
    pipeline_config.save({"activeTemplate": "does-not-exist"})
    assert pipeline_config.load()["activeTemplate"] == "feature"


def test_active_template_custom_persists(tmp_path_json):
    pipeline_config.save(
        {
            "templates": [{"id": "mine", "name": "Mine", "hops": [[{"role": "qa"}]]}],
            "activeTemplate": "mine",
        }
    )
    assert pipeline_config.load()["activeTemplate"] == "mine"


def test_providers_key_in_payload_is_ignored_by_save(tmp_path_json):
    # The settings page sends the whole blob incl. providers; save must ignore it.
    pipeline_config.save({"providers": {"codex": False, "gemini": True}, "rolesEnabled": {}})
    loaded = pipeline_config.load()
    assert "providers" not in loaded


def test_cwd_override_roundtrips(tmp_path_json):
    pipeline_config.save(
        {"templates": [{"id": "x", "name": "X", "hops": [[{"role": "backend", "cwd": "<api>"}]]}]}
    )
    x = next(t for t in pipeline_config.load()["templates"] if t["id"] == "x")
    assert x["hops"][0][0]["cwd"] == "<api>"


def test_load_returns_fresh_objects(tmp_path_json):
    a = pipeline_config.load()
    b = pipeline_config.load()
    a["templates"][0]["name"] = "mutated"
    assert b["templates"][0]["name"] == "Feature (UI+API)"


# ── provider <-> page bridge helpers (pure, no Qt) ────────────────────

_TOGGLABLE = ("codex", "gemini")


def test_with_providers_all_enabled_when_none_disabled():
    out = pipeline_config.with_providers({"x": 1}, disabled=set(), togglable=_TOGGLABLE)
    assert out["providers"] == {"codex": True, "gemini": True}
    assert out["x"] == 1  # original keys preserved


def test_with_providers_inverts_disabled_to_off():
    out = pipeline_config.with_providers({}, disabled={"codex"}, togglable=_TOGGLABLE)
    assert out["providers"] == {"codex": False, "gemini": True}


def test_with_providers_does_not_mutate_input():
    payload = {"templates": []}
    pipeline_config.with_providers(payload, disabled={"gemini"}, togglable=_TOGGLABLE)
    assert "providers" not in payload


def test_provider_disabled_targets_inverts_enabled_map():
    payload = {"providers": {"codex": False, "gemini": True}}
    assert pipeline_config.provider_disabled_targets(payload, _TOGGLABLE) == {
        "codex": True,
        "gemini": False,
    }


def test_provider_disabled_targets_only_present_providers():
    payload = {"providers": {"codex": True}}
    assert pipeline_config.provider_disabled_targets(payload, _TOGGLABLE) == {"codex": False}


def test_provider_disabled_targets_missing_or_bad_providers_key():
    assert pipeline_config.provider_disabled_targets({}, _TOGGLABLE) == {}
    assert pipeline_config.provider_disabled_targets({"providers": "nope"}, _TOGGLABLE) == {}
    assert pipeline_config.provider_disabled_targets("not a dict", _TOGGLABLE) == {}


def test_provider_helpers_round_trip_through_real_togglable():
    # with_providers (disabled→enabled map) then provider_disabled_targets
    # (enabled map→disabled targets) recovers the original disabled membership.
    disabled = {"codex"}
    composed = pipeline_config.with_providers({}, disabled, provider_state.TOGGLABLE)
    targets = pipeline_config.provider_disabled_targets(composed, provider_state.TOGGLABLE)
    assert {p for p, d in targets.items() if d} == disabled
