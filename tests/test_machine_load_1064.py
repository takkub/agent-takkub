"""#1063 / #1064: several panes running "every core" builds and suites filled
the machine and took the cockpit down. What must hold now."""

from __future__ import annotations

import json
import time

import pytest

from agent_takkub import _pty_backend, machine_load, pane_guard
from agent_takkub import auto_issue_capture as aic
from agent_takkub.resource_errors import is_resource_exhaustion


def _idle_snapshot(tmp_path):
    snap = tmp_path / "machine-state.json"
    snap.write_text(
        json.dumps(
            {
                "ts": time.time(),
                "working_panes": [],
                "overloaded": False,
                "ram_percent": 30.0,
                "cpu_percent": 10.0,
            }
        ),
        encoding="utf-8",
    )
    return snap


# ── every spelling of "run the whole suite" is recognised ────────────────
WHOLE_SUITE = [
    "npx --no-install jest --silent --ci",  # the command from the incident
    "rtk npx --no-install jest --silent --ci",
    "cd api && npx --no-install jest --silent --ci 2>&1 | tail -20",
    "npx --yes jest --ci",
    "NODE_ENV=test npx --no-install jest",
    "yarn jest",
    "pnpm jest --ci",
    "bunx jest",
    "npm exec -- jest",
    "node node_modules/jest/bin/jest.js --ci",
    "node --max-old-space-size=4096 node_modules/jest/bin/jest.js",
    "./node_modules/.bin/jest",
    "npx jest >/dev/null 2>&1",
    "pytest > out.txt",
    "pytest -q 2>&1 | tail -5",
]
TARGETED = [
    "npx --no-install jest src/a.test.ts",
    "npx jest src/a.test.ts 2>&1",
    "pnpm jest src/x.test.ts -t foo",
    "pytest tests/test_x.py > out.txt",
    "pytest tests/test_x.py 2> err.log",
    "yarn install",
    "node script.js",
    "echo jest",
]


@pytest.mark.parametrize("cmd", WHOLE_SUITE)
def test_whole_suite_is_recognised_however_it_is_launched(cmd):
    assert pane_guard._full_suite_rule(cmd) in {"jest", "pytest"}
    assert pane_guard._is_heavy_build_or_suite(cmd)[0] is True


@pytest.mark.parametrize("cmd", TARGETED)
def test_targeted_runs_stay_allowed(cmd):
    assert pane_guard._full_suite_rule(cmd) is None


@pytest.mark.parametrize(
    ("cmd", "kind"),
    [
        ("npx --no-install next build", "next_build"),
        ("pnpm next build", "next_build"),
        ("pnpm exec tsc -b", "tsc_project"),
        ("docker build .", "docker_build"),
        ("docker compose up -d --build", "docker_build"),
        ("docker-compose build api", "docker_build"),
        ("turbo build", "turbo_build"),
        ("cargo test", "cargo"),
        ("go test ./...", "go"),
        ("./gradlew build", "jvm_build"),
        ("dotnet test", "dotnet"),
        ("pnpm vitest", "vitest"),
    ],
)
def test_other_heavy_builds_are_recognised(cmd, kind):
    assert pane_guard._is_heavy_build_or_suite(cmd) == (True, kind)


@pytest.mark.parametrize(
    "cmd",
    [
        "docker compose up -d",
        "docker ps",
        "cargo test my_case",
        "cargo build -p mycrate",
        "go test ./pkg/x",
        "turbo run build --filter=web",
        "npx vitest --version",
        "npx vitest related src/a.ts",
        "npx eslint .",
        "npm install",
        'takkub done "ran: npx jest ok"',
    ],
)
def test_light_commands_are_not_called_heavy(cmd):
    assert pane_guard._is_heavy_build_or_suite(cmd)[0] is False


# ── guard verdicts ───────────────────────────────────────────────────────
def test_incident_command_is_denied_for_a_specialist(tmp_path):
    verdict = pane_guard.classify(
        "npx --no-install jest --silent --ci", "backend", snapshot_path=_idle_snapshot(tmp_path)
    )
    assert not verdict.allowed


