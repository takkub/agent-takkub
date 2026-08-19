"""TAKKUB_V2_BRAIN — off by default (plan §0 rule 3: "feature flag ทุกจุด
เชื่อม — ปิด flag = พฤติกรรมเดิมเป๊ะ"). Nothing calls into Second Brain yet
(7c Context Builder is the first real caller), so today this flag only
gates `core.brain.facade` — but it's defined up front so 7c doesn't need to
invent its own gate.

Env wins when set; unset falls back to the Settings UI's persisted toggle
(epic #309 Phase 9, `core_v2_settings.flag_enabled`).
"""

from __future__ import annotations

import os


def v2_brain_enabled() -> bool:
    raw = os.environ.get("TAKKUB_V2_BRAIN")
    if raw is not None:
        return raw == "1"
    from agent_takkub import core_v2_settings

    return core_v2_settings.flag_enabled("brain")
