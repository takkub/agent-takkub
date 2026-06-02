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

    def test_design_review_routes_to_critic(self):
        """'design review' / 'UI review' → critic with gemini cross-check.

        MUST resolve to `critic` (not `reviewer`) since the design-review
        rule sits above the generic review rule in the route table.
        """
        result = classify("design review the login page")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "critic"
        assert result.cross_check is not None
        assert "gemini" in result.cross_check

    def test_ui_review_routes_to_critic(self):
        result = classify("UI review on the dashboard screenshots")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "critic"

    def test_thai_review_ui_routes_to_critic(self):
        result = classify("รีวิว UI หน้า /login")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "critic"

    def test_thai_review_design_routes_to_critic(self):
        result = classify("รีวิวดีไซน์ของ dashboard")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "critic"

    def test_heuristic_routes_to_critic(self):
        result = classify("run heuristic evaluation on the cockpit")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "critic"

    def test_explicit_critic_role_recognised(self):
        """'ให้ critic review' → FIRE_ASSIGN (explicit-role skips propose per spec)."""
        result = classify("ให้ critic review หน้า login")
        assert result.kind == ActionKind.FIRE_ASSIGN
        assert result.role == "critic"

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

    def test_review_ui_and_api_routes_to_reviewer_not_parallel_impl(self):
        result = classify("review the API endpoint for the login form")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "reviewer"
        assert result.roles is None

    def test_test_ui_and_api_routes_to_qa_not_parallel_impl(self):
        result = classify("test the login page and auth endpoint")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "qa"
        assert result.roles is None

    def test_refactor_ui_and_api_keeps_codex_cross_check(self):
        result = classify("refactor the login page and auth endpoint")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "frontend"
        assert result.roles is None
        assert result.cross_check == ["codex"]

    def test_design_review_ui_and_api_routes_to_critic_not_parallel_impl(self):
        result = classify("design review the login page and auth endpoint")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "critic"
        assert result.roles is None
        assert result.cross_check is not None
        assert "gemini" in result.cross_check


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

    def test_optimize_is_actionable(self):
        result = classify("optimize the query performance")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "backend"

    def test_investigate_is_actionable(self):
        result = classify("investigate the login endpoint")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "backend"

    def test_upgrade_is_actionable(self):
        result = classify("upgrade Next.js")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "frontend"

    def test_enable_is_actionable(self):
        result = classify("enable dark mode")
        assert result.kind == ActionKind.PROPOSE

    def test_patch_is_actionable(self):
        result = classify("patch the XSS in the comment form")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "frontend"


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


# ─────────────────────────────────────────────────────────────────────
# Thai หน้า false-positive regression
# Ensures compound Thai words containing หน้า do NOT trigger frontend.
# ─────────────────────────────────────────────────────────────────────


class TestThaiPageFalsePositives:
    # ── negative cases (must NOT route to frontend via หน้า) ──────────

    def test_konnahna_does_not_match_frontend(self):
        """'ก่อนหน้า' (previous) must not trigger frontend rule."""
        result = classify("fix bug ที่เกิดในวันก่อนหน้า")
        # No UI keyword → should not route to frontend
        assert result.role != "frontend"

    def test_khangnahna_does_not_match_frontend(self):
        """'ข้างหน้า' (ahead) must not trigger frontend rule."""
        result = classify("scroll ไปข้างหน้า")
        assert result.role != "frontend"

    def test_dannahna_does_not_match_frontend(self):
        """'ด้านหน้า' (front side) must not trigger frontend rule."""
        result = classify("แก้ด้านหน้าของ object ใน backend")
        assert result.role != "frontend"

    def test_nahnahw_does_not_match_frontend(self):
        """'หน้าหนาว' (winter) must not trigger frontend rule."""
        result = classify("หน้าหนาวต้อง deploy infrastructure")
        # devops keyword present — role should be devops, not frontend
        assert result.role != "frontend"

    def test_nahfon_does_not_match_frontend(self):
        """'หน้าฝน' (rainy season) must not trigger frontend rule."""
        result = classify("หน้าฝนระบบ backend ล่มบ่อย")
        assert result.role != "frontend"

    # ── positive regression (must STILL route to frontend) ────────────

    def test_nah_slash_login_still_matches(self):
        """'ทำหน้า /login' must still route to frontend (Option A)."""
        result = classify("ทำหน้า /login")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "frontend"

    def test_nah_admin_still_matches(self):
        """'เพิ่มหน้า admin' must still route to frontend."""
        result = classify("เพิ่มหน้า admin")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "frontend"

    def test_nahjo_still_matches(self):
        """'หน้าจอ login พัง' must still route to frontend (via หน้าจอ)."""
        result = classify("แก้หน้าจอ login พัง")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "frontend"

    def test_pum_still_matches(self):
        """'แก้ปุ่ม submit' must still route to frontend (ปุ่ม unchanged)."""
        result = classify("แก้ปุ่ม submit")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "frontend"


