"""#817: a role that ran claude and was then switched to codex kept the claude
session uuid; done() paired it with provider=codex, and the next assign spawned
`codex resume <claude-uuid>` → "No saved session found" → exit 1 → respawn
crash-loop. Auto-resume must only resume a session the provider can open."""

from __future__ import annotations

from agent_takkub import codex_helper, spawn_engine
from agent_takkub import orchestrator as orch_mod
from agent_takkub.spawn_engine import _resume_uuid_matches_provider_cwd

from .test_opencode_provider import TEST_PROJECT, qapp  # noqa: F401 — qapp is a fixture
from .test_opencode_provider import TestOpencodeSpawnThroughGenericBranch as _Harness

CLAUDE_UUID = "91f6367e-2154-4af8-98ad-2d607e15470a"


def test_codex_store_rejects_another_providers_uuid(tmp_path, monkeypatch):
    monkeypatch.setattr(
        codex_helper, "codex_sessions_root", lambda project="": tmp_path / "sessions"
    )
    monkeypatch.setattr(
        codex_helper, "codex_archived_sessions_root", lambda project="": tmp_path / "arch"
    )
    assert not _resume_uuid_matches_provider_cwd(
        "ai-vdo", "codex", CLAUDE_UUID, str(tmp_path), "backend"
    )


def test_missing_session_spawns_fresh_and_forgets_it(qapp, monkeypatch, tmp_path):  # noqa: F811
    """Generic (non-claude) spawn branch with the store answering "not found"."""
    events = []
    monkeypatch.setattr(spawn_engine, "_resume_uuid_matches_provider_cwd", lambda *a, **k: False)
    monkeypatch.setattr(orch_mod, "_log_event", lambda ev, **kw: events.append((ev, kw)))
    spawn_kw, _, orch = _Harness._spawn_and_capture(
        None, qapp, monkeypatch, tmp_path, prior_session_id=CLAUDE_UUID
    )
    key = f"{TEST_PROJECT}::opencode"
    assert spawn_kw["argv"] == ["opencode", "--auto"]  # no `--session <stale>`
    assert (
        "auto_resume_session_missing",
        {
            "role": "opencode",
            "project": TEST_PROJECT,
            "provider": "opencode",
            "session_uuid": CLAUDE_UUID[:12],
        },
    ) in events
    assert orch._pane_state[key].session_uuid is None
    assert key not in orch._recent_exits
    assert key not in getattr(orch, "_last_session_uuid", {})
