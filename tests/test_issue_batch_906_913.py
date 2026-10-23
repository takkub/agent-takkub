"""Regression tests for the `takkub done` cluster, 2026-10-23 (#906 #907 #911 #912 #913).

#910 lives next to its original test in test_issue_batch_2026_08_29.py.
"""

from __future__ import annotations

import argparse
import os
import struct
import zlib
from pathlib import Path

from agent_takkub import cli, config, impact_contract
from agent_takkub.digest_facts import DigestFacts, _files_bit
from agent_takkub.orchestrator import Orchestrator
from agent_takkub.worktree_manager import shared_status_cwd


def _done_args(**kw) -> argparse.Namespace:
    base = dict(
        note="",
        impact_evidence_file=None,
        note_file=None,
        screenshot=None,
        fail=False,
        blocked=False,
        force=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


# --- #911 -------------------------------------------------------------------


def test_done_timeout_keeps_note_in_outbox(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)
    monkeypatch.setenv("TAKKUB_ROLE", "backend")
    sent: list[dict] = []

    def fake_request(payload):
        sent.append(payload)
        return {
            "ok": False,
            "msg": "timed out waiting for orchestrator response after 15s (cockpit may be restarting or wedged)",
        }

    monkeypatch.setattr(cli, "_request", fake_request)
    resp = cli.cmd_done(_done_args(note="PASS everything works", fail=True))
    assert not resp["ok"]
    saved = list((tmp_path / "done-outbox").glob("*.md"))
    assert len(saved) == 1
    assert saved[0].read_text(encoding="utf-8") == "PASS everything works"
    assert str(saved[0]) in resp["msg"] and "--note-file" in resp["msg"]
    assert "--fail" in resp["msg"]
    assert len(sent) == 1  # never blindly resent: the first may still land


def test_done_note_file_and_screenshot_fold_into_note(tmp_path, monkeypatch) -> None:
    note_file = tmp_path / "note.md"
    note_file.write_text("PASS from file", encoding="utf-8")
    shot = tmp_path / "shot.png"
    captured: dict = {}
    monkeypatch.setattr(cli, "_request", lambda p: captured.update(p) or {"ok": True, "msg": "x"})
    cli.cmd_done(_done_args(note_file=str(note_file), screenshot=[str(shot)]))
    assert captured["note"].startswith("PASS from file")
    assert f"screenshot: {shot.resolve()}" in captured["note"]


def test_done_parser_accepts_screenshot_flag() -> None:
    ns = cli.build_parser().parse_args(
        ["done", "PASS", "--screenshot", "a.png", "--screenshot", "b.png"]
    )
    assert ns.screenshot == ["a.png", "b.png"]


# --- #912 -------------------------------------------------------------------


def test_missing_evidence_fields_are_named_together() -> None:
    plan = {"checks": []}
    msg = impact_contract.validate_evidence(
        plan, {"checks": []}, task_id="abcd1234ffff", revision="rev1"
    )
    assert "missing: task_id, plan_digest, revision" in msg
    assert "different" not in msg
    assert "abcd1234ffff" in msg and impact_contract.plan_digest(plan) in msg


# --- #906 -------------------------------------------------------------------


def test_status_cwd_falls_back_to_baseline_repo(tmp_path) -> None:
    root = tmp_path / "unirecon"
    sub = root / "unirecon-api"
    sub.mkdir(parents=True)
    assert shared_status_cwd(str(root), str(sub)) == str(sub)
    assert shared_status_cwd(str(sub / "src"), str(sub)) == str(sub / "src")
    assert shared_status_cwd(str(sub), str(sub).replace(os.sep, "/")) == str(sub)


def test_unverifiable_files_note_is_not_wrapped_twice() -> None:
    facts = DigestFacts(role="qa", files_note="ตรวจไม่ได้ (อ่าน git status ตอน done ไม่สำเร็จ)")
    line = _files_bit(facts)
    assert line.count("ตรวจไม่ได้") == 1


# --- #907 -------------------------------------------------------------------


def _png(path: Path, width: int, height: int, size: int) -> Path:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    data = (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(ihdr))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk))
    )
    path.write_bytes(data + b"\0" * max(0, size - len(data)))
    return path


