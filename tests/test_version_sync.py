"""Release metadata must expose one version everywhere users can read it."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from agent_takkub import __version__
from agent_takkub.release import read_pyproject_version


def test_python_and_npm_versions_match_pyproject() -> None:
    root = Path(__file__).resolve().parents[1]
    project_version = read_pyproject_version((root / "pyproject.toml").read_text(encoding="utf-8"))
    npm_version = json.loads((root / "package.json").read_text(encoding="utf-8"))["version"]

    assert __version__ == project_version == npm_version


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