class TestDisabledProviders:
    """classify() no longer refuses disabled codex/gemini. The spawn layer
    (provider_config.effective_provider_for) transparently backs an
    unavailable codex/gemini role with claude ("Claude รับตำแหน่งแทน"), so
    routing proceeds identically — disabled providers stay in cross_check and
    as primary, with a substitution note in `reason`."""

    def test_disabled_codex_kept_in_cross_check(self):
        """Refactor proposes backend + codex cross-check. With codex disabled
        the cross-check STAYS (claude will back it) — not dropped."""
        action = classify(
            "refactor the auth module to use the new session helper",
            context={"disabled_providers": {"codex"}},
        )
        assert action.kind == ActionKind.PROPOSE
        assert action.cross_check == ["codex"]
        assert "substitut" in action.reason.lower()

    def test_disabled_gemini_rollout_still_proposes_gemini(self):
        """Rollout/strategy routes to gemini as primary. With gemini disabled
        it STILL proposes gemini (claude-backed) — no ASK_CLARIFY refusal."""
        action = classify(
            "rollout plan for deploying the auth changes safely",
            context={"disabled_providers": {"gemini"}},
        )
        assert action.kind == ActionKind.PROPOSE
        assert action.role == "gemini"
        assert "substitut" in action.reason.lower()

    def test_both_disabled_primary_and_cross_check_survive(self):
        """Refactor with both disabled: primary backend, cross_check codex
        still present (both claude-backed where relevant)."""
        action = classify(
            "refactor backend to extract auth service",
            context={"disabled_providers": {"codex", "gemini"}},
        )
        assert action.kind == ActionKind.PROPOSE
        assert action.role == "backend"
        assert action.cross_check == ["codex"]

    def test_explicit_disabled_role_still_fires(self):
        """Explicit-role 'ให้ gemini ทำ ...' (action verb → explicit branch)
        with gemini disabled → FIRE_ASSIGN (claude-backed substitute), not
        ASK_CLARIFY."""
        action = classify(
            "ให้ gemini ทำ rollout plan",
            context={"disabled_providers": {"gemini"}},
        )
        assert action.kind == ActionKind.FIRE_ASSIGN
        assert action.role == "gemini"
        assert "explicit role" in action.reason.lower()
        assert "substitut" in action.reason.lower()

    def test_oneshot_phrasing_disabled_fires_as_pane(self):
        """One-shot phrasing 'ให้ gemini ดู ...' (no action verb) with gemini
        disabled → FIRE_ASSIGN pane substitute (one-shot has no CLI to hit)."""
        action = classify(
            "ให้ gemini ดู plan นี้",
            context={"disabled_providers": {"gemini"}},
        )
        assert action.kind == ActionKind.FIRE_ASSIGN
        assert action.role == "gemini"
        assert "substitut" in action.reason.lower()

    def test_oneshot_codex_disabled_becomes_pane_assign(self):
        """A one-shot to a disabled provider can't run as a one-shot (no CLI)
        → degrade to FIRE_ASSIGN: a claude-backed pane in that role's slot."""
        action = classify(
            "ขอ codex review function นี้",
            context={"disabled_providers": {"codex"}},
        )
        assert action.kind == ActionKind.FIRE_ASSIGN
        assert action.role == "codex"
        assert "substitut" in action.reason.lower()

    def test_none_disabled_is_backward_compat(self):
        """Default behavior (no context, or empty disabled set) unchanged."""
        action_none = classify("refactor the auth module")
        action_empty = classify(
            "refactor the auth module",
            context={"disabled_providers": set()},
        )
        assert action_none.kind == action_empty.kind == ActionKind.PROPOSE
        assert action_none.cross_check == action_empty.cross_check == ["codex"]


