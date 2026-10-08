"""Regression tests for #818-#824 (2026-10-08, codex queued-assignment batch).

Each case reproduces the live failure from the prod events.log / issue body:
  #818  logs_panel tail read slurped a log that grew between stat() and read()
  #819  repaste-off delivery passed payload=None, blinding the not-ready
        verify branch to a draft still in the composer → settled in ~1s with
        zero Enter resends (codex task stuck:composer)
  #820  idle-drain re-dispatch while the pane was still busy re-queued the
        item yet told Lead "forwarded when pane ready" every minute
  #823  pyte broke out of draw() on Thai class-0 marks (ึ ์ ี), dropping the
        rest of the chunk (`takkub tail` showed `ข้อความถ  backend`)
  #824  close-time resume resolved "newest codex rollout in cwd", so a
        frontend pane resumed backend's conversation and redid its task
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pyte

from agent_takkub.lead_inbox import SubmitSettleOutcome, _delayed_enter_verified


# ── #818 ───────────────────────────────────────────────────────────────────
def test_log_tail_read_is_bounded_when_file_grows(tmp_path, monkeypatch) -> None:
    import builtins

    from agent_takkub.logs_panel import read_log_tail

    log = tmp_path / "events.log"
    log.write_bytes(b"line\n" * 10)
    real_open = builtins.open

    def _open_then_grow(path, mode="r", *a, **k):
        fh = real_open(path, mode, *a, **k)
        with real_open(path, "ab") as grow:
            grow.write(b"x" * 2_000_000)
        return fh

    monkeypatch.setattr(builtins, "open", _open_then_grow)
    assert len(read_log_tail(log, tail_bytes=1000)) <= 1000


# ── #819 ───────────────────────────────────────────────────────────────────
def test_repaste_off_still_resends_enter_for_pending_draft() -> None:
    sess = MagicMock()
    sess.is_at_ready_prompt.return_value = False  # codex just resumed / busy
    sess.shows_busy_queue_marker.return_value = False
    sess.shows_account_pending_marker.return_value = False
    sess.shows_pending_input.return_value = True  # the task is still a draft
    pane = MagicMock()
    pane.session = sess
    pane.model.provider_name = "codex"
    outcomes: list[SubmitSettleOutcome] = []
    resends: list[int] = []

    with patch("agent_takkub.lead_inbox.QTimer.singleShot", lambda _ms, fn: fn()):
        _delayed_enter_verified(
            pane,
            sess,
            150,
            payload="TASK BODY",
            repaste=False,
            content_fragment="TASK BODY",
            on_resend=resends.append,
            on_settled=outcomes.append,
            provider="codex",
        )

    writes = [c.args[0] for c in sess.write.call_args_list]
    assert resends, "a pending draft must get Enter resends, not an instant settle"
    assert all(w == b"\r" for w in writes), "repaste=False must never rewrite the body"
    assert outcomes and outcomes[-1].stuck_in_composer is True


# ── #820 ───────────────────────────────────────────────────────────────────
def test_requeued_dispatch_does_not_claim_forwarded() -> None:
    from agent_takkub.orchestrator import Orchestrator

    item = {"_queued_task_id": "abcdef0123456789", "_queued_backlog_id": "b10c0001"}
    queue = [item]

    def _busy_requeue(**kw):
        queue.insert(0, dict(kw))  # what the busy-guard does
        return True, "queued after current task"

    fake = SimpleNamespace(
        _pending_assignments={"p::backend": queue},
        _assign_dispatch=_busy_requeue,
        _notify_lead=MagicMock(),
    )
    assert Orchestrator._dispatch_next_assignment(fake, "p", "backend") is True
    fake._notify_lead.assert_not_called()

    fake2 = SimpleNamespace(
        _pending_assignments={"p::backend": [dict(item)]},
        _assign_dispatch=lambda **kw: (True, "sent"),
        _notify_lead=MagicMock(),
    )
    Orchestrator._dispatch_next_assignment(fake2, "p", "backend")
    msg = fake2._notify_lead.call_args.args[1]
    assert "abcdef01" in msg and "b10c0001" in msg and "forwarded" in msg


# ── #823 ───────────────────────────────────────────────────────────────────
def test_thai_class0_marks_do_not_drop_rest_of_line() -> None:
    from agent_takkub.pty_session import _DimAwareScreen, _safe_screen_display

    screen = _DimAwareScreen(50, 2)
    pyte.Stream(screen).feed('ข้อความถึง backend\r\nอ่านไฟล์นี้: "x"')
    rows = [r.rstrip() for r in _safe_screen_display(screen)]
    assert rows[0] == "ข้อความถึง backend"
    assert rows[1] == 'อ่านไฟล์นี้: "x"'


# ── #824 ───────────────────────────────────────────────────────────────────
def _rollout(root: Path, sid: str, cwd: str, started: float, mtime: float) -> None:
    day = datetime.fromtimestamp(started)
    d = root / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"rollout-{sid}.jsonl"
    stamp = datetime.fromtimestamp(started, UTC).isoformat().replace("+00:00", "Z")
    meta = {"type": "session_meta", "payload": {"id": sid, "cwd": cwd, "timestamp": stamp}}
    f.write_text(json.dumps(meta) + "\n", encoding="utf-8")
    import os

    os.utime(f, (mtime, mtime))


def test_codex_close_resolve_skips_sibling_session(tmp_path) -> None:
    from agent_takkub.codex_helper import resolve_newest_codex_session_for_cwd

    now = time.time()
    cwd = str(tmp_path / "repo")
    # backend's resumed conversation: started long ago, written most recently
    _rollout(tmp_path, "backend-sid", cwd, started=now - 3600, mtime=now)
    # frontend's own conversation: started after the frontend pane spawned
    _rollout(tmp_path, "frontend-sid", cwd, started=now - 600, mtime=now - 30)
    spawn_ts = now - 700

    # The pre-#824 call picked the sibling.
    old = resolve_newest_codex_session_for_cwd(cwd, root=tmp_path)
    assert old is not None and "backend-sid" in old.name

    got = resolve_newest_codex_session_for_cwd(
        cwd, root=tmp_path, exclude_ids=frozenset({"backend-sid"}), created_after=spawn_ts
    )
    assert got is not None and "frontend-sid" in got.name

    # Unclaimed sibling still can't be adopted: started before this pane.
    got2 = resolve_newest_codex_session_for_cwd(cwd, root=tmp_path, created_after=now - 300)
    assert got2 is None


def test_provider_session_id_never_returns_claimed_id() -> None:
    from agent_takkub import token_meter

    with patch.object(token_meter, "_provider_session_id_for_cwd", return_value="taken"):
        assert (
            token_meter.provider_session_id_for_cwd(
                "opencode", "/x", exclude_ids=frozenset({"taken"})
            )
            is None
        )
        assert token_meter.provider_session_id_for_cwd("opencode", "/x") == "taken"
