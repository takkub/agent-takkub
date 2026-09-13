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

from agent_takkub.routing_planner import (
    REVIEWER_MODE_ALIASES,
    ActionKind,
    RoutingAction,
    classify,
    classify_blocked,
    classify_failure,
    resolve_role_alias,
)


class TestAssignModeSuggestion:
    def test_scan_suggests_subagent(self) -> None:
        action = classify("audit the auth package for unsafe defaults")
        assert action.suggested_mode == "subagent"

    def test_thai_search_suggests_subagent(self) -> None:
        action = classify("ค้นหา config ที่เปิด debug ทั้ง repo")
        assert action.suggested_mode == "subagent"

    def test_thai_find_suggests_subagent(self) -> None:
        action = classify("หาไฟล์ที่ประกาศ legacy flag")
        assert action.suggested_mode == "subagent"

    def test_thai_check_suggests_subagent(self) -> None:
        action = classify("ตรวจ endpoint ทั้งโปรเจ็ค")
        assert action.suggested_mode == "subagent"

    def test_implementation_stays_pane(self) -> None:
        action = classify("fix the auth endpoint")
        assert action.suggested_mode == "pane"

    def test_cross_model_check_stays_pane(self) -> None:
        action = classify("codex cross-check this audit")
        assert action.suggested_mode == "pane"


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

    def test_actionable_test_routes_to_reviewer_e2e(self):
        """#513: qa folded into reviewer --mode e2e."""
        result = classify("test the login flow")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "reviewer"
        assert result.mode == "e2e"

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
        """#513: explicit 'qa' still fires, but resolves to reviewer --mode e2e
        (deprecation alias — the qa.md role file itself still works standalone)."""
        result = classify("ให้ qa test หน้า login")
        assert result.kind == ActionKind.FIRE_ASSIGN
        assert result.role == "reviewer"
        assert result.mode == "e2e"
        assert "#513" in result.reason

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
        """#513: qa folded into reviewer --mode e2e (browser shard workflow unchanged)."""
        result = classify("write e2e tests for the login flow")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "reviewer"
        assert result.mode == "e2e"

    def test_reviewer(self):
        result = classify("do a code review for auth PR")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "reviewer"
        assert result.mode == "code"

    def test_design_review_routes_to_reviewer_ui_mode(self):
        """'design review' / 'UI review' → reviewer --mode ui (#513, formerly
        `critic`) with gemini cross-check.

        MUST resolve to mode="ui" (not "code") since the design-review rule
        sits above the generic review rule in the route table.
        """
        result = classify("design review the login page")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "reviewer"
        assert result.mode == "ui"
        assert result.cross_check is not None
        assert "gemini" in result.cross_check

    def test_ui_review_routes_to_reviewer_ui_mode(self):
        result = classify("UI review on the dashboard screenshots")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "reviewer"
        assert result.mode == "ui"

    def test_thai_review_ui_routes_to_reviewer_ui_mode(self):
        result = classify("รีวิว UI หน้า /login")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "reviewer"
        assert result.mode == "ui"

    def test_thai_review_design_routes_to_reviewer_ui_mode(self):
        result = classify("รีวิวดีไซน์ของ dashboard")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "reviewer"
        assert result.mode == "ui"

    def test_heuristic_routes_to_reviewer_ui_mode(self):
        result = classify("run heuristic evaluation on the cockpit")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "reviewer"
        assert result.mode == "ui"

    def test_explicit_critic_role_recognised(self):
        """'ให้ critic review' → FIRE_ASSIGN (explicit-role skips propose per
        spec), resolved through the #513 alias to reviewer --mode ui."""
        result = classify("ให้ critic review หน้า login")
        assert result.kind == ActionKind.FIRE_ASSIGN
        assert result.role == "reviewer"
        assert result.mode == "ui"
        assert "#513" in result.reason

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

    def test_test_ui_and_api_routes_to_reviewer_e2e_not_parallel_impl(self):
        result = classify("test the login page and auth endpoint")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "reviewer"
        assert result.mode == "e2e"
        assert result.roles is None

    def test_refactor_ui_and_api_keeps_codex_cross_check(self):
        result = classify("refactor the login page and auth endpoint")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "frontend"
        assert result.roles is None
        assert result.cross_check == ["codex"]

    def test_design_review_ui_and_api_routes_to_reviewer_ui_not_parallel_impl(self):
        result = classify("design review the login page and auth endpoint")
        assert result.kind == ActionKind.PROPOSE
        assert result.role == "reviewer"
        assert result.mode == "ui"
        assert result.roles is None
        assert result.cross_check is not None
        assert "gemini" in result.cross_check


