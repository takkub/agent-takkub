"""2026-09-19 batch — #674 (relink silent no-op), #675/#676 (report builder:
lightbox reach, figure/imgpair captions, lint warn tiers, size warning),
#677 (done-kept pane killed mid-task by TTL; tombstone rows), #678 (done
notice eaten by the deduper; inbox reconcile), #679 (issue new --body-file),
#680 (status truth: last-progress everywhere, done unread/seen),
#681 (`แค่` inside a negation classified backup work as tiny),
#682 (self-commit warning fired on the "ห้าม commit เอง" boilerplate).

#673's own regression tests live in test_stale_marker_detector.py.
"""

from __future__ import annotations

import argparse
import base64
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import cli as cli_mod
from agent_takkub import issues as issues_mod
from agent_takkub import orchestrator as orch_mod
from agent_takkub import report_builder as rb_mod
from agent_takkub import task_scope
from agent_takkub.orchestrator import Orchestrator, _exit_key
from agent_takkub.report_builder import ReportBuilder, size_warning
from agent_takkub.task_delivery import make_notice_id

TEST_PROJECT = "batch673proj"

# 1x1 transparent PNG
_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQAB"
    "h6FO1AAAAABJRU5ErkJggg=="
)


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda project: project or TEST_PROJECT),
    )
    o = Orchestrator()
    o.shutdown_timers()
    return o


def _pane(state: str, cwd: str = ".", *, alive: bool = True, at_prompt: bool = True) -> MagicMock:
    pane = MagicMock()
    pane.state = state
    pane.session = MagicMock()
    pane.session.is_alive = alive
    pane.session.is_at_ready_prompt_cached.return_value = at_prompt
    pane.session.is_at_ready_prompt.return_value = at_prompt
    pane.session.has_background_work.return_value = False
    pane._session_cwd = cwd
    pane._transcript_path = None
    pane._session_generation = 1
    pane.set_state.side_effect = lambda s, **kw: setattr(pane, "state", s)
    return pane


# ── #681: `แค่` in a negation is not a small-task claim ─────────────────────


class TestExplicitSmallNegation:
    @pytest.mark.parametrize(
        "text",
        [
            "ซ้อมระบบสำรองข้อมูล ทดสอบเฉพาะ restore path จริง ไม่ใช่แค่รันเทสผ่าน",
            "ตรวจ cron ให้ครบทุก edge ไม่ได้แค่ดู log",
            "งานนี้ใหญ่กว่าที่คิด มากกว่าแค่ผิวเผิน ต้องรื้อดูทั้งเส้นทางข้อมูล",
        ],
    )
    def test_negated_kae_does_not_force_tiny(self, text: str) -> None:
        decision = task_scope.classify(text)
        assert decision.scope != "tiny", decision

    def test_plain_kae_still_reads_tiny(self) -> None:
        decision = task_scope.classify("แค่เปลี่ยนข้อความปุ่มหน้าแรก")
        assert decision.scope == "tiny", decision


# ── #682: self-commit warning must skip the prohibition boilerplate ─────────


class TestSelfCommitWarningNegation:
    @pytest.mark.parametrize(
        "task",
        [
            "แก้บั๊กนี้ให้จบ · ห้าม commit เอง รอ Lead",
            "ทำ UI ตาม mockup — อย่า commit เอง",
            "ไม่ต้อง commit เอง Lead จะรวบตอนท้าย batch",
            "เสร็จแล้วรายงาน done · Lead commit เอง",
        ],
    )
    def test_prohibition_does_not_warn(self, task: str) -> None:
        assert cli_mod._self_commit_isolation_warning(task, "shared") == ""

    def test_real_instruction_still_warns(self) -> None:
        warn = cli_mod._self_commit_isolation_warning(
            "ทำเสร็จแล้วให้ commit เอง แล้วรายงาน done", "shared"
        )
        assert "isolation" in warn

    def test_mixed_text_with_real_instruction_warns(self) -> None:
        # A prohibition on one line + a real instruction elsewhere: warn.
        warn = cli_mod._self_commit_isolation_warning(
            "ห้าม commit เอง ในไฟล์ config\nแต่ไฟล์งานหลักให้ commit เอง ได้เลย",
            "shared",
        )
        assert "isolation" in warn


# ── #679: issue new --body-file / stdin ─────────────────────────────────────