def test_blank_png_is_flagged(tmp_path) -> None:
    blank = _png(tmp_path / "blank.png", 1240, 1755, 12396)
    real = _png(tmp_path / "real.png", 1440, 900, 138_000)
    assert Orchestrator._evidence_mostly_blank(blank, 12396)
    assert not Orchestrator._evidence_mostly_blank(real, 138_000)
    assert "mostly-blank" in Orchestrator._evidence_format_entry(blank, 12396)
    assert "mostly-blank" not in Orchestrator._evidence_format_entry(real, 138_000)


# --- #913 -------------------------------------------------------------------


def test_worktree_pane_gets_skill_links(tmp_path, monkeypatch) -> None:
    import importlib

    from agent_takkub import spawn_engine, worktree_manager

    # The function-local `from .skill_scan import ...` resolves through
    # sys.modules; another test may have reloaded the module.
    skill_scan = importlib.import_module("agent_takkub.skill_scan")

    monkeypatch.setattr(worktree_manager, "DATA_HOME", tmp_path)
    wt = worktree_manager.worktree_root("proj") / "backend-1"
    wt.mkdir(parents=True)
    main = tmp_path / "main"
    main.mkdir()
    monkeypatch.setattr(spawn_engine, "_allowed_project_roots", lambda p: [main])
    linked: list[Path] = []
    monkeypatch.setattr(
        skill_scan, "ensure_project_skill_links", lambda root, ns: linked.append(Path(root))
    )
    spawn_engine._skill_roots_for_project("proj", str(wt))
    assert linked == [main, wt]
    linked.clear()
    spawn_engine._skill_roots_for_project("proj", str(main))
    assert linked == [main]


# --- #914 -------------------------------------------------------------------


def test_idle_reminder_names_task_sheet() -> None:
    from agent_takkub.orchestrator import IDLE_REMINDER_TEXT, idle_reminder_text

    assert idle_reminder_text(None) == IDLE_REMINDER_TEXT
    text = idle_reminder_text("C:/x/runtime/tasks/p/2026-10-23/1-reviewer.md")
    assert text.startswith(IDLE_REMINDER_TEXT)
    assert '"C:/x/runtime/tasks/p/2026-10-23/1-reviewer.md"' in text


# --- #903 -------------------------------------------------------------------


def test_effective_policy_leads_the_context_read(monkeypatch) -> None:
    from agent_takkub import lead_context, lead_readiness

    monkeypatch.setattr(lead_context, "_build_lead_context_text", lambda *a, **k: "RENDERED")
    facts = {"team": {"preset": "solo-lead", "lead_may_implement": True, "roles": {}}}
    text = lead_readiness._context("p", ".", {"/x/role.md": "POLICY DOC"}, facts)
    assert text.startswith("## Effective session policy")
    assert text.index("RENDERED") < text.index("POLICY DOC")


# --- #899 -------------------------------------------------------------------


def test_meter_probe_is_throttled_per_pane(monkeypatch) -> None:
    from agent_takkub import limit_autoresume
    from agent_takkub import orchestrator as orch_mod

    o = Orchestrator.__new__(Orchestrator)
    o._pane_running_account = lambda project, role: None
    calls: list[float] = []
    monkeypatch.setattr(limit_autoresume, "pane_account_home", lambda *a: Path("."))

    def fake_meter(provider, home, threshold, now):
        calls.append(now)
        return None

    monkeypatch.setattr(limit_autoresume, "meter_reset_at", fake_meter)
    t0 = 1_000_000.0
    o._meter_limit_reset_at("p", "backend", "claude", t0)
    o._meter_limit_reset_at("p", "backend", "claude", t0 + 5)
    o._meter_limit_reset_at("p", "frontend", "claude", t0 + 5)
    assert len(calls) == 2
    o._meter_limit_reset_at("p", "backend", "claude", t0 + orch_mod._METER_PROBE_INTERVAL_S + 1)
    assert len(calls) == 3


def test_meter_probe_cached_reset_expires(monkeypatch) -> None:
    from agent_takkub import limit_autoresume

    o = Orchestrator.__new__(Orchestrator)
    o._pane_running_account = lambda project, role: None
    monkeypatch.setattr(limit_autoresume, "pane_account_home", lambda *a: Path("."))
    monkeypatch.setattr(limit_autoresume, "meter_reset_at", lambda *a: 1_000_010.0)
    assert o._meter_limit_reset_at("p", "r", "claude", 1_000_000.0) == 1_000_010.0
    assert o._meter_limit_reset_at("p", "r", "claude", 1_000_011.0) is None
