"""Core V2 domain layer (epic #309, docs/v2/V2_IMPLEMENTATION_PLAN.md Phase 1).

Pure vocabulary + contracts for the V2 blueprint (account/model/router,
conversation, brain). Nothing here is wired into the running cockpit yet —
no import edge exists from `orchestrator.py`/`main_window.py`/`cli.py` into
this package. It is the bottom layer of the eventual V2 stack: importable
standalone, with zero PyQt6/engine/UI dependency (see the
`core-is-bottom-layer` import-linter contract in pyproject.toml).
"""

from __future__ import annotations
