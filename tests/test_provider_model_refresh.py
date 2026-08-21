"""Tests for provider_model_refresh.py — boot-time model-catalog bump
(user directive 2026-08-20: "gemini ออก 3.7 แล้วแต่ cockpit ยังรู้จักรุ่นเก่า").

No real subprocess ever runs: `_run` is monkeypatched or `_discover_gemini_models`
is patched directly in every test.

The JSON-path fixtures below are the ACTUAL stdout captured from a real
`agy --output-format json models` run (agy 1.1.15, 2026-08-20, issue #317)
— not a guessed schema.
"""

from __future__ import annotations

import json
import subprocess

import pytest

import agent_takkub.provider_model_refresh as pmr

# Byte-for-byte real capture: `agy --output-format json models`, agy 1.1.15,
# 2026-08-20 (issue #317). Order matches the text listing (newest-first
# per family).
REAL_AGY_JSON_STDOUT = (
    '{"conversation_id":"","status":"SUCCESS","response":'
    '"gemini-3.7-flash-high\\tGemini 3.7 Flash (High)\\n'
    "gemini-3.6-flash-high\\tGemini 3.6 Flash (High)\\n"
    'gemini-3.5-flash-high\\tGemini 3.5 Flash (High)\\n",'
    '"duration_seconds":0,"num_turns":0,'
    '"usage":{"input_tokens":0,"output_tokens":0,"thinking_tokens":0,'
    '"cache_read_tokens":0,"total_tokens":0},'
    '"command":{"name":"models","data":{"models":['
    '{"id":"gemini-3.7-flash-high","label":"Gemini 3.7 Flash (High)"},'
    '{"id":"gemini-3.6-flash-high","label":"Gemini 3.6 Flash (High)"},'
    '{"id":"gemini-3.5-flash-high","label":"Gemini 3.5 Flash (High)"}'
    "]}}}"
)


class TestFamilyKey:
    @pytest.mark.parametrize(
        "model_id,expected",
        [
            ("gemini-3.7-flash-high", ("gemini", "flash", "high")),
            ("gemini-3.5-flash-medium", ("gemini", "flash", "medium")),
            ("gemini-3.1-pro-low", ("gemini", "pro", "low")),
        ],
    )
    def test_strips_version_token(self, model_id: str, expected: tuple[str, ...]) -> None:
        assert pmr._family_key(model_id) == expected

    def test_no_version_token_returns_none(self) -> None:
        assert pmr._family_key("gpt-oss-120b-medium") is None

    def test_different_tiers_are_different_families(self) -> None:
        assert pmr._family_key("gemini-3.7-flash-high") != pmr._family_key("gemini-3.7-flash-low")


class TestPickLatestPerFamily:
    def test_first_occurrence_wins(self) -> None:
        ids = [
            "gemini-3.7-flash-high",
            "gemini-3.6-flash-high",
            "gemini-3.5-flash-high",
        ]
        assert pmr._pick_latest_per_family(ids) == {
            ("gemini", "flash", "high"): "gemini-3.7-flash-high"
        }

    def test_unparseable_ids_are_skipped(self) -> None:
        ids = ["gpt-oss-120b-medium", "gemini-3.7-pro-high"]
        result = pmr._pick_latest_per_family(ids)
        assert result == {("gemini", "pro", "high"): "gemini-3.7-pro-high"}


