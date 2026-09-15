"""Release-review regressions: destructive Git, sensitive edits, and retry truth.

These assert the required boundary, so failures on the reviewed revision are
intentional evidence for the fix loop. No production code is modified.
"""

import pytest

from agent_takkub import pane_guard, qa_gate


@pytest.mark.parametrize(
    "command",
    [
        'git -C "C:/shared project" restore .',
        "cmd /c git restore .",
        'pwsh -c "git restore ."',
        "git.exe restore .",
        "git -c alias.discard=restore discard .",
        # #609 H2 round 2: wrapper unwrap used to only fire at position zero
        # of the whole raw command — a chained or `rtk`-prefixed wrapper
        # sailed through unrecognised.
        "echo x && cmd /c git restore .",
        'echo x; pwsh -c "git restore ."',
        "rtk proxy cmd /c git stash",
    ],
)
def test_wrapped_shared_restore_must_be_denied(command):
    verdict = pane_guard.classify(command, "frontend", cwd="C:/shared project")
    assert not verdict.allowed, command


def test_wrapper_text_inside_a_quoted_argument_is_not_unwrapped():
    # `cmd /c` appearing only as a string literal (an argument to `echo`,
    # never invoked) must not be mistaken for an actual wrapper invocation.
    verdict = pane_guard.classify(
        'echo "cmd /c git restore ."', "frontend", cwd="C:/shared project"
    )
    assert verdict.allowed


def test_worktree_cwd_does_not_authorize_shared_target():
    verdict = pane_guard.classify(
        "git -C C:/shared restore .",
        "frontend",
        cwd="C:/data/worktrees/proj/frontend-123",
    )
    assert not verdict.allowed


def test_other_roles_worktree_is_not_owned():
    verdict = pane_guard.classify(
        "git restore .", "frontend", cwd="C:/data/worktrees/proj/backend-123"
    )
    assert not verdict.allowed


@pytest.mark.parametrize("command", ["git stash clear", "git stash drop"])
def test_own_worktree_cannot_delete_repository_shared_stashes(command):
    verdict = pane_guard.classify(command, "frontend", cwd="C:/data/worktrees/proj/frontend-123")
    assert not verdict.allowed


# #609 round 3: `--git-dir=`/`--work-tree=` (either flag form) broke
# `_GIT_SUBCMD_GAP`'s subcommand match outright — `restore` was never even
# recognised as present, so the caller's OWN correctly-owned worktree cwd
# never got a chance to matter; the command sailed through unrecognised.
@pytest.mark.parametrize(
    "command",
    [
        'git --git-dir="C:/shared/other/.git" --work-tree="C:/shared/other" restore .',
        "git --git-dir C:/shared/other/.git --work-tree C:/shared/other restore .",
    ],
)
def test_git_dir_work_tree_flags_do_not_bypass_restore(command):
    verdict = pane_guard.classify(command, "frontend", cwd="C:/data/worktrees/proj/frontend-123")
    assert not verdict.allowed, command


# #609 round 3: a `GIT_DIR=`/`GIT_WORK_TREE=`/etc override — via a bare
# prefix, `env`, cmd.exe `set`, or PowerShell `$env:` — points git at a
# different repo entirely, but the caller's cwd (its own, correctly-owned
# worktree) used to be the ONLY thing `_in_worktree` ever looked at, so the
# carve-out applied anyway even though the override redirects where the
# stash drop actually lands.
@pytest.mark.parametrize(
    "command",
    [
        "GIT_DIR=C:/shared/other/.git git stash drop",
        "env GIT_DIR=C:/shared/other/.git git stash drop",
        "set GIT_DIR=C:/shared/other/.git && git stash drop",
        '$env:GIT_DIR="C:/shared/other/.git"; git stash drop',
    ],
)
def test_git_dir_env_prefix_does_not_bypass_stash_drop(command):
    verdict = pane_guard.classify(command, "frontend", cwd="C:/data/worktrees/proj/frontend-123")
    assert not verdict.allowed, command


# #609 round 3: `git switch` is the modern alias for `checkout <branch>` —
# same blast radius, same blanket shared-tree deny regardless of which
# destructive-looking flag (or none at all) is used.
@pytest.mark.parametrize(
    "command",
    [
        "git switch --discard-changes main",
        "git switch -f main",
        "git switch -C wip main",
    ],
)
def test_switch_discard_variants_are_denied_on_shared_tree(command):
    verdict = pane_guard.classify(command, "frontend", cwd="C:/shared project")
    assert not verdict.allowed, command


