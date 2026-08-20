"""pane_guard: the shell-side half of the per-role tool policy.

Context (2026-07-23): a `frontend` pane was caught running `npx --yes
playwright` plus a `find / -maxdepth 6 -iname playwright` whole-disk sweep.
`pane_tools_policy` had correctly denied it the browser MCP — but the MCP gate
says nothing about Bash, and every pane runs with
`--dangerously-skip-permissions`, so the agent simply took the shell route.

These tests pin both halves of the contract: the denials are real, and the
*allowed* cases stay allowed (a guard that blocks `grep playwright` would be
worse than no guard at all).
"""

from __future__ import annotations

import pytest

from agent_takkub import pane_guard


class TestRoleNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("qa", "qa"),
            ("QA", "qa"),
            ("  Frontend  ", "frontend"),
            ("qa#3", "qa"),
            ("frontend#12", "frontend"),
            ("", ""),
            (None, ""),
        ],
    )
    def test_normalise(self, raw: str | None, expected: str) -> None:
        assert pane_guard.normalise_role(raw) == expected

    def test_shard_inherits_browser_permission(self) -> None:
        """`--shards 4` spawns `qa#1…qa#4`; every shard is still qa."""
        assert pane_guard.is_browser_role("qa#4")
        assert not pane_guard.is_browser_role("frontend#4")

    @pytest.mark.parametrize("role", ["qa", "critic", "designer"])
    def test_browser_roles(self, role: str) -> None:
        assert pane_guard.is_browser_role(role)

    @pytest.mark.parametrize("role", ["frontend", "backend", "mobile", "devops", "reviewer"])
    def test_non_browser_roles(self, role: str) -> None:
        assert not pane_guard.is_browser_role(role)


class TestBrowserDriverDenied:
    """Every acquisition/execution route a pane could take to get a browser."""

    @pytest.mark.parametrize(
        "command",
        [
            # the exact command the frontend pane ran
            "npx --yes playwright",
            "npx -y playwright install chromium",
            "npx playwright test",
            "bunx playwright test",
            "npm i playwright",
            "npm install --save-dev @playwright/test",
            "pnpm add -D puppeteer",
            "yarn add puppeteer-core",
            "pnpm dlx playwright install",
            "pip install playwright",
            "pip3 install selenium",
            "python -m playwright install",
            "python3 -m playwright install chromium",
            "playwright test --headed",
            "playwright install",
            "node -e \"const { chromium } = require('playwright'); chromium.launch()\"",
            "python -c 'from playwright.sync_api import sync_playwright'",
            "chrome --headless --dump-dom https://example.com",
            "chromium --remote-debugging-port=9222",
            "google-chrome --headless=new https://localhost:3000",
        ],
    )
    def test_denied_for_frontend(self, command: str) -> None:
        verdict = pane_guard.classify(command, "frontend")
        assert not verdict.allowed, f"should have blocked: {command}"
        assert verdict.rule.startswith("browser_driver:")
        assert "qa" in verdict.reason

    @pytest.mark.parametrize("role", ["backend", "mobile", "devops", "reviewer", "docs"])
    def test_denied_for_every_non_browser_role(self, role: str) -> None:
        assert not pane_guard.classify("npx --yes playwright", role).allowed

    def test_denied_for_shard_of_non_browser_role(self) -> None:
        assert not pane_guard.classify("npx playwright test", "frontend#2").allowed


