"""Tests for --auto-chain flag — one-hop impl→verify handoff.

Covers:
  assign(auto_chain=True) → state dict populated
  assign() default → state dict NOT populated
  done() → state cleared
  done() last auto-chain pane → injects handoff prompt to Lead
  done() with other auto-chain panes still pending → no handoff yet
  done() for non-auto-chain pane → no handoff
  close() → state cleared
  Multi-project isolation: proj_a auto-chain done does NOT trigger proj_b handoff
  Lead-absent handoff is queued via _pending_done_notices
  close(suppress_auto_chain=True) → no handoff (stuck-recover path)
  close(force=True) → handoff fires (regression #8)
  _emit_rate_limit_reset → clears rate_limited_until + resets last_content_change_ts
"""

from __future__ import annotations

import json
import pathlib
import time
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import config
from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator, PaneState


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def two_project_json(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """projects.json with two independent projects."""
    pj = tmp_path / "projects.json"
    pj.write_text(
        json.dumps(
            {
                "active": "proj_a",
                "projects": {
                    "proj_a": {
                        "paths": {
                            "api": str(tmp_path / "proj_a" / "api"),
                            "web": str(tmp_path / "proj_a" / "web"),
                        }
                    },
                    "proj_b": {
                        "paths": {
                            "api": str(tmp_path / "proj_b" / "api"),
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "PROJECTS_JSON", pj)
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", runtime)
    cockpit = tmp_path / "cockpit"
    monkeypatch.setattr(config, "REPO_ROOT", cockpit)
    monkeypatch.setattr(orch_mod, "REPO_ROOT", cockpit)
    return pj


def _make_orch_with_fake_panes(
    project: str,
    roles_with_session: list[str],
) -> tuple[Orchestrator, dict[str, MagicMock]]:
    """Build an Orchestrator and pre-populate fake panes for the given roles.
    Returns (orch, {role: pane}) so tests can inspect pane.session.write calls."""
    orch = Orchestrator()
    orch._idle_watchdog.stop()
    panes: dict[str, MagicMock] = {}
    for role in roles_with_session:
        pane = MagicMock()
        pane._session_cwd = "/tmp"
        pane._transcript_path = None
        pane.session = MagicMock()
        pane.session.is_alive = True
        pane.session.write = MagicMock()
        pane.set_state = MagicMock()
        pane.mark_expected_exit = MagicMock()
        panes[role] = pane
    orch._panes_by_project[project] = panes
    return orch, panes


class TestAutoChainStateLifecycle:
    def test_assign_with_auto_chain_populates_state(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch, _ = _make_orch_with_fake_panes("proj_a", ["lead", "frontend"])
        monkeypatch.setattr(orch, "spawn", MagicMock(return_value=(True, "ok")))
        monkeypatch.setattr(orch, "_send_when_ready", MagicMock())
        orch.assign("frontend", cwd="/tmp", task="ui", auto_chain=True, project="proj_a")
        assert (orch._pane_state.get("proj_a::frontend") or PaneState()).auto_chain is True

    def test_assign_without_auto_chain_does_not_populate_state(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch, _ = _make_orch_with_fake_panes("proj_a", ["lead", "frontend"])
        monkeypatch.setattr(orch, "spawn", MagicMock(return_value=(True, "ok")))
        monkeypatch.setattr(orch, "_send_when_ready", MagicMock())
        orch.assign("frontend", cwd="/tmp", task="ui", project="proj_a")
        assert not (orch._pane_state.get("proj_a::frontend") or PaneState()).auto_chain

    def test_close_clears_auto_chain_state(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch, _ = _make_orch_with_fake_panes("proj_a", ["lead", "frontend"])
        orch._ps("proj_a::frontend").auto_chain = True
        orch.close("frontend", project="proj_a", force=True)
        assert orch._pane_state.get("proj_a::frontend") is None


class TestInjectAutoChainHandoff:
    def test_writes_handoff_prompt_to_alive_lead(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch, panes = _make_orch_with_fake_panes("proj_a", ["lead", "frontend"])
        orch._inject_auto_chain_handoff("proj_a")
        lead_writes = [c.args[0] for c in panes["lead"].session.write.call_args_list]
        assert any("auto-chain handoff" in str(w) for w in lead_writes)

    def test_writes_queue_when_lead_absent(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch, _ = _make_orch_with_fake_panes("proj_a", ["frontend"])  # no lead
        orch._inject_auto_chain_handoff("proj_a")
        queue = orch._pending_done_notices.get("proj_a", [])
        assert any("auto-chain handoff" in entry.get("body", "") for entry in queue)


class TestDoneAutoChainTrigger:
    def test_done_last_auto_chain_pane_fires_handoff(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Single auto-chain pane: done() fires handoff immediately."""
        orch, panes = _make_orch_with_fake_panes("proj_a", ["lead", "frontend"])
        orch._ps("proj_a::frontend").auto_chain = True
        monkeypatch.setattr(orch, "close", MagicMock(return_value=(True, "ok")))
        monkeypatch.setattr(orch, "_save_decision_note", MagicMock())

        orch.done("frontend", note="UI shipped", project="proj_a")

        lead_writes = [c.args[0] for c in panes["lead"].session.write.call_args_list]
        assert any("[frontend done]" in str(w) for w in lead_writes)
        assert any("auto-chain handoff" in str(w) for w in lead_writes)

    def test_done_not_last_auto_chain_pane_no_handoff(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two auto-chain panes; first done → no handoff yet."""
        orch, panes = _make_orch_with_fake_panes("proj_a", ["lead", "frontend", "backend"])
        orch._ps("proj_a::frontend").auto_chain = True
        orch._ps("proj_a::backend").auto_chain = True
        monkeypatch.setattr(orch, "close", MagicMock(return_value=(True, "ok")))
        monkeypatch.setattr(orch, "_save_decision_note", MagicMock())

        orch.done("frontend", note="UI shipped", project="proj_a")

        lead_writes = [c.args[0] for c in panes["lead"].session.write.call_args_list]
        assert any("[frontend done]" in str(w) for w in lead_writes)
        assert not any("auto-chain handoff" in str(w) for w in lead_writes)

    def test_done_non_auto_chain_pane_no_handoff(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pane that was assigned without --auto-chain → no handoff."""
        orch, panes = _make_orch_with_fake_panes("proj_a", ["lead", "frontend"])
        # NOTE: _auto_chain_panes deliberately NOT set
        monkeypatch.setattr(orch, "close", MagicMock(return_value=(True, "ok")))
        monkeypatch.setattr(orch, "_save_decision_note", MagicMock())

        orch.done("frontend", note="just a scout", project="proj_a")

        lead_writes = [c.args[0] for c in panes["lead"].session.write.call_args_list]
        assert any("[frontend done]" in str(w) for w in lead_writes)
        assert not any("auto-chain handoff" in str(w) for w in lead_writes)

    def test_done_clears_auto_chain_key_after_handoff(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch, _ = _make_orch_with_fake_panes("proj_a", ["lead", "frontend"])
        orch._ps("proj_a::frontend").auto_chain = True
        monkeypatch.setattr(orch, "close", MagicMock(return_value=(True, "ok")))
        monkeypatch.setattr(orch, "_save_decision_note", MagicMock())

        orch.done("frontend", note="UI shipped", project="proj_a")

        assert not (orch._pane_state.get("proj_a::frontend") or PaneState()).auto_chain

    def test_multi_project_isolation(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Auto-chain done in proj_a does NOT trigger handoff for proj_b."""
        orch, panes_a = _make_orch_with_fake_panes("proj_a", ["lead", "frontend"])
        lead_b = MagicMock()
        lead_b.session = MagicMock()
        lead_b.session.is_alive = True
        lead_b.session.write = MagicMock()
        orch._panes_by_project["proj_b"] = {"lead": lead_b}

        orch._ps("proj_a::frontend").auto_chain = True
        orch._ps("proj_b::backend").auto_chain = True  # still pending in proj_b
        monkeypatch.setattr(orch, "close", MagicMock(return_value=(True, "ok")))
        monkeypatch.setattr(orch, "_save_decision_note", MagicMock())

        orch.done("frontend", note="proj_a UI", project="proj_a")

        a_writes = [c.args[0] for c in panes_a["lead"].session.write.call_args_list]
        assert any("auto-chain handoff" in str(w) for w in a_writes)
        b_writes = [c.args[0] for c in lead_b.session.write.call_args_list]
        assert not any("auto-chain handoff" in str(w) for w in b_writes)
        assert (orch._pane_state.get("proj_b::backend") or PaneState()).auto_chain


class TestSuppressAutoChain:
    """close(suppress_auto_chain=True) must never fire the verify-hop handoff.

    Regression: stuck-pane recovery calls close→respawn — the close is NOT a
    real done event.  Ordinary user-initiated / tab-close must still fire so
    the #8 forced-close behaviour is preserved.
    """

    def test_stuck_recover_close_does_not_fire_handoff(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """suppress_auto_chain=True: no handoff even when it's the last pane."""
        orch, _panes = _make_orch_with_fake_panes("proj_a", ["lead", "backend"])
        orch._ps("proj_a::backend").auto_chain = True
        inject = MagicMock()
        monkeypatch.setattr(orch, "_inject_auto_chain_handoff", inject)

        orch.close("backend", project="proj_a", suppress_auto_chain=True)

        inject.assert_not_called()
        # pane state should still be cleared
        assert orch._pane_state.get("proj_a::backend") is None

    def test_normal_forced_close_fires_handoff(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression #8: user / tab-close (no suppress) fires handoff for last pane."""
        orch, _panes = _make_orch_with_fake_panes("proj_a", ["lead", "backend"])
        orch._ps("proj_a::backend").auto_chain = True
        inject = MagicMock()
        monkeypatch.setattr(orch, "_inject_auto_chain_handoff", inject)

        orch.close("backend", project="proj_a", force=True)

        inject.assert_called_once_with("proj_a")

    def test_stuck_recover_close_does_not_fire_handoff_for_solo_pane(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """suppress_auto_chain=True on the ONLY auto-chain pane must still not
        fire handoff — this is the exact issue #53 scenario where rate-limit
        stuck-recover was the sole trigger."""
        orch, _panes = _make_orch_with_fake_panes("proj_a", ["lead", "backend"])
        orch._ps("proj_a::backend").auto_chain = True
        inject = MagicMock()
        monkeypatch.setattr(orch, "_inject_auto_chain_handoff", inject)

        # Stuck-recover closes the only auto-chain pane
        orch.close("backend", project="proj_a", suppress_auto_chain=True)

        inject.assert_not_called()

    def test_stuck_recover_close_then_respawn_then_done_fires_handoff(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Full happy path: stuck-recover close (no handoff) → respawn restores
        auto_chain → done() fires handoff correctly."""
        orch, panes = _make_orch_with_fake_panes("proj_a", ["lead", "backend"])
        orch._ps("proj_a::backend").auto_chain = True
        inject = MagicMock()
        monkeypatch.setattr(orch, "_inject_auto_chain_handoff", inject)

        # Step 1: stuck-recover close — no handoff
        orch.close("backend", project="proj_a", suppress_auto_chain=True)
        inject.assert_not_called()

        # Step 2: simulate _do_respawn restoring auto_chain (2 s later in prod)
        orch._ps("proj_a::backend").auto_chain = True
        # Re-add the pane so done() can find it
        orch._panes_by_project["proj_a"]["backend"] = panes["backend"]

        # Step 3: backend eventually finishes and calls done()
        monkeypatch.setattr(orch, "close", MagicMock(return_value=(True, "ok")))
        monkeypatch.setattr(orch, "_save_decision_note", MagicMock())
        orch.done("backend", note="backend impl done", project="proj_a")

        # handoff fires because done() was the real completion
        inject.assert_called_once_with("proj_a")


class TestCliAutoChainFlag:
    def test_assign_parser_accepts_auto_chain(self) -> None:
        import argparse

        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest="command")
        sa = sub.add_parser("assign")
        sa.add_argument("--role", required=True)
        sa.add_argument("--cwd", default=None)
        sa.add_argument("task")
        sa.add_argument(
            "--requires-commit",
            action="store_true",
            dest="requires_commit",
            default=False,
        )
        sa.add_argument(
            "--auto-chain",
            action="store_true",
            dest="auto_chain",
            default=False,
        )
        ns = p.parse_args(["assign", "--role", "frontend", "--auto-chain", "do the thing"])
        assert ns.auto_chain is True

    def test_cmd_assign_forwards_auto_chain_in_request_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import argparse

        from agent_takkub import cli as cli_mod

        captured: dict = {}

        def fake_request(payload):
            captured.update(payload)
            return {"ok": True, "msg": "queued"}

        monkeypatch.setattr(cli_mod, "_request", fake_request)
        monkeypatch.setattr(cli_mod, "_from_role", lambda: "lead")
        monkeypatch.setattr(cli_mod, "_from_project", lambda: "proj_a")

        ns = argparse.Namespace(
            role="frontend",
            cwd="/tmp",
            task="do the thing",
            requires_commit=False,
            auto_chain=True,
        )
        cli_mod.cmd_assign(ns)
        assert captured.get("auto_chain") is True


class TestEmitRateLimitReset:
    """_emit_rate_limit_reset must clear rate_limited_until AND reset
    last_content_change_ts so the stuck-pane watchdog doesn't immediately
    fire after the rate-limit window lifts (issue #53 root-cause fix)."""

    def test_clears_rate_limited_until(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
    ) -> None:
        orch, _panes = _make_orch_with_fake_panes("proj_a", ["lead", "backend"])
        orch._ps("proj_a::backend").rate_limited_until = time.time() - 1

        orch._emit_rate_limit_reset("proj_a", "backend")

        assert orch._ps("proj_a::backend").rate_limited_until == 0.0

    def test_resets_content_change_ts_to_prevent_stuck_recovery(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
    ) -> None:
        """After rate-limit reset, last_content_change_ts must be refreshed.

        Without this reset: pane content was static for the entire rate-limit
        window (hours); the very next watchdog tick would see
        content_static_s >> STUCK_THRESHOLD_S and trigger _auto_recover_stuck
        → close() → spurious auto-chain handoff (issue #53 RCA).
        """
        orch, _panes = _make_orch_with_fake_panes("proj_a", ["lead", "backend"])
        old_ts = time.time() - 7200  # 2 h ago — simulate hours-long rate-limit
        ps = orch._ps("proj_a::backend")
        ps.rate_limited_until = time.time() - 1
        ps.last_content_change_ts = old_ts

        t_before = time.time()
        orch._emit_rate_limit_reset("proj_a", "backend")
        t_after = time.time()

        new_ts = orch._ps("proj_a::backend").last_content_change_ts
        assert new_ts is not None
        assert new_ts >= t_before
        assert new_ts <= t_after

    def test_no_state_entry_does_not_crash(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
    ) -> None:
        """If there is no PaneState for the role, emit must not raise."""
        orch, _panes = _make_orch_with_fake_panes("proj_a", ["lead"])
        # No pane state for "ghost" — must not raise
        orch._emit_rate_limit_reset("proj_a", "ghost")