class TestIssueNewBodyFile:
    def _args(self, **kw) -> argparse.Namespace:
        base = dict(
            title="t",
            body=None,
            body_file=None,
            severity="med",
            noticed_in=None,
            role=None,
            tag=None,
            cwd=None,
            cockpit_bug=True,
            issues_dir=None,
            force=False,
        )
        base.update(kw)
        return argparse.Namespace(**base)

    def test_body_file_reads_backticks_verbatim(self, tmp_path, monkeypatch) -> None:
        body = "อธิบายบั๊ก `rm -rf` และ `$(dangerous)` ต้องรอดครบ"
        p = tmp_path / "body.md"
        p.write_text(body, encoding="utf-8")
        seen = {}

        def fake_new_issue(title, got_body, **kw):
            seen["body"] = got_body
            return 1, "http://x/1"

        monkeypatch.setattr(issues_mod, "new_issue", fake_new_issue)
        resp = issues_mod.cmd_issue_new(self._args(body_file=str(p)))
        assert resp["ok"], resp
        assert seen["body"] == body

    def test_body_and_body_file_are_mutually_exclusive(self) -> None:
        resp = issues_mod.cmd_issue_new(self._args(body="x", body_file="y"))
        assert not resp["ok"]
        assert "mutually exclusive" in resp["msg"]

    def test_empty_body_file_never_opens_editor(self, tmp_path, monkeypatch) -> None:
        p = tmp_path / "empty.md"
        p.write_text("", encoding="utf-8")
        monkeypatch.setattr(issues_mod, "new_issue", lambda *a, **k: (2, "http://x/2"))
        # if the $EDITOR branch ran it would try to launch a subprocess —
        # make that loudly fail instead of hanging
        monkeypatch.setattr(issues_mod, "subprocess", None, raising=False)
        resp = issues_mod.cmd_issue_new(self._args(body_file=str(p)))
        assert resp["ok"], resp

    def test_missing_body_file_errors(self) -> None:
        resp = issues_mod.cmd_issue_new(self._args(body_file="Z:/no/such/file.md"))
        assert not resp["ok"]
        assert "could not read" in resp["msg"]

    def test_parser_accepts_body_file_flag(self) -> None:
        parser = cli_mod.build_parser()
        args = parser.parse_args(["issue", "new", "t", "--body-file", "b.md"])
        assert args.body_file == "b.md"

    def test_parser_accepts_force_flag(self) -> None:
        parser = cli_mod.build_parser()
        args = parser.parse_args(["issue", "new", "t", "--body", "## อาการ", "--force"])
        assert args.force is True


# ── #674: relink must not report ok when it relinked nothing ────────────────


class TestRelinkRemoteOff:
    def _fake_reports(self, records, url=""):
        class _Err(Exception):
            pass

        return SimpleNamespace(
            list_shares=lambda project: records,
            is_active=lambda r: True,
            build_url=lambda ns, name, token: url,
            remote_status_text=lambda: "Remote: ปิดอยู่ → ลิงก์นี้ยังเปิดจากนอกไม่ได้",
            ReportError=_Err,
        )

    def _run_relink(self, monkeypatch, fake):
        monkeypatch.setattr(cli_mod, "_load_remote_reports", lambda: fake)
        monkeypatch.setattr(cli_mod, "_from_project", lambda: "projx", raising=False)
        monkeypatch.setattr(cli_mod.config, "validate_name", lambda v, kind: v, raising=False)
        args = argparse.Namespace(report_action="relink", project="projx")
        return cli_mod.cmd_report(args)

    def test_zero_relinked_with_pending_reports_is_a_failure(self, monkeypatch) -> None:
        rec = SimpleNamespace(name="r1.html", token="tok", label=None, created="2026-09-19")
        resp = self._run_relink(monkeypatch, self._fake_reports([rec], url=""))
        assert not resp["ok"], resp
        assert "เปิด Remote ก่อน" in resp["msg"]
        assert "1 ฉบับ" in resp["msg"]

    def test_relink_with_working_urls_stays_ok(self, monkeypatch) -> None:
        rec = SimpleNamespace(name="r1.html", token="tok", label=None, created="2026-09-19")
        resp = self._run_relink(
            monkeypatch, self._fake_reports([rec], url="https://x/r/r1.html?k=tok")
        )
        assert resp["ok"], resp
        assert "https://x/r/r1.html" in resp["msg"]

    def test_no_records_still_ok(self, monkeypatch) -> None:
        resp = self._run_relink(monkeypatch, self._fake_reports([], url=""))
        assert resp["ok"], resp
        assert "no active" in resp["msg"]


# ── #675/#676: report builder ───────────────────────────────────────────────


