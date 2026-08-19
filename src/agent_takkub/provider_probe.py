"""Shared provider-binary probing: WHICH binary a `ProviderSpec` resolves to
on this machine, and running `--version` against it.

Extracted from `doctor.py` (#309 Phase 4) so `core.versioning.detector`
shares the EXACT same resolution logic `doctor.check_providers()` /
`doctor.check_provider_auth()` already use — one resolver, not two
independently-drifting copies (`_resolve_provider_bin`/`_run` in doctor.py
now just re-export these). Behavior is unchanged from the pre-extraction
functions: this is a straight move, not a rewrite.

Pure leaf: stdlib + `_win_console` only, no sibling cockpit modules — safe
for `agent_takkub.core` (`core-is-bottom-layer`, pyproject.toml) to import.
"""

from __future__ import annotations

import shutil
import subprocess


def resolve_provider_bin(spec) -> str | None:
    """Resolve `spec`'s binary via the SAME custom_discovery_fn the cockpit
    uses at spawn time — e.g. gemini's `find_agy_executable()` falls back to
    %LOCALAPPDATA%\\agy\\bin when the installer didn't register PATH, so a
    caller doesn't falsely report "not installed" for a role that actually
    works."""
    try:
        if spec.custom_discovery_fn is not None:
            return spec.custom_discovery_fn()
    except Exception:
        pass
    for name in spec.binary_names or (spec.name,):
        found = shutil.which(name)
        if found:
            return found
    return None


def run_probe(argv: list[str], timeout: float = 5) -> tuple[int, str]:
    """Run *argv* with a short timeout. Returns (returncode, combined
    output). Never raises."""
    from ._win_console import SUBPROCESS_NO_WINDOW

    try:
        r = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
        out = (r.stdout or "").strip() or (r.stderr or "").strip()
        return r.returncode, out
    except FileNotFoundError:
        return 1, f"not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return 1, "timed out"
    except Exception as e:
        return 1, str(e)
