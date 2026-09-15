"""#633 Regression & Integration Tests: Cross-cockpit instance & protected DATA_HOME guard.

Validates:
1. Default-deny protection of foreign cockpit DATA_HOME (read-only allowed; write/move/delete denied).
2. App boot denial (bare `python -m agent_takkub`, launchers, `npm -g`) across ALL roles including Lead,
   unless `AGENT_TAKKUB_HOME` points to a temp directory in the same command.
3. Process termination denial for other cockpit instances and their child processes (`taskkill`,
   `Stop-Process`, `kill`, `pkill`, `os.kill`, `psutil`).
4. Direct tool calls (`Edit` and `Write`) targeting protected DATA_HOME denied for ALL roles.
5. Fail-closed contract for instance guard errors in `cli.cmd_guard`.
6. Normalization across quotes, relative paths, environment variables, wrappers, and chains.
7. End-to-end simulated second instance probe using an isolated temp directory.
"""

from __future__ import annotations

import io
import json
import pathlib

import pytest

from agent_takkub import cli, pane_guard


def _run_guard(monkeypatch: pytest.MonkeyPatch, payload: dict, **env) -> dict:
    stdin_text = json.dumps(payload)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(stdin_text))
    for key in ("TAKKUB_ROLE", "TAKKUB_PROJECT", "TAKKUB_PORT_FILE", "TAKKUB_STORAGE_ROOT"):
        monkeypatch.delenv(key, raising=False)
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    return cli.cmd_guard(None)


def _bash_payload(command: str, cwd: str | None = None) -> dict:
    p: dict = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }
    if cwd is not None:
        p["cwd"] = cwd
    return p


def _edit_payload(
    tool_name: str, file_path: str, content: str = "", cwd: str | None = None
) -> dict:
    p: dict = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path, "content": content},
    }
    if cwd is not None:
        p["cwd"] = cwd
    return p


class TestAppBootGuard633:
    """Requirement 4: Deny booting app agent_takkub across all roles including Lead,
    unless AGENT_TAKKUB_HOME points to temp in the same command."""

    @pytest.mark.parametrize(
        "cmd,role",
        [
            ("python -m agent_takkub report build", "lead"),
            ("python -m agent_takkub", "backend"),
            ("pythonw -m agent_takkub", "lead"),
            ("py -m agent_takkub", "lead"),
            ("agent-takkub", "lead"),
            ("agent-takkub.exe", "gemini"),
            ("npm i -g agent-takkub", "lead"),
            ("npm install -g agent-takkub", "backend"),
            ("cmd /c 'python -m agent_takkub'", "lead"),
            ("pwsh -c 'python -m agent_takkub'", "backend"),
            ("rtk proxy python -m agent_takkub", "lead"),
        ],
    )
    def test_app_boot_denied_all_roles(self, cmd: str, role: str) -> None:
        verdict = pane_guard.classify(cmd, role)
        assert not verdict.allowed, f"Command '{cmd}' should be denied for {role}"
        assert verdict.rule in ("cli_invocation:python_m_agent_takkub", "instance_guard:app_boot")

    @pytest.mark.parametrize(
        "cmd,role",
        [
            ("AGENT_TAKKUB_HOME=/tmp/test python -m agent_takkub report build", "lead"),
            ("$env:AGENT_TAKKUB_HOME='C:\\Temp\\test'; python -m agent_takkub", "lead"),
            ("AGENT_TAKKUB_HOME=/tmp/test agent-takkub", "lead"),
            ("python -m agent_takkub.cli report build", "lead"),
            ("python -m agent_takkub.headless", "backend"),
            ("python -m agent_takkub.custom_mod", "backend"),
            ("takkub report build --type customer", "lead"),
            ("takkub done 'finished'", "gemini"),
            ("python -m pytest tests/", "lead"),
        ],
    )
    def test_app_boot_allowed_exceptions(self, cmd: str, role: str) -> None:
        verdict = pane_guard.classify(cmd, role)
        assert verdict.allowed, f"Command '{cmd}' should be allowed for {role}: {verdict.reason}"