# ─────────────────────────────────────────────────────────────────────
# #513: qa + reviewer + critic merged into `reviewer --mode code|e2e|ui`.
# ─────────────────────────────────────────────────────────────────────


class TestReviewerModeAliasMapping:
    """`resolve_role_alias` — the single source of truth both the explicit-role
    branch and `_ROUTE_TABLE` funnel through."""

    def test_qa_maps_to_reviewer_e2e(self):
        assert resolve_role_alias("qa") == ("reviewer", "e2e")

    def test_critic_maps_to_reviewer_ui(self):
        assert resolve_role_alias("critic") == ("reviewer", "ui")

    def test_reviewer_maps_to_itself_code_mode(self):
        assert resolve_role_alias("reviewer") == ("reviewer", "code")

    def test_unrelated_role_passes_through_unchanged(self):
        assert resolve_role_alias("backend") == ("backend", None)
        assert resolve_role_alias("frontend") == ("frontend", None)

    def test_alias_table_only_knows_qa_and_critic(self):
        """Nothing else is silently folded into reviewer by this merge."""
        assert set(REVIEWER_MODE_ALIASES) == {"qa", "critic"}


class TestReviewerDeprecationWarning:
    """Explicitly invoking the deprecated 'qa'/'critic' role name still fires
    (the role file itself is untouched — #513 keeps it for >= 1 release) but
    the reason string must carry a real, checkable deprecation warning."""

    def test_explicit_qa_reason_names_513_and_reviewer_mode(self):
        action = classify("ให้ qa test หน้า login")
        assert action.kind == ActionKind.FIRE_ASSIGN
        assert "#513" in action.reason
        assert "reviewer" in action.reason
        assert "--mode e2e" in action.reason

    def test_explicit_critic_reason_names_513_and_reviewer_mode(self):
        action = classify("ให้ critic review หน้า login")
        assert action.kind == ActionKind.FIRE_ASSIGN
        assert "#513" in action.reason
        assert "reviewer" in action.reason
        assert "--mode ui" in action.reason

    def test_explicit_reviewer_itself_carries_no_deprecation_note(self):
        """'reviewer' isn't deprecated — only the qa/critic aliases warn."""
        action = classify("ให้ reviewer review โค้ด auth")
        assert action.kind == ActionKind.FIRE_ASSIGN
        assert "#513" not in action.reason

    def test_table_routed_e2e_reason_mentions_mode_no_deprecation_tag(self):
        """Keyword-routed (non-explicit) traffic doesn't need the '#513'
        per-alias warning — it was never asking for the old role by name."""
        action = classify("write e2e tests for the login flow")
        assert action.mode == "e2e"
        assert "reviewer --mode e2e" in action.reason


class TestReviewerE2EModeShardWorkflowPreserved:
    """mode=e2e must keep carrying everything the browser e2e shard workflow
    (--plan --shards N, Playwright MCP) needs to recognise the task — the
    #513 merge only changed which `role` string routing reports, not what
    the underlying qa.md-derived shard machinery receives."""

    def test_smoke_test_routes_e2e(self):
        assert classify("smoke test the checkout flow").mode == "e2e"

    def test_regression_routes_e2e(self):
        assert classify("run a regression pass on the auth flow").mode == "e2e"

    def test_end_to_end_phrase_routes_e2e(self):
        assert classify("write end to end coverage for signup").mode == "e2e"

    def test_e2e_mode_has_no_gemini_cross_check(self):
        """Only ui mode auto-fires gemini — e2e stays a solo reviewer pane."""
        action = classify("write e2e tests for the login flow")
        assert action.mode == "e2e"
        assert action.cross_check is None


