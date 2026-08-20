"""Tests for W3 (resume button + session picker):

* engine: `spawn(resume_uuid=...)` validates the uuid actually belongs to
  the target cwd (`_resume_uuid_matches_cwd`) before using `--resume`.
* `notify.list_recent_lead_sessions` scans the JSONL store for a project's
  cwd, newest first, skipping corrupt/empty files silently.
* `api.lead_sessions` (view-safe) / `api.resume_lead` (control-mode).
* HTTP route gating for `/api/lead/sessions` (GET, view-safe) and
  `/api/lead/resume` (POST, control-mode only).
"""

from __future__ import annotations

import json
import pathlib
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import config as _config
from agent_takkub import orchestrator as orch_mod
from agent_takkub import user_profile
from agent_takkub.orchestrator import Orchestrator, _exit_key
from agent_takkub.remote import api, http_server
from agent_takkub.remote import notify as notify_mod
from agent_takkub.remote.config import RemoteConfig
from agent_takkub.roles import LEAD

_PROJECT = "default"


def _encode_cwd(path: pathlib.Path) -> str:
    """Build the encoded directory name Claude Code would have created for
    `path`, so tests can plant fixture JSONL files findable via their real
    cwd. Delegates to the canonical `token_meter.encode_path_for_claude`
    (C1) rather than reimplementing the mapping — a hand-rolled partial
    encoder here (e.g. one that only rewrites path separators) would silently
    diverge from what the code under test actually does for any cwd
    containing '_', '.', or a space."""
    from agent_takkub.token_meter import encode_path_for_claude

    return encode_path_for_claude(path)


