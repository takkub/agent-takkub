"""Recommended dev-team plugin set + installer — backs the 🧩 Plugins button.

The cockpit ships a curated plugin team (Superpowers, Frontend Design, Code
Review, Security Review, Claude Mem). This module knows that set, can report
which are installed, and can install the missing ones via the ``claude plugin``
CLI. Installs are git-clone + network operations, so the UI runs them on a
background thread (see ``user_actions._PluginInstallThread``) — never on the Qt
main thread.

Config home: the cockpit GUI reads plugins from ``~/.claude/plugins`` (see
``lead_context._default_plugin_dirs``), so installs inherit the GUI process's
default ``CLAUDE_CONFIG_DIR`` and land where panes actually look. A session that
overrides ``CLAUDE_CONFIG_DIR`` (e.g. a ``.claude-work`` profile) would install
elsewhere — the GUI does not, which is exactly why the button lives in the GUI.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from ._win_console import SUBPROCESS_NO_WINDOW


@dataclass(frozen=True)
class RecommendedPlugin:
    """One entry in the recommended dev-team plugin set.

    ``key`` is the plugin name as ``claude plugin install`` expects it;
    ``marketplace`` is its marketplace name; ``marketplace_repo`` is the
    ``owner/repo`` passed to ``claude plugin marketplace add`` when the
    marketplace isn't registered yet. ``pane_loaded`` records whether the
    cockpit injects it into teammate panes (skill-only plugins) or only keeps
    it user-enabled (hook-heavy plugins excluded via ``_PANE_PLUGIN_DENYLIST``).
    """

    key: str
    marketplace: str
    marketplace_repo: str
    label: str
    note: str = ""
    pane_loaded: bool = True


# The dev-team set. Order = display order. superpowers has its own marketplace;
# the rest share Anthropic's official one. security-guidance + remember register
# SessionStart command hooks, so they're installed + user-enabled but NOT pushed
# into every pane (pane_loaded=False) — see lead_context._PANE_PLUGIN_DENYLIST.
RECOMMENDED: tuple[RecommendedPlugin, ...] = (
    RecommendedPlugin(
        "superpowers",
        "superpowers-dev",
        "obra/superpowers",
        "Superpowers",
        "brainstorm / TDD / debug / planning",
    ),
    RecommendedPlugin(
        "frontend-design",
        "claude-plugins-official",
        "anthropics/claude-plugins-official",
        "Frontend Design",
        "production-grade UI (Anthropic)",
    ),
    RecommendedPlugin(
        "code-review",
        "claude-plugins-official",
        "anthropics/claude-plugins-official",
        "Code Review",
        "multi-agent PR review (Anthropic)",
    ),
    RecommendedPlugin(
        "security-guidance",
        "claude-plugins-official",
        "anthropics/claude-plugins-official",
        "Security Review",
        "SAST + diff review · hook-heavy (user-only)",
        pane_loaded=False,
    ),
    RecommendedPlugin(
        "remember",
        "claude-plugins-official",
        "anthropics/claude-plugins-official",
        "Claude Mem",
        "continuous memory · hook-heavy (user-only)",
        pane_loaded=False,
    ),
)


def _claude(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    """Run a ``claude`` subcommand with NO console window + a finite timeout.
    Never inherits a shell; caller inspects returncode/stdout."""
    return subprocess.run(
        ["claude", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        encoding="utf-8",
        errors="replace",
        creationflags=SUBPROCESS_NO_WINDOW,
    )


def parse_installed_keys(plugin_list_stdout: str) -> set[str]:
    """Extract installed plugin keys from ``claude plugin list`` output.

    Lines look like ``  ❯ code-review@claude-plugins-official``. Pure (no IO)
    so it's unit-testable; ``installed_plugin_keys`` does the subprocess call.
    """
    keys: set[str] = set()
    for raw in plugin_list_stdout.splitlines():
        line = raw.strip().lstrip("❯").strip()
        if "@" in line and " " not in line:
            keys.add(line.split("@", 1)[0])
    return keys


def installed_plugin_keys() -> set[str]:
    """Set of installed plugin keys per ``claude plugin list``. Empty on error."""
    try:
        proc = _claude("plugin", "list", timeout=40)
    except Exception:
        return set()
    return parse_installed_keys(proc.stdout or "")


def installed_on_disk(home=None) -> set[str]:
    """Recommended plugin keys present under ``~/.claude/plugins/cache`` — a
    pure filesystem check (no subprocess), safe to call on the Qt main thread.

    Mirrors exactly where ``lead_context._default_plugin_dirs`` looks, so this
    reflects what cockpit panes will actually load, not just what some other
    CLAUDE_CONFIG_DIR has registered.
    """
    import pathlib

    base = (home or pathlib.Path.home()) / ".claude" / "plugins" / "cache"
    have: set[str] = set()
    for p in RECOMMENDED:
        if (base / p.marketplace / p.key).is_dir():
            have.add(p.key)
    return have


def missing_plugins(installed: set[str] | None = None) -> list[RecommendedPlugin]:
    """Recommended plugins not yet installed on disk (checks the cache if not
    given). Filesystem-based so it's safe to call without a background thread."""
    have = installed_on_disk() if installed is None else installed
    return [p for p in RECOMMENDED if p.key not in have]


def _ensure_marketplace(repo: str) -> tuple[bool, str]:
    try:
        proc = _claude("plugin", "marketplace", "add", repo, timeout=120)
    except Exception as e:  # pragma: no cover - network/binary failure
        return False, str(e)
    blob = ((proc.stdout or "") + (proc.stderr or "")).lower()
    ok = proc.returncode == 0 or "already" in blob
    return ok, "marketplace ready" if ok else "marketplace add failed"


def install_plugin(plugin: RecommendedPlugin) -> tuple[bool, str]:
    """Add the marketplace if needed, then install one plugin. (ok, message)."""
    ok, msg = _ensure_marketplace(plugin.marketplace_repo)
    if not ok:
        return False, msg
    try:
        proc = _claude("plugin", "install", f"{plugin.key}@{plugin.marketplace}", timeout=120)
    except Exception as e:  # pragma: no cover
        return False, str(e)
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0 and "successfully installed" in out.lower():
        return True, "installed"
    tail = out.strip().splitlines()
    return False, tail[-1] if tail else "install failed"
