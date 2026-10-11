"""Run local pre-commit gates from the shared venv on Windows and POSIX (#863)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {
        "import-linter",
        "pytest-collect-only",
        "depgraph-fresh",
    }:
        raise SystemExit(
            "usage: run_precommit_gate.py import-linter|pytest-collect-only|depgraph-fresh"
        )
    common = subprocess.check_output(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).strip()
    root = Path(common).resolve().parent
    checkout = Path.cwd()
    binary_dir = root / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    python = binary_dir / ("python.exe" if os.name == "nt" else "python")
    commands = {
        "import-linter": [
            str(binary_dir / ("lint-imports.exe" if os.name == "nt" else "lint-imports"))
        ],
        "pytest-collect-only": [str(python), "-m", "pytest", "tests", "--collect-only", "-q"],
        "depgraph-fresh": [str(python), "tools/gen_import_graph.py", "--check"],
    }
    env = os.environ.copy()
    env["PYTHONPATH"] = str(checkout / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.call(
        commands[sys.argv[1]],
        cwd=checkout,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


if __name__ == "__main__":
    raise SystemExit(main())