@pytest.fixture
def hyphen_free_root():
    """`decode_project_dir`'s dash-based encoding is inherently lossy for any
    ancestor directory whose real name contains a literal hyphen (it can't
    tell "was a path separator" from "was already a dash") — and pytest's own
    `tmp_path` always nests under hyphenated dirs (`pytest-of-<user>/
    pytest-<N>/...`), which breaks the encode/decode round-trip these tests
    rely on. Use a dedicated hyphen-free temp root instead."""
    root = pathlib.Path(tempfile.mkdtemp(prefix="w3resume_"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ─────────────────────────────────────────────────────────────
# Engine: _resume_uuid_matches_cwd + spawn(resume_uuid=...)
# ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture
def tmp_env(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    cockpit = tmp_path / "cockpit"
    cockpit.mkdir(parents=True, exist_ok=True)
    (cockpit / "CLAUDE.md").write_text("# Lead\n", encoding="utf-8")
    monkeypatch.setattr(_config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(_config, "REPO_ROOT", cockpit)
    monkeypatch.setattr(orch_mod, "REPO_ROOT", cockpit)
    monkeypatch.setattr(orch_mod, "find_claude_executable", lambda: "claude")
    return tmp_path


@pytest.fixture
def orch(qapp: QCoreApplication, tmp_env: pathlib.Path) -> Orchestrator:
    o = Orchestrator()
    o._idle_watchdog.stop()
    return o


def _spawn_capture(
    orch: Orchestrator, role_name: str, cwd: str, **kwargs
) -> tuple[list[str], tuple]:
    fake_pane = MagicMock()
    fake_pane.session = None
    fake_pane.state = "empty"
    fake_pane.attach_session = MagicMock()
    fake_pane._transcript_path = None
    orch._panes_by_project.setdefault(_PROJECT, {})[role_name] = fake_pane

    captured: list[list[str]] = []
    fake_session = MagicMock()
    fake_session.processExited = MagicMock()
    fake_session.processExited.connect = MagicMock()

    with patch.object(orch_mod.PtySession, "__new__", return_value=fake_session):
        with patch.object(
            fake_session,
            "spawn",
            side_effect=lambda argv, cwd, env, **kw: captured.append(list(argv)),
        ):
            result = orch.spawn(role_name, cwd=cwd, project=_PROJECT, **kwargs)

    return (captured[0] if captured else []), result


class TestSurvivesLossyPathChars:
    """C1 regression: a cwd containing '-', '_', '.', or a space (e.g. this
    very project, `agent-takkub`) must resolve correctly now that lookups
    encode forward (`token_meter.encode_path_for_claude`) instead of
    scanning every project dir and reverse-decoding names to compare.
    `decode_project_dir()` maps every non-alnum char to '-', so it can't
    tell "was a path separator" from "was already one of those chars" apart
    — the old scan-and-decode approach silently found zero sessions for any
    project whose cwd contained one. Uses ordinary `tmp_path` (itself nested
    under a hyphenated pytest-internal dir) rather than `hyphen_free_root`,
    proving the fix no longer needs a hyphen-free root at all."""

    @staticmethod
    def _plant_real_encoded(config_dir: pathlib.Path, cwd: pathlib.Path, uuid: str) -> None:
        from agent_takkub.token_meter import encode_path_for_claude

        proj_dir = config_dir / "projects" / encode_path_for_claude(cwd)
        proj_dir.mkdir(parents=True)
        rec = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        }
        (proj_dir / f"{uuid}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")

    @pytest.mark.parametrize(
        "subdir_name",
        ["agent-takkub", "my_app_web", "release.candidate", "my project name"],
        ids=["hyphen", "underscore", "dot", "space"],
    )
    def test_notify_finds_session_for_lossy_cwd(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, subdir_name: str
    ) -> None:
        config_dir = tmp_path / "claude_config"
        monkeypatch.setattr(notify_mod, "config_dir_for", lambda project: config_dir)
        cwd = tmp_path / subdir_name
        cwd.mkdir()
        monkeypatch.setattr(_config, "lead_cwd", lambda project=None: str(cwd))
        self._plant_real_encoded(config_dir, cwd, "sess-1")

        out = notify_mod.list_recent_lead_sessions("default")
        assert [s["uuid"] for s in out] == ["sess-1"]

    @pytest.mark.parametrize(
        "subdir_name",
        ["agent-takkub", "my_app_web", "release.candidate", "my project name"],
        ids=["hyphen", "underscore", "dot", "space"],
    )
    def test_resume_uuid_matches_cwd_for_lossy_cwd(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, subdir_name: str
    ) -> None:
        from agent_takkub.spawn_engine import _resume_uuid_matches_cwd

        config_dir = tmp_path / "claude_config"
        monkeypatch.setattr(user_profile, "_DEFAULT_CONFIG_DIR", config_dir)
        cwd = tmp_path / subdir_name
        cwd.mkdir()
        self._plant_real_encoded(config_dir, cwd, "sess-1")

        assert _resume_uuid_matches_cwd("default", "sess-1", str(cwd)) is True


class TestClaudeProjectDirectoryName:
    """#321: deterministic Claude directory names replace lossy cwd decoding."""

    def test_name_preserves_mixed_lossy_characters_and_version_gate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub.pane_env import (
            claude_project_dir_name,
            inject_claude_project_dir_name_env,
            supports_claude_project_dir_name,
        )

        project_ns = "project-name_with.dot"
        assert claude_project_dir_name(project_ns) == "takkub-project-project-name_with.dot"
        assert supports_claude_project_dir_name("2.1.233") is False
        assert supports_claude_project_dir_name("2.1.234") is True
        completed = MagicMock(returncode=0, stdout="2.1.234 (Claude Code)")
        monkeypatch.setattr(
            "agent_takkub.pane_env.subprocess.run", lambda *_args, **_kwargs: completed
        )
        env: dict[str, str] = {}
        inject_claude_project_dir_name_env(env, project_ns, "claude")
        assert env["CLAUDE_CODE_PROJECT_DIR_NAME"] == "takkub-project-project-name_with.dot"

    def test_new_directory_wins_and_legacy_directory_stays_resolvable(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub.pane_env import claude_project_dir_name
        from agent_takkub.spawn_engine import _resume_uuid_matches_cwd
        from agent_takkub.token_meter import find_session_by_uuid

        project_ns = "project-name_with.dot"
        config_dir = tmp_path / "claude_config"
        cwd = tmp_path / project_ns
        cwd.mkdir()
        monkeypatch.setattr(notify_mod, "config_dir_for", lambda _project: config_dir)
        monkeypatch.setattr(user_profile, "_DEFAULT_CONFIG_DIR", config_dir)
        monkeypatch.setattr(_config, "lead_cwd", lambda _project=None: str(cwd))

        legacy = config_dir / "projects" / _encode_cwd(cwd)
        named = config_dir / "projects" / claude_project_dir_name(project_ns)
        legacy.mkdir(parents=True)
        named.mkdir(parents=True)
        record = (
            json.dumps({"type": "user", "message": {"role": "user", "content": "resume me"}}) + "\n"
        )
        (legacy / "legacy.jsonl").write_text(record, encoding="utf-8")
        (legacy / "same.jsonl").write_text(record, encoding="utf-8")
        preferred = named / "same.jsonl"
        preferred.write_text(record, encoding="utf-8")
        (named / "new.jsonl").write_text(record, encoding="utf-8")

        assert find_session_by_uuid(cwd, "same", config_dir, project_ns=project_ns) == preferred
        assert _resume_uuid_matches_cwd(project_ns, "new", str(cwd)) is True
        assert _resume_uuid_matches_cwd(project_ns, "legacy", str(cwd)) is True
        assert notify_mod._resolve_claude_jsonl_path(project_ns, "new") == named / "new.jsonl"
        assert {entry["uuid"] for entry in notify_mod.list_recent_lead_sessions(project_ns)} == {
            "legacy",
            "same",
            "new",
        }


class TestResumeUuidMatchesCwd:
    def test_matches_when_encoded_dir_decodes_to_cwd(
        self, hyphen_free_root: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub.spawn_engine import _resume_uuid_matches_cwd

        config_dir = hyphen_free_root / "claude_config"
        monkeypatch.setattr(user_profile, "_DEFAULT_CONFIG_DIR", config_dir)
        cwd = hyphen_free_root / "proj"
        cwd.mkdir()
        proj_dir = config_dir / "projects" / _encode_cwd(cwd)
        proj_dir.mkdir(parents=True)
        (proj_dir / "abc123.jsonl").write_text("{}\n", encoding="utf-8")

        assert _resume_uuid_matches_cwd("default", "abc123", str(cwd)) is True

    def test_false_when_cwd_differs(
        self, hyphen_free_root: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub.spawn_engine import _resume_uuid_matches_cwd

        config_dir = hyphen_free_root / "claude_config"
        monkeypatch.setattr(user_profile, "_DEFAULT_CONFIG_DIR", config_dir)
        cwd_a = hyphen_free_root / "proj_a"
        cwd_a.mkdir()
        cwd_b = hyphen_free_root / "proj_b"
        cwd_b.mkdir()
        proj_dir = config_dir / "projects" / _encode_cwd(cwd_a)
        proj_dir.mkdir(parents=True)
        (proj_dir / "abc123.jsonl").write_text("{}\n", encoding="utf-8")

        assert _resume_uuid_matches_cwd("default", "abc123", str(cwd_b)) is False

    def test_false_when_uuid_not_found(
        self, hyphen_free_root: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub.spawn_engine import _resume_uuid_matches_cwd

        config_dir = hyphen_free_root / "claude_config"
        monkeypatch.setattr(user_profile, "_DEFAULT_CONFIG_DIR", config_dir)
        (config_dir / "projects").mkdir(parents=True)

        assert _resume_uuid_matches_cwd("default", "ghost_uuid", str(hyphen_free_root)) is False

    def test_false_for_path_traversal_uuid_even_when_target_jsonl_exists(
        self, hyphen_free_root: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F1 regression: `../<other-encoded-dir>/<real-uuid>` must be
        rejected before it ever reaches the filesystem, even though the
        target `.jsonl` genuinely exists under a *different* project's
        encoded dir (proving the old `Path.is_file()` join would otherwise
        have resolved outside `cwd`'s own encoded dir and returned True)."""
        from agent_takkub.spawn_engine import _resume_uuid_matches_cwd

        config_dir = hyphen_free_root / "claude_config"
        monkeypatch.setattr(user_profile, "_DEFAULT_CONFIG_DIR", config_dir)
        cwd = hyphen_free_root / "proj_a"
        cwd.mkdir()
        other = hyphen_free_root / "proj_b"
        other.mkdir()
        other_dir = config_dir / "projects" / _encode_cwd(other)
        other_dir.mkdir(parents=True)
        (other_dir / "real-uuid.jsonl").write_text("{}\n", encoding="utf-8")

        traversal_uuid = f"../{_encode_cwd(other)}/real-uuid"

        assert _resume_uuid_matches_cwd("default", traversal_uuid, str(cwd)) is False

    @pytest.mark.parametrize(
        "bad_uuid",
        ["../evil", "a/b", "a\\b", "..", "foo/../bar", "trailing/"],
    )
    def test_false_for_any_uuid_with_path_separators_or_dotdot(
        self, hyphen_free_root: pathlib.Path, monkeypatch: pytest.MonkeyPatch, bad_uuid: str
    ) -> None:
        from agent_takkub.spawn_engine import _resume_uuid_matches_cwd

        config_dir = hyphen_free_root / "claude_config"
        monkeypatch.setattr(user_profile, "_DEFAULT_CONFIG_DIR", config_dir)
        (config_dir / "projects").mkdir(parents=True)

        assert _resume_uuid_matches_cwd("default", bad_uuid, str(hyphen_free_root)) is False

    def test_normal_uuid_still_passes(
        self, hyphen_free_root: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard: the new charset check must not reject ordinary
        uuids (hex + hyphens, or claude's underscore-suffixed shard ids)."""
        from agent_takkub.spawn_engine import _resume_uuid_matches_cwd

        config_dir = hyphen_free_root / "claude_config"
        monkeypatch.setattr(user_profile, "_DEFAULT_CONFIG_DIR", config_dir)
        cwd = hyphen_free_root / "proj"
        cwd.mkdir()
        proj_dir = config_dir / "projects" / _encode_cwd(cwd)
        proj_dir.mkdir(parents=True)
        (proj_dir / "abc-123_def.jsonl").write_text("{}\n", encoding="utf-8")

        assert _resume_uuid_matches_cwd("default", "abc-123_def", str(cwd)) is True


class TestResumeUuidMatchesProviderCwd:
    def test_gemini_uses_provider_core_resolver(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        from agent_takkub import gemini_helper
        from agent_takkub.spawn_engine import _resume_uuid_matches_provider_cwd

        session_uuid = "89716048-a562-4bc4-80e4-a6f97a2830f3"
        seen: dict[str, str | None] = {}

        def _resolve(cwd: str, uuid: str | None) -> pathlib.Path:
            seen.update(cwd=cwd, uuid=uuid)
            return tmp_path / "session.jsonl"

        monkeypatch.setattr(gemini_helper, "resolve_gemini_jsonl_for_cwd", _resolve)

        assert _resume_uuid_matches_provider_cwd("default", "gemini", session_uuid, str(tmp_path))
        assert seen == {"cwd": str(tmp_path), "uuid": session_uuid}

    def test_codex_uses_provider_core_resolver(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        from agent_takkub import codex_helper
        from agent_takkub.spawn_engine import _resume_uuid_matches_provider_cwd

        seen: dict[str, str] = {}

        def _resolve(cwd: str, uuid: str) -> pathlib.Path:
            seen.update(cwd=cwd, uuid=uuid)
            return tmp_path / "rollout.jsonl"

        monkeypatch.setattr(codex_helper, "resolve_codex_jsonl_for_cwd", _resolve)

        assert _resume_uuid_matches_provider_cwd("default", "codex", "codex-uuid", str(tmp_path))
        assert seen == {"cwd": str(tmp_path), "uuid": "codex-uuid"}


class TestSpawnResumeUuid:
    # role_name deliberately not "lead" — a lead spawn also renders
    # runtime/lead-context.md via lead_context.py, which reads its own
    # module-level RUNTIME_DIR (not patched by tmp_env here) and would write
    # to the real repo path. The resume_uuid mechanism itself is role-agnostic
    # (validated at the engine layer regardless of role); `api.resume_lead`'s
    # own tests cover the Lead-specific call shape with a mocked orchestrator.
    def test_valid_resume_uuid_uses_resume_flag(
        self, orch: Orchestrator, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cwd = tmp_path / "proj"
        cwd.mkdir()
        monkeypatch.setattr(
            "agent_takkub.spawn_engine._resume_uuid_matches_cwd", lambda p, u, c: True
        )
        argv, result = _spawn_capture(orch, "backend", str(cwd), resume_uuid="picked-uuid")
        assert result[0] is True
        assert "--resume" in argv
        assert argv[argv.index("--resume") + 1] == "picked-uuid"
        assert "--session-id" not in argv
        key = _exit_key(_PROJECT, "backend")
        assert orch._pane_state[key].session_uuid == "picked-uuid"
        assert orch._pane_state[key].session_uuid_cwd == str(cwd)

    def test_invalid_resume_uuid_rejected_before_spawn(
        self, orch: Orchestrator, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cwd = tmp_path / "proj"
        cwd.mkdir()
        monkeypatch.setattr(
            "agent_takkub.spawn_engine._resume_uuid_matches_cwd", lambda p, u, c: False
        )
        argv, result = _spawn_capture(orch, "backend", str(cwd), resume_uuid="forged-uuid")
        assert result[0] is False
        assert argv == []  # native session.spawn() never reached
        # LOW (codex full-system review 2026-07-11): a rejected explicit
        # resume must never leave a pane capability token registered — the
        # engine now validates resume_uuid before minting one at all.
        assert not orch._pane_tokens

    def test_resume_uuid_bypasses_5min_window_check(
        self, orch: Orchestrator, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No prior exit recorded at all (never a same-run auto-resume
        candidate) — an explicit resume_uuid must still work."""
        cwd = tmp_path / "proj"
        cwd.mkdir()
        monkeypatch.setattr(
            "agent_takkub.spawn_engine._resume_uuid_matches_cwd", lambda p, u, c: True
        )
        assert _exit_key(_PROJECT, "backend") not in orch._recent_exits
        argv, result = _spawn_capture(orch, "backend", str(cwd), resume_uuid="old-session")
        assert result[0] is True
        assert argv[argv.index("--resume") + 1] == "old-session"


# ─────────────────────────────────────────────────────────────
# notify.list_recent_lead_sessions
# ─────────────────────────────────────────────────────────────


class TestListRecentLeadSessions:
    def _setup(
        self, root: pathlib.Path, monkeypatch: pytest.MonkeyPatch, cwd: pathlib.Path
    ) -> pathlib.Path:
        config_dir = root / "claude_config"
        monkeypatch.setattr(notify_mod, "config_dir_for", lambda project: config_dir)
        monkeypatch.setattr(_config, "lead_cwd", lambda project=None: str(cwd))
        return config_dir

    def test_empty_store_returns_empty_list(
        self, hyphen_free_root: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cwd = hyphen_free_root / "proj"
        cwd.mkdir()
        self._setup(hyphen_free_root, monkeypatch, cwd)
        assert notify_mod.list_recent_lead_sessions("default") == []

    def test_no_lead_cwd_returns_empty_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_config, "lead_cwd", lambda project=None: None)
        assert notify_mod.list_recent_lead_sessions("default") == []

    def test_lists_sessions_newest_first_with_preview(
        self, hyphen_free_root: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cwd = hyphen_free_root / "proj"
        cwd.mkdir()
        config_dir = self._setup(hyphen_free_root, monkeypatch, cwd)
        proj_dir = config_dir / "projects" / _encode_cwd(cwd)
        proj_dir.mkdir(parents=True)

        old = proj_dir / "old-uuid.jsonl"
        old.write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "first task"}})
            + "\n",
            encoding="utf-8",
        )
        new = proj_dir / "new-uuid.jsonl"
        new.write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "second task"}})
            + "\n",
            encoding="utf-8",
        )
        old_time = time.time() - 100
        new_time = time.time()
        import os

        os.utime(old, (old_time, old_time))
        os.utime(new, (new_time, new_time))

        sessions = notify_mod.list_recent_lead_sessions("default")
        assert [s["uuid"] for s in sessions] == ["new-uuid", "old-uuid"]
        assert sessions[0]["preview"] == "second task"
        assert sessions[1]["preview"] == "first task"

    def test_skips_directories_for_a_different_cwd(
        self, hyphen_free_root: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cwd = hyphen_free_root / "proj"
        cwd.mkdir()
        other_cwd = hyphen_free_root / "other_proj"
        other_cwd.mkdir()
        config_dir = self._setup(hyphen_free_root, monkeypatch, cwd)
        other_dir = config_dir / "projects" / _encode_cwd(other_cwd)
        other_dir.mkdir(parents=True)
        (other_dir / "unrelated-uuid.jsonl").write_text("{}\n", encoding="utf-8")

        assert notify_mod.list_recent_lead_sessions("default") == []

    def test_corrupt_jsonl_still_listed_with_empty_preview(
        self, hyphen_free_root: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cwd = hyphen_free_root / "proj"
        cwd.mkdir()
        config_dir = self._setup(hyphen_free_root, monkeypatch, cwd)
        proj_dir = config_dir / "projects" / _encode_cwd(cwd)
        proj_dir.mkdir(parents=True)
        (proj_dir / "corrupt-uuid.jsonl").write_text("not json at all\n", encoding="utf-8")

        sessions = notify_mod.list_recent_lead_sessions("default")
        assert len(sessions) == 1
        assert sessions[0]["uuid"] == "corrupt-uuid"
        assert sessions[0]["preview"] == ""

    def test_limit_is_capped(
        self, hyphen_free_root: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cwd = hyphen_free_root / "proj"
        cwd.mkdir()
        config_dir = self._setup(hyphen_free_root, monkeypatch, cwd)
        proj_dir = config_dir / "projects" / _encode_cwd(cwd)
        proj_dir.mkdir(parents=True)
        for i in range(5):
            (proj_dir / f"uuid-{i}.jsonl").write_text("{}\n", encoding="utf-8")

        assert len(notify_mod.list_recent_lead_sessions("default", limit=2)) == 2

    def test_filters_out_teammate_sessions(
        self, hyphen_free_root: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mirrors the chatlog_scanner regression: teammate panes share the
        Lead's cwd, so a "[ROLE: backend] ..." first line must be filtered
        out of the mobile picker too."""
        import os

        cwd = hyphen_free_root / "proj"
        cwd.mkdir()
        config_dir = self._setup(hyphen_free_root, monkeypatch, cwd)
        proj_dir = config_dir / "projects" / _encode_cwd(cwd)
        proj_dir.mkdir(parents=True)

        teammate = proj_dir / "teammate-uuid.jsonl"
        teammate.write_text(
            json.dumps(
                {
                    "type": "user",
                    "message": {"role": "user", "content": "[ROLE: backend] เพิ่ม endpoint"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        lead = proj_dir / "lead-uuid.jsonl"
        lead.write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "คุยกับ Lead ปกติ"}})
            + "\n",
            encoding="utf-8",
        )
        now = time.time()
        os.utime(teammate, (now, now))
        os.utime(lead, (now - 100, now - 100))

        sessions = notify_mod.list_recent_lead_sessions("default")
        assert [s["uuid"] for s in sessions] == ["lead-uuid"]

    def test_filters_out_goal_scoped_teammate_sessions(
        self, hyphen_free_root: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mirrors the chatlog_scanner regression: when a session goal is
        set, `assign()` prepends `[SESSION GOAL ...]` ahead of `[ROLE:` on
        the teammate's first user line, so the plain `[ROLE:` check alone
        lets these leak into the mobile picker too."""
        import os

        cwd = hyphen_free_root / "proj"
        cwd.mkdir()
        config_dir = self._setup(hyphen_free_root, monkeypatch, cwd)
        proj_dir = config_dir / "projects" / _encode_cwd(cwd)
        proj_dir.mkdir(parents=True)

        teammate = proj_dir / "goal-teammate-uuid.jsonl"
        teammate.write_text(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "[SESSION GOAL — ทุก role ในงานนี้ยึดเป้าหมายเดียวกัน]\n"
                        "ship RBAC v1\n\n[ROLE: backend] add POST /roles",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        lead = proj_dir / "lead-uuid.jsonl"
        lead.write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "คุยกับ Lead ปกติ"}})
            + "\n",
            encoding="utf-8",
        )
        now = time.time()
        os.utime(teammate, (now, now))
        os.utime(lead, (now - 100, now - 100))

        sessions = notify_mod.list_recent_lead_sessions("default")
        assert [s["uuid"] for s in sessions] == ["lead-uuid"]

    def test_limit_counted_after_filtering_teammates(
        self, hyphen_free_root: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`limit` caps the number of Lead sessions returned, not the number
        of jsonls scanned before filtering."""
        import os

        cwd = hyphen_free_root / "proj"
        cwd.mkdir()
        config_dir = self._setup(hyphen_free_root, monkeypatch, cwd)
        proj_dir = config_dir / "projects" / _encode_cwd(cwd)
        proj_dir.mkdir(parents=True)

        now = time.time()
        for i in range(5):
            f = proj_dir / f"teammate-{i}.jsonl"
            f.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "message": {"role": "user", "content": "[ROLE: qa] smoke test"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            os.utime(f, (now + i, now + i))
        for name, ts in (("lead-old", now - 200), ("lead-new", now - 100)):
            f = proj_dir / f"{name}.jsonl"
            f.write_text(
                json.dumps({"type": "user", "message": {"role": "user", "content": "งานปกติ"}})
                + "\n",
                encoding="utf-8",
            )
            os.utime(f, (ts, ts))

        sessions = notify_mod.list_recent_lead_sessions("default", limit=2)
        assert [s["uuid"] for s in sessions] == ["lead-new", "lead-old"]


# ─────────────────────────────────────────────────────────────
# api.lead_sessions / api.resume_lead
# ─────────────────────────────────────────────────────────────


class TestApiLeadSessions:
    def test_clamps_limit_and_forwards_to_notify(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict = {}

        def _fake(project_ns, limit, *, provider=None):
            seen["project_ns"] = project_ns
            seen["limit"] = limit
            seen["provider"] = provider
            return []

        monkeypatch.setattr(notify_mod, "list_recent_lead_sessions", _fake)
        result = api.lead_sessions(object(), "myproj", limit=999)
        assert seen["project_ns"] == "myproj"
        assert seen["limit"] == api._SESSIONS_MAX_LIMIT
        assert seen["provider"] == "claude"
        assert result == {
            "project": "myproj",
            "provider": "claude",
            "sessions": [],
            "lead_provider_note": None,
        }

    def test_bad_limit_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict = {}
        monkeypatch.setattr(
            notify_mod,
            "list_recent_lead_sessions",
            lambda project_ns, limit, **_kw: seen.setdefault("limit", limit) or [],
        )
        api.lead_sessions(object(), "myproj", limit="not-a-number")
        assert seen["limit"] == notify_mod._SESSION_LIST_DEFAULT_LIMIT


class TestApiResumeLead:
    def test_unknown_project_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_config, "list_project_names", lambda: [])
        with pytest.raises(api.RemoteApiError) as exc:
            api.resume_lead(object(), "ghost", "uuid-1")
        assert exc.value.status == 400

    def test_not_open_project_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_config, "list_project_names", lambda: ["proj"])
        monkeypatch.setattr(_config, "get_open_tabs", lambda: [])
        with pytest.raises(api.RemoteApiError) as exc:
            api.resume_lead(object(), "proj", "uuid-1")
        assert exc.value.status == 409

    def test_missing_session_uuid_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_config, "list_project_names", lambda: ["proj"])
        monkeypatch.setattr(_config, "get_open_tabs", lambda: ["proj"])
        with pytest.raises(api.RemoteApiError) as exc:
            api.resume_lead(object(), "proj", "")
        assert exc.value.status == 400

    def test_closes_and_respawns_lead_with_resume_uuid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_config, "list_project_names", lambda: ["proj"])
        monkeypatch.setattr(_config, "get_open_tabs", lambda: ["proj"])
        monkeypatch.setattr(_config, "lead_cwd", lambda project=None: "/proj/web")
        monkeypatch.setattr(
            "agent_takkub.spawn_engine._resume_uuid_matches_provider_cwd",
            lambda p, provider, u, c: True,
        )

        fake_orch = MagicMock()
        fake_orch.spawn.return_value = (True, "ok")
        result = api.resume_lead(fake_orch, "proj", "uuid-xyz")

        fake_orch.close.assert_called_once_with(
            LEAD.name, project="proj", force=True, reason="remote resume"
        )
        fake_orch.spawn.assert_called_once_with(
            LEAD.name, cwd="/proj/web", project="proj", resume_uuid="uuid-xyz"
        )
        assert result == {
            "ok": True,
            "project": "proj",
            "provider": "claude",
            "messages": [],
        }

    def test_spawn_failure_surfaces_as_409(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_config, "list_project_names", lambda: ["proj"])
        monkeypatch.setattr(_config, "get_open_tabs", lambda: ["proj"])
        monkeypatch.setattr(_config, "lead_cwd", lambda project=None: "/proj/web")
        monkeypatch.setattr(
            "agent_takkub.spawn_engine._resume_uuid_matches_provider_cwd",
            lambda p, provider, u, c: True,
        )

        fake_orch = MagicMock()
        fake_orch.spawn.return_value = (False, "resume_uuid does not match cwd for lead")
        with pytest.raises(api.RemoteApiError) as exc:
            api.resume_lead(fake_orch, "proj", "uuid-xyz")
        assert exc.value.status == 409

    def test_no_lead_cwd_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_config, "list_project_names", lambda: ["proj"])
        monkeypatch.setattr(_config, "get_open_tabs", lambda: ["proj"])
        monkeypatch.setattr(_config, "lead_cwd", lambda project=None: None)
        with pytest.raises(api.RemoteApiError) as exc:
            api.resume_lead(MagicMock(), "proj", "uuid-xyz")
        assert exc.value.status == 409

    def test_mismatched_uuid_rejected_before_close(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """C1: a uuid that doesn't belong to `cwd` must be rejected BEFORE
        `orch.close()` runs — never tear down the live Lead pane for a
        resume that's going to fail anyway."""
        monkeypatch.setattr(_config, "list_project_names", lambda: ["proj"])
        monkeypatch.setattr(_config, "get_open_tabs", lambda: ["proj"])
        monkeypatch.setattr(_config, "lead_cwd", lambda project=None: "/proj/web")
        monkeypatch.setattr(
            "agent_takkub.spawn_engine._resume_uuid_matches_provider_cwd",
            lambda p, provider, u, c: False,
        )

        fake_orch = MagicMock()
        with pytest.raises(api.RemoteApiError) as exc:
            api.resume_lead(fake_orch, "proj", "forged-uuid")
        assert exc.value.status == 409

    def test_unsupported_provider_rejected_before_uuid_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A provider without a verified store+flag is rejected cleanly."""
        monkeypatch.setattr(_config, "list_project_names", lambda: ["proj"])
        monkeypatch.setattr(_config, "get_open_tabs", lambda: ["proj"])
        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for",
            lambda role, project=None: "kimi",
        )
        fake_orch = MagicMock()
        with pytest.raises(api.RemoteApiError) as exc:
            api.resume_lead(fake_orch, "proj", "")  # empty uuid would 400 if reached
        assert exc.value.status == 409
        assert "resume unavailable" in exc.value.msg
        fake_orch.close.assert_not_called()
        fake_orch.spawn.assert_not_called()

    def test_opencode_lead_uses_provider_aware_validation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_config, "list_project_names", lambda: ["proj"])
        monkeypatch.setattr(_config, "get_open_tabs", lambda: ["proj"])
        monkeypatch.setattr(_config, "lead_cwd", lambda project=None: "/proj/web")
        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for",
            lambda role, project=None: "opencode",
        )
        seen: dict = {}

        def _matches(project, provider, uuid, cwd):
            seen.update(project=project, provider=provider, uuid=uuid, cwd=cwd)
            return True

        monkeypatch.setattr("agent_takkub.spawn_engine._resume_uuid_matches_provider_cwd", _matches)
        fake_orch = MagicMock()
        fake_orch.spawn.return_value = (True, "ok")

        assert api.resume_lead(fake_orch, "proj", "uuid-opencode") == {
            "ok": True,
            "project": "proj",
            "provider": "opencode",
            "messages": [],
        }
        assert seen == {
            "project": "proj",
            "provider": "opencode",
            "uuid": "uuid-opencode",
            "cwd": "/proj/web",
        }

    def test_codex_lead_uses_provider_aware_validation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_config, "list_project_names", lambda: ["proj"])
        monkeypatch.setattr(_config, "get_open_tabs", lambda: ["proj"])
        monkeypatch.setattr(_config, "lead_cwd", lambda project=None: "/proj/web")
        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for",
            lambda role, project=None: "codex",
        )
        seen: dict = {}

        def _matches(project, provider, uuid, cwd):
            seen.update(project=project, provider=provider, uuid=uuid, cwd=cwd)
            return True

        monkeypatch.setattr("agent_takkub.spawn_engine._resume_uuid_matches_provider_cwd", _matches)
        fake_orch = MagicMock()
        fake_orch.spawn.return_value = (True, "ok")

        assert api.resume_lead(fake_orch, "proj", "uuid-codex") == {
            "ok": True,
            "project": "proj",
            "provider": "codex",
            "messages": [],
        }
        assert seen == {
            "project": "proj",
            "provider": "codex",
            "uuid": "uuid-codex",
            "cwd": "/proj/web",
        }

    def test_gemini_lead_uses_provider_aware_validation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_config, "list_project_names", lambda: ["proj"])
        monkeypatch.setattr(_config, "get_open_tabs", lambda: ["proj"])
        monkeypatch.setattr(_config, "lead_cwd", lambda project=None: "/proj/web")
        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for",
            lambda role, project=None: "gemini",
        )
        seen: dict = {}

        def _matches(project, provider, uuid, cwd):
            seen.update(project=project, provider=provider, uuid=uuid, cwd=cwd)
            return True

        monkeypatch.setattr("agent_takkub.spawn_engine._resume_uuid_matches_provider_cwd", _matches)
        fake_orch = MagicMock()
        fake_orch.spawn.return_value = (True, "ok")

        assert api.resume_lead(fake_orch, "proj", "uuid-gemini") == {
            "ok": True,
            "project": "proj",
            "provider": "gemini",
            "messages": [],
        }
        assert seen == {
            "project": "proj",
            "provider": "gemini",
            "uuid": "uuid-gemini",
            "cwd": "/proj/web",
        }

    def test_resume_response_carries_selected_history_without_second_get(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_config, "list_project_names", lambda: ["proj"])
        monkeypatch.setattr(_config, "get_open_tabs", lambda: ["proj"])
        monkeypatch.setattr(_config, "lead_cwd", lambda project=None: "/proj/web")
        monkeypatch.setattr(
            "agent_takkub.spawn_engine._resume_uuid_matches_provider_cwd",
            lambda p, provider, u, c: True,
        )
        expected = [{"kind": "lead", "text": "restored reply"}]
        monkeypatch.setattr(notify_mod, "read_resume_session_messages", lambda *a: expected)
        fake_orch = MagicMock()
        fake_orch.spawn.return_value = (True, "ok")

        result = api.resume_lead(fake_orch, "proj", "uuid-history")

        assert result["messages"] == expected


# ─────────────────────────────────────────────────────────────
# HTTP route gating
# ─────────────────────────────────────────────────────────────


class _FakeOrch:
    _lead_token = "lead-tok"

    def _resolve_project(self, project):
        return "default"


def _pump_until(app: QCoreApplication, predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _run_pumped(fn):
    app = QCoreApplication.instance() or QCoreApplication([])
    result: dict = {}

    def _do() -> None:
        result["value"] = fn()

    t = threading.Thread(target=_do)
    t.start()
    assert _pump_until(app, lambda: not t.is_alive())
    t.join(timeout=1)
    return result["value"]


def _url(srv, path: str) -> str:
    return f"http://127.0.0.1:{srv.port}{path}"


def _get(url: str, headers: dict | None = None) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers or {}), timeout=5
        ) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _post(url: str, payload: dict, headers: dict | None = None) -> tuple[int, bytes]:
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=hdrs, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class TestLeadSessionsRoute:
    def test_works_in_view_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            api,
            "lead_sessions",
            lambda orch, project_ns, limit: {"project": project_ns, "sessions": []},
        )
        config = RemoteConfig(bind_port=0, secret_path="sek", token="tok", mode="view")
        srv = http_server.start_server(config, _FakeOrch())
        try:
            status, body = _run_pumped(
                lambda: _get(_url(srv, "/sek/api/lead/sessions"), {"Authorization": "Bearer tok"})
            )
        finally:
            srv.stop()
        assert status == 200
        assert json.loads(body) == {"project": "default", "sessions": []}

    def test_forwards_project_and_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict = {}

        def _fake(orch, project_ns, limit):
            seen["project_ns"] = project_ns
            seen["limit"] = limit
            return {"project": project_ns, "sessions": []}

        monkeypatch.setattr(api, "lead_sessions", _fake)
        config = RemoteConfig(bind_port=0, secret_path="sek", token="tok", mode="control")
        srv = http_server.start_server(config, _FakeOrch())
        try:
            _run_pumped(
                lambda: _get(
                    _url(srv, "/sek/api/lead/sessions?limit=5"), {"Authorization": "Bearer tok"}
                )
            )
        finally:
            srv.stop()
        assert seen["project_ns"] == "default"
        assert seen["limit"] == "5"


class TestLeadResumeRoute:
    def test_forbidden_in_view_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            api, "resume_lead", lambda orch, project, session_uuid: {"ok": True, "project": project}
        )
        config = RemoteConfig(bind_port=0, secret_path="sek", token="tok", mode="view")
        srv = http_server.start_server(config, _FakeOrch())
        try:
            status, body = _post(
                _url(srv, "/sek/api/lead/resume"),
                {"project": "proj", "session_uuid": "u1"},
                {"Authorization": "Bearer tok"},
            )
        finally:
            srv.stop()
        assert status == 403
        assert json.loads(body)["msg"] == "view mode: control is disabled"

    def test_control_mode_returns_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            api, "resume_lead", lambda orch, project, session_uuid: {"ok": True, "project": project}
        )
        config = RemoteConfig(bind_port=0, secret_path="sek", token="tok", mode="control")
        srv = http_server.start_server(config, _FakeOrch())
        try:
            status, body = _run_pumped(
                lambda: _post(
                    _url(srv, "/sek/api/lead/resume"),
                    {"project": "proj", "session_uuid": "u1"},
                    {"Authorization": "Bearer tok"},
                )
            )
        finally:
            srv.stop()
        assert status == 200
        assert json.loads(body) == {"ok": True, "project": "proj"}

    def test_bridge_error_surfaces_status_and_msg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_resume(orch, project, session_uuid):
            raise api.RemoteApiError(409, "resume failed")

        monkeypatch.setattr(api, "resume_lead", _fake_resume)
        config = RemoteConfig(bind_port=0, secret_path="sek", token="tok", mode="control")
        srv = http_server.start_server(config, _FakeOrch())
        try:
            status, body = _run_pumped(
                lambda: _post(
                    _url(srv, "/sek/api/lead/resume"),
                    {"project": "proj", "session_uuid": "u1"},
                    {"Authorization": "Bearer tok"},
                )
            )
        finally:
            srv.stop()
        assert status == 409
        assert json.loads(body) == {"ok": False, "msg": "resume failed"}
