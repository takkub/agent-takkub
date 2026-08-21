"""TAKKUB_V2_CONVERSATION — ON by default since 1.0.84 (epic #309) — `=0` is now the escape hatch,
and plan §0 rule 3 ("ปิด flag = พฤติกรรมเดิมเป๊ะ") still holds for it. Same shape as
`core.routing.flag.v2_router_enabled` (plan §0 rule 3). Env wins when set;
unset falls back to the Settings UI's persisted toggle (epic #309 Phase 9,
`core_v2_settings.flag_enabled`)."""

from __future__ import annotations

import os


def v2_conversation_enabled() -> bool:
    raw = os.environ.get("TAKKUB_V2_CONVERSATION")
    if raw is not None:
        return raw == "1"
    from agent_takkub import core_v2_settings

    return core_v2_settings.flag_enabled("conversation")