class TestDiscoverGeminiModelsJson:
    def test_parses_real_captured_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=REAL_AGY_JSON_STDOUT,
            stderr="Fetching available models...\n",
        )
        monkeypatch.setattr(pmr, "_run", lambda argv, timeout=30.0: fake)
        ids = pmr._discover_gemini_models_json("/bin/agy")
        assert ids == ["gemini-3.7-flash-high", "gemini-3.6-flash-high", "gemini-3.5-flash-high"]

    def test_calls_agy_with_global_flag_before_subcommand(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[list[str]] = []

        def fake_run(argv: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
            captured.append(argv)
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=REAL_AGY_JSON_STDOUT, stderr=""
            )

        monkeypatch.setattr(pmr, "_run", fake_run)
        pmr._discover_gemini_models_json("/bin/agy")
        assert captured == [["/bin/agy", "--output-format", "json", "models"]]

    def test_nonzero_exit_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # e.g. old agy rejecting an unrecognized global flag (confirmed via
        # `--totally-bogus-flag`, 2026-08-20): exit 2, empty stdout.
        fake = subprocess.CompletedProcess(
            args=[],
            returncode=2,
            stdout="",
            stderr="flags provided but not defined: -output-format",
        )
        monkeypatch.setattr(pmr, "_run", lambda argv, timeout=30.0: fake)
        assert pmr._discover_gemini_models_json("/bin/agy") is None

    def test_timeout_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            pmr,
            "_run",
            lambda argv, timeout=30.0: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(cmd="agy", timeout=30)
            ),
        )
        assert pmr._discover_gemini_models_json("/bin/agy") is None

    def test_non_json_stdout_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr="")
        monkeypatch.setattr(pmr, "_run", lambda argv, timeout=30.0: fake)
        assert pmr._discover_gemini_models_json("/bin/agy") is None

    def test_missing_command_data_models_key_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"status":"SUCCESS","response":""}', stderr=""
        )
        monkeypatch.setattr(pmr, "_run", lambda argv, timeout=30.0: fake)
        assert pmr._discover_gemini_models_json("/bin/agy") is None

    def test_empty_models_list_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"command":{"data":{"models":[]}}}', stderr=""
        )
        monkeypatch.setattr(pmr, "_run", lambda argv, timeout=30.0: fake)
        assert pmr._discover_gemini_models_json("/bin/agy") is None


class TestDiscoverGeminiModelsText:
    def test_parses_real_captured_format(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "Fetching available models...\n"
                "gemini-3.7-flash-high\tGemini 3.7 Flash (High)\n"
                "gemini-3.6-flash-high\tGemini 3.6 Flash (High)\n"
            ),
            stderr="",
        )
        monkeypatch.setattr(pmr, "_run", lambda argv, timeout=30.0: fake)
        ids = pmr._discover_gemini_models_text("/bin/agy")
        assert ids == ["gemini-3.7-flash-high", "gemini-3.6-flash-high"]

    def test_nonzero_exit_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="auth required")
        monkeypatch.setattr(pmr, "_run", lambda argv, timeout=30.0: fake)
        assert pmr._discover_gemini_models_text("/bin/agy") is None

    def test_timeout_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            pmr,
            "_run",
            lambda argv, timeout=30.0: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(cmd="agy", timeout=30)
            ),
        )
        assert pmr._discover_gemini_models_text("/bin/agy") is None

    def test_empty_output_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="Fetching...\n", stderr="")
        monkeypatch.setattr(pmr, "_run", lambda argv, timeout=30.0: fake)
        assert pmr._discover_gemini_models_text("/bin/agy") is None


class TestDiscoverGeminiModelsFallback:
    def test_prefers_json_path_when_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            pmr, "_discover_gemini_models_json", lambda binary: ["gemini-3.7-flash-high"]
        )
        monkeypatch.setattr(
            pmr,
            "_discover_gemini_models_text",
            lambda binary: (_ for _ in ()).throw(AssertionError("text path should not run")),
        )
        assert pmr._discover_gemini_models("/bin/agy") == ["gemini-3.7-flash-high"]

    def test_falls_back_to_text_when_json_path_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pmr, "_discover_gemini_models_json", lambda binary: None)
        monkeypatch.setattr(
            pmr, "_discover_gemini_models_text", lambda binary: ["gemini-3.7-flash-high"]
        )
        assert pmr._discover_gemini_models("/bin/agy") == ["gemini-3.7-flash-high"]

    def test_returns_none_when_both_paths_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pmr, "_discover_gemini_models_json", lambda binary: None)
        monkeypatch.setattr(pmr, "_discover_gemini_models_text", lambda binary: None)
        assert pmr._discover_gemini_models("/bin/agy") is None


