"""TAKKUB_V2_ROUTER — ON by default since 1.0.84 (epic #309) — `=0` is now the escape hatch,
and plan §0 rule 3 ("ปิด flag = พฤติกรรมเดิมเป๊ะ") still holds for it.
 `TAKKUB_V2_ROUTER` always wins when
set; unset falls back to the Settings UI's persisted toggle (epic #309 Phase
9, `core_v2_settings.flag_enabled`) so a user can flip this without an env
var, but an operator's explicit env override is never silently shadowed."""

from __future__ import annotations

import os


def v2_router_enabled() -> bool:
    raw = os.environ.get("TAKKUB_V2_ROUTER")
    if raw is not None:
        return raw == "1"
    from agent_takkub import core_v2_settings

    return core_v2_settings.flag_enabled("router")
