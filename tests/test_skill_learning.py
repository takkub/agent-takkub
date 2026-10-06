"""Skill Learning (backlog 2c6cb77c) — store, promoter, lifecycle, reflector,
pipeline, index, episode and the orchestrator/skill_scan seams.

Every test runs against tmp skill stores + tmp runtime/settings dirs and a
fake reflector runner — no provider CLI is ever executed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_takkub import config
from agent_takkub.skill_learning import (
    MANAGED_BY,
    episode,
    index,
    lifecycle,
    pipeline,
    promoter,
    reflector,
    settings,
    store,
)

NS = "proj"
EVIDENCE = "the build only passes after running uv pip install --no-deps -e ."
CORPUS = f"task text\nnote: {EVIDENCE}\nmore transcript"


@pytest.fixture(autouse=True)
def _tmp_stores(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_SKILLS_HOME", tmp_path / "project-skills")
    monkeypatch.setattr(config, "GLOBAL_SKILLS_HOME", tmp_path / "skills")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(config, "SETTINGS_HOME", tmp_path / "settings")
    monkeypatch.delenv(settings.ENV_MODE, raising=False)
    monkeypatch.setattr(reflector, "_failed_until", {})
    return tmp_path


def _create_intent(name="uv-editable-reinstall", **kw):
    base = {
        "op": "create",
        "layer": "project",
        "name": name,
        "category": "tooling",
        "description": "Use when the dev venv loses agent_takkub — reinstall editable",
        "body": "- run `uv pip install --no-deps -e .`\n- never `uv run` while the cockpit runs",
        "reason": "user hit it twice",
        "evidence": EVIDENCE,
    }
    base.update(kw)
    return base


def _land(intents, **kw):
    return promoter.apply(
        intents, None, NS, max_intents=kw.pop("max_intents", 3), corpus=CORPUS, **kw
    )


def _foreign_skill(name: str, layer: str = "project") -> Path:
    d = store.skills_root(layer, NS) / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: x\ndescription: user skill\n---\nbody\n", encoding="utf-8"
    )
    return d


# ── settings ─────────────────────────────────────────────────────────────


def test_settings_defaults_and_coercion():
    s = settings.load()
    assert s.mode == "auto" and s.provider == "auto"
    settings.path().parent.mkdir(parents=True, exist_ok=True)
    settings.path().write_text(
        json.dumps({"mode": "bogus", "provider": "gemini", "daily_cap": -5, "timeout_s": 1}),
        encoding="utf-8",
    )
    s = settings.load()
    assert s.mode == "auto"  # unknown mode → default
    assert s.provider == "auto"  # gemini can't reflect (no TTY print mode)
    assert s.daily_cap == 0 and s.timeout_s == 30  # clamped, not discarded


def test_env_override_is_runtime_only(monkeypatch):
    settings.update(mode="propose")
    monkeypatch.setenv(settings.ENV_MODE, "off")
    assert settings.load().mode == "off"
    settings.update(max_intents=2)  # must not write the env "off" back to disk
    monkeypatch.delenv(settings.ENV_MODE)
    assert settings.load().mode == "propose"
    assert settings.load().max_intents == 2


# ── store ────────────────────────────────────────────────────────────────


def test_create_is_native_skill_with_hidden_sidecars():
    rep = _land([_create_intent()])
    assert [o.name for o in rep.landed] == ["uv-editable-reinstall"]
    d = config.project_skills_dir(NS) / "uv-editable-reinstall"
    text = (d / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: uv-editable-reinstall\ndescription: ")
    side = json.loads((d / ".sidecar.json").read_text(encoding="utf-8"))
    assert side["managed_by"] == MANAGED_BY and side["category"] == "tooling"
    ledger = store.read_ledger(d)
    assert ledger[0]["action"] == "create"
    assert (d / ledger[0]["evidence"]).read_text(encoding="utf-8").count(EVIDENCE) == 1
    assert not [p for p in d.parent.iterdir() if p.name.startswith(".tmp-")]


def test_foreign_skills_are_invisible_and_never_mutated():
    _foreign_skill("my-own")
    assert store.list_skills(NS) == []
    assert store.name_taken(NS, "my-own")
    rep = _land(
        [
            _create_intent(name="my-own"),
            {"op": "patch", "name": "my-own", "body": "x", "reason": "r", "evidence": EVIDENCE},
            {"op": "archive", "name": "my-own", "reason": "r", "evidence": EVIDENCE},
        ]
    )
    assert not rep.landed and len(rep.rejected) == 3
    assert (
        (store.skills_root("project", NS) / "my-own" / "SKILL.md")
        .read_text(encoding="utf-8")
        .endswith("body\n")
    )


def test_archive_then_revive_restarts_probation():
    _land([_create_intent()])
    rec = store.find_managed(NS, "uv-editable-reinstall")
    dest = store.archive_skill(rec, reason="test")
    assert dest.parent.name == store.ARCHIVE_DIR and dest.is_dir()
    assert store.find_managed(NS, "uv-editable-reinstall") is None
    store.tick_requests(NS)
    back = store.revive_skill(NS, "uv-editable-reinstall", requests_now=store.requests(NS))
    assert back.sidecar["requests_at_create"] == 1
    assert [e["action"] for e in store.read_ledger(back.dir)] == ["create", "archive", "revive"]


# ── promoter validation ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"name": "Bad Name"}, "invalid name"),
        ({"evidence": "this text never appeared anywhere in the episode"}, "verbatim"),
        ({"evidence": "short"}, "shorter than 20"),
        ({"description": "x" * 90 + " use when something"}, "trigger"),
        ({"description": "Use when " + "x" * 400}, "max 300"),
        ({"body": "\n".join(f"line {i}" for i in range(40))}, "non-blank lines"),
        ({"body": "token ghp_" + "a" * 30}, "secret"),
        ({"layer": "team"}, "layer"),
        ({"reason": ""}, "missing reason"),
    ],
)
def test_promoter_rejects(change, why):
    rep = _land([_create_intent(**change)])
    assert not rep.landed
    assert why in rep.rejected[0].reason


def test_change_cap_per_run():
    rep = _land([_create_intent(name=f"skill-{i}") for i in range(5)], max_intents=2)
    assert len(rep.landed) == 2
    assert all("cap" in o.reason for o in rep.rejected)


def test_curator_may_not_create():
    rep = _land([_create_intent()], curator=True)
    assert "curator may not create" in rep.rejected[0].reason


def test_merge_folds_usage_and_records_umbrella():
    _land([_create_intent(name="aaa-umbrella"), _create_intent(name="bbb-narrow")])
    narrow = store.find_managed(NS, "bbb-narrow")
    store.update_sidecar(narrow, uses=3, views=1)
    rep = _land(
        [
            {
                "op": "merge",
                "name": "bbb-narrow",
                "into": "aaa-umbrella",
                "body": "- merged rule",
                "reason": "same scenario",
                "evidence": EVIDENCE,
            }
        ]
    )
    assert rep.landed[0].into == "aaa-umbrella"
    umbrella = store.find_managed(NS, "aaa-umbrella")
    assert umbrella.sidecar["uses"] == 3 and umbrella.sidecar["patches"] == 1
    assert store.find_managed(NS, "bbb-narrow") is None
    archived = store.list_archived(NS)
    assert archived[0][0] == "bbb-narrow"
    assert store.read_ledger(archived[0][2])[-1]["absorbed_by"] == "aaa-umbrella"


def test_merge_into_unknown_umbrella_fails_whole_intent():
    _land([_create_intent(name="bbb-narrow")])
    rep = _land(
        [
            {
                "op": "merge",
                "name": "bbb-narrow",
                "into": "ghost",
                "body": "x",
                "reason": "r",
                "evidence": EVIDENCE,
            }
        ]
    )
    assert "umbrella" in rep.rejected[0].reason
    assert store.find_managed(NS, "bbb-narrow") is not None  # content not lost


# ── lifecycle ────────────────────────────────────────────────────────────


def test_detect_usage_note_tool_and_view():
    names = {"aaa", "bbb", "ccc"}
    note = "fixed it [skill: aaa] and [skill: zzz]"
    transcript = (
        '[tool_use Skill] {"skill": "bbb"}\nRead C:\\data\\project-skills\\proj\\ccc\\SKILL.md\n'
    )
    used, viewed = lifecycle.detect_usage(names, note, transcript)
    assert used == {"aaa", "bbb"} and viewed == {"ccc"}


def test_probation_protects_then_graduation_archives_unused():
    s = settings.LearningSettings(maturity_project=5)
    _land([_create_intent(name="unused-one"), _create_intent(name="used-one")])
    lifecycle.record_usage(NS, {"used-one"}, set())
    for _ in range(4):
        store.tick_requests(NS)
    assert lifecycle.review(NS, s) == []  # still in probation
    store.tick_requests(NS)
    retired = lifecycle.review(NS, s)
    assert [r.name for r in retired] == ["unused-one"]
    assert {r.name for r in store.list_skills(NS)} == {"used-one"}


def test_capacity_evicts_lowest_usage_rate():
    s = settings.LearningSettings(maturity_project=5, capacity_project=1)
    _land([_create_intent(name="low-rate"), _create_intent(name="high-rate")])
    lifecycle.record_usage(NS, {"low-rate", "high-rate"}, set())
    lifecycle.record_usage(NS, {"high-rate"}, set())
    for _ in range(6):
        store.tick_requests(NS)
    retired = lifecycle.review(NS, s)
    assert [r.name for r in retired] == ["low-rate"] and "capacity" in retired[0].reason


# ── reflector ────────────────────────────────────────────────────────────


def test_parse_intents_fenced_bare_and_garbage():
    assert reflector.parse_intents('sure!\n```json\n{"intents": [{"op": "none"}]}\n```') == [
        {"op": "none"}
    ]
    assert reflector.parse_intents('{"intents": []}') == []
    assert reflector.parse_intents("no json here") is None
    assert reflector.parse_intents('{"other": 1}') is None


def test_choose_provider_prefers_pane_then_order(monkeypatch):
    ready = {"codex", "opencode"}
    monkeypatch.setattr(reflector, "provider_ready", lambda p: p in ready)
    s = settings.LearningSettings()
    assert reflector.choose_provider(s) == "codex"
    assert reflector.choose_provider(s, prefer="opencode") == "opencode"
    assert reflector.choose_provider(s, prefer="gemini") == "codex"  # gemini never reflects
    assert reflector.choose_provider(settings.LearningSettings(provider="claude")) is None


@pytest.mark.parametrize("provider", ["claude", "codex", "opencode", "cursor"])
def test_build_argv_every_reflector_provider(provider):
    argv = reflector.build_argv(provider, "/bin/x", "PROMPT", "m1")
    assert argv[0] == "/bin/x" and "m1" in argv
    # codex reads its prompt from stdin (`-`); the others take it as an arg
    assert ("-" in argv) if provider == "codex" else ("PROMPT" in argv)
    if provider == "claude":
        assert "Write" in argv and "Bash" in argv  # write tools denied
    if provider == "codex":
        assert "read-only" in argv and "--skip-git-repo-check" in argv


def test_reflector_env_is_a_pane_env_not_the_raw_cockpit_env(monkeypatch):
    # A cockpit started from a shell inside Claude Code inherits the parent's
    # bridge vars; raw-env `claude -p` then hits the parent's dead socket
    # (ECONNREFUSED, live 2026-10-02). A leaked API key must not ride along.
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", r"\\.\pipe\dead")
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leaked")
    for provider in ("claude", "codex", "opencode", "cursor"):
        env = reflector.reflector_env(provider, NS)
        assert env[reflector.CHILD_ENV] == "1"
        assert "PATH" in {k.upper() for k in env}
        for leaked in (
            "CLAUDE_CODE_MESSAGING_SOCKET",
            "CLAUDE_CODE_CHILD_SESSION",
            "ANTHROPIC_API_KEY",
        ):
            assert leaked not in env, (provider, leaked)


def test_build_argv_rejects_gemini():
    with pytest.raises(ValueError):
        reflector.build_argv("gemini", "agy", "p", "")


# ── episode ──────────────────────────────────────────────────────────────


def test_episode_from_ingest_filters_by_assign_time_and_redacts(monkeypatch):
    from agent_takkub.core.conversation.ingest.base import IngestBatch, IngestedMessage
    from agent_takkub.core.models.conversation import MessageRole

    msgs = [
        IngestedMessage(MessageRole.USER, "old task from before", 100.0),
        IngestedMessage(MessageRole.USER, "fix the login flow", 1000.0),
        IngestedMessage(MessageRole.ASSISTANT, "done, key sk-ant-" + "x" * 30, 1001.0),
        IngestedMessage(MessageRole.USER, "no, that's wrong — use the refresh token", 1002.0),
    ]
    fake = SimpleNamespace(
        resolve_source=lambda cwd, sid: "src",
        read_new=lambda src, cur: IngestBatch("src", msgs, "9"),
    )
    import agent_takkub.core.conversation.ingest as ingest

    monkeypatch.setattr(ingest, "adapter_for", lambda p: fake)
    ev = episode.DoneEvent(NS, "backend", "codex", cwd="/w", assigned_at=1000.0, failed=True)
    ep = episode.build(ev)
    assert ep.source == "ingest:codex" and ep.message_count == 3
    assert "old task" not in ep.transcript
    assert "sk-ant-" not in ep.transcript
    assert ep.corrections == 1 and ep.worth >= 5


def test_episode_falls_back_to_pty_transcript(tmp_path, monkeypatch):
    import agent_takkub.core.conversation.ingest as ingest

    monkeypatch.setattr(ingest, "adapter_for", lambda p: None)
    log = tmp_path / "backend-101010.transcript.log"
    log.write_bytes(b"\x1b[32mhello from opencode\x1b[0m\nsecond line\n")
    ep = episode.build(episode.DoneEvent(NS, "backend", "opencode", pty_transcript=str(log)))
    assert ep.source == "pty" and "hello from opencode" in ep.transcript


# ── pipeline ─────────────────────────────────────────────────────────────


def _event(note=EVIDENCE, failed=True):
    return episode.DoneEvent(NS, "backend", "claude", task="task text", note=note, failed=failed)


def _fake_runner(reply: dict, calls: list):
    def run(argv, cwd, timeout, env, stdin_text):
        assert env.get(reflector.CHILD_ENV) == "1"
        calls.append((argv, Path(cwd)))
        assert (Path(cwd) / "episode.md").is_file()
        return True, "```json\n" + json.dumps(reply) + "\n```"

    return run


@pytest.fixture()
def _ready(monkeypatch):
    monkeypatch.setattr(reflector, "provider_ready", lambda p: p == "claude")
    monkeypatch.setattr(reflector, "_binary", lambda p: "claude-bin")
    # A transcript source so the gate doesn't skip with "no transcript".
    monkeypatch.setattr(
        episode,
        "_from_ingest",
        lambda ev: ([("user", ev.task), ("assistant", ev.note)], "ingest:claude"),
    )


def test_pipeline_auto_lands_and_notifies(_ready):
    calls, notes = [], []
    s = settings.LearningSettings(min_worth=0)
    rec = pipeline.on_done(
        _event(),
        notify=lambda ns, line: notes.append(line),
        runner=_fake_runner({"intents": [_create_intent()]}, calls),
        settings=s,
    )
    assert rec.status == "landed" and rec.provider == "claude"
    assert store.find_managed(NS, "uv-editable-reinstall") is not None
    assert calls[0][0][0] == "claude-bin"
    assert notes and "uv-editable-reinstall" in notes[0]
    bundle = (calls[0][1] / "episode.md").read_text(encoding="utf-8")
    assert "Skills ที่ระบบเรียนรู้ไว้" in bundle and EVIDENCE in bundle


def test_pipeline_propose_then_approve(_ready):
    s = settings.LearningSettings(min_worth=0, mode="propose")
    rec = pipeline.on_done(
        _event(), runner=_fake_runner({"intents": [_create_intent()]}, []), settings=s
    )
    assert rec.status == "proposed" and store.list_skills(NS) == []
    assert pipeline.pending(NS)[0]["run_id"] == rec.run_id
    done = pipeline.approve(NS, rec.run_id, settings=s)
    assert done.status == "landed" and pipeline.pending(NS) == []


def test_pipeline_off_mode_counts_but_never_reflects(_ready):
    calls = []
    s = settings.LearningSettings(mode="off", min_worth=0)
    rec = pipeline.on_done(_event(), runner=_fake_runner({"intents": []}, calls), settings=s)
    assert rec.skip_reason == "mode off" and not calls
    assert store.requests(NS) == 1 and store.requests(None) == 1


def test_pipeline_cooldown_and_worth_gate(_ready):
    calls = []
    s = settings.LearningSettings(min_worth=0, min_interval_s=3600)
    runner = _fake_runner({"intents": []}, calls)
    assert pipeline.on_done(_event(), runner=runner, settings=s).status == "empty"
    second = pipeline.on_done(_event(), runner=runner, settings=s)
    assert "cooldown" in second.skip_reason and len(calls) == 1
    picky = settings.LearningSettings(min_worth=99, min_interval_s=0)
    assert "worth" in pipeline.on_done(_event(), runner=runner, settings=picky).skip_reason


def test_forced_reflection_always_answers(_ready):
    notes = []
    s = settings.LearningSettings(min_worth=99)
    rec = pipeline.on_done(
        _event(),
        notify=lambda ns, line: notes.append(line),
        runner=_fake_runner({"intents": []}, []),
        force=True,
        settings=s,
    )
    assert rec.status == "empty" and notes == ["🧠 Skill Learning (backend): ไม่มีบทเรียนใหม่"]


def test_pipeline_records_failed_reflector(_ready):
    s = settings.LearningSettings(min_worth=0)
    rec = pipeline.on_done(
        _event(), runner=lambda a, c, t, e, i: (False, "rate limited"), settings=s
    )
    assert rec.status == "failed" and "rate limited" in rec.error


def test_falls_back_to_next_provider_and_cools_the_failed_one(monkeypatch):
    # This machine, 2026-10-02: claude's settings.json pointed at a local
    # proxy that was down → ECONNREFUSED after ~3 min. Learning must go on
    # through whatever else works, and not pay that wait on every done.
    monkeypatch.setattr(reflector, "provider_ready", lambda p: p in ("claude", "codex"))
    monkeypatch.setattr(reflector, "_binary", lambda p: f"{p}-bin")
    monkeypatch.setattr(
        episode,
        "_from_ingest",
        lambda ev: ([("user", ev.task), ("assistant", ev.note)], "ingest:claude"),
    )
    tried = []

    inputs = []

    def runner(argv, cwd, timeout, env, stdin_text):
        tried.append(argv[0])
        inputs.append(stdin_text or "")
        if argv[0] == "claude-bin":
            return False, "API Error: Connection refused (ECONNREFUSED)"
        return True, json.dumps({"intents": [_create_intent()]})

    s = settings.LearningSettings(min_worth=0, min_interval_s=0)
    rec = pipeline.on_done(_event(), runner=runner, settings=s)
    assert tried == ["claude-bin", "codex-bin"]
    assert all(EVIDENCE in i for i in inputs)  # bundle rides stdin — no file-read tool needed
    assert rec.status == "landed" and rec.provider == "codex"
    # claude is cooling: next run goes to codex first, claude only as last resort
    assert reflector.candidates(s, prefer="claude") == ["codex", "claude"]


def test_usage_is_recorded_from_done_note(_ready):
    s = settings.LearningSettings(min_worth=0)
    pipeline.on_done(_event(), runner=_fake_runner({"intents": [_create_intent()]}, []), settings=s)
    s2 = settings.LearningSettings(mode="off")
    pipeline.on_done(_event(note="ok [skill: uv-editable-reinstall]"), settings=s2)
    assert store.find_managed(NS, "uv-editable-reinstall").sidecar["uses"] == 1


def test_curate_snapshots_and_merges(_ready):
    _land([_create_intent(name="aaa-umbrella"), _create_intent(name="bbb-narrow")])
    reply = {
        "intents": [
            {
                "op": "merge",
                "name": "bbb-narrow",
                "into": "aaa-umbrella",
                "body": "- merged",
                "reason": "dup",
                "evidence": "never `uv run` while the cockpit runs",
            }
        ]
    }
    rec = pipeline.curate(NS, runner=_fake_runner(reply, []), settings=settings.LearningSettings())
    assert rec.status == "landed"
    assert list((store.state_dir(NS) / "snapshots").glob("*-project.tar.gz"))


# ── index ────────────────────────────────────────────────────────────────


def test_index_empty_library_costs_nothing():
    assert index.render(NS, "claude") == ""


def test_index_paths_per_provider_discovery():
    _land([_create_intent(name="proj-skill"), _create_intent(name="glob-skill", layer="global")])
    claude = index.render(NS, "claude")
    codex = index.render(NS, "codex")
    gemini = index.render(NS, "gemini")
    assert "`proj-skill`" in claude and "SKILL.md" not in claude  # claude discovers both layers
    assert "[skill: <name>]" in claude
    # codex's native home carries global skills only → project skill needs its path
    assert codex.count("SKILL.md") == 1 and "proj-skill" in codex.split("SKILL.md")[0]
    assert gemini.count("SKILL.md") == 2  # no native discovery → every path


def test_lead_index_carries_last_run(_ready):
    s = settings.LearningSettings(min_worth=0)
    pipeline.on_done(_event(), runner=_fake_runner({"intents": [_create_intent()]}, []), settings=s)
    assert "รอบล่าสุด" in index.render(NS, "lead", for_lead=True)


# ── seams ────────────────────────────────────────────────────────────────


def test_project_links_skip_archive_and_prune_dangling(tmp_path):
    from agent_takkub import skill_scan

    project_root = tmp_path / "repo"
    project_root.mkdir()
    _land([_create_intent(name="keep-me"), _create_intent(name="gone-soon")])
    assert skill_scan.ensure_project_skill_links(project_root, NS) == []
    links = project_root / ".claude" / "skills"
    assert {p.name for p in links.iterdir()} == {"keep-me", "gone-soon"}
    store.archive_skill(store.find_managed(NS, "gone-soon"), reason="t")
    assert skill_scan.ensure_project_skill_links(project_root, NS) == []
    names = {p.name for p in links.iterdir()}
    assert names == {"keep-me"}  # no `.archive` link, no dangling `gone-soon`


def test_learning_report_links_new_skill_into_every_project_path_816(tmp_path, monkeypatch):
    from agent_takkub import lead_context
    from agent_takkub import skill_learning_mixin as mix

    api, web = tmp_path / "api", tmp_path / "web"
    api.mkdir()
    web.mkdir()
    monkeypatch.setattr(lead_context, "_allowed_project_roots", lambda _ns: [api, web])
    _land([_create_intent(name="local-e2e-verify")])

    class _Orch(mix.SkillLearningMixin):
        def _notify_lead(self, *a, **kw):
            pass

    _Orch()._on_skill_learning_report(NS, "🧠 Skill Learning (qa): create local-e2e-verify")
    for root in (api, web):
        assert (root / ".claude" / "skills" / "local-e2e-verify" / "SKILL.md").is_file()


def test_orchestrator_hook_is_inert_under_skip_env(monkeypatch):
    from agent_takkub import skill_learning_mixin as mix

    submitted = []
    monkeypatch.setattr(pipeline, "submit", lambda ev, **kw: submitted.append(ev))
    obj = mix.SkillLearningMixin()
    obj.skillLearningReport = SimpleNamespace(emit=lambda *a: None)
    kwargs = dict(
        task="t",
        note="n",
        failed=False,
        provider="codex",
        cwd="/w",
        session_id="s",
        pty_transcript="",
        assigned_at=1.0,
    )
    monkeypatch.setenv(mix.SKIP_ENV, "1")
    obj._skill_learning_on_done(NS, "backend", **kwargs)
    assert submitted == []
    monkeypatch.delenv(mix.SKIP_ENV)
    obj._skill_learning_on_done(NS, "backend", **kwargs)
    assert submitted[0].provider == "codex" and submitted[0].role == "backend"
    assert obj._skill_learning_last[(NS, "backend")] is submitted[0]


def test_reflector_child_env_disables_hook(monkeypatch):
    from agent_takkub import skill_learning_mixin as mix

    monkeypatch.delenv(mix.SKIP_ENV, raising=False)
    monkeypatch.setenv(reflector.CHILD_ENV, "1")
    assert mix._disabled()
    assert os.environ[reflector.CHILD_ENV] == "1"


# ── #803: fallback honours the quota policy, failure names every provider ──


def test_candidates_honour_park_and_excluded_providers(monkeypatch):
    monkeypatch.setattr(reflector, "provider_ready", lambda p: True)
    s = settings.LearningSettings()
    monkeypatch.setattr(reflector, "_switch_policy", lambda ns: (False, {"codex"}))
    assert reflector.candidates(s, prefer="claude") == ["claude", "opencode", "cursor"]
    monkeypatch.setattr(reflector, "_switch_policy", lambda ns: (True, set()))
    assert reflector.candidates(s, prefer="claude") == ["claude"]
    assert reflector.candidates(s, prefer="opencode") == ["opencode"]
    assert reflector.candidates(s, prefer="gemini") == ["claude"]  # no own reflector → claude
    # park + the pane's own provider excluded → nothing, never a forbidden one
    monkeypatch.setattr(reflector, "_switch_policy", lambda ns: (True, {"claude"}))
    assert reflector.candidates(s, prefer="claude") == []
    # pinned = explicit user choice, policy doesn't apply
    assert reflector.candidates(settings.LearningSettings(provider="codex")) == ["codex"]


def test_switch_policy_reads_quota_policy_file(tmp_path):
    (tmp_path / "settings").mkdir(parents=True, exist_ok=True)
    (tmp_path / "settings" / "quota-policy.json").write_text(
        json.dumps({"policy": "park", "exclude_providers": ["Codex"]}), encoding="utf-8"
    )
    assert reflector._switch_policy("") == (True, {"codex"})


def test_failed_run_summary_names_every_provider_tried(monkeypatch):
    monkeypatch.setattr(reflector, "provider_ready", lambda p: p in ("claude", "codex"))
    monkeypatch.setattr(reflector, "_switch_policy", lambda ns: (False, set()))
    monkeypatch.setattr(reflector, "_binary", lambda p: f"{p}-bin")
    monkeypatch.setattr(episode, "_from_ingest", lambda ev: ([("user", ev.task)], "ingest:claude"))
    errors = {
        "claude-bin": '[claude-code:unrecognized_model] {"model":"ocg/deepseek-v4-pro"}',
        "codex-bin": "You've hit your usage limit. Try again Oct 4.",
    }
    s = settings.LearningSettings(min_worth=0, min_interval_s=0)
    rec = pipeline.on_done(_event(), runner=lambda a, c, t, e, i: (False, errors[a[0]]), settings=s)
    assert rec.status == "failed"
    assert rec.summary == "reflector failed (claude: unrecognized_model · codex: quota)"


# ── #804: claude JSONL is found in the pane's own config/project dir ──────


def test_claude_adapter_resolves_cockpit_project_dir(tmp_path):
    from agent_takkub.core.conversation.ingest import claude_adapter

    cfg = tmp_path / "profile"
    proj = cfg / "projects" / "takkub-project-proj"
    proj.mkdir(parents=True)
    (proj / "abc-123.jsonl").write_text("{}\n", encoding="utf-8")
    got = claude_adapter.resolve_source(
        "/w", "abc-123", config_dir=str(cfg), project_dir_name="takkub-project-proj"
    )
    assert got == str(proj / "abc-123.jsonl")


def test_episode_passes_pane_claude_dirs_and_prefers_exact_session(monkeypatch):
    import agent_takkub.core.conversation.ingest as ingest
    from agent_takkub.core.conversation.ingest.base import IngestBatch, IngestedMessage
    from agent_takkub.core.models.conversation import MessageRole

    calls = []

    def resolve(cwd, sid, *, config_dir=None, project_dir_name=None):
        calls.append((config_dir, project_dir_name))
        # first location only has a sibling's newer file; the exact one is in the 2nd
        return "/x/other.jsonl" if len(calls) == 1 else f"/y/{sid}.jsonl"

    fake = SimpleNamespace(
        resolve_source=resolve,
        read_new=lambda src, cur: IngestBatch(
            src, [IngestedMessage(MessageRole.USER, f"from {src}", None)], "1"
        ),
    )
    monkeypatch.setattr(ingest, "adapter_for", lambda p: fake)
    ev = episode.DoneEvent(
        NS,
        "lead",
        "claude",
        cwd="/w",
        session_id="sess-1",
        claude_config_dir="/cfg",
        claude_project_dir_name="takkub-project-proj",
    )
    ep = episode.build(ev)
    assert calls[0] == ("/cfg", "takkub-project-proj")
    assert ep.source == "ingest:claude" and "/y/sess-1.jsonl" in ep.transcript


def test_pty_fallback_keeps_thai_marks_and_drops_spinner(tmp_path, monkeypatch):
    import agent_takkub.core.conversation.ingest as ingest

    monkeypatch.setattr(ingest, "adapter_for", lambda p: None)
    log = tmp_path / "lead-101010.transcript.log"
    log.write_bytes(
        "เจอแล้วว่าไฟล์ settings ของ Claude map ไปที่ gateway\n"
        "✻ Contemplating… (12s · esc to interrupt)\n"
        "  ? for shortcuts    ← for agents\n".encode()
    )
    ep = episode.build(episode.DoneEvent(NS, "lead", "claude", pty_transcript=str(log)))
    assert "เจอแล้วว่าไฟล์ settings ของ Claude map ไปที่ gateway" in ep.transcript
    assert "Contemplating" not in ep.transcript and "for shortcuts" not in ep.transcript


def test_hook_carries_pane_claude_dirs(monkeypatch):
    from agent_takkub import skill_learning_mixin as mix

    pane = SimpleNamespace(
        session=SimpleNamespace(_claude_config_dir="/cfg", _claude_project_dir_name="takkub-p")
    )
    assert mix.claude_session_dirs(pane) == {
        "claude_config_dir": "/cfg",
        "claude_project_dir_name": "takkub-p",
    }
    assert mix.claude_session_dirs(SimpleNamespace(session=None)) == {
        "claude_config_dir": "",
        "claude_project_dir_name": "",
    }