class TestBrowserDriverAllowed:
    """qa/critic/designer own browser verification — and *reading about* a
    browser driver is never blocked for anyone."""

    @pytest.mark.parametrize("role", ["qa", "critic", "designer", "qa#3"])
    @pytest.mark.parametrize(
        "command",
        ["npx --yes playwright", "npx playwright test", "playwright install"],
    )
    def test_browser_roles_may_drive(self, role: str, command: str) -> None:
        assert pane_guard.classify(command, role).allowed

    @pytest.mark.parametrize(
        "command",
        [
            # reading / searching — must never trip the guard
            "grep -rn playwright src/",
            "rg playwright --type ts",
            "cat package.json",
            "ls ~/AppData/Local/ms-playwright",
            "ls /c/Users/monch/AppData/Local/ms-playwright",
            "du -sh ~/AppData/Local/ms-playwright",
            "git log --oneline -- tests/playwright.config.ts",
            "echo 'playwright is qa-only'",
            "npm run test:unit",
            "npm install",
            "pnpm install --frozen-lockfile",
            "npx tsc --noEmit",
            "npx next build",
            # a second command that merely names it is not an invocation
            "npx tsc --noEmit && cat playwright.config.ts",
        ],
    )
    def test_reading_and_unrelated_commands_allowed(self, command: str) -> None:
        assert pane_guard.classify(command, "frontend").allowed, f"false positive: {command}"

    def test_ms_playwright_cache_path_is_not_a_package_token(self) -> None:
        """`ms-playwright` is the browser *cache* dir — listing it is fine."""
        assert pane_guard.classify("ls -la ~/.cache/ms-playwright", "backend").allowed


class TestMiniBrowserShardConstraint:
    @pytest.mark.parametrize("role", ["qa#1", "critic#2", "designer#9"])
    @pytest.mark.parametrize(
        "command",
        [
            "mb go http://localhost:3000",
            "mb shot out.png",
            "mb-start-chrome",
            "npm test && mb logs",
        ],
    )
    def test_mb_is_denied_for_browser_shards(self, role: str, command: str) -> None:
        verdict = pane_guard.classify(command, role)
        assert not verdict.allowed
        assert verdict.rule == "browser_driver:mb-shard-cdp-9222"
        assert "Playwright MCP" in verdict.reason

    @pytest.mark.parametrize("role", ["qa", "critic", "designer"])
    def test_mb_is_allowed_for_unsharded_browser_roles(self, role: str) -> None:
        assert pane_guard.classify("mb go http://localhost:3000", role).allowed

    def test_mb_fallback_granted_allows_shard(self) -> None:
        """#304 point 3: an explicit, caller-supplied grant lets the mb-shard
        deny branch through instead of hard-blocking."""
        verdict = pane_guard.classify(
            "mb go http://localhost:3000", "qa#1", mb_fallback_check=lambda: True
        )
        assert verdict.allowed

    def test_mb_fallback_denied_still_blocks(self) -> None:
        verdict = pane_guard.classify(
            "mb go http://localhost:3000", "qa#1", mb_fallback_check=lambda: False
        )
        assert not verdict.allowed
        assert verdict.rule == "browser_driver:mb-shard-cdp-9222"

    def test_mb_fallback_check_not_called_for_non_mb_commands(self) -> None:
        """The callback must be lazy — never invoked outside the one branch
        it exists for, so pane_guard stays free of unconditional I/O."""
        calls: list[None] = []

        def _spy() -> bool:
            calls.append(None)
            return True

        pane_guard.classify("npm test", "qa#1", mb_fallback_check=_spy)
        assert calls == []

    def test_mb_fallback_check_not_called_for_unsharded_roles(self) -> None:
        calls: list[None] = []

        def _spy() -> bool:
            calls.append(None)
            return True

        pane_guard.classify("mb go http://localhost:3000", "qa", mb_fallback_check=_spy)
        assert calls == []

    def test_no_fallback_check_defaults_to_hard_deny(self) -> None:
        """Omitting mb_fallback_check (every pre-#304 caller) behaves exactly
        as before: unconditional deny, no behavior change for them."""
        verdict = pane_guard.classify("mb go http://localhost:3000", "qa#1")
        assert not verdict.allowed


class TestDiskScanDenied:
    @pytest.mark.parametrize(
        "command",
        [
            # the exact command the frontend pane ran
            "find / -maxdepth 6 -iname playwright -type d",
            "find / -name '*.log'",
            "find C:\\ -name node_modules",
            "find /c/ -iname '*.ts'",
        ],
    )
    def test_root_scans_denied(self, command: str) -> None:
        verdict = pane_guard.classify(command, "frontend")
        assert not verdict.allowed, f"should have blocked: {command}"
        assert verdict.rule.startswith("disk_scan:")

    def test_denied_for_browser_roles_too(self) -> None:
        """A whole-disk sweep stalls the box no matter who runs it."""
        assert not pane_guard.classify("find / -name x", "qa").allowed

    @pytest.mark.parametrize(
        "command",
        [
            "find . -name '*.py'",
            "find src -type f -name '*.ts'",
            "find ./tests -maxdepth 2",
            "find node_modules/.bin -name next",
        ],
    )
    def test_scoped_finds_allowed(self, command: str) -> None:
        assert pane_guard.classify(command, "frontend").allowed, f"false positive: {command}"


