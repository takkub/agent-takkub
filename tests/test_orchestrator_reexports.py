"""Guard the orchestrator.py re-export façade (pyproject F401 per-file-ignore).

orchestrator.py intentionally imports symbols from mixin/helper modules purely
so tests/app/main_window can keep patching/importing them off `orchestrator`.
A "clean up unused import" pass can silently delete one of these and break
dozens of tests that patch e.g. ``orch_mod.PtySession`` — this test fails
fast, in one place, instead of that fan-out.
"""

from __future__ import annotations

import agent_takkub.orchestrator as orch_mod
from agent_takkub import pty_session, spawn_engine
from agent_takkub.lead_inbox import LeadInboxMixin
from agent_takkub.pipeline_executor import PipelineMixin


def test_pty_session_reexported():
    assert orch_mod.PtySession is pty_session.PtySession


def test_spawn_engine_symbols_reexported():
    assert orch_mod.PaneRegistry is spawn_engine.PaneRegistry
    assert orch_mod.SpawnEngineMixin is spawn_engine.SpawnEngineMixin


def test_mixins_reexported():
    assert orch_mod.LeadInboxMixin is LeadInboxMixin
    assert orch_mod.PipelineMixin is PipelineMixin


def test_lead_context_symbols_reexported():
    assert hasattr(orch_mod, "REPO_ROOT")
    assert hasattr(orch_mod, "render_lead_settings")