class TestProtectedDataHomeMutation633:
    """Requirement 2: Deny write/move/delete in protected DATA_HOME, allow read."""

    @pytest.fixture
    def mock_instance_env(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
        own_home = (tmp_path / "own_instance").resolve()
        foreign_home = (tmp_path / "foreign_instance").resolve()
        own_home.mkdir(parents=True)
        foreign_home.mkdir(parents=True)

        # Configure environment to scope own vs foreign
        monkeypatch.setenv("TAKKUB_STORAGE_ROOT", str(own_home / "v2"))
        monkeypatch.setenv("TAKKUB_PROTECTED_DATA_HOMES", str(foreign_home))
        return own_home, foreign_home

    @pytest.mark.parametrize(
        "sub_cmd",
        [
            "rm {foreign}/projects.json",
            "del '{foreign}\\projects.json'",
            "Remove-Item '{foreign}\\projects.json'",
            "echo '' > '{foreign}/projects.json'",
            "echo '{{}}' >> '{foreign}/projects.json'",
            "Set-Content -Path '{foreign}/projects.json' -Value '{{}}'",
            "Out-File -FilePath '{foreign}/projects.json'",
            "mv '{foreign}/projects.json' ./backup.json",
            "cp ./temp.json '{foreign}/projects.json'",
            "Copy-Item ./temp.json -Destination '{foreign}/'",
            "Move-Item ./temp.json -Destination '{foreign}/'",
            "git -C '{foreign}' clean -fd",
            "git -C '{foreign}' commit -m 'evil'",
            "python -c \"open('{foreign}/projects.json', 'w').write('x')\"",
            "python -c \"from pathlib import Path; Path('{foreign}/projects.json').write_text('x')\"",
            "python -c \"import os; os.remove('{foreign}/projects.json')\"",
            "cmd /c 'del {foreign}\\projects.json'",
            "pwsh -c 'Remove-Item {foreign}\\projects.json'",
            "echo ok && rm -rf '{foreign}'",
        ],
    )
    def test_mutations_denied(self, mock_instance_env, sub_cmd: str) -> None:
        own_home, foreign_home = mock_instance_env
        cmd = sub_cmd.format(foreign=foreign_home.as_posix())
        verdict = pane_guard.classify(cmd, "lead", cwd=str(own_home))
        assert not verdict.allowed, f"Should deny mutation: {cmd}"
        assert verdict.rule == "instance_guard:protected_data_home"

    @pytest.mark.parametrize(
        "sub_cmd",
        [
            "cat '{foreign}/projects.json'",
            "Get-Content '{foreign}/projects.json'",
            "ls '{foreign}'",
            "dir '{foreign}'",
            "cp '{foreign}/projects.json' ./local_copy.json",
            "python -c \"print(open('{foreign}/projects.json').read())\"",
            "git -C '{foreign}' status",
            "git -C '{foreign}' log -n 1",
            "echo 'safe' > ./own_output.txt",
            "rm ./own_temp.txt",
        ],
    )
    def test_reads_and_own_mutations_allowed(self, mock_instance_env, sub_cmd: str) -> None:
        own_home, foreign_home = mock_instance_env
        cmd = sub_cmd.format(foreign=foreign_home.as_posix())
        verdict = pane_guard.classify(cmd, "lead", cwd=str(own_home))
        assert verdict.allowed, f"Should allow read or own file: {cmd}: {verdict.reason}"


class TestCrossInstanceProcessKill633:
    """Requirement 3: Deny killing processes of another cockpit instance or its children."""

    def test_foreign_pid_kill_denied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_FOREIGN_PIDS", "26256,26257")

        for cmd in [
            "taskkill /pid 26256",
            "taskkill /F /PID 26256",
            "Stop-Process -Id 26256",
            "spp -Id 26256",
            "kill 26256",
            "kill -9 26256",
            "pkill 26256",
            "python -c 'import os; os.kill(26256, 9)'",
            "python -c 'import psutil; psutil.Process(26256).kill()'",
            "cmd /c 'taskkill /PID 26257'",
            "pwsh -c 'Stop-Process -Id 26257'",
        ]:
            verdict = pane_guard.classify(cmd, "lead")
            assert not verdict.allowed, f"Command '{cmd}' should be denied"
            assert verdict.rule == "instance_guard:kill_foreign_instance"

    def test_own_child_kill_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_FOREIGN_PIDS", "26256")

        for cmd in [
            "taskkill /pid 54321",
            "Stop-Process -Id 54321",
            "kill 54321",
        ]:
            verdict = pane_guard.classify(cmd, "lead")
            assert verdict.allowed, f"Command '{cmd}' should be allowed: {verdict.reason}"

    def test_image_kill_denied_when_foreign_cockpit_active(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TAKKUB_FOREIGN_PIDS", "26256")

        for cmd in [
            "taskkill /im pythonw.exe",
            "taskkill /im agent-takkub.exe",
            "Stop-Process -Name python",
            "killall pythonw",
        ]:
            verdict = pane_guard.classify(cmd, "lead")
            assert not verdict.allowed, f"Image kill '{cmd}' should be denied"
            assert verdict.rule == "instance_guard:kill_foreign_instance"


class TestDirectEditToolGuard633:
    """Direct Edit and Write tool calls in cmd_guard and evaluate_lead_direct_edit."""

    def test_lead_direct_edit_protected_home_denied(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        own_home = (tmp_path / "own").resolve()
        foreign_home = (tmp_path / "foreign").resolve()
        own_home.mkdir()
        foreign_home.mkdir()

        monkeypatch.setenv("TAKKUB_PROTECTED_DATA_HOMES", str(foreign_home))
        monkeypatch.setenv("TAKKUB_STORAGE_ROOT", str(own_home / "v2"))

        # 1. pane_guard.evaluate_lead_direct_edit
        target = str(foreign_home / "projects.json")
        verdict = pane_guard.evaluate_lead_direct_edit("Edit", {"file_path": target})
        assert not verdict.allowed
        assert verdict.rule == "instance_guard:protected_data_home"

        # 2. Even if note-exempt (.md), still denied if in protected DATA_HOME!
        verdict_md = pane_guard.evaluate_lead_direct_edit(
            "Write", {"file_path": str(foreign_home / "notes.md"), "content": "hi"}
        )
        assert not verdict_md.allowed
        assert verdict_md.rule == "instance_guard:protected_data_home"

    def test_cmd_guard_edit_write_denied_for_all_roles(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        own_home = (tmp_path / "own").resolve()
        foreign_home = (tmp_path / "foreign").resolve()
        own_home.mkdir()
        foreign_home.mkdir()

        monkeypatch.setenv("TAKKUB_PROTECTED_DATA_HOMES", str(foreign_home))
        monkeypatch.setenv("TAKKUB_STORAGE_ROOT", str(own_home / "v2"))

        for role in ("lead", "backend", "frontend", "gemini"):
            for tool in ("Edit", "Write"):
                payload = _edit_payload(tool, str(foreign_home / "role-providers.json"), "{}")
                resp = _run_guard(monkeypatch, payload, TAKKUB_ROLE=role)
                assert resp["exit_code"] == 2, f"Expected deny for {role} {tool}"


class TestCmdGuardFailClosed633:
    """Requirement 5: Instance guard errors must fail-closed."""

    def test_evaluator_crash_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        def boom(*_a, **_kw):
            raise RuntimeError("disk read failure during instance check")

        monkeypatch.setattr(pane_guard, "evaluate_instance_guard", boom)

        resp = _run_guard(monkeypatch, _bash_payload("echo hi"), TAKKUB_ROLE="backend")
        assert resp["exit_code"] == 2, "Must fail closed on instance evaluator exception"
        err = capsys.readouterr().err
        assert "instance_guard:guard_error" in err
        assert "disk read failure" in err


class TestEndToEndSimulatedSecondInstanceProbe633:
    """Requirement 7: Real probe with temp DATA_HOME simulating second instance
    (without touching real ~/.agent-takkub or main repo runtime/)."""

    def test_probe_second_instance(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate primary instance (e.g. dev workspace)
        primary_home = tmp_path / "cockpit_primary"
        primary_runtime = primary_home / "runtime"
        primary_runtime.mkdir(parents=True)
        primary_port = primary_runtime / "port"
        primary_port.write_text("11111", encoding="utf-8")

        # Simulate second instance (e.g. prod or another worktree)
        second_home = tmp_path / "cockpit_second"
        second_runtime = second_home / "runtime"
        second_runtime.mkdir(parents=True)
        second_projects = second_home / "projects.json"
        second_projects.write_text('{"projects": ["prod-alpha"]}', encoding="utf-8")
        second_port = second_runtime / "port"
        second_port.write_text("22222", encoding="utf-8")

        # Set environment as if running in pane of primary instance
        monkeypatch.setenv("TAKKUB_PORT_FILE", str(primary_port))
        monkeypatch.setenv("TAKKUB_STORAGE_ROOT", str(primary_home / "v2"))
        monkeypatch.setenv("TAKKUB_PROTECTED_DATA_HOMES", str(second_home))
        monkeypatch.setenv("TAKKUB_FOREIGN_PIDS", "99991")

        # 1. Verify reading second instance is allowed
        cat_verdict = pane_guard.classify(f"cat '{second_projects}'", "lead", cwd=str(primary_home))
        assert cat_verdict.allowed
        assert second_projects.read_text(encoding="utf-8") == '{"projects": ["prod-alpha"]}'

        # 2. Verify writing/mutating second instance is DENIED
        write_verdict = pane_guard.classify(
            f"echo '' > '{second_projects}'", "lead", cwd=str(primary_home)
        )
        assert not write_verdict.allowed
        assert write_verdict.rule == "instance_guard:protected_data_home"

        # 3. Verify killing second instance PID is DENIED
        kill_verdict = pane_guard.classify("taskkill /pid 99991", "lead")
        assert not kill_verdict.allowed
        assert kill_verdict.rule == "instance_guard:kill_foreign_instance"

        # 4. Verify app boot without temp home is DENIED
        boot_verdict = pane_guard.classify("python -m agent_takkub report build", "lead")
        assert not boot_verdict.allowed

        # 5. Verify writing inside own primary instance is ALLOWED
        own_write_verdict = pane_guard.classify(
            f"echo 'ok' > '{primary_home}/output.txt'", "lead", cwd=str(primary_home)
        )
        assert own_write_verdict.allowed

        # 6. Verify content of second instance was untouched throughout probe
        assert second_projects.read_text(encoding="utf-8") == '{"projects": ["prod-alpha"]}'
