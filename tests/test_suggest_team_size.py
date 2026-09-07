"""#512 item 5: routing_planner.suggest_team_size() auto-preset advisory."""

from __future__ import annotations

from agent_takkub.routing_planner import suggest_team_size


def test_typo_fix_suggests_solo_lead():
    preset, _ = suggest_team_size("README มีตัวสะกดผิด")
    assert preset == "solo-lead"


def test_multi_role_feature_suggests_full():
    preset, reason = suggest_team_size("ทำหน้า login ใหม่ พร้อม API")
    assert preset == "full"
    assert "frontend" in reason and "backend" in reason


def test_single_role_bug_fix_suggests_solo_lead():
    preset, _ = suggest_team_size("แก้ bug ปุ่ม login ไม่ทำงาน")
    assert preset == "solo-lead"


def test_explain_system_suggests_solo_lead():
    preset, _ = suggest_team_size("อธิบายโค้ดตรงนี้หน่อย")
    assert preset == "solo-lead"


def test_never_suggests_custom_or_auto():
    for msg in ["README มีตัวสะกดผิด", "ทำหน้า login ใหม่ พร้อม API", "แก้ bug ปุ่ม login ไม่ทำงาน"]:
        preset, _ = suggest_team_size(msg)
        assert preset in ("solo-lead", "pair", "full")
