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
            "del /F /Q ./own_temp.txt",
            "rmdir /S /Q ./own_temp_dir",
            "taskkill /F /PID 999999",
        ],
    )
    def test_reads_and_own_mutations_allowed(self, mock_instance_env, sub_cmd: str) -> None:
        own_home, foreign_home = mock_instance_env
        cmd = sub_cmd.format(foreign=foreign_home.as_posix())
        verdict = pane_guard.classify(cmd, "lead", cwd=str(own_home))
        assert verdict.allowed, f"Should allow read or own file: {cmd}: {verdict.reason}"

    @pytest.mark.parametrize(
        "cmd",
        [
            "rm /tmp/foreign_posix_test/projects.json",
            "mv '/tmp/foreign_posix_test/projects.json' ./backup.json",
            "cp ./temp.json '/tmp/foreign_posix_test/projects.json'",
            "Set-Content -Path '/tmp/foreign_posix_test/projects.json' -Value '{}'",
            "Out-File -FilePath '/tmp/foreign_posix_test/projects.json'",
            "echo ok && rm -rf '/tmp/foreign_posix_test'",
        ],
    )
    def test_posix_style_paths_denied(self, monkeypatch: pytest.MonkeyPatch, cmd: str) -> None:
        """Verify that absolute POSIX-style paths (/tmp/...) are NOT skipped as switches
        and are denied when targeting a protected DATA_HOME (#633)."""
        prot_home = pathlib.Path("/tmp/foreign_posix_test").resolve()
        monkeypatch.setenv("TAKKUB_PROTECTED_DATA_HOMES", str(prot_home))
        verdict = pane_guard.classify(cmd, "lead")
        assert not verdict.allowed, f"Should deny mutation for posix-style path: {cmd}"
        assert verdict.rule == "instance_guard:protected_data_home"

    def test_path_under_own_home_and_worktrees_not_protected(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#633 Point 2: Path under own home (including worktrees/** of own home)
        must NOT be protected, allowing panes to write in their own worktree."""
        own_home = (tmp_path / "repo").resolve()
        own_worktree = (own_home / "worktrees" / "repo" / "gemini-1789452890").resolve()
        own_worktree.mkdir(parents=True)

        monkeypatch.setenv("TAKKUB_STORAGE_ROOT", str(own_home / "v2"))
        monkeypatch.delenv("TAKKUB_PROTECTED_DATA_HOMES", raising=False)

        # 1. Direct check: worktree must not be in protected_data_homes
        protected = pane_guard.get_protected_data_homes(own_home=own_home)
        assert own_worktree not in protected
        assert not any(
            own_worktree == p or str(own_worktree).lower().startswith(str(p).lower() + "/")
            for p in protected
        )

        # 2. Files inside own worktree must be allowed to write/modify
        verdict = pane_guard.classify(
            f"echo 'test' > '{own_worktree}/file.txt'", "gemini", cwd=str(own_worktree)
        )
        assert verdict.allowed, f"Writing in own worktree must be allowed: {verdict.reason}"

        # 3. is_in_protected_data_home must return False for path inside worktree
        in_prot, prot_home = pane_guard.is_in_protected_data_home(
            own_worktree / "src" / "foo.py", own_home=own_home
        )
        assert not in_prot
        assert prot_home is None

    def test_git_worktree_of_same_repo_not_protected(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#633 Point 2: Candidate that is a git worktree of the same repo as own home
        (via common .git dir or worktrees/<project>/ pattern) must NOT be protected."""
        main_repo = (tmp_path / "agent-takkub").resolve()
        wt_repo = (tmp_path / "worktrees" / "agent-takkub" / "wt-test").resolve()
        main_repo.mkdir(parents=True)
        wt_repo.mkdir(parents=True)

        # Setup git repo and worktree .git pointer
        main_git = main_repo / ".git"
        main_git.mkdir()
        wt_git = wt_repo / ".git"
        wt_git.write_text(f"gitdir: {main_git.as_posix()}/worktrees/wt-test\n", encoding="utf-8")

        monkeypatch.setenv("TAKKUB_STORAGE_ROOT", str(main_repo / "v2"))

        # Candidate is own or worktree in both directions
        assert pane_guard._is_candidate_own_or_worktree(wt_repo, main_repo)
        assert pane_guard._is_candidate_own_or_worktree(main_repo, wt_repo)

        protected = pane_guard.get_protected_data_homes(own_home=main_repo)
        assert wt_repo not in protected
        assert main_repo not in protected


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
    """Requirement 5 & #633 Point 3: Instance guard errors fail-closed ONLY for candidate commands."""

    def test_evaluator_crash_fails_closed_when_candidate(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        def boom(*_a, **_kw):
            raise RuntimeError("disk read failure during instance check")

        monkeypatch.setattr(pane_guard, "evaluate_instance_guard", boom)

        resp = _run_guard(
            monkeypatch, _bash_payload("rm /foreign/projects.json"), TAKKUB_ROLE="backend"
        )
        assert resp["exit_code"] == 2, "Must fail closed on candidate command evaluator exception"
        err = capsys.readouterr().err
        assert "instance_guard:guard_error" in err
        assert "disk read failure" in err

    def test_evaluator_crash_fails_closed_for_edit_tool(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        def boom(*_a, **_kw):
            raise RuntimeError("protected home resolution error")

        monkeypatch.setattr(pane_guard, "is_in_protected_data_home", boom)

        payload = _edit_payload("Edit", "/some/path/file.json", "{}")
        resp = _run_guard(monkeypatch, payload, TAKKUB_ROLE="lead")
        assert resp["exit_code"] == 2, "Must fail closed on edit tool instance check failure"
        err = capsys.readouterr().err
        assert "instance_guard:guard_error" in err

    def test_non_candidate_does_not_fail_closed_on_probe_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#633 Point 3: Non-candidate commands (e.g. echo hi, git status, takkub list)
        must NOT be denied because of an instance guard probe error."""

        def boom(*_a, **_kw):
            raise RuntimeError("socket probe connection timeout")

        monkeypatch.setattr(pane_guard, "get_protected_data_homes", boom)
        monkeypatch.setattr(pane_guard, "get_foreign_cockpit_pids", boom)

        # Non-candidate commands must NOT be denied (exit_code != 2)
        resp_echo = _run_guard(monkeypatch, _bash_payload("echo hi"), TAKKUB_ROLE="backend")
        assert resp_echo.get("exit_code", 0) == 0, "Non-candidate echo hi must not be denied"

        resp_git = _run_guard(monkeypatch, _bash_payload("git status"), TAKKUB_ROLE="lead")
        assert resp_git.get("exit_code", 0) == 0, "Non-candidate git status must not be denied"


class TestFastPreFilterPerformance633:
    """#633 Point 1: Pre-filter regex fast path (< 20ms overhead target)."""

    @pytest.mark.parametrize(
        "cmd,expected_candidate",
        [
            ("git status", False),
            ("takkub list", False),
            ("pytest tests/", False),
            ("echo hi", False),
            ("npm test", False),
            ("git log -n 5", False),
            ("python tools/gen_import_graph.py", False),
            ("python -m agent_takkub report build", True),
            ("agent-takkub", True),
            ("npm i -g agent-takkub", True),
            ("taskkill /pid 1234", True),
            ("Stop-Process -Id 1234", True),
            ("kill -9 1234", True),
            ("rm ./file.txt", True),
            ("del ./file.txt", True),
            ("Remove-Item ./file.txt", True),
            ("echo 'data' > ./file.txt", True),
            ("git -C /other clean -fd", True),
            ("python -c \"open('file', 'w').write('x')\"", True),
        ],
    )
    def test_candidate_detection(self, cmd: str, expected_candidate: bool) -> None:
        assert pane_guard.is_instance_guard_candidate(cmd) == expected_candidate

    def test_non_candidate_overhead_under_20ms(self) -> None:
        import time

        for cmd in ("git status", "takkub list", "pytest", "echo hi"):
            t0 = time.perf_counter()
            for _ in range(100):
                v = pane_guard.evaluate_instance_guard(cmd, "lead")
                assert v is None
            elapsed_ms = (time.perf_counter() - t0) * 1000 / 100
            assert elapsed_ms < 20.0, (
                f"Overhead for '{cmd}' ({elapsed_ms:.4f}ms) exceeded 20ms limit"
            )


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


class TestHeredocDataVsCommand:
    """Validate that heredoc bodies containing dangerous command strings as DATA
    are not falsely denied by instance guard, while actual commands and protected
    redirections remain strictly denied (#633 follow-up)."""

    @pytest.mark.parametrize(
        "cmd",
        [
            '"$PY" - <<\'PY\'\nimport os\ntest = "npm i -g agent-takkub"\nPY',
            "python - <<'PY'\nimport sys\ncmd = \"python -m agent_takkub\"\nPY",
            "python3 - <<'PY'\nlauncher = \"agent-takkub\"\nPY",
            "cat <<'EOF'\nnpm install -g agent-takkub\nEOF",
            "cat <<'EOF'\nrm -rf /tmp/foreign_posix_test\nEOF",
            "cat <<'EOF'\ndel /F /Q C:/foreign_win_test/projects.json\nEOF",
            "cat <<EOF > ./safe_output.txt\npython -m agent_takkub\nEOF",
        ],
    )
    def test_heredoc_data_allowed(self, cmd: str) -> None:
        """Dangerous commands inside heredoc data bodies are stripped and allowed."""
        verdict = pane_guard.classify(cmd, "lead")
        assert verdict.allowed, f"Heredoc data should be allowed: {cmd}"

    @pytest.mark.parametrize(
        "cmd",
        [
            "npm i -g agent-takkub",
            "npm install -g agent-takkub",
            "python -m agent_takkub",
            "agent-takkub",
        ],
    )
    def test_same_commands_outside_heredoc_denied(self, cmd: str) -> None:
        """The same commands outside heredoc remain denied."""
        verdict = pane_guard.classify(cmd, "lead")
        assert not verdict.allowed

    @pytest.mark.parametrize(
        "cmd",
        [
            "npm i -g agent-takkub\ncat <<'EOF'\nplain text\nEOF",
            "cat <<'EOF'\nplain text\nEOF\npython -m agent_takkub",
            "echo 'before'\ncat <<'EOF'\nplain text\nEOF\nagent-takkub",
            "bash <<'EOF'\nnpm i -g agent-takkub\nEOF",
            "bash -c 'npm i -g agent-takkub'",
            "sh -c 'python -m agent_takkub'",
        ],
    )
    def test_real_commands_near_or_in_shell_heredoc_denied(self, cmd: str) -> None:
        """Real commands before/after heredocs, or fed to shell sinks, remain denied."""
        verdict = pane_guard.classify(cmd, "lead")
        assert not verdict.allowed

    @pytest.mark.parametrize(
        "target_path",
        [
            "/tmp/foreign_posix_test/projects.json",
            "C:/foreign_win_test/projects.json",
        ],
    )
    def test_heredoc_redirection_to_protected_data_home_denied(
        self, monkeypatch: pytest.MonkeyPatch, target_path: str
    ) -> None:
        """Redirecting heredoc output to protected DATA_HOME (POSIX and Windows) must be denied."""
        prot = pathlib.Path(target_path).parent.resolve()
        monkeypatch.setenv("TAKKUB_PROTECTED_DATA_HOMES", str(prot))

        cmd1 = f"cat <<EOF > {target_path}\nhello\nEOF"
        v1 = pane_guard.classify(cmd1, "lead")
        assert not v1.allowed, f"Should deny redirect after delimiter: {cmd1}"
        assert v1.rule == "instance_guard:protected_data_home"

        cmd2 = f"cat > {target_path} <<EOF\nhello\nEOF"
        v2 = pane_guard.classify(cmd2, "lead")
        assert not v2.allowed, f"Should deny redirect before delimiter: {cmd2}"
        assert v2.rule == "instance_guard:protected_data_home"


class TestPythonHeredocWrites633:
    """`python - <<'EOF'` bodies are executed code: heredoc stripping (data
    bodies) must not hide a write/delete into a protected DATA_HOME."""

    @pytest.mark.parametrize("root", ["/tmp/py_heredoc_posix", "C:/py_heredoc_win"])
    @pytest.mark.parametrize(
        "body",
        [
            "open('{home}/projects.json', 'w').write('{{}}')",
            "import shutil\nshutil.rmtree('{home}/runtime')",
            "import pathlib\npathlib.Path('{home}/projects.json').write_text('')",
            "import os\nos.remove('{home}\\projects.json')",
        ],
    )
    def test_python_heredoc_write_into_protected_home_denied(
        self, monkeypatch: pytest.MonkeyPatch, root: str, body: str
    ) -> None:
        home = pathlib.Path(root).resolve()
        monkeypatch.setenv("TAKKUB_PROTECTED_DATA_HOMES", str(home))
        code = body.format(home=home.as_posix())
        for intro in ("python - <<'EOF'", "python3 <<EOF", "py - <<'PY'"):
            delim = intro.rsplit("<<", 1)[1].strip("'\"")
            cmd = f"{intro}\n{code}\n{delim}"
            verdict = pane_guard.evaluate_instance_guard(cmd, "lead", cwd=str(home.parent))
            assert verdict is not None and not verdict.allowed, cmd
            assert verdict.rule == "instance_guard:protected_data_home"

    def test_python_heredoc_read_only_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        home = pathlib.Path("/tmp/py_heredoc_read").resolve()
        monkeypatch.setenv("TAKKUB_PROTECTED_DATA_HOMES", str(home))
        cmd = f"python - <<'EOF'\nprint(open('{home.as_posix()}/runtime/port').read())\nEOF"
        verdict = pane_guard.evaluate_instance_guard(cmd, "lead", cwd=str(home.parent))
        assert verdict is None or verdict.allowed

    def test_non_python_heredoc_mentioning_write_is_data(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = pathlib.Path("/tmp/py_heredoc_data").resolve()
        monkeypatch.setenv("TAKKUB_PROTECTED_DATA_HOMES", str(home))
        cmd = (
            "gh issue comment 1 --body-file - <<'EOF'\n"
            f"repro: open('{home.as_posix()}/projects.json', 'w')\nEOF"
        )
        verdict = pane_guard.evaluate_instance_guard(cmd, "lead", cwd=str(home.parent))
        assert verdict is None or verdict.allowed
