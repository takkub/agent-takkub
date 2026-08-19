"""V2 contracts — `typing.Protocol`, sync only (docs/v2/V2_IMPLEMENTATION_PLAN.md
§3.4 R1: no `async def`; heavy I/O still goes through QThread/`ProviderUsageStore`
pattern at the call site, not the Protocol itself).
"""

from __future__ import annotations
