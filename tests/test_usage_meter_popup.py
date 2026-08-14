from PyQt6.QtCore import QRect, QSize

from agent_takkub.usage_meter import _popup_position


def test_popup_right_aligns_to_meter_without_crossing_right_edge() -> None:
    position = _popup_position(
        QRect(1810, 40, 100, 24),
        QSize(380, 500),
        QRect(0, 0, 1920, 1080),
    )
    assert position.x() == 1530
    assert position.y() == 64
    assert position.x() + 380 <= 1912


def test_popup_clamps_to_left_screen_margin() -> None:
    position = _popup_position(
        QRect(4, 40, 40, 24),
        QSize(380, 500),
        QRect(0, 0, 1920, 1080),
    )
    assert position.x() == 8


def test_popup_flips_above_anchor_when_bottom_would_clip() -> None:
    position = _popup_position(
        QRect(1810, 1010, 100, 24),
        QSize(380, 500),
        QRect(0, 0, 1920, 1080),
    )
    assert position.x() == 1530
    assert position.y() == 510


def test_popup_clamps_when_larger_than_available_height() -> None:
    position = _popup_position(
        QRect(900, 350, 100, 24),
        QSize(380, 900),
        QRect(0, 0, 1920, 720),
    )
    assert position.y() == 8
