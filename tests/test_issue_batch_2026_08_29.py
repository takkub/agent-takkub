"""Regression tests for the 2026-08-29 issue sweep:

#424 codex long-paste chunking · #427 stuck-recover chain counting ·
#428/#431 `takkub wait` (comma roles, never-spawned → non-zero, bridge-
timeout retry, post-inject terminal-reply suppression) · #429 spawn-service
· #430 lock/unlock + kill --role · #432 close on a closed role = no-op ·
#433 UI self-verify done gate.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub import auto_issue_signals as sig
from agent_takkub import cli, lead_wait, pane_guard, pty_session, resource_lock, service_spawner
from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator
from agent_takkub.orchestrator_text import UI_NO_UI_MARKER, ui_evidence_gate
from agent_takkub.provider_spec import PROVIDER_REGISTRY


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication(sys.argv[:1])
    return app


@pytest.fixture
def orch(qapp, tmp_path, monkeypatch) -> Orchestrator:
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
    monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)
    with (
        patch.object(Orchestrator, "_start_hot_md_timer", lambda self: None, create=True),
        patch("agent_takkub.orchestrator.Orchestrator._load_pending_cc", lambda self: None),
        patch(
            "agent_takkub.orchestrator.Orchestrator._start_browser_mcps",
            lambda self: None,
            create=True,
        ),
    ):
        o = Orchestrator.__new__(Orchestrator)
        QObject.__init__(o)
        o._panes_by_project = {}
        o._pane_state = {}
        o._idle_state = {}
        o._recent_exits = {}
        o._recent_done = []
        o._pending_lead_cc = {}
        o._lead_last_user_input_ts = {}
        o._lead_last_user_write_ts = {}
    return o


# ── #424 ───────────────────────────────────────────────────────────────────
class TestCodexPasteChunking:
    def test_codex_spec_chunks_and_others_do_not(self):
        assert PROVIDER_REGISTRY["codex"].paste_chunk_chars > 0
        assert PROVIDER_REGISTRY["codex"].paste_chunk_delay_ms > 0
        assert PROVIDER_REGISTRY["claude"].paste_chunk_chars == 0

    def test_split_keeps_markers_and_multibyte_chars_whole(self):
        payload = "\x1b[200~" + "ก" * 700 + "\x1b[201~"
        chunks = pty_session.split_paste_chunks(payload.encode("utf-8"), 300)
        assert len(chunks) == 3
        assert b"".join(chunks) == payload.encode("utf-8")
        assert chunks[0].startswith(b"\x1b[200~")
        assert chunks[-1].endswith(b"\x1b[201~")
        for c in chunks:
            c.decode("utf-8")  # never a split code point
            assert len(c.decode("utf-8")) <= 300 + 6

    def test_short_or_disabled_is_passthrough(self):
        assert pty_session.split_paste_chunks(b"hello", 300) == [b"hello"]
        assert pty_session.split_paste_chunks(b"x" * 1000, 0) == [b"x" * 1000]

    def test_session_set_paste_chunking_before_writer_exists(self):
        s = pty_session.PtySession.__new__(pty_session.PtySession)
        s._writer = None
        s.set_paste_chunking(300, 60)
        assert s._paste_chunking == (300, 60)


# ── #427 ───────────────────────────────────────────────────────────────────
def _log(path: Path, records: list[dict]) -> Path:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8"
    )
    return path


class TestStuckRecoverChains:
    def test_one_pane_recover_chain_counts_once(self, tmp_path):
        now = datetime(2026, 8, 28, 19, 10, 0)
        recs = [
            {
                "ts": (now - timedelta(minutes=m)).isoformat(),
                "event": "stuck_pane_recover",
                "role": "devops",
                "project": "saas_admin",
            }
            for m in (28, 18, 8)  # the live #427 shape: 10 min apart
        ]
        assert sig.scan_for_signals(_log(tmp_path / "e.log", recs), now=now) == []

    def test_three_distinct_panes_still_fire(self, tmp_path):
        now = datetime(2026, 8, 28, 22, 0, 0)
        recs = [
            {
                "ts": (now - timedelta(minutes=m)).isoformat(),
                "event": "stuck_pane_recover",
                "role": role,
                "project": "saas_admin",
            }
            for m, role in (
                (200, "devops"),
                (190, "devops"),
                (180, "devops"),
                (150, "reviewer"),
                (55, "qa"),
            )
        ]
        hits = sig.scan_for_signals(_log(tmp_path / "e.log", recs), now=now)
        assert [h.rule.key for h in hits] == ["stuck_pane_recover"]
        assert hits[0].count == 3  # devops chain + reviewer + qa


# ── #428 / #431 wait ──────────────────────────────────────────────────────
class TestWaitRoles:
    def test_comma_and_repeat_forms_split(self):
        assert cli._split_role_args(["devops,backend", "frontend", " qa , devops"]) == [
            "devops",
            "backend",
            "frontend",
            "qa",
        ]
        assert cli._split_role_args(None) == []

    def test_gone_constant_pinned_between_cli_and_server(self):
        assert cli._WAIT_GONE_NEVER_SPAWNED == lead_wait._GONE_NEVER_SPAWNED
        assert lead_wait._GONE_NEVER_SPAWNED in lead_wait._GONE_NEVER_SPAWNED_DETAIL

    def test_never_spawned_role_is_not_success(self, monkeypatch):
        calls = []

        def _fake_request(payload, **kw):
            calls.append(payload)
            if payload["cmd"] == "wait-begin":
                return {"ok": True, "wait_id": "w1", "roles": payload["roles"]}
            if payload["cmd"] == "wait-poll":
                return {
                    "ok": True,
                    "pending": {},
                    "done": {},
                    "failed": {},
                    "gone": {"devops,backend": lead_wait._GONE_NEVER_SPAWNED_DETAIL},
                    "elapsed": 1,
                }
            return {"ok": True}

        monkeypatch.setattr(cli, "_request", _fake_request)
        monkeypatch.setattr(cli.time, "sleep", lambda s: None)
        args = argparse.Namespace(
            role=["devops,backend"], timeout=60, cancel=False, no_interrupt=False
        )
        # simulate a server that did not split (older cockpit): role stays literal
        out = cli.cmd_wait(args)
        assert calls[0]["roles"] == ["devops", "backend"]  # CLI split it
        assert out["ok"] is False
        assert out["exit_code"] == cli._WAIT_EXIT_ERROR
        assert "role ไม่พบ" in out["msg"]

    def test_bridge_timeout_is_retried_then_errors_with_exit_2(self, monkeypatch):
        n = {"calls": 0}

        def _fake_request(payload, **kw):
            n["calls"] += 1
            return cli._timeout_response(15.0)

        monkeypatch.setattr(cli, "_request", _fake_request)
        monkeypatch.setattr(cli.time, "sleep", lambda s: None)
        args = argparse.Namespace(role=["frontend"], timeout=60, cancel=False, no_interrupt=False)
        out = cli.cmd_wait(args)
        assert out["ok"] is False
        assert out["exit_code"] == cli._WAIT_EXIT_ERROR
        assert n["calls"] == 1 + cli._WAIT_BRIDGE_RETRIES

    def test_begin_wait_splits_commas_server_side(self, orch, monkeypatch):
        orch._active_waits = {}
        monkeypatch.setattr(orch, "list_status", lambda project=None: {}, raising=False)
        out = orch.begin_wait("proj", ["devops,backend"], 60.0)
        assert out["ok"] is True
        assert out["roles"] == ["devops", "backend"]


# ── #449 ───────────────────────────────────────────────────────────────────
class TestAmbiguousUserInputInterruptAutoResumes:
    """#449: a `user_input` interrupt whose stamped chunk had no real text
    left after stripping recognizable escape sequences (`printable: False`
    — a digest-triggered terminal echo the #357/#420/#428/#431 denylist
    didn't catch) must never end a multi-role wait as an error. The role
    that already resolved is reported live; the rest keep being watched."""

    def test_ambiguous_echo_auto_resumes_instead_of_erroring(self, monkeypatch):
        calls: list[dict] = []
        state = {"n": 0}

        def _fake_request(payload, **kw):
            calls.append(payload)
            cmd = payload["cmd"]
            if cmd == "wait-begin":
                wid = "w1" if state["n"] == 0 else "w2"
                return {"ok": True, "wait_id": wid, "roles": payload["roles"]}
            if cmd == "wait-poll":
                state["n"] += 1
                if state["n"] == 1:
                    return {
                        "ok": True,
                        "pending": {"backend": "working", "frontend": "working"},
                        "done": {},
                        "failed": {},
                        "gone": {},
                        "elapsed": 1,
                        "expired": False,
                        "interrupt": None,
                    }
                if state["n"] == 2:
                    # backend just resolved; the SAME poll tick also carries
                    # an ambiguous (non-printable) user_input interrupt —
                    # exactly the #449 incident shape.
                    return {
                        "ok": True,
                        "pending": {"frontend": "working"},
                        "done": {"backend": "delivered"},
                        "failed": {},
                        "gone": {},
                        "elapsed": 5,
                        "expired": False,
                        "interrupt": {
                            "role": "lead",
                            "detail": "มี byte แปลกๆ เข้ามาที่ pane ระหว่างรอ ไม่ใช่ข้อความที่คุณพิมพ์",
                            "reason": "user_input",
                            "printable": False,
                        },
                    }
                return {
                    "ok": True,
                    "pending": {},
                    "done": {"frontend": "delivered"},
                    "failed": {},
                    "gone": {},
                    "elapsed": 8,
                    "expired": False,
                    "interrupt": None,
                }
            return {"ok": True}

        monkeypatch.setattr(cli, "_request", _fake_request)
        monkeypatch.setattr(cli, "_request_with_retry", _fake_request)
        monkeypatch.setattr(cli.time, "sleep", lambda s: None)
        args = argparse.Namespace(
            role=["backend", "frontend"], timeout=60, cancel=False, no_interrupt=False
        )
        out = cli.cmd_wait(args)

        assert out["ok"] is True, "must resolve cleanly, not as an interrupted error"
        assert out["exit_code"] == 0
        assert out["interrupt"] is None, (
            "the ambiguous echo must not surface as the final interrupt"
        )
        # A fresh wait-begin was issued for the still-pending role(s) only.
        rebegins = [c for c in calls if c["cmd"] == "wait-begin"]
        assert len(rebegins) == 2
        assert rebegins[1]["roles"] == ["frontend"]

    def test_confirmed_typing_still_stops_the_wait(self, monkeypatch):
        """A `printable: True` interrupt (confirmed real typing) must keep
        stopping the wait exactly as before — only the ambiguous case rides
        out automatically."""

        def _fake_request(payload, **kw):
            cmd = payload["cmd"]
            if cmd == "wait-begin":
                return {"ok": True, "wait_id": "w1", "roles": payload["roles"]}
            if cmd == "wait-poll":
                return {
                    "ok": True,
                    "pending": {"frontend": "working"},
                    "done": {},
                    "failed": {},
                    "gone": {},
                    "elapsed": 3,
                    "expired": False,
                    "interrupt": {
                        "role": "lead",
                        "detail": "มีข้อความ/คำสั่งใหม่จากคุณเข้ามาระหว่างที่ wait กำลังรออยู่",
                        "reason": "user_input",
                        "printable": True,
                    },
                }
            return {"ok": True}

        monkeypatch.setattr(cli, "_request", _fake_request)
        monkeypatch.setattr(cli, "_request_with_retry", _fake_request)
        monkeypatch.setattr(cli.time, "sleep", lambda s: None)
        args = argparse.Namespace(role=["frontend"], timeout=60, cancel=False, no_interrupt=False)
        out = cli.cmd_wait(args)

        assert out["ok"] is False
        assert out["exit_code"] == 1
        assert out["interrupt"] is not None
        assert "interrupted by user input" in out["msg"]


def _fake_pane(last_write_ts: float = 0.0, last_output_ts: float = 0.0):
    """`pane`-shaped double for `_is_post_inject_terminal_reply` (#498): only
    `.session.last_write_ts` and `._last_output_ts` are read. Both default to
    0.0 (not a fresh MagicMock, which would auto-vivify a non-float attribute
    and break the `float(...)` conversion) so a test only needs to set the
    one signal it cares about."""
    pane = MagicMock()
    pane.session = MagicMock()
    pane.session.last_write_ts = last_write_ts
    pane._last_output_ts = last_output_ts
    return pane


class TestPostInjectTerminalReply:
    def test_esc_chunk_right_after_engine_write_is_not_user_input(self, orch):
        now = time.time()
        pane = _fake_pane(last_write_ts=now)  # engine just pasted a digest
        orch._lead_last_user_write_ts["proj"] = now - 30
        assert orch._is_post_inject_terminal_reply("proj", pane, b"\x1b[?1;2c")

    def test_printable_or_enter_always_counts(self, orch):
        now = time.time()
        pane = _fake_pane(last_write_ts=now)
        orch._lead_last_user_write_ts["proj"] = now - 30
        assert not orch._is_post_inject_terminal_reply("proj", pane, b"hello")
        assert not orch._is_post_inject_terminal_reply("proj", pane, b"\x1b\r")

    def test_esc_chunk_after_users_own_keystroke_counts(self, orch):
        last_write = time.time() - 1
        pane = _fake_pane(last_write_ts=last_write)
        orch._lead_last_user_write_ts["proj"] = last_write + 0.5  # owner typed last
        assert not orch._is_post_inject_terminal_reply("proj", pane, b"\x1b[D")

    def test_grace_window_expires(self, orch):
        pane = _fake_pane(last_write_ts=time.time() - orch._LEAD_INJECT_GRACE_S - 1)
        orch._lead_last_user_write_ts["proj"] = 0.0
        assert not orch._is_post_inject_terminal_reply("proj", pane, b"\x1b[D")

    def test_recent_output_outside_write_grace_still_counts(self, orch):
        """#498: the target CLI can query the terminal well after OUR own
        paste — e.g. finishing a long reply to a remote-delivered message —
        not only in the few seconds right after we wrote into the pty.
        `pane._last_output_ts` (bumped on every raw byte the pty emits,
        regardless of who caused it) must catch that case too."""
        pane = _fake_pane(
            last_write_ts=time.time() - orch._LEAD_INJECT_GRACE_S - 30,  # long past write-grace
            last_output_ts=time.time(),  # but the pane JUST emitted output
        )
        orch._lead_last_user_write_ts["proj"] = 0.0
        assert orch._is_post_inject_terminal_reply("proj", pane, b"\x1b[?1;2c")

    def test_stale_output_outside_grace_does_not_count(self, orch):
        pane = _fake_pane(
            last_write_ts=0.0,
            last_output_ts=time.time() - orch._LEAD_INJECT_GRACE_S - 1,
        )
        orch._lead_last_user_write_ts["proj"] = 0.0
        assert not orch._is_post_inject_terminal_reply("proj", pane, b"\x1b[D")

    def test_output_before_users_own_keystroke_does_not_count(self, orch):
        """Same guard as the write-based check: output that predates the
        owner's own last keystroke must never outrank it."""
        last_output = time.time() - 1
        pane = _fake_pane(last_write_ts=0.0, last_output_ts=last_output)
        orch._lead_last_user_write_ts["proj"] = last_output + 0.5
        assert not orch._is_post_inject_terminal_reply("proj", pane, b"\x1b[D")


# ── #432 ───────────────────────────────────────────────────────────────────
class TestCloseClosedRole:
    def test_known_role_without_pane_is_noop_ok(self, orch, monkeypatch):
        monkeypatch.setattr(orch, "_resolve_project", lambda p: "proj", raising=False)
        orch._panes_by_project = {"proj": {}}
        monkeypatch.setattr(orch, "_project_panes", lambda ns: {}, raising=False)
        orch._resource_governor = None
        ok, msg = orch.close("frontend#1", project="proj")
        assert ok is True
        assert "no-op" in msg

    def test_unknown_role_still_errors(self, orch, monkeypatch):
        monkeypatch.setattr(orch, "_resolve_project", lambda p: "proj", raising=False)
        monkeypatch.setattr(orch, "_project_panes", lambda ns: {}, raising=False)
        orch._resource_governor = None
        monkeypatch.setattr(
            orch, "_unknown_pane_message", lambda r, p: f"unknown role: {r}", raising=False
        )
        ok, msg = orch.close("frontnd", project="proj")
        assert ok is False
        assert "unknown role" in msg


# ── #433 ───────────────────────────────────────────────────────────────────
class TestUiEvidenceGate:
    def test_non_ui_role_never_gated(self):
        assert ui_evidence_gate("backend", "done", "แก้หน้า login", None) is None

    def test_non_ui_task_not_gated(self):
        assert ui_evidence_gate("frontend", "refactor types", "rename util fn", None) is None

    def test_ui_task_without_screenshot_rejected(self):
        msg = ui_evidence_gate(
            "frontend", "แก้ responsive เสร็จแล้ว", "แก้หน้า member ให้ responsive", None
        )
        assert msg and "#433" in msg and "screenshot" in msg

    def test_admits_unverified_rejected_even_without_task_text(self):
        msg = ui_evidence_gate(
            "mobile", "เสร็จแล้ว ยังไม่ได้เปิด browser จริง แนะนำ route ไป qa", None, None
        )
        assert msg and "self-verify" in msg

    def test_no_ui_marker_opts_out(self):
        assert (
            ui_evidence_gate("frontend", f"{UI_NO_UI_MARKER} pure logic", "หน้า login", None) is None
        )

    def test_existing_screenshot_passes(self, tmp_path):
        shot = tmp_path / "member-390.png"
        shot.write_bytes(b"\x89PNG")
        note = f"responsive fix เสร็จ\n{shot}\n"
        assert ui_evidence_gate("frontend", note, "แก้หน้า member responsive", None) is None

    def test_relative_screenshot_resolves_against_cwd(self, tmp_path):
        (tmp_path / "shots").mkdir()
        (tmp_path / "shots" / "a.png").write_bytes(b"x")
        note = "done — shots/a.png"
        assert ui_evidence_gate("frontend", note, "แก้ปุ่ม", str(tmp_path)) is None
        assert ui_evidence_gate("frontend", note, "แก้ปุ่ม", str(tmp_path / "elsewhere")) is not None

    def test_frontend_and_mobile_are_browser_roles(self):
        assert pane_guard.is_browser_role("frontend")
        assert pane_guard.is_browser_role("mobile#2")
        assert pane_guard.UI_SELF_VERIFY_ROLES == {"frontend", "mobile"}

    def test_tiny_scope_style_text_diff_exempts_screenshot(self, monkeypatch):
        from agent_takkub import orchestrator_text

        monkeypatch.setattr(
            orchestrator_text,
            "is_tiny_style_or_text_diff",
            lambda cwd, git_numstat_fn=None, **kw: True,
        )
        # Even without screenshot and with UI task text, tiny style/text diff is exempt
        msg = ui_evidence_gate("frontend", "แก้ padding ปุ่ม", "แก้หน้า member UI", None, scope="tiny")
        assert msg is None

    def test_is_tiny_style_or_text_diff_shared_tree_isolation(self):
        from agent_takkub.orchestrator_text import is_tiny_style_or_text_diff

        # Shared tree where diff contains 500 lines from other pane (backend.py),
        # but this pane only touched button.css (3 lines).
        fake_numstat = [
            (3, 0, "src/styles/button.css"),
            (400, 100, "src/backend/server.py"),
        ]
        # When touched_files isolates this pane to button.css:
        assert (
            is_tiny_style_or_text_diff(
                None,
                git_numstat_fn=lambda _: fake_numstat,
                touched_files=["src/styles/button.css"],
                is_worktree=False,
            )
            is True
        )

        # When touched_files includes non-style/text code:
        assert (
            is_tiny_style_or_text_diff(
                None,
                git_numstat_fn=lambda _: fake_numstat,
                touched_files=["src/styles/button.css", "src/backend/server.py"],
                is_worktree=False,
            )
            is False
        )

        # Fail closed: in shared tree without touched_files, cannot isolate -> False
        assert (
            is_tiny_style_or_text_diff(
                None,
                git_numstat_fn=lambda _: fake_numstat,
                touched_files=None,
                is_worktree=False,
            )
            is False
        )

        # In worktree isolation, all changes belong to this pane:
        clean_numstat = [(2, 1, "src/styles/button.css")]
        assert (
            is_tiny_style_or_text_diff(
                None,
                git_numstat_fn=lambda _: clean_numstat,
                touched_files=None,
                is_worktree=True,
            )
            is True
        )

    def test_tiny_scope_single_screenshot_passes_even_for_responsive(self, tmp_path):
        # When no screenshots are provided:
        # For normal scope, rejection message specifies dual-viewport (390px + 1440px)
        msg_normal = ui_evidence_gate(
            "frontend",
            "fix responsive แล้ว",
            "แก้หน้า member ให้ responsive",
            str(tmp_path),
            scope="normal",
        )
        assert msg_normal and "mobile 390px + desktop 1440px" in msg_normal

        # For tiny scope, rejection message asks for at least 1 screenshot (not dual-viewport)
        msg_tiny_no_shot = ui_evidence_gate(
            "frontend",
            "fix responsive แล้ว",
            "แก้หน้า member ให้ responsive",
            str(tmp_path),
            scope="tiny",
        )
        assert msg_tiny_no_shot and "อย่างน้อย 1 screenshot" in msg_tiny_no_shot
        assert "1440px" not in msg_tiny_no_shot

        # When 1 screenshot is provided and exists, tiny scope passes
        shot = tmp_path / "member.png"
        shot.write_bytes(b"\x89PNG")
        note = f"fix เสร็จ\n{shot}\n"
        assert (
            ui_evidence_gate(
                "frontend", note, "แก้หน้า member ให้ responsive", str(tmp_path), scope="tiny"
            )
            is None
        )


# ── #430 ───────────────────────────────────────────────────────────────────
class TestResourceLock:
    def test_acquire_release_roundtrip(self, tmp_path):
        ok, info = resource_lock.try_acquire(tmp_path, "proj", "web-build", "devops")
        assert ok and info.holder == "devops"
        ok2, other = resource_lock.try_acquire(tmp_path, "proj", "web-build", "qa")
        assert not ok2 and other.holder == "devops"
        ok3, msg = resource_lock.release(tmp_path, "proj", "web-build", "qa")
        assert not ok3 and "devops" in msg
        ok4, _ = resource_lock.release(tmp_path, "proj", "web-build", "devops")
        assert ok4
        assert resource_lock.list_locks(tmp_path, "proj") == []

    def test_stale_lock_is_reclaimed(self, tmp_path):
        resource_lock.try_acquire(tmp_path, "proj", "db", "backend", ttl_s=1, now=time.time() - 10)
        ok, info = resource_lock.try_acquire(tmp_path, "proj", "db", "qa")
        assert ok and info.holder == "qa"

    def test_wait_polls_until_free(self, tmp_path):
        resource_lock.try_acquire(tmp_path, "proj", "x", "a")
        sleeps = []

        def _sleep(s):
            sleeps.append(s)
            resource_lock.release(tmp_path, "proj", "x", "a")

        ok, info, _waited = resource_lock.acquire(
            tmp_path, "proj", "x", "b", wait_s=10, sleep=_sleep
        )
        assert ok and info.holder == "b" and sleeps

    def test_bad_name_rejected(self, tmp_path):
        with pytest.raises(resource_lock.LockError):
            resource_lock.try_acquire(tmp_path, "proj", "../x", "a")

    def test_cli_lock_uses_role_and_project(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli.config, "RUNTIME_DIR", tmp_path)
        monkeypatch.setenv("TAKKUB_ROLE", "devops")
        monkeypatch.setenv("TAKKUB_PROJECT", "tunnel")
        out = cli.cmd_lock(
            argparse.Namespace(name="web-build", wait=0, ttl=None, note="", list=False)
        )
        assert out["ok"]
        monkeypatch.setenv("TAKKUB_ROLE", "qa")
        out2 = cli.cmd_lock(
            argparse.Namespace(name="web-build", wait=0, ttl=None, note="", list=False)
        )
        assert out2["ok"] is False and "devops" in out2["msg"] and out2["exit_code"] == 3
        assert cli.cmd_unlock(argparse.Namespace(name="web-build", force=False))["ok"] is False
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        assert cli.cmd_unlock(argparse.Namespace(name="web-build", force=True))["ok"] is True

    def test_kill_is_lead_only(self):
        assert "kill" in cli.LEAD_ONLY_COMMANDS
        assert "service-stop" in cli.LEAD_ONLY_COMMANDS


class TestKillPaneChildren:
    def test_no_pane(self, orch, monkeypatch):
        monkeypatch.setattr(orch, "_resolve_project", lambda p: "proj", raising=False)
        monkeypatch.setattr(orch, "_project_panes", lambda ns: {}, raising=False)
        ok, msg = orch.kill_pane_children("devops", project="proj")
        assert not ok and "no live pane" in msg

    def test_pid_outside_pane_tree_refused(self, orch, monkeypatch):
        monkeypatch.setattr(orch, "_resolve_project", lambda p: "proj", raising=False)
        pane = MagicMock()
        pane.session._pid = 999999999
        monkeypatch.setattr(orch, "_project_panes", lambda ns: {"devops": pane}, raising=False)
        fake_psutil = MagicMock()
        fake_psutil.Process.return_value.children.return_value = []
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
        ok, msg = orch.kill_pane_children("devops", project="proj", pid=4242)
        assert not ok and "refusing" in msg

    def test_kill_pid_terminates_full_descendant_tree_797(self, orch, monkeypatch):
        monkeypatch.setattr(orch, "_resolve_project", lambda p: "proj", raising=False)
        pane = MagicMock()
        pane.session._pid = 1000
        monkeypatch.setattr(orch, "_project_panes", lambda ns: {"devops": pane}, raising=False)

        proc_pwsh = MagicMock()
        proc_pwsh.pid = 10560
        proc_pwsh.name.return_value = "pwsh.exe"
        proc_pwsh.parents.return_value = [pane.session._pid]

        proc_docker = MagicMock()
        proc_docker.pid = 19712
        proc_docker.name.return_value = "docker.exe"
        proc_docker.parents.return_value = [proc_pwsh, pane.session._pid]

        proc_pwsh.children.return_value = [proc_docker]

        fake_psutil = MagicMock()
        fake_psutil.Process.return_value.children.return_value = [proc_pwsh, proc_docker]
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

        ok, msg = orch.kill_pane_children("devops", project="proj", pid=10560)
        assert ok is True
        assert "pwsh.exe(10560)" in msg
        assert "docker.exe(19712)" in msg
        assert proc_pwsh.kill.called
        assert proc_docker.kill.called

    def test_kill_orphan_after_parent_gone_797(self, orch, monkeypatch):
        monkeypatch.setattr(orch, "_resolve_project", lambda p: "proj", raising=False)
        pane = MagicMock()
        pane.session._pid = 1000
        monkeypatch.setattr(orch, "_project_panes", lambda ns: {"devops": pane}, raising=False)

        # Populate seen_set
        seen = orch.__dict__.setdefault("_pane_child_pids", {}).setdefault("proj::devops", set())
        seen.add(19712)

        proc_docker = MagicMock()
        proc_docker.pid = 19712
        proc_docker.name.return_value = "docker.exe"
        proc_docker.parents.return_value = []
        proc_docker.children.return_value = []

        fake_psutil = MagicMock()
        # root process no longer has docker as a direct descendant (orphan)
        fake_psutil.Process.side_effect = lambda pid: (
            proc_docker if pid == 19712 else MagicMock(children=lambda **kw: [])
        )
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

        ok, msg = orch.kill_pane_children("devops", project="proj", pid=19712)
        assert ok is True
        assert "docker.exe(19712)" in msg
        assert proc_docker.kill.called


# ── #429 ───────────────────────────────────────────────────────────────────
class TestSpawnService:
    def test_spawn_survives_and_is_registered_then_stopped(self, tmp_path):
        rec = service_spawner.spawn(
            tmp_path,
            "proj",
            "sleeper",
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=str(tmp_path),
            by_role="devops",
        )
        try:
            assert rec.pid > 0
            assert Path(rec.log_path).is_file()
            assert rec.pid in service_spawner.registered_pids(tmp_path)
            rows = service_spawner.list_services(tmp_path, "proj")
            assert rows and rows[0]["name"] == "sleeper" and rows[0]["alive"]
        finally:
            ok, msg = service_spawner.stop(tmp_path, "proj", "sleeper")
        assert ok, msg
        assert service_spawner.list_services(tmp_path, "proj") == []

    def test_bad_inputs(self, tmp_path):
        with pytest.raises(service_spawner.ServiceSpawnError):
            service_spawner.spawn(tmp_path, "p", "bad name!", ["x"], cwd=None, by_role="a")
        with pytest.raises(service_spawner.ServiceSpawnError):
            service_spawner.spawn(tmp_path, "p", "ok", [], cwd=None, by_role="a")
        with pytest.raises(service_spawner.ServiceSpawnError):
            service_spawner.spawn(
                tmp_path, "p", "ok", ["x"], cwd=str(tmp_path / "nope"), by_role="a"
            )

    def test_child_env_drops_pane_identity(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TAKKUB_ROLE", "devops")
        monkeypatch.setenv("TAKKUB_PANE_TOKEN", "tok")
        out = tmp_path / "env.txt"
        code = (
            "import os,sys;open(sys.argv[1],'w').write("
            "str(sorted(k for k in os.environ if k.startswith('TAKKUB_'))))"
        )
        service_spawner.spawn(
            tmp_path,
            "p",
            "envdump",
            [sys.executable, "-c", code, str(out)],
            cwd=str(tmp_path),
            by_role="devops",
        )
        for _ in range(100):
            if out.is_file() and out.read_text():
                break
            time.sleep(0.05)
        service_spawner.stop(tmp_path, "p", "envdump")
        text = out.read_text()
        assert "TAKKUB_ROLE" not in text and "TAKKUB_PANE_TOKEN" not in text
        assert "TAKKUB_SERVICE" in text

    def test_cli_spawn_service_builds_request(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            cli, "_request", lambda p, **k: seen.update(p) or {"ok": True, "msg": "x"}
        )
        args = argparse.Namespace(
            name=None, cwd="/w", list=False, service_argv=["--", "cloudflared", "tunnel", "run"]
        )
        cli.cmd_spawn_service(args)
        assert seen["cmd"] == "spawn-service"
        assert seen["argv"] == ["cloudflared", "tunnel", "run"]
        assert seen["name"] == "cloudflared"
        assert seen["cwd"] == "/w"

    def test_cli_main_spawn_service_survives_role_gate(self, monkeypatch):
        """#483 regression: the top-level `add_subparsers(dest="command")`
        used to collide with spawn-service's own positional named "command"
        (argparse.REMAINDER) — by the time `_enforce_role_gate(args.command)`
        ran in `main()`, `args.command` had been overwritten with the
        service's argv list instead of staying "spawn-service", crashing
        every invocation with `TypeError: unhashable type: 'list'` on the
        `in LEAD_ONLY_COMMANDS` check. Exercise the real argparse path (not
        cmd_spawn_service directly) with a non-lead role, since that's
        exactly the path `_enforce_role_gate` runs on."""
        monkeypatch.setenv("TAKKUB_ROLE", "devops")
        seen = {}
        monkeypatch.setattr(
            cli, "_request", lambda p, **k: seen.update(p) or {"ok": True, "msg": "started"}
        )
        rc = cli.main(["spawn-service", "--name", "docker", "--", "docker", "desktop"])
        assert rc == 0
        assert seen["cmd"] == "spawn-service"
        assert seen["argv"] == ["docker", "desktop"]
        assert seen["name"] == "docker"


# ── #794 ───────────────────────────────────────────────────────────────────
class TestCliPositionalRole794:
    def test_close_supports_positional_and_flag(self):
        parser = cli.build_parser()

        # Positional
        args = parser.parse_args(["close", "backend"])
        role, err = cli._resolve_role_arg(args)
        assert role == "backend" and err is None

        # Named flag
        args = parser.parse_args(["close", "--role", "backend"])
        role, err = cli._resolve_role_arg(args)
        assert role == "backend" and err is None

        # Missing role
        args = parser.parse_args(["close"])
        role, err = cli._resolve_role_arg(args)
        assert role is None and "role is required" in err

        # Conflicting roles
        args = parser.parse_args(["close", "backend", "--role", "frontend"])
        role, err = cli._resolve_role_arg(args)
        assert role is None and "conflicting roles" in err

    def test_tail_supports_positional_and_flag(self):
        parser = cli.build_parser()

        args = parser.parse_args(["tail", "codex", "-n", "35"])
        role, err = cli._resolve_role_arg(args)
        assert role == "codex" and err is None
        assert args.lines == 35

        args = parser.parse_args(["tail", "--role", "codex"])
        role, err = cli._resolve_role_arg(args)
        assert role == "codex" and err is None

    def test_kill_supports_positional_and_flag(self):
        parser = cli.build_parser()

        args = parser.parse_args(["kill", "qa", "--pid", "5432"])
        role, err = cli._resolve_role_arg(args)
        assert role == "qa" and err is None
        assert args.pid == 5432

        args = parser.parse_args(["kill", "--role", "qa"])
        role, err = cli._resolve_role_arg(args)
        assert role == "qa" and err is None

    def test_cmd_dispatch_positional(self, monkeypatch):
        parser = cli.build_parser()
        recorded = []
        monkeypatch.setattr(
            cli, "_request", lambda p, **k: recorded.append(p) or {"ok": True, "msg": "done"}
        )
        monkeypatch.setattr(cli, "_from_role", lambda: "lead")

        # close
        args = parser.parse_args(["close", "backend"])
        res = args.func(args)
        assert res["ok"] is True
        assert recorded[-1]["cmd"] == "close"
        assert recorded[-1]["role"] == "backend"

        # kill
        args = parser.parse_args(["kill", "devops", "--pid", "99"])
        res = args.func(args)
        assert res["ok"] is True
        assert recorded[-1]["cmd"] == "kill"
        assert recorded[-1]["role"] == "devops"
        assert recorded[-1]["pid"] == 99

        # tail
        args = parser.parse_args(["tail", "reviewer", "-n", "10"])
        res = args.func(args)
        assert res["ok"] is True
        assert recorded[-1]["cmd"] == "tail"
        assert recorded[-1]["role"] == "reviewer"
        assert recorded[-1]["lines"] == 10


# ── #795 ───────────────────────────────────────────────────────────────────
class TestBoundTaskIdQueue795:
    def test_extract_cited_task_ids(self):
        from agent_takkub.orchestrator_text import extract_cited_task_ids

        text = "PASS [ใบงานใหม่ · task 1234abcd] verified and tests pass"
        assert extract_cited_task_ids(text) == {"1234abcd"}

        text2 = "[task feedbeef] ok\nOther note: [task cafe0001]"
        assert extract_cited_task_ids(text2) == {"feedbeef", "cafe0001"}

    def test_bare_project_ids_are_not_task_citations_811(self):
        from agent_takkub.orchestrator_text import extract_cited_task_ids

        note = (
            "[task 13c947f8] STATUS: PASS. EVIDENCE (prod, task 08baf710-1c2d-4e5f period), "
            "task: a9b63290 re-run, task=480fc77f"
        )
        assert extract_cited_task_ids(note) == {"13c947f8"}
        assert extract_cited_task_ids("task 08baf710 (kex-offline) done") == set()

    def test_qa_zero_files_not_stale(self):
        from agent_takkub.orchestrator_text import stale_done_reasons

        reasons = stale_done_reasons(
            "All 45 tests pass without regressions",
            task_id="task1",
            files_touched=0,
            elapsed_s=120,
            implementation=False,  # qa / review role
        )
        assert not any("files" in r for r in reasons)

    def test_task_show_info_with_pending_respawn_and_queue(self):
        from unittest.mock import MagicMock

        from agent_takkub.orchestrator import Orchestrator

        orch = MagicMock(spec=Orchestrator)
        orch.resolve_pane_role = lambda r, p: r
        orch._resolve_project = lambda p: "proj"
        orch._pane_state = {}
        orch._pending_respawn_tasks = {"proj::qa": {"task": "Pending Task B", "task_file": None}}
        orch._pending_assignments = {}

        # 1. Shows pending respawn task
        ok, tag, data = Orchestrator.task_show_info(orch, "qa", "proj")
        assert ok is True
        assert data["task"] == "Pending Task B"

        # 2. When no pending respawn, shows queued task
        orch._pending_respawn_tasks.clear()
        orch._pending_assignments = {"proj::qa": [{"task": "Queued Task C"}]}
        ok, tag, data = Orchestrator.task_show_info(orch, "qa", "proj")
        assert ok is True
        assert tag == "queued task"
        assert data["task"] == "Queued Task C"

    def test_done_cites_older_task_leaves_active_task_intact(self, tmp_path):
        from unittest.mock import MagicMock

        from agent_takkub.orchestrator import Orchestrator
        from agent_takkub.spawn_engine import PaneState

        orch = MagicMock(spec=Orchestrator)
        orch.resolve_pane_role = lambda r, p: r
        orch._resolve_project = lambda p: "proj"
        orch._notices = []
        orch._notify_lead = lambda proj, msg, **k: orch._notices.append(msg)
        orch._save_decision_note = MagicMock(return_value="/tmp/note.md")
        orch._evidence_dedup_gate = MagicMock(return_value=None)
        orch._last_evidence_dedup_warning = None
        orch._pane_reports_undelivered_task = MagicMock(return_value=False)
        orch._current_pane_identity = MagicMock(return_value="tok")

        ps = PaneState()
        ps.task_id = "feedbeef"
        ps.task_delivered = True
        ps.last_assigned_task = "Task B"
        orch._pane_state = {"proj::qa": ps}
        orch._ps = lambda k: orch._pane_state.setdefault(k, PaneState())
        pane = MagicMock()
        pane.state = "running"
        pane.session = MagicMock()
        pane.session.is_alive = True
        orch._project_panes = lambda proj: {"qa": pane}

        # Call done with note citing older task cafe0001
        ok, _msg = Orchestrator.done(
            orch,
            "qa",
            "PASS [task cafe0001] previous task completed",
            project="proj",
        )
        assert ok is True
        # Check active feedbeef is still active and pane is NOT set to done
        assert ps.task_id == "feedbeef"
        assert pane.set_state.call_count == 0
        assert any("cafe0001" in n for n in orch._notices)

    def test_queue_dispatch_during_post_done_respawn(self):
        from unittest.mock import MagicMock

        from agent_takkub.orchestrator import Orchestrator

        orch = MagicMock(spec=Orchestrator)
        orch.resolve_pane_role = lambda r, p: r
        orch._resolve_project = lambda p: "proj"
        orch._pane_state = {}
        pane = MagicMock()
        pane.state = "done"
        pane.session = MagicMock()
        pane.session.is_alive = True
        orch._project_panes = lambda p: {"qa": pane}
        orch._pending_assignments = {}
        orch._pending_respawn_tasks = {}
        orch._notify_lead = MagicMock()
        orch.close = MagicMock()

        # Mock _assign_dispatch to call real _assign_dispatch or simulate the post_done_respawn branch
        orch._assign_dispatch = lambda *args, **kwargs: Orchestrator._assign_dispatch(
            orch, *args, **kwargs
        )
        orch._pane_idle_for_reassign = MagicMock(return_value=False)

        item = {
            "role_name": "qa",
            "cwd": "/some/cwd",
            "task": "Task B payload",
            "_queued_task_id": "bbbb0002",
            "project": "proj",
        }
        orch._pending_assignments["proj::qa"] = [item]

        res = Orchestrator._dispatch_next_assignment(orch, "proj", "qa")
        assert res is True
        assert "proj::qa" in orch._pending_respawn_tasks
        assert orch._pending_respawn_tasks["proj::qa"]["task"] == "Task B payload"

        # task_show_info returns Task B!
        ok, _tag, data = Orchestrator.task_show_info(orch, "qa", "proj")
        assert ok is True
        assert data["task"] == "Task B payload"


# ── #791 & #798: Quota Policy, Exclude Providers & Low-Quota Check ─────────
class TestQuotaPolicyAndExclude791_798:
    def test_quota_policy_and_exclude_persistence(self, tmp_path, monkeypatch):
        from agent_takkub import auto_resume

        monkeypatch.setattr(
            auto_resume, "_quota_policy_path", lambda: tmp_path / "quota-policy.json"
        )

        # Default policy is reroute
        assert auto_resume.quota_policy() == auto_resume.QUOTA_POLICY_REROUTE

        # Set to park
        auto_resume.set_quota_policy(auto_resume.QUOTA_POLICY_PARK)
        assert auto_resume.quota_policy() == auto_resume.QUOTA_POLICY_PARK

        # Exclude providers
        assert auto_resume.quota_exclude_providers() == set()
        auto_resume.set_quota_exclude_providers(["codex", "cursor"])
        assert auto_resume.quota_exclude_providers() == {"codex", "cursor"}

    def test_reroute_or_park_parks_when_policy_is_park(self, monkeypatch):
        from unittest.mock import MagicMock

        from agent_takkub import auto_resume
        from agent_takkub.limit_autoresume import AutoResumeMixin
        from agent_takkub.spawn_engine import PaneState

        mixin = MagicMock(spec=AutoResumeMixin)
        ps = PaneState()
        ps.quota_provider = "claude"
        ps.rate_limited_until = 123456789.0

        monkeypatch.setattr(
            auto_resume, "effective_quota_policy", lambda proj: auto_resume.QUOTA_POLICY_PARK
        )

        AutoResumeMixin._reroute_or_park(mixin, "proj", "qa", ps)
        # Should call _park_pane_for_limit and NEVER call _pick_reroute_provider
        mixin._park_pane_for_limit.assert_called_once_with("proj", "qa", ps)
        assert mixin._pick_reroute_provider.call_count == 0

    def test_pick_reroute_provider_respects_exclude_and_skips_low_quota(self, monkeypatch):
        from unittest.mock import MagicMock

        from agent_takkub import auto_resume
        from agent_takkub.limit_autoresume import AutoResumeMixin
        from agent_takkub.spawn_engine import PaneState

        mixin = MagicMock(spec=AutoResumeMixin)
        ps = PaneState()
        ps.distinct_from = None

        # Exclude codex
        monkeypatch.setattr(
            auto_resume, "effective_quota_exclude_providers", lambda proj: {"codex"}
        )
        monkeypatch.setattr(
            "agent_takkub.limit_autoresume.confirm_verdict_for_provider",
            lambda prov, cfg: (
                "confirmed" if prov == "opencode" else "denied",
                95.0 if prov == "opencode" else 10.0,
            ),
        )

        cand = AutoResumeMixin._pick_reroute_provider(
            mixin, "proj", "qa", ps, hit_provider="claude"
        )
        # Should NOT pick codex (excluded) or opencode (low quota/confirmed limit)
        assert cand != "codex"
        assert cand != "opencode"


# ── #799: Subprocess Reaper False Alarm on Close ───────────────────────────
class TestReaperCloseCleanDone799:
    def test_reaper_warns_only_when_pane_not_done(self):
        from unittest.mock import MagicMock

        from agent_takkub.orchestrator import Orchestrator

        orch = MagicMock(spec=Orchestrator)
        orch._live_non_scaffolding_children = MagicMock(return_value=["chrome.exe", "bash.exe"])
        notices = []
        orch._notify_lead = lambda proj, msg, **k: notices.append((msg, k.get("kind")))

        # Case 1: Pane state is 'done' (successful done report)
        pane_done = MagicMock()
        pane_done.state = "done"
        orch._project_panes = lambda proj: {"qa": pane_done}

        Orchestrator._warn_if_live_children(orch, "proj", "qa", MagicMock())
        assert len(notices) == 1
        msg, kind = notices[0]
        assert "Cleaned up" in msg
        assert "if the work wasn't actually finished" not in msg
        assert kind == "subprocess-cleanup"

        # Case 2: Pane state is 'running' (aborted mid-run)
        notices.clear()
        pane_running = MagicMock()
        pane_running.state = "running"
        orch._project_panes = lambda proj: {"qa": pane_running}

        Orchestrator._warn_if_live_children(orch, "proj", "qa", MagicMock())
        assert len(notices) == 1
        msg, kind = notices[0]
        assert "if the work wasn't actually finished" in msg
        assert kind == "subprocess-kill-warning"


# ── #793: Stale Backlog Notice Revalidation ───────────────────────────────
class TestStaleBacklogNotice793:
    def test_revalidate_drops_notice_when_cards_done(self, monkeypatch):
        from unittest.mock import MagicMock

        from agent_takkub import backlog
        from agent_takkub.lead_inbox import LeadInboxMixin

        inbox = MagicMock(spec=LeadInboxMixin)
        inbox.list_status = MagicMock(return_value={})

        body = "📋 qa ปิดแล้ว แต่มี backlog รอยืนยัน 1 ใบ:\n- [card-42] Fix login bug · `takkub backlog done card-42`"

        # Case 1: Card is still in 'review' status -> keep notice
        monkeypatch.setattr(backlog, "get_item", lambda proj, cid: {"id": cid, "status": "review"})
        res = LeadInboxMixin._revalidate_system_notice(inbox, "proj", body)
        assert "card-42" in res

        # Case 2: Card is already 'done' -> drop notice
        monkeypatch.setattr(backlog, "get_item", lambda proj, cid: {"id": cid, "status": "done"})
        res = LeadInboxMixin._revalidate_system_notice(inbox, "proj", body)
        assert res == ""


# ── #792: Subagent Browser Capability Guard ────────────────────────────────
class TestSubagentBrowserGuard792:
    def test_is_browser_task_detection(self):
        from agent_takkub.orchestrator_text import is_browser_task

        assert is_browser_task("Run playwright e2e tests against auth flow") is True
        assert is_browser_task("Check responsive layout with cypress browser") is True
        assert is_browser_task("Refactor backend models and add unit tests") is False
        assert is_browser_task("Analyze memory leaks in rust daemon") is False

    def test_assign_subagent_refuses_browser_task(self):
        from unittest.mock import MagicMock

        from agent_takkub.orchestrator import Orchestrator

        orch = MagicMock(spec=Orchestrator)
        orch._resolve_project = lambda p: "proj"
        orch._is_valid_role = lambda r: True
        orch._project_panes = lambda p: {}
        orch.resolve_pane_role = lambda r, p: r

        ok, msg = Orchestrator.assign(
            orch,
            "reviewer",
            None,
            "Run browser e2e testing with Playwright",
            mode="subagent",
            project="proj",
        )
        assert ok is False
        assert "lacks browser/e2e capability" in msg
        assert "--mode subagent" in msg


# ── #801: Windows PTY Transcript Newline Normalization ─────────────────────
class TestPtyTranscriptNewlineNormalization801:
    def test_normalize_pty_transcript_chunk_injects_newlines(self):
        from agent_takkub.pty_session import _normalize_pty_transcript_chunk

        # TUI with cursor repositioning but 0 \n
        data = b"\x1b[1;1HHeader\x1b[2;1HMenu items\x1b[3;1HPrompt > "
        state = [0]
        res = _normalize_pty_transcript_chunk(data, state)
        assert b"\n" in res
        lines = res.split(b"\n")
        assert len(lines) == 3

    def test_normalize_pty_skips_when_newline_present(self):
        from agent_takkub.pty_session import _normalize_pty_transcript_chunk

        data = b"Line 1\nLine 2\nLine 3"
        state = [0]
        res = _normalize_pty_transcript_chunk(data, state)
        assert res == data


# ── #800: Multi-Repo Project Sub-Paths Awareness ───────────────────────────
class TestMultiRepoSubPaths800:
    def test_worktree_falls_back_to_subpath_git_root(self, monkeypatch):
        from unittest.mock import MagicMock

        from agent_takkub.orchestrator import Orchestrator

        orch = MagicMock(spec=Orchestrator)
        orch._resolve_project = lambda p: "multi_proj"
        orch._worktree_bare_role_collision = lambda r, p: False
        orch._pane_state = {}

        # base_cwd has no .git, but paths.api has .git
        proj_paths = {"root": "/workspace/multi", "api": "/workspace/multi/api"}
        monkeypatch.setattr(
            "agent_takkub.config._project_dict",
            lambda p: {"paths": proj_paths},
        )
        monkeypatch.setattr(
            "agent_takkub.worktree_manager.WorktreeManager.git_root",
            lambda self, p: p if p == "/workspace/multi/api" else None,
        )

        res = Orchestrator.worktree_assign_inputs(orch, "backend", "/workspace/multi", "multi_proj")
        assert res is not None
        assert res["base_cwd"] == "/workspace/multi/api"


# ── #787: Codex Session Ownership Lock Detection ──────────────────────────
class TestCodexOwnershipLock787:
    def test_ownership_lock_detected_in_pty_session(self):
        from agent_takkub.pty_session import PtySession

        sess = PtySession.__new__(PtySession)
        sess._screen_lock = MagicMock()
        sess._display_lines_locked = lambda: [
            "Resuming session...",
            "This conversation is open in another app.",
            "Close it there and press R to continue here.",
        ]

        assert sess.is_blocked_on_ownership_lock() is not None

    def test_prompt_block_reason_returns_ownership_lock(self):
        from agent_takkub.lead_inbox import _prompt_block_reason

        sess = MagicMock()
        sess.is_at_trust_prompt.return_value = False
        sess.is_at_feedback_prompt.return_value = False
        sess.is_blocked_on_permission_prompt.return_value = None
        sess.is_blocked_on_tty_prompt.return_value = None
        sess.is_blocked_on_ownership_lock.return_value = "session ownership lock detected"

        assert _prompt_block_reason(sess) == "ownership_lock"


# ── #788: Reassign After Close & Worktree Close Notice ────────────────────
class TestCloseEmptyReassignAndNoCommitNotice788:
    def test_empty_pane_with_dead_session_is_idle_for_reassign(self):
        from agent_takkub.orchestrator import Orchestrator

        orch = MagicMock(spec=Orchestrator)
        orch._IDLE_PANE_STATES = frozenset({"done", "empty", "exited", "active", "error"})

        pane = MagicMock()
        pane.state = "empty"
        pane.session = MagicMock()
        pane.session.is_alive = False

        # Must return True so assigning again doesn't block with "มี pane อยู่แล้ว"
        assert Orchestrator._pane_idle_for_reassign(orch, pane) is True

    def test_worktree_bare_role_collision_none_for_empty_pane(self):
        from agent_takkub.orchestrator import Orchestrator

        orch = MagicMock(spec=Orchestrator)
        orch._IDLE_PANE_STATES = frozenset({"done", "empty", "exited", "active", "error"})
        orch._resolve_project = lambda p: "proj"

        pane = MagicMock()
        pane.state = "empty"
        pane.session = None  # closed pane has no session
        orch._project_panes = lambda p: {"backend": pane}
        orch._pane_idle_for_reassign = lambda p: Orchestrator._pane_idle_for_reassign(orch, p)

        # No collision!
        res = Orchestrator._worktree_bare_role_collision(orch, "backend", "proj")
        assert res is None

    def test_worktree_no_commit_notice_reflects_closed_and_never_delivered(self):
        from agent_takkub.orchestrator import Orchestrator

        orch = MagicMock(spec=Orchestrator)
        orch._resolve_project = lambda p: "proj"
        notices = []
        orch._notify_lead = lambda p, msg, **k: notices.append(msg)

        info = MagicMock()
        info.branch = "wt/backend-1"
        info.path = "/fake/wt"
        info.git_root = "/fake/root"
        info.base_sha = "abc"

        mgr = MagicMock()
        mgr.commit_count.return_value = 0
        mgr.real_dirty.return_value = False
        mgr.head_sha.return_value = "abc"
        mgr.branch_merged_into_base.return_value = False

        with patch("agent_takkub.worktree_manager.WorktreeManager", return_value=mgr):
            with patch("agent_takkub.worktree_manager.WorktreeInfo.from_dict", return_value=info):
                # Case 1: normal close -> "ปิด pane"
                Orchestrator._finalize_worktree(orch, "proj", "backend", {}, note="close")
                assert len(notices) == 1
                assert "ปิด pane แต่ไม่มี commit" in notices[0]

                # Case 2: task never delivered -> "ปิด pane ก่อนงานส่งถึง (task never delivered)"
                notices.clear()
                Orchestrator._finalize_worktree(
                    orch, "proj", "backend", {"never_delivered": True}, note="close"
                )
                assert len(notices) == 1
                assert "ปิด pane ก่อนงานส่งถึง (task never delivered) แต่ไม่มี commit" in notices[0]
                assert "done แต่ไม่มี commit" not in notices[0]
