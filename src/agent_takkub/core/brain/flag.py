"""TAKKUB_V2_BRAIN — off by default (plan §0 rule 3: "feature flag ทุกจุด
เชื่อม — ปิด flag = พฤติกรรมเดิมเป๊ะ"). Nothing calls into Second Brain yet
(7c Context Builder is the first real caller), so today this flag only
gates `core.brain.facade` — but it's defined up front so 7c doesn't need to
invent its own gate.
"""

from __future__ import annotations

import os


def v2_brain_enabled() -> bool:
    return os.environ.get("TAKKUB_V2_BRAIN", "0") == "1"
