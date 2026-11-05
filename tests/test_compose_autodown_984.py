"""#984: local compose stacks go down when a project's work ends."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_takkub import compose_autodown as cad


class FakeDocker:
    """Records docker argv; answers ps/stats/compose down like the real CLI."""

    def __init__(self, rows: list[tuple[str, str, str, str]], down_rc: dict | None = None):
        self.rows = rows
        self.down_rc = down_rc or {}
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], timeout: int):
        self.calls.append(list(args))
        if args[0] == "ps":
            return 0, "\n".join("\t".join(r) for r in self.rows) + "\n"
        if args[0] == "stats":
            names = args[4:]
            return 0, "\n".join(f"{n}\t70.5MiB / 7.6GiB" for n in names)
        if args[:2] == ["compose", "-p"]:
            stack = args[2]
            rc = self.down_rc.get(stack, 0)
            if rc == 0:
                self.rows = [r for r in self.rows if r[2] != stack]
                return 0, ""
            return rc, "Error response from daemon: boom"
        return 1, "unexpected"

    def downs(self) -> list[str]:
        return [c[2] for c in self.calls if c[:2] == ["compose", "-p"]]


def _p(tmp_path, *parts) -> str:
    path = tmp_path.joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def test_downs_only_own_stacks_keeps_volumes_and_removes_exited(tmp_path) -> None:
    evc = _p(tmp_path, "evc")
    other = _p(tmp_path, "saas")
    fake = FakeDocker(
        [
            ("evcstg-db-1", "running", "evcstg", os.path.join(evc, "deploy", "staging")),
            ("evcstg-api-1", "running", "evcstg", os.path.join(evc, "deploy", "staging")),
            ("evc-db-1", "exited", "evc", evc),
            ("saas-pg-1", "running", "saas", other),
        ]
    )
    with patch.object(cad, "_docker", fake):
        report = cad.run_auto_down("evc", "end-session", [evc], {})

    assert sorted(fake.downs()) == ["evc", "evcstg"]
    assert all("-v" not in c and "--volumes" not in c and "--rmi" not in c for c in fake.calls)
    assert [r[0] for r in fake.rows] == ["saas-pg-1"]  # project B untouched
    # only running containers count toward the RAM figure (2 × 70.5)
    assert report.freed_mib == pytest.approx(141.0)
    assert "evcstg-db-1" in report.summary() and "141 MiB" in report.summary()


def test_stack_shared_with_another_open_tab_is_skipped(tmp_path) -> None:
    root = _p(tmp_path, "mono")
    fake = FakeDocker([("mono-db-1", "running", "mono", root)])
    with patch.object(cad, "_docker", fake):
        report = cad.run_auto_down("a", "tab-close", [root], {"b": [root]})
    assert fake.downs() == []
    assert report.skipped and "b" in report.skipped[0][1]


def test_broader_open_project_does_not_block_more_specific_owner(tmp_path) -> None:
    parent = str(tmp_path)
    child = _p(tmp_path, "evc")
    fake = FakeDocker([("evc-db-1", "running", "evc", child)])
    with patch.object(cad, "_docker", fake):
        cad.run_auto_down("evc", "end-session", [child], {"workspace": [parent]})
    assert fake.downs() == ["evc"]


def test_failed_down_is_reported_not_silent(tmp_path) -> None:
    evc = _p(tmp_path, "evc")
    fake = FakeDocker([("evc-db-1", "running", "evc", evc)], down_rc={"evc": 1})
    with patch.object(cad, "_docker", fake):
        report = cad.run_auto_down("evc", "end-session", [evc], {})
    assert report.failed and "boom" in report.summary()


def test_docker_missing_means_nothing_to_do(tmp_path) -> None:
    with patch.object(cad, "_docker", lambda args, timeout: (127, "docker not found")):
        report = cad.run_auto_down("evc", "end-session", [str(tmp_path)], {})
    assert report.docker_unavailable and report.summary() == ""


def test_opt_out_flag() -> None:
    assert cad.auto_down_enabled({}) is True
    assert cad.auto_down_enabled({"services": {"auto_down": True}}) is True
    assert cad.auto_down_enabled({"services": {"auto_down": False}}) is False


@pytest.mark.parametrize(
    ("note", "expected"),
    [
        ("deploy prod เสร็จ health ผ่าน 200 ทุก service", True),
        ("Deployed to production, healthcheck passed", True),
        ("deploy staging เสร็จ health ผ่าน", False),
        ("deploy prod ไม่ผ่าน rollback แล้ว", False),
        ("prod deploy FAILED: health 502", False),
        ("แก้ README", False),
    ],
)
def test_prod_deploy_success_detection(note: str, expected: bool) -> None:
    assert cad.is_prod_deploy_success(note) is expected


def _host(panes: dict, enabled: bool = True):
    from agent_takkub.orchestrator import Orchestrator

    host = SimpleNamespace(
        _project_panes=lambda project_ns: panes,
        _notify_lead=lambda *a, **k: notes.append((a, k)),
        compose_auto_down=lambda project_ns, trigger: started.append((project_ns, trigger)) or True,
    )
    notes: list = []
    started: list = []
    fn = Orchestrator._maybe_auto_down_after_prod_deploy.__get__(host)
    return fn, notes, started


def test_devops_prod_deploy_done_triggers_auto_down() -> None:
    fn, _notes, started = _host({"lead": object(), "devops": SimpleNamespace(state="working")})
    assert fn("p", "devops", "deploy prod เสร็จ health ผ่าน", False) is True
    assert started == [("p", "prod-deploy")]


def test_prod_deploy_done_waits_while_another_pane_works() -> None:
    fn, notes, started = _host(
        {"devops": SimpleNamespace(state="done"), "qa": SimpleNamespace(state="working")}
    )
    assert fn("p", "devops", "deploy prod เสร็จ health ผ่าน", False) is False
    assert started == [] and notes and "qa" in notes[0][0][1]


def test_non_devops_or_failed_done_never_triggers() -> None:
    fn, _notes, started = _host({})
    assert fn("p", "backend", "deploy prod เสร็จ health ผ่าน", False) is False
    assert fn("p", "devops", "deploy prod เสร็จ health ผ่าน", True) is False
    assert started == []
