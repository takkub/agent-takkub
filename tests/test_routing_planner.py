"""Tests for routing_planner.classify().

All rules are sourced from CLAUDE.md §Auto-routing (propose-then-fire).
Coverage:
  - Actionable detector (Thai + English verbs)
  - Routing decision table (all roles + cross-check rules)
  - Confirm handling (fire / abort / edit / ambiguous)
  - Auto-fire exceptions (explicit role, one-shot codex/gemini)
  - Edge cases (case insensitivity, whitespace, mixed messages)
"""

from __future__ import annotations

from agent_takkub.routing_planner import ActionKind, RoutingAction, classify

# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _propose(msg, **kw) -> RoutingAction:
    return classify(msg, **kw)


def _fire(msg, **kw) -> RoutingAction:
    return classify(msg, **kw)


# ─────────────────────────────────────────────────────────────────────
# Actionable detector
# ─────────────────────────────────────────────────────────────────────


class TestActionableDetector:
    def test_actionable_thai_add(self):
        result = classify("เพิ่ม login form")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "frontend"

    def test_actionable_thai_create(self):
        result = classify("สร้าง dashboard component")
        assert result.kind == ActionKind.PROPOSE

    def test_actionable_thai_fix(self):
        result = classify("แก้ bug ใน API")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "backend"

    def test_actionable_english_implement(self):
        result = classify("implement /auth/logout endpoint")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "backend"

    def test_actionable_english_build(self):
        result = classify("build the user registration form")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "frontend"

    def test_actionable_english_fix(self):
        result = classify("fix the login API handler")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "backend"

    def test_actionable_deploy_devops(self):
        result = classify("deploy the app with docker")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "devops"

    def test_actionable_test_routes_to_qa(self):
        result = classify("test the login flow")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "qa"

    def test_floating_imperative_lorg_du(self):
        """'ลอง X ดู' → propose (default actionable per spec)."""
        result = classify("ลอง dark mode ดู")
        assert result.kind == ActionKind.PROPOSE

    def test_extra_whitespace_tolerated(self):
        result = classify("  implement   /auth/login   endpoint  ")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "backend"

    def test_case_insensitive_english(self):
        result = classify("IMPLEMENT /auth/logout ENDPOINT")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "backend"


# ─────────────────────────────────────────────────────────────────────
# Informational detector
# ─────────────────────────────────────────────────────────────────────


class TestInformationalDetector:
    def test_thai_how_does_it_work(self):
        result = classify("X ทำงานยังไง?")
        assert result.kind == ActionKind.INFORMATIONAL

    def test_thai_explain(self):
        result = classify("อธิบาย JWT ให้หน่อย")
        assert result.kind == ActionKind.INFORMATIONAL

    def test_thai_why(self):
        result = classify("ทำไม backend ช้า?")
        assert result.kind == ActionKind.INFORMATIONAL

    def test_thai_summarise(self):
        result = classify("สรุป session ที่ผ่านมา")
        assert result.kind == ActionKind.INFORMATIONAL

    def test_english_what(self):
        result = classify("what does the auth middleware do?")
        assert result.kind == ActionKind.INFORMATIONAL

    def test_english_explain(self):
        result = classify("explain the refresh token flow")
        assert result.kind == ActionKind.INFORMATIONAL

    def test_english_why(self):
        result = classify("why is the API returning 403?")
        assert result.kind == ActionKind.INFORMATIONAL

    def test_question_mark_alone(self):
        result = classify("session ใช้ยังไง?")
        assert result.kind == ActionKind.INFORMATIONAL


# ─────────────────────────────────────────────────────────────────────
# Mixed messages
# ─────────────────────────────────────────────────────────────────────


class TestMixedMessages:
    def test_explain_then_fix(self):
        """'X ทำงานยังไง แล้วช่วย fix หน่อย' → PROPOSE with mixed=True."""
        result = classify("auth ทำงานยังไง แล้วช่วย fix bug หน่อย")
        assert result.kind == ActionKind.PROPOSE
        assert result.mixed is True

    def test_why_then_implement(self):
        result = classify("why is it broken? fix the endpoint")
        assert result.kind == ActionKind.PROPOSE
        assert result.mixed is True


# ─────────────────────────────────────────────────────────────────────
# Auto-fire exceptions: explicit role
# ─────────────────────────────────────────────────────────────────────


