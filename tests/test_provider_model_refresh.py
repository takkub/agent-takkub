"""Tests for provider_model_refresh.py — boot-time model-catalog bump
(user directive 2026-08-20: "gemini ออก 3.7 แล้วแต่ cockpit ยังรู้จักรุ่นเก่า").

No real subprocess ever runs: `_run` is monkeypatched or `_discover_gemini_models`
is patched directly in every test.
"""

from __future__ import annotations

import subprocess

import pytest

import agent_takkub.provider_model_refresh as pmr


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


class TestDiscoverGeminiModels:
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
        ids = pmr._discover_gemini_models("/bin/agy")
        assert ids == ["gemini-3.7-flash-high", "gemini-3.6-flash-high"]

    def test_nonzero_exit_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="auth required")
        monkeypatch.setattr(pmr, "_run", lambda argv, timeout=30.0: fake)
        assert pmr._discover_gemini_models("/bin/agy") is None

    def test_timeout_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            pmr,
            "_run",
            lambda argv, timeout=30.0: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(cmd="agy", timeout=30)
            ),
        )
        assert pmr._discover_gemini_models("/bin/agy") is None

    def test_empty_output_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="Fetching...\n", stderr="")
        monkeypatch.setattr(pmr, "_run", lambda argv, timeout=30.0: fake)
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
