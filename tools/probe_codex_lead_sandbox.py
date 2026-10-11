"""No-model release probe of Codex restricted Lead filesystem enforcement."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_takkub.lead_enforcement import verify_codex_boundary


def main() -> None:
    codex = shutil.which("codex")
    if not codex:
        raise SystemExit("codex CLI is required for the sandbox release probe")
    with tempfile.TemporaryDirectory(prefix="takkub-lead-sandbox-") as tmp:
        scratch = Path(tmp) / "scratch"
        scratch.mkdir()
        verify_codex_boundary(codex, str(Path.cwd()), os.environ.copy(), [str(scratch)])
    print("Codex restricted Lead sandbox probe passed")


if __name__ == "__main__":
    main()
