"""Tests for CliServer dispatch — focus on the async spawn/assign path that
acks the client immediately and runs the heavy pane spawn on the next event
loop tick (so a slow spawn never blows the CLI's 15 s timeout / freezes IPC;
see docs/cockpit-freeze-rca-2026-05-29.md)."""

from __future__ import annotations

import json
import re

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.cli_server import CliServer


def _delay_ms(reply_msg: str) -> int:
    """Extract the `+<n>ms` stagger suffix the dispatcher reports."""
    m = re.search(r"\+(\d+)ms", reply_msg)
    assert m is not None, f"no +Nms suffix in {reply_msg!r}"
    return int(m.group(1))


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


class _FakeSock:
    def __init__(self) -> None:
        self.written = b""

    def write(self, b) -> None:
        self.written += bytes(b)

    def flush(self) -> None:
        pass


class _FakeOrch:
    _lead_token = "tok"

    def __init__(self) -> None:
        self.assign_calls: list[tuple] = []
        self.spawn_calls: list[tuple] = []

    def assign(
        self,
        role,
        cwd=None,
        task="",
        requires_commit=False,
        auto_chain=False,
        shard_total=0,
        plan=False,
        isolation="shared",
        project=None,
        feature="",
        model=None,
    ):
        self.assign_calls.append((role, cwd, task, requires_commit, auto_chain, isolation))
        self.last_assign_model = model
        return True, "ok"

    def spawn(self, role, cwd=None, project=None):
        self.spawn_calls.append((role, cwd))
        return True, "ok"


def _replies(sock: _FakeSock) -> list[dict]:
    return [json.loads(line) for line in sock.written.decode().splitlines() if line.strip()]


def _auth(extra: dict) -> dict:
    base = {"from": "lead", "auth": "tok"}
    base.update(extra)
    return base


class TestAsyncSpawnDispatch:
    def test_assign_acked_immediately_then_deferred(self, qapp: QCoreApplication) -> None:
        orch = _FakeOrch()
        srv = CliServer(orch)
        sock = _FakeSock()

        srv._dispatch(sock, _auth({"cmd": "assign", "role": "backend", "task": "do x"}))

        # Replied right away, before the orchestrator did any spawn work.
        r = _replies(sock)
        assert len(r) == 1 and r[0]["ok"] is True
        assert orch.assign_calls == [], "assign must be deferred, not run inline"

        # Runs on the next event-loop tick.
        qapp.processEvents()
        assert orch.assign_calls == [("backend", None, "do x", False, False, "shared")]

    def test_assign_passes_flags(self, qapp: QCoreApplication) -> None:
        orch = _FakeOrch()
        srv = CliServer(orch)
        sock = _FakeSock()
        srv._dispatch(
            sock,
            _auth(
                {
                    "cmd": "assign",
                    "role": "backend",
                    "cwd": "C:/x",
                    "task": "t",
                    "requires_commit": True,
                    "auto_chain": True,
                    "isolation": "worktree",
                    "model": "claude-haiku-4-5",
                }
            ),
        )
        qapp.processEvents()
        assert orch.assign_calls == [("backend", "C:/x", "t", True, True, "worktree")]
        assert orch.last_assign_model == "claude-haiku-4-5"

    def test_assign_rejects_unsupported_model_before_scheduling(
        self, qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "agent_takkub.provider_config.assign_model_override_error",
            lambda *_args, **_kwargs: "--model is not supported by provider 'demo'",
        )
        orch = _FakeOrch()
        srv = CliServer(orch)
        sock = _FakeSock()

        srv._dispatch(
            sock,
            _auth({"cmd": "assign", "role": "qa", "model": "cheap", "task": "scan"}),
        )

        assert _replies(sock)[0]["ok"] is False
        qapp.processEvents()
        assert orch.assign_calls == []

    def test_spawn_acked_immediately_then_deferred(self, qapp: QCoreApplication) -> None:
        orch = _FakeOrch()
        srv = CliServer(orch)
        sock = _FakeSock()
        srv._dispatch(sock, _auth({"cmd": "spawn", "role": "frontend"}))
        assert _replies(sock)[0]["ok"] is True
        assert orch.spawn_calls == []
        qapp.processEvents()
        assert orch.spawn_calls == [("frontend", None)]

    def test_missing_role_is_immediate_error(self, qapp: QCoreApplication) -> None:
        orch = _FakeOrch()
        srv = CliServer(orch)
        sock = _FakeSock()
        srv._dispatch(sock, _auth({"cmd": "assign", "task": "x"}))
        r = _replies(sock)
        assert r[0]["ok"] is False and "role" in r[0]["msg"]
        qapp.processEvents()
        assert orch.assign_calls == []  # nothing scheduled

    def test_unauthorized_assign_rejected_not_deferred(self, qapp: QCoreApplication) -> None:
        orch = _FakeOrch()
        srv = CliServer(orch)
        sock = _FakeSock()
        # Wrong token → the lead-only gate rejects before any scheduling.
        srv._dispatch(sock, {"cmd": "assign", "from": "lead", "auth": "WRONG", "role": "backend"})
        r = _replies(sock)
        assert r[0]["ok"] is False
        qapp.processEvents()
        assert orch.assign_calls == []

    def test_non_lead_assign_rejected(self, qapp: QCoreApplication) -> None:
        orch = _FakeOrch()
        srv = CliServer(orch)
        sock = _FakeSock()
        srv._dispatch(sock, {"cmd": "assign", "from": "backend", "role": "qa"})
        r = _replies(sock)
        assert r[0]["ok"] is False and "lead" in r[0]["msg"].lower()
        qapp.processEvents()
        assert orch.assign_calls == []