@pytest.fixture
def content_dir(tmp_path):
    d = tmp_path / "content"
    d.mkdir()
    (d / "imgs").mkdir()
    (d / "imgs" / "a.png").write_bytes(_PNG_1PX)
    (d / "imgs" / "b.png").write_bytes(_PNG_1PX + b"x")  # different md5
    (d / "images.txt").write_text(
        f"shot-a|{d / 'imgs' / 'a.png'}\nshot-b|{d / 'imgs' / 'b.png'}\n",
        encoding="utf-8",
    )
    return d


class TestReportBuilderPlaceholders:
    def test_figure_placeholder_emits_caption(self, content_dir) -> None:
        (content_dir / "content.html").write_text(
            "{{figure:shot-a|หน้าคิวถอนเงิน มีคำขอรออยู่ 1 รายการ}}", encoding="utf-8"
        )
        html = ReportBuilder("customer", str(content_dir)).build()
        assert "<figcaption>หน้าคิวถอนเงิน มีคำขอรออยู่ 1 รายการ</figcaption>" in html
        assert "data:image" in html

    def test_imgpair_emits_device_chips_and_shared_caption(self, content_dir) -> None:
        (content_dir / "content.html").write_text(
            "{{imgpair:shot-a|shot-b|หน้าแรกของสมาชิก}}", encoding="utf-8"
        )
        html = ReportBuilder("customer", str(content_dir)).build()
        assert 'class="imgpair"' in html
        assert "คอมพิวเตอร์" in html and "มือถือ" in html
        assert '<p class="pair-cap">หน้าแรกของสมาชิก</p>' in html

    def test_plain_img_placeholder_still_works(self, content_dir) -> None:
        (content_dir / "content.html").write_text(
            '<figure><img src="{{img:shot-a}}"></figure>', encoding="utf-8"
        )
        html = ReportBuilder("customer", str(content_dir)).build()
        assert 'src="data:image' in html

    def test_missing_image_raises_value_error_not_system_exit(self, content_dir) -> None:
        (content_dir / "content.html").write_text("{{img:no-such}}", encoding="utf-8")
        with pytest.raises(ValueError, match="Missing images"):
            ReportBuilder("customer", str(content_dir)).build()

    def test_built_page_carries_a_lightbox(self, content_dir) -> None:
        # #675: whatever template is in play, the built page must contain the
        # lightbox container — plain <figure><img> content included.
        (content_dir / "content.html").write_text(
            '<figure><img src="{{img:shot-a}}"></figure>', encoding="utf-8"
        )
        html = ReportBuilder("customer", str(content_dir)).build()
        assert 'id="lb"' in html

    def test_max_width_reaches_builder(self, content_dir) -> None:
        b = ReportBuilder("customer", str(content_dir), max_width=800)
        assert b.max_width == 800


class TestCustomerLintTiers:
    def _builder(self, content_dir, text: str) -> ReportBuilder:
        (content_dir / "content.html").write_text(text, encoding="utf-8")
        return ReportBuilder("customer", str(content_dir))

    def test_time_guarantee_and_automatic_claims_warn_not_block(self, content_dir) -> None:
        b = self._builder(
            content_dir,
            "เงินจะเข้ากระเป๋าอัตโนมัติ ภายใน 1-3 นาที",
        )
        blockers, warnings = b.lint_customer_full()
        assert blockers == []
        assert any("รับประกันเวลา" in w for w in warnings)
        assert any("อัตโนมัติ" in w for w in warnings)

    def test_internal_accounts_warn(self, content_dir) -> None:
        b = self._builder(
            content_dir,
            "ล็อกอินด้วย staff@backoffice.local หรือบัญชี UAT Zero Balance ที่ 10.0.0.5:5432",
        )
        _blockers, warnings = b.lint_customer_full()
        joined = "\n".join(warnings)
        assert "อีเมล" in joined
        assert "บัญชีทดสอบ" in joined
        assert "host:port" in joined

    def test_forbidden_words_still_block(self, content_dir) -> None:
        b = self._builder(content_dir, "พบ error ระหว่าง migration")
        blockers, _warnings = b.lint_customer_full()
        assert blockers  # unchanged contract
        assert b.lint_customer() == blockers

    def test_disclaimer_constant_names_the_pixel_gap(self) -> None:
        assert "ไม่ได้ตรวจเนื้อหาในภาพ" in rb_mod.LINT_TEXT_ONLY_DISCLAIMER


