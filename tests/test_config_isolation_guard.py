"""#584 — Guard against unisolated host paths leaking into tests.

Detects any module in `agent_takkub` that binds an import-time copy of
`SETTINGS_HOME`, `DATA_HOME`, `RUNTIME_DIR`, `EVENTS_LOG`, `PROJECTS_JSON`, etc.
which points to the developer's real machine state rather than an isolated
tmp directory during test execution.
"""

from __future__ import annotations

import importlib
import pkgutil
import tempfile
import types
from pathlib import Path

import pytest

import agent_takkub

# Modules and attributes intentionally permitted to point to repository source
# or shipped read-only assets (never mutable user state).
# Format: (module_name, attribute_name): "Detailed rationale"
ALLOWLIST: dict[tuple[str, str], str] = {
    ("agent_takkub.config", "REPO_ROOT"): "Read-only application source tree root",
    (
        "agent_takkub.config",
        "ASSETS_ROOT",
    ): "Read-only shipped asset root (.claude/agents, CLAUDE.md)",
    ("agent_takkub.config", "CLI_BIN_DIR"): "Read-only shipped CLI executable directory (bin/)",
    ("agent_takkub.config", "AGENTS_DIR"): "Read-only shipped role prompt templates",
    ("agent_takkub.config", "SKILLS_DIR"): "Read-only shipped skill catalog",
    ("agent_takkub.cockpit_theme", "_FONTS_DIR"): "Static font bundle shipped with package",
    ("agent_takkub.cockpit_theme", "_STATIC_DIR"): "Static UI bundle shipped with package",
    ("agent_takkub.editor_widget", "_STATIC_DIR"): "Static editor UI bundle shipped with package",
    (
        "agent_takkub.remote.http_server",
        "_STATIC_ROOT",
    ): "Static web UI bundle shipped with package",
    (
        "agent_takkub.settings_window",
        "_NAV_ICONS_DIR",
    ): "Static navigation icons shipped with package",
    ("agent_takkub.terminal_widget", "_STATIC_DIR"): "Static xterm.js bundle shipped with package",
    ("agent_takkub.lead_context", "ASSETS_ROOT"): "Read-only shipped asset templates",
    ("agent_takkub.lead_context", "REPO_ROOT"): "Read-only source repository reference",
    (
        "agent_takkub.issues",
        "REPO_ROOT",
    ): "Read-only cockpit repository reference for git rev-parse",
    ("agent_takkub.orchestrator", "REPO_ROOT"): "Read-only cockpit repository reference",
    ("agent_takkub.spawn_engine", "REPO_ROOT"): "Read-only cockpit repository reference",
    (
        "agent_takkub.spawn_engine",
        "CLI_BIN_DIR",
    ): "Read-only CLI bin directory for spawned subprocesses",
    ("agent_takkub.vault_graph", "REPO_ROOT"): "Read-only cockpit repository reference",
    ("agent_takkub.vault_mirror", "REPO_ROOT"): "Read-only cockpit repository reference",
    ("agent_takkub.update_helper", "REPO_ROOT"): "Read-only repository root for update checks",
    ("agent_takkub.update_panel", "REPO_ROOT"): "Read-only repository root for update UI",
    ("agent_takkub.update_worker", "REPO_ROOT"): "Read-only repository root for update worker",
    ("agent_takkub.worktree_manager", "_WORKTREE_CONFIG_RELPATH"): (
        "Relative path inside worktree root (.takkub/worktree.json)"
    ),
}


def _is_path_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _is_isolated(path: Path, tmp_dir: Path | None) -> bool:
    try:
        resolved = path.resolve()
    except (ValueError, OSError):
        resolved = path

    # Under current test's tmp_path
    if tmp_dir is not None and _is_path_under(resolved, tmp_dir):
        return True

    # Under system temp directory
    system_tmp = Path(tempfile.gettempdir()).resolve()
    if _is_path_under(resolved, system_tmp):
        return True

    # Check for known isolation folder names
    parts = set(resolved.parts)
    if any(p.startswith("_isolated_") or p.startswith("pytest-") for p in parts):
        return True

    return False


