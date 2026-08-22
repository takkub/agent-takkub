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
    # Marks the tree as a Python project so the gate takes the pytest/ruff/
    # lint-imports path rather than #329's non-Python branch.
    (root / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
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


def test_full_gate_runs_pytest_under_xdist(repo, monkeypatch):
    _make_complete_venv(repo)
    monkeypatch.delenv("TAKKUB_QA_XDIST_N", raising=False)
    recorder: list = []
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0, 0, 0]))

    qa_gate.run_gate(cwd=repo, write_report=False)

    pytest_cmd = recorder[0][0]
    # A fixed worker count, never "auto" — see _xdist_worker_count's
    # docstring (#349: more workers than this box has headroom for risks a
    # commit-charge fault, not just a slower run).
    assert "-n" in pytest_cmd
    assert pytest_cmd[pytest_cmd.index("-n") + 1] == "8"
    # loadscope, not loadgroup: loadgroup only groups items explicitly marked
    # with @pytest.mark.xdist_group — everything else is freely distributed
    # with NO per-module/class grouping, which is unsafe for a suite that has
    # never been audited for cross-worker safety (see _pytest_cmd's docstring).
    assert "--dist" in pytest_cmd and "loadscope" in pytest_cmd
    # Timeout flags: full run gets 300s per test, xdist worker restart disabled.
    assert "--timeout=300" in pytest_cmd
    assert "--max-worker-restart=0" in pytest_cmd


def test_full_gate_xdist_worker_count_overridable_via_env(repo, monkeypatch):
    _make_complete_venv(repo)
    monkeypatch.setenv("TAKKUB_QA_XDIST_N", "3")
    recorder: list = []
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0, 0, 0]))

    qa_gate.run_gate(cwd=repo, write_report=False)

    pytest_cmd = recorder[0][0]
    assert pytest_cmd[pytest_cmd.index("-n") + 1] == "3"


def test_full_gate_xdist_worker_count_ignores_bad_env(repo, monkeypatch):
    _make_complete_venv(repo)
    monkeypatch.setenv("TAKKUB_QA_XDIST_N", "not-a-number")
    recorder: list = []
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0, 0, 0]))

    qa_gate.run_gate(cwd=repo, write_report=False)

    pytest_cmd = recorder[0][0]
    assert pytest_cmd[pytest_cmd.index("-n") + 1] == "8"


def test_targeted_mode_never_uses_xdist(repo, monkeypatch):
    """A handful of mid-flight paths — worker spin-up would cost more than
    the run saves (team policy: full suite once at the batch gate)."""
    _make_complete_venv(repo)
    recorder: list = []
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0]))

    qa_gate.run_gate(cwd=repo, targeted=["tests/test_x.py"], write_report=None)

    pytest_cmd = recorder[0][0]
    assert "-n" not in pytest_cmd
    # Targeted tier gets shorter timeout (120s per test) and worker-restart disabled.
    assert "--timeout=120" in pytest_cmd
    assert "--max-worker-restart=0" in pytest_cmd


def test_pythonpath_src_never_injected(repo, monkeypatch):
    """The gate must NOT add <root>/src to PYTHONPATH — that's the exact
    system-python footgun it exists to rule out: it leaks into
    test_installed_mode_gate's fresh wheel venvs and makes an installed
    build resolve agent_takkub from the checkout instead (fake packaging
    bug, gate run 2026-08-20-112822). A caller's own PYTHONPATH passes
    through untouched; resolution relies on the venv's editable install."""
    _make_complete_venv(repo)
    monkeypatch.setenv("PYTHONPATH", "/existing")
    recorder: list = []
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0, 0, 0]))

    qa_gate.run_gate(cwd=repo, write_report=False)

    _, env = recorder[0]
    assert str(repo / "src") not in env.get("PYTHONPATH", "").split(os.pathsep)
    assert env["PYTHONPATH"] == "/existing"


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


# ── #329: a tree with no Python in it ─────────────────────────────────────