class TestSizeWarning:
    def test_over_budget_warns_and_forbids_silent_cuts(self) -> None:
        msg = size_warning("x" * 2048, max_bytes=1024)
        assert "เกินเพดาน" in msg
        assert "ห้ามตัดหัวข้อทิ้งเงียบๆ" in msg

    def test_under_budget_is_silent(self) -> None:
        assert size_warning("x" * 10, max_bytes=1024) == ""


# ── #678: dedupe key must distinguish different reports ─────────────────────


class TestNoticeIdFingerprint:
    def test_same_task_and_generation_different_reports_differ(self) -> None:
        a = make_notice_id("p", "devops", "pane-1", 1, "devops-125237.md")
        b = make_notice_id("p", "devops", "pane-1", 1, "devops-131243.md")
        assert a != b

    def test_same_report_replay_still_dedupes(self) -> None:
        a = make_notice_id("p", "devops", "t1", 1, "devops-131243.md")
        b = make_notice_id("p", "devops", "t1", 1, "devops-131243.md")
        assert a == b

    def test_two_dones_via_send_both_reach_lead(self, orch, monkeypatch, tmp_path) -> None:
        """Field repro 2026-09-19: done → follow-up via send → done again.
        The second report must NOT be eaten by the deduper."""
        monkeypatch.setattr(orch_mod, "CLOSE_ON_DONE", False)
        monkeypatch.setattr("agent_takkub.orchestrator.QTimer.singleShot", lambda ms, cb: None)
        key = _exit_key(TEST_PROJECT, "devops")
        pane = _pane("working", str(tmp_path))
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["devops"] = pane

        notices: list[str] = []
        monkeypatch.setattr(
            orch,
            "_notify_lead",
            lambda ns, body, **kw: notices.append(body),
        )

        ps = orch._ps(key)
        ps.last_assigned_task = "งานแรก"
        ps.task_delivered = True
        ok, msg = orch.done("devops", note="รายงานฉบับแรก", project=TEST_PROJECT)
        assert ok, msg

        # follow-up work handed over WITHOUT a fresh assign (send-style):
        # PaneState was popped by done(), so no new task_id exists.
        pane.state = "working"
        ps2 = orch._ps(key)
        ps2.last_assigned_task = "งานตามมา"
        ps2.task_delivered = True
        ok, msg = orch.done("devops", note="รายงานฉบับที่สอง", project=TEST_PROJECT)
        assert ok, msg

        done_notices = [n for n in notices if "done]" in n]
        assert len(done_notices) == 2, notices
        assert any("รายงานฉบับแรก" in n for n in done_notices)
        assert any("รายงานฉบับที่สอง" in n for n in done_notices)


# ── #677: done-kept pane must survive follow-up work ────────────────────────