class TestHostDestructiveDenied:
    """#169: kill-by-image-name targets every process with that name on the
    box, not just the caller's own children — the exact incident was a
    `frontend` pane's `taskkill /F /T /IM node.exe` killing every teammate
    pane's node process."""

    @pytest.mark.parametrize(
        "command",
        [
            # the exact command from the incident
            "taskkill /F /T /IM node.exe",
            "taskkill /IM node.exe /F",
            "Taskkill /im python.exe",
            "pkill node",
            "pkill -9 -f next",
            "killall node",
            "Stop-Process -Name node -Force",
            "Stop-Process -Force -Name chrome",
        ],
    )
    def test_denied_for_frontend(self, command: str) -> None:
        verdict = pane_guard.classify(command, "frontend")
        assert not verdict.allowed, f"should have blocked: {command}"
        assert verdict.rule.startswith("host_destructive:")
        assert "PID" in verdict.reason

    @pytest.mark.parametrize("role", ["backend", "mobile", "devops", "reviewer", "qa", "critic"])
    def test_denied_for_every_role_no_allowlist(self, role: str) -> None:
        """Unlike browser_driver, no role legitimately needs a host-wide
        kill-by-name — qa/critic are browser roles but still denied."""
        assert not pane_guard.classify("taskkill /F /IM node.exe", role).allowed

    @pytest.mark.parametrize(
        "command",
        [
            "taskkill /PID 12345 /F",
            "taskkill /F /PID 12345",
            "Stop-Process -Id 12345 -Force",
            "kill 12345",
            "kill -9 12345",
            # reading/mentioning by name must never trip the guard
            "grep -rn taskkill scripts/",
            "echo 'use taskkill /PID not /IM'",
            "cat docs/kill-policy.md",
        ],
    )
    def test_pid_targeted_and_unrelated_commands_allowed(self, command: str) -> None:
        assert pane_guard.classify(command, "frontend").allowed, f"false positive: {command}"


class TestPipEditableDenied:
    """#202: `pip install -e .` rewrites the SHARED venv's
    `__editable__*.pth` to point at the caller's cwd — the exact incident was
    a `backend` pane running it inside its own `--isolation worktree`
    checkout, which broke every pane's venv once the worktree was removed."""

    @pytest.mark.parametrize(
        "command",
        [
            "pip install -e .",
            "pip install --editable .",
            "pip3 install -e .",
            "python -m pip install -e .",
            "python3 -m pip install --editable ./src",
            "pip install -e ./worktrees/backend-1",
        ],
    )
    def test_denied_for_backend(self, command: str) -> None:
        verdict = pane_guard.classify(command, "backend")
        assert not verdict.allowed, f"should have blocked: {command}"
        assert verdict.rule.startswith("pip_editable:")
        assert "pytest" in verdict.reason

    @pytest.mark.parametrize("role", ["frontend", "mobile", "devops", "reviewer", "qa", "critic"])
    def test_denied_for_every_role_no_allowlist(self, role: str) -> None:
        """No role legitimately reinstalls the package into a venv every
        other pane shares — qa/critic are browser roles but still denied."""
        assert not pane_guard.classify("pip install -e .", role).allowed

    @pytest.mark.parametrize(
        "command",
        [
            "pip install -r requirements.txt",
            "pip install requests",
            "pip install pytest-cov",
            "pip list",
            "pip show agent-takkub",
            # reading/mentioning must never trip the guard
            "grep -rn 'pip install -e' docs/",
            "echo 'never run pip install -e . here'",
            "cat pyproject.toml",
        ],
    )
    def test_unrelated_pip_commands_allowed(self, command: str) -> None:
        assert pane_guard.classify(command, "backend").allowed, f"false positive: {command}"