@pytest.fixture
def node_repo(tmp_path):
    """A git repo that is a Node project and nothing else — no pyproject.toml,
    no tests/, exactly the saas_admin shape that used to die on
    `No module named pytest`."""
    root = tmp_path / "node-repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "package.json").write_text('{"scripts": {"test": "vitest run"}}', encoding="utf-8")
    (root / "tsconfig.json").write_text("{}", encoding="utf-8")
    (root / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return root


class TestDetectProjectKind:
    def test_python_marker_wins_over_a_package_json(self, tmp_path) -> None:
        """A Python project that also carries package.json for tooling must
        still take the Python gate."""
        (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        assert qa_gate.detect_project_kind(tmp_path) == "python"

    def test_a_bare_tests_dir_is_enough_to_count_as_python(self, tmp_path) -> None:
        """Refusing a Python project that merely packages itself unusually
        would be a worse failure than the one this replaces."""
        (tmp_path / "tests").mkdir()
        assert qa_gate.detect_project_kind(tmp_path) == "python"

    def test_node_markers(self, tmp_path) -> None:
        (tmp_path / "pnpm-workspace.yaml").write_text("", encoding="utf-8")
        assert qa_gate.detect_project_kind(tmp_path) == "node"

    def test_neither(self, tmp_path) -> None:
        assert qa_gate.detect_project_kind(tmp_path) == "unknown"


class TestNodeProjectGate:
    def test_runs_the_projects_own_checks_instead_of_pytest(self, node_repo, monkeypatch) -> None:
        recorder: list = []
        monkeypatch.setattr(subprocess, "run", _fake_run_factory(recorder, []))

        report = qa_gate.run_gate(cwd=node_repo, write_report=False)

        ran = [cmd for cmd, _env in recorder]
        assert not any("pytest" in " ".join(map(str, cmd)) for cmd in ran), (
            "a Node project must never be handed pytest — that IS #329"
        )
        assert any("tsc" in " ".join(map(str, cmd)) for cmd in ran)
        assert report.ok

    def test_a_failing_check_fails_the_gate_and_skips_the_rest(
        self, node_repo, monkeypatch
    ) -> None:
        # detect step is free; the first real check fails.
        monkeypatch.setattr(subprocess, "run", _fake_run_factory([], [1]))

        report = qa_gate.run_gate(cwd=node_repo, write_report=False)

        assert not report.ok
        assert report.exit_code != 0
        assert any(s.skipped for s in report.steps), "fail-fast must skip the later checks"

    def test_targeted_paths_are_reported_as_ignored_not_swallowed(
        self, node_repo, monkeypatch
    ) -> None:
        """The original complaint: `--targeted apps/frontend ...` vanished with
        no word that it had done nothing."""
        monkeypatch.setattr(subprocess, "run", _fake_run_factory([], []))

        report = qa_gate.run_gate(
            cwd=node_repo, targeted=["apps/frontend", "packages/ui"], write_report=False
        )

        note = next(s for s in report.steps if s.name == "targeted")
        assert note.skipped
        assert "apps/frontend" in note.detail

    def test_a_node_project_with_nothing_runnable_refuses_clearly(
        self, node_repo, monkeypatch
    ) -> None:
        (node_repo / "package.json").write_text('{"name": "x"}', encoding="utf-8")
        (node_repo / "tsconfig.json").unlink()
        monkeypatch.setattr(subprocess, "run", _fake_run_factory([], []))

        report = qa_gate.run_gate(cwd=node_repo, write_report=False)

        assert not report.ok
        assert "refuse" in report.steps[0].detail


# ── #349: PYTHONFAULTHANDLER default, memory sampling, native-abort detail ─


def test_pythonfaulthandler_defaults_on(repo, monkeypatch):
    monkeypatch.delenv("PYTHONFAULTHANDLER", raising=False)
    _make_complete_venv(repo)
    recorder: list = []
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0, 0, 0]))

    qa_gate.run_gate(cwd=repo, write_report=False)

    _, env = recorder[0]
    assert env["PYTHONFAULTHANDLER"] == "1"


def test_pythonfaulthandler_explicit_override_preserved(repo, monkeypatch):
    monkeypatch.setenv("PYTHONFAULTHANDLER", "0")
    _make_complete_venv(repo)
    recorder: list = []
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0, 0, 0]))

    qa_gate.run_gate(cwd=repo, write_report=False)

    _, env = recorder[0]
    assert env["PYTHONFAULTHANDLER"] == "0"


def test_memory_log_written_alongside_step_log(repo, monkeypatch):
    _make_complete_venv(repo)
    recorder: list = []
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0, 0, 0]))

    report = qa_gate.run_gate(cwd=repo, write_report=True)

    pytest_step = report.steps[1]
    assert pytest_step.memory_log_path is not None
    assert pytest_step.memory_log_path.exists()
    content = pytest_step.memory_log_path.read_text(encoding="utf-8")
    assert "system_available=" in content
    assert "subprocess_rss=" in content


def test_no_memory_log_when_report_not_written(repo, monkeypatch):
    _make_complete_venv(repo)
    recorder: list = []
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0, 0, 0]))

    report = qa_gate.run_gate(cwd=repo, write_report=False)

    assert report.steps[1].memory_log_path is None


def test_sample_memory_line_never_raises(monkeypatch):
    import psutil

    def _boom(*a, **k):
        raise RuntimeError("permission denied")

    monkeypatch.setattr(psutil, "virtual_memory", _boom)
    line = qa_gate._sample_memory_line()
    assert "memory sample failed" in line


class TestSilentAbortDetection:
    def test_defined_pytest_exit_code_is_not_flagged_as_abort(self):
        # exit 1 = normal "tests failed", pytest's own documented code.
        assert not qa_gate._is_silent_pytest_abort("pytest", 1, "= 2 failed, 3 passed in 1.2s =")

    def test_undefined_exit_code_with_summary_is_not_flagged(self):
        # summary line present → pytest reached its own reporting, not an abort.
        assert not qa_gate._is_silent_pytest_abort("pytest", 127, "= 5 passed in 0.1s =")

    def test_undefined_exit_code_with_no_summary_is_the_349_signature(self):
        assert qa_gate._is_silent_pytest_abort("pytest", 127, "..F..s..\n(cut off, no summary)")

    def test_only_applies_to_the_pytest_step(self):
        assert not qa_gate._is_silent_pytest_abort("ruff", 127, "no summary here at all")

    def test_native_abort_marks_step_detail_and_points_at_349(self, repo, monkeypatch):
        _make_complete_venv(repo)
        recorder: list = []
        monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [127]))

        report = qa_gate.run_gate(cwd=repo, write_report=False)

        pytest_step = report.steps[1]
        assert pytest_step.ok is False
        assert pytest_step.returncode == 127
        assert "NATIVE ABORT" in pytest_step.detail
        assert "#349" in pytest_step.detail


class TestUnknownProjectGate:
    def test_refuses_with_a_readable_message_not_no_module_named_pytest(
        self, tmp_path, monkeypatch
    ) -> None:
        root = tmp_path / "plain"
        root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        monkeypatch.setattr(subprocess, "run", _fake_run_factory([], []))

        report = qa_gate.run_gate(cwd=root, write_report=False)

        assert not report.ok
        detail = report.steps[0].detail
        assert "refuse" in detail
        assert "pyproject.toml" in detail and "package.json" in detail
