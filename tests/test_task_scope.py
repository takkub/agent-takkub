"""Tests for task scope budget classification & prose injection (#585)."""

from __future__ import annotations

import pytest

from agent_takkub.task_scope import ScopeDecision, budget_block, classify, inject_budget


class TestTaskScopeClassify:
    @pytest.mark.parametrize(
        ("task_text", "expected_scope"),
        [
            ("แก้ 5 บรรทัด UI ตรงปุ่ม submit", "tiny"),
            ("แก้ 1 บรรทัด", "tiny"),
            ("แก้ typo ใน docs/intro.md", "tiny"),
            ("แก้คำผิดในหน้า settings", "tiny"),
            ("แก้สี header เป็นสีฟ้า", "tiny"),
            ("ปรับ margin นิดเดียว", "tiny"),
            ("เปลี่ยนข้อความปุ่ม", "tiny"),
            ("แก้ค่า config ตัวเดียว", "tiny"),
            ("แก้ 1 จุดใน src/agent_takkub/cli.py", "tiny"),
            # Section A regression fixtures from real project problems (#585 round 2)
            ("เพิ่มปุ่มปิด session ใน header ของ pane", "normal"),
            ("ทำ token meter ให้โชว์ยอดคงเหลือต่อ provider", "normal"),
            ("เพิ่ม retry ตอน CI แดงเพราะ flake", "normal"),
            ("ทำ release 2.1.4", "normal"),
            ("แก้ docker compose ให้ map port ใหม่", "normal"),
            ("rewrite orchestrator.py ใหม่ทั้งไฟล์", "deep"),
            ("ย้าย logic กรอง notice ออกจาก lead_inbox.py", "normal"),
            ("แก้ copy ปุ่ม submit เป็น ยืนยัน", "tiny"),
            # Single file without size signal is normal (not tiny)
            ("แก้ใน src/utils/format.py", "normal"),
            # Copy / ข้อความ must pair with แก้ / เปลี่ยน
            ("copy file config.json ไปที่ dist/", "normal"),
            ("แสดงข้อความ error เมื่อ token หมดอายุ", "normal"),
            # Forbidden tiny verbs (rewrite, refactor, ย้าย, etc.)
            ("ย้าย logic ออกมา", "normal"),
            ("refactor ฟังก์ชันคำนวณ", "normal"),
            # Deep wins over everything, even "แค่" / small hints
            ("แค่แก้ auth นิดเดียว", "deep"),
            ("แก้ 1 บรรทัดใน prisma schema", "deep"),
            ("database migration สำหรับ users table", "deep"),
            ("แก้ security vulnerability xss", "deep"),
            ("หมุน api token ใหม่", "deep"),
            ("เข้ารหัส password ด้วย bcrypt", "deep"),
            ("แก้ระบบ stripe payment", "deep"),
            ("อัปเกรด package.json dependencies", "deep"),
            ("แก้ pnpm-lock.yaml", "deep"),
            ("setup k8s deployment", "deep"),
            ("แก้ .github/workflows/ci.yml", "deep"),
            ("refactor ข้ามโมดูลระหว่าง frontend และ backend", "deep"),
            ("เปลี่ยนชื่อทั้งโปรเจค", "deep"),
            ("deploy ขึ้น prod", "deep"),
            # Normal defaults
            ("", "normal"),
            ("   ", "normal"),
            ("ทำฟีเจอร์ export excel สำหรับหน้ารายงาน", "normal"),
            ("สร้างระบบ notification แจ้งเตือนผู้ใช้", "normal"),
            ("เพิ่ม endpoint ดึงประวัติการใช้งาน", "normal"),
            # #602: deep keywords in context/references/prohibitions must NOT deep
            ("งานแก้ UI ที่พูดถึง `minDepositValid` ในฟอร์มเช็คอิน", "normal"),
            (
                "## ข้อเท็จจริง\nงาน e2e รันกับ dist ที่ใช้ prisma schema อยู่\n## ทำ\nแก้ nginx proxy ให้ timeout เพิ่ม",
                "normal",
            ),
            ("ห้ามแก้ prisma schema โดยตรง ทำงานแค่ UI", "normal"),
            # #602: signal inside the action section is still deep
            ("แก้ schema Prisma เพิ่มคอลัมน์ users.role", "deep"),
        ],
    )
    def test_classify_table(self, task_text: str, expected_scope: str) -> None:
        decision = classify(task_text)
        assert isinstance(decision, ScopeDecision)
        assert decision.scope == expected_scope
        assert decision.reason != ""

    def test_deep_wins_over_tiny_signal(self) -> None:
        # "แค่" + auth -> deep wins
        decision = classify("แค่อัปเดต auth นิดเดียว")
        assert decision.scope == "deep"
        assert "deep" in decision.reason

    def test_empty_string_is_normal(self) -> None:
        decision = classify("")
        assert decision.scope == "normal"
        assert "เปล่า" in decision.reason