# ─────────────────────────────────────────────────────────────────────
# Explain / review the system → HTML explainer
# ─────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────
# Generate guide / setup / how-to / checklist → HTML
# ─────────────────────────────────────────────────────────────────────


class TestGenerateGuideHTML:
    # ── Positive: should return GENERATE_GUIDE_HTML ──────────────────

    def test_thai_setup_guide(self):
        assert classify("เขียน setup guide สำหรับ LINE").kind == ActionKind.GENERATE_GUIDE_HTML

    def test_thai_kumrue_kaichaigan(self):
        assert classify("สร้างคู่มือการใช้งาน takkub").kind == ActionKind.GENERATE_GUIDE_HTML

    def test_thai_kumrue_standalone(self):
        assert classify("เขียนคู่มือ takkub").kind == ActionKind.GENERATE_GUIDE_HTML

    def test_thai_withi_tangkha(self):
        assert classify("วิธีตั้งค่า LINE Notify").kind == ActionKind.GENERATE_GUIDE_HTML

    def test_thai_withi_chai(self):
        assert classify("วิธีใช้ takkub").kind == ActionKind.GENERATE_GUIDE_HTML

    def test_thai_ekasan_titatang(self):
        assert classify("เขียนเอกสารติดตั้ง docker").kind == ActionKind.GENERATE_GUIDE_HTML

    def test_thai_kheian_docs_hai_user(self):
        assert classify("เขียน docs ให้ user").kind == ActionKind.GENERATE_GUIDE_HTML

    def test_thai_kheian_ekasan_samrab(self):
        assert classify("เขียน เอกสาร สำหรับ LINE setup").kind == ActionKind.GENERATE_GUIDE_HTML

    def test_thai_checklist_samrab(self):
        """'สร้าง checklist สำหรับ LINE setup' — canonical issue #30 example."""
        assert classify("สร้าง checklist สำหรับ LINE setup").kind == ActionKind.GENERATE_GUIDE_HTML

    def test_thai_kheian_checklist(self):
        assert classify("เขียน checklist deploy").kind == ActionKind.GENERATE_GUIDE_HTML

    def test_en_setup_guide(self):
        assert (
            classify("write a setup guide for the project").kind == ActionKind.GENERATE_GUIDE_HTML
        )

    def test_en_installation_guide(self):
        assert classify("create an installation guide").kind == ActionKind.GENERATE_GUIDE_HTML

    def test_en_how_to_guide(self):
        assert classify("write a how-to guide for the API").kind == ActionKind.GENERATE_GUIDE_HTML

    def test_en_checklist_for(self):
        assert (
            classify("write a checklist for the deploy process").kind
            == ActionKind.GENERATE_GUIDE_HTML
        )

    def test_en_getting_started_guide(self):
        assert classify("create a getting started guide").kind == ActionKind.GENERATE_GUIDE_HTML

    def test_en_user_guide(self):
        assert classify("write a user guide").kind == ActionKind.GENERATE_GUIDE_HTML

    def test_en_onboarding_checklist(self):
        assert classify("create onboarding checklist").kind == ActionKind.GENERATE_GUIDE_HTML

    def test_en_step_by_step_guide(self):
        assert (
            classify("write a step-by-step guide for deployment").kind
            == ActionKind.GENERATE_GUIDE_HTML
        )

    def test_task_hint_carried(self):
        action = classify("วิธีใช้ takkub")
        assert action.task_hint == "วิธีใช้ takkub"

    def test_reason_mentions_html(self):
        action = classify("สร้างคู่มือ takkub")
        assert ".html" in action.reason.lower()

    # ── Negative: must NOT trigger GENERATE_GUIDE_HTML ───────────────

    def test_setup_docker_is_devops(self):
        """'setup docker compose' is a devops task, not a guide."""
        result = classify("setup docker compose for the project")
        assert result.kind != ActionKind.GENERATE_GUIDE_HTML
        assert result.role == "devops"

    def test_setup_ci_is_devops(self):
        """'setup CI pipeline' is devops, not a guide."""
        result = classify("setup CI pipeline")
        assert result.kind != ActionKind.GENERATE_GUIDE_HTML
        assert result.role == "devops"

    def test_add_checklist_component_is_frontend(self):
        """'add checklist component' is frontend UI — no doc-intent marker."""
        result = classify("add a checklist component")
        assert result.kind != ActionKind.GENERATE_GUIDE_HTML
        assert result.role == "frontend"

    def test_explain_system_still_explain_system(self):
        """EXPLAIN_SYSTEM pattern 'อธิบายระบบ' unaffected by new check."""
        result = classify("อธิบายระบบ")
        assert result.kind == ActionKind.EXPLAIN_SYSTEM

    def test_design_review_unaffected(self):
        result = classify("design review หน้า login")
        assert result.kind != ActionKind.GENERATE_GUIDE_HTML
        assert result.role == "critic"

    def test_code_review_unaffected(self):
        result = classify("do a code review for auth PR")
        assert result.kind != ActionKind.GENERATE_GUIDE_HTML
        assert result.role == "reviewer"

    def test_normal_impl_unaffected(self):
        result = classify("เพิ่ม login form")
        assert result.kind != ActionKind.GENERATE_GUIDE_HTML
        assert result.role == "frontend"

    def test_rollout_plan_not_guide(self):
        result = classify("create a rollout plan for the new auth system")
        assert result.kind != ActionKind.GENERATE_GUIDE_HTML
        assert result.role == "gemini"