class TestReviewerUIModeGeminiCrossCheckPreserved:
    """mode=ui must keep the critic pipeline's gemini cross-check (#512
    preset sizing untouched) so the design-review pipeline still spawns a
    gemini pane in parallel."""

    def test_design_review_keeps_gemini_cross_check(self):
        action = classify("design review the checkout page")
        assert action.mode == "ui"
        assert action.cross_check == ["gemini"]

    def test_ux_review_keeps_gemini_cross_check(self):
        action = classify("UX review on the settings screen")
        assert action.mode == "ui"
        assert action.cross_check == ["gemini"]

    def test_thai_ui_review_keeps_gemini_cross_check(self):
        action = classify("รีวิว UI หน้า /checkout")
        assert action.mode == "ui"
        assert action.cross_check == ["gemini"]


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


class TestDisabledRoles:
    """#510: a role OFF in Settings → Providers & Roles (`disabled_roles`
    context) has NO substitute — unlike a disabled provider (claude backs the
    same slot), a disabled role must never be proposed/fired at all."""

    def test_explicit_disabled_role_not_fired(self):
        action = classify(
            "ให้ qa ทำ test นี้",
            context={"disabled_roles": {"qa"}},
        )
        assert action.kind == ActionKind.INFORMATIONAL
        assert action.role is None
        assert "qa" in action.reason and "ปิด" in action.reason

    def test_oneshot_disabled_role_not_fired(self):
        action = classify(
            "ขอ codex review function นี้",
            context={"disabled_roles": {"codex"}},
        )
        assert action.kind == ActionKind.INFORMATIONAL
        assert action.role is None

    def test_disabled_primary_role_not_proposed(self):
        """'test the login flow' routes to qa; qa disabled → no proposal."""
        action = classify("test the login flow", context={"disabled_roles": {"qa"}})
        assert action.kind == ActionKind.INFORMATIONAL
        assert action.role is None
        assert "qa" in action.reason

    def test_disabled_cross_check_role_dropped_not_refused(self):
        """Refactor proposes backend + codex cross-check; codex disabled as a
        ROLE (not just provider) → dropped from cross_check, backend still fires."""
        action = classify(
            "refactor the auth module to use the new session helper",
            context={"disabled_roles": {"codex"}},
        )
        assert action.kind == ActionKind.PROPOSE
        assert action.role == "backend"
        assert action.cross_check is None

    def test_multirole_split_one_disabled_falls_back_to_other(self):
        """UI+API implementation normally proposes frontend+backend; backend
        role disabled → collapses to a single frontend proposal."""
        action = classify(
            "add a login form component that calls the backend API endpoint",
            context={"disabled_roles": {"backend"}},
        )
        assert action.kind == ActionKind.PROPOSE
        assert action.role == "frontend"
        assert action.roles is None

    def test_multirole_split_both_disabled_no_proposal(self):
        action = classify(
            "add a login form component that calls the backend API endpoint",
            context={"disabled_roles": {"frontend", "backend"}},
        )
        assert action.kind == ActionKind.INFORMATIONAL
        assert action.role is None

    def test_none_disabled_roles_is_backward_compat(self):
        action_none = classify("test the login flow")
        action_empty = classify("test the login flow", context={"disabled_roles": set()})
        assert action_none.kind == action_empty.kind == ActionKind.PROPOSE
        assert action_none.role == action_empty.role == "reviewer"
        assert action_none.mode == action_empty.mode == "e2e"

    def test_disabled_legacy_qa_still_blocks_reviewer_e2e_mode(self):
        """#513: 'qa' was never renamed as a Settings toggle — disabling it
        must still block the e2e mode it now maps to, even though the
        routing table's `role` field reads "reviewer" post-merge."""
        action = classify("test the login flow", context={"disabled_roles": {"qa"}})
        assert action.kind == ActionKind.INFORMATIONAL
        assert action.role is None
        assert "qa" in action.reason and "ปิด" in action.reason

    def test_disabled_legacy_critic_still_blocks_reviewer_ui_mode(self):
        action = classify("design review the login page", context={"disabled_roles": {"critic"}})
        assert action.kind == ActionKind.INFORMATIONAL
        assert action.role is None
        assert "critic" in action.reason and "ปิด" in action.reason

    def test_disabled_reviewer_blocks_every_mode(self):
        """Disabling the canonical 'reviewer' role blocks code/e2e/ui alike."""
        for msg in (
            "do a code review for auth PR",
            "test the login flow",
            "design review หน้า login",
        ):
            action = classify(msg, context={"disabled_roles": {"reviewer"}})
            assert action.kind == ActionKind.INFORMATIONAL, msg
            assert action.role is None, msg


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
        assert result.role == "reviewer"
        assert result.mode == "ui"

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

    def test_design_review_still_reviewer_ui_mode(self):
        action = classify("design review หน้า login")
        assert action.kind == ActionKind.PROPOSE
        assert action.role == "reviewer"
        assert action.mode == "ui"

    def test_normal_impl_task_unaffected(self):
        action = classify("เพิ่ม login form")
        assert action.kind == ActionKind.PROPOSE
        assert action.role == "frontend"

    def test_rollout_for_a_system_not_explain(self):
        # "rollout plan ... auth system" is strategy, not an explainer
        action = classify("create a rollout plan for the new auth system")
        assert action.kind != ActionKind.EXPLAIN_SYSTEM
        assert action.role == "gemini"


