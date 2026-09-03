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
import sys

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


def test_venv_check_refuses_when_python_itself_is_missing(repo, monkeypatch):
    """No `python` binary at all in the resolved venv is still a genuinely
    broken/incomplete install — a hard refuse, unlike #401's missing
    pytest/ruff/lint-imports (see the tests below)."""
    bin_dir = _bin_dir(repo)
    bin_dir.mkdir(parents=True)
    # python itself missing — nothing under bin_dir at all.

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


def test_full_gate_writes_report_file_outside_the_repo(repo, monkeypatch, tmp_path):
    """The report is runtime state (DATA_HOME/runtime/qa-reports), never a
    file inside the checkout — the old docs/qa/ location left one committed
    1KB file per full gate behind."""
    _make_complete_venv(repo)
    recorder: list = []
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0, 0, 0]))
    runtime = tmp_path / "data-home" / "runtime"
    monkeypatch.setattr(qa_gate, "_runtime_dir", lambda: runtime)

    report = qa_gate.run_gate(cwd=repo, write_report=True)

    assert report.report_path is not None
    assert report.report_path.exists()
    content = report.report_path.read_text(encoding="utf-8")
    assert "Result: PASS" in content
    assert str(report.report_path).startswith(str(runtime / "qa-reports"))
    assert not (repo / "docs" / "qa").exists()
    assert not (repo / "runtime").exists()


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
    # Timeout flags: full run gets 600s per test, xdist worker restart disabled.
    assert "--timeout=600" in pytest_cmd
    assert "--timeout-method=thread" in pytest_cmd
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
    # Targeted tier gets a shorter timeout than full (300s per test) and worker-restart disabled.
    assert "--timeout=300" in pytest_cmd
    assert "--timeout-method=thread" in pytest_cmd
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

    def test_typecheck_red_fails_the_gate_even_when_test_would_pass(
        self, node_repo, monkeypatch
    ) -> None:
        """#368: vitest transpiles via esbuild and never sees a type error —
        a spec calling `new CustomerController(platform)` after the ctor
        grew a 2nd arg passed the old gate (`npm test` only) and went red at
        CI with TS2554. Typecheck now runs first; red there = gate FAIL."""
        recorder: list = []
        # rc sequence for the non-git calls: typecheck=1, (test would be 0)
        monkeypatch.setattr(subprocess, "run", _fake_run_factory(recorder, [1, 0]))

        report = qa_gate.run_gate(cwd=node_repo, write_report=False)

        names = [s.name for s in report.steps]
        assert names.index("typecheck") < names.index("test")
        assert not report.ok
        assert next(s for s in report.steps if s.name == "typecheck").ok is False
        assert next(s for s in report.steps if s.name == "test").skipped

    def test_verify_script_is_preferred_and_uses_the_lockfile_pm(
        self, node_repo, monkeypatch
    ) -> None:
        (node_repo / "package.json").write_text(
            '{"scripts": {"test": "turbo run test", "verify": "turbo run typecheck test"}}',
            encoding="utf-8",
        )
        (node_repo / "tsconfig.json").unlink()
        (node_repo / "pnpm-lock.yaml").write_text("", encoding="utf-8")
        recorder: list = []
        monkeypatch.setattr(subprocess, "run", _fake_run_factory(recorder, []))

        report = qa_gate.run_gate(cwd=node_repo, write_report=False)

        ran = [cmd for cmd, _env in recorder]
        assert len(ran) == 1 and ran[0][1:] == ["run", "verify"]
        assert "pnpm" in str(ran[0][0])
        assert report.ok

    def test_targeted_still_typechecks_whole_project(self, node_repo, monkeypatch) -> None:
        recorder: list = []
        monkeypatch.setattr(subprocess, "run", _fake_run_factory(recorder, []))

        qa_gate.run_gate(cwd=node_repo, targeted=["src/x.ts"], write_report=False)

        assert any("tsc" in " ".join(map(str, cmd)) for cmd, _ in recorder)

    def test_a_node_project_with_nothing_runnable_refuses_clearly(
        self, node_repo, monkeypatch
    ) -> None:
        (node_repo / "package.json").write_text('{"name": "x"}', encoding="utf-8")
        (node_repo / "tsconfig.json").unlink()
        monkeypatch.setattr(subprocess, "run", _fake_run_factory([], []))

        report = qa_gate.run_gate(cwd=node_repo, write_report=False)

        assert not report.ok
        assert "refuse" in report.steps[0].detail


