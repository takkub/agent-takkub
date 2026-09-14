"""#591: pure-logic tests for pane_provider_label — no Qt, no spawn."""

from agent_takkub.pane_provider_label import (
    resolve_provider_model_display,
    shorten_model_name,
    tab_label_with_model,
)


class TestShortenModelName:
    def test_strips_claude_prefix(self) -> None:
        assert shorten_model_name("claude-sonnet-5") == "sonnet-5"

    def test_strips_date_suffix(self) -> None:
        assert shorten_model_name("claude-sonnet-4-6-20251015") == "sonnet-4-6"

    def test_strips_1m_suffix(self) -> None:
        assert shorten_model_name("claude-opus-5 [1m]") == "opus-5"

    def test_non_claude_id_passes_through(self) -> None:
        assert shorten_model_name("gpt-5.6-sol") == "gpt-5.6-sol"

    def test_empty_or_none(self) -> None:
        assert shorten_model_name(None) == ""
        assert shorten_model_name("") == ""


class TestResolveProviderModelDisplay:
    def test_no_provider_returns_none(self) -> None:
        assert resolve_provider_model_display(provider=None, spawn_model=None) is None

    def test_shell_pane_returns_none(self) -> None:
        assert resolve_provider_model_display(provider="shell", spawn_model=None) is None

    def test_live_model_wins_over_spawn_model(self) -> None:
        d = resolve_provider_model_display(
            provider="claude",
            spawn_model="claude-sonnet-5",
            live_model="claude-opus-5",
        )
        assert d is not None
        assert "opus-5" in d.short_text
        assert d.mismatch is True
        assert "⚠" in d.short_text
        assert "ตาม CLI จริง" in d.tooltip

    def test_no_live_model_falls_back_to_spawn_model(self) -> None:
        d = resolve_provider_model_display(
            provider="codex", spawn_model="gpt-5.6-sol", live_model=None
        )
        assert d is not None
        assert d.short_text == "codex · gpt-5.6-sol"
        assert d.mismatch is False
        assert "ตามที่ตั้งตอนเปิด" in d.tooltip

    def test_explicit_override_source_line(self) -> None:
        d = resolve_provider_model_display(
            provider="claude",
            spawn_model="claude-haiku-4-5",
            spawn_explicit=True,
        )
        assert d is not None
        assert "--model ที่ระบุ" in d.tooltip

    def test_no_model_at_all_shows_default_marker(self) -> None:
        d = resolve_provider_model_display(provider="claude", spawn_model=None)
        assert d is not None
        assert d.short_text == "claude"
        assert "(ค่าเริ่มต้น)" in d.tooltip

    def test_provider_that_never_reports_live_says_so(self) -> None:
        d = resolve_provider_model_display(
            provider="opencode", spawn_model="some-model", live_model=None
        )
        assert d is not None
        assert "ไม่รายงาน model" in d.tooltip

    def test_matching_live_and_spawn_model_is_not_a_mismatch(self) -> None:
        d = resolve_provider_model_display(
            provider="claude",
            spawn_model="claude-sonnet-5",
            live_model="claude-sonnet-5",
        )
        assert d is not None
        assert d.mismatch is False
        assert "⚠" not in d.short_text

    def test_effort_included_in_tooltip(self) -> None:
        d = resolve_provider_model_display(
            provider="claude", spawn_model="claude-opus-5", spawn_effort="high"
        )
        assert d is not None
        assert "effort: high" in d.tooltip

    def test_bare_alias_spawn_model_matches_its_resolved_live_id(self) -> None:
        """#591 follow-up: Settings stores the tier alias ("opus"), the CLI
        reports the concrete id it resolved to ("claude-opus-5") — that is
        not drift, so no ⚠."""
        for alias, live in (
            ("opus", "claude-opus-5"),
            ("sonnet", "claude-sonnet-5"),
            ("haiku", "claude-haiku-4-5"),
        ):
            d = resolve_provider_model_display(
                provider="claude", spawn_model=alias, live_model=live
            )
            assert d is not None
            assert d.mismatch is False, f"{alias} vs {live} should not mismatch"
            assert "⚠" not in d.short_text

    def test_different_family_still_flags_mismatch(self) -> None:
        d = resolve_provider_model_display(
            provider="claude", spawn_model="sonnet", live_model="claude-haiku-4-5"
        )
        assert d is not None
        assert d.mismatch is True
        assert "⚠" in d.short_text

    def test_different_version_of_same_family_still_flags_mismatch(self) -> None:
        d = resolve_provider_model_display(
            provider="claude",
            spawn_model="claude-sonnet-5",
            live_model="claude-sonnet-4-6",
        )
        assert d is not None
        assert d.mismatch is True
        assert "⚠" in d.short_text


class TestTabLabelWithModel:
    def test_none_display_keeps_base_label(self) -> None:
        assert tab_label_with_model("Backend", None) == "Backend"

    def test_appends_short_model(self) -> None:
        d = resolve_provider_model_display(provider="claude", spawn_model="claude-sonnet-5")
        assert tab_label_with_model("Backend", d) == "Backend · sonnet-5"

    def test_truncates_long_result(self) -> None:
        d = resolve_provider_model_display(
            provider="codex", spawn_model="gpt-5.6-experimental-solstice"
        )
        text = tab_label_with_model("Backend", d, max_len=24)
        assert len(text) <= 24
        assert text.startswith("Backend · ")
        assert text.endswith("…")
