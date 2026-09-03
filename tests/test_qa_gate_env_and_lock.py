"""Unit tests for #471 (worktree env injection) and #472 (full-gate lock/queue).

Same interception convention as test_qa_gate.py: real `git` plumbing runs for
real (worktree creation, `.env` discovery), everything that would shell out to
a real pytest/ruff/lint-imports/node tool is faked via `_fake_run_factory`.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

import pytest

from agent_takkub import qa_gate

from .test_qa_gate import _fake_run_factory, _make_complete_venv


def _init_repo(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "README.md").write_text("x", encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


@pytest.fixture
def repo(tmp_path):
    """Local copy of test_qa_gate.py's `repo` fixture — importing that one
    directly triggers ruff F811 (its name gets rebound by every `repo`
    parameter in this file) for no benefit over just having our own."""
    root = tmp_path / "repo"
    _init_repo(root)
    return root


@pytest.fixture
def main_and_worktree(tmp_path):
    """A real main checkout + a real linked `git worktree` off it — the same
    shape a specialist pane's `--isolation worktree` actually produces. The
    main checkout carries a root `.env` and a workspace-package `.env`
    (`apps/api/.env`); the worktree carries neither (gitignored, #471)."""
    main = tmp_path / "main"
    _init_repo(main)
    (main / ".env").write_text(
        "DATABASE_URL=postgres://main/db\nSHARED=from-main\n", encoding="utf-8"
    )
    (main / "apps" / "api").mkdir(parents=True)
    (main / "apps" / "api" / ".env").write_text("API_SECRET=s3cr3t\n", encoding="utf-8")
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-b", "wt/x", str(wt)], cwd=main, check=True)
    return main, wt


# ── #471: worktree env injection ────────────────────────────────────────────


def test_find_main_checkout_from_a_linked_worktree(main_and_worktree):
    main, wt = main_and_worktree
    found = qa_gate._find_main_checkout(wt)
    assert found is not None
    assert found.resolve() == main.resolve()


def test_find_main_checkout_none_outside_a_worktree_setup(tmp_path):
    plain = tmp_path / "plain"
    _init_repo(plain)
    # A lone checkout is still its own (only) worktree — main == self, so the
    # caller (`_inject_worktree_env`) is the one that turns "found, but same"
    # into "nothing to inject", not this function.
    found = qa_gate._find_main_checkout(plain)
    assert found is not None
    assert found.resolve() == plain.resolve()


def test_find_main_checkout_none_without_git(tmp_path):
    no_git = tmp_path / "no-git"
    no_git.mkdir()
    assert qa_gate._find_main_checkout(no_git) is None


def test_parse_dotenv_handles_quotes_comments_and_export(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        "\n".join(
            [
                "# a comment",
                "",
                "PLAIN=value",
                'QUOTED="quoted value"',
                "export EXPORTED=yes",
                "SINGLE='single'",
                "NOEQUALS-broken",
            ]
        ),
        encoding="utf-8",
    )
    assert qa_gate._parse_dotenv(p) == {
        "PLAIN": "value",
        "QUOTED": "quoted value",
        "EXPORTED": "yes",
        "SINGLE": "single",
    }


def test_find_env_files_root_and_workspace_package_skips_node_modules(tmp_path):
    root = tmp_path / "main"
    root.mkdir()
    (root / ".env").write_text("A=1", encoding="utf-8")
    (root / "apps" / "api").mkdir(parents=True)
    (root / "apps" / "api" / ".env").write_text("B=2", encoding="utf-8")
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / ".env").write_text("C=3", encoding="utf-8")

    files = qa_gate._find_env_files(root)
    rel = sorted(str(f.relative_to(root)).replace("\\", "/") for f in files)
    assert rel == [".env", "apps/api/.env"]


def test_inject_worktree_env_none_when_not_a_worktree(tmp_path):
    root = tmp_path / "plain"
    _init_repo(root)
    env = {"EXISTING": "1"}
    assert qa_gate._inject_worktree_env(root, env) is None
    assert env == {"EXISTING": "1"}


def test_inject_worktree_env_injects_without_overriding_existing_keys(main_and_worktree):
    _main, wt = main_and_worktree
    env = {"DATABASE_URL": "postgres://worktree-own/db"}

    step = qa_gate._inject_worktree_env(wt, env)

    assert step is not None
    assert step.ok is True
    assert step.env_gap is False
    # already present in `env` (the worktree's own) — never overridden
    assert env["DATABASE_URL"] == "postgres://worktree-own/db"
    # only existed in the main checkout — injected
    assert env["SHARED"] == "from-main"
    assert env["API_SECRET"] == "s3cr3t"
    # counts, never values
    assert "postgres" not in step.detail
    assert "s3cr3t" not in step.detail
    assert "2" in step.detail  # 2 keys actually injected (DATABASE_URL was skipped)


