"""Guard: UI-side files must never reach into Orchestrator's private
pane-registry state directly (`orch._panes_by_project` / `orch._pane_state`).

These are plain attribute reads, not imports, so import-linter's module-layer
contracts can't see them — this test greps the source instead. Use
`Orchestrator.iter_all_panes()` (or another public accessor) rather than
poking the private dicts owned by `SpawnEngineMixin` (see
`docs/architecture/godfile-map.md`, spawn_engine cluster).
"""

from __future__ import annotations

import re
from pathlib import Path

SRC_DIR = Path(__file__).parent.parent / "src" / "agent_takkub"

UI_FILES = (
    "update_panel.py",
    "main_window.py",
    "logs_panel.py",
    "user_actions.py",
    "settings_window.py",
    "usage_meter.py",
    "terminal_widget.py",
)

FORBIDDEN_PATTERN = re.compile(r"orch\._panes_by_project|orch\._pane_state")


def test_ui_files_do_not_touch_orchestrator_private_pane_state() -> None:
    offenders = {}
    for name in UI_FILES:
        path = SRC_DIR / name
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")
        matches = FORBIDDEN_PATTERN.findall(content)
        if matches:
            offenders[name] = matches

    assert not offenders, (
        "UI files must use Orchestrator.iter_all_panes() instead of reaching "
        f"into private pane-registry state directly: {offenders}"
    )
