"""#697: harvest must not close a role on another role's artifacts."""

from __future__ import annotations

import pathlib
import time
from types import SimpleNamespace

import pytest

from agent_takkub import cli
from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator


def _fake_orch(panes: dict) -> SimpleNamespace:
    return SimpleNamespace(
        _resolve_project=lambda p: "proj",
        _project_panes=lambda ns: panes,
    )


@pytest.fixture
def shared_root(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path / "runtime")
    from agent_takkub import config

    monkeypatch.setattr(
        config,
        "load_projects",
        lambda: {"projects": {"proj": {"paths": {"root": str(root)}}}},
    )
    return root


class TestHarvestInfoAttribution:
    def test_other_active_role_on_same_root_marks_unattributed(
        self, shared_root: pathlib.Path
    ) -> None:
        t0 = time.time() - 60
        panes = {
            "backend": SimpleNamespace(state="working", _spawn_ts=t0),
            "frontend": SimpleNamespace(state="working", _spawn_ts=t0),
            "lead": SimpleNamespace(state="working", _spawn_ts=t0),
        }
        (shared_root / "page.tsx").write_text("frontend wrote this", encoding="utf-8")

        ok, _msg, payload = Orchestrator.harvest_info(_fake_orch(panes), "backend")

        assert ok
        assert [a["path"] for a in payload["artifacts"]] == [str(shared_root / "page.tsx")]
        assert payload["other_active"] == ["frontend"]
        assert payload["attribution"] == "unattributed"

    def test_sole_role_is_attributed(self, shared_root: pathlib.Path) -> None:
        t0 = time.time() - 60
        panes = {
            "backend": SimpleNamespace(state="working", _spawn_ts=t0),
            "frontend": SimpleNamespace(state="ready", _spawn_ts=t0),
        }
        (shared_root / "audit.md").write_text("backend wrote this", encoding="utf-8")

        ok, _msg, payload = Orchestrator.harvest_info(_fake_orch(panes), "backend")

        assert ok
        assert payload["other_active"] == []
        assert payload["attribution"] == "sole"


class TestCmdHarvest:
    def _resp(self, attribution: str, other: list[str]) -> dict:
        return {
            "ok": True,
            "state": "working",
            "since_ts": time.time() - 60,
            "artifacts": [
                {"path": "/proj/unirecon-web/page.tsx", "mtime_rel": "5m ago"},
                {
                    "path": "/runtime/exports/proj/screenshots/report-builder-mobile.png",
                    "mtime_rel": "6m ago",
                },
            ],
            "other_active": other,
            "attribution": attribution,
        }

    def test_auto_confirm_refused_when_unattributed_and_no_note(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict] = []

        def fake_request(payload: dict) -> dict:
            calls.append(payload)
            return self._resp("unattributed", ["frontend"])

        monkeypatch.setattr(cli, "_request", fake_request)
        monkeypatch.setattr(cli, "_from_role", lambda: "lead")
        args = SimpleNamespace(role="backend", since=None, limit=100, auto_confirm=True, note=None)

        result = cli.cmd_harvest(args)

        assert result["ok"] is False
        assert result["exit_code"] == 4
        assert "frontend" in result["msg"] and "--note" in result["msg"]
        assert [c["cmd"] for c in calls] == ["harvest"]  # no harvest-done fired

    def test_auto_confirm_with_note_composes_evidence_note(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict] = []

        def fake_request(payload: dict) -> dict:
            calls.append(payload)
            if payload["cmd"] == "harvest":
                return self._resp("unattributed", ["frontend"])
            return {"ok": True}

        monkeypatch.setattr(cli, "_request", fake_request)
        monkeypatch.setattr(cli, "_from_role", lambda: "lead")
        args = SimpleNamespace(
            role="backend",
            since=None,
            limit=100,
            auto_confirm=True,
            note="audit: docs/lotus-audit-2026-09-22.md",
        )

        result = cli.cmd_harvest(args)

        assert result["ok"] is True
        done = next(c for c in calls if c["cmd"] == "harvest-done")
        note = done["note"]
        assert "unattributed: frontend also active" in note
        assert "audit: docs/lotus-audit-2026-09-22.md" in note
        assert "/runtime/exports/proj/screenshots/report-builder-mobile.png" in note

    def test_sole_role_auto_confirm_note_lists_artifact_paths(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict] = []

        def fake_request(payload: dict) -> dict:
            calls.append(payload)
            if payload["cmd"] == "harvest":
                return self._resp("sole", [])
            return {"ok": True}

        monkeypatch.setattr(cli, "_request", fake_request)
        monkeypatch.setattr(cli, "_from_role", lambda: "lead")
        args = SimpleNamespace(role="frontend", since=None, limit=100, auto_confirm=True, note=None)

        result = cli.cmd_harvest(args)

        assert result["ok"] is True
        done = next(c for c in calls if c["cmd"] == "harvest-done")
        assert done["note"].startswith("harvest: 2 artifact(s)")
        assert "- /runtime/exports/proj/screenshots/report-builder-mobile.png" in done["note"]
        assert "unattributed" not in done["note"]
