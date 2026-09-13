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