class TestReadOnlyIntentWeighting:
    """#670: a deep keyword must not unconditionally beat explicit read-only
    intent / explicit small-size signals — and the reason must name the
    competing signals, not just the winner."""

    # The exact 2026-09-18 field case (v2.1.19, project unirecon): a
    # read-only DB investigation sized `deep` off the single word "schema".
    REPRO_INVESTIGATE = (
        "INVESTIGATE (read-only, ห้ามแก้โค้ด ห้าม commit) — งานเล็ก แค่ query local DB อย่างเดียว\n"
        "ต้องการแค่: query local reconcile-db ดู field mapping schema ของ template ที่ local ใช้\n"
        "ห้ามแตะ prod ห้ามแก้ไฟล์"
    )

    def test_read_only_investigate_with_schema_keyword_is_not_deep(self) -> None:
        decision = classify(self.REPRO_INVESTIGATE)
        assert decision.scope == "normal"
        # #670 item 4: the reason names both sides of the fight.
        assert "schema" in decision.reason
        assert "read-only" in decision.reason

    def test_read_only_blocks_tiny_from_bare_kae(self) -> None:
        # The same session's reverse miss (v2.1.18): a 12-minute PROD
        # read-only browser audit sized `tiny` off the word "แค่".
        decision = classify(
            "PROD read-only audit — แค่ไล่ตรวจหน้า report ทุกหน้าใน prod ด้วย browser "
            "แล้วรายงานผล ไม่มีการแก้อะไรทั้งสิ้น"
        )
        assert decision.scope == "normal"
        assert "read-only" in decision.reason

    def test_read_only_suppresses_multi_category_deep_too(self) -> None:
        decision = classify(
            "ตรวจสอบอย่างเดียว: อ่าน schema ปัจจุบัน และดูว่า migration ล่าสุดรันครบไหม รายงานผลอย่างเดียว"
        )
        assert decision.scope == "normal"

    def test_write_schema_task_still_deep_despite_small_words(self) -> None:
        # Weighting must not neuter genuine deep work: a task that MODIFIES
        # the schema stays deep even with "แค่/นิดเดียว" in it.
        decision = classify("แค่แก้ prisma schema เพิ่มคอลัมน์เดียว นิดเดียวเอง")
        assert decision.scope == "deep"
        # ...and the reason discloses the competing small signal it beat.
        assert "แข่ง" in decision.reason

    def test_read_context_deep_keyword_with_small_signal_is_normal(self) -> None:
        # No explicit read-only marker, but the deep keyword is something the
        # task READS (query/ดู) and an explicit small-size signal competes.
        decision = classify("งานเล็ก แค่ query ดู schema ของ template แล้วสรุปให้ Lead")
        assert decision.scope == "normal"

    def test_partial_file_ban_does_not_count_as_read_only(self) -> None:
        # "ห้ามแก้ไฟล์ <เฉพาะจุด>" is a boundary inside a writing task, not a
        # read-only declaration — deep keyword keeps its authority.
        decision = classify("เพิ่ม endpoint จ่ายเงินผ่าน stripe — ห้ามแก้ไฟล์ config กลาง")
        assert decision.scope == "deep"

    def test_commit_ban_alone_does_not_count_as_read_only(self) -> None:
        decision = classify("แก้ database migration ของ users table — ห้าม commit เอง รอ Lead")
        assert decision.scope == "deep"


