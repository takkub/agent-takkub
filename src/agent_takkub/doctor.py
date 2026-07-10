"""takkub doctor — diagnose cockpit environment.

Pure-logic checks: no orchestrator TCP, no installs. Network is avoided except
by `check_version`, which does ONE best-effort `git fetch` (short timeout,
degrades to the last-known ref offline) so a CLI-only user learns they're behind
origin/main. Every subprocess call uses a short timeout + SUBPROCESS_NO_WINDOW
to prevent hangs.
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
    """Run *argv* with timeout=5. Returns (returncode, combined output)."""
    from ._win_console import SUBPROCESS_NO_WINDOW

    try:
        r = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=5,
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
                zshrc.write_text(existing + sep + _ARM64_GUARD_BLOCK, encoding="utf-8")
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
    from .config import AGENTS_DIR, ASSETS_ROOT, CLI_BIN_DIR, DATA_HOME, REPO_ROOT, RUNTIME_DIR

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
# [providers]
# ---------------------------------------------------------------------------


def check_providers() -> list[Finding]:
    findings: list[Finding] = []

    # The `gemini` teammate role runs on Antigravity's `agy` binary
    # (Google retired the standalone Gemini CLI on 2026-06-18). Resolve via the
    # SAME helper the cockpit uses at spawn time — `find_agy_executable()` falls
    # back to %LOCALAPPDATA%\agy\bin when the installer didn't register PATH, so
    # doctor doesn't falsely report "not installed" for a gemini role that
    # actually works. Use the resolved absolute path in `_run` so `--version`
    # succeeds even when the binary is off-PATH.
    def _resolve_provider_bin(provider: str, binary: str) -> str | None:
        try:
            if provider == "gemini":
                from .gemini_helper import find_agy_executable

                return find_agy_executable()
            if provider == "codex":
                from .codex_helper import find_codex_executable

                return find_codex_executable()
        except Exception:
            pass
        return shutil.which(binary)

    for provider, binary in (("codex", "codex"), ("gemini", "agy")):
        path = _resolve_provider_bin(provider, binary)
        if path:
            rc, ver = _run([path, "--version"])
            version = (ver.splitlines()[0] if ver else path) if rc == 0 else path
            findings.append(Finding("providers", provider, Status.INFO, version))
        else:
            hint = (
                "install the Antigravity CLI (https://antigravity.google/download) "
                "to use the 'gemini' teammate role"
                if provider == "gemini"
                else f"install {binary} CLI to use '{provider}' teammate role"
            )
            findings.append(
                Finding(
                    "providers",
                    provider,
                    Status.SKIP,
                    "not installed (optional)",
                    hint,
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
            rc.write_text(existing + block, encoding="utf-8")
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


def run_all_checks() -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(check_claude())
    findings.extend(check_env_path())
    findings.extend(check_runtime())
    findings.extend(check_installed_integrity())
    findings.extend(check_arch())
    findings.extend(check_qt())
    findings.extend(check_plugins())
    findings.extend(check_mcps())
    findings.extend(check_projects())
    findings.extend(check_providers())
    findings.extend(check_hooks())
    findings.extend(check_hook_wiring())
    findings.extend(check_ready_markers())
    findings.extend(check_version())
    return findings


def run_auto_fixes(findings: list[Finding]) -> None:
    for f in findings:
        if f.auto_fix is not None:
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