class TestExplicitRole:
    def test_explicit_backend(self):
        result = classify("ให้ backend ทำ /auth/login endpoint")
        assert result.kind == ActionKind.FIRE_ASSIGN
        assert result.role == "backend"

    def test_explicit_frontend(self):
        result = classify("ให้ frontend สร้าง login form")
        assert result.kind == ActionKind.FIRE_ASSIGN
        assert result.role == "frontend"

    def test_explicit_qa(self):
        result = classify("ให้ qa test หน้า login")
        assert result.kind == ActionKind.FIRE_ASSIGN
        assert result.role == "qa"

    def test_explicit_devops(self):
        result = classify("ให้ devops deploy docker")
        assert result.kind == ActionKind.FIRE_ASSIGN
        assert result.role == "devops"

    def test_explicit_reviewer(self):
        result = classify("ให้ reviewer review โค้ด auth")
        assert result.kind == ActionKind.FIRE_ASSIGN
        assert result.role == "reviewer"


# ─────────────────────────────────────────────────────────────────────
# Auto-fire exceptions: one-shot codex/gemini
# ─────────────────────────────────────────────────────────────────────


class TestOneShot:
    def test_ask_codex(self):
        result = classify("ถาม codex ว่า edge cases ของ JWT blacklist คืออะไร")
        assert result.kind == ActionKind.FIRE_ONESHOT
        assert result.role == "codex"

    def test_ask_gemini(self):
        result = classify("ถาม gemini ว่า rollout strategy ที่ดีคืออะไร")
        assert result.kind == ActionKind.FIRE_ONESHOT
        assert result.role == "gemini"

    def test_codex_review(self):
        result = classify("ขอ codex review approach นี้")
        assert result.kind == ActionKind.FIRE_ONESHOT
        assert result.role == "codex"

    def test_gemini_check(self):
        result = classify("ให้ gemini ดู plan นี้")
        assert result.kind == ActionKind.FIRE_ONESHOT
        assert result.role == "gemini"


# ─────────────────────────────────────────────────────────────────────
# Routing decision table
# ─────────────────────────────────────────────────────────────────────


class TestRoutingTable:
    def test_frontend_ui_keyword(self):
        result = classify("add a modal component")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "frontend"

    def test_frontend_form(self):
        result = classify("create the signup form")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "frontend"

    def test_frontend_css(self):
        result = classify("fix the CSS for the sidebar")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "frontend"

    def test_backend_endpoint(self):
        result = classify("implement the /users endpoint")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "backend"

    def test_backend_schema(self):
        result = classify("add schema for users table")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "backend"

    def test_backend_db(self):
        result = classify("write a db query for orders")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "backend"

    def test_mobile(self):
        result = classify("implement login screen for iOS")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "mobile"

    def test_devops_docker(self):
        result = classify("setup docker compose for the project")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "devops"

    def test_devops_ci(self):
        result = classify("add CI pipeline for tests")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "devops"

    def test_qa(self):
        result = classify("write e2e tests for the login flow")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "qa"

    def test_reviewer(self):
        result = classify("do a code review for auth PR")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "reviewer"

    def test_refactor_adds_codex_cross_check(self):
        """Refactor → primary role + codex cross-check (per spec rule of thumb)."""
        result = classify("refactor the auth module")
        assert result.kind == ActionKind.PROPOSE
        assert result.cross_check is not None
        assert "codex" in result.cross_check

    def test_rename_adds_codex_cross_check(self):
        result = classify("rename UserService to AuthService")
        assert result.kind == ActionKind.PROPOSE
        assert result.cross_check is not None
        assert "codex" in result.cross_check

    def test_rollout_routes_to_gemini(self):
        """'rollout/strategy/phase' → gemini."""
        result = classify("create a rollout plan for the new auth system")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "gemini"

    def test_strategy_routes_to_gemini(self):
        result = classify("build a deployment strategy for v2")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "gemini"

    def test_large_feature_parallel_roles(self):
        """'add login page with API' → frontend + backend parallel."""
        result = classify("add login page with API endpoint")
        assert result.kind == ActionKind.PROPOSE
        assert result.roles is not None
        assert "frontend" in result.roles
        assert "backend" in result.roles

    def test_large_feature_ui_and_api(self):
        result = classify("implement the dashboard UI and its REST API")
        assert result.kind == ActionKind.PROPOSE
        assert result.roles is not None
        assert "frontend" in result.roles
        assert "backend" in result.roles


# ─────────────────────────────────────────────────────────────────────
# Confirm handling (requires pending_proposal context)
# ─────────────────────────────────────────────────────────────────────

