"""Tests for provider_model_refresh.py — boot-time model-catalog bump
(user directive 2026-08-20: "gemini ออก 3.7 แล้วแต่ cockpit ยังรู้จักรุ่นเก่า").

No real subprocess ever runs: `_run` is monkeypatched or `_discover_gemini_models`
is patched directly in every test.

The JSON-path fixtures below are the ACTUAL stdout captured from a real
`agy --output-format json models` run (agy 1.1.15, 2026-08-20, issue #317)
— not a guessed schema.
"""

from __future__ import annotations

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
        assert pmr._family_key_gemini(model_id) == expected

    def test_no_version_token_returns_none(self) -> None:
        assert pmr._family_key_gemini("gpt-oss-120b-medium") is None

    def test_different_tiers_are_different_families(self) -> None:
        assert pmr._family_key_gemini("gemini-3.7-flash-high") != pmr._family_key_gemini(
            "gemini-3.7-flash-low"
        )


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
    @pytest.mark.parametrize("name", ["claude", "codex", "kimi", "cursor", "opencode"])
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
        for name in ("claude", "codex", "kimi", "cursor", "opencode"):
            assert name in pmr.NO_MODEL_DISCOVERY_GAPS