def test_uncapped_suite_is_denied_even_on_an_idle_machine(tmp_path):
    """`tester` may run the raw suite — but not on the runner's default
    worker count, and the Lead (unguarded for most rules) may not either."""
    snap = _idle_snapshot(tmp_path)
    for role in ("tester", "lead"):
        verdict = pane_guard.classify("npx jest --ci", role, snapshot_path=snap)
        assert not verdict.allowed and verdict.rule == "worker_cap:jest"
        assert "--maxWorkers=" in verdict.reason


@pytest.mark.parametrize(
    "cmd",
    [
        "npx jest --ci --maxWorkers=2",
        "npx jest -w 2",
        "npx jest --runInBand",
        "npx vitest run --maxWorkers=2",
        "npx vitest run --pool=forks --poolOptions.forks.maxForks=2",
        "npx playwright test --workers=2",
    ],
)
def test_capped_suite_passes_the_worker_rule(cmd, tmp_path):
    verdict = pane_guard.classify(cmd, "tester", snapshot_path=_idle_snapshot(tmp_path))
    assert not (verdict.rule or "").startswith("worker_cap:")


def test_busy_machine_still_answers_busy_first(tmp_path):
    snap = tmp_path / "machine-state.json"
    snap.write_text(
        json.dumps(
            {
                "ts": time.time(),
                "working_panes": [],
                "overloaded": False,
                "ram_percent": 97.0,
                "cpu_percent": 99.0,
            }
        ),
        encoding="utf-8",
    )
    verdict = pane_guard.classify("npx jest --ci", "tester", snapshot_path=snap)
    assert verdict.rule == "busy_machine:jest"
    verdict = pane_guard.classify("docker build .", "devops", snapshot_path=snap)
    assert verdict.rule == "busy_machine:docker_build"


# ── worker budget ────────────────────────────────────────────────────────
def test_budget_is_a_share_of_the_machine_never_a_fixed_number():
    b = machine_load.worker_budget
    small = b(mode="balanced", logical_cpus=4, total_memory_gb=8, heavy_slots=2)
    mid = b(mode="balanced", logical_cpus=16, total_memory_gb=40, heavy_slots=4)
    big = b(mode="balanced", logical_cpus=64, total_memory_gb=256, heavy_slots=8)
    assert 1 <= small <= mid <= big
    assert mid == 2  # 16 threads / 40 GB / 4 heavy slots
    assert b(mode="safe", logical_cpus=16, total_memory_gb=40, heavy_slots=4) <= mid
    assert b(mode="maximum", logical_cpus=16, total_memory_gb=40, heavy_slots=4) >= mid
    # memory binds before cores on a RAM-poor box
    assert b(mode="maximum", logical_cpus=64, total_memory_gb=8, heavy_slots=2) == 1
    # all workers of all heavy slots stay under the machine's core count
    assert mid * 4 <= 16


def test_budget_reaches_pane_env_without_overriding_the_user():
    env: dict[str, str] = {"VITEST_MAX_THREADS": "7"}
    machine_load.apply_worker_budget_env(env)
    assert env["VITEST_MAX_THREADS"] == "7"
    budget = env["TAKKUB_WORKER_BUDGET"]
    assert int(budget) >= 1
    assert env["PYTEST_XDIST_AUTO_NUM_WORKERS"] == budget and env["CARGO_BUILD_JOBS"] == budget


def test_pane_env_carries_the_budget():
    from agent_takkub import pane_env

    assert int(pane_env._build_pane_env()["TAKKUB_WORKER_BUDGET"]) >= 1


# ── a starved machine must not kill the pane reader or file a bug ────────
def _winerror(code):
    exc = OSError(code, "Insufficient system resources")
    exc.winerror = code
    return exc


def test_resource_exhaustion_is_told_apart_from_real_errors():
    assert is_resource_exhaustion(_winerror(1450))
    assert is_resource_exhaustion(_winerror(1455))
    assert is_resource_exhaustion(MemoryError())
    assert not is_resource_exhaustion(_winerror(5))  # access denied
    assert not is_resource_exhaustion(ValueError("x"))
    assert not is_resource_exhaustion(None)


