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
    assert checks[0].cmd[1:] == ["run", "verify"]
    assert "pnpm" in Path(checks[0].cmd[0]).name


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
