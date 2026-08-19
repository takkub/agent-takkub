"""Where V2 state lives on disk — reads `config.DATA_HOME`/`config.RUNTIME_DIR`,
never declares a new home (plan §3.4). `agent_takkub.config` is import-linter's
`leaf-modules-pure` (stdlib only, no PyQt6/orchestrator) — verified safe to
import from the core bottom layer by `tests/test_core_jsonl_store.py`'s
subprocess Qt-import check.
"""

from __future__ import annotations

from pathlib import Path

from agent_takkub import config

from .layout import storage_layout_v2


def core_home() -> Path:
    """Core V2's own internal store (journal, version.json, jsonl stores).

    Storage V2 (#309 Phase 8b) reserves `StorageLayoutV2.system` as this
    dir's V2 destination — but no ladder step relocates it yet (the ladder
    in `docs/v2/V2_IMPLEMENTATION_PLAN.md` §5.3 only migrates V1 *config*
    files, not Core's already-established V2 internal storage). So this
    resolves to the V2 `system/` dir once something creates it, and falls
    back to the original `RUNTIME_DIR/core` location until then — today
    that's every existing installation, so Phase 1–8a callers see byte
    identical behaviour (plan §3.4's legacy-fallback rule).
    """
    v2 = storage_layout_v2().system
    if v2.is_dir():
        return v2
    return config.RUNTIME_DIR / "core"


def core_store_path(name: str) -> Path:
    return core_home() / f"{name}.jsonl"


def conversations_root() -> Path:
    return core_home() / "conversations"


def conversation_dir(project_id: str, conversation_id: str) -> Path:
    """One directory per conversation, project-scoped (plan §5.2) — never a
    single flat file, so a size cap / rotation only ever touches its own
    conversation's data.

    Legacy fallback (plan §3.4): once *project_id*'s own V2 project subtree
    exists (`StorageLayoutV2.project(project_id).root`), conversations move
    there per blueprint §9 ("Project conversation ตัวจริงควรเก็บใน Project
    เพื่อ isolation"); until then this stays under the original
    `core_home()/conversations/<project_id>/` — unchanged for every project
    that hasn't been migrated.
    """
    safe_project = project_id or "_no_project"
    project_layout_root = storage_layout_v2().project(safe_project).root
    if project_layout_root.is_dir():
        return project_layout_root / "conversations" / conversation_id
    return conversations_root() / safe_project / conversation_id