_PENDING = {"pending_proposal": True}


class TestConfirmHandling:
    def test_ok_fires(self):
        result = classify("ok", context=_PENDING)
        assert result.kind == ActionKind.FIRE_ASSIGN

    def test_luy_fires(self):
        result = classify("ลุย", context=_PENDING)
        assert result.kind == ActionKind.FIRE_ASSIGN

    def test_luyloey_fires(self):
        result = classify("ลุยเลย", context=_PENDING)
        assert result.kind == ActionKind.FIRE_ASSIGN

    def test_go_fires(self):
        result = classify("go", context=_PENDING)
        assert result.kind == ActionKind.FIRE_ASSIGN

    def test_aoloy_fires(self):
        result = classify("เอาเลย", context=_PENDING)
        assert result.kind == ActionKind.FIRE_ASSIGN

    def test_ok_with_punctuation_fires(self):
        result = classify("ok!", context=_PENDING)
        assert result.kind == ActionKind.FIRE_ASSIGN

    def test_abort_mai_ao(self):
        result = classify("ไม่เอา", context=_PENDING)
        assert result.kind == ActionKind.INFORMATIONAL

    def test_abort_stop(self):
        result = classify("stop", context=_PENDING)
        assert result.kind == ActionKind.INFORMATIONAL

    def test_abort_yud(self):
        result = classify("หยุด", context=_PENDING)
        assert result.kind == ActionKind.INFORMATIONAL

    def test_ambiguous_aoao(self):
        """'เออๆ' → ASK_CLARIFY (cannot assume confirm)."""
        result = classify("เออๆ", context=_PENDING)
        assert result.kind == ActionKind.ASK_CLARIFY

    def test_ambiguous_ok_but(self):
        """'ok แต่...' → ASK_CLARIFY."""
        result = classify("ok แต่ใช้ qa แทน codex", context=_PENDING)
        assert result.kind == ActionKind.ASK_CLARIFY

    def test_edit_only_reproposes(self):
        """'ใช้ qa แทน codex' (no fire) → ASK_CLARIFY (re-propose)."""
        result = classify("ใช้ qa แทน codex", context=_PENDING)
        assert result.kind == ActionKind.ASK_CLARIFY

    def test_edit_then_fire(self):
        """'แก้ X แล้วลุยเลย' → FIRE_ASSIGN (edit + fire)."""
        result = classify("แก้เป็น gemini แล้วลุยเลย", context=_PENDING)
        assert result.kind == ActionKind.FIRE_ASSIGN

    def test_confirm_without_pending_context_is_not_fire(self):
        """'ok' without pending_proposal should NOT become FIRE_ASSIGN."""
        result = classify("ok")
        # Without pending proposal, "ok" alone has no actionable verb
        # so it should be INFORMATIONAL (not FIRE_ASSIGN)
        assert result.kind != ActionKind.FIRE_ASSIGN


# ─────────────────────────────────────────────────────────────────────
# Thai UI keywords (หน้าจอ, หน้า, ปุ่ม)
# ─────────────────────────────────────────────────────────────────────


class TestThaiUIKeywords:
    def test_thai_screen_routes_frontend(self):
        """'ทำหน้าจอ login' should route to frontend, not backend."""
        result = classify("ทำหน้าจอ login")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "frontend"

    def test_thai_button_routes_frontend(self):
        """'แก้ปุ่ม submit' should route to frontend."""
        result = classify("แก้ปุ่ม submit")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "frontend"

    def test_thai_page_routes_frontend(self):
        """'ทำหน้า profile' should route to frontend."""
        result = classify("ทำหน้า profile")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "frontend"


# ─────────────────────────────────────────────────────────────────────
# Thai API keywords (ฐานข้อมูล, หลังบ้าน)
# ─────────────────────────────────────────────────────────────────────


class TestThaiAPIKeywords:
    def test_thai_database_routes_backend(self):
        """'แก้ฐานข้อมูล' should route to backend."""
        result = classify("แก้ฐานข้อมูล")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "backend"

    def test_thai_server_side_routes_backend(self):
        """'แก้หลังบ้าน' should route to backend."""
        result = classify("แก้หลังบ้าน")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "backend"


# ─────────────────────────────────────────────────────────────────────
# New Thai actionable verbs (จัด, ฝาก, รบกวน, เช็ค)
# ─────────────────────────────────────────────────────────────────────


