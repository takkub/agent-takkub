"""Tests for the `takkub` CLI argument parsing (offline — orchestrator is mocked)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_takkub import cli


@pytest.fixture
def fake_request(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture the JSON payloads the CLI would send to the orchestrator."""
    sent: list[dict[str, Any]] = []

    def _fake(payload: dict[str, Any]) -> dict[str, Any]:
        sent.append(payload)
        return {"ok": True, "msg": "stubbed"}

    monkeypatch.setattr(cli, "_request", _fake)
    monkeypatch.delenv("TAKKUB_ROLE", raising=False)  # prevent pane env bleeding into tests
    return sent


class TestArgparse:
    """Argument parsing → request payload shape. Every payload now carries a
    `from_project` field (None when the CLI runs outside a cockpit-spawned
    pane); tests only assert on the fields the CLI actively populates."""

    def test_assign_requires_role_and_task(self, fake_request: list[dict[str, Any]]) -> None:
        cli.main(["assign", "--role", "frontend", "make a thing"])
        payload = fake_request[-1]
        assert payload["cmd"] == "assign"
        assert payload["role"] == "frontend"
        assert payload["cwd"] is None
        assert payload["task"] == "make a thing"

    def test_assign_with_cwd(self, fake_request: list[dict[str, Any]]) -> None:
        cli.main(["assign", "--role", "backend", "--cwd", "/x", "do work"])
        assert fake_request[-1]["cwd"] == "/x"

    def test_send_passes_from_role_env(
        self, fake_request: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "frontend")
        cli.main(["send", "--to", "backend", "hi"])
        payload = fake_request[-1]
        assert payload["cmd"] == "send"
        assert payload["to"] == "backend"
        assert payload["msg"] == "hi"
        assert payload["from"] == "frontend"

    def test_send_without_env_passes_none_from(
        self, fake_request: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        cli.main(["send", "--to", "backend", "hi"])
        assert fake_request[-1]["from"] is None

    def test_done_uses_env_role(
        self, fake_request: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "qa")
        cli.main(["done", "tests passing"])
        payload = fake_request[-1]
        assert payload["cmd"] == "done"
        assert payload["from"] == "qa"
        assert payload["note"] == "tests passing"

    def test_done_without_note(
        self, fake_request: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "qa")
        cli.main(["done"])
        assert fake_request[-1]["note"] == ""

    def test_list_command(self, fake_request: list[dict[str, Any]]) -> None:
        cli.main(["list"])
        assert fake_request[-1]["cmd"] == "list"

    def test_close_all(self, fake_request: list[dict[str, Any]]) -> None:
        cli.main(["close-all"])
        assert fake_request[-1]["cmd"] == "close-all"

    def test_close_role(self, fake_request: list[dict[str, Any]]) -> None:
        cli.main(["close", "--role", "backend"])
        payload = fake_request[-1]
        assert payload["cmd"] == "close"
        assert payload["role"] == "backend"

    def test_spawn_optional_cwd(self, fake_request: list[dict[str, Any]]) -> None:
        cli.main(["spawn", "--role", "frontend"])
        payload = fake_request[-1]
        assert payload["cmd"] == "spawn"
        assert payload["role"] == "frontend"
        assert payload["cwd"] is None

    def test_payload_includes_from_project_env(
        self, fake_request: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TAKKUB_PROJECT", "unirecon")
        cli.main(["list"])
        assert fake_request[-1]["from_project"] == "unirecon"

    def test_payload_from_project_unset_is_none(
        self, fake_request: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TAKKUB_PROJECT", raising=False)
        cli.main(["list"])
        assert fake_request[-1]["from_project"] is None

    def test_gemini_one_shot_routes_to_helper(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `takkub gemini "<prompt>"` is pure-local (does NOT go through
        # the orchestrator socket). Mock gemini_exec and assert the CLI
        # routes the prompt + flags through correctly.
        seen: dict[str, object] = {}

        def fake_gemini_exec(
            prompt: str, *, cwd: str | None = None, timeout: float = 120.0, model: str | None = None
        ):
            seen["prompt"] = prompt
            seen["cwd"] = cwd
            seen["timeout"] = timeout
            seen["model"] = model
            return True, "gemini answered"

        from agent_takkub import gemini_helper

        monkeypatch.setattr(gemini_helper, "gemini_exec", fake_gemini_exec)
        rc = cli.main(["gemini", "review this approach"])
        assert rc == 0
        assert seen["prompt"] == "review this approach"
        assert seen["cwd"] is None
        assert seen["model"] is None
        out = capsys.readouterr().out
        assert "gemini answered" in out

    def test_gemini_forwards_cwd_and_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, object] = {}

        def fake_gemini_exec(
            prompt: str, *, cwd: str | None = None, timeout: float = 120.0, model: str | None = None
        ):
            seen["cwd"] = cwd
            seen["model"] = model
            seen["timeout"] = timeout
            return True, ""

        from agent_takkub import gemini_helper

        monkeypatch.setattr(gemini_helper, "gemini_exec", fake_gemini_exec)
        cli.main(
            [
                "gemini",
                "--cwd",
                "C:/x/proj",
                "--model",
                "gemini-2.5-pro",
                "--timeout",
                "30",
                "do thing",
            ]
        )
        assert seen["cwd"] == "C:/x/proj"
        assert seen["model"] == "gemini-2.5-pro"
        assert seen["timeout"] == 30.0

    def test_assign_requires_commit_flag_parsed(self, fake_request: list[dict[str, Any]]) -> None:
        """--requires-commit is parsed and forwarded as True in the payload."""
        cli.main(["assign", "--role", "backend", "--requires-commit", "do work"])
        payload = fake_request[-1]
        assert payload["cmd"] == "assign"
        assert payload["requires_commit"] is True

    def test_assign_default_no_requires_commit(self, fake_request: list[dict[str, Any]]) -> None:
        """Without the flag, requires_commit is False in the payload."""
        cli.main(["assign", "--role", "backend", "do work"])
        payload = fake_request[-1]
        assert payload["cmd"] == "assign"
        assert payload.get("requires_commit") is False

    def test_assign_isolation_defaults_shared(self, fake_request: list[dict[str, Any]]) -> None:
        """Without --isolation the payload carries the shared default (#81)."""
        cli.main(["assign", "--role", "frontend", "build X"])
        assert fake_request[-1]["isolation"] == "shared"

    def test_assign_isolation_worktree_forwarded(self, fake_request: list[dict[str, Any]]) -> None:
        """--isolation worktree is parsed and forwarded (#81)."""
        cli.main(["assign", "--role", "frontend", "--isolation", "worktree", "build X"])
        assert fake_request[-1]["isolation"] == "worktree"

    def test_assign_isolation_forwarded_on_shards(self, fake_request: list[dict[str, Any]]) -> None:
        """Each shard inherits the isolation choice so a fan-out can isolate too."""
        cli.main(["assign", "--role", "qa", "--shards", "2", "--isolation", "worktree", "build X"])
        # last two payloads are the two shard assigns
        assert fake_request[-1]["isolation"] == "worktree"
        assert fake_request[-2]["isolation"] == "worktree"

    def test_assign_isolation_worktree_rejects_plan(
        self, fake_request: list[dict[str, Any]]
    ) -> None:
        """--isolation worktree + --plan is refused before any request is sent."""
        n_before = len(fake_request)
        rc = cli.main(
            ["assign", "--role", "qa", "--plan", "--shards", "2", "--isolation", "worktree", "t"]
        )
        assert rc != 0
        assert len(fake_request) == n_before  # nothing dispatched


class TestHarvestPayload:
    """Regression for the harvest dead-on-arrival bug (review 2026-06-16). The
    client built the harvest / harvest-done payloads WITHOUT a `from` stamp, so
    the server's layer-1 role gate (only-lead) rejected every invocation before
    the token check. Server-side tests masked it by hand-injecting
    `from: "lead"`; these go through the real cli.main -> cmd_harvest payload
    construction so the missing stamp is actually exercised."""

    def test_harvest_payload_stamps_from_role(
        self, fake_request: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        cli.main(["harvest", "--role", "backend"])
        payload = fake_request[-1]
        assert payload["cmd"] == "harvest"
        assert payload["from"] == "lead"

    def test_harvest_done_payload_stamps_from_role(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent: list[dict[str, Any]] = []

        def _fake(payload: dict[str, Any]) -> dict[str, Any]:
            sent.append(payload)
            if payload["cmd"] == "harvest":
                return {
                    "ok": True,
                    "state": "working",
                    "since_ts": 1_700_000_000.0,
                    "artifacts": [{"path": "/p/foo.py", "mtime_rel": "5m ago"}],
                }
            return {"ok": True, "msg": "done"}

        monkeypatch.setattr(cli, "_request", _fake)
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        cli.main(["harvest", "--role", "backend", "--auto-confirm"])
        cmds = {p["cmd"]: p for p in sent}
        assert "harvest-done" in cmds, "harvest-done was never reached"
        assert cmds["harvest"]["from"] == "lead"
        assert cmds["harvest-done"]["from"] == "lead"


class TestExitCodes:
    def test_ok_response_exit_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(cli, "_request", lambda _p: {"ok": True, "msg": "done"})
        rc = cli.main(["list"])
        assert rc == 0

    def test_err_response_exit_one(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(cli, "_request", lambda _p: {"ok": False, "msg": "no orchestrator"})
        rc = cli.main(["list"])
        assert rc == 1

    def test_status_payload_is_printed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            cli,
            "_request",
            lambda _p: {
                "ok": True,
                "msg": "status",
                "status": {"lead": "active", "frontend": "working"},
            },
        )
        cli.main(["list"])
        out = capsys.readouterr().out
        assert "lead" in out and "active" in out
        assert "frontend" in out and "working" in out


class TestRoleGate:
    """Lead-only commands (spawn/assign/close/close-all) must be blocked when
    invoked from a teammate pane. Prevents an agent that drifted into Lead
    behavior (e.g. after compaction at high context) from orchestrating."""

    def test_teammate_cannot_assign(
        self,
        fake_request: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "devops")
        rc = cli.main(["assign", "--role", "devops", "--cwd", "/x", "self-assign attempt"])
        assert rc == 1
        assert fake_request == []  # never reached orchestrator
        err = capsys.readouterr().err
        assert "only lead" in err and "devops" in err

    def test_teammate_cannot_spawn(
        self,
        fake_request: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "frontend")
        rc = cli.main(["spawn", "--role", "backend"])
        assert rc == 1
        assert fake_request == []

    def test_teammate_cannot_close(
        self,
        fake_request: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "qa")
        rc = cli.main(["close", "--role", "frontend"])
        assert rc == 1
        assert fake_request == []

    def test_teammate_cannot_close_all(
        self,
        fake_request: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "reviewer")
        rc = cli.main(["close-all"])
        assert rc == 1
        assert fake_request == []

    def test_lead_can_assign(
        self,
        fake_request: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        rc = cli.main(["assign", "--role", "backend", "do work"])
        assert rc == 0
        assert fake_request[-1]["cmd"] == "assign"

    def test_unset_role_allows_everything(
        self,
        fake_request: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """User running CLI manually from a terminal (no pane) must still work."""
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        rc = cli.main(["assign", "--role", "backend", "do work"])
        assert rc == 0
        assert fake_request[-1]["cmd"] == "assign"

    def test_teammate_can_send(
        self,
        fake_request: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "devops")
        rc = cli.main(["send", "--to", "backend", "need env list"])
        assert rc == 0
        assert fake_request[-1]["cmd"] == "send"

    def test_teammate_can_done(
        self,
        fake_request: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "devops")
        rc = cli.main(["done", "pipeline green"])
        assert rc == 0
        assert fake_request[-1]["cmd"] == "done"

    def test_teammate_can_list(
        self,
        fake_request: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "devops")
        rc = cli.main(["list"])
        assert rc == 0
        assert fake_request[-1]["cmd"] == "list"

    def test_teammate_can_run_gemini_one_shot(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # `gemini` is local — not in LEAD_ONLY_COMMANDS — so a teammate
        # pane can fire it for a second opinion mid-task.
        monkeypatch.setenv("TAKKUB_ROLE", "backend")
        from agent_takkub import gemini_helper

        monkeypatch.setattr(
            gemini_helper,
            "gemini_exec",
            lambda *_a, **_kw: (True, "answer"),
        )
        rc = cli.main(["gemini", "ping"])
        assert rc == 0


def test_request_payload_serialises_cleanly() -> None:
    """Smoke check: every payload we'd send is round-trippable JSON."""
    payload = {
        "cmd": "send",
        "to": "backend",
        "msg": "hi ภาษาไทย",
        "from": "frontend",
    }
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    assert b"\xe0" in encoded  # Thai bytes survived
    assert json.loads(encoded.decode("utf-8")) == payload


# ──────────────────────────────────────────────────────────────────────────────
# harvest command
# ──────────────────────────────────────────────────────────────────────────────

_SAMPLE_ARTIFACTS = [
    {"path": "/proj/src/foo.py", "mtime_ts": 1_700_000_000.0, "mtime_rel": "5m ago"},
    {"path": "/proj/docs/notes.md", "mtime_ts": 1_700_000_100.0, "mtime_rel": "3m ago"},
]


def _make_harvest_responder(
    *,
    artifacts: list[dict] | None = None,
    role_missing: bool = False,
) -> Any:
    """Return a fake _request callable that handles harvest + harvest-done calls."""
    calls: list[dict] = []

    def _fake(payload: dict) -> dict:
        calls.append(payload)
        cmd = payload.get("cmd")
        if cmd == "harvest":
            if role_missing:
                return {"ok": False, "msg": "role not running: backend"}
            return {
                "ok": True,
                "msg": "ok",
                "state": "working",
                "spawn_ts": 1_700_000_000.0,
                "since_ts": 1_699_996_400.0,
                "artifacts": artifacts if artifacts is not None else _SAMPLE_ARTIFACTS,
            }
        if cmd == "harvest-done":
            return {"ok": True, "msg": "backend reported done"}
        return {"ok": True, "msg": "stubbed"}

    _fake.calls = calls  # type: ignore[attr-defined]
    return _fake


class TestHarvestArgparse:
    def test_harvest_payload_has_role(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        responder = _make_harvest_responder()
        monkeypatch.setattr(cli, "_request", responder)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        cli.main(["harvest", "--role", "backend"])
        first = responder.calls[0]
        assert first["cmd"] == "harvest"
        assert first["role"] == "backend"

    def test_harvest_since_passed_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        responder = _make_harvest_responder()
        monkeypatch.setattr(cli, "_request", responder)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        cli.main(["harvest", "--role", "backend", "--since", "14:30"])
        first = responder.calls[0]
        assert first["since"] == "14:30"

    def test_harvest_limit_passed_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        responder = _make_harvest_responder()
        monkeypatch.setattr(cli, "_request", responder)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        cli.main(["harvest", "--role", "backend", "--limit", "50"])
        first = responder.calls[0]
        assert first["limit"] == 50

    def test_harvest_default_limit_is_100(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        responder = _make_harvest_responder()
        monkeypatch.setattr(cli, "_request", responder)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        cli.main(["harvest", "--role", "backend"])
        assert responder.calls[0]["limit"] == 100


class TestHarvestFlow:
    def test_auto_confirm_sends_harvest_done(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        responder = _make_harvest_responder()
        monkeypatch.setattr(cli, "_request", responder)
        rc = cli.main(["harvest", "--role", "backend", "--auto-confirm"])
        assert rc == 0
        cmds = [c["cmd"] for c in responder.calls]
        assert "harvest" in cmds
        assert "harvest-done" in cmds

    def test_harvest_done_carries_role(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        responder = _make_harvest_responder()
        monkeypatch.setattr(cli, "_request", responder)
        cli.main(["harvest", "--role", "backend", "--auto-confirm"])
        done_calls = [c for c in responder.calls if c["cmd"] == "harvest-done"]
        assert done_calls
        assert done_calls[0]["role"] == "backend"

    def test_user_declines_returns_exit_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        responder = _make_harvest_responder()
        monkeypatch.setattr(cli, "_request", responder)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        rc = cli.main(["harvest", "--role", "backend"])
        assert rc == 1
        cmds = [c["cmd"] for c in responder.calls]
        assert "harvest-done" not in cmds

    def test_role_not_running_returns_exit_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        responder = _make_harvest_responder(role_missing=True)
        monkeypatch.setattr(cli, "_request", responder)
        rc = cli.main(["harvest", "--role", "backend"])
        assert rc == 2

    def test_no_artifacts_returns_exit_3(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        responder = _make_harvest_responder(artifacts=[])
        monkeypatch.setattr(cli, "_request", responder)
        rc = cli.main(["harvest", "--role", "backend"])
        assert rc == 3

    def test_harvest_blocked_for_teammates(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "backend")
        responder = _make_harvest_responder()
        monkeypatch.setattr(cli, "_request", responder)
        rc = cli.main(["harvest", "--role", "backend"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "only lead" in err


class TestEnsureUtf8Stdio:
    """_ensure_utf8_stdio() must reconfigure stdout/stderr to UTF-8 so Thai and
    other non-ASCII text doesn't appear as ???? on Windows consoles."""

    def test_reconfigures_stdout_and_stderr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[str, str]] = []

        class _FakeStream:
            def reconfigure(self, encoding: str) -> None:
                calls.append(("stream", encoding))

        monkeypatch.setattr("sys.stdout", _FakeStream())
        monkeypatch.setattr("sys.stderr", _FakeStream())
        cli._ensure_utf8_stdio()
        assert calls.count(("stream", "utf-8")) == 2, (
            "_ensure_utf8_stdio must reconfigure both stdout and stderr to utf-8"
        )

    def test_skips_streams_without_reconfigure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Streams that lack reconfigure (e.g. binary wrappers) must not raise."""
        import io

        monkeypatch.setattr("sys.stdout", io.BytesIO())
        monkeypatch.setattr("sys.stderr", io.BytesIO())
        cli._ensure_utf8_stdio()  # must not raise

    def test_swallows_reconfigure_exceptions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If reconfigure raises (e.g. already closed stream), it must be swallowed."""

        class _BadStream:
            def reconfigure(self, encoding: str) -> None:
                raise OSError("stream closed")

        monkeypatch.setattr("sys.stdout", _BadStream())
        monkeypatch.setattr("sys.stderr", _BadStream())
        cli._ensure_utf8_stdio()  # must not propagate the OSError


class TestWorktreeCli:
    """`takkub worktree list/merge/clean` (P2.4) — pure-local, lead-gated."""

    class _FakeWtMgr:
        # ClassVar: shared scripted state, reset by the autouse fixture per test
        from typing import ClassVar

        rows: ClassVar[list] = []
        merge_result: ClassVar[tuple] = (True, "merged wt/frontend-9 + cleanup เรียบร้อย")
        clean_lines: ClassVar[list] = ["REMOVED wt/qa-7"]
        merge_calls: ClassVar[list] = []

        def __init__(self, *a, **k):
            pass

        def git_root(self, cwd):
            return "/repo"

        def list_isolated(self, root):
            return type(self).rows

        def merge_isolated(self, root, branch, keep=False):
            type(self).merge_calls.append((branch, keep))
            return type(self).merge_result

        def clean_isolated(self, root, force=False):
            return type(self).clean_lines

    @pytest.fixture(autouse=True)
    def _fake_mgr(self, monkeypatch):
        from agent_takkub import worktree_manager as wm

        self._FakeWtMgr.rows = []
        self._FakeWtMgr.merge_calls = []
        self._FakeWtMgr.clean_lines = ["REMOVED wt/qa-7"]
        self._FakeWtMgr.merge_result = (True, "merged")
        monkeypatch.setattr(wm, "WorktreeManager", self._FakeWtMgr)
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)

    def test_teammate_blocked_by_role_gate(self, monkeypatch):
        monkeypatch.setenv("TAKKUB_ROLE", "backend")
        rc = cli.main(["worktree", "list"])
        assert rc != 0

    def test_lead_allowed(self, monkeypatch):
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        assert cli.main(["worktree", "list"]) == 0

    def test_list_empty_ok(self):
        assert cli.main(["worktree", "list"]) == 0

    def test_merge_resolves_newest_branch_for_role(self):
        self._FakeWtMgr.rows = [
            {"path": "/w1", "branch": "wt/frontend-100", "sha": "a", "ahead": 1, "dirty": False},
            {"path": "/w2", "branch": "wt/frontend-200", "sha": "b", "ahead": 1, "dirty": False},
            {"path": "/w3", "branch": "wt/qa-300", "sha": "c", "ahead": 0, "dirty": False},
        ]
        rc = cli.main(["worktree", "merge", "--role", "frontend"])
        assert rc == 0
        assert self._FakeWtMgr.merge_calls == [("wt/frontend-200", False)]  # newest ts

    def test_merge_exact_branch_and_keep(self):
        rc = cli.main(["worktree", "merge", "--branch", "wt/qa-300", "--keep"])
        assert rc == 0
        assert self._FakeWtMgr.merge_calls == [("wt/qa-300", True)]

    def test_merge_requires_role_or_branch(self):
        assert cli.main(["worktree", "merge"]) != 0

    def test_merge_no_candidates_for_role(self):
        self._FakeWtMgr.rows = []
        assert cli.main(["worktree", "merge", "--role", "ghost"]) != 0

    def test_clean_reports_lines(self):
        assert cli.main(["worktree", "clean"]) == 0

    def test_clean_failed_line_sets_exit(self):
        self._FakeWtMgr.clean_lines = ["FAILED wt/qa-7 — locked"]
        assert cli.main(["worktree", "clean", "--force"]) != 0
