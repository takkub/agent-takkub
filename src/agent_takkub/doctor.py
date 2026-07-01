"""takkub doctor — diagnose cockpit environment.

Pure-logic checks: no orchestrator TCP, no installs. Network is avoided except
by `check_version`, which does ONE best-effort `git fetch` (short timeout,
degrades to the last-known ref offline) so a CLI-only user learns they're behind
origin/main. Every subprocess call uses a short timeout + SUBPROCESS_NO_WINDOW
to prevent hangs.
"""

from __future__ import annotations

import json
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
    if sys.platform == "win32":
        # credentials may live in Windows Credential Manager — not directly checkable
        creds = Path.home() / ".claude" / "credentials.json"
        if creds.is_file():
            try:
                json.loads(creds.read_text(encoding="utf-8"))
                findings.append(
                    Finding("claude", "authenticated", Status.OK, "credentials.json present")
                )
            except Exception:
                findings.append(
                    Finding(
                        "claude",
                        "authenticated",
                        Status.WARN,
                        "credentials.json present but unreadable",
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
        creds = Path.home() / ".claude" / "credentials.json"
        if creds.is_file():
            try:
                json.loads(creds.read_text(encoding="utf-8"))
                findings.append(
                    Finding("claude", "authenticated", Status.OK, "credentials.json present")
                )
            except Exception:
                findings.append(
                    Finding(
                        "claude",
                        "authenticated",
                        Status.WARN,
                        "credentials.json present but unreadable",
                        "run 'claude login' from a terminal",
                    )
                )
        else:
            findings.append(
                Finding(
                    "claude",
                    "authenticated",
                    Status.WARN,
                    "credentials.json not found",
                    "run 'claude login' from a terminal",
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
    dp_file = Path.home() / ".takkub" / "disabled-providers.json"
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
        return [
            Finding(
                "version",
                "tracking",
                Status.INFO,
                "not a git checkout — version-behind / one-click update disabled",
                "convert via the cockpit's update chip ('Enable updates') to enable updates",
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


def run_all_checks() -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(check_claude())
    findings.extend(check_runtime())
    findings.extend(check_qt())
    findings.extend(check_plugins())
    findings.extend(check_mcps())
    findings.extend(check_projects())
    findings.extend(check_providers())
    findings.extend(check_hooks())
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