def find_unisolated_paths(
    modules: list[tuple[str, types.ModuleType]] | None = None,
    tmp_dir: Path | None = None,
) -> list[tuple[str, str, Path, str]]:
    """Scan modules for module-level Path attributes pointing to real host state."""
    if modules is None:
        modules = []
        package = agent_takkub
        for _, modname, _ in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
            try:
                mod = importlib.import_module(modname)
                modules.append((modname, mod))
            except Exception:
                pass

    repo_root = Path(__file__).resolve().parents[1]
    real_targets = [
        (Path.home() / ".takkub", "real SETTINGS_HOME (~/.takkub)"),
        (Path.home() / ".claude", "real Claude profile directory (~/.claude)"),
        (Path.home() / ".agent-takkub", "real DATA_HOME (~/.agent-takkub)"),
        (repo_root / "runtime", "real RUNTIME_DIR (<repo>/runtime)"),
        (repo_root / "projects.json", "real PROJECTS_JSON (<repo>/projects.json)"),
        (repo_root / "project-skills", "real PROJECT_SKILLS_HOME (<repo>/project-skills)"),
        (repo_root / "skills", "real GLOBAL_SKILLS_HOME (<repo>/skills)"),
    ]

    leaks: list[tuple[str, str, Path, str]] = []

    for modname, mod in modules:
        for attr, val in list(vars(mod).items()):
            if attr.startswith("__"):
                continue
            if (modname, attr) in ALLOWLIST:
                continue
            if not isinstance(val, Path):
                continue

            if _is_isolated(val, tmp_dir):
                continue

            # Check if this unisolated path points to a real host state target
            try:
                res = val.resolve()
            except (ValueError, OSError):
                res = val

            for target_path, target_desc in real_targets:
                if target_path.exists():
                    target_resolved = target_path.resolve()
                else:
                    target_resolved = target_path

                if res == target_resolved or _is_path_under(res, target_resolved):
                    leaks.append((modname, attr, val, target_desc))
                    break

    return leaks


def test_no_unisolated_paths_in_agent_takkub_modules(tmp_path: Path) -> None:
    """Every module in agent_takkub must resolve paths to an isolated tmp dir
    during test execution, never touching the real host machine settings or runtime."""
    leaks = find_unisolated_paths(tmp_dir=tmp_path)
    if leaks:
        lines = [
            f"Found {len(leaks)} unisolated module path(s) pointing to real host machine state:"
        ]
        for modname, attr, val, target_desc in leaks:
            lines.append(f"  - {modname}.{attr} = {val} -> points under {target_desc}")
        lines.append(
            "\nFix: Isolate the attribute in `tests/conftest.py`'s `_isolate_runtime` fixture, "
            "or add it to `ALLOWLIST` in `tests/test_config_isolation_guard.py` with rationale."
        )
        pytest.fail("\n".join(lines))


def test_guard_detects_simulated_unisolated_module(tmp_path: Path) -> None:
    """Prove that find_unisolated_paths correctly catches a module that copies
    a host path at import time and escapes isolation."""
    # Create a dummy module with an unisolated path pointing to ~/.takkub
    fake_mod = types.ModuleType("agent_takkub.fake_vulnerable_module")
    fake_mod._BASE_DIR = Path.home() / ".takkub"  # type: ignore[attr-defined]
    fake_mod._EVENTS = Path(__file__).resolve().parents[1] / "runtime" / "events.log"  # type: ignore[attr-defined]

    leaks = find_unisolated_paths(
        modules=[("agent_takkub.fake_vulnerable_module", fake_mod)],
        tmp_dir=tmp_path,
    )
    assert len(leaks) == 2, f"Expected 2 leaks, found: {leaks}"

    attrs = {leak[1] for leak in leaks}
    assert "_BASE_DIR" in attrs
    assert "_EVENTS" in attrs


def test_guard_respects_allowlist(tmp_path: Path) -> None:
    """Prove that allowlisted attributes are exempt from guard failure."""
    fake_mod = types.ModuleType("agent_takkub.config")
    fake_mod.REPO_ROOT = Path(__file__).resolve().parents[1]  # type: ignore[attr-defined]

    leaks = find_unisolated_paths(
        modules=[("agent_takkub.config", fake_mod)],
        tmp_dir=tmp_path,
    )
    assert len(leaks) == 0, f"Allowlisted attribute should not be reported, got: {leaks}"


def test_guard_detects_nested_path_leak(tmp_path: Path) -> None:
    """Prove that nested/derived paths (e.g. SETTINGS_HOME / 'subdir' / 'file.json')
    are also caught by the guard."""
    fake_mod = types.ModuleType("agent_takkub.fake_nested_module")
    fake_mod._NESTED_PATH = Path.home() / ".takkub" / "projects" / "custom" / "profile.json"  # type: ignore[attr-defined]

    leaks = find_unisolated_paths(
        modules=[("agent_takkub.fake_nested_module", fake_mod)],
        tmp_dir=tmp_path,
    )
    assert len(leaks) == 1, f"Expected 1 leak for nested path, found: {leaks}"
    assert leaks[0][1] == "_NESTED_PATH"


def test_allowlist_entries_are_valid() -> None:
    """Verify that every entry in ALLOWLIST refers to an existing module and attribute
    in the codebase so the allowlist doesn't accumulate dead entries."""
    for (modname, attr), reason in ALLOWLIST.items():
        assert reason.strip(), (
            f"ALLOWLIST entry ({modname}, {attr}) must have a non-empty rationale"
        )
        try:
            mod = importlib.import_module(modname)
        except Exception as exc:
            pytest.fail(f"ALLOWLIST module {modname!r} could not be imported: {exc}")
        assert hasattr(mod, attr), (
            f"ALLOWLIST attribute {attr!r} does not exist on module {modname!r}"
        )