def test_inject_worktree_env_reports_gap_when_main_checkout_has_no_env(tmp_path):
    main = tmp_path / "main"
    _init_repo(main)
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-b", "wt/y", str(wt)], cwd=main, check=True)

    step = qa_gate._inject_worktree_env(wt, {})

    assert step is not None
    assert step.ok is True  # an env gap must not fail the gate by itself
    assert "not this diff" in step.detail


def test_run_gate_from_worktree_injects_main_env_into_subprocess(main_and_worktree, monkeypatch):
    main, wt = main_and_worktree
    _make_complete_venv(main)  # shared venv lives off git-common-dir → the main checkout
    recorder: list = []
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0, 0, 0]))
    monkeypatch.setattr(qa_gate, "_qa_gate_lock_dir", lambda: wt.parent / "locks")

    report = qa_gate.run_gate(cwd=wt, write_report=False)

    assert report.ok
    assert any(s.name == "env-inject" for s in report.steps)
    _, pytest_env = recorder[0]
    assert pytest_env["DATABASE_URL"] == "postgres://main/db"
    assert pytest_env["API_SECRET"] == "s3cr3t"


# ── #472: full-gate lock/queue ───────────────────────────────────────────────


def test_try_acquire_slot_then_blocks_until_released(tmp_path):
    base = tmp_path / "locks"
    h1 = qa_gate._try_acquire_slot(base, 0)
    assert h1 is not None
    assert (h1.path / "pid").read_text(encoding="utf-8").strip() == str(os.getpid())
    assert (h1.path / "heartbeat").exists()

    assert qa_gate._try_acquire_slot(base, 0) is None  # already held

    h1.release()
    assert not h1.path.exists()

    h2 = qa_gate._try_acquire_slot(base, 0)
    assert h2 is not None
    h2.release()


def test_lock_slot_is_stale_when_holder_pid_is_dead(tmp_path, monkeypatch):
    slot_dir = tmp_path / "locks" / "slot-0"
    slot_dir.mkdir(parents=True)
    (slot_dir / "pid").write_text("999999999", encoding="utf-8")
    (slot_dir / "heartbeat").write_text(str(time.time()), encoding="utf-8")
    monkeypatch.setattr(qa_gate, "_pid_alive", lambda pid: False)

    assert qa_gate._lock_slot_is_stale(slot_dir) is True


