"""takkub doctor — diagnose cockpit environment.

Pure-logic checks: no orchestrator TCP, no installs. Network is avoided except
by `check_version`, which does ONE best-effort `git fetch` (short timeout,
degrades to the last-known ref offline) so a CLI-only user learns they're behind
origin/main. Every subprocess call uses a short timeout + SUBPROCESS_NO_WINDOW
to prevent hangs.

The one other exception is `check_spawn_queue_live` (#141) — it interprets a
live spawn-arbiter status response, but never fetches it itself: doctor.py is
a `leaf-modules-pure` module (import-linter contract) and must not import
`cli`/`orchestrator`, so the TCP round-trip lives in `cli.cmd_doctor` and is
only made when the caller explicitly opts in via `takkub doctor --live`. It
is NOT part of `run_all_checks()`, so a plain `takkub doctor` is unaffected.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"
    INFO = "info"


@dataclass
class Finding:
    category: str
    name: str
    status: Status
    detail: str = ""
    fix_hint: str = ""
    auto_fix: Callable[[], tuple[bool, str]] | None = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run(argv: list[str]) -> tuple[int, str]:
    """Run *argv* with timeout=5. Returns (returncode, combined output).

    Moved to `agent_takkub.provider_probe.run_probe` (#309 Phase 4) so
    `core.versioning.detector` shares the exact same subprocess-probe
    behavior; kept as a thin re-export here so this module's ~40 existing
    call sites are untouched."""
    from .provider_probe import run_probe

    return run_probe(argv, timeout=5)


# ---------------------------------------------------------------------------
# [claude]
# ---------------------------------------------------------------------------


def check_claude() -> list[Finding]:
    findings: list[Finding] = []

    # binary
    try:
        from .config import find_claude_executable

        path = find_claude_executable()
        _, out = _run([path, "--version"])
        version = out.splitlines()[0] if out else "(unknown)"
        # Grade the CLI version against the shared baseline. The binary exists,
        # so an old-but-working CLI is only a WARN nudge (never FAIL) — matches
        # the non-breaking "recommended" policy for the core set.
        from . import system_baseline as _bl

        res = _bl.evaluate("claude", version)
        note = _bl.baseline_note(_bl.TOOL_BY_KEY["claude"])
        if res.level in (_bl.LEVEL_BELOW_MIN, _bl.LEVEL_RECOMMEND):
            findings.append(
                Finding(
                    "claude",
                    "binary",
                    Status.WARN,
                    f"{version}  {path}  (below recommended · {note})",
                    _bl.TOOL_BY_KEY["claude"].upgrade_hint,
                )
            )
        else:
            findings.append(Finding("claude", "binary", Status.OK, f"{version}  {path}  ({note})"))
    except Exception as e:
        findings.append(
            Finding(
                "claude", "binary", Status.FAIL, str(e), "install claude code from claude.ai/code"
            )
        )

    # authenticated
    # The real file is `.credentials.json` (leading dot) — Windows/Linux only.
    # macOS keeps the OAuth token in the login Keychain instead (see
    # limit_status._read_keychain_credentials), not a file at all.
    creds = Path.home() / ".claude" / ".credentials.json"
    if sys.platform == "darwin":
        from .limit_status import _read_keychain_credentials

        if _read_keychain_credentials():
            findings.append(
                Finding("claude", "authenticated", Status.OK, "found in macOS Keychain")
            )
        elif creds.is_file():
            try:
                json.loads(creds.read_text(encoding="utf-8"))
                findings.append(
                    Finding("claude", "authenticated", Status.OK, ".credentials.json present")
                )
            except Exception:
                findings.append(
                    Finding(
                        "claude",
                        "authenticated",
                        Status.WARN,
                        ".credentials.json present but unreadable",
                        "run 'claude login' from a terminal",
                    )
                )
        else:
            findings.append(
                Finding(
                    "claude",
                    "authenticated",
                    Status.WARN,
                    "not found in macOS Keychain or .credentials.json",
                    "run 'claude login' from a terminal",
                )
            )
    elif sys.platform == "win32":
        # credentials may also live in Windows Credential Manager — not directly checkable
        if creds.is_file():
            try:
                json.loads(creds.read_text(encoding="utf-8"))
                findings.append(
                    Finding("claude", "authenticated", Status.OK, ".credentials.json present")
                )
            except Exception:
                findings.append(
                    Finding(
                        "claude",
                        "authenticated",
                        Status.WARN,
                        ".credentials.json present but unreadable",
                        "run 'claude login' from a terminal",
                    )
                )
        else:
            findings.append(
                Finding(
                    "claude",
                    "authenticated",
                    Status.SKIP,
                    "auth state not directly checkable on Windows; try 'claude --print Hello' to verify",
                    "run 'claude login' from a terminal if needed",
                )
            )
    else:
        if creds.is_file():
            try:
                json.loads(creds.read_text(encoding="utf-8"))
                findings.append(
                    Finding("claude", "authenticated", Status.OK, ".credentials.json present")
                )
            except Exception:
                findings.append(
                    Finding(
                        "claude",
                        "authenticated",
                        Status.WARN,
                        ".credentials.json present but unreadable",
                        "run 'claude login' from a terminal",
                    )
                )
        else:
            findings.append(
                Finding(
                    "claude",
                    "authenticated",
                    Status.WARN,
                    ".credentials.json not found",
                    "run 'claude login' from a terminal",
                )
            )

    # Installed instances get an isolated default Claude profile
    # (DATA_HOME/claude-config) — separate from a dev checkout's ~/.claude on
    # the same machine. That profile is cloned from ~/.claude on first boot
    # (session history/plugins), but login is NOT — it needs its own
    # `claude login` under that CLAUDE_CONFIG_DIR. See
    # docs/audit/2026-07-05-isolation-plan-crosscheck-codex.md, finding C5.
    from .config import DATA_HOME, REPO_ROOT

    if DATA_HOME != REPO_ROOT:
        from .user_profile import _DEFAULT_CONFIG_DIR

        prod_creds = _DEFAULT_CONFIG_DIR / ".credentials.json"
        if prod_creds.is_file():
            findings.append(
                Finding(
                    "claude",
                    "prod_profile_authenticated",
                    Status.OK,
                    f"{_DEFAULT_CONFIG_DIR} has credentials",
                )
            )
        else:
            findings.append(
                Finding(
                    "claude",
                    "prod_profile_authenticated",
                    Status.WARN,
                    f"prod Claude profile not logged in yet ({_DEFAULT_CONFIG_DIR})",
                    f"run 'claude login' with CLAUDE_CONFIG_DIR={_DEFAULT_CONFIG_DIR}",
                )
            )

    return findings


# ---------------------------------------------------------------------------
# [runtime]
# ---------------------------------------------------------------------------


def _core_finding(category: str, key: str, version_text: str, path: str = "") -> Finding:
    """Grade an installed tool version against the central system-core baseline
    (:mod:`system_baseline`) and turn the result into a ``Finding``.

    Below ``minimum`` → FAIL (unsupported); above ``minimum`` but below
    ``recommended`` → WARN (fleet-parity nudge); at/above ``recommended`` → OK;
    unparseable version → INFO. The baseline note ("min X · rec Y") is appended
    so every machine reads the exact bar it's measured against — the whole point
    of the shared manifest.
    """
    from . import system_baseline as _bl

    tool = _bl.TOOL_BY_KEY[key]
    res = _bl.evaluate(key, version_text)
    note = _bl.baseline_note(tool)
    shown = version_text.strip() or path or "(unknown)"

    if res.level == _bl.LEVEL_BELOW_MIN:
        return Finding(category, key, Status.FAIL, f"{shown}  ({note})", tool.upgrade_hint)
    if res.level == _bl.LEVEL_RECOMMEND:
        return Finding(
            category,
            key,
            Status.WARN,
            f"{shown}  (below recommended · {note})",
            tool.upgrade_hint,
        )
    if res.level == _bl.LEVEL_UNKNOWN:
        # Version couldn't be parsed. If the binary is present anyway (e.g. npx
        # on Windows is a .CMD that CreateProcess can't run headless), that's a
        # benign "present but not probed" — report OK, not an alarming INFO. Only
        # a truly empty result (no path either) stays INFO.
        if path:
            return Finding(category, key, Status.OK, f"{path}  (present · not probed · {note})")
        return Finding(category, key, Status.INFO, f"{shown}  (version unreadable · {note})")
    return Finding(category, key, Status.OK, f"{shown}  ({note})")


def check_runtime() -> list[Finding]:
    """[runtime] — core interpreters/tooling graded against the shared baseline.

    node / npx / python versions are compared to :mod:`system_baseline` so a
    machine that has drifted below the fleet's minimum (FAIL) or recommended
    (WARN) shows up here instead of every check inventing its own threshold.
    """
    findings: list[Finding] = []

    # node
    node = shutil.which("node")
    if node:
        rc, ver = _run(["node", "--version"])
        findings.append(_core_finding("runtime", "node", ver if rc == 0 else "", node))
    else:
        findings.append(
            Finding(
                "runtime", "node", Status.FAIL, "not found", "install Node.js 20+ from nodejs.org"
            )
        )

    # npx
    npx = shutil.which("npx")
    if npx:
        rc, ver = _run(["npx", "--version"])
        findings.append(_core_finding("runtime", "npx", ver if rc == 0 else "", npx))
    else:
        findings.append(
            Finding(
                "runtime", "npx", Status.FAIL, "not found", "comes with Node.js — reinstall Node"
            )
        )

    # python (this interpreter — no subprocess needed)
    vi = sys.version_info
    findings.append(_core_finding("runtime", "python", f"{vi[0]}.{vi[1]}.{vi[2]}"))

    return findings


# ---------------------------------------------------------------------------
# [browser] — mini-browser CLI used by provider-neutral browser QA (#123)
# ---------------------------------------------------------------------------


def check_mini_browser() -> list[Finding]:
    """Check/install the provider-neutral ``mb`` CLI.

    Normal doctor runs remain read-only. ``takkub doctor --fix`` invokes the
    fixed-package auto-fix when mb is absent; unlike optional model providers,
    mb is shared browser-role infrastructure and does not require
    ``--install-providers``.
    """
    if sys.platform == "win32":
        from ._win_console import sanitize_win32_mb_shims

        sanitize_win32_mb_shims()

    mb = shutil.which("mb.cmd") or shutil.which("mb")
    if mb:
        return [Finding("browser", "mini-browser", Status.OK, mb)]

    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        return [
            Finding(
                "browser",
                "mini-browser",
                Status.WARN,
                "mb not found; npm is unavailable",
                "install Node.js, then run `npm install --global @runablehq/mini-browser`",
            )
        ]

    def _install() -> tuple[bool, str]:
        from ._win_console import SUBPROCESS_NO_WINDOW

        try:
            result = subprocess.run(
                [npm, "install", "--global", "@runablehq/mini-browser"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                creationflags=SUBPROCESS_NO_WINDOW,
                env={**os.environ, "npm_config_yes": "true"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        if result.returncode == 0:
            return True, output[-300:] or "mini-browser installed"
        return False, output[-300:] or f"npm exited {result.returncode}"

    return [
        Finding(
            "browser",
            "mini-browser",
            Status.WARN,
            "mb not found — browser roles cannot use provider-neutral CLI QA",
            "`takkub doctor --fix` or `npm install --global @runablehq/mini-browser`",
            auto_fix=_install,
        )
    ]


# ---------------------------------------------------------------------------
# [graft] — NanoNets graft CLI (code-intelligence: symbol search / call-graph)
# ---------------------------------------------------------------------------


def _graft_store_size_finding() -> Finding:
    """[graft/store-size] — surface `graft-graphs/` live-store size without
    making the user run `takkub disk` separately. Read-only: reuses
    `disk_usage.scan_graft_graphs`, the same aggregation `takkub disk` and
    `takkub prune` already rely on, so the oversized-store threshold (H1,
    2026-08-05 cross-OS audit) stays defined in exactly one place.
    """
    from . import disk_usage
    from .config import DATA_HOME

    gg = disk_usage.scan_graft_graphs(DATA_HOME)
    live_mb = gg["live_bytes"] / (1024 * 1024)
    if gg["live_count"] == 0:
        return Finding("graft", "store-size", Status.INFO, "no graph built yet for any project")
    if gg["oversized_live"]:
        names = ", ".join(Path(e["path"]).name[:12] for e in gg["oversized_live"][:3])
        return Finding(
            "graft",
            "store-size",
            Status.WARN,
            f"{gg['live_count']} live store(s), {live_mb:.0f} MB total — "
            f"{len(gg['oversized_live'])} over the size-warning threshold ({names}...)",
            "takkub disk  for detail, then takkub prune --include-live to reclaim",
        )
    return Finding(
        "graft",
        "store-size",
        Status.OK,
        f"{gg['live_count']} live store(s), {live_mb:.0f} MB total",
    )


def check_graft() -> list[Finding]:
    """Check/install the global ``graft`` CLI (NanoNets code-intelligence tool).

    Separate from the graft MCP server (``shared_dev_tools.GRAFT_MCP``,
    launched ephemerally per-pane via ``npx -y @nanonets/graft@<pin> mcp`` —
    no install needed there). This checks the CLI binary a human/agent runs
    directly (``graft build``, ``graft ask``, ``graft skeleton``, ...).
    Pilot findings: ``docs/audit/2026-08-05-graft-pilot.md``.

    Like ``check_mini_browser``, normal doctor runs stay read-only;
    ``takkub doctor --fix`` installs it when missing. graft is shared
    dev-tool infra, not a role/pane provider, so it does NOT require
    ``--install-providers``. Never runs ``graft init`` — that would
    overwrite the cockpit's own ``.claude/settings.json`` + statusline.

    Node >= 20 is graft's real ``engines.node`` floor — stricter than the
    cockpit's own Node baseline (WARN-only below 20, FAIL only below 18 —
    see system_baseline.py), so a machine that clears ``check_runtime``
    with just a WARN can still be too old for graft. Flagged here
    explicitly instead of failing silently at install/build time.

    Never blocks on a missing graph: a project that hasn't run
    ``graft build`` yet gets graceful empty answers (confirmed in the
    pilot — no crash/hang), so this only nudges toward running it.
    """
    from .shared_dev_tools import _GRAFT_MCP_VERSION

    findings: list[Finding] = []

    node = shutil.which("node")
    if node:
        rc, ver = _run(["node", "--version"])
        if rc == 0 and ver:
            try:
                major = int(ver.lstrip("v").split(".")[0])
            except ValueError:
                major = 0
            if 0 < major < 20:
                findings.append(
                    Finding(
                        "graft",
                        "node-version",
                        Status.WARN,
                        f"node {ver} — graft requires Node >= 20 (cockpit min is 18)",
                        "upgrade Node.js to 20+ from nodejs.org",
                    )
                )

    graft = shutil.which("graft.cmd") or shutil.which("graft")
    if graft:
        rc, out = _run([graft, "--version"])
        version = out.strip() if rc == 0 else ""
        if version and version != _GRAFT_MCP_VERSION:
            findings.append(
                Finding(
                    "graft",
                    "cli",
                    Status.WARN,
                    f"{version}  {graft}  (pinned: {_GRAFT_MCP_VERSION})",
                    f"npm install -g @nanonets/graft@{_GRAFT_MCP_VERSION}  — match the pinned version",
                )
            )
        else:
            findings.append(
                Finding("graft", "cli", Status.OK, f"{version or '(unknown)'}  {graft}")
            )
        findings.append(
            Finding(
                "graft",
                "graph",
                Status.INFO,
                "a project that hasn't run `graft build` yet returns empty-but-valid "
                "answers (not a crash) — run `graft build` in that project's root first",
            )
        )
        findings.append(_graft_store_size_finding())
        return findings

    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        findings.append(
            Finding(
                "graft",
                "cli",
                Status.SKIP,
                "graft not found; npm is unavailable",
                f"install Node.js, then run `npm install -g @nanonets/graft@{_GRAFT_MCP_VERSION}`",
            )
        )
        return findings

    def _install() -> tuple[bool, str]:
        from ._win_console import SUBPROCESS_NO_WINDOW

        try:
            result = subprocess.run(
                [npm, "install", "-g", f"@nanonets/graft@{_GRAFT_MCP_VERSION}"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                creationflags=SUBPROCESS_NO_WINDOW,
                env={**os.environ, "npm_config_yes": "true"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        if result.returncode == 0:
            return True, output[-300:] or "graft installed"
        return False, output[-300:] or f"npm exited {result.returncode}"

    findings.append(
        Finding(
            "graft",
            "cli",
            Status.WARN,
            "graft CLI not found — code-intelligence checks unavailable",
            f"`takkub doctor --fix` or `npm install -g @nanonets/graft@{_GRAFT_MCP_VERSION}`",
            auto_fix=_install,
        )
    )
    return findings


# ---------------------------------------------------------------------------
# [arch] — Apple Silicon Rosetta / native-arm64 shell hygiene (macOS only)
# ---------------------------------------------------------------------------

# The exact block appended to ~/.zshrc by --fix. Guarded three ways so it is a
# no-op on Intel Macs (hw.optional.arm64 != 1) and non-macOS shells (OSTYPE), and
# never loops (after `exec`, arch == arm64 → condition false). Kept in sync with
# the `arch/zshrc-guard` check below via the marker line.
_ARM64_GUARD_MARKER = "# takkub: force native arm64 shell on Apple Silicon"
_ARM64_GUARD_BLOCK = f"""
{_ARM64_GUARD_MARKER}
# Safe on Intel Macs (skipped: hw.optional.arm64 != 1) and non-macOS (skipped: OSTYPE).
# exec replaces the shell once, so no loop (after exec, arch == arm64 → guard false).
if [[ "$OSTYPE" == darwin* ]] \\
  && [[ "$(sysctl -n hw.optional.arm64 2>/dev/null)" == "1" ]] \\
  && [[ "$(arch)" == "i386" ]]; then
  exec arch -arm64 zsh