class TestExplainSystem:
    def test_thai_review_system_how_it_works(self):
        # the user's canonical example
        assert classify("รีวิวระบบหน่อย ทำงานยังไง").kind == ActionKind.EXPLAIN_SYSTEM

    def test_thai_explain_system(self):
        assert classify("อธิบายระบบ").kind == ActionKind.EXPLAIN_SYSTEM

    def test_thai_how_does_this_system_work(self):
        assert classify("ระบบนี้ทำงานยังไง").kind == ActionKind.EXPLAIN_SYSTEM

    def test_thai_project_overview(self):
        assert classify("ขอภาพรวมระบบ").kind == ActionKind.EXPLAIN_SYSTEM

    def test_en_how_does_the_system_work(self):
        assert classify("how does the system work").kind == ActionKind.EXPLAIN_SYSTEM

    def test_en_explain_architecture(self):
        assert (
            classify("explain the architecture of this project").kind == ActionKind.EXPLAIN_SYSTEM
        )

    def test_en_system_overview(self):
        assert classify("give me a system overview").kind == ActionKind.EXPLAIN_SYSTEM

    def test_task_hint_carried(self):
        action = classify("อธิบายระบบ")
        assert action.task_hint == "อธิบายระบบ"

    # ── negatives: must NOT steal normal traffic ──
    def test_code_review_endpoint_still_reviewer(self):
        action = classify("review the login endpoint")
        assert action.kind == ActionKind.PROPOSE
        assert action.role == "reviewer"

    def test_design_review_still_critic(self):
        action = classify("design review หน้า login")
        assert action.kind == ActionKind.PROPOSE
        assert action.role == "critic"

    def test_normal_impl_task_unaffected(self):
        action = classify("เพิ่ม login form")
        assert action.kind == ActionKind.PROPOSE
        assert action.role == "frontend"

    def test_rollout_for_a_system_not_explain(self):
        # "rollout plan ... auth system" is strategy, not an explainer
        action = classify("create a rollout plan for the new auth system")
        assert action.kind != ActionKind.EXPLAIN_SYSTEM
        assert action.role == "gemini"
