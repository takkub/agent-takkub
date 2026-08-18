"""#296: a pane that is BLOCKED must not be handed to Lead as a FAILED result.

BLOCKED = the work could not RUN (a credential, test account, permission, data
or external service is missing). FAILED = the work ran and something broke.
Only the second one has a role to route back to; proposing a fix loop for the
first sends a teammate to change working code, and in the reported field case
that meant backend touching a live auth system so QA could log in.
"""

from __future__ import annotations

from agent_takkub.orchestrator import Orchestrator


class TestBlockedHandoff:
    def test_credential_blocker_produces_a_blocked_handoff_not_a_fix_loop(self) -> None:
        out = Orchestrator._build_verify_fail_handoff(
            "qa", "สร้าง tenant ทดสอบไม่ได้เพราะไม่มีรหัสผ่าน super admin"
        )
        assert "BLOCKED" in out
        assert "fix loop" not in out
        # The exact failure mode from the issue: never name a role to route to.
        assert "signature ชี้ไปที่" not in out
        assert "backend" not in out

    def test_blocked_handoff_forbids_working_around_the_blocker(self) -> None:
        out = Orchestrator._build_verify_fail_handoff("qa", "เข้าระบบไม่ได้ ไม่มี credential ของ admin")
        assert "ห้าม" in out

    def test_blocked_handoff_says_what_is_missing(self) -> None:
        out = Orchestrator._build_verify_fail_handoff("qa", "ยังไม่มีข้อมูลทดสอบของเดือนนี้")
        assert "ติด blocker" in out

    def test_real_failure_still_gets_the_fix_loop(self) -> None:
        out = Orchestrator._build_verify_fail_handoff(
            "qa", "login endpoint returns 401 unauthorized for valid users"
        )
        assert "FAILED" in out
        assert "fix loop" in out
        assert "backend" in out

    def test_blank_note_still_gets_the_fix_loop_framing(self) -> None:
        out = Orchestrator._build_verify_fail_handoff("qa", "")
        assert "FAILED" in out
        assert "BLOCKED" not in out


class TestBlockedCliSurface:
    def test_blocked_flag_reaches_the_wire_as_blocked_and_failed(self, monkeypatch) -> None:
        """`--blocked` still means the task is NOT done, so it rides on the
        same `failed` bookkeeping — the difference is who gets asked next.

        Driven through `cli.main` so the argparse registration is exercised
        too: asserting on `cmd_done` alone would keep passing if the flag were
        never wired into the `done` subparser.
        """
        from agent_takkub import cli

        # `done` is a teammate verb — the CLI refuses it for lead.
        monkeypatch.setenv("TAKKUB_ROLE", "qa")
        sent: dict = {}
        monkeypatch.setattr(cli, "_request", lambda payload: sent.update(payload) or {"ok": True})

        cli.main(["done", "note here", "--blocked"])

        assert sent["cmd"] == "done"
        assert sent["blocked"] is True
        assert sent["failed"] is True

    def test_plain_done_is_neither_failed_nor_blocked(self, monkeypatch) -> None:
        from agent_takkub import cli

        # `done` is a teammate verb — the CLI refuses it for lead.
        monkeypatch.setenv("TAKKUB_ROLE", "qa")
        sent: dict = {}
        monkeypatch.setattr(cli, "_request", lambda payload: sent.update(payload) or {"ok": True})

        cli.main(["done", "note here"])

        assert sent["blocked"] is False
        assert sent["failed"] is False

    def test_fail_flag_is_still_failed_but_not_blocked(self, monkeypatch) -> None:
        from agent_takkub import cli

        # `done` is a teammate verb — the CLI refuses it for lead.
        monkeypatch.setenv("TAKKUB_ROLE", "qa")
        sent: dict = {}
        monkeypatch.setattr(cli, "_request", lambda payload: sent.update(payload) or {"ok": True})

        cli.main(["done", "boom", "--fail"])

        assert sent["failed"] is True
        assert sent["blocked"] is False
