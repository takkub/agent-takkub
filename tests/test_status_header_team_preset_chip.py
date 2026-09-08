"""#512 — the "ทีม: <preset>" status-bar chip that took the space #505
freed by making the plan badge read-only.

Same Mock-based mixin pattern as test_status_header_plan_badge.py — no Qt
widget tree needed for `_refresh_team_preset_chip` (pure state -> label).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from agent_takkub import team_preset
from agent_takkub.status_header import StatusHeaderMixin


@pytest.fixture(autouse=True)
def _isolate_team_preset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(team_preset, "_BASE_DIR", tmp_path)
    yield


def _fake_self() -> Mock:
    fake = Mock()
    fake._chip_team_preset = Mock()
    return fake


class TestTeamPresetChipRefresh:
    def test_no_active_project_shows_dash(self) -> None:
        fake = _fake_self()
        with patch("agent_takkub.config.active_project", return_value=(None, None)):
            StatusHeaderMixin._refresh_team_preset_chip(fake)
        fake._chip_team_preset.setText.assert_called_once_with("ตั้งค่า: —")

    def test_default_project_shows_auto_label(self) -> None:
        fake = _fake_self()
        with patch("agent_takkub.config.active_project", return_value=("demo", None)):
            StatusHeaderMixin._refresh_team_preset_chip(fake)
        fake._chip_team_preset.setText.assert_called_once_with(f"ตั้งค่า: {team_preset.label('auto')}")

    def test_standing_preset_reflected(self) -> None:
        team_preset.set_current("full", "demo")
        fake = _fake_self()
        with patch("agent_takkub.config.active_project", return_value=("demo", None)):
            StatusHeaderMixin._refresh_team_preset_chip(fake)
        fake._chip_team_preset.setText.assert_called_once_with("ตั้งค่า: ทีมเต็ม")

    def test_active_override_wins_over_standing_in_the_chip_text(self) -> None:
        team_preset.set_current("full", "demo")
        team_preset.set_override("solo-lead", "demo")
        fake = _fake_self()
        with patch("agent_takkub.config.active_project", return_value=("demo", None)):
            StatusHeaderMixin._refresh_team_preset_chip(fake)
        fake._chip_team_preset.setText.assert_called_once_with("ตั้งค่า: ทำเอง")
        tooltip = fake._chip_team_preset.setToolTip.call_args[0][0]
        assert "override" in tooltip
        assert "ทีมเต็ม" in tooltip  # names the standing preset it's overriding

    def test_no_op_when_chip_was_never_built(self) -> None:
        StatusHeaderMixin._refresh_team_preset_chip(Mock(spec=[]))  # must not raise


class TestTeamPresetChipStyle:
    def test_style_uses_gold_chip_tokens_not_hardcoded_colors(self) -> None:
        css = StatusHeaderMixin._team_preset_chip_style()
        from agent_takkub import cockpit_theme

        assert cockpit_theme.GOLD_CHIP_BG in css
        assert cockpit_theme.GOLD_CHIP_BORDER in css

    def test_chip_text_never_carries_a_tofu_prone_glyph(self) -> None:
        """2026-07-24 design review #4: IBM Plex Sans/Mono ship neither the
        dropdown triangle ("▾"/"▸") nor the bullet radio glyphs ("●"/"○") —
        every prior chip that used one had to be swapped for a painted
        `color_dot`/plain text. Guard the fix, not just the current text."""
        fake = _fake_self()
        with patch("agent_takkub.config.active_project", return_value=("demo", None)):
            StatusHeaderMixin._refresh_team_preset_chip(fake)
        text = fake._chip_team_preset.setText.call_args[0][0]
        for glyph in ("▾", "▸", "●", "○"):  # ▾ ▸ ● ○
            assert glyph not in text
