"""Regression tests for the 2026-11-04 maintenance batch (#970, #973, #975,
#976, #977). Each class pins the behavior the fix added."""

from __future__ import annotations

import json
import os
import subprocess
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agent_takkub import gpu_fallback, graft_autobuild, report_builder
from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator, PaneState
from agent_takkub.worktree_manager import WorktreeManager

# ---------------------------------------------------------------- #976


def _git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo_with_detached(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    det = tmp_path / "outside" / "det"
    _git("init", "-q", str(repo))
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    _git("worktree", "add", "-q", "--detach", str(det), cwd=repo)
    return repo, det


class TestWorktreeCleanPath976:
    def test_detached_outside_managed_root_is_invisible_without_path(self, repo_with_detached):
        repo, _det = repo_with_detached
        assert WorktreeManager().list_isolated(str(repo)) == []

    def test_path_removes_clean_detached_worktree(self, repo_with_detached):
        repo, det = repo_with_detached
        lines = WorktreeManager().clean_isolated(str(repo), path=str(det))
        assert len(lines) == 1 and lines[0].startswith("REMOVED"), lines
        assert not det.exists()
        listed = subprocess.run(
            ["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True
        ).stdout
        assert len(listed.strip().splitlines()) == 1, listed

    def test_path_keeps_detached_with_unmerged_commit_even_with_force(self, repo_with_detached):
        repo, det = repo_with_detached
        (det / "x.txt").write_text("x", encoding="utf-8")
        _git("add", "x.txt", cwd=det)
        _git("commit", "-qm", "work", cwd=det)
        lines = WorktreeManager().clean_isolated(str(repo), path=str(det), force=True)
        assert lines[0].startswith("KEEP"), lines
        assert det.exists()

    def test_path_never_matches_main_worktree(self, repo_with_detached):
        repo, _det = repo_with_detached
        lines = WorktreeManager().clean_isolated(str(repo), path=str(repo))
        assert lines[0].startswith("NOT FOUND"), lines
        assert repo.exists()

    def test_path_match_is_spelling_insensitive(self, repo_with_detached):
        repo, det = repo_with_detached
        spelled = str(det).replace("/", os.sep)
        if os.name == "nt":
            spelled = spelled.upper()
        lines = WorktreeManager().clean_isolated(str(repo), path=spelled)
        assert lines[0].startswith("REMOVED"), lines

    def test_cli_parser_accepts_path(self):
        from agent_takkub import cli
        from agent_takkub.cli_parser import build_parser

        ns = build_parser(cli).parse_args(["worktree", "clean", "--path", "/tmp/x"])
        assert ns.path == "/tmp/x"

    def test_guard_message_points_at_path(self):
        from agent_takkub.pane_guard import RAW_WORKTREE_REMOVE_RULE_TEXT

        assert "--path" in RAW_WORKTREE_REMOVE_RULE_TEXT


# ---------------------------------------------------------------- #975


def _fake_playwright(root: Path) -> Path:
    pw = root / "node_modules" / "playwright"
    pw.mkdir(parents=True)
    (pw / "package.json").write_text("{}", encoding="utf-8")
    return pw


class TestNodePlaywrightDiscovery975:
    def _isolate(self, monkeypatch, tmp_path):
        for var in (
            "npm_config_cache",
            "NPM_CONFIG_CACHE",
            "npm_config_prefix",
            "NPM_CONFIG_PREFIX",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
        monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
        monkeypatch.setattr(report_builder, "_npm_global_roots", lambda: [tmp_path / "global"])
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    def test_finds_newest_npx_cache_entry(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        npx = tmp_path / "local" / "npm-cache" / "_npx"
        old = _fake_playwright(npx / "aaa")
        new = _fake_playwright(npx / "bbb")
        os.utime(old / "package.json", (1, 1))
        os.utime(old, (1, 1))
        assert report_builder.find_node_playwright() == new

    def test_global_root_wins_over_npx(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        _fake_playwright(tmp_path / "local" / "npm-cache" / "_npx" / "aaa")
        glob_pw = tmp_path / "global" / "playwright"
        glob_pw.mkdir(parents=True)
        (glob_pw / "package.json").write_text("{}", encoding="utf-8")
        assert report_builder.find_node_playwright() == glob_pw

    def test_none_when_nothing_installed(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        assert report_builder.find_node_playwright() is None

    def test_node_check_passes_found_playwright(self, monkeypatch, tmp_path):
        found = tmp_path / "pw"
        monkeypatch.setattr(report_builder, "find_node_playwright", lambda: found)
        monkeypatch.setattr("shutil.which", lambda name: "node")
        script_dir = tmp_path / "kit"
        script_dir.mkdir()
        (script_dir / "mobile-check.cjs").write_text("//", encoding="utf-8")
        monkeypatch.setattr(report_builder, "report_asset_root", lambda: script_dir)
        seen: list[list[str]] = []

        def fake_run(cmd, **_kw):
            seen.append(cmd)
            return types.SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"360": {"scrollWidth": 360, "screenshot": "s.png"}}),
                stderr="",
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        html = tmp_path / "r.html"
        html.write_text("<p>x</p>", encoding="utf-8")
        ok, lines = report_builder._check_with_node(html, tmp_path / "out")
        assert ok and "OK" in lines[0]
        assert seen[0][-2:] == ["--playwright", str(found.resolve())]

    def test_explicit_playwright_skips_python_path(self, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr(
            report_builder,
            "_check_with_node",
            lambda src, out, pw=None: calls.append(pw) or (True, []),
        )
        html = tmp_path / "r.html"
        html.write_text("<p>x</p>", encoding="utf-8")
        report_builder.check_report_file(str(html), str(tmp_path / "o"), playwright="/x/pw")
        assert calls == ["/x/pw"]


# ---------------------------------------------------------------- #977 GPU


_EVENT_XML = (
    "<Event xmlns='x'><System><EventID>1000</EventID></System><EventData>"
    "<Data Name='AppName'>pythonw.exe</Data><Data Name='ModuleName'>d3d11.dll</Data>"
    "<Data Name='ProcessId'>0x30cc</Data></EventData></Event>"
    "<Event xmlns='x'><System><EventID>1000</EventID></System><EventData>"
    "<Data Name='AppName'>pythonw.exe</Data><Data Name='ModuleName'>python311.dll</Data>"
    "<Data Name='ProcessId'>0xa50</Data></EventData></Event>"
)


class TestGpuFallback977:
    def test_previous_run_crash_between_markers(self):
        log = "--- boot 7 ---\nx\nWindows fatal exception: access violation\n\n--- boot 9 ---\n"
        assert gpu_fallback.previous_run_crash(log) == (True, 7)

    def test_previous_run_crash_when_rotation_cut_the_marker(self):
        log = "--- (boot.log rotated) ---\nWindows fatal exception: access violation\n--- boot 9 ---\n"
        assert gpu_fallback.previous_run_crash(log) == (True, None)

    def test_clean_previous_run(self):
        assert gpu_fallback.previous_run_crash("--- boot 1 ---\nok\n--- boot 2 ---\n") == (
            False,
            None,
        )

    def test_parse_event_xml_and_match_pid(self):
        evs = gpu_fallback.parse_app_error_events(_EVENT_XML)
        assert [e["ModuleName"] for e in evs] == ["d3d11.dll", "python311.dll"]

        def run(*_a, **_k):
            return types.SimpleNamespace(stdout=_EVENT_XML)

        if os.name == "nt":
            assert gpu_fallback.faulting_module_for_pid(0x30CC, _run=run) == "d3d11.dll"
            assert gpu_fallback.faulting_module_for_pid(0xA50, _run=run) == "python311.dll"
            assert gpu_fallback.faulting_module_for_pid(1, _run=run) is None
        else:
            assert gpu_fallback.faulting_module_for_pid(0x30CC, _run=run) is None

    def _log(self, tmp_path: Path) -> Path:
        p = tmp_path / "boot.log"
        p.write_text(
            "--- boot 12492 ---\nWindows fatal exception: access violation\n--- boot 26200 ---\n",
            encoding="utf-8",
        )
        return p

    def test_gpu_crash_switches_to_software_and_persists(self, tmp_path):
        log = self._log(tmp_path)
        d = gpu_fallback.decide(
            tmp_path, log, env={}, now=1000.0, _module_lookup=lambda pid: "d3d11.dll"
        )
        assert d.software
        names = [e[0] for e in d.events]
        assert names == ["native_crash_detected", "gpu_software_fallback"]
        marker = json.loads((tmp_path / "gpu-software-fallback.json").read_text(encoding="utf-8"))
        assert marker["crashed_pid"] == 12492
        # next clean boot, marker still in force; same crash is not re-reported
        log.write_text("--- boot 1 ---\n--- boot 2 ---\n", encoding="utf-8")
        d2 = gpu_fallback.decide(tmp_path, log, env={}, now=2000.0)
        assert d2.software and [e[0] for e in d2.events] == ["gpu_software_fallback"]
        # expires after FALLBACK_DAYS
        later = 1000.0 + gpu_fallback.FALLBACK_DAYS * 86400 + 1
        assert not gpu_fallback.decide(tmp_path, log, env={}, now=later).software

    def test_non_gpu_crash_does_not_switch(self, tmp_path):
        d = gpu_fallback.decide(
            tmp_path, self._log(tmp_path), env={}, _module_lookup=lambda pid: "python311.dll"
        )
        assert not d.software
        assert d.events == [
            (
                "native_crash_detected",
                {"crashed_pid": 12492, "module": "python311.dll", "gpu_module": False},
            )
        ]

    def test_env_override_wins_both_ways(self, tmp_path):
        log = self._log(tmp_path)
        lookup = lambda pid: "d3d11.dll"  # noqa: E731
        assert not gpu_fallback.decide(
            tmp_path, log, env={"TAKKUB_FORCE_SOFTWARE_GPU": "0"}, _module_lookup=lookup
        ).software
        clean = tmp_path / "clean.log"
        clean.write_text("--- boot 1 ---\n", encoding="utf-8")
        (tmp_path / "gpu-software-fallback.json").unlink()
        assert gpu_fallback.decide(tmp_path, clean, env={"TAKKUB_FORCE_SOFTWARE_GPU": "1"}).software


# ---------------------------------------------------------------- #977 Lead resume


class TestCrashResumeLead977:
    @pytest.fixture
    def snap_file(self, tmp_path, monkeypatch) -> Path:
        f = tmp_path / "last-session.json"
        monkeypatch.setattr(orch_mod, "_LAST_SESSION_FILE", f)
        return f

    def _write(self, f: Path, **kw) -> None:
        d = {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "pid": 1,
            "leads": {"proj": {"session_uuid": "uuid-1", "cwd": "/c", "provider": "claude"}},
        }
        d.update(kw)
        f.write_text(json.dumps(d), encoding="utf-8")

    def _fresh(self) -> types.SimpleNamespace:
        s = types.SimpleNamespace()
        s._crash_resume_leads = Orchestrator._load_crash_resume_leads(s)  # type: ignore[arg-type]
        return s

    def test_crash_snapshot_resumes_once(self, snap_file):
        self._write(snap_file, clean_shutdown=False)
        s = self._fresh()
        assert Orchestrator.take_crash_resume_lead(s, "proj") == "uuid-1"  # type: ignore[arg-type]
        assert Orchestrator.take_crash_resume_lead(s, "proj") is None  # type: ignore[arg-type]
        assert s._crash_resume_uuids == {"uuid-1"}

    @pytest.mark.parametrize(
        "kw",
        [
            {"clean_shutdown": True},
            {},  # snapshot written before #977: never guess a crash
            {"clean_shutdown": False, "pid": os.getpid()},
            {
                "clean_shutdown": False,
                "saved_at": (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds"),
            },
        ],
    )
    def test_no_resume(self, snap_file, kw):
        self._write(snap_file, **kw)
        assert Orchestrator.take_crash_resume_lead(self._fresh(), "proj") is None  # type: ignore[arg-type]

    def test_snapshot_records_clean_flag_and_leads(self):
        s = types.SimpleNamespace(_governor_snapshot_fields=lambda: (None, None, False))
        out = Orchestrator._finish_snapshot(  # type: ignore[arg-type]
            s, {"projects": {}, "working_panes": [], "leads": {"p": {"session_uuid": "u"}}}
        )
        assert out["clean_shutdown"] is False and out["leads"] == {"p": {"session_uuid": "u"}}
        Orchestrator.mark_clean_shutdown(s)  # type: ignore[arg-type]
        assert (
            Orchestrator._finish_snapshot(s, {"projects": {}, "working_panes": []})[  # type: ignore[arg-type]
                "clean_shutdown"
            ]
            is True
        )

    def test_failed_crash_resume_falls_back_to_fresh_spawn(self, monkeypatch):
        from agent_takkub.spawn_engine import SpawnEngineMixin

        calls: list = []

        class _Fake:
            _crash_resume_uuids: set

            def _resolve_project(self, p):
                return p or "proj"

            def take_crash_resume_lead(self, project_ns):
                self._crash_resume_uuids = {"uuid-1"}
                return "uuid-1"

            def _spawn_once(self, role_name, **kw):
                calls.append(kw.get("resume_uuid"))
                if kw.get("resume_uuid"):
                    return False, "resume_uuid does not match cwd for lead"
                return True, "spawned"

        monkeypatch.setattr(
            "agent_takkub.spawn_engine._from_orch", lambda name: lambda *a, **k: None
        )
        ok, msg = SpawnEngineMixin.spawn(_Fake(), "lead", project="proj")  # type: ignore[arg-type]
        assert ok and msg == "spawned"
        assert calls == ["uuid-1", None]


# ---------------------------------------------------------------- #977 wedge / #973 / #970


class TestMaintenanceClassifies977:
    def test_new_events_are_severe(self):
        from agent_takkub import maintenance

        assert "main_thread_wedged" in maintenance._SEVERE_EVENTS
        assert "native_crash_detected" in maintenance._SEVERE_EVENTS


class TestGraftYieldsGil973:
    def test_yield_every_n_items(self, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr(graft_autobuild.time, "sleep", sleeps.append)
        for i in range(graft_autobuild._GIL_YIELD_EVERY * 3 + 1):
            graft_autobuild._yield_gil(i)
        assert sleeps == [graft_autobuild._GIL_YIELD_S] * 3

    def test_stage_files_yields(self, monkeypatch, tmp_path):
        n = graft_autobuild._GIL_YIELD_EVERY * 2 + 1
        src = tmp_path / "src"
        src.mkdir()
        rels = []
        for i in range(n):
            (src / f"f{i}.txt").write_text("x", encoding="utf-8")
            rels.append(f"f{i}.txt")
        sleeps: list[float] = []
        monkeypatch.setattr(graft_autobuild.time, "sleep", sleeps.append)
        graft_autobuild._stage_files(src, rels, tmp_path / "stg")
        assert len(sleeps) == 2
        assert (tmp_path / "stg" / "f0.txt").exists()


class TestStuckSameScreen970:
    @pytest.fixture(autouse=True)
    def _inline_qtimer(self, monkeypatch):
        # the respawn half (_do_respawn) runs on a QTimer — fire it inline,
        # as test_stuck_recover does, so the carried-over state is real.
        class _Shot:
            @staticmethod
            def singleShot(_ms, fn):
                fn()

        monkeypatch.setattr("agent_takkub.orchestrator.QTimer", _Shot)

    def _fake(self):
        from tests.test_stuck_recover import _CapOrch, _FakePane

        fake = _CapOrch()
        pane = _FakePane(state="working", last_out=0.0)
        pane.session.display_lines.return_value = ["Some unrecognized dialog", "> press 1 or 2"]
        pane.session.is_at_ready_prompt.return_value = False
        pane.session.shows_busy_marker.return_value = False
        fake._panes_by_project["p"] = {"reviewer": pane}
        return fake, pane

    def test_second_wedge_on_identical_screen_gives_up(self, monkeypatch):
        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(orch_mod, "_log_event", lambda e, **kw: events.append((e, kw)))
        fake, pane = self._fake()
        Orchestrator._auto_recover_stuck(fake, "reviewer", "p", pane, 1_000_000.0)  # type: ignore[arg-type]
        assert len(fake.close_calls) == 1
        rec = next(kw for e, kw in events if e == "stuck_pane_recover")
        assert (
            rec["attempt"] == 1 and rec["at_ready_prompt"] is False and rec["busy_marker"] is False
        )
        assert "provider" in rec and "scope" in rec
        Orchestrator._auto_recover_stuck(fake, "reviewer", "p", pane, 1_002_000.0)  # type: ignore[arg-type]
        assert len(fake.close_calls) == 1, "same screen again must not respawn a 2nd time"
        assert fake._pane_state["p::reviewer"].stuck_recover_gave_up is True
        assert any(e == "stuck_recover_same_screen" for e, _ in events)

    def test_different_screen_still_recovers(self):
        fake, pane = self._fake()
        Orchestrator._auto_recover_stuck(fake, "reviewer", "p", pane, 1_000_000.0)  # type: ignore[arg-type]
        pane.session.display_lines.return_value = ["a different screen"]
        Orchestrator._auto_recover_stuck(fake, "reviewer", "p", pane, 1_002_000.0)  # type: ignore[arg-type]
        assert len(fake.close_calls) == 2
        assert fake._pane_state["p::reviewer"].stuck_recover_gave_up is False

    def test_pane_state_default_tail_is_empty(self):
        assert PaneState().last_stuck_tail == ()
