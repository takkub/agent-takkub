"""Release metadata must expose one version everywhere users can read it."""

from __future__ import annotations

import json
from pathlib import Path

from agent_takkub import __version__
from agent_takkub.release import read_pyproject_version


def test_python_and_npm_versions_match_pyproject() -> None:
    root = Path(__file__).resolve().parents[1]
    project_version = read_pyproject_version((root / "pyproject.toml").read_text(encoding="utf-8"))
    npm_version = json.loads((root / "package.json").read_text(encoding="utf-8"))["version"]

    assert __version__ == project_version == npm_version
