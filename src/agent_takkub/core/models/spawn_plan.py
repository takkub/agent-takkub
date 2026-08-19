"""`SpawnPlan` — the pure-data output of `core.providers` plan-building
(epic #309 Phase 3b: "core กำหนด SpawnPlan ... เป็น pure data · spawn_engine
เป็นคน execute plan").

Carries everything a spawn needs that can be computed WITHOUT touching Qt/
PaneState (argv, env, cwd, system prompt file, paste timing, ready-detection
rules) — see `core/providers/plan.py` for the builders. Anything still tied
to `PaneState`/Qt (token minting, `_launch_session`'s `PtySession`
construction, MCP subprocess resolution, agents.md file rendering) stays in
`spawn_engine.py`, which executes a plan rather than building one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class SpawnPlan:
    provider_id: str
    argv: tuple[str, ...] = field(default_factory=tuple)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    system_prompt_file: str | None = None
    paste_timing: dict[str, Any] = field(default_factory=dict)
    ready_rules: dict[str, Any] = field(default_factory=dict)
