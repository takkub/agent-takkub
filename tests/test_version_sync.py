"""Release metadata must expose one version everywhere users can read it."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import yaml

from agent_takkub import __version__
from agent_takkub.release import read_pyproject_version


def test_python_and_npm_versions_match_pyproject() -> None:
    root = Path(__file__).resolve().parents[1]
    project_version = read_pyproject_version((root / "pyproject.toml").read_text(encoding="utf-8"))
    npm_version = json.loads((root / "package.json").read_text(encoding="utf-8"))["version"]

    assert __version__ == project_version == npm_version


def _pre_commit_rev(repo_url_suffix: str) -> str:
    root = Path(__file__).resolve().parents[1]
    hooks = yaml.safe_load((root / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    for repo in hooks["repos"]:
        if repo["repo"].endswith(repo_url_suffix):
            return repo["rev"]
    raise AssertionError(f"no repo ending with {repo_url_suffix!r} in .pre-commit-config.yaml")


def test_ruff_pin_matches_pre_commit_rev() -> None:
    # #246: pyproject.toml's ruff==X dev pin and .pre-commit-config.yaml's
    # ruff-pre-commit rev must move together — CI and `pip install -e .[dev]`
    # use the former, the local pre-commit gate uses the latter. Drift means
    # the local gate can pass on rules a newer/older CI ruff doesn't have.
    root = Path(__file__).resolve().parents[1]
    dev_deps = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]["dev"]
    (ruff_dep,) = (dep for dep in dev_deps if dep.startswith("ruff=="))
    pyproject_version = ruff_dep.removeprefix("ruff==")

    rev = _pre_commit_rev("astral-sh/ruff-pre-commit")
    pre_commit_version = rev.removeprefix("v")

    assert pyproject_version == pre_commit_version


def test_gitleaks_pin_matches_security_workflow() -> None:
    # Same drift risk as ruff (#246): .pre-commit-config.yaml's gitleaks rev
    # and .github/workflows/security.yml's pinned VER must match, or the
    # local pre-commit gate and CI's secret scan silently run different
    # gitleaks releases.
    root = Path(__file__).resolve().parents[1]
    rev = _pre_commit_rev("gitleaks/gitleaks")
    pre_commit_version = rev.removeprefix("v")

    workflow = (root / ".github" / "workflows" / "security.yml").read_text(encoding="utf-8")
    (workflow_version,) = re.findall(r"^\s*VER=([\d.]+)\s*$", workflow, flags=re.MULTILINE)

    assert pre_commit_version == workflow_version


def test_qt_dependencies_match_doctor_supported_lts_series() -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    qt_dependencies = {dep for dep in project["dependencies"] if dep.startswith("PyQt6")}

    assert qt_dependencies == {
        "PyQt6>=6.8,<6.9",
        "PyQt6-Qt6>=6.8,<6.9",
        "PyQt6-WebEngine>=6.8,<6.9",
        "PyQt6-WebEngine-Qt6>=6.8,<6.9",
    }
