"""TAKKUB_V2_SCHEDULER — off by default (plan §0 rule 3: "feature flag ทุกจุด
เชื่อม — ปิด flag = พฤติกรรมเดิมเป๊ะ"), same shape as
`core.conversation.flag.v2_conversation_enabled`."""

from __future__ import annotations

import os


def v2_scheduler_enabled() -> bool:
    return os.environ.get("TAKKUB_V2_SCHEDULER", "0") == "1"
