from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def test_deterministic_stress_harness_a_through_i(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [
            str(root / ".venv" / "Scripts" / "python.exe"),
            str(root / "tools" / "performance_reliability_stress.py"),
            "--seed",
            "12345",
            "--stall-seconds",
            "0.01",
            "--output-dir",
            str(tmp_path),
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    locator = json.loads(result.stdout.strip().splitlines()[-1])
    report = json.loads(Path(locator["json"]).read_text(encoding="utf-8"))
    assert report["pass"] is True
    assert [row["scenario"] for row in report["scenarios"]] == [
        "STR-A",
        "STR-B",
        "STR-C",
        "STR-D",
        "STR-E",
        "STR-F",
        "STR-G",
        "STR-H",
        "STR-I",
    ]
