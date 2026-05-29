"""Global test isolation.

The orchestrator writes its audit log (`_log_event` → events.log) and various
session/brief/task files under `RUNTIME_DIR`. Those paths are module-level
constants imported *by value* from `agent_takkub.config`, so a test that only
monkeypatches `orchestrator.RUNTIME_DIR` still lets `_log_event` (and
`ensure_runtime`) write to the REAL `runtime/events.log`. That pollution is
how the live log once bloated to 10 MB and wedged the cockpit (see
docs/cockpit-freeze-rca-2026-05-29.md).

This autouse fixture redirects EVENTS_LOG and RUNTIME_DIR — in every module
that bound them — to a per-test tmp dir, so no test can touch the real runtime.
"""

from __future__ import annotations

import importlib
import sys

import pytest

# Modules that bind RUNTIME_DIR / EVENTS_LOG as a module-level name. config is
# the source of truth (and what ensure_runtime uses); the rest copy the value
# at import time, so each needs its own patch. main_window is patched only if
# already imported — we never force-import the GUI window in unit tests.
_RUNTIME_DIR_MODULES = (
    "agent_takkub.config",
    "agent_takkub.orchestrator",
    "agent_takkub.agent_pane",
    "agent_takkub.lead_bash_audit",
    "agent_takkub.shared_dev_tools",
)
_EVENTS_LOG_MODULES = (
    "agent_takkub.config",
    "agent_takkub.orchestrator",
)
# Patched when present but never force-imported (heavy GUI deps).
_OPTIONAL_MODULES = ("agent_takkub.main_window",)


def _maybe_module(name: str, *, force: bool):
    mod = sys.modules.get(name)
    if mod is None and force:
        try:
            mod = importlib.import_module(name)
        except Exception:
            return None
    return mod


@pytest.fixture(autouse=True)
def _isolate_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path):
    # Distinct name (not "runtime") so we don't collide with test-local fixtures
    # that do `(tmp_path / "runtime").mkdir()` without exist_ok. Tests that set
    # their own RUNTIME_DIR re-patch over ours (autouse runs first); this is
    # just the safety net for everything else.
    runtime = tmp_path / "_isolated_runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    events = runtime / "events.log"

    targets = [(n, "RUNTIME_DIR", runtime) for n in _RUNTIME_DIR_MODULES]
    targets += [(n, "EVENTS_LOG", events) for n in _EVENTS_LOG_MODULES]
    for name in _OPTIONAL_MODULES:
        targets.append((name, "RUNTIME_DIR", runtime))
        targets.append((name, "EVENTS_LOG", events))

    for name, attr, value in targets:
        force = name in ("agent_takkub.config", "agent_takkub.orchestrator")
        mod = _maybe_module(name, force=force)
        if mod is not None and hasattr(mod, attr):
            monkeypatch.setattr(mod, attr, value, raising=False)

    yield