class TestDoneKeptPaneSurvivesFollowUp:
    def test_reap_spares_pane_with_recent_send(self, orch, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(orch_mod, "DONE_PANE_TTL_S", 100.0)
        key = _exit_key(TEST_PROJECT, "devops")
        pane = _pane("done", str(tmp_path))
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["devops"] = pane
        now = time.time()
        ps = orch._ps(key)
        ps.done_kept_since = now - 1800  # long past TTL
        ps.last_send_ts = now - 30  # …but Lead sent work 30 s ago
        with patch.object(orch, "close") as close:
            orch._reap_done_panes(now)
        close.assert_not_called()

    def test_reap_spares_pane_with_recent_content_change(self, orch, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(orch_mod, "DONE_PANE_TTL_S", 100.0)
        key = _exit_key(TEST_PROJECT, "devops")
        pane = _pane("done", str(tmp_path))
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["devops"] = pane
        now = time.time()
        ps = orch._ps(key)
        ps.done_kept_since = now - 1800
        ps.last_content_change_ts = now - 10
        with patch.object(orch, "close") as close:
            orch._reap_done_panes(now)
        close.assert_not_called()

    def test_reap_still_closes_a_truly_idle_pane(self, orch, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(orch_mod, "DONE_PANE_TTL_S", 100.0)
        key = _exit_key(TEST_PROJECT, "qa")
        pane = _pane("done", str(tmp_path))
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["qa"] = pane
        now = time.time()
        ps = orch._ps(key)
        ps.done_kept_since = now - 500
        ps.last_send_ts = 0.0
        with patch.object(orch, "close", return_value=(True, "closed")) as close:
            orch._reap_done_panes(now)
        close.assert_called_once()

    def test_send_reactivates_done_pane(self, orch, monkeypatch, tmp_path) -> None:
        key = _exit_key(TEST_PROJECT, "devops")
        pane = _pane("done", str(tmp_path))
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["devops"] = pane
        ps = orch._ps(key)
        ps.done_kept_since = time.time() - 60
        monkeypatch.setattr(orch_mod, "_safe_session_write", lambda *a, **k: True)
        monkeypatch.setattr(orch_mod, "_delayed_enter_verified", lambda *a, **k: None)
        monkeypatch.setattr(orch, "_record_role_message", lambda *a, **k: "mid")
        ok, msg = orch.send("devops", "งานตามมา: ทำต่อจากรายงานเมื่อกี้", project=TEST_PROJECT)
        assert ok, msg
        assert pane.state == "working"
        assert orch._ps(key).done_kept_since == 0.0


# ── #677/#680: tombstone rows + last-progress + done (unread) ───────────────


class TestStatusTruth:
    def test_recently_exited_role_keeps_a_tombstone_row(self, orch, tmp_path) -> None:
        orch._recent_exits[f"{TEST_PROJECT}::devops"] = {
            "cwd": str(tmp_path),
            "ts": time.time() - 120,
        }
        status = orch.list_status(project=TEST_PROJECT)
        assert "devops" in status
        assert status["devops"].startswith("closed (")
        detailed = orch.list_status_detailed(project=TEST_PROJECT)
        assert detailed["devops"]["state"].startswith("closed (")

    def test_old_exits_do_not_linger_and_live_rows_win(self, orch, tmp_path) -> None:
        orch._recent_exits[f"{TEST_PROJECT}::old"] = {
            "cwd": str(tmp_path),
            "ts": time.time() - 3 * 3600,
        }
        orch._recent_exits[f"{TEST_PROJECT}::backend"] = {
            "cwd": str(tmp_path),
            "ts": time.time() - 60,
        }
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = _pane(
            "working", str(tmp_path)
        )
        status = orch.list_status(project=TEST_PROJECT)
        assert "old" not in status
        assert status["backend"] == "working"

    def test_last_progress_reported_for_non_working_panes(self, orch, tmp_path) -> None:
        key = _exit_key(TEST_PROJECT, "qa")
        pane = _pane("done", str(tmp_path))
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["qa"] = pane
        stamp = time.time() - 300
        orch._ps(key).last_content_change_ts = stamp
        detailed = orch.list_status_detailed(project=TEST_PROJECT)
        assert detailed["qa"]["last_progress_ts"] == pytest.approx(stamp)

    def test_done_unread_flag_lifecycle(self, orch, tmp_path) -> None:
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["qa"] = _pane("done", str(tmp_path))
        orch._done_unread = {(TEST_PROJECT, "qa"): time.time()}
        detailed = orch.list_status_detailed(project=TEST_PROJECT)
        assert detailed["qa"]["done_unread"] is True
        # pump wrote the notice into Lead's pane → flag clears
        orch._mark_done_notices_delivered(TEST_PROJECT, "[qa done] เสร็จแล้ว …")
        detailed = orch.list_status_detailed(project=TEST_PROJECT)
        assert detailed["qa"]["done_unread"] is False

    def test_digest_body_clears_every_role_it_names(self, orch) -> None:
        orch._done_unread = {
            (TEST_PROJECT, "qa"): time.time(),
            (TEST_PROJECT, "backend"): time.time(),
            (TEST_PROJECT, "devops"): time.time(),
        }
        orch._mark_done_notices_delivered(
            TEST_PROJECT, "สรุป: [qa done] ผ่าน · [backend FAILED] เทสแดง"
        )
        assert (TEST_PROJECT, "qa") not in orch._done_unread
        assert (TEST_PROJECT, "backend") not in orch._done_unread
        assert (TEST_PROJECT, "devops") in orch._done_unread

    def test_inbox_surfaces_report_missing_from_every_queue(self, orch) -> None:
        # unread for 5 minutes with nothing queued anywhere = delivery lost
        orch._done_unread = {(TEST_PROJECT, "devops"): time.time() - 300}
        items = orch.inbox_report(project=TEST_PROJECT)
        missing = [i for i in items if i.get("queue") == "missing"]
        assert missing and missing[0]["role"] == "devops"
        assert "#678" in missing[0]["body"]

    def test_inbox_stays_quiet_inside_debounce_window(self, orch) -> None:
        orch._done_unread = {(TEST_PROJECT, "devops"): time.time() - 10}
        items = orch.inbox_report(project=TEST_PROJECT)
        assert not [i for i in items if i.get("queue") == "missing"]
