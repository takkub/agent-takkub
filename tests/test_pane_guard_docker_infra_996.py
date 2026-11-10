"""#996: a frontend pane clearing a busy port killed Docker Desktop (Docker's
port proxy holds a published container port) and every other project's
containers restarted with it. Docker/WSL infrastructure is never a pane's to
kill — by image name, by PID, or as "whoever owns port N". Lead included."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_takkub import pane_guard


@pytest.mark.parametrize(
    "cmd",
    [
        'taskkill /F /IM "Docker Desktop.exe"',
        "Stop-Process -Name com.docker.backend -Force",
        "pkill -f dockerd",
        "killall vpnkit",
        "taskkill /IM vmmemWSL /F",
    ],
)
@pytest.mark.parametrize("role", ["frontend", "lead"])
def test_killing_docker_infra_by_name_is_denied(cmd: str, role: str) -> None:
    verdict = pane_guard.classify(cmd, role)
    assert not verdict.allowed
    assert verdict.rule == "instance_guard:kill_docker_infra"


def test_ordinary_kills_are_not_this_rule() -> None:
    for cmd in ("kill 1234", "taskkill /PID 999 /F"):
        assert pane_guard.classify(cmd, "lead").rule != "instance_guard:kill_docker_infra"
    assert not pane_guard._is_docker_infra("node.exe")
    assert not pane_guard._is_docker_infra("docker.exe")  # the CLI client is fine


def _fake_psutil(port: int, owner: str):
    conn = SimpleNamespace(laddr=SimpleNamespace(port=port), pid=4242)
    return SimpleNamespace(
        net_connections=lambda kind="inet": [conn],
        Process=lambda pid: SimpleNamespace(name=lambda: owner, is_running=lambda: False),
    )


@pytest.mark.parametrize(
    "cmd",
    [
        "Get-NetTCPConnection -LocalPort 5432 | % { Stop-Process -Id $_.OwningProcess -Force }",
        "kill -9 $(lsof -ti:5432)",
        "npx kill-port 5432",
    ],
)
def test_killing_the_owner_of_a_docker_held_port_is_denied(monkeypatch, cmd: str) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(5432, "com.docker.backend.exe"))
    verdict = pane_guard.classify(cmd, "frontend")
    assert not verdict.allowed
    assert verdict.rule == "instance_guard:kill_docker_infra"
    assert "5432" in verdict.reason


def test_port_held_by_the_panes_own_dev_server_is_not_this_rule(monkeypatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(3000, "node.exe"))
    verdict = pane_guard.classify("kill -9 $(lsof -ti:3000)", "frontend")
    assert verdict.rule != "instance_guard:kill_docker_infra"
