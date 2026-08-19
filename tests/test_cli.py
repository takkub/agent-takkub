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

    def test_assign_mode_defaults_to_pane(self, fake_request: list[dict[str, Any]]) -> None:
        cli.main(["assign", "--role", "reviewer", "scan auth"])
        assert fake_request[-1]["mode"] == "pane"

    def test_assign_subagent_mode_forwarded(self, fake_request: list[dict[str, Any]]) -> None:
        cli.main(["assign", "--role", "reviewer", "--mode", "subagent", "scan auth"])
        assert fake_request[-1]["mode"] == "subagent"

    def test_subagent_mode_allows_twenty_shards(self, fake_request: list[dict[str, Any]]) -> None:
        cli.main(["assign", "--role", "reviewer", "--mode", "subagent", "--shards", "20", "scan"])
        assert len(fake_request) == 20
        assert all(payload["mode"] == "subagent" for payload in fake_request)

    def test_pane_mode_keeps_existing_eight_shard_cap(
        self, fake_request: list[dict[str, Any]]
    ) -> None:
        assert cli.main(["assign", "--role", "qa", "--shards", "9", "scan"]) == 1
        assert fake_request == []

    def test_subagent_mode_rejects_model_override(self, fake_request: list[dict[str, Any]]) -> None:
        rc = cli.main(
            ["assign", "--role", "reviewer", "--mode", "subagent", "--model", "x", "scan"]
        )
        assert rc == 1
        assert fake_request == []

    def test_subagent_done_payload(
        self, fake_request: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        cli.main(["subagent-done", "--role", "reviewer", "audit clean"])
        assert fake_request[-1]["cmd"] == "subagent-done"
        assert fake_request[-1]["role"] == "reviewer"
        assert fake_request[-1]["note"] == "audit clean"

    def test_assign_with_model_override(self, fake_request: list[dict[str, Any]]) -> None:
        cli.main(["assign", "--role", "qa", "--model", "claude-haiku-4-5", "scan"])
        assert fake_request[-1]["model"] == "claude-haiku-4-5"

    def test_assign_model_override_is_forwarded_to_every_shard(
        self, fake_request: list[dict[str, Any]]
    ) -> None:
        cli.main(["assign", "--role", "qa", "--shards", "2", "--model", "flash", "scan"])
        assert [payload["model"] for payload in fake_request[-2:]] == ["flash", "flash"]

    def test_assign_model_validation_error_sends_nothing(
        self,
        fake_request: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "agent_takkub.provider_config.assign_model_override_error",
            lambda *_args, **_kwargs: "--model unsupported",
        )
        before = len(fake_request)
        rc = cli.main(["assign", "--role", "qa", "--model", "flash", "scan"])
        assert rc == 1
        assert len(fake_request) == before

    def test_assign_with_provider_override(
        self, fake_request: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Issue #270: a validated --provider is a no-op stub here (offline
        # test, orchestrator itself is mocked) — this only proves the CLI
        # forwards it.
        monkeypatch.setattr(
            "agent_takkub.provider_config.assign_provider_override_error",
            lambda *_a, **_kw: None,
        )
        cli.main(["assign", "--role", "backend", "--provider", "claude", "scan"])
        assert fake_request[-1]["provider"] == "claude"

    def test_provider_override_is_forwarded_to_every_shard(
        self, fake_request: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "agent_takkub.provider_config.assign_provider_override_error",
            lambda *_a, **_kw: None,
        )
        cli.main(["assign", "--role", "backend", "--shards", "2", "--provider", "claude", "scan"])
        assert [payload["provider"] for payload in fake_request[-2:]] == ["claude", "claude"]

    def test_subagent_mode_rejects_provider_override(
        self, fake_request: list[dict[str, Any]]
    ) -> None:
        rc = cli.main(
            ["assign", "--role", "reviewer", "--mode", "subagent", "--provider", "claude", "scan"]
        )
        assert rc == 1
        assert fake_request == []

    def test_assign_provider_validation_error_sends_nothing(
        self,
        fake_request: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "agent_takkub.provider_config.assign_provider_override_error",
            lambda *_args, **_kwargs: "--provider unavailable",
        )
        rc = cli.main(["assign", "--role", "backend", "--provider", "codex", "scan"])
        assert rc == 1
        assert fake_request == []

    def test_model_override_validated_against_provider_override(
        self, fake_request: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The --model check must be told about a validated --provider on
        # the same assign, so it validates against what will actually
        # spawn — capture the kwarg the CLI passes through.
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            "agent_takkub.provider_config.assign_provider_override_error",
            lambda *_a, **_kw: None,
        )

        def _fake_model_error(role, model, project, provider_override=None):
            captured["provider_override"] = provider_override
            return None

        monkeypatch.setattr(
            "agent_takkub.provider_config.assign_model_override_error", _fake_model_error
        )
        cli.main(
            [
                "assign",
                "--role",
                "backend",
                "--provider",
                "claude",
                "--model",
                "claude-opus-5",
                "scan",
            ]
        )
        assert captured["provider_override"] == "claude"

    def test_assign_model_warning_prints_but_does_not_block(
        self,
        fake_request: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Issue #127: an unrecognized (but not provably-wrong-provider) model
        # id must warn on stderr and still let the assign go through.
        monkeypatch.setattr(
            "agent_takkub.provider_config.assign_model_override_warning",
            lambda *_args, **_kwargs: "model id not recognized for provider 'claude'",
        )
        rc = cli.main(["assign", "--role", "qa", "--model", "totally-new-model", "scan"])
        assert rc == 0
        assert len(fake_request) == 1
        assert fake_request[-1]["model"] == "totally-new-model"
        err = capsys.readouterr().err
        assert "warn:" in err
        assert "not recognized" in err

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


class TestBrowserShardAssignWarning:
    """#304 point 5: warn Lead in the `assign` response itself when fanning
    out a browser-role (qa/critic/designer) shard — Playwright MCP has been
    observed failing to connect under concurrent shard spawn (#146/#304)
    with the `mb` fallback blocked by design (#92)."""

    def test_qa_shard_fanout_warns(
        self, fake_request: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli.main(["assign", "--role", "qa", "--shards", "2", "smoke test"])
        out = capsys.readouterr().out
        assert "#146" in out or "#304" in out
        assert "เบราว์เซอร์" in out

    def test_qa_plan_fanout_warns(
        self, fake_request: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli.main(["assign", "--role", "qa", "--plan", "--shards", "2", "smoke test"])
        out = capsys.readouterr().out
        assert "เบราว์เซอร์" in out

    def test_non_browser_role_shard_fanout_does_not_warn(
        self, fake_request: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli.main(["assign", "--role", "frontend", "--shards", "2", "build X"])
        out = capsys.readouterr().out
        assert "เบราว์เซอร์" not in out

    def test_single_qa_assign_does_not_warn(
        self, fake_request: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli.main(["assign", "--role", "qa", "smoke test"])
        out = capsys.readouterr().out
        assert "เบราว์เซอร์" not in out


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


class TestInstanceBanner:
    @staticmethod
    def _mock_prod_instance(monkeypatch: pytest.MonkeyPatch, tmp_path) -> tuple:
        repo_root = tmp_path / "checkout"
        current_port_file = tmp_path / "prod" / "runtime" / "port"
        monkeypatch.setattr(cli.config, "REPO_ROOT", repo_root)
        monkeypatch.setattr(cli.config, "DATA_HOME", tmp_path / "prod")
        monkeypatch.setattr(cli.config, "instance_identity_label", lambda: "v9.9.9")
        monkeypatch.setattr(cli.config, "read_port", lambda: 43123)
        monkeypatch.setattr(cli.config, "_get_port_file", lambda: current_port_file)
        return repo_root, current_port_file

    def test_banner_contains_label_port_and_runtime_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        _, port_file = self._mock_prod_instance(monkeypatch, tmp_path)

        banner = cli._instance_banner()

        assert banner == f"▸ v9.9.9   (port 43123 · {port_file.parent})"

    def test_banner_warns_when_other_instance_is_reachable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        repo_root, _ = self._mock_prod_instance(monkeypatch, tmp_path)
        other_port_file = repo_root / "runtime" / "port"
        other_port_file.parent.mkdir(parents=True)
        other_port_file.write_text("43124", encoding="utf-8")
        calls: list[tuple[tuple[str, int], float]] = []

        class FakeSocket:
            closed = False

            def close(self) -> None:
                self.closed = True

        fake_socket = FakeSocket()

        def _connect(address: tuple[str, int], timeout: float):
            calls.append((address, timeout))
            return fake_socket

        monkeypatch.setattr(cli.socket, "create_connection", _connect)

        banner = cli._instance_banner()

        assert calls == [(("127.0.0.1", 43124), 0.3)]
        assert fake_socket.closed is True
        assert "⚠ dev · checkout ก็รันอยู่ด้วย (port 43124)" in banner
        assert "คำสั่งนี้คุม v9.9.9 เท่านั้น" in banner

    def test_banner_is_silent_when_other_instance_is_unreachable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        repo_root, port_file = self._mock_prod_instance(monkeypatch, tmp_path)
        other_port_file = repo_root / "runtime" / "port"
        other_port_file.parent.mkdir(parents=True)
        other_port_file.write_text("43124", encoding="utf-8")

        def _unreachable(*_args, **_kwargs):
            raise OSError("connection refused")

        monkeypatch.setattr(cli.socket, "create_connection", _unreachable)

        banner = cli._instance_banner()

        assert banner == f"▸ v9.9.9   (port 43123 · {port_file.parent})"
        assert "⚠" not in banner

    def test_banner_failure_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _broken_label() -> str:
            raise RuntimeError("bad config")

        monkeypatch.setattr(cli.config, "instance_identity_label", _broken_label)

        assert cli._instance_banner() == ""

    def test_banner_keeps_project_name_for_pane_assigned_into_worktree(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """#185 end-to-end: a teammate pane spawned with `--cwd` into a git
        worktree of the same repo must still show the real project name in
        `takkub list`'s header, not the worktree's own basename — otherwise
        Lead reads "controls X only" and wrongly believes it lost some panes.
        Exercises the real `config.instance_identity_label()` (not stubbed),
        driven purely by the env var the orchestrator stamps on every spawn."""
        worktree = tmp_path / "worktrees" / "agent-takkub" / "frontend-1786615682"
        worktree.mkdir(parents=True)
        current_port_file = tmp_path / "runtime" / "port"
        monkeypatch.setattr(cli.config, "REPO_ROOT", worktree)
        monkeypatch.setattr(cli.config, "DATA_HOME", worktree)
        monkeypatch.setenv("TAKKUB_PROJECT", "agent-takkub")
        monkeypatch.setattr(cli.config, "read_port", lambda: 56919)
        monkeypatch.setattr(cli.config, "_get_port_file", lambda: current_port_file)
        # This case tests the worktree-derived project label only. Isolate the
        # optional cross-instance probe so a real prod cockpit running on the
        # developer machine cannot append an unrelated warning to the banner.
        isolated_home = tmp_path / "isolated-home"
        monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: isolated_home))

        banner = cli._instance_banner()

        assert banner == f"▸ dev · agent-takkub   (port 56919 · {current_port_file.parent})"

    @pytest.mark.parametrize(
        ("command", "response"),
        [
            ("list", {"ok": True, "status": {"lead": "active"}}),
            (
                "status",
                {
                    "ok": True,
                    "report": {"project": "demo", "panes": {}, "any_stalled": False},
                },
            ),
        ],
    )
    def test_list_and_status_print_banner_first(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
        response: dict[str, object],
    ) -> None:
        monkeypatch.setattr(cli, "_request", lambda _payload: response)
        monkeypatch.setattr(cli, "_instance_banner", lambda: "▸ test-instance")
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)

        assert cli.main([command]) == 0

        assert capsys.readouterr().out.splitlines()[0] == "▸ test-instance"


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
        merge_live_paths_calls: ClassVar[list] = []
        clean_live_paths_calls: ClassVar[list] = []

        def __init__(self, *a, **k):
            pass

        def git_root(self, cwd):
            return "/repo"

        def list_isolated(self, root):
            return type(self).rows

        def merge_isolated(self, root, branch, keep=False, live_paths=frozenset()):
            type(self).merge_calls.append((branch, keep))
            type(self).merge_live_paths_calls.append(set(live_paths))
            return type(self).merge_result

        def clean_isolated(self, root, force=False, live_paths=frozenset()):
            type(self).clean_live_paths_calls.append(set(live_paths))
            return type(self).clean_lines

    @staticmethod
    def _no_cockpit(_payload):
        raise RuntimeError("agent-takkub cockpit is not running (no port file).")

    @pytest.fixture(autouse=True)
    def _fake_mgr(self, monkeypatch):
        from agent_takkub import worktree_manager as wm

        self._FakeWtMgr.rows = []
        self._FakeWtMgr.merge_calls = []
        self._FakeWtMgr.merge_live_paths_calls = []
        self._FakeWtMgr.clean_lines = ["REMOVED wt/qa-7"]
        self._FakeWtMgr.clean_live_paths_calls = []
        self._FakeWtMgr.merge_result = (True, "merged")
        monkeypatch.setattr(wm, "WorktreeManager", self._FakeWtMgr)
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        # `clean` now makes a best-effort live-pane-guard query (#187). Default
        # to "cockpit unreachable" so the rest of this class's tests — which
        # predate that query — stay hermetic/deterministic regardless of
        # whether a real cockpit happens to be running on the dev machine.
        monkeypatch.setattr(cli, "_request", self._no_cockpit)

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

    def test_merge_forwards_live_paths_from_orchestrator(self, monkeypatch):
        """#227 — `merge` had no live-pane guard at all (unlike `clean`'s
        #187 fix); it must now query the cockpit the same way `clean` does
        and pass the result through to `merge_isolated`."""
        monkeypatch.setattr(
            cli,
            "_request",
            lambda payload: {"ok": True, "msg": "1 live worktree(s)", "paths": ["/w/live-9"]},
        )
        assert cli.main(["worktree", "merge", "--branch", "wt/qa-300"]) == 0
        assert self._FakeWtMgr.merge_live_paths_calls[-1] == {"/w/live-9"}

    def test_merge_no_live_paths_when_cockpit_unreachable(self):
        """Default fixture stubs `_request` to raise (cockpit not running) —
        `merge` must still work, with an empty live-paths set rather than
        propagating the connection error."""
        assert cli.main(["worktree", "merge", "--branch", "wt/qa-300"]) == 0
        assert self._FakeWtMgr.merge_live_paths_calls[-1] == set()

    def test_clean_reports_lines(self):
        assert cli.main(["worktree", "clean"]) == 0

    def test_clean_failed_line_sets_exit(self):
        self._FakeWtMgr.clean_lines = ["FAILED wt/qa-7 — locked"]
        assert cli.main(["worktree", "clean", "--force"]) != 0

    def test_clean_forwards_live_paths_from_orchestrator(self, monkeypatch):
        """#187 — when the cockpit IS reachable, `clean` must query it for
        live-pane worktree paths and pass them straight through to
        `clean_isolated` so the live-pane guard has something to check."""
        monkeypatch.setattr(
            cli,
            "_request",
            lambda payload: {"ok": True, "msg": "1 live worktree(s)", "paths": ["/w/live-9"]},
        )
        assert cli.main(["worktree", "clean", "--force"]) == 0
        assert self._FakeWtMgr.clean_live_paths_calls[-1] == {"/w/live-9"}

    def test_clean_no_live_paths_when_cockpit_unreachable(self):
        """Default fixture already stubs `_request` to raise (cockpit not
        running) — `clean` must still work, with an empty live-paths set
        rather than propagating the connection error."""
        assert cli.main(["worktree", "clean"]) == 0
        assert self._FakeWtMgr.clean_live_paths_calls[-1] == set()


class TestDiskPruneCli:
    """`takkub disk` (all roles) / `takkub prune` (lead only) — pure-local."""

    @pytest.fixture(autouse=True)
    def _isolated_data_home(self, tmp_path, monkeypatch):
        from agent_takkub import disk_usage

        monkeypatch.setattr(disk_usage, "DATA_HOME", tmp_path)
        # disk_report/prune's claude-config-derived categories (chat-history,
        # shell-snapshots) call default_claude_config_dir() independently of
        # DATA_HOME — pin it under tmp_path too so no CLI test here ever
        # reads/lists the real ~/.claude or ~/.agent-takkub/claude-config.
        monkeypatch.setattr(
            disk_usage, "default_claude_config_dir", lambda: tmp_path / "claude-config"
        )
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        return tmp_path

    def test_disk_available_to_teammate(self, monkeypatch):
        monkeypatch.setenv("TAKKUB_ROLE", "backend")
        assert cli.main(["disk"]) == 0

    def test_disk_json(self, capsys, tmp_path):
        (tmp_path / "venv").mkdir()
        assert cli.main(["disk", "--json"]) == 0
        out = capsys.readouterr().out
        # main() appends a trailing human-readable "ok: ..." line after the
        # JSON blob (same convention as `verify --json`/`audit-skills --json`)
        # — decode just the first JSON value and ignore that trailer.
        report, _ = json.JSONDecoder().raw_decode(out)
        assert report["data_home"] == str(tmp_path.resolve())
        assert any(c["key"] == "venv" for c in report["categories"])

    def test_prune_blocked_for_teammate(self, monkeypatch):
        monkeypatch.setenv("TAKKUB_ROLE", "backend")
        assert cli.main(["prune"]) != 0

    def test_prune_allowed_for_lead_dry_run_default(self, monkeypatch):
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        assert cli.main(["prune"]) == 0

    def test_prune_unknown_category_rejected(self):
        rc = cli.main(["prune", "--category", "not-a-real-category"])
        assert rc != 0

    def test_prune_help_lists_every_valid_category(self, capsys):
        """qa (2026-08-06): `--category`'s help text is a hand-maintained
        listing that had already drifted from `disk_usage.VALID_CATEGORIES`
        once (missing `graft-graphs`, functionally accepted but undiscoverable
        via `--help`). Pin every valid category name so the two can't drift
        apart silently again. Whitespace stripped before the substring check:
        argparse's HelpFormatter line-wraps on the hyphens inside category
        names (e.g. `browser-profiles` → `browser-\n  profiles`), which is
        cosmetic, not a real absence."""
        from agent_takkub import disk_usage

        with pytest.raises(SystemExit) as exc:
            cli.main(["prune", "--help"])
        assert exc.value.code == 0
        squashed = "".join(capsys.readouterr().out.split())
        for category in disk_usage.VALID_CATEGORIES:
            assert category in squashed, f"{category!r} missing from `takkub prune --help`"

    def test_prune_review_category_without_level_is_refused(self, capsys, tmp_path):
        rc = cli.main(["prune", "--category", "chat-history", "--yes"])
        out = capsys.readouterr().out
        assert "REFUSED" in out
        assert rc != 0 or "REFUSED" in out

    def test_prune_dry_run_does_not_delete(self, tmp_path):
        wt = tmp_path / "worktrees" / "proj" / "frontend-1"
        wt.mkdir(parents=True)
        rc = cli.main(["prune", "--category", "orphan-worktrees"])
        assert rc == 0
        assert wt.exists()  # no --yes → nothing removed

    def test_prune_yes_removes_orphan_worktree(self, tmp_path):
        wt = tmp_path / "worktrees" / "proj" / "frontend-2"
        wt.mkdir(parents=True)
        rc = cli.main(["prune", "--category", "orphan-worktrees", "--yes"])
        assert rc == 0
        assert not wt.exists()

    def test_prune_yes_leaves_orphan_with_source_files_at_safe_level(self, tmp_path):
        """#132: a source file (not node_modules) inside an orphan worktree
        must survive the default (safe) category+level even with --yes."""
        wt = tmp_path / "worktrees" / "proj" / "frontend-3"
        wt.mkdir(parents=True)
        (wt / "f.txt").write_text("x")
        rc = cli.main(["prune", "--category", "orphan-worktrees", "--yes"])
        assert rc == 0
        assert wt.exists()
        assert (wt / "f.txt").exists()

    def test_prune_yes_review_category_removes_orphan_with_source_files_and_prints_targets(
        self, tmp_path, capsys
    ):
        wt = tmp_path / "worktrees" / "proj" / "frontend-4"
        wt.mkdir(parents=True)
        (wt / "f.txt").write_text("x")
        rc = cli.main(
            [
                "prune",
                "--category",
                "orphan-worktrees-review",
                "--level",
                "review",
                "--yes",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert not wt.exists()
        assert str(wt) in out  # target path printed before/while deleting, never silent


class TestRestartCli:
    """`takkub restart` — full cockpit restart from the terminal (no button)."""

    def test_restart_sends_payload(self, fake_request, monkeypatch):
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        assert cli.main(["restart"]) == 0
        assert fake_request[-1]["cmd"] == "restart"

    def test_lead_allowed(self, fake_request, monkeypatch):
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        assert cli.main(["restart"]) == 0
        assert fake_request[-1]["cmd"] == "restart"

    def test_teammate_blocked(self, fake_request, monkeypatch):
        n = len(fake_request)
        monkeypatch.setenv("TAKKUB_ROLE", "backend")
        assert cli.main(["restart"]) != 0
        assert len(fake_request) == n  # never reached the socket


class TestPrintStatusReport:
    """#236: `takkub status` must surface a pane blocked on a prompt (e.g.
    Claude Code's own permission-approval dialog) distinctly from ordinary
    `working`, not silently collapse the two."""

    def test_blocked_reason_renders_marker_line(self, capsys):
        report = {
            "project": "p",
            "panes": {
                "backend": {
                    "state": "working",
                    "stall_minutes": None,
                    "last_progress_human": "0s ago",
                    "last_progress_abs": "12:00:00",
                    "blocked_reason": "permission",
                }
            },
        }
        cli._print_status_report(report)
        out = capsys.readouterr().out
        assert "⛔ blocked:permission-prompt" in out

    def test_no_blocked_marker_for_ordinary_working_pane(self, capsys):
        report = {
            "project": "p",
            "panes": {
                "backend": {
                    "state": "working",
                    "stall_minutes": None,
                    "last_progress_human": "0s ago",
                    "last_progress_abs": "12:00:00",
                    "blocked_reason": None,
                }
            },
        }
        cli._print_status_report(report)
        out = capsys.readouterr().out
        assert "blocked:" not in out


class _FakeCliSocket:
    """Stand-in for the connected `socket.socket` `_request` talks to."""

    def __init__(self, recv_fn) -> None:
        self._recv_fn = recv_fn
        self.sent: list[bytes] = []
        self.settimeout_calls: list[float] = []
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def settimeout(self, t: float) -> None:
        self.settimeout_calls.append(t)

    def recv(self, n: int) -> bytes:
        return self._recv_fn()

    def close(self) -> None:
        self.closed = True


class TestRequestDeadline:
    """#233: `takkub assign` (and every other command routed through
    `_request`) must always return within a bounded time, even when the
    server keeps the connection open by dribbling data that never completes
    a newline-terminated frame — `socket.settimeout()` alone only bounds a
    single blocking call, not the total wait."""

    def test_returns_timeout_when_server_dribbles_data_without_newline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Every recv() call "succeeds" (returns non-empty bytes within its
        # own per-call timeout) but never delivers a newline — the old code
        # would loop on this forever.
        clock = iter([0.0] + [float(i) for i in range(1, 40)])
        monkeypatch.setattr(cli.time, "monotonic", lambda: next(clock))
        sock = _FakeCliSocket(recv_fn=lambda: b"x")
        monkeypatch.setattr(cli, "_connect", lambda: sock)

        result = cli._request({"cmd": "assign"}, response_timeout=15.0)

        assert result["ok"] is False
        assert "timed out" in result["msg"]
        assert sock.closed is True

    def test_returns_timeout_when_recv_raises_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = iter([0.0, 1.0, 2.0])
        monkeypatch.setattr(cli.time, "monotonic", lambda: next(clock))

        def _raise():
            raise TimeoutError("timed out")

        sock = _FakeCliSocket(recv_fn=_raise)
        monkeypatch.setattr(cli, "_connect", lambda: sock)

        result = cli._request({"cmd": "assign"}, response_timeout=15.0)

        assert result["ok"] is False
        assert "timed out" in result["msg"]

    def test_returns_clear_error_on_malformed_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chunks = iter([b"not json at all\n", b""])
        monkeypatch.setattr(cli.time, "monotonic", lambda: 0.0)
        sock = _FakeCliSocket(recv_fn=lambda: next(chunks))
        monkeypatch.setattr(cli, "_connect", lambda: sock)

        result = cli._request({"cmd": "assign"})

        assert result["ok"] is False
        assert "malformed response" in result["msg"]

    def test_returns_reply_promptly_when_server_responds_normally(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chunks = iter([b'{"ok": true, "msg": "done"}\n'])
        monkeypatch.setattr(cli.time, "monotonic", lambda: 0.0)
        sock = _FakeCliSocket(recv_fn=lambda: next(chunks))
        monkeypatch.setattr(cli, "_connect", lambda: sock)

        result = cli._request({"cmd": "assign"})

        assert result == {"ok": True, "msg": "done"}