# ── Tier 2c — dependency sequencing + failure classification ────────────────


class TestDependencySequencing:
    """UI+API tasks with a data dependency must come back SEQUENCED
    (backend→frontend), not parallel — a wrong parallel split now costs a
    worktree each under Multi×worktree."""

    def test_independent_ui_api_stays_parallel(self):
        act = classify("เพิ่มหน้า /login form และ endpoint POST /auth/login")
        assert act.kind == ActionKind.PROPOSE
        assert act.roles == ["frontend", "backend"]
        assert act.sequence is None  # independent → parallel OK

    def test_thai_dependency_signal_sequences(self):
        act = classify("สร้างหน้า form โปรไฟล์ ใช้ข้อมูลจาก endpoint /api/profile ที่ backend ทำ")
        assert act.roles == ["frontend", "backend"]
        assert act.sequence == ["backend", "frontend"]
        assert "SEQUENCE" in act.reason

    def test_english_dependency_signal_sequences(self):
        act = classify("build the settings page UI based on the API response from the backend db")
        assert act.sequence == ["backend", "frontend"]

    def test_ta_m_schema_signal(self):
        act = classify("ทำหน้า checkout form ตาม schema ที่ API ส่งมา พร้อม endpoint ใหม่")
        assert act.sequence == ["backend", "frontend"]

    def test_single_role_never_sequences(self):
        act = classify("เพิ่มปุ่ม logout ที่หน้า navbar")
        assert act.sequence is None


class TestClassifyFailure:
    def test_infra_signature_beats_backend(self):
        # container down produces API errors too — root cause wins
        role, why = classify_failure("QA fail: API 500 เพราะ container ยังไม่ขึ้น ECONNREFUSED :5310")
        assert role == "devops"
        assert "infra" in why

    def test_backend_signature(self):
        role, _ = classify_failure("POST /auth/login returns 500, traceback ใน log: sql error")
        assert role == "backend"

    def test_auth_signature_is_backend(self):
        role, _ = classify_failure("expected 200 got 401 unauthorized, jwt token expired")
        assert role == "backend"

    def test_frontend_signature(self):
        role, _ = classify_failure(
            "หน้า dashboard เพี้ยน — console error: undefined is not a function"
        )
        assert role == "frontend"

    def test_qa_flaky_signature(self):
        role, why = classify_failure("test flaky: timeout รอ element #submit แต่ retry passed")
        assert role == "qa"
        assert "test" in why

    def test_unknown_returns_none(self):
        role, why = classify_failure("ผลไม่ตรง spec นิดหน่อย")
        assert role is None and why == ""

    def test_empty_note(self):
        assert classify_failure("") == (None, "")