class TestNewThaiActionableVerbs:
    def test_jad_is_actionable(self):
        """'จัด layout หน้า home' should be classified as actionable (PROPOSE)."""
        result = classify("จัด layout หน้า home")
        assert result.kind == ActionKind.PROPOSE

    def test_fak_is_actionable(self):
        """'ฝากดู bug นี้' should be actionable (not INFORMATIONAL)."""
        result = classify("ฝากดู bug นี้")
        assert result.kind != ActionKind.INFORMATIONAL

    def test_robkuan_is_actionable(self):
        """'รบกวนช่วยแก้ X' should be actionable."""
        result = classify("รบกวนช่วยแก้ bug นี้")
        assert result.kind == ActionKind.PROPOSE

    def test_check_th_is_actionable(self):
        """'เช็ค endpoint /auth' should be actionable and route to backend."""
        result = classify("เช็ค endpoint /auth")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "backend"


# ─────────────────────────────────────────────────────────────────────
# New English actionable verbs (debug, analyze, verify)
# ─────────────────────────────────────────────────────────────────────


class TestNewEnglishActionableVerbs:
    def test_debug_is_actionable(self):
        """'debug login flow' should be actionable (not INFORMATIONAL)."""
        result = classify("debug login flow")
        assert result.kind == ActionKind.PROPOSE

    def test_verify_is_actionable(self):
        """'verify migration script' should be actionable."""
        result = classify("verify migration script")
        assert result.kind == ActionKind.PROPOSE

    def test_analyze_is_actionable(self):
        """'analyze the query performance' should be actionable."""
        result = classify("analyze the query performance")
        assert result.kind == ActionKind.PROPOSE


# ─────────────────────────────────────────────────────────────────────
# Flexible explicit role patterns (ช่วย, ฝาก, role review/check/ดู)
# ─────────────────────────────────────────────────────────────────────


class TestFlexibleExplicitRole:
    def test_role_chuay_verb(self):
        """'backend ช่วยแก้ X' should fire directly as FIRE_ASSIGN backend."""
        result = classify("backend ช่วยแก้ bug นี้หน่อย")
        assert result.kind == ActionKind.FIRE_ASSIGN
        assert result.role == "backend"

    def test_fak_role_verb(self):
        """'ฝาก devops ดู pipeline' should fire directly as FIRE_ASSIGN devops."""
        result = classify("ฝาก devops ดู pipeline")
        assert result.kind == ActionKind.FIRE_ASSIGN
        assert result.role == "devops"

    def test_fak_backend_fix(self):
        """'ฝาก backend แก้ bug นี้' should fire as FIRE_ASSIGN backend."""
        result = classify("ฝาก backend แก้ bug นี้")
        assert result.kind == ActionKind.FIRE_ASSIGN
        assert result.role == "backend"

    def test_role_review_direct(self):
        """'reviewer ดู โค้ดนี้' should fire as FIRE_ASSIGN reviewer."""
        result = classify("reviewer ดู โค้ดนี้")
        assert result.kind == ActionKind.FIRE_ASSIGN
        assert result.role == "reviewer"


# ─────────────────────────────────────────────────────────────────────
# One-shot codex/gemini natural patterns (without ถาม/ขอ/ให้ prefix)
# ─────────────────────────────────────────────────────────────────────


class TestOneShotNatural:
    def test_codex_review_direct(self):
        """'codex review function นี้' should fire as FIRE_ONESHOT codex."""
        result = classify("codex review function นี้")
        assert result.kind == ActionKind.FIRE_ONESHOT
        assert result.role == "codex"

    def test_gemini_check_direct(self):
        """'gemini check this approach' should fire as FIRE_ONESHOT gemini."""
        result = classify("gemini check this approach")
        assert result.kind == ActionKind.FIRE_ONESHOT
        assert result.role == "gemini"

    def test_codex_lorg_direct(self):
        """'codex ลอง refactor นี้' should fire as FIRE_ONESHOT codex."""
        result = classify("codex ลอง refactor นี้")
        assert result.kind == ActionKind.FIRE_ONESHOT
        assert result.role == "codex"


# ─────────────────────────────────────────────────────────────────────
# Thai-English hybrid messages
# ─────────────────────────────────────────────────────────────────────


class TestThaiEnglishHybrid:
    def test_fix_bug_thai_page(self):
        """'แก้ bug หน้า login' should route to frontend."""
        result = classify("แก้ bug หน้า login")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "frontend"