# ── #469: Node gate wiring for the Prisma drift/checksum checks ───────────
# `prisma_gate.py` itself is unit-tested in `test_prisma_gate.py`; these only
# prove `_non_python_gate` calls into it at the right time and the result
# actually fails/passes the overall report.


@pytest.fixture
def node_repo_with_prisma(tmp_path):
    """Same shape as `node_repo`, plus one already-"applied" migration — on a
    branch literally named `main` so `check_migration_integrity`'s
    merge-base lookup is deterministic regardless of the machine's
    `init.defaultBranch`."""
    root = tmp_path / "node-prisma-repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "package.json").write_text('{"scripts": {"test": "vitest run"}}', encoding="utf-8")
    (root / "tsconfig.json").write_text("{}", encoding="utf-8")
    (root / "prisma").mkdir()
    (root / "prisma" / "schema.prisma").write_text("// v1\n", encoding="utf-8")
    mig_dir = root / "prisma" / "migrations" / "20260101000000_init"
    mig_dir.mkdir(parents=True)
    (mig_dir / "migration.sql").write_text("CREATE TABLE x (id INT);\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return root


class TestNodePrismaGate:
    def test_no_prisma_project_has_no_prisma_steps_at_all(self, node_repo, monkeypatch) -> None:
        monkeypatch.setattr(subprocess, "run", _fake_run_factory([], []))

        report = qa_gate.run_gate(cwd=node_repo, write_report=False)

        assert not any(s.name.startswith("prisma-") for s in report.steps)

    def test_schema_drift_fails_the_gate(self, node_repo_with_prisma, monkeypatch) -> None:
        # rc sequence for non-git calls: prisma-drift=2 (diff detected), then
        # typecheck/test default to 0 via the empty-list fallback.
        monkeypatch.setattr(subprocess, "run", _fake_run_factory([], [2]))

        report = qa_gate.run_gate(cwd=node_repo_with_prisma, write_report=False)

        drift = next(s for s in report.steps if s.name == "prisma-drift")
        assert drift.ok is False
        assert drift.skipped is False
        assert not report.ok

    def test_modified_applied_migration_fails_the_gate(
        self, node_repo_with_prisma, monkeypatch
    ) -> None:
        mig_file = (
            node_repo_with_prisma
            / "prisma"
            / "migrations"
            / "20260101000000_init"
            / "migration.sql"
        )
        mig_file.write_text("CREATE TABLE x (id INT, extra TEXT);\n", encoding="utf-8")
        monkeypatch.setattr(subprocess, "run", _fake_run_factory([], [0]))  # prisma-drift passes

        report = qa_gate.run_gate(cwd=node_repo_with_prisma, write_report=False)

        integrity = next(s for s in report.steps if s.name == "prisma-migration-integrity")
        assert integrity.ok is False
        assert not report.ok

    def test_clean_prisma_project_passes_with_both_checks_present(
        self, node_repo_with_prisma, monkeypatch
    ) -> None:
        monkeypatch.setattr(subprocess, "run", _fake_run_factory([], []))

        report = qa_gate.run_gate(cwd=node_repo_with_prisma, write_report=False)

        assert report.ok
        assert any(s.name == "prisma-drift" for s in report.steps)
        assert any(s.name == "prisma-migration-integrity" for s in report.steps)

    def test_style_only_auto_tier_never_runs_prisma_checks(
        self, node_repo_with_prisma, monkeypatch
    ) -> None:
        """#436 auto-tier: a style-only diff (untouched schema/migrations)
        must not even attempt the prisma checks."""
        (node_repo_with_prisma / "README.md").write_text("hello\n", encoding="utf-8")
        monkeypatch.setattr(subprocess, "run", _fake_run_factory([], []))

        report = qa_gate.run_gate(cwd=node_repo_with_prisma, auto=True, write_report=False)

        assert not any(s.name.startswith("prisma-") for s in report.steps)


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


# ── #401: a venv with `python` but no pytest/ruff/lint-imports on purpose ──


def _make_venv_missing(root, *missing_names: str):
    """A venv with `python` present but the named tools absent — the #401
    shape (e.g. a unittest-only project designed to run its tests in
    Docker), distinct from `test_venv_check_refuses_when_python_itself_is_
    missing`'s genuinely broken venv."""
    bin_dir = _bin_dir(root)
    bin_dir.mkdir(parents=True)
    ext = ".exe" if qa_gate._WIN else ""
    for name in ("python", "pytest", "ruff", "lint-imports"):
        if name in missing_names:
            continue
        (bin_dir / f"{name}{ext}").write_text("", encoding="utf-8")
    return bin_dir


class TestEnvGap:
    def test_venv_check_reports_gap_not_refuse_when_only_pytest_missing(self, repo) -> None:
        _make_venv_missing(repo, "pytest")
        vc = qa_gate._venv_check(_bin_dir(repo))
        assert vc.ok is True
        assert vc.env_gap is False  # venv-check itself is informational only
        assert "env gap" in vc.detail
        assert "pytest" in vc.detail

    def test_missing_pytest_with_no_tests_dir_is_env_gap(self, repo, monkeypatch) -> None:
        _make_venv_missing(repo, "pytest", "ruff", "lint-imports")
        recorder: list = []
        monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, []))

        report = qa_gate.run_gate(cwd=repo, write_report=False)

        pytest_step = next(s for s in report.steps if s.name == "pytest")
        assert pytest_step.env_gap is True
        assert pytest_step.ok is True
        assert "ENV_GAP" in pytest_step.detail
        assert "pip install pytest" in pytest_step.detail
        # a real broken-venv refuse never shells out; an env gap likewise
        # never invokes the missing tool.
        assert recorder == []
        # ruff/lint-imports are independently missing too -> also ENV_GAP,
        # not skipped as a fail-fast cascade (nothing actually failed).
        ruff_step = next(s for s in report.steps if s.name == "ruff")
        li_step = next(s for s in report.steps if s.name == "lint-imports")
        assert ruff_step.env_gap is True
        assert li_step.env_gap is True
        assert report.ok is True
        assert report.env_gap is True
        assert report.exit_code == qa_gate.ENV_GAP_EXIT_CODE

    def test_missing_pytest_with_a_tests_dir_falls_back_to_unittest_discover(
        self, repo, monkeypatch
    ) -> None:
        _make_venv_missing(repo, "pytest", "ruff", "lint-imports")
        (repo / "tests").mkdir()
        (repo / "tests" / "test_x.py").write_text("import unittest\n", encoding="utf-8")

        recorder: list = []
        monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0]))

        report = qa_gate.run_gate(cwd=repo, write_report=False)

        pytest_step = next(s for s in report.steps if s.name == "pytest")
        assert pytest_step.env_gap is False
        assert pytest_step.ok is True
        assert "unittest discover fallback" in pytest_step.detail
        assert len(recorder) == 1
        cmd = recorder[0][0]
        assert cmd[1:4] == ["-m", "unittest", "discover"]
        assert str(repo / "tests") in cmd

    def test_unittest_fallback_failure_still_fails_the_gate(self, repo, monkeypatch) -> None:
        """A real unittest failure in fallback mode must behave exactly like
        a real pytest failure — fail-fast, non-zero exit — not get waved
        through as an env gap."""
        _make_venv_missing(repo, "pytest")
        (repo / "tests").mkdir()
        (repo / "tests" / "test_x.py").write_text("import unittest\n", encoding="utf-8")

        recorder: list = []
        monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [1]))

        report = qa_gate.run_gate(cwd=repo, write_report=False)

        pytest_step = next(s for s in report.steps if s.name == "pytest")
        assert pytest_step.env_gap is False
        assert pytest_step.ok is False
        assert next(s for s in report.steps if s.name == "ruff").skipped is True
        assert report.ok is False
        assert report.exit_code == 1

    def test_targeted_mode_notes_the_fallback_is_unnarrowed(self, repo, monkeypatch) -> None:
        _make_venv_missing(repo, "pytest")
        (repo / "tests").mkdir()
        (repo / "tests" / "test_x.py").write_text("import unittest\n", encoding="utf-8")
        recorder: list = []
        monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0]))

        report = qa_gate.run_gate(cwd=repo, targeted=["tests/test_x.py"], write_report=None)

        pytest_step = next(s for s in report.steps if s.name == "pytest")
        assert "unnarrowed" in pytest_step.detail

    def test_missing_ruff_and_lint_imports_only_are_gap_after_pytest_passes(
        self, repo, monkeypatch
    ) -> None:
        bin_dir = _bin_dir(repo)
        bin_dir.mkdir(parents=True)
        ext = ".exe" if qa_gate._WIN else ""
        (bin_dir / f"python{ext}").write_text("", encoding="utf-8")
        (bin_dir / f"pytest{ext}").write_text("", encoding="utf-8")
        # ruff/lint-imports missing.
        recorder: list = []
        monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0]))

        report = qa_gate.run_gate(cwd=repo, write_report=False)

        assert len(recorder) == 1  # only the real pytest call
        assert next(s for s in report.steps if s.name == "pytest").env_gap is False
        assert next(s for s in report.steps if s.name == "ruff").env_gap is True
        assert next(s for s in report.steps if s.name == "lint-imports").env_gap is True
        assert report.ok is True
        assert report.env_gap is True

    def test_render_table_shows_gap_label_and_footer_note(self, repo, monkeypatch) -> None:
        _make_venv_missing(repo, "pytest", "ruff", "lint-imports")
        monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory([], []))

        report = qa_gate.run_gate(cwd=repo, write_report=False)
        table = qa_gate.render_table(report)

        assert "GAP" in table
        assert "#401" in table
        assert "GATE: PASS" in table  # nothing genuinely failed

    def test_exec_prefix_bypasses_local_venv_and_env_gap_entirely(self, repo, monkeypatch) -> None:
        """#401's `--exec`: the target is trusted to own its own toolchain —
        no ENV_GAP detection, no local venv resolution, every command is
        prefixed verbatim."""
        # No .venv at all under repo — proves --exec doesn't need one.
        recorder: list = []
        monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0, 0, 0]))

        report = qa_gate.run_gate(
            cwd=repo, write_report=False, exec_prefix=["docker", "compose", "exec", "-T", "gw"]
        )

        assert all(not s.env_gap for s in report.steps)
        assert report.ok is True
        assert len(recorder) == 3
        assert recorder[0][0][:5] == ["docker", "compose", "exec", "-T", "gw"]
        assert recorder[0][0][5] == "pytest"
        assert recorder[1][0][:6] == ["docker", "compose", "exec", "-T", "gw", "ruff"]
        assert recorder[2][0][:5] == ["docker", "compose", "exec", "-T", "gw"]
        assert recorder[2][0][5] == "lint-imports"

    def test_exec_prefix_missing_tool_on_target_is_a_real_fail_not_env_gap(
        self, repo, monkeypatch
    ) -> None:
        recorder: list = []
        monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [127]))

        report = qa_gate.run_gate(
            cwd=repo, write_report=False, exec_prefix=["docker", "compose", "exec", "-T", "gw"]
        )

        pytest_step = next(s for s in report.steps if s.name == "pytest")
        assert pytest_step.env_gap is False
        assert pytest_step.ok is False
        assert report.env_gap is False
        assert report.exit_code == 127