class TestIncidentalWordMisscope685:
    """#685: scope must not be decided by words that happen to appear in a
    sentence describing something else — 3 real field cases (saas_admin_amb,
    2026-09-20), wrong in both directions."""

    def test_kae_scoping_a_noun_is_not_tiny(self) -> None:
        # "เอาแค่ตัวกงล้อมาใช้" = take ONLY the wheel part (scope of the thing
        # taken) — the work itself was a 400+ line new component.
        decision = classify("เขียนคอมโพเนนต์กงล้อใหม่ในหน้าโปรโมชั่น เอาแค่ตัวกงล้อมาใช้ ไม่เอาพื้นหลัง")
        assert decision.scope != "tiny"

    def test_kae_before_small_edit_verb_still_tiny(self) -> None:
        decision = classify("แค่เปลี่ยนข้อความปุ่มหน้าแรก")
        assert decision.scope == "tiny"
        # #685 item 4: the reason shows where the trigger fired.
        assert "พบที่" in decision.reason

    def test_data_line_count_is_not_a_line_budget(self) -> None:
        # "ระวังเคส 2 บรรทัด" describes prize-label DATA (2 lines of text),
        # not an edit budget.
        decision = classify("แก้ป้ายรางวัลกงล้อ ระวังเคส 2 บรรทัด (wedgeLines คืน 2 ค่า)")
        assert decision.scope != "tiny"

    def test_bounded_line_budget_still_tiny(self) -> None:
        assert classify("แก้ได้ไม่เกิน 2 บรรทัด ตรง label ปุ่ม").scope == "tiny"

    def test_login_as_access_instruction_is_not_deep(self) -> None:
        # "ต้องล็อกอินสมาชิกก่อน" tells the reader how to REACH the page —
        # the task never touches auth.
        decision = classify("ติดตั้งกงล้อในหน้าโปรโมชั่น หน้ากงล้ออยู่ในหน้าโปรโมชั่น ต้องล็อกอินสมาชิกก่อน")
        assert decision.scope != "deep"
        assert "เข้าถึง" in decision.reason

    def test_login_as_work_target_still_deep(self) -> None:
        assert classify("แก้ระบบล็อกอินให้รองรับ OTP").scope == "deep"


class TestBudgetBlock:
    def test_tiny_prose(self) -> None:
        block = budget_block("tiny")
        assert "งานขนาดเล็ก" in block
        assert "ห้ามเขียนไฟล์เทสใหม่" in block
        assert "ห้ามรัน test suite หรือ takkub qa-gate" in block

    def test_normal_prose(self) -> None:
        block = budget_block("normal")
        assert "**ไม่ต้องเขียนไฟล์เทสใหม่**" in block
        assert "ทดสอบของจริงว่าสิ่งที่แก้ทำงานถูก" in block

    def test_deep_prose(self) -> None:
        block = budget_block("deep")
        assert "เขียนเทสกันถอยเฉพาะ logic ที่เสี่ยงจริง" in block

    def test_unknown_scope_falls_back_to_normal(self) -> None:
        assert budget_block("unknown") == budget_block("normal")


class TestInjectBudget:
    def test_prepends_budget_as_first_line(self) -> None:
        task = "[ROLE: frontend] แก้สีปุ่ม"
        injected = inject_budget(task, "tiny")
        assert injected.startswith(budget_block("tiny"))
        assert "[ROLE: frontend] แก้สีปุ่ม" in injected

    def test_idempotent_if_already_injected(self) -> None:
        task = "[ROLE: frontend] แก้สีปุ่ม"
        injected1 = inject_budget(task, "tiny")
        injected2 = inject_budget(injected1, "tiny")
        assert injected1 == injected2
