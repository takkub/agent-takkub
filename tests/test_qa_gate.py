"""Unit tests for the canonical QA gate (#325).

Every test that would otherwise shell out to a *real* pytest/ruff/lint-imports
intercepts `subprocess.run` for those calls (never git — git plumbing here is
cheap and is exactly what's under test for worktree/venv resolution). This
proves the gate's own fail-fast/exit-code/env-var logic without ever running
the actual full suite recursively from inside a test of the suite itself.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from agent_takkub import qa_gate


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_run_factory(recorder: list, returncodes: list[int]):
    """subprocess.run stand-in: passes real `git ...` calls through (needed
    for worktree_root/shared_venv_bin/_head_sha), fakes everything else
    (the pytest/ruff/lint-imports tool invocations) and records each call."""
    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if cmd[0] == "git":
            return real_run(cmd, **kwargs)
        recorder.append((cmd, kwargs.get("env")))
        rc = returncodes.pop(0) if returncodes else 0
        return _FakeCompleted(rc, stdout="fake tool output\n")

    return fake_run


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "src").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return root


def _bin_dir(root):
    return root / ".venv" / ("Scripts" if qa_gate._WIN else "bin")


def _make_complete_venv(root):
    bin_dir = _bin_dir(root)
    bin_dir.mkdir(parents=True)
    ext = ".exe" if qa_gate._WIN else ""
    for name in ("python", "pytest", "ruff", "lint-imports"):
        (bin_dir / f"{name}{ext}").write_text("", encoding="utf-8")
    return bin_dir


def test_shared_venv_bin_resolves_when_present(repo):
    bin_dir = _bin_dir(repo)
    bin_dir.mkdir(parents=True)
    assert qa_gate.shared_venv_bin(cwd=repo) == bin_dir


def test_shared_venv_bin_none_when_absent(repo):
    assert qa_gate.shared_venv_bin(cwd=repo) is None


def test_venv_check_refuses_incomplete_venv(repo, monkeypatch):
    bin_dir = _bin_dir(repo)
    bin_dir.mkdir(parents=True)
    ext = ".exe" if qa_gate._WIN else ""
    (bin_dir / f"python{ext}").write_text("", encoding="utf-8")  # pytest/ruff/lint-imports missing

    recorder: list = []
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, []))

    report = qa_gate.run_gate(cwd=repo, write_report=False)

    assert [s.name for s in report.steps] == ["venv-check", "pytest", "ruff", "lint-imports"]
    vc = report.steps[0]
    assert vc.ok is False
    assert "refuse" in vc.detail
    assert all(s.skipped for s in report.steps[1:])
    assert report.ok is False
    # the footgun is refused before any tool is ever shelled out to
    assert recorder == []


def test_venv_check_passes_when_no_local_venv(repo, monkeypatch):
    recorder: list = []
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0, 0, 0]))

    report = qa_gate.run_gate(cwd=repo, write_report=False)

    assert report.steps[0].ok is True
    assert "no shared .venv" in report.steps[0].detail


def test_fail_fast_skips_later_steps_and_preserves_exact_returncode(repo, monkeypatch):
    _make_complete_venv(repo)
    recorder: list = []
    # 127 is the historically real footgun code (#234-adjacent: a PyQt6
    # abort that a shell pipe used to swallow silently).
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [127]))

    report = qa_gate.run_gate(cwd=repo, write_report=False)

    assert [s.name for s in report.steps] == ["venv-check", "pytest", "ruff", "lint-imports"]
    pytest_step = report.steps[1]
    assert pytest_step.ok is False
    assert pytest_step.returncode == 127
    assert report.steps[2].skipped is True
    assert report.steps[3].skipped is True
    assert report.ok is False
    assert report.exit_code == 127
    # ruff/lint-imports were never actually invoked — fail-fast really stops work
    assert len(recorder) == 1


def test_full_gate_success_runs_all_three_steps_in_order(repo, monkeypatch):
    _make_complete_venv(repo)
    recorder: list = []
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0, 0, 0]))

    report = qa_gate.run_gate(cwd=repo, write_report=False)

    assert [s.name for s in report.steps] == ["venv-check", "pytest", "ruff", "lint-imports"]
    assert report.ok is True
    assert report.exit_code == 0
    assert len(recorder) == 3
    assert recorder[0][0][0].endswith(("pytest", "pytest.exe"))
    assert recorder[1][0][0].endswith(("ruff", "ruff.exe"))
    assert recorder[1][0][1] == "check", "ruff must be invoked as `check`, never bare"
    assert recorder[2][0][0].endswith(("lint-imports", "lint-imports.exe"))


def test_full_gate_writes_report_file(repo, monkeypatch):
    _make_complete_venv(repo)
    recorder: list = []
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0, 0, 0]))

    report = qa_gate.run_gate(cwd=repo, write_report=True)

    assert report.report_path is not None
    assert report.report_path.exists()
    content = report.report_path.read_text(encoding="utf-8")
    assert "Result: PASS" in content
    assert str(report.report_path).startswith(str(repo / "docs" / "qa"))


def test_targeted_mode_runs_pytest_only_and_writes_no_report(repo, monkeypatch):
    _make_complete_venv(repo)
    recorder: list = []
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0]))

    report = qa_gate.run_gate(cwd=repo, targeted=["tests/test_x.py"], write_report=None)

    assert [s.name for s in report.steps] == ["venv-check", "pytest"]
    assert report.report_path is None
    assert recorder[0][0][-1] == "tests/test_x.py"


def test_pythonpath_prepends_worktree_src(repo, monkeypatch):
    _make_complete_venv(repo)
    monkeypatch.setenv("PYTHONPATH", "/existing")
    recorder: list = []
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0, 0, 0]))

    qa_gate.run_gate(cwd=repo, write_report=False)

    _, env = recorder[0]
    parts = env["PYTHONPATH"].split(os.pathsep)
    assert parts[0] == str(repo / "src")
    assert "/existing" in parts


def test_v2_flags_forces_all_five_env_vars(repo, monkeypatch):
    _make_complete_venv(repo)
    recorder: list = []
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0, 0, 0]))

    qa_gate.run_gate(cwd=repo, v2_flags=True, write_report=False)

    _, env = recorder[0]
    for name in qa_gate.V2_FLAG_ENV_VARS:
        assert env[name] == "1"


def test_v2_flags_off_by_default(repo, monkeypatch):
    _make_complete_venv(repo)
    for name in qa_gate.V2_FLAG_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    recorder: list = []
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0, 0, 0]))

    qa_gate.run_gate(cwd=repo, write_report=False)

    _, env = recorder[0]
    for name in qa_gate.V2_FLAG_ENV_VARS:
        assert env.get(name) != "1"


def test_render_table_smoke(repo, monkeypatch):
    _make_complete_venv(repo)
    recorder: list = []
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0, 0, 0]))
    report = qa_gate.run_gate(cwd=repo, write_report=False)

    table = qa_gate.render_table(report)
    assert "GATE: PASS" in table
    assert "pytest" in table and "ruff" in table and "lint-imports" in table
