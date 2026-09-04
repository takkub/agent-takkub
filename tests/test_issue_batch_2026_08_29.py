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


class TestPostInjectTerminalReply:
    def test_esc_chunk_right_after_engine_write_is_not_user_input(self, orch):
        session = MagicMock()
        session.last_write_ts = time.time()  # engine just pasted a digest
        orch._lead_last_user_write_ts["proj"] = session.last_write_ts - 30
        assert orch._is_post_inject_terminal_reply("proj", session, b"\x1b[?1;2c")

    def test_printable_or_enter_always_counts(self, orch):
        session = MagicMock()
        session.last_write_ts = time.time()
        orch._lead_last_user_write_ts["proj"] = session.last_write_ts - 30
        assert not orch._is_post_inject_terminal_reply("proj", session, b"hello")
        assert not orch._is_post_inject_terminal_reply("proj", session, b"\x1b\r")

    def test_esc_chunk_after_users_own_keystroke_counts(self, orch):
        session = MagicMock()
        session.last_write_ts = time.time() - 1
        orch._lead_last_user_write_ts["proj"] = session.last_write_ts + 0.5  # owner typed last
        assert not orch._is_post_inject_terminal_reply("proj", session, b"\x1b[D")

    def test_grace_window_expires(self, orch):
        session = MagicMock()
        session.last_write_ts = time.time() - orch._LEAD_INJECT_GRACE_S - 1
        orch._lead_last_user_write_ts["proj"] = 0.0
        assert not orch._is_post_inject_terminal_reply("proj", session, b"\x1b[D")


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
