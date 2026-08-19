"""Tests for the shared path-segment sanitizer (`path_safe.safe_segment`, #294).

Guards the three failure modes proven in the issue: two distinct non-ASCII
names of equal length collapsing to the identical underscore string, Windows
reserved device names passing through unguarded, and unbounded segment
length risking MAX_PATH.
"""

from __future__ import annotations

import re

import pytest

from agent_takkub.path_safe import safe_segment

_SAFE_CHARS = re.compile(r"^[A-Za-z0-9._-]+$")


class TestBackwardCompat:
    def test_plain_ascii_name_unchanged(self) -> None:
        assert safe_segment("myproject") == "myproject"
        assert safe_segment("backend") == "backend"

    def test_domain_style_dots_preserved(self) -> None:
        assert safe_segment("www.abc.com") == "www.abc.com"

    def test_hyphen_and_underscore_preserved(self) -> None:
        assert safe_segment("my-proj_v2") == "my-proj_v2"

    def test_empty_name_falls_back_to_default(self) -> None:
        assert safe_segment("") == "default"
        assert safe_segment("", default="fallback") == "fallback"


class TestThaiCollisionFix:
    def test_two_equal_length_thai_names_get_distinct_segments(self) -> None:
        a = safe_segment("โปรเจกต์ไทย")
        b = safe_segment("ระบบขายของ")
        assert a != b

    def test_thai_segment_is_filesystem_safe(self) -> None:
        result = safe_segment("โปรเจกต์ไทย")
        assert _SAFE_CHARS.match(result)

    def test_same_name_is_deterministic(self) -> None:
        assert safe_segment("โปรเจกต์ไทย") == safe_segment("โปรเจกต์ไทย")

    def test_many_thai_and_mixed_names_never_collide(self) -> None:
        names = [
            "โปรเจกต์ไทย",
            "ระบบขายของ",
            "ทดสอบ",
            "ทดสอบ2",
            "project-ไทย",
            "ไทย-project",
            "โครงการ A",
            "โครงการ B",
        ]
        segments = [safe_segment(n) for n in names]
        assert len(segments) == len(set(segments))


class TestWindowsReservedNames:
    @pytest.mark.parametrize(
        "reserved",
        ["CON", "NUL", "PRN", "AUX", "COM1", "COM9", "LPT1", "LPT9", "con", "Nul"],
    )
    def test_reserved_name_gets_suffixed(self, reserved: str) -> None:
        result = safe_segment(reserved)
        assert result != reserved
        assert result.rstrip("_").upper() == reserved.upper()

    def test_reserved_basename_stays_reserved_once_extension_is_appended(self) -> None:
        # role_memory_path appends ".md" AFTER safe_segment, so the guard must
        # fire on the bare segment, not leave a still-reserved "CON.md".
        filename = f"{safe_segment('CON')}.md"
        assert filename.split(".", 1)[0].upper() != "CON"

    def test_non_reserved_name_unaffected(self) -> None:
        assert safe_segment("CONFIG") == "CONFIG"
        assert safe_segment("CONNECT") == "CONNECT"


class TestLengthCap:
    def test_long_ascii_name_capped_at_64(self) -> None:
        result = safe_segment("a" * 300)
        assert len(result) <= 64

    def test_two_long_names_sharing_a_prefix_stay_distinct(self) -> None:
        a = safe_segment("a" * 300 + "-one")
        b = safe_segment("a" * 300 + "-two")
        assert a != b
        assert len(a) <= 64
        assert len(b) <= 64

    def test_short_name_uncapped(self) -> None:
        assert safe_segment("short") == "short"


class TestTraversalSafety:
    @pytest.mark.parametrize("evil", ["..", "../../etc", "a..b", "..."])
    def test_no_dotdot_in_result(self, evil: str) -> None:
        result = safe_segment(evil)
        assert ".." not in result

    def test_result_is_always_a_single_safe_segment(self) -> None:
        for evil in ("..", "../../etc", "/etc/passwd", "a/b\\c", "\x00null"):
            result = safe_segment(evil)
            assert _SAFE_CHARS.match(result)
            assert "/" not in result
            assert "\\" not in result
