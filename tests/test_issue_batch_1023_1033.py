"""Regression tests for the 2.2.59 batch (#1023–#1033)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub import lead_readiness


def _solo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agent_takkub.team_preset.current",
        lambda project=None: {"preset": "solo-lead", "roles": {}, "lead_may_implement": True},
    )


class TestLeadContextFirstRound1028:
    def test_embedded_system_prompt_counts_as_read(self, tmp_path: Path, monkeypatch) -> None:
        """A Lead whose system prompt carries the current policy owes no read
        round — at spawn, and again after a compaction invalidates the epoch."""
        monkeypatch.setattr("agent_takkub.config.RUNTIME_DIR", tmp_path / "runtime")
        _solo(monkeypatch)
        path = lead_readiness.prepare("default", "claude", str(tmp_path))
        assert "has not been read" in lead_readiness.reason(path, project="default")

        docs, revision = lead_readiness.docs_for_system_prompt(path)
        assert "## Effective session policy" in docs
        assert "do not spend a round on it" in docs
        lead_readiness.mark_embedded(path, revision)
        assert lead_readiness.reason(path, project="default") == ""

        lead_readiness.invalidate(path, "compact")
        assert lead_readiness.reason(path, project="default") == ""

    def test_policy_change_still_demands_a_read(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("agent_takkub.config.RUNTIME_DIR", tmp_path / "runtime")
        _solo(monkeypatch)
        path = lead_readiness.prepare("default", "claude", str(tmp_path))
        _docs, revision = lead_readiness.docs_for_system_prompt(path)
        lead_readiness.mark_embedded(path, revision)
        (tmp_path / "CLAUDE.md").write_text("# project rule changed\n", encoding="utf-8")
        assert "changed" in lead_readiness.reason(path, project="default")
        state, _text = lead_readiness.refresh(path)
        # The prompt is now stale, so readiness comes only from the read itself.
        assert "has not been read" in lead_readiness.reason(path, project="default")
        lead_readiness.acknowledge(path, state)
        assert lead_readiness.reason(path, project="default") == ""

    @pytest.mark.parametrize(
        "command",
        ["rtk takkub context read", "rtk proxy takkub context read 2>&1", "rtk takkub team status"],
    )
    def test_rtk_prefixed_bootstrap_is_allowed(self, command: str) -> None:
        assert lead_readiness.bootstrap_allowed("Bash", {"command": command})

    def test_pipe_and_chain_stay_refused(self) -> None:
        for command in ("rtk takkub context read | tail -n 40", "rtk takkub context read; ls"):
            assert not lead_readiness.bootstrap_allowed("Bash", {"command": command})


class TestModelSwitch1031:
    def test_claude_switch_line_is_parsed(self) -> None:
        from agent_takkub.provider_spec import model_switch_in

        hit = model_switch_in(
            "claude",
            [
                "● working",
                "  ⎿  Switched to Haiku 4.5 due to high demand for Sonnet 5.5 · context window 1M → 200K tokens",
            ],
        )
        assert hit is not None
        assert (hit["to"], hit["from"], hit["context"]) == ("Haiku 4.5", "Sonnet 5.5", "1M → 200K")

    def test_prose_and_other_providers_do_not_match(self) -> None:
        from agent_takkub.provider_spec import model_switch_in

        assert model_switch_in("claude", ["we saw it Switched to Haiku 4.5 due to load"]) is None
        assert model_switch_in("codex", ["Switched to x due to y"]) is None

    def test_fallback_defaults_are_the_newest_of_each_family(self) -> None:
        from agent_takkub.orchestrator_text import _DEFAULT_TEAMMATE_TIER, _ROLE_MODEL_TIERS

        fallbacks = {_DEFAULT_TEAMMATE_TIER[2], *(tier[2] for tier in _ROLE_MODEL_TIERS.values())}
        assert fallbacks == {"claude-haiku-5-5", "claude-sonnet-5-5"}

    def test_fallback_precedence_and_same_model_drop(self, monkeypatch) -> None:
        from agent_takkub import provider_models
        from agent_takkub.spawn_engine import _resolve_fallback_model

        monkeypatch.delenv("TAKKUB_TEAMMATE_FALLBACK", raising=False)
        monkeypatch.setattr(provider_models, "fallback_for", lambda provider: None)
        resolve = _resolve_fallback_model
        assert (
            resolve("TAKKUB_TEAMMATE_FALLBACK", "claude-haiku-5-5", "claude-sonnet-5-5", {})
            == "claude-haiku-5-5"
        )
        # falling back to the model that is overloaded does nothing
        assert resolve("TAKKUB_TEAMMATE_FALLBACK", "claude-haiku-5-5", "claude-haiku-5-5", {}) == ""
        monkeypatch.setattr(provider_models, "fallback_for", lambda provider: "claude-opus-5-5")
        assert resolve("TAKKUB_TEAMMATE_FALLBACK", "claude-haiku-5-5", "x", {}) == "claude-opus-5-5"
        monkeypatch.setenv("TAKKUB_TEAMMATE_FALLBACK", "")
        assert resolve("TAKKUB_TEAMMATE_FALLBACK", "claude-haiku-5-5", "x", {}) == ""


class TestBootstrapMarker1032:
    def test_wrapped_line_and_new_wording_match(self) -> None:
        from agent_takkub.provider_spec import bootstrap_failure_marker

        wrapped = [
            "› Error: account/read failed during TUI boot",
            "strap: account/read failed: workspace routing discovery failed (code -32603)",
        ]
        assert bootstrap_failure_marker("codex", wrapped)
        assert bootstrap_failure_marker(
            "codex", ["workspace routing discovery failed (code -32603)"]
        )
        assert bootstrap_failure_marker("claude", wrapped) is None


class TestShardFanout1026:
    def test_self_verify_only_task_stays_a_subagent_fanout(self) -> None:
        from agent_takkub.shard_fanout import resolve_shard_fanout

        kind, _note = resolve_shard_fanout(
            "frontend",
            4,
            task="ปรับ UI 4 หัวข้อ: header, footer, form, table แล้ว screenshot self-verify",
            provider="claude",
        )
        assert kind == "subagent"

    def test_browser_as_the_work_stays_a_pane_fanout(self) -> None:
        from agent_takkub.shard_fanout import resolve_shard_fanout

        for task in ("UI audit 15 pages", "screenshot ทุกหน้า แล้วยืนยัน", "e2e checkout flow"):
            assert resolve_shard_fanout("frontend", 3, task=task, provider="claude")[0] == "pane"

    def test_lead_closed_shard_settles_the_group(self) -> None:
        from agent_takkub.pipeline_executor import PipelineMixin, ShardGroup

        class _Orch(PipelineMixin):
            def __init__(self) -> None:
                self._shard_groups = {"p::frontend": ShardGroup(base_role="frontend", total=3)}
                self.notices: list[str] = []

            def _notify_lead(self, project, message, **_kw) -> None:
                self.notices.append(message)

        orch = _Orch()
        group = orch._shard_groups["p::frontend"]
        orch._withdraw_shard("p", "frontend#2")
        orch._withdraw_shard("p", "frontend#3")
        assert not group.closed and not orch.notices  # #1 still owes a result
        group.done["frontend#1"] = "done"
        assert group.settled()
        orch._inject_shard_fanout_handoff("p", group)
        assert "2 ปิดโดย Lead" in orch.notices[0]
        assert "NO RESPONSE" not in orch.notices[0]

    def test_all_shards_closed_by_lead_is_silent(self) -> None:
        from agent_takkub.pipeline_executor import PipelineMixin, ShardGroup

        class _Orch(PipelineMixin):
            def __init__(self) -> None:
                self._shard_groups = {"p::frontend": ShardGroup(base_role="frontend", total=2)}
                self.notices: list[str] = []

            def _notify_lead(self, project, message, **_kw) -> None:
                self.notices.append(message)

        orch = _Orch()
        orch._withdraw_shard("p", "frontend#1")
        orch._withdraw_shard("p", "frontend#2")
        assert orch._shard_groups == {} and orch.notices == []


class TestTaskDock1023:
    def test_only_recent_groups_are_built_but_open_work_always_is(self) -> None:
        from agent_takkub.task_dock import _DOCK_MAX_ROWS, visible_groups

        def group(n: int, rows: int, status: str = "ok") -> dict:
            return {
                "date": f"d{n}",
                "goal": f"g{n}",
                "features": [{"name": "f", "rows": [{"status": status}] * rows}],
            }

        newest_first = [group(i, 50) for i in range(10)]
        newest_first.append(group(99, 3, status="working"))  # old, but still open
        shown, hidden = visible_groups({"groups": newest_first})
        built = sum(len(f["rows"]) for g in shown for f in g["features"])
        assert built == _DOCK_MAX_ROWS + 3
        assert shown[-1]["goal"] == "g99"
        assert hidden == 10 * 50 - _DOCK_MAX_ROWS

    def test_small_ledger_is_untouched(self) -> None:
        from agent_takkub.task_dock import visible_groups

        state = {"groups": [{"features": [{"rows": [{"status": "ok"}] * 5}]}]}
        assert visible_groups(state) == (state["groups"], 0)


class TestSkillLinkFastPath1023:
    def test_second_pass_with_nothing_changed_does_no_work(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from agent_takkub import config, skill_scan

        store = tmp_path / "store"
        (store / "alpha").mkdir(parents=True)
        (store / "alpha" / "SKILL.md").write_text("---\nname: alpha\n---\n", encoding="utf-8")
        empty = tmp_path / "global"
        empty.mkdir()
        root = tmp_path / "wt"
        root.mkdir()
        monkeypatch.setattr(config, "project_skills_dir", lambda ns: store)
        monkeypatch.setattr(config, "global_skills_dir", lambda: empty)
        monkeypatch.setattr(config, "project_skill_excludes", lambda ns: set())
        monkeypatch.setattr(skill_scan, "_exclude_skill_links_from_git", lambda root: None)
        skill_scan._LINKS_FRESH.clear()

        assert skill_scan.ensure_project_skill_links(root, "p", copy=True) == []
        assert (root / ".claude" / "skills" / "alpha" / "SKILL.md").is_file()

        full_passes: list[int] = []
        real = skill_scan._ensure_project_skill_links_full
        monkeypatch.setattr(
            skill_scan,
            "_ensure_project_skill_links_full",
            lambda *a, **k: full_passes.append(1) or real(*a, **k),
        )
        assert skill_scan.ensure_project_skill_links(root, "p", copy=True) == []
        assert full_passes == []

        # a new skill in the store is seen at once
        (store / "beta").mkdir()
        (store / "beta" / "SKILL.md").write_text("---\nname: beta\n---\n", encoding="utf-8")
        assert skill_scan.ensure_project_skill_links(root, "p", copy=True) == []
        assert full_passes == [1]
        assert (root / ".claude" / "skills" / "beta" / "SKILL.md").is_file()


class TestRecallTokenCache1023:
    def test_record_content_is_tokenized_once(self, monkeypatch) -> None:
        from agent_takkub.core.brain import retrieval

        calls: list[str] = []
        monkeypatch.setattr(retrieval, "tokenize", lambda text: calls.append(text) or text.split())
        retrieval._TOKEN_CACHE.clear()
        assert retrieval._doc_tokens(["a b", "c d"]) == [["a", "b"], ["c", "d"]]
        assert retrieval._doc_tokens(["a b", "c d", "e"]) == [["a", "b"], ["c", "d"], ["e"]]
        assert calls == ["a b", "c d", "e"]


class TestReportCsp1030:
    def test_report_route_allows_workers_and_wasm_but_no_other_origin(self) -> None:
        from agent_takkub.remote import http_server
        from agent_takkub.report_csp import REPORT_CSP_HEADER

        assert http_server._REPORT_CSP_HEADER is REPORT_CSP_HEADER
        assert "default-src 'self'" in REPORT_CSP_HEADER
        assert "worker-src 'self' blob:" in REPORT_CSP_HEADER
        assert "'wasm-unsafe-eval'" in REPORT_CSP_HEADER
        assert "'unsafe-eval'" not in REPORT_CSP_HEADER.replace("'wasm-unsafe-eval'", "")
        assert "http" not in REPORT_CSP_HEADER

    def test_check_serves_the_report_under_that_header(self, tmp_path: Path) -> None:
        import urllib.request

        from agent_takkub import report_builder
        from agent_takkub.report_csp import REPORT_CSP_HEADER

        page = tmp_path / "r.html"
        page.write_text("<p>hi</p>", encoding="utf-8")
        with report_builder._served_like_remote(page) as url:
            with urllib.request.urlopen(url, timeout=10) as resp:
                assert resp.headers["Content-Security-Policy"] == REPORT_CSP_HEADER
                assert resp.read() == b"<p>hi</p>"


class TestWorktreeClean1027:
    def test_several_branches_and_streamed_lines(self) -> None:
        from agent_takkub.worktree_manager import WorktreeManager

        mgr = WorktreeManager()
        mgr.list_isolated = lambda root, **_k: [  # type: ignore[method-assign]
            {
                "path": "/gone/a",
                "branch": "wt/a-1",
                "sha": "a",
                "ref": "wt/a-1",
                "ahead": 2,
                "dirty": False,
            },
            {
                "path": "/gone/b",
                "branch": "wt/b-1",
                "sha": "b",
                "ref": "wt/b-1",
                "ahead": 1,
                "dirty": False,
            },
            {
                "path": "/gone/c",
                "branch": "wt/c-1",
                "sha": "c",
                "ref": "wt/c-1",
                "ahead": 1,
                "dirty": False,
            },
        ]
        mgr._all_commits_landed_via_cherry_pick = lambda *a: False  # type: ignore[method-assign]
        seen: list[str] = []
        lines = mgr.clean_isolated(
            "/repo-that-is-not-there",
            branch=["wt/a-1", "wt/c-1", "wt/ghost-9"],
            on_line=seen.append,
        )
        assert lines == seen
        assert [line.split()[0] for line in lines] == ["NOT", "KEEP", "KEEP"]
        assert "wt/ghost-9" in lines[0] and "wt/b-1" not in "".join(lines)


class TestReflectRunUsage:
    """A reflect run writes no session file, so its tokens are logged here."""

    def test_claude_json_envelope_yields_reply_and_usage(self) -> None:
        import json

        from agent_takkub.skill_learning import reflector

        envelope = json.dumps(
            {
                "result": '{"intents": []}',
                "usage": {
                    "input_tokens": 2,
                    "cache_creation_input_tokens": 19542,
                    "cache_read_input_tokens": 649,
                    "output_tokens": 94,
                },
                "modelUsage": {"claude-haiku-5-5": {}},
                "total_cost_usd": 0.00401,
            }
        )
        text, usage = reflector._unwrap_reply("claude", envelope)
        assert reflector.parse_intents(text) == []
        assert usage["cache_creation_input_tokens"] == 19542
        assert usage["output_tokens"] == 94
        assert usage["models"] == "claude-haiku-5-5"
        assert usage["cost_usd"] == 0.004

    def test_plain_text_and_other_providers_pass_through(self) -> None:
        from agent_takkub.skill_learning import reflector

        bare = '{"intents": [{"op": "skip"}]}'
        assert reflector._unwrap_reply("claude", bare) == (bare, {})
        assert reflector._unwrap_reply("claude", "not json") == ("not json", {})
        assert reflector._unwrap_reply("codex", '{"result": "x"}') == ('{"result": "x"}', {})

    def test_claude_reflector_asks_for_the_json_envelope(self) -> None:
        from agent_takkub.skill_learning import reflector

        argv = reflector.build_argv("claude", "claude", "p", "")
        assert argv[argv.index("--output-format") + 1] == "json"
        assert "--no-session-persistence" in argv
