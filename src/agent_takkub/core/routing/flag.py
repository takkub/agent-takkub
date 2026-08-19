"""TAKKUB_V2_ROUTER — off by default (plan §0 rule 3: "feature flag ทุกจุด
เชื่อม — ปิด flag = พฤติกรรมเดิมเป๊ะ")."""

from __future__ import annotations

import os


def v2_router_enabled() -> bool:
    return os.environ.get("TAKKUB_V2_ROUTER", "0") == "1"
