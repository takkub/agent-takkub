"""Tests for verify.py — auto-detect stack + run lint/test gate."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agent_takkub.verify import (
    Check,
    CheckResult,
    VerifyResult,
    detect_stack,
    format_summary,
    run_checks,
)

# ---------------------------------------------------------------------------
# detect_stack
# ---------------------------------------------------------------------------


def test_detect_stack_pyproject_with_tests_dir(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (tmp_path / "tests").mkdir()
    checks = detect_stack(tmp_path)
    names = [c.name for c in checks]
    assert "pytest" in names
    assert "ruff-lint" in names
    assert "ruff-format" in names


def test_detect_stack_pyproject_without_tests_dir(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    checks = detect_stack(tmp_path)
    names = [c.name for c in checks]
    assert "pytest" not in names
    assert "ruff-lint" in names
    assert "ruff-format" in names


def test_detect_stack_package_json_with_test_script(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    checks = detect_stack(tmp_path)
    names = [c.name for c in checks]
    assert "test" in names


def test_detect_stack_package_json_with_tsconfig(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {}}))
    (tmp_path / "tsconfig.json").write_text("{}")
    checks = detect_stack(tmp_path)
    names = [c.name for c in checks]
    assert "typecheck" in names
    tc = next(c for c in checks if c.name == "typecheck")
    assert tc.cmd[-4:] == ["tsc", "-p", "tsconfig.json", "--noEmit"]


def test_detect_stack_package_json_with_eslintrc(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {}}))
    (tmp_path / ".eslintrc.json").write_text("{}")
    checks = detect_stack(tmp_path)
    names = [c.name for c in checks]
    assert "lint" in names


def test_detect_stack_empty_cwd(tmp_path: Path) -> None:
    checks = detect_stack(tmp_path)
    assert checks == []


def test_detect_stack_mixed_both_stacks(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    checks = detect_stack(tmp_path)
    names = [c.name for c in checks]
    assert "pytest" in names
    assert "test" in names


# ---------------------------------------------------------------------------
# #368 — Node gate must typecheck, always, and use the project's own pm
# ---------------------------------------------------------------------------


def _pkg(tmp_path: Path, scripts: dict, **extra) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"scripts": scripts, **extra}))


def test_node_verify_script_wins_and_runs_alone(tmp_path: Path) -> None:
    """lottery shape: root `verify` = `turbo run typecheck test`, no root
    tsconfig — the old gate ran only `npm test` here (false-PASS, #368)."""
    _pkg(
        tmp_path,
        {
            "test": "turbo run test",
            "typecheck": "turbo run typecheck",
            "verify": "turbo run typecheck test",
        },
    )
    (tmp_path / "pnpm-lock.yaml").write_text("")
    checks = detect_stack(tmp_path)
    assert [c.name for c in checks] == ["verify"]
    # #600: a turbo-backed script must force a real, fully-logged run — a
    # cache hit would otherwise replay a bare `PASS 58.7s` line with no
    # underlying jest/vitest summary. #608: `--continue` so one workspace
    # failing doesn't fail-fast-kill every other workspace's task.
    assert checks[0].cmd[1:] == [
        "run",
        "verify",
        "--",
        "--output-logs=full",
        "--force",
        "--continue",
    ]
    assert "pnpm" in Path(checks[0].cmd[0]).name


def test_node_test_script_turbo_forces_full_output_no_cache(tmp_path: Path) -> None:
    """#600: qa-gate's own log must always carry proof of a real run — never
    a turbo cache-hit stub with no `Tests: N passed` line."""
    _pkg(tmp_path, {"test": "turbo run test"})
    (tmp_path / "package-lock.json").write_text("{}")
    checks = detect_stack(tmp_path)
    test_check = next(c for c in checks if c.name == "test")
    assert test_check.cmd[-4:] == ["--", "--output-logs=full", "--force", "--continue"]


def test_node_test_script_non_turbo_untouched(tmp_path: Path) -> None:
    """A plain (non-turbo) `test` script must not gain turbo-only flags."""
    _pkg(tmp_path, {"test": "jest --ci"}, devDependencies={"jest": "^29.0.0"})
    (tmp_path / "package-lock.json").write_text("{}")
    checks = detect_stack(tmp_path)
    test_check = next(c for c in checks if c.name == "test")
    assert test_check.cmd[1:] == ["run", "test"]


def test_node_test_script_turbo_json_present_but_script_direct_untouched(tmp_path: Path) -> None:
    """#600 follow-up: `turbo.json`/turbo in devDependencies for OTHER
    scripts must not flag a `test`/`verify` script that calls vitest/jest
    directly — a real monorepo sub-package shape. The old deps/turbo.json
    presence check sent `-- --output-logs=full --force` into vitest, which
    rejects it as an unknown option and turns a healthy script red."""
    _pkg(tmp_path, {"test": "vitest run"}, devDependencies={"turbo": "^2.0.0"})
    (tmp_path / "turbo.json").write_text("{}")
    (tmp_path / "package-lock.json").write_text("{}")
    checks = detect_stack(tmp_path)
    test_check = next(c for c in checks if c.name == "test")
    assert test_check.cmd[1:] == ["run", "test"]


def test_node_test_script_turbo_chained_command_forces_full_output(tmp_path: Path) -> None:
    """A chained script (`lint && turbo run test`) still routes through
    turbo and must still be forced to a real, fully-logged run."""
    _pkg(tmp_path, {"test": "eslint . && turbo run test"})
    (tmp_path / "package-lock.json").write_text("{}")
    checks = detect_stack(tmp_path)
    test_check = next(c for c in checks if c.name == "test")
    assert test_check.cmd[-4:] == ["--", "--output-logs=full", "--force", "--continue"]


def test_node_test_script_npx_turbo_forces_full_output(tmp_path: Path) -> None:
    """#605 L3: `npx turbo ...` is still turbo, even though the first word
    isn't literally `turbo`."""
    _pkg(tmp_path, {"test": "npx turbo run test"})
    (tmp_path / "package-lock.json").write_text("{}")
    checks = detect_stack(tmp_path)
    test_check = next(c for c in checks if c.name == "test")
    assert test_check.cmd[-4:] == ["--", "--output-logs=full", "--force", "--continue"]


def test_node_test_script_pnpm_exec_turbo_forces_full_output(tmp_path: Path) -> None:
    """#605 L3: `pnpm exec turbo ...` must also be detected."""
    _pkg(tmp_path, {"test": "pnpm exec turbo run test"})
    (tmp_path / "pnpm-lock.yaml").write_text("")
    checks = detect_stack(tmp_path)
    test_check = next(c for c in checks if c.name == "test")
    assert test_check.cmd[-4:] == ["--", "--output-logs=full", "--force", "--continue"]


def test_node_test_script_pnpm_turbo_direct_forces_full_output(tmp_path: Path) -> None:
    """#605 L3: `pnpm turbo ...` (direct bin invocation, no `exec`) must
    also be detected — as well as `yarn exec turbo ...`."""
    _pkg(tmp_path, {"test": "pnpm turbo run test"})
    (tmp_path / "pnpm-lock.yaml").write_text("")
    checks = detect_stack(tmp_path)
    test_check = next(c for c in checks if c.name == "test")
    assert test_check.cmd[-4:] == ["--", "--output-logs=full", "--force", "--continue"]


# ---------------------------------------------------------------------------
# #607 — limit_concurrency: opt-in worker/task-parallelism cap
# ---------------------------------------------------------------------------


def test_limit_concurrency_default_false_leaves_turbo_untouched(tmp_path: Path) -> None:
    """Default (no caller opts in) must be byte-identical to before #607 —
    every existing detect_stack/node_checks caller keeps its current
    command shape."""
    _pkg(tmp_path, {"test": "turbo run test"})
    (tmp_path / "package-lock.json").write_text("{}")
    checks = detect_stack(tmp_path)
    test_check = next(c for c in checks if c.name == "test")
    assert "--concurrency=1" not in test_check.cmd


def test_limit_concurrency_true_adds_turbo_concurrency_flag(tmp_path: Path) -> None:
    _pkg(tmp_path, {"test": "turbo run test"})
    (tmp_path / "package-lock.json").write_text("{}")
    checks = detect_stack(tmp_path, limit_concurrency=True)
    test_check = next(c for c in checks if c.name == "test")
    assert test_check.cmd[-5:] == [
        "--",
        "--output-logs=full",
        "--force",
        "--continue",
        "--concurrency=1",
    ]


def test_limit_concurrency_true_caps_direct_vitest(tmp_path: Path) -> None:
    """A bare (non-turbo) `vitest run` in a monorepo sub-package hits the
    same worker-pool timeout under machine load — must still get capped."""
    _pkg(tmp_path, {"test": "vitest run"}, devDependencies={"vitest": "^2.0.0"})
    (tmp_path / "package-lock.json").write_text("{}")
    checks = detect_stack(tmp_path, limit_concurrency=True)
    test_check = next(c for c in checks if c.name == "test")
    assert test_check.cmd[1:] == [
        "run",
        "test",
        "--",
        "--pool=forks",
        "--poolOptions.forks.maxForks=2",
    ]


def test_limit_concurrency_true_caps_direct_vitest4_with_maxworkers(tmp_path: Path) -> None:
    """#607 H4: Vitest 4 dropped `--poolOptions.forks.maxForks` — CLI
    parsing rejects it outright (`CACError: Unknown option --poolOptions`),
    verified against a real Vitest 4.0.0 binary. The version-gated flag
    must switch to `--maxWorkers=N` once vitest resolves to major >= 4."""
    _pkg(tmp_path, {"test": "vitest run"}, devDependencies={"vitest": "^4.0.0"})
    (tmp_path / "package-lock.json").write_text("{}")
    checks = detect_stack(tmp_path, limit_concurrency=True)
    test_check = next(c for c in checks if c.name == "test")
    assert test_check.cmd[1:] == ["run", "test", "--", "--maxWorkers=2"]


def test_limit_concurrency_true_prefers_installed_over_declared_vitest_version(
    tmp_path: Path,
) -> None:
    """The installed node_modules version is ground truth over a declared
    devDependency spec (e.g. a caret range left stale after a major bump)."""
    _pkg(tmp_path, {"test": "vitest run"}, devDependencies={"vitest": "^2.0.0"})
    (tmp_path / "package-lock.json").write_text("{}")
    vitest_pkg = tmp_path / "node_modules" / "vitest"
    vitest_pkg.mkdir(parents=True)
    (vitest_pkg / "package.json").write_text(json.dumps({"version": "4.0.0"}))
    checks = detect_stack(tmp_path, limit_concurrency=True)
    test_check = next(c for c in checks if c.name == "test")
    assert test_check.cmd[1:] == ["run", "test", "--", "--maxWorkers=2"]


def test_limit_concurrency_true_injects_nothing_when_vitest_version_unknown(
    tmp_path: Path,
) -> None:
    """#607 H4: with no node_modules and no declared vitest version to read,
    guessing either flag family risks a hard CLI-parse failure on whichever
    major is actually installed — inject nothing (fail-safe) rather than
    turn a healthy project's gate red."""
    _pkg(tmp_path, {"test": "vitest run"})
    (tmp_path / "package-lock.json").write_text("{}")
    checks = detect_stack(tmp_path, limit_concurrency=True)
    test_check = next(c for c in checks if c.name == "test")
    assert test_check.cmd[1:] == ["run", "test"]


def test_limit_concurrency_true_caps_direct_jest(tmp_path: Path) -> None:
    _pkg(tmp_path, {"test": "jest --ci"}, devDependencies={"jest": "^29.0.0"})
    (tmp_path / "package-lock.json").write_text("{}")
    checks = detect_stack(tmp_path, limit_concurrency=True)
    test_check = next(c for c in checks if c.name == "test")
    assert test_check.cmd[1:] == ["run", "test", "--", "--maxWorkers=50%"]


def test_limit_concurrency_true_leaves_script_with_own_pool_flag_alone(tmp_path: Path) -> None:
    """A script that already pins concurrency itself must not get a second,
    possibly conflicting flag forced onto it."""
    _pkg(
        tmp_path,
        {"test": "vitest run --pool=threads"},
        devDependencies={"vitest": "^2.0.0"},
    )
    (tmp_path / "package-lock.json").write_text("{}")
    checks = detect_stack(tmp_path, limit_concurrency=True)
    test_check = next(c for c in checks if c.name == "test")
    assert test_check.cmd[1:] == ["run", "test"]


def test_node_typecheck_script_runs_before_test(tmp_path: Path) -> None:
    _pkg(tmp_path, {"test": "vitest run", "typecheck": "tsc --noEmit"})
    (tmp_path / "yarn.lock").write_text("")
    checks = detect_stack(tmp_path)
    assert [c.name for c in checks] == ["typecheck", "test"]
    assert all("yarn" in Path(c.cmd[0]).name for c in checks)
    assert checks[0].cmd[1:] == ["run", "typecheck"]


def test_node_root_tsconfig_falls_back_to_tsc_then_test(tmp_path: Path) -> None:
    _pkg(tmp_path, {"test": "vitest run"})
    (tmp_path / "tsconfig.json").write_text("{}")
    (tmp_path / "package-lock.json").write_text("{}")
    checks = detect_stack(tmp_path)
    assert [c.name for c in checks] == ["typecheck", "test"]
    assert "npx" in Path(checks[0].cmd[0]).name
    assert checks[0].cmd[-3:] == ["-p", "tsconfig.json", "--noEmit"]
    assert checks[0].cwd is None


def test_node_monorepo_without_root_tsconfig_typechecks_each_workspace(tmp_path: Path) -> None:
    _pkg(tmp_path, {"test": "vitest run"})
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'apps/*'\n  - packages/*\n")
    (tmp_path / "pnpm-lock.yaml").write_text("")
    for rel in ("apps/api", "apps/web", "packages/shared", "packages/no-ts"):
        d = tmp_path / rel
        d.mkdir(parents=True)
        (d / "package.json").write_text("{}")
        if rel != "packages/no-ts":
            (d / "tsconfig.json").write_text("{}")
    (tmp_path / "apps" / "api" / "node_modules" / "dep").mkdir(parents=True)
    checks = detect_stack(tmp_path)
    names = [c.name for c in checks]
    assert names == [
        "typecheck:apps/api",
        "typecheck:apps/web",
        "typecheck:packages/shared",
        "test",
    ]
    assert checks[0].cwd == tmp_path / "apps" / "api"
    assert checks[0].cmd[1:] == ["exec", "tsc", "-p", "tsconfig.json", "--noEmit"]


def test_node_package_json_workspaces_field_is_honoured(tmp_path: Path) -> None:
    _pkg(tmp_path, {"test": "jest"}, workspaces={"packages": ["libs/*"]})
    d = tmp_path / "libs" / "core"
    d.mkdir(parents=True)
    (d / "package.json").write_text("{}")
    (d / "tsconfig.json").write_text("{}")
    checks = detect_stack(tmp_path)
    assert [c.name for c in checks] == ["typecheck:libs/core", "test"]


def test_node_without_typescript_still_just_runs_test(tmp_path: Path) -> None:
    _pkg(tmp_path, {"test": "jest"})
    checks = detect_stack(tmp_path)
    assert [c.name for c in checks] == ["test"]


def test_detect_package_manager_order(tmp_path: Path) -> None:
    from agent_takkub.verify import detect_package_manager

    assert detect_package_manager(tmp_path) == "npm"
    assert detect_package_manager(tmp_path, {"packageManager": "pnpm@9.1.0"}) == "pnpm"
    (tmp_path / "yarn.lock").write_text("")
    assert detect_package_manager(tmp_path, {"packageManager": "pnpm@9.1.0"}) == "yarn"
    (tmp_path / "pnpm-lock.yaml").write_text("")
    assert detect_package_manager(tmp_path) == "pnpm"


def test_run_checks_uses_the_checks_own_cwd(tmp_path: Path) -> None:
    sub = tmp_path / "pkg"
    sub.mkdir()
    c = Check(
        name="x",
        cmd=[sys.executable, "-c", "import os; print(os.getcwd())"],
        stack="node",
        cwd=sub,
    )
    res = run_checks([c], tmp_path)
    assert res.all_passed
    assert Path(res.checks[0].stdout_tail.strip()).resolve() == sub.resolve()


# ---------------------------------------------------------------------------
# run_checks
# ---------------------------------------------------------------------------


def _make_check(name: str, cmd: list[str], stack: str = "python") -> Check:
    return Check(name=name, cmd=cmd, stack=stack)


def test_run_checks_all_passing(tmp_path: Path) -> None:
    checks = [_make_check("echo", [sys.executable, "-c", "import sys; sys.exit(0)"])]
    result = run_checks(checks, cwd=tmp_path)
    assert result.all_passed is True
    assert result.checks[0].exit_code == 0


def test_run_checks_one_failing(tmp_path: Path) -> None:
    checks = [
        _make_check("ok", [sys.executable, "-c", "import sys; sys.exit(0)"]),
        _make_check("fail", [sys.executable, "-c", "import sys; sys.exit(1)"]),
    ]
    result = run_checks(checks, cwd=tmp_path)
    assert result.all_passed is False


def test_run_checks_captures_stdout_stderr(tmp_path: Path) -> None:
    long_out = "x" * 200
    checks = [_make_check("print", [sys.executable, "-c", f"print('{long_out}')"])]
    result = run_checks(checks, cwd=tmp_path)
    # stdout_tail captures last 50 lines — a single long line still appears
    assert "x" in result.checks[0].stdout_tail


# ---------------------------------------------------------------------------
# format_summary
# ---------------------------------------------------------------------------


def test_format_summary_empty() -> None:
    result = VerifyResult(checks=[], all_passed=True)
    summary = format_summary(result)
    assert "No checks configured" in summary


def test_format_summary_mix_pass_fail(tmp_path: Path) -> None:
    pass_check = Check(name="pytest", cmd=["python", "-m", "pytest"], stack="python")
    fail_check = Check(
        name="ruff-lint", cmd=["python", "-m", "ruff", "check", "src"], stack="python"
    )
    results = [
        CheckResult(
            check=pass_check, exit_code=0, stdout_tail="", stderr_tail="", duration_ms=4600.0
        ),
        CheckResult(
            check=fail_check,
            exit_code=1,
            stdout_tail="E001 error",
            stderr_tail="",
            duration_ms=200.0,
        ),
    ]
    result = VerifyResult(checks=results, all_passed=False)
    summary = format_summary(result)
    assert "pytest" in summary
    assert "ruff-lint" in summary
    assert "PASS" in summary
    assert "FAIL" in summary