class TestGitLeadOnlyDenied:
    """#314: a `backend`/custom `admin` pane self-committed on a task
    instruction of "commit เอง" while a `frontend` pane in the same session
    refused the identical instruction — both role files carried the same
    prohibition in prose, but prose alone is only as strong as how
    convincingly the task text argues past it. This pins the real
    `PreToolUse` deny."""

    @pytest.mark.parametrize(
        "command",
        [
            'git commit -m "fix bug"',
            "git commit --amend",
            "git add -A && git commit -m x",
            "git push",
            "git push origin main",
            "git reset --hard",
            "git reset --hard HEAD~1",
            "git branch -D feature-x",
            "git tag -d v1.0.0",
            "git rebase main",
            "git rebase -i HEAD~3",
            "git merge feature-x",
            "git checkout main",
            "git checkout -b new-branch",
        ],
    )
    def test_denied_for_backend(self, command: str) -> None:
        verdict = pane_guard.classify(command, "backend")
        assert not verdict.allowed, f"should have blocked: {command}"
        assert verdict.rule.startswith("git_lead_only:")
        assert "Lead" in verdict.reason

    @pytest.mark.parametrize("role", ["frontend", "mobile", "devops", "reviewer", "qa", "critic"])
    def test_denied_for_every_role_no_allowlist(self, role: str) -> None:
        """No teammate role legitimately commits on the shared tree —
        qa/critic are browser roles but still denied."""
        assert not pane_guard.classify('git commit -m "x"', role).allowed
        assert not pane_guard.classify("git push", role).allowed

    def test_denied_for_shard(self) -> None:
        assert not pane_guard.classify('git commit -m "x"', "backend#2").allowed

    def test_admin_style_custom_role_denied(self) -> None:
        """The #314 report's own trigger: a custom role (no `.claude/agents`
        file of its own) must be denied exactly like a built-in one — the
        guard has no allowlist to fall through."""
        assert not pane_guard.classify('git commit -m "x"', "admin").allowed

    @pytest.mark.parametrize(
        "command",
        [
            "git status",
            "git diff",
            "git diff --staged",
            "git log --oneline -5",
            "git log --oneline --all --grep=commit",
            "git show HEAD",
            "git stash",
            "git stash pop",
            "git branch -d merged-branch",  # lowercase -d: safe delete, not -D
            "git tag -l",
            # reading/mentioning must never trip the guard
            "grep -rn 'git commit' docs/",
            "echo 'only Lead runs git commit'",
            "cat docs/lead/role-and-workflow.md",
        ],
    )
    def test_unrelated_git_commands_allowed(self, command: str) -> None:
        assert pane_guard.classify(command, "backend").allowed, f"false positive: {command}"

    def test_lead_and_shell_exempt(self) -> None:
        assert pane_guard.classify('git commit -m "x"', "lead").allowed
        assert pane_guard.classify("git push", "shell").allowed


class TestGitLeadOnlyWorktreeCarveOut:
    """#81: an `--isolation worktree` pane owns a private branch and is
    explicitly told (by `orchestrator_text._append_worktree_hint`) to commit
    there itself — "the 'wait for Lead' policy is for the shared tree only".
    Only `commit` is carved out; push/rebase/merge/checkout stay blocked
    even there, matching that same hint's "ห้าม push · ห้าม switch/merge"."""

    _WT_CWD = r"C:\Users\dev\agent-takkub\worktrees\myproj\backend-3-1700000000"
    _WT_CWD_POSIX = "/home/dev/.agent-takkub/worktrees/myproj/backend-3-1700000000"

    @pytest.mark.parametrize("cwd", [_WT_CWD, _WT_CWD_POSIX])
    def test_commit_allowed_from_worktree_cwd(self, cwd: str) -> None:
        assert pane_guard.classify('git commit -m "x"', "backend", cwd=cwd).allowed

    @pytest.mark.parametrize(
        "command",
        ["git push", "git rebase main", "git merge main", "git checkout main"],
    )
    def test_push_rebase_merge_checkout_still_denied_from_worktree_cwd(self, command: str) -> None:
        verdict = pane_guard.classify(command, "backend", cwd=self._WT_CWD)
        assert not verdict.allowed, f"should still block from a worktree cwd: {command}"

    def test_commit_denied_when_cwd_missing(self) -> None:
        """No cwd (e.g. an older Claude Code build, or a malformed hook
        payload) must fail toward "not a worktree" — the safe default is
        Lead-only, never an accidental grant."""
        assert not pane_guard.classify('git commit -m "x"', "backend").allowed
        assert not pane_guard.classify('git commit -m "x"', "backend", cwd=None).allowed
        assert not pane_guard.classify('git commit -m "x"', "backend", cwd="").allowed

    def test_commit_denied_from_shared_tree_cwd(self) -> None:
        """A normal (non-worktree) checkout path must not accidentally match
        the "worktrees" substring check."""
        shared = r"C:\Users\dev\my-project"
        assert not pane_guard.classify('git commit -m "x"', "backend", cwd=shared).allowed


