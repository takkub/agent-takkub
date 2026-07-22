"""Install a provider CLI from its ProviderSpec (`install_command`).

One installer path shared by every surface that offers "install this
provider": `takkub doctor --fix` (Finding.auto_fix), the `takkub provider
install <name>` CLI, and Settings → Providers & Roles. All of them
call :func:`install_provider`, so behavior (discovery short-circuit,
resolution of the package-manager binary, post-install verification, login
reminder) can never drift between surfaces.

Providers whose spec has ``install_command=None`` (agy — GUI installer
download) are reported as manual-install with the spec's human
``install_instructions``; nothing is executed.

Cross-platform: the first command token is resolved via ``shutil.which`` so
``npm`` finds ``npm.cmd`` on Windows and the plain binary on macOS/Linux.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

from ._win_console import SUBPROCESS_NO_WINDOW
from .provider_spec import PROVIDER_REGISTRY, ProviderSpec

_log = logging.getLogger(__name__)

# Package-manager installs of these CLIs can take ~1-2 min on a cold cache;
# give slack for a slow network without letting a wedged install hang forever.
_INSTALL_TIMEOUT_S = 600


def _discover(spec: ProviderSpec) -> str | None:
    """Locate the provider binary — spec probe first, bare PATH fallback."""
    try:
        if spec.custom_discovery_fn is not None:
            found = spec.custom_discovery_fn()
            if found:
                return found
    except Exception:
        pass
    for name in spec.binary_names or (spec.name,):
        found = shutil.which(name)
        if found:
            return found
    return None


def installable_providers() -> list[str]:
    """Names of registered providers that carry a machine-runnable installer."""
    return [
        name
        for name, spec in PROVIDER_REGISTRY.items()
        if name != "claude" and spec.install_command
    ]


def install_provider(name: str) -> tuple[bool, str]:
    """Install provider *name*'s CLI. Returns (ok, human message).

    Idempotent: already-installed → (True, "already installed ..."). A
    successful package-manager run is only reported ok after the binary
    actually resolves (post-install verification) — a package-manager exit 0 that
    still leaves nothing on PATH is a failure, not a success.
    """
    spec = PROVIDER_REGISTRY.get(name)
    if spec is None:
        return False, f"unknown provider: {name!r} (known: {', '.join(PROVIDER_REGISTRY)})"
    if name == "claude":
        return False, "claude is the cockpit's baseline CLI — manage it via npm/claude directly"

    existing = _discover(spec)
    if existing:
        return True, f"already installed: {existing}"

    if not spec.install_command:
        return False, f"no automated installer for {name} — {spec.install_instructions}"

    program = shutil.which(spec.install_command[0])
    if program is None:
        return False, (
            f"cannot install {name}: `{spec.install_command[0]}` not found on PATH "
            f"(install `{spec.install_command[0]}` first)"
        )

    argv = [program, *spec.install_command[1:]]
    _log.info("installing provider %s: %s", name, argv)
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_INSTALL_TIMEOUT_S,
            check=False,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return False, f"install timed out after {_INSTALL_TIMEOUT_S}s: {' '.join(argv)}"
    except OSError as e:
        return False, f"install failed to launch: {e}"

    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
        return False, f"install exited {result.returncode}: {' | '.join(tail) or 'no output'}"

    # Post-install verification — trust the filesystem, not npm's exit code.
    installed = _discover(spec)
    if installed is None:
        return False, (
            f"installer succeeded but {name} still not found on PATH — "
            "a new terminal/PATH refresh may be needed"
        )
    note = f" · next: {spec.post_install_note}" if spec.post_install_note else ""
    return True, f"installed {name}: {installed}{note}"
