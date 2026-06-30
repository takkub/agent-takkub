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


def installed_on_disk(home=None) -> set[str]:
    """Recommended plugin keys with a *loadable* install under
    ``~/.claude/plugins/cache`` — a pure filesystem check (no subprocess), safe
    on the Qt main thread.

    Applies the SAME condition as ``lead_context._default_plugin_dirs``: a
    ``<marketplace>/<plugin>/<version>/.claude-plugin/plugin.json`` must exist.
    Checking only the plugin folder would mark a half-populated cache dir (an
    interrupted install) as installed while panes silently skip it.
    """
    import pathlib

    base = (home or pathlib.Path.home()) / ".claude" / "plugins" / "cache"
    have: set[str] = set()
    for p in RECOMMENDED:
        plugin_dir = base / p.marketplace / p.key
        if not plugin_dir.is_dir():
            continue
        for v in plugin_dir.iterdir():
            if v.is_dir() and (v / ".claude-plugin" / "plugin.json").is_file():
                have.add(p.key)
                break
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


def ensure_marketplaces(plugins: list[RecommendedPlugin]) -> dict[str, tuple[bool, str]]:
    """Add each plugin's marketplace exactly once (deduped by repo).

    The recommended set shares one marketplace across four plugins, so adding it
    per-plugin would fire the same `claude plugin marketplace add` 4× (3 wasted
    120s-bounded network calls). Callers run this once before the install loop.
    """
    results: dict[str, tuple[bool, str]] = {}
    for repo in dict.fromkeys(p.marketplace_repo for p in plugins):
        results[repo] = _ensure_marketplace(repo)
    return results


def install_plugin(
    plugin: RecommendedPlugin, *, ensure_marketplace: bool = True
) -> tuple[bool, str]:
    """Install one plugin. (ok, message).

    Success is decided by the CLI **exit code**, not by matching a substring of
    stdout — a 0 exit with reworded output ("Installed X" / "already installed")
    is still a success. Pass ``ensure_marketplace=False`` when the caller has
    already run :func:`ensure_marketplaces` to avoid a redundant per-plugin add.
    """
    if ensure_marketplace:
        ok, msg = _ensure_marketplace(plugin.marketplace_repo)
        if not ok:
            return False, msg
    try:
        proc = _claude("plugin", "install", f"{plugin.key}@{plugin.marketplace}", timeout=120)
    except Exception as e:  # pragma: no cover
        return False, str(e)
    if proc.returncode == 0:
        return True, "installed"
    tail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
    return False, tail[-1] if tail else "install failed"