def test_lock_slot_is_stale_when_heartbeat_silent_even_if_pid_alive(tmp_path, monkeypatch):
    slot_dir = tmp_path / "locks" / "slot-0"
    slot_dir.mkdir(parents=True)
    (slot_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")
    stale_ts = time.time() - qa_gate._LOCK_STALE_AFTER_S - 1
    (slot_dir / "heartbeat").write_text(str(stale_ts), encoding="utf-8")
    monkeypatch.setattr(qa_gate, "_pid_alive", lambda pid: True)

    assert qa_gate._lock_slot_is_stale(slot_dir) is True


def test_lock_slot_not_stale_when_pid_alive_and_heartbeat_fresh(tmp_path, monkeypatch):
    slot_dir = tmp_path / "locks" / "slot-0"
    slot_dir.mkdir(parents=True)
    (slot_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")
    (slot_dir / "heartbeat").write_text(str(time.time()), encoding="utf-8")
    monkeypatch.setattr(qa_gate, "_pid_alive", lambda pid: True)

    assert qa_gate._lock_slot_is_stale(slot_dir) is False


def test_acquire_full_gate_lock_reclaims_a_dead_holders_stale_lock(tmp_path, monkeypatch):
    base = tmp_path / "locks"
    slot_dir = base / "slot-0"
    slot_dir.mkdir(parents=True)
    (slot_dir / "pid").write_text("999999999", encoding="utf-8")
    (slot_dir / "heartbeat").write_text(str(time.time() - 999), encoding="utf-8")
    monkeypatch.setattr(qa_gate, "_pid_alive", lambda pid: pid == os.getpid())

    handle = qa_gate.acquire_full_gate_lock(base, poll_interval=0.01)
    try:
        assert handle.slot == 0
        assert (handle.path / "pid").read_text(encoding="utf-8").strip() == str(os.getpid())
    finally:
        handle.release()


def test_full_gate_slots_reads_env_var_and_rejects_garbage(monkeypatch):
    monkeypatch.setenv("TAKKUB_QA_GATE_SLOTS", "3")
    assert qa_gate._full_gate_slots() == 3
    monkeypatch.setenv("TAKKUB_QA_GATE_SLOTS", "not-a-number")
    assert qa_gate._full_gate_slots() == 1
    monkeypatch.setenv("TAKKUB_QA_GATE_SLOTS", "0")
    assert qa_gate._full_gate_slots() == 1
    monkeypatch.delenv("TAKKUB_QA_GATE_SLOTS", raising=False)
    assert qa_gate._full_gate_slots() == 1


def test_multiple_slots_allow_concurrent_holders(tmp_path, monkeypatch):
    monkeypatch.setenv("TAKKUB_QA_GATE_SLOTS", "2")
    base = tmp_path / "locks"
    h1 = qa_gate.acquire_full_gate_lock(base, poll_interval=0.01)
    h2 = qa_gate.acquire_full_gate_lock(base, poll_interval=0.01)
    try:
        assert {h1.slot, h2.slot} == {0, 1}
        assert qa_gate._active_full_gate_count(base) == 2
        assert qa_gate._active_full_gate_count(base, exclude=h1) == 1
    finally:
        h1.release()
        h2.release()


def test_second_waiter_queues_then_acquires_after_release_and_announces(tmp_path, monkeypatch):
    monkeypatch.setattr(qa_gate, "_QUEUE_ANNOUNCE_INTERVAL_S", 0.05)
    base = tmp_path / "locks"
    holder = qa_gate.acquire_full_gate_lock(base, poll_interval=0.01)

    announcements: list[str] = []
    result: dict = {}

    def waiter():
        result["handle"] = qa_gate.acquire_full_gate_lock(
            base, label="proj", poll_interval=0.02, print_fn=announcements.append
        )

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.2)
    assert result.get("handle") is None  # still queued — holder hasn't released
    holder.release()
    t.join(timeout=5)

    assert result["handle"] is not None
    result["handle"].release()
    assert any("queue position" in a and "proj" in a for a in announcements)


def test_run_step_contended_retries_once_on_timeout_during_contention(tmp_path, monkeypatch):
    calls: list = []

    def fake_run_step(name, cmd, env, cwd, log_dir):
        calls.append(cmd)
        if len(calls) == 1:
            return qa_gate.StepResult(name, False, False, 1.0, "FAILED: Timeout >5000ms exceeded")
        return qa_gate.StepResult(name, True, False, 1.0, "1 passed")

    monkeypatch.setattr(qa_gate, "_run_step", fake_run_step)
    monkeypatch.setattr(qa_gate, "_active_full_gate_count", lambda base, exclude=None: 1)
    monkeypatch.setattr(qa_gate, "_wait_for_full_gate_clear", lambda base, exclude=None, **kw: None)

    step = qa_gate._run_step_contended(
        "vitest", ["vitest", "run"], {}, tmp_path, None, lock_base=tmp_path / "locks"
    )

    assert step.ok is True
    assert len(calls) == 2
    assert "retry after contention" in step.detail


def test_run_step_contended_does_not_retry_a_real_failure(tmp_path, monkeypatch):
    calls: list = []

    def fake_run_step(name, cmd, env, cwd, log_dir):
        calls.append(1)
        return qa_gate.StepResult(name, False, False, 1.0, "AssertionError: expected 1 got 2")

    monkeypatch.setattr(qa_gate, "_run_step", fake_run_step)
    monkeypatch.setattr(qa_gate, "_active_full_gate_count", lambda base, exclude=None: 1)

    step = qa_gate._run_step_contended(
        "vitest", ["vitest", "run"], {}, tmp_path, None, lock_base=tmp_path / "locks"
    )

    assert step.ok is False
    assert len(calls) == 1  # not a timeout signature — no retry


def test_run_step_contended_does_not_retry_a_timeout_with_no_contention(tmp_path, monkeypatch):
    calls: list = []

    def fake_run_step(name, cmd, env, cwd, log_dir):
        calls.append(1)
        return qa_gate.StepResult(name, False, False, 1.0, "Test timed out after 5000ms")

    monkeypatch.setattr(qa_gate, "_run_step", fake_run_step)
    monkeypatch.setattr(qa_gate, "_active_full_gate_count", lambda base, exclude=None: 0)

    step = qa_gate._run_step_contended(
        "vitest", ["vitest", "run"], {}, tmp_path, None, lock_base=tmp_path / "locks"
    )

    assert step.ok is False
    assert len(calls) == 1  # no other full gate running — nothing to blame contention on


def test_run_gate_full_run_acquires_and_releases_the_lock(repo, monkeypatch, tmp_path):
    _make_complete_venv(repo)
    recorder: list = []
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0, 0, 0]))
    lock_base = tmp_path / "locks"
    monkeypatch.setattr(qa_gate, "_qa_gate_lock_dir", lambda: lock_base)

    report = qa_gate.run_gate(cwd=repo, write_report=False)

    assert report.ok
    assert not (lock_base / "slot-0").exists()  # released, nothing left behind


def test_run_gate_targeted_run_never_takes_the_full_gate_lock(repo, monkeypatch):
    _make_complete_venv(repo)
    recorder: list = []
    monkeypatch.setattr(qa_gate.subprocess, "run", _fake_run_factory(recorder, [0]))

    def _boom(*a, **k):
        raise AssertionError("targeted run must not acquire the full-gate lock")

    monkeypatch.setattr(qa_gate, "acquire_full_gate_lock", _boom)

    report = qa_gate.run_gate(cwd=repo, targeted=["tests/test_x.py"], write_report=False)

    assert report.ok