class TestRefreshGemini:
    def test_no_pin_is_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import agent_takkub.provider_models as provider_models

        monkeypatch.setattr(provider_models, "model_for", lambda name: None)
        outcome = pmr.refresh_provider_model("gemini", "/bin/agy")
        assert outcome.status == pmr.STATUS_NO_PIN

    def test_stale_pin_gets_bumped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import agent_takkub.provider_models as provider_models

        monkeypatch.setattr(provider_models, "model_for", lambda name: "gemini-3.5-flash-medium")
        monkeypatch.setattr(
            pmr,
            "_discover_gemini_models",
            lambda binary: [
                "gemini-3.7-flash-medium",
                "gemini-3.6-flash-medium",
                "gemini-3.5-flash-medium",
            ],
        )
        set_calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            provider_models, "set_model", lambda name, model: set_calls.append((name, model))
        )
        outcome = pmr.refresh_provider_model("gemini", "/bin/agy")
        assert outcome.status == pmr.STATUS_BUMPED
        assert outcome.detail == "gemini-3.5-flash-medium -> gemini-3.7-flash-medium"
        assert set_calls == [("gemini", "gemini-3.7-flash-medium")]

    def test_already_latest_is_up_to_date(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import agent_takkub.provider_models as provider_models

        monkeypatch.setattr(provider_models, "model_for", lambda name: "gemini-3.7-flash-medium")
        monkeypatch.setattr(
            pmr, "_discover_gemini_models", lambda binary: ["gemini-3.7-flash-medium"]
        )
        outcome = pmr.refresh_provider_model("gemini", "/bin/agy")
        assert outcome.status == pmr.STATUS_UP_TO_DATE

    def test_discovery_failure_reported_not_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import agent_takkub.provider_models as provider_models

        monkeypatch.setattr(provider_models, "model_for", lambda name: "gemini-3.5-flash-medium")
        monkeypatch.setattr(pmr, "_discover_gemini_models", lambda binary: None)
        outcome = pmr.refresh_provider_model("gemini", "/bin/agy")
        assert outcome.status == pmr.STATUS_DISCOVERY_FAILED

    def test_pin_not_in_catalog_left_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A pin whose FAMILY (not just exact version) has no match in the
        current catalog at all — e.g. a discontinued tier — is left alone
        rather than guessed at."""
        import agent_takkub.provider_models as provider_models

        monkeypatch.setattr(provider_models, "model_for", lambda name: "gemini-3.1-ultra-medium")
        monkeypatch.setattr(
            pmr, "_discover_gemini_models", lambda binary: ["gemini-3.7-flash-medium"]
        )
        set_calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            provider_models, "set_model", lambda name, model: set_calls.append((name, model))
        )
        outcome = pmr.refresh_provider_model("gemini", "/bin/agy")
        assert outcome.status == pmr.STATUS_UNMATCHED
        assert not set_calls


class TestOtherProvidersAreGaps:
    @pytest.mark.parametrize("name", ["claude", "kimi", "cursor", "opencode"])
    def test_reports_gap_without_touching_subprocess(
        self, name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = []
        monkeypatch.setattr(pmr, "_run", lambda *a, **kw: called.append(1))
        outcome = pmr.refresh_provider_model(name, f"/bin/{name}")
        assert outcome.status == pmr.STATUS_GAP
        assert outcome.detail  # every gap has a real, non-empty reason
        assert not called

    def test_every_gap_provider_has_a_documented_reason(self) -> None:
        for name in ("claude", "kimi", "cursor", "opencode"):
            assert name in pmr.NO_MODEL_DISCOVERY_GAPS

    def test_a_provider_is_never_both_supported_and_a_documented_gap(self) -> None:
        """The two tables answer opposite questions — a name in both would
        make `refresh_provider_model` silently ignore a documented gap (or
        keep claiming a gap for a channel that now exists, the exact drift
        that left codex listed as unsupported after its cache was wired up).
        """
        assert not (set(pmr._DISCOVERY) & set(pmr.NO_MODEL_DISCOVERY_GAPS))

    def test_every_registered_provider_lands_in_exactly_one_table(self) -> None:
        from agent_takkub.provider_spec import PROVIDER_REGISTRY

        covered = set(pmr._DISCOVERY) | set(pmr.NO_MODEL_DISCOVERY_GAPS)
        assert set(PROVIDER_REGISTRY) <= covered


class TestRolePinsAreRefreshedToo:
    """The regression this class exists for: role-models.json is where a
    team's real per-role models live, and the first wave of this feature only
    ever read the provider-level store — so a config with every pin at role
    level reported NO_PIN forever and never bumped anything."""

    @pytest.fixture(autouse=True)
    def _isolate_stores(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        from agent_takkub import provider_models, role_models

        monkeypatch.setattr(role_models, "_PATH", tmp_path / "role-models.json")
        monkeypatch.setattr(provider_models, "model_for", lambda name: None)

    def test_role_pin_is_bumped_even_with_no_provider_level_pin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub import role_models

        role_models.set_model("backend", "gemini", "gemini-3.5-flash-medium")
        monkeypatch.setattr(
            pmr,
            "_discover_gemini_models",
            lambda binary: ["gemini-3.7-flash-medium", "gemini-3.5-flash-medium"],
        )
        outcome = pmr.refresh_provider_model("gemini", "/bin/agy")
        assert outcome.status == pmr.STATUS_BUMPED
        assert outcome.detail == "backend: gemini-3.5-flash-medium -> gemini-3.7-flash-medium"
        assert role_models.model_for("backend", "gemini") == "gemini-3.7-flash-medium"

    def test_bump_preserves_the_roles_effort_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bump goes through the same `role_models.set_model` Settings uses,
        which keeps the sibling effort field — writing the entry any other way
        would silently drop the role's configured effort."""
        from agent_takkub import role_models

        role_models.set_model("backend", "gemini", "gemini-3.5-flash-medium")
        role_models.set_effort("backend", "gemini", "high")
        monkeypatch.setattr(
            pmr, "_discover_gemini_models", lambda binary: ["gemini-3.7-flash-medium"]
        )
        assert pmr.refresh_provider_model("gemini", "/bin/agy").status == pmr.STATUS_BUMPED
        assert role_models.effort_for("backend", "gemini") == "high"

    def test_only_pins_bound_to_this_provider_are_touched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub import role_models

        role_models.set_model("backend", "gemini", "gemini-3.5-flash-medium")
        role_models.set_model("qa", "kimi", "gemini-3.5-flash-medium")  # same id, other CLI
        monkeypatch.setattr(
            pmr, "_discover_gemini_models", lambda binary: ["gemini-3.7-flash-medium"]
        )
        pmr.refresh_provider_model("gemini", "/bin/agy")
        assert role_models.model_for("qa", "kimi") == "gemini-3.5-flash-medium"

    def test_several_stale_pins_all_bump_and_all_appear_in_the_detail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub import role_models

        role_models.set_model("backend", "gemini", "gemini-3.5-flash-medium")
        role_models.set_model("critic", "gemini", "gemini-3.1-pro-low")
        monkeypatch.setattr(
            pmr,
            "_discover_gemini_models",
            lambda binary: ["gemini-3.7-flash-medium", "gemini-3.6-pro-low"],
        )
        outcome = pmr.refresh_provider_model("gemini", "/bin/agy")
        assert outcome.status == pmr.STATUS_BUMPED
        assert "backend: gemini-3.5-flash-medium -> gemini-3.7-flash-medium" in outcome.detail
        assert "critic: gemini-3.1-pro-low -> gemini-3.6-pro-low" in outcome.detail
        assert role_models.model_for("critic", "gemini") == "gemini-3.6-pro-low"

    def test_no_pin_anywhere_still_skips_discovery_entirely(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        monkeypatch.setattr(pmr, "_discover_gemini_models", lambda binary: calls.append(binary))
        assert pmr.refresh_provider_model("gemini", "/bin/agy").status == pmr.STATUS_NO_PIN
        assert not calls


class TestCodexDiscovery:
    """codex ships no models-list subcommand, but its CLI keeps its own
    `models_cache.json` — the source `settings_window._MODELS_BY_PROVIDER`
    already documents reading by hand. The shape below is the real one
    captured from codex 0.149.0 on 2026-08-21."""

    def _write_cache(self, tmp_path, models):
        home = tmp_path / ".codex"
        home.mkdir()
        (home / "models_cache.json").write_text(
            json.dumps({"fetched_at": "2026-08-21T04:29:59Z", "models": models}),
            encoding="utf-8",
        )
        return home

    def _patch_home(self, monkeypatch: pytest.MonkeyPatch, home) -> None:
        import agent_takkub.codex_helper as codex_helper

        monkeypatch.setattr(codex_helper, "codex_home", lambda: home)

    def test_parses_real_cache_shape_newest_version_first(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        home = self._write_cache(
            tmp_path,
            [
                {"slug": "gpt-5.6-sol", "visibility": "list", "priority": 1},
                {"slug": "gpt-5.6-terra", "visibility": "list", "priority": 2},
                {"slug": "gpt-5.5", "visibility": "list", "priority": 7},
            ],
        )
        self._patch_home(monkeypatch, home)
        assert pmr._discover_codex_models("/bin/codex") == [
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.5",
        ]

    def test_hidden_models_never_become_bump_targets(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        home = self._write_cache(
            tmp_path,
            [
                {"slug": "gpt-reserve", "visibility": "hide", "priority": 3},
                {"slug": "codex-auto-review", "visibility": "hide", "priority": 43},
                {"slug": "gpt-5.6-terra", "visibility": "list", "priority": 2},
            ],
        )
        self._patch_home(monkeypatch, home)
        assert pmr._discover_codex_models("/bin/codex") == ["gpt-5.6-terra"]

    def test_version_beats_priority_when_the_two_disagree(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """`priority` ranks prominence, not recency — a newer release listed
        below an older one must still win, or a bump would move a pin
        BACKWARDS."""
        home = self._write_cache(
            tmp_path,
            [
                {"slug": "gpt-5.6-terra", "visibility": "list", "priority": 1},
                {"slug": "gpt-5.7-terra", "visibility": "list", "priority": 9},
            ],
        )
        self._patch_home(monkeypatch, home)
        assert pmr._discover_codex_models("/bin/codex")[0] == "gpt-5.7-terra"

    def test_missing_cache_reports_discovery_failed_not_a_crash(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        import agent_takkub.codex_helper as codex_helper
        from agent_takkub import provider_models, role_models

        monkeypatch.setattr(codex_helper, "codex_home", lambda: tmp_path / "nope")
        monkeypatch.setattr(role_models, "_PATH", tmp_path / "role-models.json")
        monkeypatch.setattr(provider_models, "model_for", lambda name: "gpt-5.6-terra")
        assert pmr._discover_codex_models("/bin/codex") is None
        assert (
            pmr.refresh_provider_model("codex", "/bin/codex").status == pmr.STATUS_DISCOVERY_FAILED
        )

    def test_role_pin_on_codex_bumps_through_the_cache(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from agent_takkub import provider_models, role_models

        home = self._write_cache(
            tmp_path,
            [
                {"slug": "gpt-5.7-terra", "visibility": "list", "priority": 1},
                {"slug": "gpt-5.6-terra", "visibility": "list", "priority": 2},
            ],
        )
        self._patch_home(monkeypatch, home)
        monkeypatch.setattr(role_models, "_PATH", tmp_path / "role-models.json")
        monkeypatch.setattr(provider_models, "model_for", lambda name: None)
        role_models.set_model("backend", "codex", "gpt-5.6-terra")
        outcome = pmr.refresh_provider_model("codex", "/bin/codex")
        assert outcome.status == pmr.STATUS_BUMPED
        assert role_models.model_for("backend", "codex") == "gpt-5.7-terra"
