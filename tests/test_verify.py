"""Tests for verify.py — auto-detect stack + run lint/test gate."""

from __future__ import annotations

import json
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
    assert "npm-test" in names


def test_detect_stack_package_json_with_tsconfig(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {}}))
    (tmp_path / "tsconfig.json").write_text("{}")
    checks = detect_stack(tmp_path)
    names = [c.name for c in checks]
    assert "tsc" in names


def test_detect_stack_package_json_with_eslintrc(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {}}))
    (tmp_path / ".eslintrc.json").write_text("{}")
    checks = detect_stack(tmp_path)
    names = [c.name for c in checks]
    assert "eslint" in names


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
    assert "npm-test" in names


# ---------------------------------------------------------------------------
# run_checks
# ---------------------------------------------------------------------------


def _make_check(name: str, cmd: list[str], stack: str = "python") -> Check:
    return Check(name=name, cmd=cmd, stack=stack)


def test_run_checks_all_passing(tmp_path: Path) -> None:
    checks = [_make_check("echo", ["python", "-c", "import sys; sys.exit(0)"])]
    result = run_checks(checks, cwd=tmp_path)
    assert result.all_passed is True
    assert result.checks[0].exit_code == 0


def test_run_checks_one_failing(tmp_path: Path) -> None:
    checks = [
        _make_check("ok", ["python", "-c", "import sys; sys.exit(0)"]),
        _make_check("fail", ["python", "-c", "import sys; sys.exit(1)"]),
    ]
    result = run_checks(checks, cwd=tmp_path)
    assert result.all_passed is False


def test_run_checks_captures_stdout_stderr(tmp_path: Path) -> None:
    long_out = "x" * 200
    checks = [_make_check("print", ["python", "-c", f"print('{long_out}')"])]
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