class TestClassifyBlockedVsFailed:
    """#296: BLOCKED is not FAILED.

    Field case (2026-08-18, two qa panes, identical text): QA could not create
    a test tenant because it had no super-admin password. Nothing was broken —
    but the signature matcher read the credential words as a backend fault and
    proposed routing the fix loop to backend. Acting on that sends a teammate
    to "fix" a working auth system, and the worst version of that is backend
    resetting a real password so QA can get in.

    The discriminator is an ABSENCE cue next to the thing, so a genuine auth
    bug still routes to backend.
    """

    def test_missing_super_admin_password_is_blocked_not_backend(self) -> None:
        note = "สร้าง tenant ทดสอบไม่ได้เพราะไม่มีรหัสผ่าน super admin"
        is_blocked, what = classify_blocked(note)
        assert is_blocked is True
        assert what
        assert classify_failure(note) == (None, "")

    def test_english_missing_credentials_is_blocked(self) -> None:
        note = "Cannot run the suite: missing API credentials for the staging tenant"
        assert classify_blocked(note)[0] is True
        assert classify_failure(note)[0] is None

    def test_waiting_for_access_is_blocked(self) -> None:
        assert classify_blocked("blocked on access to the reporting dashboard")[0] is True

    def test_missing_test_data_is_blocked(self) -> None:
        assert classify_blocked("รันไม่ได้ ยังไม่มีข้อมูลทดสอบของเดือนนี้")[0] is True

    def test_human_step_required_is_blocked(self) -> None:
        assert classify_blocked("ต้องให้เจ้าของกดอนุมัติในระบบก่อน")[0] is True

    # ── the other side: real bugs must still route ──────────────────────────

    def test_real_401_bug_still_routes_to_backend(self) -> None:
        role, _why = classify_failure("login endpoint returns 401 unauthorized for valid users")
        assert role == "backend"

    def test_expired_token_bug_still_routes_to_backend(self) -> None:
        note = "API คืน token invalid ทั้งที่เพิ่ง login — jwt พัง"
        assert classify_blocked(note)[0] is False
        assert classify_failure(note)[0] == "backend"

    def test_container_down_still_routes_to_devops(self) -> None:
        assert classify_failure("ECONNREFUSED :5310 — container ยังไม่ขึ้น")[0] == "devops"

    def test_ui_bug_still_routes_to_frontend(self) -> None:
        assert classify_failure("ปุ่ม submit กดไม่ได้ console error")[0] == "frontend"

    def test_empty_note_is_not_blocked(self) -> None:
        assert classify_blocked("") == (False, "")
        assert classify_blocked("   ") == (False, "")


class TestClassifyPreconditionMismatch:
    """#495 sub-item: a BLOCKED-shaped report whose real cause is the task
    SPEC pointing at the wrong target (tenant/env/branch/...), not anything
    actually missing. Field case: a qa pane couldn't find a member because
    the task pointed it at the wrong tenant — fixable by the Lead correcting
    the spec and re-assigning, not by waiting on an external owner."""

    def test_wrong_tenant_thai_is_mismatch(self) -> None:
        from agent_takkub.routing_planner import classify_precondition_mismatch

        note = "หาสมาชิกไม่เจอเพราะหาผิด tenant"
        is_mismatch, what = classify_precondition_mismatch(note)
        assert is_mismatch is True
        assert what

    def test_tenant_wrong_word_order_is_mismatch(self) -> None:
        from agent_takkub.routing_planner import classify_precondition_mismatch

        assert classify_precondition_mismatch("task spec ระบุ tenant ผิด")[0] is True

    def test_spec_pointed_wrong_english_is_mismatch(self) -> None:
        from agent_takkub.routing_planner import classify_precondition_mismatch

        assert classify_precondition_mismatch("task spec pointed to the wrong project")[0] is True

    def test_wrong_environment_english_is_mismatch(self) -> None:
        from agent_takkub.routing_planner import classify_precondition_mismatch

        assert classify_precondition_mismatch("looked in the wrong environment")[0] is True

    def test_genuine_missing_credential_is_not_a_mismatch(self) -> None:
        from agent_takkub.routing_planner import classify_precondition_mismatch

        note = "สร้าง tenant ทดสอบไม่ได้เพราะไม่มีรหัสผ่าน super admin"
        assert classify_precondition_mismatch(note)[0] is False
        # ... and stays classified as a genuine blocker.
        assert classify_blocked(note)[0] is True

    def test_real_bug_is_not_a_mismatch(self) -> None:
        from agent_takkub.routing_planner import classify_precondition_mismatch

        note = "login endpoint returns 401 unauthorized for valid users"
        assert classify_precondition_mismatch(note)[0] is False

    def test_empty_note_is_not_a_mismatch(self) -> None:
        from agent_takkub.routing_planner import classify_precondition_mismatch

        assert classify_precondition_mismatch("") == (False, "")


