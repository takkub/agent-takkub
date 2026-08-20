"""Boot-time provider CLI update runner (#313 boot-update policy).

Pure business logic — no Qt, no widget imports — so it stays unit-testable
without a display and importable from both the boot splash (`boot_update_window.py`)
and a future non-UI caller (headless boot, `takkub doctor`) without pulling in
PyQt6.

Design: the ORIGINAL #313 incident was an npm self-update racing a live
pane's spawn of `claude.exe`. `pane_env.inject_provider_no_autoupdate_env`
now suppresses every provider's own self-update knob for the lifetime of a
pane (see that module) — so the only place a provider CLI is EVER allowed to
update itself is here, once, at cockpit boot, before any pane exists to race
it. `app.py`'s boot-update gate runs this for every eligible provider and
only constructs MainWindow (and therefore allows the first pane spawn) once
every row reaches a terminal state or a bounded timeout elapses.

Eligibility (user directive 2026-08-20): update ONLY a provider that is both
INSTALLED and ENABLED. A provider that is either not installed or disabled
via `provider_state` is never probed or downloaded — it just reports
"skipped" instantly, no network/subprocess cost.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from ._win_console import SUBPROCESS_NO_WINDOW

# Bounded so one hung installer can't stall the whole boot-update phase past
# the outer gate's own timeout (see boot_update_window.py) — this is a
# per-provider ceiling, the gate timeout is the overall one.
_UPDATE_TIMEOUT_S = 180.0

STATUS_UP_TO_DATE = "up_to_date"
STATUS_UPDATED = "updated"
STATUS_FAILED = "failed"
STATUS_SKIPPED_NOT_INSTALLED = "skipped_not_installed"
STATUS_SKIPPED_DISABLED = "skipped_disabled"
STATUS_SKIPPED_NO_MECHANISM = "skipped_no_mechanism"

_TERMINAL_STATUSES = frozenset(
    {
        STATUS_UP_TO_DATE,
        STATUS_UPDATED,
        STATUS_FAILED,
        STATUS_SKIPPED_NOT_INSTALLED,
        STATUS_SKIPPED_DISABLED,
        STATUS_SKIPPED_NO_MECHANISM,
    }
)

# Providers with no machine-runnable update mechanism at all (mirrors
# pane_env.NO_AUTOUPDATE_KNOB_GAPS / config.PROVIDER_ISOLATION_GAPS — same
# "document the gap, never guess" policy, #103):
#   gemini/agy — GUI installer download only (`ProviderSpec.install_command`
#     is None), no package-manager command exists to re-run.
#   cursor     — official install is a remote curl/irm script; re-running an
#     arbitrary remote script unattended at every boot is out of proportion
#     (and unlike npm/uv it has no idempotent "ensure latest" semantics) —
#     never auto-executed here.
NO_UPDATE_MECHANISM_GAPS: dict[str, str] = {
    "gemini": "agy ships as a GUI installer download — no package-manager command to re-run",
    "cursor": ("official install is a remote curl/irm script — not auto-run unattended at boot"),
}


@dataclass(frozen=True)
class UpdateOutcome:
    provider: str
    status: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_UP_TO_DATE, STATUS_UPDATED, *_SKIPPED_STATUSES)


_SKIPPED_STATUSES = (
    STATUS_SKIPPED_NOT_INSTALLED,
    STATUS_SKIPPED_DISABLED,
    STATUS_SKIPPED_NO_MECHANISM,
)


def _discover(spec) -> str | None:
    """Locate *spec*'s binary — mirrors provider_install._discover, kept as
    a separate tiny copy so this module has zero import-time dependency on
    provider_install (which pulls in subprocess-heavy install flows)."""
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


def eligibility_gap(name: str) -> UpdateOutcome | None:
    """Terminal skip outcome for *name* if it's not eligible for an update
    run this boot, else None when it IS eligible (installed and enabled).

    Free of subprocess/network cost either way — `_discover` is a PATH probe
    only. Shared by `eligible_providers()` and the boot splash, which needs
    the skip REASON (not installed vs. disabled) to label each row.
    """
    from . import provider_state
    from .provider_spec import PROVIDER_REGISTRY

    spec = PROVIDER_REGISTRY.get(name)
    if spec is None:
        return UpdateOutcome(name, STATUS_FAILED, f"unknown provider: {name!r}")
    if provider_state.is_disabled(name):
        return UpdateOutcome(name, STATUS_SKIPPED_DISABLED, "disabled in Settings")
    if _discover(spec) is None:
        return UpdateOutcome(name, STATUS_SKIPPED_NOT_INSTALLED, "not installed")
    return None


def eligible_providers() -> list[str]:
    """Provider names that are both installed and enabled — the only ones
    boot-update ever probes or downloads for (user directive 2026-08-20)."""
    from .provider_spec import PROVIDER_REGISTRY

    return [name for name in PROVIDER_REGISTRY if eligibility_gap(name) is None]


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        encoding="utf-8",
        errors="replace",
        creationflags=SUBPROCESS_NO_WINDOW,
    )


def _generic_update_argv(spec) -> list[str] | None:
    """Derive an update command from `spec.install_command` — the SAME
    command already used to install this provider, re-run to fetch latest.

    Dispatches on the package manager (`install_command[0]`), not on the
    provider's name, so a future PROVIDER_REGISTRY entry gets update support
    for free as long as its `install_command` uses one of these two managers:

    - npm  — `npm install -g <pkg>` with no version pin already resolves to
      (and upgrades to) the latest published version on every run; reusing
      `install_command` verbatim IS the update command.
    - uv   — `uv tool install` does NOT re-check for a newer version once a
      tool is already installed; `uv tool upgrade <pkg>` is uv's own
      documented subcommand for that (confirmed via `uv tool upgrade --help`
      on this machine, 2026-08-20) — `install_command`'s package name
      (its last token) is reused as the upgrade target.

    Returns None when `install_command` is unset or uses an unrecognised
    program — never guesses an update flag for a manager this module hasn't
    verified.
    """
    cmd = spec.install_command
    if not cmd:
        return None
    program = cmd[0]
    if program == "npm":
        return list(cmd)
    if program == "uv":
        pkg = cmd[-1]
        return ["uv", "tool", "upgrade", pkg]
    return None


def _update_claude() -> UpdateOutcome:
    from ._pty_backend import _looks_like_valid_executable
    from .claude_update import apply_update, compare_versions, current_version, latest_version

    current = current_version()
    if current is None:
        return UpdateOutcome("claude", STATUS_SKIPPED_NOT_INSTALLED, "claude not on PATH")

    ok_latest, latest = latest_version()
    if not ok_latest:
        return UpdateOutcome("claude", STATUS_FAILED, f"version check failed: {latest}")

    if compare_versions(current, latest) >= 0:
        return UpdateOutcome("claude", STATUS_UP_TO_DATE, f"v{current}")

    ok, msg = apply_update()
    if not ok:
        return UpdateOutcome("claude", STATUS_FAILED, msg)

    # Real incident (2026-08-20): claude ships bin/claude.exe as a ~500B
    # placeholder; postinstall (install.cjs) copies the real binary from the
    # optional dependency @anthropic-ai/claude-code-win32-x64. If that
    # optional-dep fetch fails mid-update, npm still exits 0 and the
    # placeholder is left in place — every future spawn dies. Re-run the
    # SAME pre-flight header check spawn_pty already uses (#313) before
    # trusting the update as done.
    from .config import find_claude_executable

    try:
        resolved = find_claude_executable()
    except RuntimeError as e:
        return UpdateOutcome("claude", STATUS_FAILED, f"updated but not found after: {e}")
    if not _looks_like_valid_executable(resolved):
        return UpdateOutcome(
            "claude",
            STATUS_FAILED,
            f"updated to v{latest} but the binary failed the header check (placeholder left "
            "by a failed optional-dep fetch) — run `npm i -g "
            "@anthropic-ai/claude-code-win32-x64` then `node install.cjs`",
        )
    return UpdateOutcome("claude", STATUS_UPDATED, f"v{current} -> v{latest}")


def _update_generic(name: str) -> UpdateOutcome:
    from .provider_spec import PROVIDER_REGISTRY

    spec = PROVIDER_REGISTRY[name]
    gap = NO_UPDATE_MECHANISM_GAPS.get(name)
    if gap:
        return UpdateOutcome(name, STATUS_SKIPPED_NO_MECHANISM, gap)

    argv = _generic_update_argv(spec)
    if argv is None:
        return UpdateOutcome(name, STATUS_SKIPPED_NO_MECHANISM, "no npm/uv install_command on file")

    program = shutil.which(argv[0])
    if program is None:
        return UpdateOutcome(name, STATUS_FAILED, f"`{argv[0]}` not found on PATH")

    try:
        result = _run([program, *argv[1:]], timeout=_UPDATE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return UpdateOutcome(
            name, STATUS_FAILED, f"update timed out after {_UPDATE_TIMEOUT_S:.0f}s"
        )
    except OSError as e:
        return UpdateOutcome(name, STATUS_FAILED, f"failed to launch: {e}")

    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-2:]
        return UpdateOutcome(name, STATUS_FAILED, f"exit {result.returncode}: {' | '.join(tail)}")

    resolved = _discover(spec)
    if resolved is None:
        return UpdateOutcome(name, STATUS_FAILED, "updater exited 0 but binary no longer resolves")
    return UpdateOutcome(name, STATUS_UPDATED, resolved)


def update_provider(name: str) -> UpdateOutcome:
    """Run the update for *name* and return its terminal outcome.

    Callers MUST have already filtered through `eligible_providers()` for
    the disabled/not-installed skip to be free of subprocess cost — this
    function still defends both checks so it is safe to call directly (e.g.
    from a test or `takkub doctor`) without duplicating that filter.
    """
    gap = eligibility_gap(name)
    if gap is not None:
        return gap

    if name == "claude":
        return _update_claude()
    return _update_generic(name)
