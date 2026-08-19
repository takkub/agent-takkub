"""TAKKUB_V2_ROUTER — off by default (plan §0 rule 3: "feature flag ทุกจุด
เชื่อม — ปิด flag = พฤติกรรมเดิมเป๊ะ"). `TAKKUB_V2_ROUTER` always wins when
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
