"""TAKKUB_V2_BRAIN / TAKKUB_V2_CONTEXT — both off by default (plan §0 rule
3: "feature flag ทุกจุดเชื่อม — ปิด flag = พฤติกรรมเดิมเป๊ะ").

`TAKKUB_V2_BRAIN` gates every *write/read* path into the Second Brain
itself: `core.brain.facade.recall`/`submit`/`on_pane_done`.

`TAKKUB_V2_CONTEXT` is a SEPARATE flag (Phase 7c) gating only the
Context-Injection hook in `orchestrator._assign_dispatch` —
`core.brain.facade.build_context_for_assign`. It is independent on purpose:
Context Builder only *reads* whatever the Second Brain already has on disk
(directly via `RetrievalEngine`/`BrainStore`, not through `recall()`), so it
can be enabled even while `TAKKUB_V2_BRAIN` — the write-path flag — is off,
and vice versa.
"""

from __future__ import annotations

import os


def v2_brain_enabled() -> bool:
    return os.environ.get("TAKKUB_V2_BRAIN", "0") == "1"


def v2_context_enabled() -> bool:
    return os.environ.get("TAKKUB_V2_CONTEXT", "0") == "1"
