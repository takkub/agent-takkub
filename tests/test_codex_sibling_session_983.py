"""#983: a codex devops pane resumed backend's conversation in the same cwd.

Chain (prod 2026-11-05, macOS 2.2.46): devops' done-close guessed "newest
codex rollout for the cwd" — backend's, whose id the cockpit never learned so
#824's exclude list could not name it → next devops spawn ran `codex resume
<backend id>` and sat on "open in another app" → manual close kept the
PaneState (task undelivered, #484) WITH that id → re-assign resumed backend's
thread again and the pane saw backend's history.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import Orchestrator, _exit_key

PROJECT = "codexsib983"


def _rollout(
    root: Path, sid: str, cwd: str, started: float, mtime: float, marker: str = ""
) -> None:
    day = datetime.fromtimestamp(started)
    d = root / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"rollout-{sid}.jsonl"
    stamp = datetime.fromtimestamp(started, UTC).isoformat().replace("+00:00", "Z")
    meta = {"type": "session_meta", "payload": {"id": sid, "cwd": cwd, "timestamp": stamp}}
    dev = {"type": "response_item", "payload": {"role": "developer", "content": marker}}
    f.write_text(json.dumps(meta) + "\n" + json.dumps(dev) + "\n", encoding="utf-8")
    os.utime(f, (mtime, mtime))


def test_marker_bound_resolve_ignores_unclaimed_sibling(tmp_path) -> None:
    from agent_takkub.codex_helper import resolve_newest_codex_session_for_cwd
    from agent_takkub.pane_instructions import session_marker

    now = time.time()
    cwd = str(tmp_path / "repo")
    devops_marker = session_marker("devops-nonce")
    spawn_ts = now - 900
    _rollout(tmp_path, "devops-sid", cwd, started=now - 800, mtime=now - 60, marker=devops_marker)
    # backend started AFTER devops spawned and wrote last: the #824 bounds
    # (no exclude id, created_after=spawn) still pick it.
    _rollout(tmp_path, "backend-sid", cwd, started=now - 300, mtime=now, marker=session_marker("b"))

    unbound = resolve_newest_codex_session_for_cwd(cwd, root=tmp_path, created_after=spawn_ts)
    assert unbound is not None and "backend-sid" in unbound.name

    got = resolve_newest_codex_session_for_cwd(
        cwd, root=tmp_path, created_after=spawn_ts, required_marker=devops_marker
    )
    assert got is not None and "devops-sid" in got.name

    nothing = resolve_newest_codex_session_for_cwd(
        cwd, root=tmp_path, created_after=spawn_ts, required_marker=session_marker("other")
    )
    assert nothing is None


def test_codex_close_resolve_without_marker_resolves_nothing() -> None:
    from agent_takkub import token_meter

    with patch.object(token_meter, "_provider_session_id_for_cwd", return_value="sib") as inner:
        assert token_meter.provider_session_id_for_cwd("codex", "/x", codex_marker="") is None
        inner.assert_not_called()
        # an exact known id still resolves without a marker
        assert (
            token_meter.provider_session_id_for_cwd(
                "codex", "/x", session_uuid="own", codex_marker=""
            )
            == "sib"
        )
        # other providers are unaffected by the codex marker rule
        assert token_meter.provider_session_id_for_cwd("opencode", "/x", codex_marker="") == "sib"


def test_spawn_never_auto_resumes_a_session_another_slot_holds() -> None:
    from agent_takkub.spawn_engine import SpawnEngineMixin

    host = SimpleNamespace(
        _pane_state={
            "p::backend": SimpleNamespace(session_uuid="backend-sid"),
            "p::devops": SimpleNamespace(session_uuid="backend-sid"),
        }
    )
    held = SpawnEngineMixin._session_held_by_other_slot
    assert held(host, "p::devops", "backend-sid") is True
    assert held(host, "p::devops", "devops-sid") is False
    assert held(host, "p::devops", "") is False


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture
def orch(qapp, monkeypatch) -> Orchestrator:
    monkeypatch.setattr(Orchestrator, "_resolve_project", staticmethod(lambda p: p or PROJECT))
    o = Orchestrator()
    o.shutdown_timers()
    return o


def _close(orch: Orchestrator, **kw):
    pane = MagicMock()
    pane.session = None
    orch._panes_by_project[PROJECT] = {"devops": pane}
    with (
        patch("agent_takkub.task_ledger.mark_done", return_value=None),
        patch.object(orch, "_drain_pane_health", return_value=None),
        patch.object(orch, "_notify_lead"),
    ):
        return orch.close("devops", project=PROJECT, **kw)


def test_manual_close_of_undelivered_pane_drops_its_session_id(orch) -> None:
    ekey = _exit_key(PROJECT, "devops")
    ps = orch._ps(ekey)
    ps.last_assigned_task = "[ROLE: devops] never reached the pane"
    ps.session_uuid = "backend-sid"
    ps.session_uuid_cwd = "/repo"

    ok, msg = _close(orch)

    assert ok is True, msg
    kept = orch._pane_state.get(ekey)
    assert kept is not None and kept.last_assigned_task  # #484 still recoverable
    assert kept.session_uuid is None