# #609 round 3: `worktree remove`/`move`/`prune`/`lock`/`unlock` used to only
# check the CALLER's own cwd/target, never the worktree actually being
# mutated — Lead-only unconditionally now, regardless of the caller sitting
# inside its own correctly-owned worktree.
@pytest.mark.parametrize(
    "command",
    [
        "git worktree remove --force C:/data/worktrees/proj/backend-123",
        "git worktree remove C:/data/worktrees/proj/backend-123",
        "git worktree move C:/data/worktrees/proj/backend-123 C:/tmp/x",
        "git worktree prune",
        "git worktree lock C:/data/worktrees/proj/backend-123",
        "git worktree unlock C:/data/worktrees/proj/backend-123",
    ],
)
def test_worktree_admin_subcommands_are_always_lead_only(command):
    verdict = pane_guard.classify(command, "frontend", cwd="C:/data/worktrees/proj/frontend-123")
    assert not verdict.allowed, command


# Read-only `worktree list` and an unrelated env-var prefix must not be
# swept up by the round-3 hardening above.
@pytest.mark.parametrize(
    "command",
    [
        "git worktree list",
        "GIT_PAGER=cat git log -1",
    ],
)
def test_read_only_worktree_list_and_unrelated_env_prefix_stay_allowed(command):
    verdict = pane_guard.classify(command, "frontend", cwd="C:/data/worktrees/proj/frontend-123")
    assert verdict.allowed, command


def test_sensitive_production_module_cannot_opt_out_by_spec_suffix(tmp_path):
    # A .spec.ts suffix does not prevent a production entrypoint importing it.
    verdict = pane_guard.evaluate_lead_direct_edit(
        "Edit",
        {
            "file_path": "src/auth/verify.spec.ts",
            "old_string": "return verifySignature(value);",
            "new_string": "return true;",
        },
        cwd=str(tmp_path),
        scope="tiny",
        state_file=tmp_path / "guard-state.json",
    )
    assert not verdict.allowed


def test_real_assertion_failure_with_worker_error_is_not_infra_only():
    step = qa_gate.StepResult(
        "test",
        False,
        False,
        0.0,
        "FAIL authorization.test.ts: expected false to be true\n"
        "Tests  1 failed | 41 passed (42)\n"
        "[vitest-pool]: Timeout waiting for worker to respond",
        1,
    )
    assert not qa_gate._node_worker_timeout_flake(step)


def _evidence_harness(tmp_path, monkeypatch):
    from agent_takkub import orchestrator

    monkeypatch.setattr(orchestrator, "RUNTIME_DIR", tmp_path)
    cls = orchestrator.Orchestrator

    class Harness:
        _find_evidence_files = cls._find_evidence_files
        _evidence_content_hash = staticmethod(cls._evidence_content_hash)
        _evidence_dedup_gate = cls._evidence_dedup_gate

    return Harness()


def _write_capture(tmp_path, subdir, name, timestamp):
    import base64
    import os
    import time

    path = tmp_path / "exports" / time.strftime("%Y-%m-%d") / "probe" / subdir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
        )
    )
    os.utime(path, (timestamp, timestamp))


def test_new_task_can_report_unchanged_visual_state(tmp_path, monkeypatch):
    import time

    harness = _evidence_harness(tmp_path, monkeypatch)
    now = time.time()
    _write_capture(tmp_path, "qa", "task-A.png", now - 90)
    assert harness._evidence_dedup_gate("probe", "qa", now - 100, "task-A.png") is None
    # Independently rendered bytes can be identical for an unchanged page.
    _write_capture(tmp_path, "qa", "task-B.png", now - 20)
    assert harness._evidence_dedup_gate("probe", "qa", now - 30, "task-B.png") is None


def test_uncited_other_role_images_cannot_block_code_review(tmp_path, monkeypatch):
    import time

    harness = _evidence_harness(tmp_path, monkeypatch)
    now = time.time()
    _write_capture(tmp_path, "screenshots", "qa-a.png", now - 20)
    _write_capture(tmp_path, "screenshots", "qa-b.png", now - 19)
    assert (
        harness._evidence_dedup_gate(
            "probe", "reviewer", now - 30, "Reviewed Python logic; targeted tests passed"
        )
        is None
    )
