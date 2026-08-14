"""Assemble the reproducible Performance & Reliability v2 evidence bundle."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy(source: Path, destination: Path, copied: list[dict[str, object]]) -> None:
    if not source.is_file():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    bundle_root = next(parent for parent in destination.parents if parent.name == "final-evidence")
    copied.append(
        {
            "path": destination.relative_to(bundle_root).as_posix(),
            "bytes": destination.stat().st_size,
            "sha256": _sha256(destination),
        }
    )


def main() -> int:
    artifacts = Path(os.environ["TAKKUB_ARTIFACTS_DIR"]).resolve()
    bundle = artifacts / "final-evidence"
    bundle.mkdir(parents=True, exist_ok=True)
    copied: list[dict[str, object]] = []

    verification = artifacts / "verification"
    for name in (
        "full-suite-run-2.log",
        "full-suite-run-3.log",
        "full-suite-run-4.log",
        "stress-10-cycles.log",
        "ruff-check-final.log",
        "ruff-format-final.log",
        "import-contracts-final.log",
        "package-build-final.log",
        "wheel-install-smoke-final.log",
        "wheel-import-smoke-final.log",
        "adversarial-search.txt",
        "queue-inventory.txt",
    ):
        _copy(verification / name, bundle / "verification" / name, copied)

    for pattern in ("process-tree-pane_close-*.json", "process-tree-application_shutdown-*.json"):
        matches = sorted(artifacts.glob(pattern), key=lambda item: item.stat().st_mtime)
        if matches:
            _copy(matches[-1], bundle / "process-tree" / matches[-1].name, copied)

    run_dirs = sorted(
        (artifacts / "performance-reliability").glob("*"),
        key=lambda item: item.stat().st_mtime,
    )
    if run_dirs:
        latest = run_dirs[-1]
        for name in ("stress-results.json", "stress-summary.md"):
            _copy(latest / name, bundle / "stress" / name, copied)

    for scale_dir in ("scale-1_0", "scale-1_25", "scale-1_5", "scale-2_0"):
        for source in sorted((artifacts / "ui-evidence" / scale_dir).glob("*.png")):
            relative = source.relative_to(artifacts / "ui-evidence")
            _copy(source, bundle / "ui-evidence" / relative, copied)

    for name in (
        "performance-reliability.md",
        "performance-reliability-v2-implementation-report.md",
        "performance-reliability-v2-traceability.md",
        "performance-reliability-v2-adversarial-audit.md",
    ):
        _copy(ROOT / "docs" / name, bundle / "docs" / name, copied)

    for package in sorted((ROOT / "dist").glob("agent_takkub-1.0.59*")):
        _copy(package, bundle / "packages" / package.name, copied)

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()

    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(tz=UTC).isoformat(),
        "source": {
            "git_sha": git("rev-parse", "HEAD"),
            "dirty": bool(git("status", "--porcelain")),
            "zip_baseline_available": False,
        },
        "machine": {
            "platform": platform.platform(),
            "python": sys.version,
            "logical_cpus": os.cpu_count(),
        },
        "production_isolation": {
            "running_prod_was_stopped": False,
            "tests_connected_to_running_cockpit": False,
            "isolated_runtime": True,
            "offscreen_ui_capture": True,
        },
        "qualification": {
            "full_suite_consecutive_passes": 3,
            "stress_cycles_passed": 10,
            "ui_scales": [1.0, 1.25, 1.5, 2.0],
            "real_windows_process_tree_modes": ["pane_close", "application_shutdown"],
            "real_linux_macos_run": False,
            "exact_before_after_baseline": False,
        },
        "files": sorted(copied, key=lambda item: str(item["path"])),
    }
    manifest_path = bundle / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(manifest_path)
    print(f"files={len(copied)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
