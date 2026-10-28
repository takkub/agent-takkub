"""PM pane (cross-project project manager): report, project wiring, policy."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from agent_takkub import backlog, config, pm_project, pm_report, task_ledger, team_preset

TODAY = date(2026, 10, 28)


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    rt = tmp_path / "runtime"
    for mod in (config, backlog, task_ledger):
        monkeypatch.setattr(mod, "RUNTIME_DIR", rt)
    monkeypatch.setattr(config, "list_project_names", lambda: ["alpha", "beta", "PM"])
    return rt


def _ledger(rt: Path, project: str, groups: list[dict]) -> None:
    path = rt / "tasks" / project / ".ledger-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"groups": groups, "open": {}}), encoding="utf-8")


def _row(role: str, status: str, summary: str, **extra) -> dict:
    return {
        "role": role,
        "status": status,
        "summary": summary,
        "assign_hhmmss": "10:00:00",
        **extra,
    }


def _note(rt: Path, day: str, project: str, name: str, note: str) -> None:
    path = rt / "sessions" / day / project / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nrole: x\n---\n\n# done\n\n## Note\n\n{note}\n\n## Transcript\n\nx\n", "utf-8"
    )


def test_summary_lists_what_is_left_per_project(runtime: Path) -> None:
    brief = runtime.parent / "brief.md"
    brief.write_text("[no-impact] \n# แก้หน้า checkout ให้รองรับ QR\n", encoding="utf-8")
    _ledger(
        runtime,
        "alpha",
        [
            {
                "date": "2026-10-27",
                "goal": "g",
                "features": [
                    {
                        "name": "f",
                        "rows": [
                            _row("backend", "ok", "done thing"),
                            _row("qa", "fail", f'อ่านไฟล์นี้: "{brief.as_posix()}"'),
                            _row("frontend", "working", "still going"),
                        ],
                    }
                ],
            },
            {
                "date": "2026-10-01",
                "goal": "old",
                "features": [{"name": "f", "rows": [_row("qa", "fail", "too old")]}],
            },
        ],
    )
    _note(
        runtime,
        "2026-10-27",
        "alpha",
        "backend-101500",
        "STATUS: ok; REMAINING: deploy prod [skill: x]",
    )
    _note(runtime, "2026-10-28", "alpha", "qa-090000", "STATUS: PASS; REMAINING: None")
    backlog.add_item("alpha", "งานเปิดค้าง")

    reports = pm_report.build(2, today=TODAY)
    assert [r.name for r in reports] == ["alpha"]  # beta has nothing; PM never listed
    alpha = reports[0]
    assert alpha.done_count == 1
    assert [t.summary for t in alpha.failed_tasks] == ["แก้หน้า checkout ให้รองรับ QR"]
    assert [t.role for t in alpha.open_tasks] == ["frontend"]
    assert [n.remaining for n in alpha.notes if n.remaining] == ["deploy prod"]

    text = pm_report.render(reports, 2, pending_only=True, today=TODAY)
    assert "2026-10-27 → 2026-10-28" in text
    assert "deploy prod" in text and "งานเปิดค้าง" in text and "too old" not in text


def test_pending_only_drops_projects_with_nothing_left(runtime: Path) -> None:
    _note(runtime, "2026-10-28", "beta", "qa-090000", "STATUS: PASS; REMAINING: none")
    reports = pm_report.build(1, today=TODAY)
    assert [r.name for r in reports] == ["beta"]
    assert "ไม่มีโปรเจค" in pm_report.render(reports, 1, pending_only=True, today=TODAY)


def test_pm_project_is_registered_once_under_runtime(runtime: Path, monkeypatch) -> None:
    store: dict = {"projects": {}}
    saves: list[dict] = []
    monkeypatch.setattr(config, "load_projects", lambda: json.loads(json.dumps(store)))

    def _save(data: dict) -> bool:
        store.clear()
        store.update(data)
        saves.append(data)
        return True

    monkeypatch.setattr(config, "save_projects_json", _save)
    assert pm_project.ensure_pm_project() == "PM"
    entry = store["projects"]["PM"]
    assert entry["kind"] == "pm" and entry["paths"]["main"] == (runtime / "pm").as_posix()
    assert (runtime / "pm").is_dir()
    pm_project.ensure_pm_project()
    assert len(saves) == 1  # idempotent
    assert pm_project.is_pm_project("PM") and not pm_project.is_pm_project("alpha")


def test_pm_tab_has_no_team() -> None:
    ok, msg = team_preset.can_spawn("backend", "PM")
    assert not ok and "PM" in msg
    assert team_preset.can_spawn("lead", "PM")[0]


def test_pm_lead_gets_the_pm_role_not_the_lead_playbook() -> None:
    from agent_takkub import lead_context

    text = lead_context._build_lead_context_text("PM")
    assert text is not None and "PM — project manager" in text
    assert "takkub pm summary" in text and "<runtime>" not in text
    assert "team conventions" not in text


def test_pm_readiness_hashes_only_the_pm_doc(tmp_path: Path) -> None:
    from agent_takkub import lead_readiness

    _rev, documents, facts = lead_readiness._sources("PM", str(tmp_path))
    assert [Path(p).name for p in documents] == ["role.md"]
    assert "PM: read-only" in lead_readiness._policy_block(
        {**facts, "project_config": {"kind": "pm"}}
    )