class TestFailOpen:
    """The guard must never be able to wedge a pane or police a human."""

    @pytest.mark.parametrize("role", ["lead", "shell"])
    def test_user_driven_panes_never_guarded(self, role: str) -> None:
        assert pane_guard.classify("npx --yes playwright", role).allowed
        assert pane_guard.classify("find / -name x", role).allowed
        assert pane_guard.classify("taskkill /F /IM node.exe", role).allowed
        assert pane_guard.classify("pip install -e .", role).allowed
        assert pane_guard.classify('git commit -m "x"', role).allowed

    @pytest.mark.parametrize("role", [None, "", "   "])
    def test_unknown_role_allows(self, role: str | None) -> None:
        """No TAKKUB_ROLE = a person at a terminal, not a cockpit pane."""
        assert pane_guard.classify("npx --yes playwright", role).allowed

    @pytest.mark.parametrize("command", [None, "", "   "])
    def test_empty_command_allows(self, command: str | None) -> None:
        assert pane_guard.classify(command, "frontend").allowed


class TestRuleTextSyncedWithRoleFiles:
    def test_guard_rule_text_is_actionable(self) -> None:
        """The denial reason is the only thing the blocked agent sees — it has
        to name the hand-off, or the pane just retries a different way."""
        assert "qa" in pane_guard.GUARD_RULE_TEXT
        assert "takkub done" in pane_guard.GUARD_RULE_TEXT
        assert "playwright" in pane_guard.GUARD_RULE_TEXT.lower()

    def test_host_destructive_rule_text_is_actionable(self) -> None:
        """Same contract as GUARD_RULE_TEXT: name the safe alternative (PID),
        not just the prohibition."""
        assert "PID" in pane_guard.HOST_DESTRUCTIVE_RULE_TEXT
        assert "taskkill" in pane_guard.HOST_DESTRUCTIVE_RULE_TEXT.lower()
        assert "169" in pane_guard.HOST_DESTRUCTIVE_RULE_TEXT

    def test_pip_editable_rule_text_is_actionable(self) -> None:
        """Same contract as GUARD_RULE_TEXT: name the safe alternative
        (pytest, no reinstall), not just the prohibition."""
        assert "pytest" in pane_guard.PIP_EDITABLE_RULE_TEXT
        assert "pip install -e" in pane_guard.PIP_EDITABLE_RULE_TEXT
        assert "202" in pane_guard.PIP_EDITABLE_RULE_TEXT

    def test_git_lead_only_rule_text_is_actionable(self) -> None:
        """Same contract as GUARD_RULE_TEXT: name the safe alternative
        (takkub done, Lead review), not just the prohibition — and name the
        worktree carve-out so a blocked pane isn't left guessing why the
        exact same command worked for a teammate in isolation mode."""
        assert "takkub done" in pane_guard.GIT_LEAD_ONLY_RULE_TEXT
        assert "git commit" in pane_guard.GIT_LEAD_ONLY_RULE_TEXT
        assert "314" in pane_guard.GIT_LEAD_ONLY_RULE_TEXT
        assert "worktree" in pane_guard.GIT_LEAD_ONLY_RULE_TEXT
