"""#690: non-claude teammates (codex / gemini-agy / opencode — and kimi/cursor,
which share the same `agents_md_file` branch) must see the project memory
(#33/#687) and their role learned notes, as claude teammates always have.

Drives the REAL `Orchestrator.spawn()` / `assign()` with only the PTY launch,
provider lookup and memory resolvers pinned — same scaffolding as
test_spawn_generic_argv_matches_plan.py (#621 M3), which this extends.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

# Imported at module top ON PURPOSE: conftest only redirects role_memory's
# ROLE_MEMORY_DIR when the module is already imported (force=False) — a lazy
# first import inside spawn() would write into this checkout's real runtime/.
from agent_takkub import memory_prompt, role_memory  # noqa: F401
from agent_takkub.orchestrator import Orchestrator

PROJECT = "memory690test"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


def _spec(name: str):
    from agent_takkub.provider_spec import PROVIDER_REGISTRY

    return PROVIDER_REGISTRY[name]


def _orch(monkeypatch) -> Orchestrator:
    monkeypatch.setattr(Orchestrator, "_resolve_project", staticmethod(lambda p: p or PROJECT))
    o = Orchestrator()
    o.shutdown_timers()
    return o


def _pane(role: str) -> MagicMock:
    pane = MagicMock()
    pane.role = MagicMock()
    pane.role.name = role
    pane.session = None
    pane.state = "empty"
    pane._transcript_path = None
    pane.attach_session = MagicMock()
    return pane


def _run(
    qapp,
    monkeypatch,
    tmp_path,
    *,
    provider: str,
    role: str = "backend",
    agents_md: tuple[bool, str] = (True, "written"),
    via_assign: bool = False,
):
    """Spawn `role` on `provider`; return (extra passed to ensure_agents_md,
    env handed to the PTY, delivered paste text or None, logged events)."""
    from agent_takkub import shared_dev_tools as sdt

    orch = _orch(monkeypatch)
    orch._panes_by_project[PROJECT] = {role: _pane(role)}
    monkeypatch.setattr(sdt, "SHARED_MCP_FILE", tmp_path / "shared-mcp.json")

    mem_md = tmp_path / "central" / "MEMORY.md"
    mem_md.parent.mkdir(parents=True, exist_ok=True)
    mem_md.write_text("- [rule](rule.md)\n", encoding="utf-8")
    role_file = tmp_path / "role-memory" / PROJECT / f"{role}.md"
    role_file.parent.mkdir(parents=True, exist_ok=True)
    role_file.write_text("# notes\n", encoding="utf-8")

    captured: dict = {}

    def fake_ensure(cwd, extra=""):
        captured["extra"] = extra
        return agents_md

    events: list[tuple[str, dict]] = []
    pty_calls: list[dict] = []
    spec = _spec(provider)
    with (
        patch.object(orch, "_is_spawn_blocked", return_value=False),
        patch.object(orch, "_final_gate_clear", return_value=True),
        patch("agent_takkub.orchestrator.PtySession") as pty_cls,
        patch("agent_takkub.orchestrator.QTimer.singleShot"),
        patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
        patch("agent_takkub.spawn_engine.sys.platform", "win32"),
        patch("agent_takkub.provider_config.effective_provider_for", return_value=spec.name),
        patch("agent_takkub.codex_helper.find_codex_executable", return_value="codex"),
        patch("agent_takkub.codex_agents_md.ensure_agents_md", side_effect=fake_ensure),
        patch("agent_takkub.spawn_engine._cwd_within_project", return_value=True),
        patch("agent_takkub.orchestrator.inject_user_profile_env"),
        patch("agent_takkub.provider_models.model_for", return_value=None),
        patch("agent_takkub.mcp_bridge.mcp_argv_for_provider", return_value=[]),
        patch("agent_takkub.mcp_bridge.describe_mcp_handshake", return_value={}),
        patch("agent_takkub.task_ledger.create_assignment", return_value=None),
        patch("agent_takkub.spawn_engine._resolve_project_memory", return_value=mem_md),
        patch("agent_takkub.role_memory.ensure_role_memory", return_value=role_file),
        # spawn() resolves _log_event from orchestrator's live namespace
        # (`_from_orch`), not spawn_engine's module global.
        patch(
            "agent_takkub.orchestrator._log_event",
            side_effect=lambda ev, **kw: events.append((ev, kw)),
        ),
        patch.object(orch, "_send_when_ready") as send,
    ):
        pty = MagicMock()
        pty.spawn.side_effect = lambda **kw: pty_calls.append(kw)
        pty_cls.return_value = pty
        if via_assign:
            ok, msg = orch.assign(role, cwd=str(tmp_path), task="do it", project=PROJECT)
        else:
            ok, msg = orch.spawn(role, project=PROJECT, cwd=str(tmp_path))

    assert ok is True, msg
    assert pty_calls, f"{provider}: PtySession.spawn was not called ({msg})"
    paste = send.call_args.args[1] if (via_assign and send.called) else None
    return (
        captured.get("extra", ""),
        pty_calls[0].get("env") or {},
        paste,
        events,
        mem_md,
        role_file,
    )


# ── the three providers the user asked for ────────────────────────────────────


@pytest.mark.parametrize("provider", ["codex", "gemini", "opencode"])
def test_agents_md_carries_project_memory_and_role_rule(qapp, monkeypatch, tmp_path, provider):
    if _spec(provider).context_strategy != "agents_md_file":
        pytest.skip(f"{provider} no longer uses AGENTS.md")
    extra, env, _paste, _ev, mem_md, role_file = _run(
        qapp, monkeypatch, tmp_path, provider=provider
    )

    assert "Project memory" in extra
    assert str(mem_md) in extra
    # role part names the DIRECTORY + the env rule, never one role's file —
    # AGENTS.md is shared per cwd (see memory_prompt docstring)
    assert "learned notes" in extra
    assert str(role_file.parent) in extra
    assert "TAKKUB_BASE_ROLE" in extra
    assert str(role_file) not in extra
    # ...and the env the rule points at is actually set for this pane
    assert env.get("TAKKUB_BASE_ROLE") == "backend"


def test_shard_pane_gets_base_role_not_shard_name(qapp, monkeypatch, tmp_path):
    """`qa#1` must look for qa.md — TAKKUB_ROLE alone would be `qa#1`."""
    _extra, env, *_ = _run(qapp, monkeypatch, tmp_path, provider="codex", role="qa#1")
    assert env.get("TAKKUB_ROLE") == "qa#1"
    assert env.get("TAKKUB_BASE_ROLE") == "qa"


# ── user-owned AGENTS.md: never silent ────────────────────────────────────────


def test_user_owned_agents_md_carries_memory_through_the_paste(qapp, monkeypatch, tmp_path):
    _extra, _env, paste, events, mem_md, role_file = _run(
        qapp,
        monkeypatch,
        tmp_path,
        provider="codex",
        agents_md=(False, "user-owned"),
        via_assign=True,
    )
    assert paste is not None
    assert "#690" in paste
    assert str(mem_md) in paste
    assert str(role_file) in paste  # per-pane paste: the concrete file is safe
    assert paste.index("#690") < paste.index("do it")
    fallbacks = [
        kw
        for ev, kw in events
        if ev == "provider_capability_fallback" and kw.get("capability") == "memory"
    ]
    assert fallbacks and fallbacks[0]["fallback"] == "initial_task_paste"


def test_user_owned_keeps_the_language_line_first(qapp, monkeypatch, tmp_path):
    """#621 M3's contract: the language directive stays the paste's first line."""
    _extra, _env, paste, *_ = _run(
        qapp,
        monkeypatch,
        tmp_path,
        provider="codex",
        agents_md=(False, "user-owned"),
        via_assign=True,
    )
    from agent_takkub.response_language import prompt_directive

    if prompt_directive():
        assert paste.startswith("[ภาษาที่ตอบ (#621)]")
        assert paste.index("ภาษาที่ตอบ") < paste.index("#690")


def test_managed_agents_md_does_not_duplicate_memory_in_the_paste(qapp, monkeypatch, tmp_path):
    _extra, _env, paste, *_ = _run(qapp, monkeypatch, tmp_path, provider="codex", via_assign=True)
    assert paste is None or "#690" not in paste


# ── the text itself ───────────────────────────────────────────────────────────


def test_project_block_is_what_claude_teammates_always_got():
    block = memory_prompt.project_memory_block("C:/x/MEMORY.md")
    assert "📋 Project memory (Lead's constraint registry)" in block
    assert 'Read("C:/x/MEMORY.md")' in block


def test_paste_note_is_empty_when_there_is_nothing_to_point_at():
    assert memory_prompt.paste_memory_note(None, None) == ""
    note = memory_prompt.paste_memory_note(None, "C:/r/qa.md")
    assert "C:/r/qa.md" in note and "project memory" not in note