class TestClassifyCodeRootCause:
    """#538: a BLOCKED/PARTIAL-shaped note that names a concrete file or
    function is a code bug, not something missing from outside the codebase.
    Field case: qa reported a parser bug ("extractLauncherTargetUrl ถอด URL
    ไม่ได้") that also phrased the symptom near an absence-cue word, so
    `classify_blocked` fired and the cockpit rendered the "no role can fix
    this" credential template over a report that named the exact function to
    fix."""

    def test_camel_case_function_name_is_code_bug(self) -> None:
        from agent_takkub.routing_planner import classify_code_root_cause

        note = "extractLauncherTargetUrl ถอด URL ไม่ได้ — missing token in the decoded payload"
        is_code_bug, what = classify_code_root_cause(note)
        assert is_code_bug is True
        assert what == "extractLauncherTargetUrl"

    def test_camel_case_note_overrides_blocked_classification(self) -> None:
        # The exact shape from the field case: the note independently
        # matches classify_blocked's credential rule...
        from agent_takkub.routing_planner import classify_blocked, classify_code_root_cause

        note = "extractLauncherTargetUrl ถอด URL ไม่ได้ — missing token in the decoded payload"
        assert classify_blocked(note)[0] is True
        # ...but callers must check classify_code_root_cause first and skip
        # the credential template when it says True.
        assert classify_code_root_cause(note)[0] is True

    def test_file_with_code_extension_is_code_bug(self) -> None:
        from agent_takkub.routing_planner import classify_code_root_cause

        note = "ไม่มี access ไปเปิดหน้าเกม เพราะ launcher.ts ถอด query param ไม่ได้"
        is_code_bug, what = classify_code_root_cause(note)
        assert is_code_bug is True
        assert what == "launcher.ts"

    def test_function_call_syntax_is_code_bug(self) -> None:
        from agent_takkub.routing_planner import classify_code_root_cause

        note = "missing permission — parseUrl() throws before it can run"
        assert classify_code_root_cause(note)[0] is True

    def test_genuine_missing_credential_without_code_ref_is_not_a_code_bug(self) -> None:
        from agent_takkub.routing_planner import classify_code_root_cause

        note = "สร้าง tenant ทดสอบไม่ได้เพราะไม่มีรหัสผ่าน super admin"
        assert classify_code_root_cause(note)[0] is False

    def test_all_caps_acronym_is_not_a_code_bug(self) -> None:
        from agent_takkub.routing_planner import classify_code_root_cause

        note = "Cannot run the suite: missing API credentials for the staging tenant"
        assert classify_code_root_cause(note)[0] is False

    def test_empty_note_is_not_a_code_bug(self) -> None:
        from agent_takkub.routing_planner import classify_code_root_cause

        assert classify_code_root_cause("") == (False, "")


class TestScopeBudgetClassification:
    def test_tiny_scope_attached_to_action(self) -> None:
        action = classify("แก้คำผิดบนปุ่ม submit")
        assert action.scope == "tiny"
        assert action.scope_reason

    def test_deep_scope_attached_to_action(self) -> None:
        action = classify("refactor auth flow and migrate database schema")
        assert action.scope == "deep"
        assert action.scope_reason

    def test_normal_scope_default(self) -> None:
        action = classify("implement user profile page")
        assert action.scope == "normal"
        assert action.scope_reason
