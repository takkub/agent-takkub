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

Env wins when set; unset falls back to the Settings UI's persisted toggle
(epic #309 Phase 9, `core_v2_settings.flag_enabled`).
"""

from __future__ import annotations

import os


def _env_or_setting(env_name: str, setting_name: str) -> bool:
    raw = os.environ.get(env_name)
    if raw is not None:
        return raw == "1"
    from agent_takkub import core_v2_settings

    return core_v2_settings.flag_enabled(setting_name)


def v2_brain_enabled() -> bool:
    return _env_or_setting("TAKKUB_V2_BRAIN", "brain")


def v2_context_enabled() -> bool:
    return _env_or_setting("TAKKUB_V2_CONTEXT", "context")
