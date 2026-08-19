"""ProviderAdapter implementations (Phase 3, epic #309) — see
`claude_adapter.py`/`cli_adapter.py` module docstrings for the WRAP scope
decision (why `spawn`/`send`/`is_ready`/`terminate` are documented stubs)."""

from __future__ import annotations

from .claude_adapter import ClaudeCliAdapter
from .cli_adapter import CliProviderAdapter
from .errors import ProviderAdapterNotWired
from .registry import adapter_for

__all__ = [
    "ClaudeCliAdapter",
    "CliProviderAdapter",
    "ProviderAdapterNotWired",
    "adapter_for",
]