def test_reader_survives_resource_errors_and_keeps_delivering(monkeypatch):
    import socket
    import threading

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    class _Pty:
        def __init__(self):
            self.script = ["one", _winerror(1450), _winerror(1450), "two", "three"]
            self.done = False

        def read(self, blocking=False):
            item = self.script.pop(0)
            if not self.script:
                self.done = True
            if isinstance(item, BaseException):
                raise item
            return item

        def iseof(self):
            return self.done

    monkeypatch.setattr("time.sleep", lambda _s: None)
    t = threading.Thread(
        target=_pty_backend._resilient_read_in_thread,
        args=(server.getsockname(), _Pty(), False),
        daemon=True,
    )
    t.start()
    conn, _ = server.accept()
    conn.settimeout(5)
    got = b""
    while b"three" not in got:
        chunk = conn.recv(4096)
        if not chunk:
            break
        got += chunk
    t.join(5)
    conn.close()
    server.close()
    assert got.startswith(b"one") and b"two" in got and b"three" in got


def test_reader_still_stops_on_a_real_error():
    import socket
    import threading

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    class _Pty:
        def read(self, blocking=False):
            raise ValueError("pty is gone")

        def iseof(self):
            return False

    t = threading.Thread(
        target=_pty_backend._resilient_read_in_thread,
        args=(server.getsockname(), _Pty(), False),
        daemon=True,
    )
    t.start()
    conn, _ = server.accept()
    t.join(5)
    assert not t.is_alive()
    conn.close()
    server.close()


def test_resource_exhaustion_is_never_filed_as_a_cockpit_bug(monkeypatch):
    calls = []
    monkeypatch.setattr(aic.issues, "new_issue", lambda *a, **k: calls.append(a) or (1, "x"))
    monkeypatch.setattr(aic, "_auto_issue_suppressed", lambda: False)

    class _Inline:
        def __init__(self, target=None, **_k):
            self._t = target

        def start(self):
            self._t()

    monkeypatch.setattr(aic, "_spawn", _Inline)
    exc = _winerror(1450)
    aic.capture_cockpit_crash(OSError, exc, None, source="thread:reader")
    aic.capture_cockpit_crash(MemoryError, MemoryError(), None, source="thread:x")
    assert calls == []


# ── no new pane on a machine with nothing left ───────────────────────────
def test_governor_holds_ordinary_work_when_ram_is_critical():
    from agent_takkub.resource_governor import ResourceClass, ResourceGovernor

    gov = ResourceGovernor(sampler=lambda: (10.0, 4.0, 0, 0, 0))  # 4% RAM free
    gov._available_ram_percent = 4.0
    gov._cpu_percent = 10.0
    assert gov._denial_reason("p", ResourceClass.NORMAL) == "memory_low"
    assert gov._denial_reason("p", ResourceClass.TEST) == "memory_low"
    assert gov._denial_reason("p", ResourceClass.LIGHT) == ""  # Lead / shell
    gov._available_ram_percent = 40.0
    assert gov._denial_reason("p", ResourceClass.NORMAL) == ""


# ── `takkub status` shows the whole machine ──────────────────────────────
def test_status_line_names_load_and_panes_of_every_project(tmp_path):
    from agent_takkub import cli

    state = tmp_path / "machine-state.json"
    state.write_text(
        json.dumps(
            {
                "ts": 1000.0,
                "working_panes": ["backend@unirecon", "frontend@pms"],
                "ram_percent": 91.4,
                "cpu_percent": 97.0,
                "overloaded": True,
            }
        ),
        encoding="utf-8",
    )
    line = cli.machine_load_line(state, now=1010.0)
    assert "RAM 91%" in line and "CPU 97%" in line and "OVERLOADED" in line
    assert "backend@unirecon" in line and "frontend@pms" in line
    assert "งบ worker ต่อ pane" in line
    assert cli.machine_load_line(state, now=1000.0 + 600) == ""  # stale: say nothing
    assert cli.machine_load_line(tmp_path / "missing.json") == ""
