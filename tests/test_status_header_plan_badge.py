"""#505 scope addition — the read-only plan badge that replaced the clickable
Pro/Max plan chip, plus dead-code guards for the removed 🏁 End Session button
and plan-chip handlers.

Same Mock-based mixin pattern as test_main_window_status_bar.py's
TestOverageChip — no Qt widget tree needed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock, patch

from agent_takkub.limit_status import LimitWindow, UsageData
from agent_takkub.status_header import StatusHeaderMixin
from agent_takkub.user_actions import UserActionsMixin


def _fake_self(*, limit_store=None, cache=None, busy=True):
    fake = Mock()
    fake._plan_badge = Mock()
    fake._plan_badge_cache = cache if cache is not None else {}
    # busy=True by default so a cache miss can't try to start a QThreadPool
    # worker inside these no-Qt tests.
    fake._plan_badge_probe_busy = busy
    fake._limit_store = limit_store
    return fake


def _usage(plan: str) -> UsageData:
    return UsageData(
        plan=plan,
        windows=[
            LimitWindow(
                name="five_hour",
                utilization=10.0,
                resets_at=datetime(2026, 9, 7, 10, 0, 0, tzinfo=UTC),
            )
        ],
        extra_usage_enabled=False,
    )


class TestPlanBadgeRefresh:
    def test_no_active_project_shows_dash(self) -> None:
        fake = _fake_self()
        with patch("agent_takkub.config.active_project", return_value=(None, None)):
            StatusHeaderMixin._refresh_plan_badge(fake)
        fake._plan_badge.setText.assert_called_once_with("Plan: —")

    def test_plan_from_limit_store_cache(self) -> None:
        store = Mock()
        store.get.return_value = _usage("Max 20x")
        fake = _fake_self(limit_store=store)
        with (
            patch("agent_takkub.config.active_project", return_value=("demo", None)),
            patch("agent_takkub.user_profile.profile_for", return_value="default"),
            patch("agent_takkub.user_profile.config_dir_for", return_value="/fake/cd"),
        ):
            StatusHeaderMixin._refresh_plan_badge(fake)
        fake._plan_badge.setText.assert_called_once_with("Plan: Max 20x")
        # tooltip names the account backing the number
        tooltip = fake._plan_badge.setToolTip.call_args[0][0]
        assert "default" in tooltip

    def test_falls_back_to_probe_cache_when_store_empty(self) -> None:
        store = Mock()
        store.get.return_value = None
        fake = _fake_self(limit_store=store, cache={"/fake/cd": "Pro"})
        with (
            patch("agent_takkub.config.active_project", return_value=("demo", None)),
            patch("agent_takkub.user_profile.profile_for", return_value="office"),
            patch("agent_takkub.user_profile.config_dir_for", return_value="/fake/cd"),
        ):
            StatusHeaderMixin._refresh_plan_badge(fake)
        fake._plan_badge.setText.assert_called_once_with("Plan: Pro")

    def test_unknown_when_nothing_cached(self) -> None:
        fake = _fake_self(limit_store=None)
        with (
            patch("agent_takkub.config.active_project", return_value=("demo", None)),
            patch("agent_takkub.user_profile.profile_for", return_value="default"),
            patch("agent_takkub.user_profile.config_dir_for", return_value="/fake/cd"),
        ):
            StatusHeaderMixin._refresh_plan_badge(fake)
        fake._plan_badge.setText.assert_called_once_with("Plan: ไม่ทราบ")

    def test_no_op_when_badge_was_never_built(self) -> None:
        StatusHeaderMixin._refresh_plan_badge(Mock(spec=[]))  # must not raise

    def test_probe_result_lands_in_cache(self) -> None:
        fake = _fake_self()
        fake._refresh_plan_badge = Mock()
        StatusHeaderMixin._on_plan_probe_done(fake, "/fake/cd", "Max 5x")
        assert fake._plan_badge_cache["/fake/cd"] == "Max 5x"
        assert fake._plan_badge_probe_busy is False
        fake._refresh_plan_badge.assert_called_once()


class TestRemovedControlsStayRemoved:
    """Owner directive 2026-09-07: removing UI means removing the whole dead
    chain — these guards keep the handlers/styles from creeping back."""

    def test_plan_chip_helpers_gone(self) -> None:
        for name in ("_plan_chip_label", "_plan_chip_style", "_plan_chip_tooltip"):
            assert not hasattr(StatusHeaderMixin, name), name

    def test_end_session_ui_chain_gone(self) -> None:
        assert not hasattr(StatusHeaderMixin, "_danger_button_style")
        for name in ("_on_end_session_clicked", "_show_end_session_summary"):
            assert not hasattr(UserActionsMixin, name), name

    def test_plan_chip_handlers_gone(self) -> None:
        for name in ("_on_plan_chip_clicked", "_on_plan_tier_changed"):
            assert not hasattr(UserActionsMixin, name), name

    def test_cli_end_session_survives(self) -> None:
        """`takkub end-session` stays — only the status-bar button went."""
        from agent_takkub import cli

        assert hasattr(cli, "cmd_end_session")

    def test_exec_mode_and_auto_resume_chip_stubs_gone(self) -> None:
        """#512 — these chips were already gone (no widget ever wired to
        them, no test referenced them — verified by grep before deleting);
        this issue's UI pass retired the orphaned stub handlers too, plus
        the style helper's own stale docstring reference to one of them."""
        for name in (
            "_on_exec_mode_chip_clicked",
            "_on_exec_mode_changed",
            "_on_auto_resume_chip_clicked",
            "_on_auto_resume_changed",
        ):
            assert not hasattr(UserActionsMixin, name), name
        for name in ("_exec_mode_chip_style", "_auto_resume_chip_style"):
            assert not hasattr(StatusHeaderMixin, name), name