class TestLogStem:
    """#378: step names double as log filenames; `:` is illegal on NTFS and
    `/` is a path separator everywhere, so the full gate died before its
    first step on a Node monorepo (`typecheck:apps/admin`)."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("pytest", "pytest"),
            ("typecheck:apps/admin", "typecheck-apps-admin"),
            ("typecheck:packages/ui", "typecheck-packages-ui"),
            ("test:apps\\api", "test-apps-api"),
            ("lint-imports", "lint-imports"),
            ("weird name*?<>|", "weird-name"),
            ("", "step"),
        ],
    )
    def test_sanitised(self, name: str, expected: str) -> None:
        assert qa_gate._log_stem(name) == expected

    def test_run_step_writes_log_for_colon_slash_name(self, tmp_path, monkeypatch) -> None:
        log_dir = tmp_path / "exports"
        step = qa_gate._run_step(
            "typecheck:apps/admin",
            [sys.executable, "-c", "print('hi')"],
            dict(os.environ),
            tmp_path,
            log_dir,
        )
        assert step.log_path == log_dir / "typecheck-apps-admin.log"
        assert step.log_path.read_text(encoding="utf-8").strip() == "hi"
        assert (log_dir / "typecheck-apps-admin-memory.log").exists()


# ── #436: --auto picks the tier from the diff ────────────────────────────────


def _ignore_venv(root):
    """The fixture venv is untracked; a real project gitignores its own."""
    (root / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    _commit_all(root, "gitignore")


def _commit_all(root, msg="c"):
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=root, check=True)


def test_auto_style_only_diff_runs_no_test_suite(repo, monkeypatch):
    """A CSS/asset/wording-only diff is the #436 case — the gate must say so
    and run nothing, not the whole suite."""
    _make_complete_venv(repo)
    _ignore_venv(repo)
    (repo / "src" / "app.css").write_text(".x{padding:2px}", encoding="utf-8")
    (repo / "README.md").write_text("y", encoding="utf-8")
    recorder: list = []
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, []))

    report = qa_gate.run_gate(cwd=repo, auto=True)

    assert report.ok
    assert report.steps[0].name == "auto-tier"
    assert report.steps[0].detail.startswith("style:")
    assert recorder == []  # no pytest/ruff/lint-imports launched at all
    assert all(s.skipped for s in report.steps[1:])
    assert report.report_path is None


def test_auto_module_logic_runs_only_the_mapped_tests(repo, monkeypatch):
    _make_complete_venv(repo)
    _ignore_venv(repo)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_widget.py").write_text("def test_a(): pass", encoding="utf-8")
    _commit_all(repo)
    (repo / "src" / "widget.py").write_text("X = 2", encoding="utf-8")
    recorder: list = []
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0]))

    report = qa_gate.run_gate(cwd=repo, auto=True)

    assert report.steps[0].detail.startswith("targeted:")
    assert [s.name for s in report.steps] == ["auto-tier", "venv-check", "pytest"]
    assert recorder[0][0][-1] == "tests/test_widget.py"
    assert report.report_path is None


def test_auto_source_without_a_test_widens_to_full(repo, monkeypatch):
    _make_complete_venv(repo)
    _ignore_venv(repo)
    (repo / "src" / "orphan.py").write_text("X = 2", encoding="utf-8")
    recorder: list = []
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0, 0, 0]))
    monkeypatch.setattr(qa_gate, "_runtime_dir", lambda: repo.parent / "rt")

    report = qa_gate.run_gate(cwd=repo, auto=True)

    assert report.steps[0].detail.startswith("full:")
    assert [s.name for s in report.steps] == [
        "auto-tier",
        "venv-check",
        "pytest",
        "ruff",
        "lint-imports",
    ]


def test_auto_tooling_or_schema_change_is_full_tier(repo, monkeypatch):
    _make_complete_venv(repo)
    (repo / "pyproject.toml").write_text('[project]\nname = "y"\n', encoding="utf-8")
    recorder: list = []
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0, 0, 0]))
    monkeypatch.setattr(qa_gate, "_runtime_dir", lambda: repo.parent / "rt")

    report = qa_gate.run_gate(cwd=repo, auto=True)

    assert report.steps[0].detail.startswith("full:")
    assert "pyproject.toml" in report.steps[0].detail


def test_auto_clean_tree_reads_the_last_commit(repo):
    """After the specialist committed, the diff to gate is HEAD~1..HEAD."""
    (repo / "src" / "app.css").write_text(".x{}", encoding="utf-8")
    _commit_all(repo)
    tier = qa_gate.classify_diff(repo, "python")
    assert tier.tier == "style"
    assert tier.files == ["src/app.css"]


def test_auto_explicit_targeted_wins_over_auto(repo, monkeypatch):
    _make_complete_venv(repo)
    (repo / "src" / "app.css").write_text(".x{}", encoding="utf-8")
    recorder: list = []
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0]))
    report = qa_gate.run_gate(cwd=repo, auto=True, targeted=["tests/test_x.py"])
    assert [s.name for s in report.steps] == ["venv-check", "pytest"]


def test_classify_node_style_vs_logic(tmp_path):
    root = tmp_path / "n"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "package.json").write_text("{}", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "i"],
        cwd=root,
        check=True,
    )
    (root / "app").mkdir()
    (root / "app" / "menu.module.css").write_text(".m{}", encoding="utf-8")
    assert qa_gate.classify_diff(root, "node").tier == "style"
    (root / "app" / "Menu.tsx").write_text("export {}", encoding="utf-8")
    assert qa_gate.classify_diff(root, "node").tier == "targeted"
    (root / "app" / "api").mkdir()
    (root / "app" / "api" / "route.ts").write_text("export {}", encoding="utf-8")
    assert qa_gate.classify_diff(root, "node").tier == "full"


def test_auto_non_python_logic_change_widens_to_full_not_empty_targeted(repo, monkeypatch):
    """`--targeted []` would silently be a full pytest run with a 'targeted'
    label — a script/binary change must be called full out loud instead."""
    _make_complete_venv(repo)
    _ignore_venv(repo)
    (repo / "tools.sh").write_text("echo hi", encoding="utf-8")
    tier = qa_gate.classify_diff(repo, "python")
    assert tier.tier == "full"
    assert tier.targeted == []
