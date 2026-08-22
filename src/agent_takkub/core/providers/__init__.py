"""ProviderAdapter implementations (Phase 3, epic #309) — see
`claude_adapter.py`/`cli_adapter.py` module docstrings for the WRAP scope
decision (why `spawn`/`send`/`is_ready`/`terminate` are documented stubs)."""

from __future__ import annotations

from .claude_adapter import ClaudeCliAdapter
from .claude_plan import assemble_claude_argv
from .cli_adapter import CliProviderAdapter
from .errors import ProviderAdapterNotWired
from .plan import account_env_overrides, assemble_generic_argv, build_generic_spawn_plan
from .registry import adapter_for

__all__ = [
    "ClaudeCliAdapter",
    "CliProviderAdapter",
    "ProviderAdapterNotWired",
    "account_env_overrides",
    "adapter_for",
    "assemble_claude_argv",
    "assemble_generic_argv",
    "build_generic_spawn_plan",
]