class _FakeOrchWithProject(_FakeOrch):
    """`_FakeOrch` plus a `_resolve_project` that mirrors the real
    Orchestrator's (project explicitly given → returned as-is, no disk I/O)
    so cwd-validation tests don't depend on the real ~/.takkub/projects.json."""

    def _resolve_project(self, project):
        return project or "default"


class TestSyncCwdValidation:
    """#143: cwd escaping the project's configured paths must be rejected
    synchronously, before the "task queued" ack — not discovered later via
    an async [spawn-failed] notice after the CLI already printed ok."""

    @pytest.fixture
    def project_setup(self, tmp_path, monkeypatch: pytest.MonkeyPatch):
        import agent_takkub.config as config_mod

        web = tmp_path / "myproject" / "web"
        api = tmp_path / "myproject" / "api"
        web.mkdir(parents=True)
        api.mkdir(parents=True)
        pj = tmp_path / "projects.json"
        pj.write_text(
            json.dumps(
                {
                    "active": "myproject",
                    "projects": {"myproject": {"paths": {"web": str(web), "api": str(api)}}},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(config_mod, "PROJECTS_JSON", pj)
        return {"web": web, "api": api, "root": web.parent, "tmp_path": tmp_path}

    def test_assign_rejects_cwd_outside_project_before_ack(
        self, qapp: QCoreApplication, project_setup
    ) -> None:
        orch = _FakeOrchWithProject()
        srv = CliServer(orch)
        sock = _FakeSock()
        outside = str(project_setup["tmp_path"] / "unrelated")

        srv._dispatch(
            sock,
            _auth(
                {
                    "cmd": "assign",
                    "role": "backend",
                    "cwd": outside,
                    "task": "x",
                    "from_project": "myproject",
                }
            ),
        )

        r = _replies(sock)
        assert len(r) == 1
        assert r[0]["ok"] is False
        assert "outside project" in r[0]["msg"]
        assert str(project_setup["web"]) in r[0]["msg"]  # valid paths named
        qapp.processEvents()
        assert orch.assign_calls == [], "invalid cwd must never reach assign(), even async"

    def test_spawn_rejects_cwd_outside_project_before_ack(
        self, qapp: QCoreApplication, project_setup
    ) -> None:
        orch = _FakeOrchWithProject()
        srv = CliServer(orch)
        sock = _FakeSock()
        outside = str(project_setup["tmp_path"] / "unrelated")

        srv._dispatch(
            sock,
            _auth(
                {"cmd": "spawn", "role": "frontend", "cwd": outside, "from_project": "myproject"}
            ),
        )

        r = _replies(sock)
        assert r[0]["ok"] is False
        assert "outside project" in r[0]["msg"]
        qapp.processEvents()
        assert orch.spawn_calls == []

    def test_assign_accepts_cwd_inside_configured_path(
        self, qapp: QCoreApplication, project_setup
    ) -> None:
        orch = _FakeOrchWithProject()
        srv = CliServer(orch)
        sock = _FakeSock()

        srv._dispatch(
            sock,
            _auth(
                {
                    "cmd": "assign",
                    "role": "backend",
                    "cwd": str(project_setup["api"]),
                    "task": "x",
                    "from_project": "myproject",
                }
            ),
        )

        assert _replies(sock)[0]["ok"] is True
        qapp.processEvents()
        assert orch.assign_calls == [
            ("backend", str(project_setup["api"]), "x", False, False, "shared")
        ]

    def test_assign_accepts_project_root_cwd(self, qapp: QCoreApplication, project_setup) -> None:
        """The project's own root (common parent of its configured paths) is
        a legal cwd for any role, not just Lead (#143)."""
        orch = _FakeOrchWithProject()
        srv = CliServer(orch)
        sock = _FakeSock()

        srv._dispatch(
            sock,
            _auth(
                {
                    "cmd": "assign",
                    "role": "devops",
                    "cwd": str(project_setup["root"]),
                    "task": "x",
                    "from_project": "myproject",
                }
            ),
        )

        assert _replies(sock)[0]["ok"] is True
        qapp.processEvents()
        assert orch.assign_calls == [
            ("devops", str(project_setup["root"]), "x", False, False, "shared")
        ]

    def test_assign_skips_validation_without_resolve_project(
        self, qapp: QCoreApplication, project_setup
    ) -> None:
        """A caller whose orchestrator stub lacks `_resolve_project` (only
        the plain `_FakeOrch` — as in the rest of this test module) must
        degrade to the pre-existing 'default namespace, no validation'
        behavior rather than crash."""
        orch = _FakeOrch()
        srv = CliServer(orch)
        sock = _FakeSock()

        srv._dispatch(
            sock,
            _auth(
                {
                    "cmd": "assign",
                    "role": "backend",
                    "cwd": str(project_setup["tmp_path"] / "unrelated"),
                    "task": "x",
                }
            ),
        )

        assert _replies(sock)[0]["ok"] is True
        qapp.processEvents()
        assert len(orch.assign_calls) == 1


class TestSpawnStagger:
    """Concurrent assigns must be spaced apart so back-to-back ConPTY spawns
    don't collide on one event-loop tick (#44); codex gets a bigger gap so its
    npm self-update windows don't overlap (#38). Non-blocking — QTimer only."""

    @pytest.fixture(autouse=True)
    def _stub_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """codex detection now resolves the effective provider, which reads real
        ~/.takkub config + probes for the codex binary. Stub it to a deterministic
        name-based rule so these tests isolate the stagger math (and never touch
        the user's real config / installed CLIs)."""
        import agent_takkub.provider_config as pc

        monkeypatch.setattr(
            pc,
            "effective_provider_for",
            lambda role, project=None: (
                pc.CODEX if (role or "").lower().startswith("codex") else pc.CLAUDE
            ),
        )

    def test_first_assign_has_zero_delay(self, qapp: QCoreApplication) -> None:
        srv = CliServer(_FakeOrch())
        sock = _FakeSock()
        srv._dispatch(sock, _auth({"cmd": "assign", "role": "backend", "task": "x"}))
        assert _delay_ms(_replies(sock)[0]["msg"]) == 0  # lone assign unchanged

    def test_parallel_assigns_are_staggered(self, qapp: QCoreApplication) -> None:
        srv = CliServer(_FakeOrch())
        srv._spawn_gap_ms = 400
        delays = []
        for _ in range(3):
            sock = _FakeSock()
            srv._dispatch(sock, _auth({"cmd": "assign", "role": "backend", "task": "x"}))
            delays.append(_delay_ms(_replies(sock)[0]["msg"]))
        d0, d1, d2 = delays
        assert d0 == 0
        assert 0 < d1 <= 400
        assert d1 < d2 <= 800  # spawns spaced ~one gap apart, not all on one tick

    def test_codex_gets_larger_gap(self, qapp: QCoreApplication) -> None:
        srv = CliServer(_FakeOrch())
        srv._spawn_gap_ms = 400
        srv._codex_gap_ms = 10_000
        s1, s2 = _FakeSock(), _FakeSock()
        srv._dispatch(s1, _auth({"cmd": "assign", "role": "codex", "task": "x"}))
        srv._dispatch(s2, _auth({"cmd": "assign", "role": "codex", "task": "y"}))
        assert _delay_ms(_replies(s1)[0]["msg"]) == 0
        # second codex waits the (much larger) codex gap, not the 400ms general gap.
        assert _delay_ms(_replies(s2)[0]["msg"]) > 5_000

    def test_non_codex_after_codex_not_penalized(self, qapp: QCoreApplication) -> None:
        srv = CliServer(_FakeOrch())
        srv._spawn_gap_ms = 400
        srv._codex_gap_ms = 10_000
        s1, s2 = _FakeSock(), _FakeSock()
        srv._dispatch(s1, _auth({"cmd": "assign", "role": "codex", "task": "x"}))
        srv._dispatch(s2, _auth({"cmd": "assign", "role": "backend", "task": "y"}))
        backend_delay = _delay_ms(_replies(s2)[0]["msg"])
        # backend is spaced by the general gap, NOT held back the full codex gap.
        assert 0 < backend_delay <= 400


class TestCodexDetection:
    """#38: the codex gap follows the EFFECTIVE provider, not the role name.
    Exercises the REAL effective_provider_for (no name-based stub), so these would
    fail if _is_codex_spawn reverted to the old role.startswith('codex') check.
    conftest isolates provider_config paths off the real ~/.takkub."""

    def test_remapped_role_detected_as_codex(self, qapp: QCoreApplication, monkeypatch) -> None:
        import agent_takkub.provider_config as pc

        monkeypatch.setattr(pc, "_provider_available", lambda provider: True)  # codex installed
        pc.save_providers({"backend": "codex"})  # remap backend → codex
        srv = CliServer(_FakeOrch())
        assert srv._is_codex_spawn("backend", None) is True
        assert srv._is_codex_spawn("backend#2", None) is True  # shard form too

    def test_degraded_codex_not_detected(self, qapp: QCoreApplication, monkeypatch) -> None:
        import agent_takkub.provider_config as pc

        # codex unavailable (toggled off / not installed) → degrades to claude.
        monkeypatch.setattr(pc, "_provider_available", lambda provider: provider == pc.CLAUDE)
        srv = CliServer(_FakeOrch())
        assert srv._is_codex_spawn("codex", None) is False
        assert srv._is_codex_spawn("backend", None) is False

    def test_named_codex_detected_when_available(self, qapp: QCoreApplication, monkeypatch) -> None:
        import agent_takkub.provider_config as pc

        monkeypatch.setattr(pc, "_provider_available", lambda provider: True)
        srv = CliServer(_FakeOrch())
        assert srv._is_codex_spawn("codex", None) is True
        assert srv._is_codex_spawn("codex#1", None) is True
