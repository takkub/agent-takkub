"""The version number lives in three places and they must agree.

Release 2.1.14 (2026-09-17) bumped `pyproject.toml` + `package.json` but left
`agent_takkub.__version__` at "2.1.13" — every auto-captured issue from a
2.1.14 install (`auto_issue_capture`/`auto_issue_signals` stamp
`__version__` into the report body) then blamed the wrong release, and the
boot-migration/versioning stores (`core/migration/steps.py`,
`core/versioning/store.py`) recorded the wrong app version too. CI was green
the whole time because nothing pinned the three together — this does.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import agent_takkub

_ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
    assert m, "pyproject.toml lost its version line"
    return m.group(1)


def _package_json_version() -> str:
    data = json.loads((_ROOT / "package.json").read_text(encoding="utf-8"))
    return data["version"]


def test_dunder_version_matches_pyproject() -> None:
    assert agent_takkub.__version__ == _pyproject_version()


def test_package_json_matches_pyproject() -> None:
    assert _package_json_version() == _pyproject_version()
