"""Tests for system_baseline — the shared system-core version bar.

The manifest is the single source of truth every machine's `takkub doctor`
grades against, so the version-parsing + tiered comparison must be rock solid:
a false "below minimum" nags a healthy machine, a false "ok" lets a drifted
machine hide. See src/agent_takkub/system_baseline.py.
"""

from __future__ import annotations

import pytest

from agent_takkub import system_baseline as bl

# ---------------------------------------------------------------------------
# parse_version — every shape the cockpit's --version probes emit
# ---------------------------------------------------------------------------


class TestParseVersion:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Python 3.11.8", (3, 11, 8)),
            ("v22.22.1", (22, 22, 1)),
            ("10.9.4", (10, 9, 4)),
            ("2.1.197 (Claude Code)", (2, 1, 197)),
            ("3.11", (3, 11)),
            ("18", (18,)),
        ],
    )
    def test_extracts_version(self, text: str, expected: tuple[int, ...]) -> None:
        assert bl.parse_version(text) == expected

    @pytest.mark.parametrize("text", ["", None, "no numbers here", "(unknown)"])
    def test_unparseable_returns_none(self, text: str | None) -> None:
        assert bl.parse_version(text) is None


# ---------------------------------------------------------------------------
# meets — zero-padded component comparison
# ---------------------------------------------------------------------------


class TestMeets:
    def test_equal_meets(self) -> None:
        assert bl.meets((3, 11), (3, 11)) is True

    def test_higher_meets(self) -> None:
        assert bl.meets((3, 12, 0), (3, 11)) is True

    def test_lower_fails(self) -> None:
        assert bl.meets((3, 10, 9), (3, 11)) is False

    def test_short_installed_padded_not_penalised(self) -> None:
        # (18,) must satisfy (18, 0, 0) — a bare major shouldn't sort below it.
        assert bl.meets((18,), (18, 0, 0)) is True

    def test_patch_higher_meets_minor_bar(self) -> None:
        assert bl.meets((22, 22, 1), (20, 0)) is True


# ---------------------------------------------------------------------------
# evaluate — the three tiers + unknown
# ---------------------------------------------------------------------------


class TestEvaluate:
    def test_ok_when_at_or_above_recommended(self) -> None:
        # node recommended is 20.0; v22 clears it.
        res = bl.evaluate("node", "v22.22.1")
        assert res.level == bl.LEVEL_OK
        assert res.installed == (22, 22, 1)

    def test_recommend_when_between_min_and_recommended(self) -> None:
        # node minimum 18.0, recommended 20.0 → v18.19 is a WARN nudge.
        res = bl.evaluate("node", "v18.19.0")
        assert res.level == bl.LEVEL_RECOMMEND

    def test_below_min_when_under_minimum(self) -> None:
        res = bl.evaluate("node", "v16.20.0")
        assert res.level == bl.LEVEL_BELOW_MIN

    def test_unknown_when_unparseable(self) -> None:
        res = bl.evaluate("node", "totally not a version")
        assert res.level == bl.LEVEL_UNKNOWN
        assert res.installed is None
        assert res.installed_str == "(unknown)"

    def test_python_311_is_ok(self) -> None:
        # This machine / the baseline floor: 3.11 meets both min and recommended.
        assert bl.evaluate("python", "3.11.8").level == bl.LEVEL_OK

    def test_claude_current_is_ok(self) -> None:
        assert bl.evaluate("claude", "2.1.197 (Claude Code)").level == bl.LEVEL_OK

    def test_claude_old_is_below_min(self) -> None:
        assert bl.evaluate("claude", "1.9.0").level == bl.LEVEL_BELOW_MIN


# ---------------------------------------------------------------------------
# manifest shape + note
# ---------------------------------------------------------------------------


class TestManifest:
    def test_all_core_tools_indexed(self) -> None:
        assert set(bl.TOOL_BY_KEY) == {"python", "node", "npx", "claude"}

    def test_recommended_never_below_minimum(self) -> None:
        # A recommended tier below its own minimum would be nonsensical.
        for tool in bl.CORE_TOOLS:
            assert bl.meets(tool.recommended, tool.minimum)

    def test_every_tool_has_upgrade_hint(self) -> None:
        for tool in bl.CORE_TOOLS:
            assert tool.upgrade_hint.strip()

    def test_baseline_note_shows_both_tiers(self) -> None:
        note = bl.baseline_note(bl.TOOL_BY_KEY["node"])
        assert "min 18.0" in note
        assert "rec 20.0" in note
