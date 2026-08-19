"""Where V2 state lives on disk — reads `config.DATA_HOME`/`config.RUNTIME_DIR`,
never declares a new home (plan §3.4). `agent_takkub.config` is import-linter's
`leaf-modules-pure` (stdlib only, no PyQt6/orchestrator) — verified safe to
import from the core bottom layer by `tests/test_core_jsonl_store.py`'s
subprocess Qt-import check.
"""

from __future__ import annotations

from pathlib import Path

from agent_takkub import config


def core_home() -> Path:
    return config.RUNTIME_DIR / "core"


def core_store_path(name: str) -> Path:
    return core_home() / f"{name}.jsonl"


def conversations_root() -> Path:
    return core_home() / "conversations"


def conversation_dir(project_id: str, conversation_id: str) -> Path:
    """One directory per conversation, project-scoped (plan §5.2) — never a
    single flat file, so a size cap / rotation only ever touches its own
    conversation's data.
    """
    safe_project = project_id or "_no_project"
    return conversations_root() / safe_project / conversation_id