fi
"""


def _rosetta_installed() -> bool:
    """True if Rosetta 2 is present. Checks the runtime paths Apple installs; the
    directory form is what a fresh `softwareupdate --install-rosetta` drops."""
    return (
        Path("/Library/Apple/usr/libexec/oah/libRosettaRuntime").exists()
        or Path("/Library/Apple/usr/share/rosetta").exists()
    )


def _zshrc_has_guard() -> bool:
    zshrc = Path.home() / ".zshrc"
    try:
        return _ARM64_GUARD_MARKER in zshrc.read_text(encoding="utf-8")
    except OSError:
        return False


def check_arch() -> list[Finding]:
    """[arch] — keep Apple Silicon Macs off the Rosetta trap.

    On an Apple Silicon Mac a terminal with "Open using Rosetta" ticked runs the
    shell (and everything it spawns) as x86_64. Universal python then builds
    x86_64 wheels into venvs — which import-crash the moment that venv/code is
    copied to a native-arm64 Mac. That's the "works here, breaks on the other
    mac" report. This surfaces it and, with ``--fix``, installs Rosetta (for the
    unavoidable Intel-only apps) AND drops a guarded ``exec arch -arm64`` into
    ``~/.zshrc`` so every new shell lands native.

    macOS-only: returns ``[]`` on Windows/Linux, and a single benign OK on a
    genuine Intel Mac where the whole topic is moot.
    """
    if sys.platform != "darwin":
        return []

    _, arm64_opt = _run(["sysctl", "-n", "hw.optional.arm64"])
    if arm64_opt.strip() != "1":
        # Real Intel Mac — no arm64 slice exists, Rosetta/arm64 hygiene is N/A.
        return [Finding("arch", "cpu", Status.OK, "Intel Mac — Rosetta/arm64 checks N/A")]

    findings: list[Finding] = []

    # 1. Is THIS shell (doctor's parent) running translated under Rosetta? This is
    #    the process arch venvs/pip inherit, so it's the one that actually bites.
    _, translated = _run(["sysctl", "-n", "sysctl.proc_translated"])
    if translated.strip() == "1":
        findings.append(
            Finding(
                "arch",
                "shell",
                Status.WARN,
                "running under Rosetta (x86_64) — pip/venv build Intel wheels that "
                "import-crash when moved to a native-arm64 Mac",
                "takkub doctor --fix → adds the ~/.zshrc arm64 guard, then REOPEN the "
                "terminal (and rebuild any .venv created while translated)",
            )
        )
    else:
        findings.append(Finding("arch", "shell", Status.OK, "native arm64"))

    # 2. Rosetta present? Needed by the unavoidable Intel-only apps (games, some
    #    dev tools). --fix installs it (long-running, so its own timeout).
    if _rosetta_installed():
        findings.append(Finding("arch", "rosetta", Status.OK, "installed"))
    else:

        def _install_rosetta() -> tuple[bool, str]:
            from ._win_console import SUBPROCESS_NO_WINDOW

            try:
                r = subprocess.run(
                    ["softwareupdate", "--install-rosetta", "--agree-to-license"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=600,
                    creationflags=SUBPROCESS_NO_WINDOW,
                )
            except Exception as e:
                return False, str(e)
            if r.returncode == 0:
                return True, "Rosetta installed"
            return False, ((r.stderr or r.stdout or "").strip()[-200:] or "install failed")

        findings.append(
            Finding(
                "arch",
                "rosetta",
                Status.WARN,
                "not installed — Intel-only apps (games, some dev tools) will fail to launch",
                "takkub doctor --fix → installs Rosetta (needs network)",
                auto_fix=_install_rosetta,
            )
        )

    # 3. Does ~/.zshrc pin new shells to native arm64? This is the durable fix that
    #    travels with the dotfile to every machine — the whole "all machines" ask.
    if _zshrc_has_guard():
        findings.append(Finding("arch", "zshrc-guard", Status.OK, "native-arm64 guard present"))
    else:

        def _add_zshrc_guard() -> tuple[bool, str]:
            zshrc = Path.home() / ".zshrc"
            try:
                existing = zshrc.read_text(encoding="utf-8") if zshrc.exists() else ""
                if _ARM64_GUARD_MARKER in existing:
                    return True, "guard already present"
                sep = "" if existing.endswith("\n") or not existing else "\n"
                with zshrc.open("a", encoding="utf-8") as fh:
                    fh.write(sep + _ARM64_GUARD_BLOCK)
            except OSError as e:
                return False, str(e)
            return True, "added arm64 guard to ~/.zshrc — reopen the terminal to take effect"

        findings.append(
            Finding(
                "arch",
                "zshrc-guard",
                Status.WARN,
                "~/.zshrc has no native-arm64 guard — a Rosetta-ticked terminal stays x86_64",
                "takkub doctor --fix → appends a Rosetta-safe `exec arch -arm64` guard",
                auto_fix=_add_zshrc_guard,
            )
        )

    return findings


# ---------------------------------------------------------------------------
# [qt] — Qt version pin + crash guard (cross-platform stability gate)
# ---------------------------------------------------------------------------


def check_qt() -> list[Finding]:
    """[qt] — enforce the pinned Qt 6.8 LTS series + the runtime crash guard.

    Qt 6.11.0 shipped a Qt6Core regression that hard-crashes the cockpit on pane
    teardown (``0xc0000409`` __fastfail on Windows, abort on macOS). pyproject
    pins the 6.8 LTS series, but a machine that ran a bare ``pip install PyQt6``
    silently pulls the latest (6.11) and crashes — the exact "works on my box,
    crashes on the other mac" trap. This surfaces the mismatch and, with
    ``--fix``, reinstalls the pinned range.

    The runtime slot-exception guard (``app._install_exception_guard``) is
    checked *statically from source* so the CLI process never imports the GUI
    stack (import-linter cli↔GUI boundary).
    """
    findings: list[Finding] = []

    # 1. Qt runtime version vs the pinned 6.8 LTS series.
    try:
        from PyQt6.QtCore import QT_VERSION_STR
    except Exception as e:
        return [
            Finding(
                "qt",
                "runtime",
                Status.FAIL,
                f"PyQt6 not importable: {e}",
                "pip install -e .  (from the repo root)",
            )
        ]

    ver = QT_VERSION_STR
    try:
        major, minor = (int(x) for x in ver.split(".")[:2])
    except ValueError:
        major, minor = 0, 0

    if (major, minor) == (6, 8):
        findings.append(Finding("qt", "version", Status.OK, f"Qt {ver} (pinned 6.8 LTS)"))
    else:

        def _reinstall_qt() -> tuple[bool, str]:
            """--fix: force the 6.8 LTS pins back over whatever bare install pulled."""
            from ._win_console import SUBPROCESS_NO_WINDOW

            try:
                r = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        "--upgrade",
                        "PyQt6>=6.8,<6.9",
                        "PyQt6-Qt6>=6.8,<6.9",
                        "PyQt6-WebEngine>=6.8,<6.9",
                        "PyQt6-WebEngine-Qt6>=6.8,<6.9",
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=600,
                    creationflags=SUBPROCESS_NO_WINDOW,
                )
            except Exception as e:
                return False, str(e)
            if r.returncode == 0:
                return True, "reinstalled PyQt6 6.8 LTS — restart the cockpit to load it"
            return False, ((r.stderr or r.stdout or "").strip()[-200:] or "pip install failed")

        known_bad = (major, minor) >= (6, 11)
        why = (
            "6.11+ has a pane-teardown crash regression"
            if known_bad
            else "untested outside the pinned 6.8 LTS"
        )
        findings.append(
            Finding(
                "qt",
                "version",
                Status.FAIL,
                f"Qt {ver} — not the pinned 6.8 LTS ({why})",
                "takkub doctor --fix  → reinstalls 6.8 LTS, then restart the cockpit",
                auto_fix=_reinstall_qt,
            )
        )

    # 2. Runtime crash guard — checked from source (importing app would pull the
    #    GUI stack into the CLI process, crossing the import-linter boundary).
    app_src = Path(__file__).with_name("app.py")
    try:
        has_guard = "_install_exception_guard" in app_src.read_text(encoding="utf-8")
    except OSError:
        has_guard = False
    if has_guard:
        findings.append(
            Finding("qt", "crash-guard", Status.OK, "slot-exception guard present in app.py")
        )
    else:
        findings.append(
            Finding(
                "qt",
                "crash-guard",
                Status.WARN,
                "exception guard missing — pane teardown may hard-crash the process",
                "git pull --ff-only origin main  (update to latest)",
            )
        )
    return findings


# ---------------------------------------------------------------------------
# [plugins]
# ---------------------------------------------------------------------------


def _plugin_cache_root() -> Path:
    return Path.home() / ".claude" / "plugins" / "cache"


def check_plugins(cache_root: Path | None = None) -> list[Finding]:
    from .config import _SAFE_PLUGINS

    root = cache_root if cache_root is not None else _plugin_cache_root()
    findings: list[Finding] = []

    for marketplace in _SAFE_PLUGINS:
        mp_dir = root / marketplace
        if not mp_dir.is_dir():
            findings.append(
                Finding(
                    "plugins",
                    marketplace,
                    Status.WARN,
                    "not installed",
                    "install via /plugin in a Claude Code session",
                )
            )
            continue

        # 3-level walk: marketplace / plugin / version / .claude-plugin / plugin.json
        found = False
        try:
            for plugin_dir in sorted(mp_dir.iterdir()):
                if not plugin_dir.is_dir():
                    continue
                versions = sorted((v for v in plugin_dir.iterdir() if v.is_dir()), reverse=True)
                for v in versions:
                    plugin_json = v / ".claude-plugin" / "plugin.json"
                    if not plugin_json.is_file():
                        continue
                    try:
                        json.loads(plugin_json.read_text(encoding="utf-8"))
                    except Exception as e:
                        findings.append(
                            Finding(
                                "plugins",
                                marketplace,
                                Status.FAIL,
                                f"plugin.json broken: {e}",
                                "re-install via /plugin",
                            )
                        )
                        found = True
                        break
                    label = f"{marketplace}/{plugin_dir.name}@{v.name}"
                    findings.append(Finding("plugins", marketplace, Status.OK, label))
                    found = True
                    break
                if found:
                    break
        except OSError as e:
            findings.append(
                Finding(
                    "plugins",
                    marketplace,
                    Status.WARN,
                    f"plugin cache unreadable: {e}",
                )
            )
            continue

        if not found:
            findings.append(
                Finding(
                    "plugins",
                    marketplace,
                    Status.FAIL,
                    f"no plugin.json found under {marketplace}",
                    "re-install via /plugin",
                )
            )

    return findings


# ---------------------------------------------------------------------------
# [mcps]
# ---------------------------------------------------------------------------


def check_mcps(shared_mcp_file: Path | None = None) -> list[Finding]:
    from .shared_dev_tools import SHARED_MCP_FILE as _DEFAULT_SHARED_MCP

    mcp_path = shared_mcp_file if shared_mcp_file is not None else _DEFAULT_SHARED_MCP

    findings: list[Finding] = []

    if not mcp_path.is_file():

        def _auto_fix_mcp() -> tuple[bool, str]:
            from .shared_dev_tools import ensure_browser_mcps, ensure_user_mcps

            ok1, msg1 = ensure_browser_mcps()
            ok2, msg2 = ensure_user_mcps()
            return (ok1 and ok2), f"{msg1}; {msg2}"

        findings.append(
            Finding(
                "mcps",
                "shared-mcp.json",
                Status.WARN,
                "file missing",
                "run 'takkub doctor --fix' to regenerate",
                auto_fix=_auto_fix_mcp,
            )
        )
        return findings

    try:
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        findings.append(
            Finding(
                "mcps",
                "shared-mcp.json",
                Status.FAIL,
                f"JSON broken: {e}",
                "delete and re-run cockpit",
            )
        )
        return findings

    servers: dict = data.get("mcpServers") or {}
    findings.append(Finding("mcps", "shared-mcp.json", Status.OK, f"{len(servers)} server(s)"))

    for srv_name, cfg in servers.items():
        if not isinstance(cfg, dict):
            findings.append(Finding("mcps", srv_name, Status.WARN, "entry is not a dict"))
            continue

        srv_type = cfg.get("type", "")
        if srv_type == "stdio":
            cmd = cfg.get("command", "")

            # obsidian-vault: check vault path instead of generic npx check
            if srv_name == "obsidian-vault":
                args = cfg.get("args") or []
                if args:
                    vault_path = Path(args[-1])
                    if vault_path.is_dir():
                        findings.append(Finding("mcps", srv_name, Status.OK, "vault path ok"))
                    else:
                        findings.append(
                            Finding(
                                "mcps",
                                srv_name,
                                Status.WARN,
                                f"vault path not found: {args[-1]}",
                                "update the vault path in ~/.claude.json",
                            )
                        )
                else:
                    findings.append(Finding("mcps", srv_name, Status.WARN, "no vault path arg"))
            elif cmd == "npx":
                findings.append(Finding("mcps", srv_name, Status.OK, "npx ok (connection skipped)"))
            elif cmd and shutil.which(cmd):
                findings.append(Finding("mcps", srv_name, Status.OK, f"{cmd} found"))
            elif cmd:
                findings.append(
                    Finding(
                        "mcps",
                        srv_name,
                        Status.WARN,
                        f"command '{cmd}' not found in PATH",
                        f"install {cmd} or remove this MCP entry",
                    )
                )
            else:
                findings.append(Finding("mcps", srv_name, Status.WARN, "no command specified"))
        else:
            # non-stdio: skip network probe
            findings.append(Finding("mcps", srv_name, Status.INFO, f"type={srv_type!r} (skipped)"))

    return findings


# ---------------------------------------------------------------------------
# [projects]
# ---------------------------------------------------------------------------


def check_projects() -> list[Finding]:
    from .config import load_projects

    findings: list[Finding] = []

    try:
        data = load_projects()
    except Exception as e:
        findings.append(Finding("projects", "projects.json", Status.FAIL, str(e)))
        return findings

    projects: dict = data.get("projects") or {}
    active: str | None = data.get("active")
    open_tabs: list = data.get("open_tabs") or []

    n = len(projects)
    active_label = f"active={active}" if active else "no active"
    findings.append(
        Finding("projects", "projects.json", Status.OK, f"{n} project(s), {active_label}")
    )

    if active and active not in projects:
        findings.append(
            Finding(
                "projects",
                "active",
                Status.WARN,
                f"active project '{active}' not in projects map",
                "edit projects.json or run 'takkub project set <name>'",
            )
        )

    for proj_name, proj_data in projects.items():
        paths: dict = proj_data.get("paths") or {}
        for path_key, path_val in paths.items():
            if not isinstance(path_val, str) or not path_val:
                findings.append(
                    Finding(
                        "projects",
                        proj_name,
                        Status.WARN,
                        f"path '{path_key}' is invalid: {path_val!r}",
                        "edit projects.json or run 'takkub project rm " + proj_name + "'",
                    )
                )
                continue
            if not Path(path_val).exists():
                findings.append(
                    Finding(
                        "projects",
                        proj_name,
                        Status.FAIL,
                        f"path '{path_key}' not found: {path_val}",
                        "edit projects.json or run 'takkub project rm " + proj_name + "'",
                    )
                )

    for tab in open_tabs:
        if tab not in projects:
            findings.append(
                Finding(
                    "projects",
                    f"tab:{tab}",
                    Status.WARN,
                    f"orphaned tab '{tab}' not in projects map",
                    "edit open_tabs in projects.json",
                )
            )

    return findings


# ---------------------------------------------------------------------------
# [installed] — integrity checks for a pip/npm-installed build (skipped for
# dev checkouts, which read these paths straight from the repo already).
# ---------------------------------------------------------------------------


def check_installed_integrity() -> list[Finding]:
    """[installed] — installed-build-only sanity checks (Phase D gate).

    A dev checkout's ASSETS_ROOT/CLI_BIN_DIR are just REPO_ROOT/bin — always
    present, so this whole category is a no-op there (``DATA_HOME ==
    REPO_ROOT``). Catches the "prod cockpit boots but can't spawn teammates"
    bug class: a packaging regression that ships a wheel missing CLAUDE.md,
    the role files, or the console script, or a DATA_HOME that turned out
    not to be writable after all.
    """
    from .config import (
        AGENTS_DIR,
        ASSETS_ROOT,
        CLI_BIN_DIR,
        DATA_HOME,
        REPO_ROOT,
        RUNTIME_DIR,
        SKILLS_DIR,
    )

    if DATA_HOME == REPO_ROOT:
        return []

    findings: list[Finding] = []

    claude_md = ASSETS_ROOT / "CLAUDE.md"
    if claude_md.is_file():
        findings.append(Finding("installed", "assets-claude-md", Status.OK, str(claude_md)))
    else:
        findings.append(
            Finding(
                "installed",
                "assets-claude-md",
                Status.FAIL,
                f"missing: {claude_md}",
                "reinstall — the wheel shipped without its Lead playbook",
            )
        )

    agent_files = sorted(AGENTS_DIR.glob("*.md")) if AGENTS_DIR.is_dir() else []
    if agent_files:
        findings.append(
            Finding("installed", "assets-role-files", Status.OK, f"{len(agent_files)} role file(s)")
        )
    else:
        findings.append(
            Finding(
                "installed",
                "assets-role-files",
                Status.FAIL,
                f"no *.md role files under {AGENTS_DIR}",
                "reinstall — the wheel shipped with no .claude/agents",
            )
        )

    # Default skill bundle — supplementary reference material for the New
    # Role / Skill Catalog pickers (skill_scan.scan_skills), not required for
    # a pane to spawn (unlike CLAUDE.md/role files above), so a missing
    # bundle is a WARN nudge rather than a FAIL.
    skill_files = sorted(SKILLS_DIR.glob("*/SKILL.md")) if SKILLS_DIR.is_dir() else []
    if skill_files:
        findings.append(
            Finding("installed", "assets-skill-files", Status.OK, f"{len(skill_files)} skill(s)")
        )
    else:
        findings.append(
            Finding(
                "installed",
                "assets-skill-files",
                Status.WARN,
                f"no SKILL.md files under {SKILLS_DIR}",
                "reinstall — the wheel shipped with no default skill bundle "
                "(New Role / Skill Catalog pickers will show no built-in skills)",
            )
        )

    script_name = "takkub.exe" if sys.platform == "win32" else "takkub"
    script_path = CLI_BIN_DIR / script_name
    if script_path.exists():
        findings.append(Finding("installed", "cli-bin", Status.OK, str(script_path)))
    else:
        findings.append(
            Finding(
                "installed",
                "cli-bin",
                Status.FAIL,
                f"missing: {script_path}",
                "reinstall — pip did not place a takkub console script next to python",
            )
        )

    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        probe = RUNTIME_DIR / ".doctor-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        findings.append(Finding("installed", "runtime-writable", Status.OK, str(RUNTIME_DIR)))
    except OSError as e:
        findings.append(
            Finding(
                "installed",
                "runtime-writable",
                Status.FAIL,
                f"{RUNTIME_DIR} not writable: {e}",
                "check permissions on the DATA_HOME directory",
            )
        )

    return findings


# ---------------------------------------------------------------------------
# [capability-hub] — Phase 5a skill store migration (epic #309)
# ---------------------------------------------------------------------------


def check_capability_skill_store() -> list[Finding]:
    """[capability-hub] — Phase 5a: the shipped skill store's new location
    (`capabilities/skills/`) plus the `.claude/skills` discovery surface
    every reader (claude's Skill tool, `skill_scan.scan_skills`) still
    depends on. Unlike `check_installed_integrity`, this runs on BOTH dev
    checkouts and installed builds — the surface-link mechanism applies to
    both (`config.ASSETS_ROOT == REPO_ROOT` for a dev checkout doesn't
    change that `.claude/skills/<name>` must still resolve)."""
    from .config import SKILLS_DIR
    from .core.capabilities.skill_store import ensure_shipped_skill_surface, shipped_skills_root

    findings: list[Finding] = []
    real_root = shipped_skills_root()
    if real_root.is_dir():
        findings.append(
            Finding("capability-hub", "skill-store-location", Status.OK, str(real_root))
        )
        errors = ensure_shipped_skill_surface()
        if errors:
            findings.append(
                Finding(
                    "capability-hub",
                    "skill-store-surface",
                    Status.WARN,
                    f"{len(errors)} skill link issue(s): {errors[0]}",
                    "check filesystem permissions for the .claude/skills surface dir",
                )
            )
        else:
            findings.append(
                Finding(
                    "capability-hub",
                    "skill-store-surface",
                    Status.OK,
                    f".claude/skills surface linked to {real_root}",
                )
            )
    else:
        findings.append(
            Finding(
                "capability-hub",
                "skill-store-location",
                Status.INFO,
                f"legacy layout — no {real_root}, reading {SKILLS_DIR} directly",
            )
        )
    return findings


# ---------------------------------------------------------------------------
# [venv] — dev-checkout shared editable install (#202)
# ---------------------------------------------------------------------------


def _dev_site_packages(repo_root: Path) -> Path | None:
    """Locate the dev checkout's ``.venv`` site-packages dir, or ``None`` when
    there is no ``.venv`` (e.g. CI running from a system interpreter).
    Cross-platform: Windows lays site-packages at ``.venv/Lib/site-packages``;
    POSIX at ``.venv/lib/python<major.minor>/site-packages``."""
    venv = repo_root / ".venv"
    win_site = venv / "Lib" / "site-packages"
    if win_site.is_dir():
        return win_site
    posix_matches = sorted(venv.glob("lib/python*/site-packages"))
    return posix_matches[0] if posix_matches else None


def check_editable_install() -> list[Finding]:
    """[venv] — dev-checkout-only: catch a shared-venv editable install
    (``__editable__.agent_takkub-*.pth``) left pointing at a path that no
    longer exists, or into a ``worktrees/`` checkout instead of the repo
    itself (#202).

    Root incident (2026-08-14): a `backend` pane ran `pip install -e .` from
    inside its own ``--isolation worktree`` checkout, rewriting the pointer
    every OTHER pane's Python/``takkub`` shares. Once the Lead removed that
    worktree after merging, the pointer went stale and the whole cockpit's
    ``.venv`` broke (``ModuleNotFoundError``). An installed build has its own
    non-editable install and no shared dev venv to police, so this is a no-op
    there — same guard `check_installed_integrity` uses.
    """
    from .config import DATA_HOME, REPO_ROOT

    if DATA_HOME != REPO_ROOT:
        return []

    site_packages = _dev_site_packages(REPO_ROOT)
    if site_packages is None:
        return []  # no .venv here — nothing to police

    pth_files = sorted(site_packages.glob("__editable__.agent_takkub-*.pth"))
    if not pth_files:
        return [
            Finding(
                "venv",
                "editable-install",
                Status.WARN,
                f"ไม่พบ __editable__.agent_takkub-*.pth ใน {site_packages}",
                "รัน `pip install -e . --no-deps` จาก repo root",
            )
        ]

    def _reinstall() -> tuple[bool, str]:
        from ._win_console import SUBPROCESS_NO_WINDOW

        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-e", ".", "--no-deps"],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                creationflags=SUBPROCESS_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        if result.returncode == 0:
            return True, output[-300:] or "reinstalled"
        return False, output[-300:] or f"pip exited {result.returncode}"

    findings: list[Finding] = []
    expected = (REPO_ROOT / "src").resolve()
    for pth in pth_files:
        try:
            lines = pth.read_text(encoding="utf-8").splitlines()
            target_raw = next((ln.strip() for ln in lines if ln.strip()), "")
        except OSError as exc:
            findings.append(
                Finding("venv", "editable-install", Status.WARN, f"{pth.name} อ่านไม่ได้: {exc}")
            )
            continue
        if not target_raw:
            findings.append(Finding("venv", "editable-install", Status.WARN, f"{pth.name} ว่างเปล่า"))
            continue
        target = Path(target_raw)
        if not target.exists():
            findings.append(
                Finding(
                    "venv",
                    "editable-install",
                    Status.FAIL,
                    f"{pth.name} ชี้ path ที่ไม่มีอยู่จริง: {target}",
                    "รัน `pip install -e . --no-deps` จาก repo root เพื่อซ่อม",
                    auto_fix=_reinstall,
                )
            )
        elif "worktrees" in target.parts:
            findings.append(
                Finding(
                    "venv",
                    "editable-install",
                    Status.FAIL,
                    f"{pth.name} ชี้เข้า worktree ({target}) แทน repo root — venv ทั้งเครื่องจะพัง "
                    "ทันทีที่ worktree นี้ถูกลบ (#202)",
                    "รัน `pip install -e . --no-deps` จาก repo root เพื่อซ่อม",
                    auto_fix=_reinstall,
                )
            )
        elif target.resolve() != expected:
            findings.append(
                Finding(
                    "venv",
                    "editable-install",
                    Status.WARN,
                    f"{pth.name} ชี้ {target} ไม่ตรงกับ repo root ที่คาดไว้ ({expected})",
                    "ถ้าไม่ตั้งใจ รัน `pip install -e . --no-deps` จาก repo root",
                    auto_fix=_reinstall,
                )
            )
        else:
            findings.append(Finding("venv", "editable-install", Status.OK, str(target)))
    return findings


# ---------------------------------------------------------------------------
# [providers]
# ---------------------------------------------------------------------------


def _resolve_provider_bin(spec) -> str | None:
    """Resolve `spec`'s binary — shared by check_providers()
    (version/install-state) and check_provider_auth() (#248/#247 round 2) so
    both agree on whether a provider is installed at all.

    Moved to `agent_takkub.provider_probe.resolve_provider_bin` (#309 Phase
    4) so `core.versioning.detector` uses the identical resolution logic
    instead of a second, independently-drifting copy; kept as a thin
    re-export here for the existing call sites."""
    from .provider_probe import resolve_provider_bin

    return resolve_provider_bin(spec)


def check_providers() -> list[Finding]:
    findings: list[Finding] = []

    # One row per registered non-claude provider (#103 Phase 1 — registry-
    # driven, so a new PROVIDER_REGISTRY entry shows up here automatically).
    # Use the resolved absolute path in `_run` so `--version` succeeds even
    # when the binary is off-PATH.
    from .provider_spec import PROVIDER_REGISTRY

    for provider, spec in PROVIDER_REGISTRY.items():
        if provider == "claude":
            continue  # claude has its own dedicated checks elsewhere
        path = _resolve_provider_bin(spec)
        if path:
            rc, ver = _run([path, "--version"])
            version = (ver.splitlines()[0] if ver else path) if rc == 0 else path
            findings.append(Finding("providers", provider, Status.INFO, version))
        elif spec.install_command:
            # Machine-installable (npm) → offer it as a --fix auto-fix. Bind
            # the provider name via default arg so the loop variable doesn't
            # leak the last iteration into every closure.
            def _install(provider=provider) -> tuple[bool, str]:
                from .provider_install import install_provider

                return install_provider(provider)

            findings.append(
                Finding(
                    "providers",
                    provider,
                    Status.SKIP,
                    "not installed (optional)",
                    (
                        f"install this provider: `takkub provider install {provider}`; "
                        "or install all missing providers: "
                        "`takkub doctor --fix --install-providers`"
                    ),
                    auto_fix=_install,
                )
            )
        else:
            findings.append(
                Finding(
                    "providers",
                    provider,
                    Status.SKIP,
                    "not installed (optional)",
                    spec.install_instructions
                    or f"install the {provider} CLI to use the '{provider}' teammate role",
                )
            )

    # disabled-providers.json
    from .config import SETTINGS_HOME

    dp_file = SETTINGS_HOME / "disabled-providers.json"
    if dp_file.is_file():
        try:
            json.loads(dp_file.read_text(encoding="utf-8"))
            findings.append(
                Finding("providers", "disabled-providers.json", Status.OK, "valid JSON")
            )
        except Exception as e:
            findings.append(
                Finding(
                    "providers",
                    "disabled-providers.json",
                    Status.WARN,
                    f"JSON broken: {e}",
                    f"fix or delete {dp_file}",
                )
            )

    return findings


def _codex_auth_finding() -> Finding:
    """[providers/codex-auth] — presence-only check against
    ``$CODEX_HOME/auth.json`` (``~/.codex/auth.json`` when CODEX_HOME is
    unset), the same CODEX_HOME resolver ``codex_helper.py`` already uses
    for its sessions dir. This is a well-known convention of the
    ``@openai/codex`` CLI, not something empirically confirmed against a
    real machine in this repo — presence proves *a* login happened at some
    point, not that the token is still valid, so a stale/expired file still
    reads OK here. Best-effort, not a guarantee; WARN (never FAIL) either
    way so a wrong guess can't block `takkub doctor` on a healthy machine.
    """
    # Must go through the shared resolver, not `os.environ` directly: an
    # installed cockpit runs codex panes under an isolated CODEX_HOME inside
    # DATA_HOME, so reading the inherited env var here would report on a
    # login file the panes never use.
    from .codex_helper import codex_home as _codex_home

    auth_file = _codex_home() / "auth.json"
    if not auth_file.is_file():
        return Finding(
            "providers",
            "codex-auth",
            Status.WARN,
            f"{auth_file} not found (heuristic — presence check only, unverified "
            "against a real codex auth failure; #248/#247)",
            "run `codex login` once to sign in",
        )
    try:
        json.loads(auth_file.read_text(encoding="utf-8"))
    except Exception as e:
        return Finding(
            "providers",
            "codex-auth",
            Status.WARN,
            f"{auth_file} present but unreadable: {e}",
            "run `codex login` again",
        )
    return Finding("providers", "codex-auth", Status.OK, f"{auth_file} present (heuristic)")


def check_provider_auth() -> list[Finding]:
    """[providers/*-auth] — auth status for every INSTALLED non-claude
    provider in the registry (#248/#247 round 2). Before this, `takkub
    doctor` had zero auth signal for any provider except claude
    (check_claude's credential-file check) — codex/gemini/opencode/kimi/
    cursor only ever got a binary-presence check from check_providers()
    above.

    Every provider's actual credential-storage location is otherwise a
    black box the cockpit deliberately never reads (see
    codex_helper.py/gemini_helper.py module docstrings: "the cockpit never
    touches those credentials") — codex is the one exception with a
    reasonably confident convention (CODEX_HOME/auth.json, see
    `_codex_auth_finding`). Every other provider reports Status.INFO
    "unknown" rather than a guessed "ok" — the explicit requirement here is
    never claiming a provider is authenticated without proof. A provider
    that isn't installed is skipped entirely; check_providers() already
    reports that.
    """
    from .provider_spec import PROVIDER_REGISTRY

    findings: list[Finding] = []
    for provider, spec in PROVIDER_REGISTRY.items():
        if provider == "claude":
            continue  # own dedicated check in check_claude()
        if _resolve_provider_bin(spec) is None:
            continue  # not installed — check_providers() already reports this
        if provider == "codex":
            findings.append(_codex_auth_finding())
            continue
        display = spec.display_name or provider.capitalize()
        findings.append(
            Finding(
                "providers",
                f"{provider}-auth",
                Status.INFO,
                f"unknown — no confirmed credential-file location for {display} yet (#248/#247)",
                spec.post_install_note or f"run the {provider} CLI once to verify sign-in manually",
            )
        )
    return findings


# ---------------------------------------------------------------------------
# [secrets]
# ---------------------------------------------------------------------------


def check_secret_backend() -> list[Finding]:
    """[secrets/*-backend] — added alongside `check_provider_auth()` (never
    replacing it), Phase 3 (epic #309): reports which `core.secrets`
    backend each installed provider resolves to, and whether that backend
    actually finds a credential. Same installed-provider filtering as
    `check_provider_auth()` via `_resolve_provider_bin`, so an uninstalled
    provider is skipped exactly like it is there (check_providers() already
    reports install state)."""
    from .core.secrets.backends import BackendStatus
    from .core.secrets.manager import default_backends
    from .provider_spec import PROVIDER_REGISTRY

    findings: list[Finding] = []
    backends = default_backends()
    for provider, spec in PROVIDER_REGISTRY.items():
        if _resolve_provider_bin(spec) is None:
            continue  # not installed — check_providers() already reports this
        backend = backends.get(provider)
        if backend is None:
            findings.append(
                Finding(
                    "secrets",
                    f"{provider}-backend",
                    Status.INFO,
                    "no secret backend registered yet — credential location unconfirmed (#309)",
                )
            )
            continue
        try:
            status = backend.status("default")
        except Exception as e:
            findings.append(
                Finding(
                    "secrets",
                    f"{provider}-backend",
                    Status.WARN,
                    f"check errored: {type(e).__name__}: {e}",
                )
            )
            continue
        detail = f"backend={backend.name}"
        if status is BackendStatus.FOUND:
            findings.append(Finding("secrets", f"{provider}-backend", Status.OK, detail))
        elif status is BackendStatus.MISSING:
            findings.append(
                Finding(
                    "secrets",
                    f"{provider}-backend",
                    Status.WARN,
                    f"{detail} — credential not found",
                    f"run the {provider} CLI once to sign in",
                )
            )
        else:
            findings.append(
                Finding(
                    "secrets",
                    f"{provider}-backend",
                    Status.INFO,
                    f"{detail} — unavailable on this platform",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# [hooks]
# ---------------------------------------------------------------------------


def check_hooks() -> list[Finding]:
    findings: list[Finding] = []

    if sys.platform == "win32":
        import os

        comspec = os.environ.get("COMSPEC")
        if comspec:
            findings.append(Finding("hooks", "COMSPEC", Status.OK, comspec))
        else:
            findings.append(
                Finding(
                    "hooks",
                    "COMSPEC",
                    Status.WARN,
                    "not set",
                    "missing — codex pane may crash; cockpit fixed this in cf6529b",
                )
            )

    return findings


def check_hook_wiring() -> list[Finding]:
    """Verify the Stop/Notification → `takkub _hook` wiring every spawned
    claude pane gets (hook_wiring.py) actually resolves: the generated
    settings file is well-formed AND the internal `_hook` command runs
    without crashing (invoked exactly like Claude Code would — hook JSON on
    stdin, no TAKKUB_ROLE — so it must fail-open with exit 0, no output)."""
    findings: list[Finding] = []
    try:
        from .hook_wiring import HOOK_COMMAND, ensure_hook_settings_file

        settings_path = ensure_hook_settings_file()
        data = json.loads(Path(settings_path).read_text(encoding="utf-8"))
        stop_cmds = [
            h.get("command")
            for grp in data.get("hooks", {}).get("Stop", [])
            for h in grp.get("hooks", [])
        ]
        notif_cmds = [
            h.get("command")
            for grp in data.get("hooks", {}).get("Notification", [])
            for h in grp.get("hooks", [])
        ]
        if HOOK_COMMAND in stop_cmds and HOOK_COMMAND in notif_cmds:
            findings.append(Finding("hooks", "settings-file", Status.OK, settings_path))
        else:
            findings.append(
                Finding(
                    "hooks",
                    "settings-file",
                    Status.FAIL,
                    f"Stop/Notification not wired to {HOOK_COMMAND!r} in {settings_path}",
                    "regenerate via hook_wiring.ensure_hook_settings_file()",
                )
            )
    except Exception as e:
        findings.append(Finding("hooks", "settings-file", Status.FAIL, str(e)))

    try:
        from ._win_console import SUBPROCESS_NO_WINDOW

        r = subprocess.run(
            [sys.executable, "-m", "agent_takkub.cli", "_hook"],
            input="{}",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
        if r.returncode == 0 and not r.stdout.strip():
            findings.append(
                Finding("hooks", "_hook command", Status.OK, "exits 0, no output (fail-open)")
            )
        else:
            detail = (r.stdout or r.stderr or "").strip()[:200] or f"exit {r.returncode}"
            findings.append(Finding("hooks", "_hook command", Status.FAIL, detail))
    except Exception as e:
        findings.append(Finding("hooks", "_hook command", Status.FAIL, str(e)))

    return findings


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------
# [markers] — ready-prompt detection self-test (M4#17)
# ---------------------------------------------------------------------------


def check_ready_markers() -> list[Finding]:
    """Self-test the central ready-prompt marker table against canonical sample
    screens. A FAIL here means an upstream CLI reword (or an edit) has broken
    idle/done detection for a provider — fix the table or set
    TAKKUB_EXTRA_READY_MARKERS."""
    from .pty_session import ready_marker_selftest

    failures = ready_marker_selftest()
    if not failures:
        return [Finding("markers", "ready-prompt", Status.OK, "all provider markers verified")]
    return [
        Finding(
            "markers",
            "ready-prompt",
            Status.FAIL,
            "; ".join(failures),
            "an upstream prompt reword likely broke detection — update _READY_RULES "
            "in pty_session.py or set TAKKUB_EXTRA_READY_MARKERS",
        )
    ]


def check_version() -> list[Finding]:
    """Report the cockpit's own version + how far behind origin/main it is.

    The GUI update chip already shows this, but a CLI-only user never sees it —
    so `takkub doctor` surfaces "you're N commits behind, here's how to update"
    too. This is the ONE check that touches the network: a best-effort
    `git fetch` (short timeout) so the behind-count is live; offline it
    degrades to the last-known origin/main ref and says so.
    """
    from .update_helper import (
        current_version_describe,
        fetch_remote,
        is_git_repo,
        local_status,
        pyproject_will_change_on_pull,
    )

    if not is_git_repo():
        from .config import is_installed_package

        remedy = (
            "run `npm update -g agent-takkub` to update"
            if is_installed_package()
            else "convert via the cockpit's update chip ('Enable updates') to enable updates"
        )
        return [
            Finding(
                "version",
                "tracking",
                Status.INFO,
                "not a git checkout — version-behind / one-click update disabled",
                remedy,
            )
        ]

    described = current_version_describe() or "(unknown)"
    fetched, _ = fetch_remote(timeout=8.0)  # best-effort; offline → last-known ref
    st = local_status()
    if not st.get("ok"):
        return [
            Finding("version", "current", Status.WARN, described, f"git: {st.get('error', '?')}")
        ]

    freshness = "" if fetched else "  (offline — vs last-known origin/main)"
    behind = st.get("behind", 0)
    findings: list[Finding] = []
    if behind == 0:
        findings.append(
            Finding("version", "current", Status.OK, f"{described} — up to date{freshness}")
        )
    else:
        hint = "update via the cockpit chip, or `git pull --ff-only origin main`"
        if pyproject_will_change_on_pull():
            hint += " then `pip install -e .` (dependencies changed)"
        findings.append(
            Finding(
                "version",
                "behind",
                Status.WARN,
                f"{described} — {behind} commit{'s' if behind != 1 else ''} behind "
                f"origin/main{freshness}",
                hint,
            )
        )
    if not st.get("clean", True):
        n = len(st.get("dirty_files", []))
        findings.append(
            Finding(
                "version",
                "local-edits",
                Status.INFO,
                f"{n} tracked file{'s' if n != 1 else ''} with uncommitted changes",
                "commit or stash before pulling",
            )
        )
    return findings


# ---------------------------------------------------------------------------
# [env] — persistent PATH health (npm global bin dir must stay on PATH)
# ---------------------------------------------------------------------------
# Field incident 2026-07-04: a Node update dropped %APPDATA%\npm from the user
# PATH → claude/takkub/agent-takkub all "command not found", panes couldn't
# spawn, and the user had to hand-repair the registry. This check makes that a
# one-click `takkub doctor --fix`.


def _npm_global_bin_dir() -> str | None:
    """The directory npm puts global shims in (None when npm is missing)."""
    from shutil import which as _which

    from ._win_console import SUBPROCESS_NO_WINDOW

    npm = _which("npm.cmd") or _which("npm")
    if not npm:
        return None
    try:
        r = subprocess.run(
            [npm, "prefix", "-g"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
    except Exception:
        return None
    prefix = (r.stdout or "").strip()
    if r.returncode != 0 or not prefix:
        return None
    return prefix if sys.platform == "win32" else str(Path(prefix) / "bin")


def _dir_on_path(target: str, path_value: str) -> bool:
    """Case/format-insensitive membership test for one dir in a PATH string."""

    def _norm(p: str) -> str:
        return os.path.normcase(os.path.normpath(os.path.expandvars(p.strip())))

    want = _norm(target)
    return any(_norm(p) == want for p in path_value.split(os.pathsep) if p.strip())


def _read_win_user_path() -> tuple[str, int]:
    """(value, registry value-kind) of HKCU\\Environment\\Path ('' if absent)."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        try:
            value, kind = winreg.QueryValueEx(key, "Path")
            return str(value), int(kind)
        except FileNotFoundError:
            return "", winreg.REG_EXPAND_SZ


def _append_win_user_path(bin_dir: str) -> tuple[bool, str]:
    """Append *bin_dir* to the persistent user PATH, preserving the existing
    registry value kind (REG_SZ vs REG_EXPAND_SZ), then broadcast
    WM_SETTINGCHANGE so new shells pick it up without a re-login."""
    import ctypes
    import winreg

    try:
        value, kind = _read_win_user_path()
        if _dir_on_path(bin_dir, value):
            return True, "already on PATH"
        new_value = (value.rstrip(";") + ";" if value else "") + bin_dir
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, "Path", 0, kind, new_value)
        # HWND_BROADCAST / WM_SETTINGCHANGE / SMTO_ABORTIFHUNG
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 0x0002, 5000, None)
    except OSError as e:
        return False, str(e)
    return True, f"added {bin_dir} to user PATH — open a NEW terminal to pick it up"


_PATHFIX_MARKER = "# >>> agent-takkub PATH >>>"


def _append_posix_rc_path(bin_dir: str) -> tuple[bool, str]:
    """Idempotently append an export block to ~/.zshrc (and ~/.bashrc if it
    exists) so login shells regain the npm global bin dir."""
    block = f'\n{_PATHFIX_MARKER}\nexport PATH="$PATH:{bin_dir}"\n# <<< agent-takkub PATH <<<\n'
    touched: list[str] = []
    try:
        rcs = [Path.home() / ".zshrc"]
        bashrc = Path.home() / ".bashrc"
        if bashrc.exists():
            rcs.append(bashrc)
        for rc in rcs:
            existing = rc.read_text(encoding="utf-8") if rc.exists() else ""
            if _PATHFIX_MARKER in existing:
                continue
            with rc.open("a", encoding="utf-8") as fh:
                fh.write(block)
            touched.append(rc.name)
    except OSError as e:
        return False, str(e)
    if not touched:
        return True, "already configured"
    return True, f"added PATH export to {', '.join(touched)} — restart the terminal"


def check_env_path() -> list[Finding]:
    """[env] — is the npm global bin dir on the *persistent* PATH?"""
    findings: list[Finding] = []
    bin_dir = _npm_global_bin_dir()
    if not bin_dir:
        findings.append(Finding("env", "npm-global-bin", Status.SKIP, "npm not found"))
        return findings

    if sys.platform == "win32":
        try:
            persistent, _kind = _read_win_user_path()
        except OSError as e:
            findings.append(Finding("env", "npm-global-bin", Status.WARN, f"registry: {e}"))
            return findings
        # The MACHINE PATH may also carry it (e.g. nvm4w installs system-wide).
        on_path = _dir_on_path(bin_dir, persistent) or _dir_on_path(
            bin_dir, os.environ.get("PATH", "")
        )
        if on_path:
            findings.append(Finding("env", "npm-global-bin", Status.OK, f"{bin_dir} on PATH"))
        else:
            findings.append(
                Finding(
                    "env",
                    "npm-global-bin",
                    Status.WARN,
                    f"{bin_dir} NOT on user PATH — claude/takkub can vanish from new terminals",
                    "takkub doctor --fix → appends it to the user PATH (registry-safe)",
                    auto_fix=lambda d=bin_dir: _append_win_user_path(d),
                )
            )
    else:
        if _dir_on_path(bin_dir, os.environ.get("PATH", "")):
            findings.append(Finding("env", "npm-global-bin", Status.OK, f"{bin_dir} on PATH"))
        else:
            findings.append(
                Finding(
                    "env",
                    "npm-global-bin",
                    Status.WARN,
                    f"{bin_dir} NOT on PATH — claude/takkub unavailable in new shells",
                    "takkub doctor --fix → adds an export block to ~/.zshrc",
                    auto_fix=lambda d=bin_dir: _append_posix_rc_path(d),
                )
            )
    return findings


def check_npm_registry() -> list[Finding]:
    """[env] — report private npm config without mutating user settings."""
    from .config import DEFAULT_NPM_REGISTRY, npm_registry

    override = os.environ.get("TAKKUB_NPM_REGISTRY")
    if override:
        return [
            Finding(
                "env",
                "npm-registry",
                Status.OK,
                f"TAKKUB_NPM_REGISTRY={npm_registry()}",
            )
        ]

    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        return [Finding("env", "npm-registry", Status.SKIP, "npm not found")]

    code, configured = _run([npm, "config", "get", "registry"])
    if code != 0 or not configured:
        return [
            Finding(
                "env",
                "npm-registry",
                Status.SKIP,
                f"อ่าน npm registry ไม่ได้: {configured or 'unknown error'}",
            )
        ]

    if configured.rstrip("/").lower() == DEFAULT_NPM_REGISTRY.rstrip("/").lower():
        return [Finding("env", "npm-registry", Status.OK, configured)]

    detail = (
        f"npm registry เป็น private ({configured}) — Claude CLI/takkub update ดึงจาก public npm; "
        "ตั้ง TAKKUB_NPM_REGISTRY=<mirror> หรือให้ public npm เข้าถึงได้"
    )
    return [Finding("env", "npm-registry", Status.WARN, detail)]


# ---------------------------------------------------------------------------
# [spawn-queue] — live wedge check (#141, `takkub doctor --live` only)
# ---------------------------------------------------------------------------

# A healthy spawn (ConPTY construction + gate checks) finishes in well under
# a second; the 400ms / 10s spawn-stagger slots in cli_server.py bound how
# fast the arbiter drains a busy-but-healthy queue. Anything sitting this
# long is a wedge, not load.
_SPAWN_QUEUE_STUCK_S = 60.0


def check_spawn_queue_live(resp: dict | None) -> list[Finding]:
    """[spawn-queue] — interpret a live spawn-arbiter status response (#141).

    Pure interpretation only — doctor.py is a `leaf-modules-pure` module
    (see pyproject.toml's import-linter contracts) and must NOT import
    `cli`/`orchestrator` itself, not even lazily inside a function (the
    linter's static analysis flags any import statement regardless of
    laziness). So the TCP round-trip lives in the CALLER: `cli.cmd_doctor`
    (when `--live` is passed) queries the running cockpit via
    `cli._request({"cmd": "spawn-queue-status"})` — the same protocol/socket
    the `takkub` CLI already uses — and passes the raw response dict here.
    Pass `None` when the cockpit isn't running at all (no port file) — NOT
    part of run_all_checks() / the pure-logic default, so a plain
    `takkub doctor` still needs no cockpit running. #141: the pure-logic
    checks reported "all checks passed" while 4 `takkub assign` calls sat
    wedged in the spawn arbiter's FIFO queue — this is the only way doctor
    can see that in-memory Orchestrator state at all.
    """
    if resp is None:
        return [
            Finding(
                "spawn-queue",
                "wedge",
                Status.SKIP,
                "cockpit is not running — live check unavailable",
                "start the cockpit, then re-run `takkub doctor --live`",
            )
        ]

    if not resp.get("ok"):
        return [
            Finding(
                "spawn-queue",
                "wedge",
                Status.WARN,
                f"live check failed: {resp.get('msg', 'unknown error')}",
            )
        ]

    depth = int(resp.get("queue_depth") or 0)
    in_progress = bool(resp.get("spawn_in_progress", False))
    in_progress_age = resp.get("spawn_in_progress_age_s")
    oldest_age = resp.get("oldest_queued_age_s")

    if depth == 0 and not in_progress:
        return [Finding("spawn-queue", "wedge", Status.OK, "queue empty, arbiter idle")]

    detail = f"depth={depth} in_progress={in_progress}"
    if in_progress_age is not None:
        detail += f" in_progress_age={in_progress_age:.0f}s"
    if oldest_age is not None:
        detail += f" oldest_queued_age={oldest_age:.0f}s"

    stuck = (in_progress_age is not None and in_progress_age >= _SPAWN_QUEUE_STUCK_S) or (
        oldest_age is not None and oldest_age >= _SPAWN_QUEUE_STUCK_S
    )
    if stuck:
        return [
            Finding(
                "spawn-queue",
                "wedge",
                Status.FAIL,
                detail,
                "spawn arbiter appears wedged — `takkub restart` to clear it",
            )
        ]
    return [Finding("spawn-queue", "wedge", Status.OK, detail)]


def check_performance_live(resp: dict | None) -> list[Finding]:
    """Interpret live governor, writer-queue and delivery telemetry."""
    if resp is None:
        return [
            Finding(
                "performance",
                "resource-governor",
                Status.SKIP,
                "cockpit is not running — live check unavailable",
            )
        ]
    if not resp.get("ok"):
        return [
            Finding(
                "performance",
                "resource-governor",
                Status.WARN,
                f"live check failed: {resp.get('msg', 'unknown error')}",
            )
        ]
    queues = resp.get("writer_queues") or {}
    max_depth = max((int(row.get("depth", 0)) for row in queues.values()), default=0)
    stale = sum(int(row.get("stale_dropped", 0)) for row in queues.values())
    status = Status.WARN if resp.get("overloaded") else Status.OK
    detail = (
        f"CPU={float(resp.get('cpu_percent', 0)):.0f}% "
        f"RAM-free={float(resp.get('available_memory_percent', 0)):.0f}% "
        f"heavy={int(resp.get('active_heavy_tasks', 0))} "
        f"resource-wait={int(resp.get('queued_resource_tasks', 0))} "
        f"writer-max={max_depth} stale-dropped={stale}"
    )
    return [Finding("performance", "resource-governor", status, detail)]


# ---------------------------------------------------------------------------
# [remote-mirror] — live check (2026-08-13, `takkub doctor --live` only)
# ---------------------------------------------------------------------------


def check_remote_mirror_live(resp: dict | None) -> list[Finding]:
    """[remote-mirror] — interpret a live `remote-mirror-status` response.

    Diagnoses "phone shows nothing back from Lead" one item at a time,
    reported by a friend running an OpenCode Lead (BlueParking project):
    the message DID reach Lead (cli_server writes straight into the pane —
    unrelated to this check), but the phone's live mirror never showed a
    reply because remote/notify.py had no registered history scanner for
    that provider at all.

    Pure interpretation only, same reasoning as `check_spawn_queue_live`
    above: doctor.py must not import `cli`/`orchestrator`, and (per the
    `remote-bolt-on-isolation` import-linter contract) must not import
    `agent_takkub.remote` either — so the live socket round-trip AND the
    pane-state read both live in the caller (`cli.cmd_doctor` when
    `--live` is passed → `cli_server.py`'s `remote-mirror-status` handler,
    which deliberately duplicates remote/notify.py's small uuid/path
    resolution rather than importing it). Pass `None` when the cockpit
    isn't running.
    """
    if resp is None:
        return [
            Finding(
                "remote-mirror",
                "lead-pane",
                Status.SKIP,
                "cockpit is not running — live check unavailable",
                "start the cockpit, then re-run `takkub doctor --live`",
            )
        ]
    if not resp.get("ok"):
        return [
            Finding(
                "remote-mirror",
                "lead-pane",
                Status.WARN,
                f"live check failed: {resp.get('msg', 'unknown error')}",
            )
        ]

    findings: list[Finding] = []
    project = resp.get("project") or "(unknown)"
    provider = str(resp.get("provider") or "").strip() or "(unknown)"
    lead_open = bool(resp.get("lead_pane_open"))
    supports = bool(resp.get("supports_remote_history"))
    session_uuid = resp.get("session_uuid")
    transcript_exists = resp.get("transcript_exists")

    if not lead_open:
        findings.append(
            Finding(
                "remote-mirror",
                "lead-pane",
                Status.WARN,
                f"project={project} has no open Lead pane — nothing to mirror right now",
                "open/spawn the Lead pane for this project, then re-run `takkub doctor --live`",
            )
        )
        return findings
    findings.append(
        Finding("remote-mirror", "lead-pane", Status.OK, f"project={project} provider={provider}")
    )

    if supports:
        findings.append(
            Finding(
                "remote-mirror",
                "history-scanner",
                Status.OK,
                f"provider={provider} has a registered remote-history scanner",
            )
        )
    else:
        findings.append(
            Finding(
                "remote-mirror",
                "history-scanner",
                Status.FAIL,
                f"provider={provider} has NO remote-history scanner registered "
                "(ProviderSpec.supports_remote_history=False) — the phone will "
                "never show a live reply for this Lead pane, by design, not a bug",
                "known gap for opencode/kimi/cursor (issue #103) — desktop is "
                "the only place to read this Lead's replies until a scanner "
                "ships for this provider",
            )
        )

    if session_uuid is None:
        findings.append(
            Finding(
                "remote-mirror",
                "session-uuid",
                Status.SKIP if not supports else Status.WARN,
                "no session_uuid recorded for this Lead pane yet",
                "check `takkub doctor` (non-live) hook-wiring findings — the "
                "SessionStart hook (`takkub session-report`) stamps this on "
                "every Claude spawn/resume; a non-claude Lead may never set it",
            )
        )
    else:
        findings.append(
            Finding("remote-mirror", "session-uuid", Status.OK, f"session_uuid={session_uuid[:8]}…")
        )

    if provider == "claude" and session_uuid is not None:
        if transcript_exists:
            findings.append(
                Finding(
                    "remote-mirror",
                    "transcript-file",
                    Status.OK,
                    "transcript file for this exact session_uuid exists on disk",
                )
            )
        else:
            findings.append(
                Finding(
                    "remote-mirror",
                    "transcript-file",
                    Status.FAIL,
                    "no transcript file matches this exact session_uuid — the "
                    "mirror will stay blank (never falls back to a newer/other "
                    "session file — that fallback was a proven bug, removed on "
                    "purpose, see remote/notify.py)",
                    "the pane's session_uuid has drifted from its actual "
                    "transcript (usually a manual `/resume` typed on the "
                    "desktop after spawn) — `/resume` again, or restart the "
                    "Lead pane so a fresh spawn re-stamps a matching uuid",
                )
            )

    return findings


def check_provider_isolation() -> list[Finding]:
    """[provider-isolation] — where each provider keeps its prod state.

    An installed cockpit is supposed to keep everything it runs inside
    DATA_HOME (user directive 2026-08-19), so a prod cockpit never shares
    sessions, config or logins with a dev checkout or with the user's own
    hand-run CLI. claude has done this since the C5 audit
    (``CLAUDE_CONFIG_DIR``); codex and opencode joined via
    ``config.provider_home_env``.

    gemini/kimi/cursor expose no directory env var at all, so they still
    write to their OS-wide homes. That is reported as INFO, not FAIL: it is
    an upstream gap (#103), not a broken install, and hiding it is how the
    isolation story silently looked complete while two thirds of the
    providers were outside it.
    """
    from .config import (
        DATA_HOME,
        PROVIDER_ISOLATION_GAPS,
        REPO_ROOT,
        provider_home_env,
    )

    if DATA_HOME == REPO_ROOT:
        return [
            Finding(
                "provider-isolation",
                "mode",
                Status.OK,
                "dev checkout — providers use their normal OS-wide homes by design",
            )
        ]

    findings: list[Finding] = []
    for provider in ("codex", "opencode"):
        env = provider_home_env(provider)
        if not env:
            continue
        detail = ", ".join(f"{var}={value}" for var, value in sorted(env.items()))
        findings.append(Finding("provider-isolation", provider, Status.OK, detail))
    for provider, reason in sorted(PROVIDER_ISOLATION_GAPS.items()):
        findings.append(
            Finding(
                "provider-isolation",
                provider,
                Status.INFO,
                f"not isolated — {reason}",
                "no action available yet; tracked as a multi-provider gap (#103)",
            )
        )
    return findings


def check_core_version_compat() -> list[Finding]:
    """[version/*] — Core V2 Schema/Adapter/Compat verdict per provider
    (#309 Phase 4): detected CLI version vs `core.versioning.compatibility`,
    plus a live-store schema fingerprint from `core.versioning.probe`.

    Deliberately NOT part of `run_all_checks()`'s default tuple — opt-in
    only via `takkub doctor --core-version` (cli.cmd_doctor), the same
    pattern `check_spawn_queue_live`/`check_remote_mirror_live` already use
    for "--live" — so a plain `takkub doctor` stays byte-identical to
    before this landed (project rule: every new connection point to
    existing behavior sits behind a flag that leaves the old path
    untouched when off). Never FAILs: an unregistered/unparseable provider
    is INFO, not a broken machine.
    """
    try:
        from .core.versioning.compatibility import DEFAULT_MATRIX, CompatVerdict
        from .core.versioning.detector import ProviderVersionDetector
        from .core.versioning.probe import probe_store
        from .provider_spec import PROVIDER_REGISTRY
    except Exception as e:
        return [Finding("version", "core-v2", Status.INFO, f"core.versioning unavailable: {e}")]

    findings: list[Finding] = []
    detector = ProviderVersionDetector()
    for provider in PROVIDER_REGISTRY:
        detected = detector.detect(provider)
        ev = DEFAULT_MATRIX.evaluate(provider, detected.version_text)
        shown = detected.version_text or "(not detected)"
        if ev.verdict == CompatVerdict.OK:
            findings.append(Finding("version", f"{provider}-compat", Status.OK, shown))
        elif ev.verdict == CompatVerdict.BELOW_MIN:
            findings.append(
                Finding(
                    "version",
                    f"{provider}-compat",
                    Status.WARN,
                    f"{shown} (below calibrated minimum)",
                )
            )
        elif ev.verdict == CompatVerdict.ABOVE_MAX:
            findings.append(
                Finding(
                    "version", f"{provider}-compat", Status.WARN, f"{shown} (above calibrated max)"
                )
            )
        elif ev.verdict == CompatVerdict.UNKNOWN:
            findings.append(
                Finding(
                    "version",
                    f"{provider}-compat",
                    Status.INFO,
                    f"{shown} (version string unparseable)",
                )
            )
        else:
            findings.append(
                Finding("version", f"{provider}-compat", Status.INFO, f"{shown} (not calibrated)")
            )

        probe = probe_store(provider)
        if probe.found:
            findings.append(
                Finding(
                    "version",
                    f"{provider}-store",
                    Status.INFO,
                    f"live store found — schema fingerprint: {len(probe.fingerprint)} keys/tables",
                )
            )
        else:
            findings.append(
                Finding("version", f"{provider}-store", Status.SKIP, probe.note or "no store found")
            )
    return findings


def check_storage_layout_state() -> list[Finding]:
    """[storage-layout/*] — V1/V2/mixed layout state (#309 Phase 8b) +
    which legacy files are still sitting unmigrated.

    Deliberately NOT part of `run_all_checks()`'s default tuple — opt-in
    only via `takkub doctor --storage-layout`, same "--live"/"--core-
    version" pattern this module already uses, so a plain `takkub doctor`
    stays byte-identical to before this landed. Never FAILs — a V1-only
    machine (every install today) is OK, not broken.
    """
    try:
        from .core.storage.layout import LEGACY_MAPPING, layout_state, storage_layout_v2
    except Exception as e:
        return [
            Finding(
                "storage-layout", "core-v2", Status.INFO, f"core.storage.layout unavailable: {e}"
            )
        ]

    state = layout_state()
    findings = [
        Finding(
            "storage-layout",
            "state",
            Status.OK,
            f"{state} — {storage_layout_v2().root}",
        )
    ]
    if state == "v1":
        findings.append(
            Finding(
                "storage-layout",
                "ladder",
                Status.INFO,
                f"{len({e.ladder_step for e in LEGACY_MAPPING if e.ladder_step > 0})} ladder step(s) "
                "not yet applied — `takkub migrate inspect` for detail",
            )
        )
    elif state == "mixed":
        findings.append(
            Finding(
                "storage-layout",
                "legacy-leftover",
                Status.INFO,
                "V2 layout exists alongside V1 files — expected until the deprecation ladder "
                "(plan §2, Phase 10) removes V1; not itself a problem",
            )
        )
    return findings


def run_all_checks() -> list[Finding]:
    findings: list[Finding] = []
    checks = (
        ("check_claude", check_claude),
        ("check_env_path", check_env_path),
        ("check_npm_registry", check_npm_registry),
        ("check_runtime", check_runtime),
        ("check_mini_browser", check_mini_browser),
        ("check_graft", check_graft),
        ("check_installed_integrity", check_installed_integrity),
        ("check_capability_skill_store", check_capability_skill_store),
        ("check_editable_install", check_editable_install),
        ("check_arch", check_arch),
        ("check_qt", check_qt),
        ("check_plugins", check_plugins),
        ("check_mcps", check_mcps),
        ("check_projects", check_projects),
        ("check_providers", check_providers),
        ("check_provider_isolation", check_provider_isolation),
        ("check_provider_auth", check_provider_auth),
        ("check_secret_backend", check_secret_backend),
        ("check_hooks", check_hooks),
        ("check_hook_wiring", check_hook_wiring),
        ("check_ready_markers", check_ready_markers),
        ("check_version", check_version),
    )
    for check_name, check in checks:
        try:
            findings.extend(check())
        except Exception as exc:
            findings.append(
                Finding(
                    "doctor",
                    check_name,
                    Status.FAIL,
                    f"check {check_name} errored: {type(exc).__name__}: {exc}",
                )
            )
    return findings


def run_auto_fixes(findings: list[Finding], install_providers: bool = False) -> None:
    """Run available fixes, keeping machine-level provider installs opt-in."""
    for f in findings:
        if f.auto_fix is None:
            continue
        if f.category == "providers" and not install_providers:
            print(
                f"  [skipped (opt-in)] {f.category}/{f.name}: "
                "takkub doctor --fix --install-providers or "
                f"takkub provider install {f.name}"
            )
            continue
        ok, msg = f.auto_fix()
        label = "fixed" if ok else "fix failed"
        print(f"  [{label}] {f.category}/{f.name}: {msg}")


# ---------------------------------------------------------------------------
# formatter
# ---------------------------------------------------------------------------

_STATUS_ICON: dict[Status, str] = {
    Status.OK: "✓",
    Status.WARN: "⚠",
    Status.FAIL: "✗",
    Status.SKIP: "-",
    Status.INFO: "·",
}


def format_report(findings: list[Finding]) -> str:
    lines: list[str] = []
    current_cat = ""
    counts: dict[Status, int] = {s: 0 for s in Status}

    for f in findings:
        if f.category != current_cat:
            if current_cat:
                lines.append("")
            lines.append(f"[{f.category}]")
            current_cat = f.category

        icon = _STATUS_ICON[f.status]
        name_col = f"{f.name:<18}"
        detail_part = f"  {f.detail}" if f.detail else ""
        lines.append(f"  {icon} {name_col}{detail_part}")
        if f.fix_hint:
            lines.append(f"    → fix: {f.fix_hint}")

        counts[f.status] += 1

    lines.append("")
    parts = []
    if counts[Status.OK]:
        parts.append(f"{counts[Status.OK]} ok")
    if counts[Status.WARN]:
        parts.append(f"{counts[Status.WARN]} warn")
    if counts[Status.FAIL]:
        parts.append(f"{counts[Status.FAIL]} fail")
    if counts[Status.SKIP]:
        parts.append(f"{counts[Status.SKIP]} skip")
    if counts[Status.INFO]:
        parts.append(f"{counts[Status.INFO]} info")
    lines.append("Summary: " + ", ".join(parts))

    return "\n".join(lines)
