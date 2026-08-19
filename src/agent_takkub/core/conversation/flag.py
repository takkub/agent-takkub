"""TAKKUB_V2_CONVERSATION — off by default, same shape as
`core.routing.flag.v2_router_enabled` (plan §0 rule 3)."""

from __future__ import annotations

import os


def v2_conversation_enabled() -> bool:
    return os.environ.get("TAKKUB_V2_CONVERSATION", "0") == "1"
