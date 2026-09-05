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

    def test_assign_with_effort_override(
        self, fake_request: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "agent_takkub.provider_config.assign_effort_override_error",
            lambda *_a, **_kw: None,
        )
        cli.main(["assign", "--role", "backend", "--effort", "low", "scan"])
        assert fake_request[-1]["effort"] == "low"

    def test_no_effort_flag_sends_none(self, fake_request: list[dict[str, Any]]) -> None:
        cli.main(["assign", "--role", "backend", "scan"])
        assert fake_request[-1]["effort"] is None

    def test_effort_override_is_forwarded_to_every_shard(
        self, fake_request: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "agent_takkub.provider_config.assign_effort_override_error",
            lambda *_a, **_kw: None,
        )
        cli.main(["assign", "--role", "backend", "--shards", "2", "--effort", "high", "scan"])
        assert [payload["effort"] for payload in fake_request[-2:]] == ["high", "high"]

    def test_subagent_mode_rejects_effort_override(
        self, fake_request: list[dict[str, Any]]
    ) -> None:
        rc = cli.main(
            ["assign", "--role", "reviewer", "--mode", "subagent", "--effort", "low", "scan"]
        )
        assert rc == 1
        assert fake_request == []

    def test_assign_effort_validation_error_sends_nothing(
        self,
        fake_request: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "agent_takkub.provider_config.assign_effort_override_error",
            lambda *_args, **_kwargs: "--effort not accepted",
        )
        rc = cli.main(["assign", "--role", "backend", "--effort", "low", "scan"])
        assert rc == 1
        assert fake_request == []

    def test_effort_choices_rejects_unknown_value(
        self, fake_request: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        # argparse itself only knows about the common low/medium/high
        # baseline (issue #323) — a provider-specific tier like claude's
        # xhigh/max is not a valid --effort value on this CLI knob.
        with pytest.raises(SystemExit):
            cli.main(["assign", "--role", "backend", "--effort", "xhigh", "scan"])
        assert fake_request == []

    def test_effort_override_validated_against_provider_override(
        self, fake_request: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            "agent_takkub.provider_config.assign_provider_override_error",
            lambda *_a, **_kw: None,
        )

        def _fake_effort_error(role, effort, project, provider_override=None):
            captured["provider_override"] = provider_override
            return None

        monkeypatch.setattr(
            "agent_takkub.provider_config.assign_effort_override_error", _fake_effort_error
        )
        cli.main(
            [
                "assign",
                "--role",
                "backend",
                "--provider",
                "codex",
                "--effort",
                "low",
                "scan",
            ]
        )
        assert captured["provider_override"] == "codex"

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


class TestTaskFileAndFromFile:
    """#491: shell interpolation on the SENDING side eats backticks/$()/parens
    out of a task/message positional before `takkub` ever sees them —
    --task-file/--from-file (and "-"/stdin) bypass shell quoting entirely."""

    _TRICKY = "run `rm -rf tmp` then $(echo done) and (parens) literally"

    def test_assign_task_file_reads_file_verbatim(
        self, fake_request: list[dict[str, Any]], tmp_path: Any
    ) -> None:
        task_path = tmp_path / "task.txt"
        task_path.write_text(self._TRICKY, encoding="utf-8")
        rc = cli.main(["assign", "--role", "backend", "--task-file", str(task_path)])
        assert rc == 0
        assert fake_request[-1]["task"] == self._TRICKY

    def test_assign_task_file_dash_reads_stdin(
        self,
        fake_request: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO(self._TRICKY))
        rc = cli.main(["assign", "--role", "backend", "--task-file", "-"])
        assert rc == 0
        assert fake_request[-1]["task"] == self._TRICKY

    def test_assign_positional_dash_reads_stdin(
        self,
        fake_request: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO(self._TRICKY))
        rc = cli.main(["assign", "--role", "backend", "-"])
        assert rc == 0
        assert fake_request[-1]["task"] == self._TRICKY

    def test_assign_task_file_and_positional_are_mutually_exclusive(
        self, fake_request: list[dict[str, Any]], tmp_path: Any
    ) -> None:
        task_path = tmp_path / "task.txt"
        task_path.write_text("from file", encoding="utf-8")
        n_before = len(fake_request)
        rc = cli.main(
            ["assign", "--role", "backend", "--task-file", str(task_path), "from positional"]
        )
        assert rc != 0
        assert len(fake_request) == n_before  # nothing dispatched

    def test_assign_requires_task_or_task_file(self, fake_request: list[dict[str, Any]]) -> None:
        n_before = len(fake_request)
        rc = cli.main(["assign", "--role", "backend"])
        assert rc != 0
        assert len(fake_request) == n_before

    def test_send_from_file_reads_file_verbatim(
        self, fake_request: list[dict[str, Any]], tmp_path: Any
    ) -> None:
        msg_path = tmp_path / "msg.txt"
        msg_path.write_text(self._TRICKY, encoding="utf-8")
        rc = cli.main(["send", "--to", "backend", "--from-file", str(msg_path)])
        assert rc == 0
        assert fake_request[-1]["msg"] == self._TRICKY

    def test_send_from_file_dash_reads_stdin(
        self,
        fake_request: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO(self._TRICKY))
        rc = cli.main(["send", "--to", "backend", "--from-file", "-"])
        assert rc == 0
        assert fake_request[-1]["msg"] == self._TRICKY

    def test_send_from_file_and_positional_are_mutually_exclusive(
        self, fake_request: list[dict[str, Any]], tmp_path: Any
    ) -> None:
        msg_path = tmp_path / "msg.txt"
        msg_path.write_text("from file", encoding="utf-8")
        n_before = len(fake_request)
        rc = cli.main(["send", "--to", "backend", "--from-file", str(msg_path), "from positional"])
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

    def test_qa_plan_fanout_warns(
        self, fake_request: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli.main(["assign", "--role", "qa", "--plan", "--shards", "2", "smoke test"])
        out = capsys.readouterr().out
        assert "เบราว์เซอร์" in out

    def test_non_browser_role_shard_fanout_does_not_warn(
        self, fake_request: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli.main(
            ["assign", "--role", "backend", "--shards", "2", "build X"]
        )  # backend: not a browser role (#433 made frontend one)
        out = capsys.readouterr().out
        assert "เบราว์เซอร์" not in out

    def test_single_qa_assign_does_not_warn(
        self, fake_request: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli.main(["assign", "--role", "qa", "smoke test"])
        out = capsys.readouterr().out
        assert "เบราว์เซอร์" not in out


class TestShardFanoutResourceWaitVisibility:
    """#412: a shard fan-out reply used to collapse every shard's own `msg`
    into a bare "queued N/N shards" count, discarding a resource-governor
    wait reason (e.g. `heavy_project_limit`) some shard's own `assign`
    response carried — the summary read "queued 3/3 shards" even when one
    shard was actually just PARKED behind another pane's slot, not spawned."""

    def test_resource_blocked_shard_reason_is_not_swallowed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        responses = iter(
            [
                {"ok": True, "msg": "task queued for backend#1 (sending when ready)"},
                {
                    "ok": True,
                    "msg": (
                        "backend#2 queued — waiting for heavy slot "
                        "(heavy_project_limit, blocked by backend#1)"
                    ),
                },
            ]
        )
        monkeypatch.setattr(cli, "_request", lambda _payload: next(responses))
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)

        cli.main(["assign", "--role", "backend", "--shards", "2", "build it"])

        out = capsys.readouterr().out
        assert "heavy_project_limit" in out
        assert "blocked by backend#1" in out

    def test_ordinary_shard_success_is_not_mistaken_for_resource_wait(
        self, fake_request: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The routine per-shard success wording (the default stub's plain
        "stubbed" message) must not itself trip the resource-wait detection."""
        cli.main(["assign", "--role", "backend", "--shards", "2", "build it"])
        out = capsys.readouterr().out
        assert "queued 2/2 shards" in out
        assert "heavy_project_limit" not in out


class TestSelfCommitIsolationWarning:
    """#399: a task that tells the pane to commit its own work only actually
    works under `--isolation worktree` — `pane_guard`'s git_lead_only rule
    hard-blocks `git commit` on the shared tree for every teammate role
    (#314). Root incident: a task said "commit the result" on a shared-tree
    assign; the pane's commit was denied and Lead ended up committing by
    hand — this warns Lead at assign time instead of after a burned turn."""

    @pytest.mark.parametrize(
        "task",
        [
            "fix the bug and commit เอง",
            "แก้บั๊กแล้ว commit ด้วยตัวเอง",
            "implement this and commit it yourself",
            "self-commit the result when done",
        ],
    )
    def test_shared_isolation_self_commit_task_warns(
        self, fake_request: list[dict[str, Any]], capsys: pytest.CaptureFixture[str], task: str
    ) -> None:
        rc = cli.main(["assign", "--role", "backend", task])
        assert rc == 0
        out = capsys.readouterr().out
        assert "--isolation worktree" in out
        assert "399" in out

    def test_worktree_isolation_self_commit_task_does_not_warn(
        self, fake_request: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli.main(
            ["assign", "--role", "backend", "--isolation", "worktree", "fix it and commit เอง"]
        )
        out = capsys.readouterr().out
        assert "--isolation worktree" not in out

    def test_ordinary_task_does_not_warn(
        self, fake_request: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli.main(["assign", "--role", "backend", "add /auth/login endpoint"])
        out = capsys.readouterr().out
        assert "--isolation worktree" not in out

    def test_shard_fanout_self_commit_task_warns(
        self, fake_request: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli.main(["assign", "--role", "backend", "--shards", "2", "build it and commit เอง"])
        out = capsys.readouterr().out
        assert "--isolation worktree" in out


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

    def test_banner_warns_only_once_per_session(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """#464 item 4: the 'other cockpit also running' line used to print
        on every `takkub status`/`inbox`/`list` call — the first call in a
        pane's lifetime (same parent shell PID) still shows it, every
        following call from that same shell must not repeat it. The own
        identity line stays every time regardless."""
        repo_root, port_file = self._mock_prod_instance(monkeypatch, tmp_path)
        monkeypatch.setattr(cli.config, "RUNTIME_DIR", tmp_path / "prod" / "runtime")
        other_port_file = repo_root / "runtime" / "port"
        other_port_file.parent.mkdir(parents=True)
        other_port_file.write_text("43124", encoding="utf-8")

        class _FakeSocket:
            def close(self) -> None:
                pass

        monkeypatch.setattr(cli.socket, "create_connection", lambda *_a, **_k: _FakeSocket())

        first = cli._instance_banner()
        second = cli._instance_banner()

        assert "ก็รันอยู่ด้วย" in first
        assert f"▸ v9.9.9   (port 43123 · {port_file.parent})" == second
        assert "ก็รันอยู่ด้วย" not in second

    def test_banner_warns_only_once_per_pane_despite_changing_ppid(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """#464 follow-up: on Windows (and reportedly macOS) a Claude pane
        spawns a brand-new child shell for every `takkub <cmd>` invocation,
        so os.getppid() changes on every single call from the SAME pane —
        keying the warned-once marker on it never actually deduped anything
        (real incident: two `takkub status` calls from one pane produced two
        different marker files, other-instance-14156-54147 and
        other-instance-2232-54147, so the banner reprinted every command).
        Inside a cockpit pane (TAKKUB_ROLE set), the marker must instead key
        off the pane's own spawn-time env, which never changes for that
        pane's lifetime — so dedup must hold even when getppid() changes
        between calls."""
        repo_root, _ = self._mock_prod_instance(monkeypatch, tmp_path)
        monkeypatch.setattr(cli.config, "RUNTIME_DIR", tmp_path / "prod" / "runtime")
        other_port_file = repo_root / "runtime" / "port"
        other_port_file.parent.mkdir(parents=True)
        other_port_file.write_text("43124", encoding="utf-8")
        monkeypatch.setenv("TAKKUB_ROLE", "backend")
        monkeypatch.setenv("TAKKUB_PROJECT", "demo")
        monkeypatch.setenv("TAKKUB_PANE_TOKEN", "tok-abc")

        class _FakeSocket:
            def close(self) -> None:
                pass

        monkeypatch.setattr(cli.socket, "create_connection", lambda *_a, **_k: _FakeSocket())

        monkeypatch.setattr(cli.os, "getppid", lambda: 14156)
        first = cli._instance_banner()
        monkeypatch.setattr(cli.os, "getppid", lambda: 2232)
        second = cli._instance_banner()

        assert "ก็รันอยู่ด้วย" in first
        assert "ก็รันอยู่ด้วย" not in second

    def test_banner_warned_marker_falls_back_to_ppid_outside_a_pane(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """No TAKKUB_ROLE at all (manual `takkub` use from a bare user
        shell, not a cockpit pane) — there is no pane-lifetime env to key
        off, so the marker falls back to the parent PID, same as before."""
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        monkeypatch.setattr(cli.os, "getppid", lambda: 9999)

        assert cli._pane_identity_key() == "ppid-9999"

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
        check_calls: ClassVar[list] = []
        clean_branches: ClassVar[list] = []
        merge_live_paths_calls: ClassVar[list] = []
        clean_live_paths_calls: ClassVar[list] = []
        orphans: ClassVar[list] = []
        orphans_live_paths_calls: ClassVar[list] = []
        trash_lines: ClassVar[list] = []
        trash_live_paths_calls: ClassVar[list] = []

        def __init__(self, *a, **k):
            pass

        def git_root(self, cwd):
            return "/repo"

        def list_isolated(self, root):
            return type(self).rows

        def merge_isolated(
            self, root, branch, keep=False, live_paths=frozenset(), check_only=False
        ):
            type(self).merge_calls.append((branch, keep))
            type(self).check_calls.append(check_only)
            type(self).merge_live_paths_calls.append(set(live_paths))
            return type(self).merge_result

        def clean_isolated(self, root, force=False, live_paths=frozenset(), branch=None):
            type(self).clean_branches.append(branch)
            type(self).clean_live_paths_calls.append(set(live_paths))
            return type(self).clean_lines

        def list_orphans(self, root, live_paths=frozenset()):
            type(self).orphans_live_paths_calls.append(set(live_paths))
            return type(self).orphans

        def sweep_trash(self, root, live_paths=frozenset()):
            type(self).trash_live_paths_calls.append(set(live_paths))
            return type(self).trash_lines

    @staticmethod
    def _no_cockpit(_payload):
        raise RuntimeError("agent-takkub cockpit is not running (no port file).")

    @pytest.fixture(autouse=True)
    def _fake_mgr(self, monkeypatch):
        from agent_takkub import worktree_manager as wm

        self._FakeWtMgr.rows = []
        self._FakeWtMgr.merge_calls = []
        self._FakeWtMgr.check_calls = []
        self._FakeWtMgr.clean_branches = []
        self._FakeWtMgr.merge_live_paths_calls = []
        self._FakeWtMgr.clean_lines = ["REMOVED wt/qa-7"]
        self._FakeWtMgr.clean_live_paths_calls = []
        self._FakeWtMgr.merge_result = (True, "merged")
        self._FakeWtMgr.orphans = []
        self._FakeWtMgr.orphans_live_paths_calls = []
        self._FakeWtMgr.trash_lines = []
        self._FakeWtMgr.trash_live_paths_calls = []
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
        rc = cli.main(["worktree", "merge", "--role", "frontend", "--latest"])
        assert rc == 0
        assert self._FakeWtMgr.merge_calls == [("wt/frontend-200", False)]  # newest ts

    def test_merge_role_matches_exactly_not_by_prefix(self):
        """#403 — `--role backend` must not swallow `--role backend#3`'s
        branch just because `wt/backend-3-<ts>` starts with `wt/backend-`."""
        self._FakeWtMgr.rows = [
            {
                "path": "/w1",
                "branch": "wt/backend-1787711320",
                "sha": "a",
                "ahead": 1,
                "dirty": False,
            },
            {
                "path": "/w2",
                "branch": "wt/backend-3-1787712691",
                "sha": "b",
                "ahead": 1,
                "dirty": False,
            },
            {
                "path": "/w3",
                "branch": "wt/backend-2-1787712041",
                "sha": "c",
                "ahead": 1,
                "dirty": False,
            },
        ]
        rc = cli.main(["worktree", "merge", "--role", "backend"])
        assert rc == 0
        assert self._FakeWtMgr.merge_calls == [("wt/backend-1787711320", False)]

    def test_merge_role_hash_suffix_matches_its_own_branch(self):
        self._FakeWtMgr.rows = [
            {
                "path": "/w1",
                "branch": "wt/backend-1787711320",
                "sha": "a",
                "ahead": 1,
                "dirty": False,
            },
            {
                "path": "/w2",
                "branch": "wt/backend-3-1787712691",
                "sha": "b",
                "ahead": 1,
                "dirty": False,
            },
            {
                "path": "/w3",
                "branch": "wt/backend-2-1787712041",
                "sha": "c",
                "ahead": 1,
                "dirty": False,
            },
        ]
        rc = cli.main(["worktree", "merge", "--role", "backend#3"])
        assert rc == 0
        assert self._FakeWtMgr.merge_calls == [("wt/backend-3-1787712691", False)]

    def test_merge_role_hash2_matches_its_own_branch(self):
        self._FakeWtMgr.rows = [
            {
                "path": "/w1",
                "branch": "wt/backend-1787711320",
                "sha": "a",
                "ahead": 1,
                "dirty": False,
            },
            {
                "path": "/w2",
                "branch": "wt/backend-3-1787712691",
                "sha": "b",
                "ahead": 1,
                "dirty": False,
            },
            {
                "path": "/w3",
                "branch": "wt/backend-2-1787712041",
                "sha": "c",
                "ahead": 1,
                "dirty": False,
            },
        ]
        rc = cli.main(["worktree", "merge", "--role", "backend#2"])
        assert rc == 0
        assert self._FakeWtMgr.merge_calls == [("wt/backend-2-1787712041", False)]

    def test_merge_role_multiple_candidates_refuses_and_lists_branches(self, capsys):
        # #439: the newest worktree is usually the one still being worked on
        # — guessing merged the wrong one (or failed on its dirty tree with
        # no way to pick the finished sibling). Refuse, list, point at
        # --branch / --latest.
        self._FakeWtMgr.rows = [
            {"path": "/w1", "branch": "wt/backend-100", "sha": "a", "ahead": 1, "dirty": False},
            {"path": "/w2", "branch": "wt/backend-200", "sha": "b", "ahead": 0, "dirty": True},
        ]
        rc = cli.main(["worktree", "merge", "--role", "backend"])
        assert rc != 0
        assert self._FakeWtMgr.merge_calls == []
        out = capsys.readouterr().out + capsys.readouterr().err
        assert "--branch wt/backend-100" in out and "--branch wt/backend-200" in out
        assert "dirty" in out and "1 commit ahead" in out
        assert "--latest" in out

    def test_merge_role_multiple_candidates_latest_flag_takes_newest(self, capsys):
        self._FakeWtMgr.rows = [
            {"path": "/w1", "branch": "wt/backend-100", "sha": "a", "ahead": 1, "dirty": False},
            {"path": "/w2", "branch": "wt/backend-200", "sha": "b", "ahead": 1, "dirty": False},
        ]
        self._FakeWtMgr.merge_result = (True, "merged wt/backend-200 + cleanup เรียบร้อย")
        rc = cli.main(["worktree", "merge", "--role", "backend", "--latest"])
        assert rc == 0
        assert self._FakeWtMgr.merge_calls == [("wt/backend-200", False)]
        out = capsys.readouterr().out
        assert "wt/backend-200" in out
        assert "2 worktree" in out

    def test_merge_check_flag_forwards_check_only(self):
        self._FakeWtMgr.merge_result = (True, "wt/qa-300: 1 commit ahead · merge-tree clean")
        rc = cli.main(["worktree", "merge", "--branch", "wt/qa-300", "--check"])
        assert rc == 0
        assert self._FakeWtMgr.check_calls == [True]

    def test_clean_branch_flag_forwards_branch(self):
        rc = cli.main(["worktree", "clean", "--branch", "wt/frontend-100"])
        assert rc == 0
        assert self._FakeWtMgr.clean_branches == ["wt/frontend-100"]

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

    def test_clean_reports_orphans_without_flag_but_does_not_delete(self, tmp_path):
        """#355 default: orphans are surfaced (path + size) but never
        deleted without an explicit flag — they may hold uncommitted work
        git no longer has any record of."""
        orphan = tmp_path / "orphan-1"
        orphan.mkdir()
        (orphan / "f.txt").write_text("x")
        self._FakeWtMgr.orphans = [
            {"path": str(orphan), "size_bytes": 1, "file_count": 1, "has_node_modules": False}
        ]
        assert cli.main(["worktree", "clean"]) == 0
        assert orphan.exists()  # report-only — never deleted by default

    def test_clean_orphans_flag_deletes_the_whole_dir(self, tmp_path):
        orphan = tmp_path / "orphan-2"
        orphan.mkdir()
        (orphan / "f.txt").write_text("x")
        self._FakeWtMgr.orphans = [
            {"path": str(orphan), "size_bytes": 1, "file_count": 1, "has_node_modules": False}
        ]
        assert cli.main(["worktree", "clean", "--orphans"]) == 0
        assert not orphan.exists()

    def test_clean_orphans_node_modules_only_keeps_source(self, tmp_path):
        orphan = tmp_path / "orphan-3"
        (orphan / "src").mkdir(parents=True)
        (orphan / "src" / "app.py").write_text("code")
        (orphan / "node_modules" / "pkg").mkdir(parents=True)
        (orphan / "node_modules" / "pkg" / "index.js").write_text("x")
        self._FakeWtMgr.orphans = [
            {"path": str(orphan), "size_bytes": 2, "file_count": 2, "has_node_modules": True}
        ]
        assert cli.main(["worktree", "clean", "--orphans-node-modules-only"]) == 0
        assert (orphan / "src" / "app.py").exists()  # source kept
        assert not (orphan / "node_modules").exists()  # only node_modules removed

    def test_clean_orphans_and_node_modules_only_together_rejected(self):
        rc = cli.main(["worktree", "clean", "--orphans", "--orphans-node-modules-only"])
        assert rc != 0

    def test_clean_forwards_live_paths_to_list_orphans(self, monkeypatch):
        """#355's own scope: list_orphans must get the same live-pane guard
        as clean_isolated — a dir a live pane sits in is never reported as
        deletable, even if git has already forgotten it."""
        monkeypatch.setattr(
            cli,
            "_request",
            lambda payload: {"ok": True, "msg": "1 live worktree(s)", "paths": ["/w/live-9"]},
        )
        assert cli.main(["worktree", "clean"]) == 0
        assert self._FakeWtMgr.orphans_live_paths_calls[-1] == {"/w/live-9"}

    def test_clean_sweeps_trash_dirs_by_default_no_flag_needed(self, capsys):
        """#411 (1) — `.trash-*` leftovers from a partial `remove_worktree_tree`
        must be swept every plain `clean`, without `--orphans`."""
        self._FakeWtMgr.trash_lines = ["REMOVED .trash-backend-3-1 (.trash ค้างจากรอบก่อน)"]
        assert cli.main(["worktree", "clean"]) == 0
        out = capsys.readouterr().out
        assert ".trash-backend-3-1" in out

    def test_clean_trash_sweep_failure_sets_nonzero_exit(self):
        self._FakeWtMgr.trash_lines = ["FAILED  .trash-x-1 — locked"]
        assert cli.main(["worktree", "clean"]) != 0

    def test_clean_forwards_live_paths_to_sweep_trash(self, monkeypatch):
        monkeypatch.setattr(
            cli,
            "_request",
            lambda payload: {"ok": True, "msg": "1 live worktree(s)", "paths": ["/w/live-9"]},
        )
        assert cli.main(["worktree", "clean"]) == 0
        assert self._FakeWtMgr.trash_live_paths_calls[-1] == {"/w/live-9"}

    def test_clean_orphans_verify_retries_leftover_after_first_delete_fails(
        self, tmp_path, monkeypatch
    ):
        """#411 (3) — when the first `remove_worktree_tree` call during
        `--orphans` leaves the dir on disk (e.g. a transient Windows file
        lock), `clean --orphans` retries it in the SAME round instead of
        requiring the caller to notice and re-run."""
        from agent_takkub import worktree_manager as wm

        orphan = tmp_path / "orphan-stuck"
        orphan.mkdir()
        self._FakeWtMgr.orphans = [
            {"path": str(orphan), "size_bytes": 0, "file_count": 0, "has_node_modules": False}
        ]
        calls = {"n": 0}
        real_remove = wm.remove_worktree_tree

        def flaky_remove(path):
            calls["n"] += 1
            if calls["n"] == 1:
                return False, "ลบไม่ได้ (mock lock)", ""
            return real_remove(path)

        monkeypatch.setattr(wm, "remove_worktree_tree", flaky_remove)
        rc = cli.main(["worktree", "clean", "--orphans"])
        assert rc == 0  # the retry succeeded, so nothing stays failed
        assert calls["n"] == 2  # first attempt (failed) + verify retry (succeeded)
        assert not orphan.exists()

    def test_clean_orphans_verify_reports_still_stuck_after_retry(self, tmp_path, monkeypatch):
        """A dir that is STILL there even after the verify retry must keep
        the command's exit non-zero — the verify pass corrects the count,
        it never silently swallows a genuine, persistent failure."""
        from agent_takkub import worktree_manager as wm

        orphan = tmp_path / "orphan-truly-stuck"
        orphan.mkdir()
        self._FakeWtMgr.orphans = [
            {"path": str(orphan), "size_bytes": 0, "file_count": 0, "has_node_modules": False}
        ]
        monkeypatch.setattr(wm, "remove_worktree_tree", lambda path: (False, "ลบไม่ได้ (locked)", ""))
        rc = cli.main(["worktree", "clean", "--orphans"])
        assert rc != 0
        assert orphan.exists()  # never actually removed by the mock


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
