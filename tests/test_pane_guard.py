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
        assert not pane_guard.is_browser_role("backend#4")

    @pytest.mark.parametrize("role", ["qa", "critic", "designer", "frontend", "mobile"])
    def test_browser_roles(self, role: str) -> None:
        # #433: frontend/mobile self-verify UI work with real screenshots.
        assert pane_guard.is_browser_role(role)

    @pytest.mark.parametrize("role", ["backend", "devops", "reviewer", "docs", "security"])
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
    def test_denied_for_backend(self, command: str) -> None:
        verdict = pane_guard.classify(command, "backend")
        assert not verdict.allowed, f"should have blocked: {command}"
        assert verdict.rule.startswith("browser_driver:")
        assert "qa" in verdict.reason

    @pytest.mark.parametrize("role", ["backend", "devops", "reviewer", "docs", "security"])
    def test_denied_for_every_non_browser_role(self, role: str) -> None:
        assert not pane_guard.classify("npx --yes playwright", role).allowed

    def test_denied_for_shard_of_non_browser_role(self) -> None:
        assert not pane_guard.classify("npx playwright test", "backend#2").allowed

    @pytest.mark.parametrize("role", ["frontend", "frontend#2", "mobile"])
    def test_ui_roles_may_screenshot_their_own_work(self, role: str) -> None:
        """#433: self-verification needs the project's own playwright."""
        assert pane_guard.classify(
            "npx playwright screenshot --viewport-size=390,844 http://localhost:3000 out.png",
            role,
        ).allowed


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
        assert pane_guard.classify(command, "backend").allowed, f"false positive: {command}"

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
        verdict = pane_guard.classify(command, "backend")
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


class TestHostNetworkDenied:
    """#400: the host machine's network belongs to the user at the keyboard,
    not a sandboxed pane — the exact incident was a pane running `netsh wlan
    connect` to test a networking change and dropping the whole box off the
    internet with zero warning."""

    @pytest.mark.parametrize(
        "command",
        [
            # Windows
            "netsh wlan connect name=Guest",
            "netsh wlan disconnect",
            "netsh wlan delete profile name=Office",
            "netsh wlan add profile filename=profile.xml",
            "netsh interface ip set address name=Wi-Fi static 10.0.0.5 255.255.255.0",
            "netsh interface ipv4 add address Wi-Fi 10.0.0.6/24",
            'netsh interface set interface "Wi-Fi" disabled',
            "netsh winhttp set proxy proxy.example.com:8080",
            "netsh winhttp reset proxy",
            "ipconfig /release",
            "ipconfig /renew",
            "ipconfig /release6",
            "route add 10.0.0.0 mask 255.0.0.0 10.0.0.1",
            "route delete 10.0.0.0",
            "route change 10.0.0.0 mask 255.0.0.0 10.0.0.2",
            "rasdial MyVPN user pass",
            # macOS
            "networksetup -setairportnetwork en0 MyNetwork password",
            "networksetup -setairportpower en0 off",
            "networksetup -setnetworkserviceenabled Wi-Fi off",
            "networksetup -setwebproxy Wi-Fi proxy.example.com 8080",
            "networksetup -setsecurewebproxy Wi-Fi proxy.example.com 8080",
            "ifconfig en0 down",
            "ifconfig en0 up",
            "scutil --proxy",
            "sudo networksetup -setairportpower en0 off",
            "sudo ifconfig en0 down",
            "sudo scutil --proxy",
        ],
    )
    def test_denied_for_backend(self, command: str) -> None:
        verdict = pane_guard.classify(command, "backend")
        assert not verdict.allowed, f"should have blocked: {command}"
        assert verdict.rule.startswith("host_network:")
        assert "host" in verdict.reason.lower()

    @pytest.mark.parametrize("role", ["frontend", "mobile", "devops", "reviewer", "qa", "critic"])
    def test_denied_for_every_role_no_allowlist(self, role: str) -> None:
        assert not pane_guard.classify("netsh wlan connect name=Guest", role).allowed

    @pytest.mark.parametrize(
        "command",
        [
            # read-only diagnostics stay allowed
            "netsh wlan show networks",
            "netsh wlan show interfaces",
            "netsh interface show interface",
            "ipconfig",
            "ipconfig /all",
            "route print",
            "networksetup -getairportnetwork en0",
            "ifconfig",
            "ifconfig en0",
            # reading/mentioning must never trip the guard
            "grep -rn 'netsh wlan connect' docs/",
            "echo 'never run netsh wlan connect here'",
            "cat docs/network-policy.md",
        ],
    )
    def test_unrelated_and_readonly_commands_allowed(self, command: str) -> None:
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
            "git stash list",
            "git stash show",
            "git stash show -p",
            "git tag -l",
            # #385: hyphenated longer subcommands are different (read-only)
            # commands — `merge\b` used to match `merge-base`.
            "git merge-base HEAD master",
            "git merge-base --is-ancestor master HEAD",
            "git merge-tree --write-tree master HEAD",
            "git log --merges --oneline -5",
            "git branch --merged",
            # #609/#611 round 5: `merge.ff` isn't on the round-5 config
            # key-safe-list — see `TestGitConfigInjectionRound5` below for the
            # write-form deny this now gets. `core.quotepath` IS safe.
            "git config core.quotepath true",
            "git config --get user.name",
            "git commit-tree HEAD^{tree} -m x",
            # reading/mentioning must never trip the guard
            "grep -rn 'git commit' docs/",
            "echo 'only Lead runs git commit'",
            "cat docs/lead/role-and-workflow.md",
        ],
    )
    def test_unrelated_git_commands_allowed(self, command: str) -> None:
        assert pane_guard.classify(command, "backend").allowed, f"false positive: {command}"

    def test_checkout_index_denied_on_shared_tree(self) -> None:
        """#609 round 4: `checkout-index -a -f` overwrites tracked files in
        the CALLER's working directory from the index — on the shared tree
        that IS another pane's working tree too. Moved out of
        `test_unrelated_git_commands_allowed` (was allowed pre-round-4,
        before the shared-tree default-deny existed) rather than added to the
        allow-list, since it writes shared state."""
        verdict = pane_guard.classify("git checkout-index -a -f", "backend")
        assert not verdict.allowed
        assert verdict.rule == "git_shared_default_deny:checkout-index"

    def test_lead_and_shell_exempt(self) -> None:
        assert pane_guard.classify('git commit -m "x"', "lead").allowed
        assert pane_guard.classify("git push", "shell").allowed


class TestGitLeadOnlyWorktreeCarveOut:
    """#81: an `--isolation worktree` pane owns a private branch and is
    explicitly told (by `orchestrator_text._append_worktree_hint`) to commit
    there itself — "the 'wait for Lead' policy is for the shared tree only".
    `commit`, (#385) `merge`, and (#545) `reset --hard`/`checkout`/
    `branch -D` are carved out — a worktree pane may pull base INTO its own
    branch and freely rewrite/switch/delete branches inside its own
    disposable checkout; `push`/`rebase`/`tag -d` stay blocked even there."""

    _WT_CWD = r"C:\Users\dev\agent-takkub\worktrees\myproj\backend-3-1700000000"
    _WT_CWD_POSIX = "/home/dev/.agent-takkub/worktrees/myproj/backend-3-1700000000"

    @pytest.mark.parametrize("cwd", [_WT_CWD, _WT_CWD_POSIX])
    def test_commit_allowed_from_worktree_cwd(self, cwd: str) -> None:
        assert pane_guard.classify('git commit -m "x"', "backend", cwd=cwd).allowed

    @pytest.mark.parametrize(
        "command",
        ["git push", "git rebase main", "git tag -d v1.0.0"],
    )
    def test_push_rebase_tagdelete_still_denied_from_worktree_cwd(self, command: str) -> None:
        verdict = pane_guard.classify(command, "backend", cwd=self._WT_CWD)
        assert not verdict.allowed, f"should still block from a worktree cwd: {command}"

    @pytest.mark.parametrize("cwd", [_WT_CWD, _WT_CWD_POSIX])
    @pytest.mark.parametrize(
        "command",
        [
            "git checkout main",
            "git checkout -b new-branch",
            "git reset --hard",
            "git reset --hard HEAD~1",
        ],
    )
    def test_checkout_reset_allowed_from_worktree_cwd(self, command: str, cwd: str) -> None:
        """#545: the checkout is disposable by definition — blocking these
        protected nothing and forced worse workarounds in practice (`merge`
        used in place of `reset --hard`, `git archive | tar -x` used in
        place of `checkout`)."""
        assert pane_guard.classify(command, "backend", cwd=cwd).allowed, (
            f"should now be allowed from the pane's own worktree cwd: {command}"
        )

    @pytest.mark.parametrize("cwd", [_WT_CWD, _WT_CWD_POSIX])
    @pytest.mark.parametrize("command", ["git branch -D feature-x", "git branch -f feature-x HEAD"])
    def test_branch_force_forms_denied_even_from_worktree_cwd(self, command: str, cwd: str) -> None:
        """#609/#611 round 5: `-D`/`-f`/`-M` name an arbitrary branch by
        string, not necessarily one scoped to the caller's own worktree (the
        same cross-role blast radius R3 found for `worktree remove`) — no
        worktree carve-out any more, unlike the disposable-checkout ops
        above."""
        verdict = pane_guard.classify(command, "backend", cwd=cwd)
        assert not verdict.allowed, f"should still block even from a worktree cwd: {command}"

    @pytest.mark.parametrize("cwd", [_WT_CWD, _WT_CWD_POSIX])
    def test_branch_create_allowed_from_worktree_cwd(self, cwd: str) -> None:
        assert pane_guard.classify("git branch new-feature", "backend", cwd=cwd).allowed

    @pytest.mark.parametrize(
        "command",
        ["git checkout main", "git reset --hard", "git branch -D feature-x"],
    )
    def test_checkout_reset_branchdelete_still_denied_from_shared_tree(self, command: str) -> None:
        shared = r"C:\Users\dev\my-project"
        verdict = pane_guard.classify(command, "backend", cwd=shared)
        assert not verdict.allowed, f"should still block outside a worktree: {command}"
        assert not pane_guard.classify(command, "backend").allowed

    @pytest.mark.parametrize("cwd", [_WT_CWD, _WT_CWD_POSIX])
    @pytest.mark.parametrize(
        "command",
        ["git merge master", "git merge --no-edit origin/main", "git -C . merge main"],
    )
    def test_merge_allowed_from_worktree_cwd(self, command: str, cwd: str) -> None:
        """#385: a second worktree pane needs base's latest commits to avoid
        re-implementing what a sibling landed; merging base INTO its own
        branch touches nothing shared."""
        assert pane_guard.classify(command, "backend", cwd=cwd).allowed

    def test_merge_still_denied_from_shared_tree(self) -> None:
        verdict = pane_guard.classify("git merge feature-x", "backend", cwd=r"C:\Users\dev\proj")
        assert not verdict.allowed
        assert verdict.rule == "git_lead_only:merge"
        assert not pane_guard.classify("git merge feature-x", "backend").allowed

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


class TestGitStashRestoreCleanDenied:
    """#609: a `frontend` pane ran `git stash && vitest ...; git stash pop`
    on the shared tree while a `backend` pane had ~165 files of uncommitted
    work in progress — every dirty file's mtime changed and the backend
    pane's work was one `git stash drop` away from gone. `pane_guard` had no
    rule for `stash`/`restore`/`clean -f` at all (unlike `reset --hard`/
    `checkout`, already covered)."""

    @pytest.mark.parametrize(
        "command",
        [
            "git stash",
            "git stash push",
            "git stash push -u -m wip",
            "git stash pop",
            "git stash apply",
            "git stash drop",
            "git stash clear",
            "git stash branch tmp",
            "git restore .",
            "git restore --staged foo.py",
            "git restore --source=HEAD~1 --worktree --staged .",
            "git clean -f",
            "git clean -fd",
            "git clean -fdx",
            "git clean --force",
        ],
    )
    def test_denied_on_shared_tree(self, command: str) -> None:
        verdict = pane_guard.classify(command, "frontend")
        assert not verdict.allowed, f"should have blocked: {command}"
        assert verdict.rule.startswith("git_lead_only:")

    def test_stash_denied_even_mixed_with_readonly(self) -> None:
        """One mutating stash call in a chain denies the whole command, even
        alongside a read-only one — same conservative direction as every
        other rule here."""
        assert not pane_guard.classify("git stash list && git stash pop", "frontend").allowed

    @pytest.mark.parametrize(
        "command",
        ["git stash list", "git stash show", "git stash show -p", "git clean -n", "git clean"],
    )
    def test_readonly_forms_allowed(self, command: str) -> None:
        assert pane_guard.classify(command, "frontend").allowed, f"false positive: {command}"

    @pytest.mark.parametrize(
        "command",
        [
            "git stash",
            "git stash push",
            "git stash apply",
            "git restore .",
            "git clean -fd",
        ],
    )
    def test_allowed_from_worktree_cwd(self, command: str) -> None:
        wt = r"C:\Users\dev\.agent-takkub\worktrees\myproj\devops-1789429492"
        assert pane_guard.classify(command, "devops", cwd=wt).allowed, (
            f"should be allowed from the pane's own worktree cwd: {command}"
        )

    @pytest.mark.parametrize("command", ["git stash pop", "git stash drop", "git stash clear"])
    def test_shared_ref_stash_still_denied_from_own_worktree_cwd(self, command: str) -> None:
        """#609/#611 H1: `pop`/`drop`/`clear` remove an entry from
        `refs/stash`, which every linked worktree of the same repository
        shares — not private to this pane's own checkout, unlike
        `push`/`apply` above. Proven live: a `clear` run from one worktree
        deleted a stash a sibling worktree had pushed
        (`ART/stash-cross-worktree.json`)."""
        wt = r"C:\Users\dev\.agent-takkub\worktrees\myproj\devops-1789429492"
        verdict = pane_guard.classify(command, "devops", cwd=wt)
        assert not verdict.allowed, f"must stay Lead-only even in-worktree: {command}"

    def test_denied_from_shared_tree_cwd(self) -> None:
        shared = r"C:\Users\dev\my-project"
        assert not pane_guard.classify("git stash", "devops", cwd=shared).allowed
        assert not pane_guard.classify("git restore .", "devops", cwd=shared).allowed
        assert not pane_guard.classify("git clean -f", "devops", cwd=shared).allowed


class TestRtkPrefixSeenThrough:
    """#466: root CLAUDE.md's own "Golden rule — always prefix shell commands
    with `rtk`" silently defeated every `_CMD_START`-anchored rule, because
    none of them recognised a command starting right after that mandated
    wrapper — only at true start-of-string, after a shell separator, or
    after `sudo `. Confirmed live before the fix: `rtk git push origin main
    --force`, `rtk taskkill /F /T /IM node.exe`, `rtk pip install -e .` were
    all silently ALLOWED for a guarded role, not even denied for the wrong
    reason — the rule simply never fired."""

    @pytest.mark.parametrize(
        "command",
        [
            "rtk git commit -m x",
            "rtk git push origin main --force",
            "rtk git reset --hard HEAD~1",
            "rtk git branch -D main",
            "rtk git stash",
            "rtk git -C x stash",
            "rtk taskkill /F /T /IM node.exe",
            "rtk pkill node",
            "rtk netsh wlan connect name=Guest",
            "rtk pip install -e .",
            "rtk proxy git commit -m x",
            "rtk proxy pip install -e .",
        ],
    )
    def test_still_denied_behind_rtk_prefix(self, command: str) -> None:
        verdict = pane_guard.classify(command, "backend")
        assert not verdict.allowed, f"rtk prefix must not bypass the guard: {command}"

    def test_own_worktree_push_still_allowed_behind_rtk_prefix(self) -> None:
        """The mandated prefix must not cost the pane its own #438 carve-out
        either — only widen what a bare `rtk` shell command can smuggle
        past, never narrow the legitimate path."""
        wt = r"C:\Users\dev\.agent-takkub\worktrees\tunnel\backend-1787986742"
        assert pane_guard.classify(
            "rtk git push -u origin wt/backend-1787986742", "backend", cwd=wt
        ).allowed
        assert pane_guard.classify(
            "rtk proxy git push -u origin wt/backend-1787986742", "backend", cwd=wt
        ).allowed

    @pytest.mark.parametrize(
        "command",
        [
            "echo 'rtk git commit is still just a suggestion'",
            "cat notes-about-rtk.md",
            "grep -rn 'rtk git' docs/",
        ],
    )
    def test_unrelated_rtk_mentions_allowed(self, command: str) -> None:
        assert pane_guard.classify(command, "backend").allowed, f"false positive: {command}"


class TestFailOpen:
    """The guard must never be able to wedge a pane or police a human."""

    @pytest.mark.parametrize("role", ["lead", "shell"])
    def test_user_driven_panes_never_guarded(self, role: str) -> None:
        assert pane_guard.classify("npx --yes playwright", role).allowed
        assert pane_guard.classify("find / -name x", role).allowed
        assert pane_guard.classify("taskkill /F /IM node.exe", role).allowed
        assert pane_guard.classify("netsh wlan connect name=Guest", role).allowed
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

    def test_host_network_rule_text_is_actionable(self) -> None:
        """Same contract as GUARD_RULE_TEXT: name the safe alternative
        (a second device), not just the prohibition."""
        assert "netsh" in pane_guard.HOST_NETWORK_RULE_TEXT.lower()
        assert "networksetup" in pane_guard.HOST_NETWORK_RULE_TEXT.lower()
        assert "400" in pane_guard.HOST_NETWORK_RULE_TEXT
        assert "มือถือ" in pane_guard.HOST_NETWORK_RULE_TEXT

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

    def test_full_suite_rule_text_is_actionable(self) -> None:
        """Same contract as GUARD_RULE_TEXT: name the safe alternative
        (qa-gate --targeted), not just the prohibition."""
        assert "qa-gate --targeted" in pane_guard.FULL_SUITE_RULE_TEXT
        assert "528" in pane_guard.FULL_SUITE_RULE_TEXT


class TestFullSuiteDenied:
    """#528: a raw, un-narrowed test-runner invocation forks every worker the
    runner owns and has pinned the user's box at 100% CPU/RAM more than once
    — #485's "targeted mid-batch, full gate once via qa-gate" was prose-only
    until this rule."""

    @pytest.mark.parametrize(
        "command",
        [
            "pytest",
            "pytest .",
            "pytest ./",
            "python -m pytest",
            "python3 -m pytest",
            "py.test",
            "pytest -v --tb=short",
            "vitest run",
            "npx vitest run",
            "pnpm exec vitest run",
            "yarn dlx vitest run",
            "jest",
            "npx jest",
            "jest --ci",
            "turbo run test",
            "turbo run test --cache-dir=.turbo",
            "pnpm -r test",
            "pnpm --recursive test",
            "yarn workspaces run test",
        ],
    )
    def test_denied_for_backend(self, command: str) -> None:
        verdict = pane_guard.classify(command, "backend")
        assert not verdict.allowed, f"should have blocked: {command}"
        assert verdict.rule.startswith("full_suite:")
        assert "qa-gate" in verdict.reason

    @pytest.mark.parametrize("role", ["qa", "frontend", "devops", "reviewer"])
    def test_denied_for_every_role_including_qa(self, role: str) -> None:
        """qa is the role that ultimately owns full-suite verification, but
        it must reach for `takkub qa-gate --auto` too, never a raw runner —
        no role-based allowlist for this rule, except `tester` (see
        TestFullSuiteTesterExemption below)."""
        assert not pane_guard.classify("pytest", role).allowed

    def test_denied_for_shard(self) -> None:
        assert not pane_guard.classify("vitest run", "frontend#2").allowed


class TestFullSuiteTesterExemption:
    """`tester` is an optional, on-demand role (.claude/agents/tester.md)
    whose entire job is running the raw test runner on its own pane so other
    roles stop each forking one in parallel — it is the one deliberate
    allowlist entry for this rule."""

    @pytest.mark.parametrize(
        "command",
        ["pytest", "python -m pytest", "vitest run", "jest", "turbo run test", "pnpm -r test"],
    )
    def test_raw_runner_allowed_for_tester(self, command: str) -> None:
        assert pane_guard.classify(command, "tester").allowed, f"should have allowed: {command}"

    def test_raw_runner_allowed_for_tester_shard(self) -> None:
        assert pane_guard.classify("pytest", "tester#2").allowed

    def test_tester_still_denied_by_other_rules(self) -> None:
        """The exemption is narrow to `_full_suite_rule` — every other guard
        (version control, host-destructive, ...) still applies to `tester`."""
        assert not pane_guard.classify("git commit -m 'x'", "tester").allowed
        assert not pane_guard.classify("git push --force", "tester").allowed


class TestFullSuiteAllowed:
    @pytest.mark.parametrize(
        "command",
        [
            # exactly the shape this project's own sessions already use
            "pytest tests/test_worktree_manager.py tests/test_cli.py -k 'worktree or Merge'",
            "pytest tests/test_pane_guard.py",
            "pytest -k test_something",
            "pytest -m 'not slow'",
            "python -m pytest tests/test_x.py::TestY::test_z",
            "vitest run src/foo.test.ts",
            "npx vitest run src/foo.test.ts",
            "vitest related --run src/foo.ts",
            "vitest --run -t 'renders correctly'",
            "jest src/foo.test.js",
            "jest -t 'renders correctly'",
            "jest --findRelatedTests src/foo.ts",
            "turbo run test --filter=web",
            "turbo run test --filter web",
            "pnpm --recursive test --filter=web",
            # unrelated commands that merely mention these words in passing
            "npm run test:unit",
            "npm test",
            "cat vitest.config.ts",
            "grep -rn pytest tests/",
            "echo 'run pytest before you push'",
        ],
    )
    def test_allowed(self, command: str) -> None:
        assert pane_guard.classify(command, "backend").allowed, f"false positive: {command}"

    def test_qa_gate_itself_never_matches(self) -> None:
        """`takkub qa-gate`'s internal test runs happen inside the CLI
        process via subprocess.run — never through a Bash tool call this
        hook can see — so the literal `takkub qa-gate ...` invocation must
        never itself trip this rule for any role."""
        assert pane_guard.classify("takkub qa-gate --targeted src/foo.ts", "qa").allowed
        assert pane_guard.classify("takkub qa-gate --auto", "qa").allowed


class TestScopeBudgetGuards:
    """#585: When task scope is tiny, block takkub qa-gate and full test suite,
    while allowing targeted test executions."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "takkub qa-gate",
            "takkub qa-gate --auto",
            "takkub qa-gate --targeted src/foo.ts",
        ],
    )
    def test_qa_gate_denied_for_tiny_scope(self, cmd: str) -> None:
        for role in ["backend", "frontend", "qa"]:
            verdict = pane_guard.classify(cmd, role, scope="tiny")
            assert not verdict.allowed
            assert verdict.rule == "scope_tiny:qa_gate"
            assert "งานเล็ก" in verdict.reason

    @pytest.mark.parametrize(
        "cmd",
        [
            "pytest",
            "npm test",
            "pnpm test",
            "npm run test",
            "yarn test",
            "vitest run",
        ],
    )
    def test_full_suite_denied_for_tiny_scope(self, cmd: str) -> None:
        for role in ["backend", "frontend", "tester"]:
            verdict = pane_guard.classify(cmd, role, scope="tiny")
            assert not verdict.allowed
            assert verdict.rule.startswith("scope_tiny")
            assert "งานเล็ก" in verdict.reason

    @pytest.mark.parametrize(
        "cmd",
        [
            "pytest tests/test_task_scope.py",
            "python -m pytest tests/test_task_scope.py",
            "vitest run src/foo.test.ts",
            "npm test -- tests/foo.test.ts",
            "git status",
            "git diff",
        ],
    )
    def test_targeted_tests_and_other_commands_allowed_for_tiny_scope(self, cmd: str) -> None:
        verdict = pane_guard.classify(cmd, "backend", scope="tiny")
        assert verdict.allowed


class TestBusyMachineDenied:
    """#585 round 2: When machine is busy (other pane working or RAM/CPU overloaded),
    deny heavy build or full test suite (within 30s of snapshot).
    Allow targeted tests, dev server, and commands when snapshot is stale or machine is idle."""

    def test_heavy_build_denied_when_other_pane_working(self, tmp_path) -> None:
        import json
        from datetime import datetime

        snap = tmp_path / "last-session.json"
        data = {
            "saved_at": datetime.now().isoformat(),
            "working_panes": ["frontend"],
            "overloaded": False,
            "ram_percent": 50.0,
            "cpu_percent": 30.0,
        }
        snap.write_text(json.dumps(data), encoding="utf-8")

        for cmd in [
            "npm run build",
            "pnpm run build",
            "yarn run build",
            "bun run build",
            "next build",
            "vite build",
            "tsc -b",
            "tsc -p tsconfig.json",
            "pytest",
            "npm test",
            "vitest run",
            "playwright test",
            "takkub qa-gate",
        ]:
            verdict = pane_guard.classify(cmd, "backend", snapshot_path=snap)
            assert not verdict.allowed, f"Should be blocked: {cmd}"
            assert verdict.rule.startswith("busy_machine:")
            assert "เครื่องกำลังไม่ว่าง" in verdict.reason

    def test_heavy_build_denied_when_overloaded(self, tmp_path) -> None:
        import json
        from datetime import datetime

        snap = tmp_path / "last-session.json"
        data = {
            "saved_at": datetime.now().isoformat(),
            "working_panes": [],
            "overloaded": True,
            "ram_percent": 88.0,
            "cpu_percent": 40.0,
        }
        snap.write_text(json.dumps(data), encoding="utf-8")

        verdict = pane_guard.classify("npm run build", "backend", snapshot_path=snap)
        assert not verdict.allowed
        assert verdict.rule.startswith("busy_machine:")

    def test_targeted_tests_and_dev_servers_allowed_when_busy(self, tmp_path) -> None:
        import json
        from datetime import datetime

        snap = tmp_path / "last-session.json"
        data = {
            "saved_at": datetime.now().isoformat(),
            "working_panes": ["frontend"],
            "overloaded": False,
            "ram_percent": 50.0,
            "cpu_percent": 30.0,
        }
        snap.write_text(json.dumps(data), encoding="utf-8")

        for cmd in [
            "pytest tests/test_foo.py",
            "tsc src/foo.ts",
            "npm run dev",
            "next dev",
            "vite dev",
            "npm test -- src/foo.test.ts",
        ]:
            verdict = pane_guard.classify(cmd, "backend", snapshot_path=snap)
            assert verdict.allowed, f"Should be allowed: {cmd}"

        # Browser role running targeted playwright test is allowed
        verdict = pane_guard.classify("playwright test tests/foo.spec.ts", "qa", snapshot_path=snap)
        assert verdict.allowed

    def test_stale_snapshot_passes_through(self, tmp_path) -> None:
        import json
        import os
        from datetime import datetime

        snap = tmp_path / "last-session.json"
        stale_time = datetime.now().timestamp() - 60
        data = {
            "saved_at": datetime.fromtimestamp(stale_time).isoformat(),
            "working_panes": ["frontend"],
            "overloaded": True,
        }
        snap.write_text(json.dumps(data), encoding="utf-8")
        os.utime(snap, (stale_time, stale_time))

        verdict = pane_guard.classify("npm run build", "backend", snapshot_path=snap)
        assert verdict.allowed

    def test_pane_does_not_block_itself_as_working(self, tmp_path) -> None:
        import json
        from datetime import datetime

        snap = tmp_path / "last-session.json"
        data = {
            "saved_at": datetime.now().isoformat(),
            "working_panes": ["backend"],  # own role
            "overloaded": False,
            "ram_percent": 50.0,
            "cpu_percent": 30.0,
        }
        snap.write_text(json.dumps(data), encoding="utf-8")

        verdict = pane_guard.classify("npm run build", "backend", snapshot_path=snap)
        assert verdict.allowed

    def test_lead_is_denied_heavy_build_when_machine_busy(self, tmp_path) -> None:
        import json
        from datetime import datetime

        snap = tmp_path / "last-session.json"
        data = {
            "saved_at": datetime.now().isoformat(),
            "working_panes": ["frontend"],
            "overloaded": False,
            "ram_percent": 50.0,
            "cpu_percent": 30.0,
        }
        snap.write_text(json.dumps(data), encoding="utf-8")

        # Lead running heavy build or full suite is denied (#585 round 4 item 3)
        v1 = pane_guard.classify("npm run build", "lead", snapshot_path=snap)
        assert not v1.allowed
        assert v1.rule == "busy_machine:pm_build"
        assert "เครื่องกำลังไม่ว่าง" in v1.reason

        v2 = pane_guard.classify("pytest", "lead", snapshot_path=snap)
        assert not v2.allowed
        assert v2.rule == "busy_machine:pytest"

    def test_shell_is_exempt_from_busy_machine_gate(self, tmp_path) -> None:
        import json
        from datetime import datetime

        snap = tmp_path / "last-session.json"
        data = {
            "saved_at": datetime.now().isoformat(),
            "working_panes": ["frontend"],
            "overloaded": True,
            "ram_percent": 95.0,
            "cpu_percent": 95.0,
        }
        snap.write_text(json.dumps(data), encoding="utf-8")

        # shell is user-driven pane — never blocked (#585 round 4 item 3)
        v1 = pane_guard.classify("npm run build", "shell", snapshot_path=snap)
        assert v1.allowed

        v2 = pane_guard.classify("pytest", "shell", snapshot_path=snap)
        assert v2.allowed

    def test_human_terminal_is_exempt_from_busy_machine_gate(self, tmp_path) -> None:
        import json
        from datetime import datetime

        snap = tmp_path / "last-session.json"
        data = {
            "saved_at": datetime.now().isoformat(),
            "working_panes": ["frontend"],
            "overloaded": True,
            "ram_percent": 95.0,
            "cpu_percent": 95.0,
        }
        snap.write_text(json.dumps(data), encoding="utf-8")

        # human outside cockpit (role=None) is never blocked
        v1 = pane_guard.classify("npm run build", None, snapshot_path=snap)
        assert v1.allowed


class TestLeadDirectEdit:
    """#585 round 2: Lead tiny-fix carve-out.
    <=2 files, <=15 lines per call, cumulative <=2 files/30 lines per task, non-deep."""

    def test_allowed_when_tiny_and_within_budget(self, tmp_path) -> None:
        state_file = tmp_path / "state.json"
        verdict = pane_guard.evaluate_lead_direct_edit(
            "Edit",
            {
                "file_path": "src/agent_takkub/foo.py",
                "old_string": "x = 1\n",
                "new_string": "x = 2\n",
            },
            scope="tiny",
            state_file=state_file,
        )
        assert verdict.allowed

    def test_denied_when_scope_is_not_tiny(self, tmp_path) -> None:
        state_file = tmp_path / "state.json"
        verdict = pane_guard.evaluate_lead_direct_edit(
            "Edit",
            {
                "file_path": "src/agent_takkub/foo.py",
                "old_string": "x = 1\n",
                "new_string": "x = 2\n",
            },
            scope="normal",
            state_file=state_file,
        )
        assert not verdict.allowed
        assert verdict.rule == "lead_direct_edit:normal_scope"

    def test_denied_when_deep_category_file(self, tmp_path) -> None:
        state_file = tmp_path / "state.json"
        for bad_file in [
            "prisma/schema.prisma",
            "src/auth/jwt.py",
            "package.json",
            "package-lock.json",
            ".github/workflows/ci.yml",
            "docker-compose.yml",
        ]:
            verdict = pane_guard.evaluate_lead_direct_edit(
                "Edit",
                {
                    "file_path": bad_file,
                    "old_string": "x\n",
                    "new_string": "y\n",
                },
                scope="tiny",
                state_file=state_file,
            )
            assert not verdict.allowed
            assert verdict.rule == "lead_direct_edit:deep_category"

    def test_denied_when_lines_per_call_exceeds_15(self, tmp_path) -> None:
        state_file = tmp_path / "state.json"
        verdict = pane_guard.evaluate_lead_direct_edit(
            "Edit",
            {
                "file_path": "src/agent_takkub/foo.py",
                "old_string": "\n".join(f"line {i}" for i in range(20)),
                "new_string": "short\n",
            },
            scope="tiny",
            state_file=state_file,
        )
        assert not verdict.allowed
        assert verdict.rule == "lead_direct_edit:lines_per_call"

    def test_denied_when_cumulative_files_exceeds_2(self, tmp_path) -> None:
        state_file = tmp_path / "state.json"
        v1 = pane_guard.evaluate_lead_direct_edit(
            "Edit",
            {"file_path": "src/file1.py", "old_string": "a\n", "new_string": "b\n"},
            scope="tiny",
            state_file=state_file,
        )
        assert v1.allowed

        v2 = pane_guard.evaluate_lead_direct_edit(
            "Edit",
            {"file_path": "src/file2.py", "old_string": "a\n", "new_string": "b\n"},
            scope="tiny",
            state_file=state_file,
        )
        assert v2.allowed

        v3 = pane_guard.evaluate_lead_direct_edit(
            "Edit",
            {"file_path": "src/file3.py", "old_string": "a\n", "new_string": "b\n"},
            scope="tiny",
            state_file=state_file,
        )
        assert not v3.allowed
        assert v3.rule == "lead_direct_edit:cumulative_files"

    def test_denied_when_cumulative_lines_exceeds_30(self, tmp_path) -> None:
        state_file = tmp_path / "state.json"
        v1 = pane_guard.evaluate_lead_direct_edit(
            "Edit",
            {
                "file_path": "src/file1.py",
                "old_string": "\n".join(f"x{i}" for i in range(15)),
                "new_string": "y\n",
            },
            scope="tiny",
            state_file=state_file,
        )
        assert v1.allowed

        v2 = pane_guard.evaluate_lead_direct_edit(
            "Edit",
            {
                "file_path": "src/file1.py",
                "old_string": "\n".join(f"z{i}" for i in range(15)),
                "new_string": "w\n",
            },
            scope="tiny",
            state_file=state_file,
        )
        assert v2.allowed

        v3 = pane_guard.evaluate_lead_direct_edit(
            "Edit",
            {"file_path": "src/file1.py", "old_string": "a\nb\n", "new_string": "c\n"},
            scope="tiny",
            state_file=state_file,
        )
        assert not v3.allowed
        assert v3.rule == "lead_direct_edit:cumulative_lines"

    def test_rolling_window_resets_after_30_minutes(self, tmp_path) -> None:
        import json
        from datetime import datetime, timedelta

        state_file = tmp_path / "state.json"
        # Reach 30 lines limit
        v1 = pane_guard.evaluate_lead_direct_edit(
            "Edit",
            {
                "file_path": "src/file1.py",
                "old_string": "\n".join(f"x{i}" for i in range(15)),
                "new_string": "y\n",
            },
            state_file=state_file,
        )
        assert v1.allowed
        v2 = pane_guard.evaluate_lead_direct_edit(
            "Edit",
            {
                "file_path": "src/file1.py",
                "old_string": "\n".join(f"z{i}" for i in range(15)),
                "new_string": "w\n",
            },
            state_file=state_file,
        )
        assert v2.allowed

        # 3rd edit is denied (exceeds 30 cumulative lines)
        v3 = pane_guard.evaluate_lead_direct_edit(
            "Edit",
            {"file_path": "src/file1.py", "old_string": "a\n", "new_string": "b\n"},
            state_file=state_file,
        )
        assert not v3.allowed
        assert v3.rule == "lead_direct_edit:cumulative_lines"

        # Backdate updated_at by 31 minutes (> 1800 seconds)
        s_data = json.loads(state_file.read_text(encoding="utf-8"))
        old_time = datetime.now() - timedelta(minutes=31)
        s_data["updated_at"] = old_time.isoformat()
        state_file.write_text(json.dumps(s_data), encoding="utf-8")

        # Now edit should be allowed because rolling window reset (#585 round 4 item 1)
        v4 = pane_guard.evaluate_lead_direct_edit(
            "Edit",
            {"file_path": "src/file1.py", "old_string": "a\n", "new_string": "b\n"},
            state_file=state_file,
        )
        assert v4.allowed

        # Check that state is reset to only lines from v4
        s_new = json.loads(state_file.read_text(encoding="utf-8"))
        assert s_new["total_lines"] == 1

    def test_reset_lead_edits_function(self, tmp_path) -> None:
        state_file = tmp_path / "state.json"
        v1 = pane_guard.evaluate_lead_direct_edit(
            "Edit",
            {"file_path": "src/file1.py", "old_string": "a\n", "new_string": "b\n"},
            state_file=state_file,
        )
        assert v1.allowed
        v2 = pane_guard.evaluate_lead_direct_edit(
            "Edit",
            {"file_path": "src/file2.py", "old_string": "a\n", "new_string": "b\n"},
            state_file=state_file,
        )
        assert v2.allowed
        v3 = pane_guard.evaluate_lead_direct_edit(
            "Edit",
            {"file_path": "src/file3.py", "old_string": "a\n", "new_string": "b\n"},
            state_file=state_file,
        )
        assert not v3.allowed
        assert v3.rule == "lead_direct_edit:cumulative_files"

        # Explicit reset via reset_lead_edits (#585 round 4 item 1)
        res = pane_guard.reset_lead_edits(state_file=state_file)
        assert res is True
        assert not state_file.is_file()

        # Next edit is allowed again
        v4 = pane_guard.evaluate_lead_direct_edit(
            "Edit",
            {"file_path": "src/file3.py", "old_string": "a\n", "new_string": "b\n"},
            state_file=state_file,
        )
        assert v4.allowed

    def test_deny_message_informs_about_auto_reset_and_command(self, tmp_path) -> None:
        state_file = tmp_path / "state.json"
        v1 = pane_guard.evaluate_lead_direct_edit(
            "Edit",
            {
                "file_path": "src/file1.py",
                "old_string": "\n".join(f"x{i}" for i in range(15)),
                "new_string": "y\n",
            },
            state_file=state_file,
        )
        assert v1.allowed
        v2 = pane_guard.evaluate_lead_direct_edit(
            "Edit",
            {
                "file_path": "src/file1.py",
                "old_string": "\n".join(f"z{i}" for i in range(15)),
                "new_string": "w\n",
            },
            state_file=state_file,
        )
        assert v2.allowed
        v3 = pane_guard.evaluate_lead_direct_edit(
            "Edit",
            {"file_path": "src/file1.py", "old_string": "a\n", "new_string": "b\n"},
            state_file=state_file,
        )
        assert not v3.allowed
        assert "เพดานจะรีเซ็ตเองใน" in v3.reason
        assert "takkub lead-edits --reset" in v3.reason

    def test_get_lead_edits_status(self, tmp_path) -> None:
        state_file = tmp_path / "state.json"
        status_empty = pane_guard.get_lead_edits_status(state_file=state_file)
        assert status_empty["files_count"] == 0
        assert status_empty["total_lines"] == 0

        pane_guard.evaluate_lead_direct_edit(
            "Edit",
            {"file_path": "src/file1.py", "old_string": "a\n", "new_string": "b\n"},
            state_file=state_file,
        )
        status_after = pane_guard.get_lead_edits_status(state_file=state_file)
        assert status_after["files_count"] == 1
        assert status_after["total_lines"] == 1
        assert status_after["updated_at"] is not None


class TestLeadDirectEditSensitiveKeywordVsTestPath:
    """#611: Lead's own `apps/api/src/security-e2e/test-harness.ts` type-fix
    (1 line) was denied because "security" sits in the folder name
    (`security-e2e/`), not because anything sensitive was actually touched —
    forcing a full backend-pane spawn for a one-line type fix. The
    sensitive-keyword half of the deep-category check (auth/security/token/
    crypto/payment) must skip a test/e2e path on the PATH leg, while still
    catching a genuinely sensitive DIFF hiding inside one."""

    def test_allowed_type_fix_in_security_e2e_test_file(self, tmp_path) -> None:
        state_file = tmp_path / "state.json"
        verdict = pane_guard.evaluate_lead_direct_edit(
            "Edit",
            {
                "file_path": "apps/api/src/security-e2e/test-harness.ts",
                "old_string": "const id: number = getId();",
                "new_string": "const id: string = getId();",
            },
            scope="tiny",
            state_file=state_file,
        )
        assert verdict.allowed

    @pytest.mark.parametrize(
        "file_path",
        [
            "apps/api/src/security-e2e/test-harness.ts",
            "src/auth/__tests__/login.spec.ts",
            "tests/test_auth.py",
            "src/payments.test.ts",
        ],
    )
    def test_allowed_when_test_path_and_diff_has_no_sensitive_content(
        self, tmp_path, file_path: str
    ) -> None:
        state_file = tmp_path / f"state-{hash(file_path)}.json"
        verdict = pane_guard.evaluate_lead_direct_edit(
            "Edit",
            {"file_path": file_path, "old_string": "x = 1\n", "new_string": "x = 2\n"},
            scope="tiny",
            state_file=state_file,
        )
        assert verdict.allowed, f"should allow non-sensitive edit in test path: {file_path}"

    def test_denied_when_source_auth_path_even_with_bland_diff(self, tmp_path) -> None:
        """A genuinely deep SOURCE path (not a test path) still denies by
        path alone, same as before #611 — only test/e2e paths are exempted
        from the path leg."""
        state_file = tmp_path / "state.json"
        verdict = pane_guard.evaluate_lead_direct_edit(
            "Edit",
            {"file_path": "src/auth/login.ts", "old_string": "x\n", "new_string": "y\n"},
            scope="tiny",
            state_file=state_file,
        )
        assert not verdict.allowed
        assert verdict.rule == "lead_direct_edit:deep_category"

    def test_denied_when_sensitive_content_hides_inside_test_path(self, tmp_path) -> None:
        """The path leg is exempt for a test file, but the DIFF is still
        checked — a hardcoded secret/token sneaking into a test fixture must
        still deny."""
        state_file = tmp_path / "state.json"
        verdict = pane_guard.evaluate_lead_direct_edit(
            "Edit",
            {
                "file_path": "src/security-e2e/fixtures.ts",
                "old_string": "const apiKey = readFromEnv();",
                "new_string": 'const apiKey = "hardcoded-not-from-env";',
            },
            scope="tiny",
            state_file=state_file,
        )
        assert not verdict.allowed
        assert verdict.rule == "lead_direct_edit:deep_category"

    def test_structural_deep_category_unaffected_by_test_path(self, tmp_path) -> None:
        """package.json/lockfile/migration/CI/Dockerfile stay path-only with
        NO test-path exemption — those are deep because of WHERE the file
        lives, not a word that can appear in a folder name."""
        state_file = tmp_path / "state.json"
        for bad_file in [
            "tests/fixtures/package.json",
            "e2e/migrations/001_init.sql",
            "__tests__/.github/workflows/ci.yml",
        ]:
            verdict = pane_guard.evaluate_lead_direct_edit(
                "Edit",
                {"file_path": bad_file, "old_string": "x\n", "new_string": "y\n"},
                scope="tiny",
                state_file=state_file,
            )
            assert not verdict.allowed, f"structural deep category must still deny: {bad_file}"
            assert verdict.rule == "lead_direct_edit:deep_category"


class TestDirectEditExemptRuntime:
    """#587 F2: `_is_direct_edit_exempt`'s runtime carve-out must only apply
    to a `runtime/` directory sitting directly at a configured project root
    (or cockpit's own `config.RUNTIME_DIR`) — not any `runtime` segment
    anywhere in the path (previously exempted `src/runtime/app.py` too,
    letting Lead edit arbitrary project source unlimited)."""

    def test_runtime_at_project_root_is_exempt(self, tmp_path, monkeypatch) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        monkeypatch.setattr(
            "agent_takkub.lead_context._allowed_project_roots",
            lambda p: [root.resolve()],
        )
        file_path = str(root / "runtime" / "state.json")
        assert pane_guard._is_direct_edit_exempt(file_path, str(root), "proj") is True

    def test_runtime_nested_under_src_is_not_exempt(self, tmp_path, monkeypatch) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        monkeypatch.setattr(
            "agent_takkub.lead_context._allowed_project_roots",
            lambda p: [root.resolve()],
        )
        file_path = str(root / "src" / "runtime" / "app.py")
        assert pane_guard._is_direct_edit_exempt(file_path, str(root), "proj") is False

    def test_cockpit_runtime_dir_is_exempt_anywhere(self, tmp_path, monkeypatch) -> None:
        fake_runtime = tmp_path / "cockpit_runtime"
        monkeypatch.setattr("agent_takkub.config.RUNTIME_DIR", fake_runtime)
        file_path = str(fake_runtime / "lead_edits" / "proj.json")
        assert pane_guard._is_direct_edit_exempt(file_path, None, None) is True


class TestDeepCategoryOutranksRootExemption:
    """#587 F3: `evaluate_lead_direct_edit` must check the deep-file-category
    patterns BEFORE the runtime/outside-root exemption. A multi-root project
    (paths={"web": ".../pms-web"}) can easily leave a repo-level file like
    `package.json` outside every configured root — that must still deny,
    not slip past via the exemption meant for scratchpad/memory notes."""

    def test_deep_file_outside_every_root_is_still_denied(self, tmp_path, monkeypatch) -> None:
        repo_root = tmp_path / "proj"
        sub_root = repo_root / "pms-web"
        sub_root.mkdir(parents=True)
        monkeypatch.setattr(
            "agent_takkub.lead_context._allowed_project_roots",
            lambda p: [sub_root.resolve()],
        )
        state_file = tmp_path / "state.json"
        for bad_file in [
            str(repo_root / "package.json"),
            str(repo_root / ".github" / "workflows" / "ci.yml"),
        ]:
            verdict = pane_guard.evaluate_lead_direct_edit(
                "Write",
                {"file_path": bad_file, "content": "{}"},
                cwd=str(repo_root),
                project="proj",
                scope="tiny",
                state_file=state_file,
            )
            assert not verdict.allowed
            assert verdict.rule == "lead_direct_edit:deep_category"

    def test_non_deep_file_outside_every_root_still_exempt(self, tmp_path, monkeypatch) -> None:
        repo_root = tmp_path / "proj"
        sub_root = repo_root / "pms-web"
        sub_root.mkdir(parents=True)
        monkeypatch.setattr(
            "agent_takkub.lead_context._allowed_project_roots",
            lambda p: [sub_root.resolve()],
        )
        scratch = tmp_path / "scratch" / "note.json"
        scratch.parent.mkdir(parents=True)
        state_file = tmp_path / "state.json"
        verdict = pane_guard.evaluate_lead_direct_edit(
            "Write",
            {"file_path": str(scratch), "content": "x" * 500},
            cwd=str(repo_root),
            project="proj",
            scope="normal",
            state_file=state_file,
        )
        assert verdict.allowed


class TestLeadDirectEditIssues628And625:
    """#628: Snake_case, camelCase, PascalCase, dot-notation, and kebab-case
    sensitive identifiers must not escape sensitive_deep_patterns.
    #625: Scratchpad/temporary files outside project root must not be denied
    by sensitive content checks."""

    def test_scratchpad_outside_root_with_sensitive_content_is_exempt(
        self, tmp_path, monkeypatch
    ) -> None:
        """#625: Write to a scratchpad in temp or outside project root with sensitive
        keywords (e.g. echo check auth token flow) must be allowed."""
        repo_root = tmp_path / "proj"
        repo_root.mkdir()
        monkeypatch.setattr(
            "agent_takkub.lead_context._allowed_project_roots",
            lambda p: [repo_root.resolve()],
        )
        scratch_dir = tmp_path / "scratchpad"
        scratch_dir.mkdir()
        probe_sh = scratch_dir / "probe.sh"
        state_file = tmp_path / "state.json"
        verdict = pane_guard.evaluate_lead_direct_edit(
            "Write",
            {"file_path": str(probe_sh), "content": "echo check auth token flow\n"},
            cwd=str(repo_root),
            project="proj",
            scope="tiny",
            state_file=state_file,
        )
        assert verdict.allowed

    def test_scratchpad_outside_root_structural_deep_still_denied(
        self, tmp_path, monkeypatch
    ) -> None:
        """#625/#587 F3: Structural deep files (e.g. package.json, lockfile) outside root
        must still be denied everywhere."""
        repo_root = tmp_path / "proj"
        repo_root.mkdir()
        monkeypatch.setattr(
            "agent_takkub.lead_context._allowed_project_roots",
            lambda p: [repo_root.resolve()],
        )
        scratch_dir = tmp_path / "scratchpad"
        scratch_dir.mkdir()
        pkg = scratch_dir / "package.json"
        state_file = tmp_path / "state.json"
        verdict = pane_guard.evaluate_lead_direct_edit(
            "Write",
            {"file_path": str(pkg), "content": "{}\n"},
            cwd=str(repo_root),
            project="proj",
            scope="tiny",
            state_file=state_file,
        )
        assert not verdict.allowed
        assert verdict.rule == "lead_direct_edit:deep_category"

    @pytest.mark.parametrize(
        "file_name",
        [
            "migration-plan.html",
            "schema-plan.html",
            "migration_notes.txt",
            "schema.csv",
            "schema_diagram.png",
            "schema_export.jpg",
        ],
    )
    def test_scratchpad_outside_root_nonsource_schema_migration_allowed(
        self, tmp_path, monkeypatch, file_name: str
    ) -> None:
        """Non-source files (.html, .txt, .csv, .png, .jpg) outside project root
        containing schema/migration words must be allowed (e.g. scratchpad plans).
        Tests both Windows and POSIX path structures."""
        repo_root = tmp_path / "proj"
        repo_root.mkdir()
        monkeypatch.setattr(
            "agent_takkub.lead_context._allowed_project_roots",
            lambda p: [repo_root.resolve()],
        )
        state_file = tmp_path / "state.json"

        # 1. Windows / tmp_path fixture path
        scratch_win = tmp_path / "scratchpad" / file_name
        scratch_win.parent.mkdir(exist_ok=True)
        v_win = pane_guard.evaluate_lead_direct_edit(
            "Write",
            {"file_path": str(scratch_win), "content": "<h1>Plan</h1>\n"},
            cwd=str(repo_root),
            project="proj",
            scope="tiny",
            state_file=state_file,
        )
        assert v_win.allowed, f"Should allow {file_name} in scratchpad (Windows path)"

        # 2. POSIX path structure
        posix_path = f"/tmp/scratchpad/{file_name}"
        v_posix = pane_guard.evaluate_lead_direct_edit(
            "Write",
            {"file_path": posix_path, "content": "<h1>Plan</h1>\n"},
            cwd=str(repo_root),
            project="proj",
            scope="tiny",
            state_file=state_file,
        )
        assert v_posix.allowed, f"Should allow {posix_path} in scratchpad (POSIX path)"

    @pytest.mark.parametrize(
        "file_name",
        [
            "migration.py",
            "schema.sql",
            "schema.ts",
            "schema.json",
            "docker-compose.yml",
            "Dockerfile",
        ],
    )
    def test_scratchpad_outside_root_source_config_schema_migration_still_denied(
        self, tmp_path, monkeypatch, file_name: str
    ) -> None:
        """Source and config files (.py, .sql, .ts, .json, .yml, Dockerfile) outside project root
        that match structural patterns must still be denied."""
        repo_root = tmp_path / "proj"
        repo_root.mkdir()
        monkeypatch.setattr(
            "agent_takkub.lead_context._allowed_project_roots",
            lambda p: [repo_root.resolve()],
        )
        state_file = tmp_path / "state.json"

        # 1. Windows path
        scratch_win = tmp_path / "scratchpad" / file_name
        scratch_win.parent.mkdir(exist_ok=True)
        v_win = pane_guard.evaluate_lead_direct_edit(
            "Write",
            {"file_path": str(scratch_win), "content": "data = 1\n"},
            cwd=str(repo_root),
            project="proj",
            scope="tiny",
            state_file=state_file,
        )
        assert not v_win.allowed, f"Should deny source/config {file_name} in scratchpad"
        assert v_win.rule == "lead_direct_edit:deep_category"

        # 2. POSIX path
        posix_path = f"/tmp/scratchpad/{file_name}"
        v_posix = pane_guard.evaluate_lead_direct_edit(
            "Write",
            {"file_path": posix_path, "content": "data = 1\n"},
            cwd=str(repo_root),
            project="proj",
            scope="tiny",
            state_file=state_file,
        )
        assert not v_posix.allowed, f"Should deny POSIX source/config {posix_path}"
        assert v_posix.rule == "lead_direct_edit:deep_category"

    @pytest.mark.parametrize(
        "file_name",
        [
            "package.json",
            "requirements.txt",
            "pyproject.toml",
            "go.mod",
            "cargo.toml",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
        ],
    )
    def test_scratchpad_outside_root_manifests_lockfiles_still_denied(
        self, tmp_path, monkeypatch, file_name: str
    ) -> None:
        """Manifests and lockfiles must be denied everywhere, including scratchpad (#587 F3)."""
        repo_root = tmp_path / "proj"
        repo_root.mkdir()
        monkeypatch.setattr(
            "agent_takkub.lead_context._allowed_project_roots",
            lambda p: [repo_root.resolve()],
        )
        state_file = tmp_path / "state.json"

        scratch_win = tmp_path / "scratchpad" / file_name
        scratch_win.parent.mkdir(exist_ok=True)
        v_win = pane_guard.evaluate_lead_direct_edit(
            "Write",
            {"file_path": str(scratch_win), "content": "{}\n"},
            cwd=str(repo_root),
            project="proj",
            scope="tiny",
            state_file=state_file,
        )
        assert not v_win.allowed
        assert v_win.rule == "lead_direct_edit:deep_category"

        posix_path = f"/tmp/scratchpad/{file_name}"
        v_posix = pane_guard.evaluate_lead_direct_edit(
            "Write",
            {"file_path": posix_path, "content": "{}\n"},
            cwd=str(repo_root),
            project="proj",
            scope="tiny",
            state_file=state_file,
        )
        assert not v_posix.allowed
        assert v_posix.rule == "lead_direct_edit:deep_category"

    @pytest.mark.parametrize(
        "rel_path",
        [
            "migration-plan.html",
            "schema-plan.html",
            "src/schema.ts",
            "migrations/0001_init.sql",
        ],
    )
    def test_inside_project_root_structural_deep_denied(
        self, tmp_path, monkeypatch, rel_path: str
    ) -> None:
        """Inside project root, structural deep patterns (including html plans) are denied."""
        repo_root = tmp_path / "proj"
        repo_root.mkdir()
        monkeypatch.setattr(
            "agent_takkub.lead_context._allowed_project_roots",
            lambda p: [repo_root.resolve()],
        )
        state_file = tmp_path / "state.json"

        target_file = repo_root / rel_path
        target_file.parent.mkdir(parents=True, exist_ok=True)
        verdict = pane_guard.evaluate_lead_direct_edit(
            "Write",
            {"file_path": str(target_file), "content": "hello\n"},
            cwd=str(repo_root),
            project="proj",
            scope="tiny",
            state_file=state_file,
        )
        assert not verdict.allowed, f"Should deny inside root: {rel_path}"
        assert verdict.rule == "lead_direct_edit:deep_category"

    @pytest.mark.parametrize(
        "content",
        [
            "def check_auth_token(): pass\n",
            "refresh_token = get_token()\n",
            "def api_auth(): pass\n",
            "is_admin_user = True\n",
            "def bypass_auth(): pass\n",
            "def skip_auth_check(): pass\n",
            "def disable_signature_validation(): pass\n",
            "def bypass_signature(): pass\n",
            "api_key = read_key()\n",
            "get_secret_key()\n",
            "jwt_token = encode()\n",
            "user_password = hash_pass()\n",
            "user_permission = check()\n",
            "check_2fa(code)\n",
            "verify_otp(code)\n",
            "user_mfa(user)\n",
            "rate_limit = 100\n",
        ],
    )
    def test_snake_case_sensitive_identifiers_denied(self, tmp_path, content: str) -> None:
        """#628: Snake_case identifiers in project source must be caught by sensitive_deep_patterns."""
        state_file = tmp_path / "state.json"
        verdict = pane_guard.evaluate_lead_direct_edit(
            "Write",
            {"file_path": "src/agent_takkub/probe_tmp.py", "content": content},
            scope="tiny",
            project=None,
            state_file=state_file,
        )
        assert not verdict.allowed
        assert verdict.rule == "lead_direct_edit:deep_category"

    @pytest.mark.parametrize(
        "file_path",
        [
            "src/agent_takkub/check_auth_token.py",
            "src/agent_takkub/refresh_token.py",
            "src/agent_takkub/api_auth.py",
            "src/agent_takkub/is_admin_user.py",
        ],
    )
    def test_snake_case_sensitive_paths_denied(self, tmp_path, file_path: str) -> None:
        """#628: Snake_case file paths in project source must be caught by sensitive_deep_patterns."""
        state_file = tmp_path / "state.json"
        verdict = pane_guard.evaluate_lead_direct_edit(
            "Write",
            {"file_path": file_path, "content": "x = 1\n"},
            scope="tiny",
            project=None,
            state_file=state_file,
        )
        assert not verdict.allowed
        assert verdict.rule == "lead_direct_edit:deep_category"

    @pytest.mark.parametrize(
        "content",
        [
            "function checkAuthToken() {}\n",
            "const refreshToken = token;\n",
            "function apiAuth() {}\n",
            "const isAdminUser = true;\n",
            "function bypassAuth() {}\n",
            "function skipAuthCheck() {}\n",
            "disableSignatureValidation()\n",
            "const checkApiKey = key;\n",
            "getSecretKey()\n",
            "const jwtToken = sign();\n",
            "const userPassword = val;\n",
            "const userPermission = p;\n",
            "check2FA(code)\n",
            "verifyOtp(code)\n",
            "userMfa(user)\n",
            "const rateLimit = 50;\n",
        ],
    )
    def test_camel_case_sensitive_identifiers_denied(self, tmp_path, content: str) -> None:
        """#628: CamelCase identifiers in project source must be caught by sensitive_deep_patterns."""
        state_file = tmp_path / "state.json"
        verdict = pane_guard.evaluate_lead_direct_edit(
            "Write",
            {"file_path": "src/agent_takkub/probe_tmp.ts", "content": content},
            scope="tiny",
            project=None,
            state_file=state_file,
        )
        assert not verdict.allowed
        assert verdict.rule == "lead_direct_edit:deep_category"

    @pytest.mark.parametrize(
        "file_path",
        [
            "src/agent_takkub/checkAuthToken.ts",
            "src/agent_takkub/refreshToken.ts",
            "src/agent_takkub/apiAuth.ts",
            "src/agent_takkub/isAdminUser.ts",
        ],
    )
    def test_camel_case_sensitive_paths_denied(self, tmp_path, file_path: str) -> None:
        """#628: CamelCase file paths in project source must be caught by sensitive_deep_patterns."""
        state_file = tmp_path / "state.json"
        verdict = pane_guard.evaluate_lead_direct_edit(
            "Write",
            {"file_path": file_path, "content": "x = 1\n"},
            scope="tiny",
            project=None,
            state_file=state_file,
        )
        assert not verdict.allowed
        assert verdict.rule == "lead_direct_edit:deep_category"

    @pytest.mark.parametrize(
        "content",
        [
            'AUTH_TOKEN = "secret"\n',
            'API_KEY = "key123"\n',
            'JWT_SECRET = "jwt"\n',
            'url = "/api/check-auth-token"\n',
            "const userToken = req.auth_token;\n",
            "const k = config.apiKey;\n",
        ],
    )
    def test_screaming_snake_kebab_and_dot_notation_denied(self, tmp_path, content: str) -> None:
        """#628: SCREAMING_SNAKE, kebab-case, and dot-notation properties must be denied."""
        state_file = tmp_path / "state.json"
        verdict = pane_guard.evaluate_lead_direct_edit(
            "Write",
            {"file_path": "src/agent_takkub/probe_tmp.ts", "content": content},
            scope="tiny",
            project=None,
            state_file=state_file,
        )
        assert not verdict.allowed
        assert verdict.rule == "lead_direct_edit:deep_category"

    @pytest.mark.parametrize(
        "content",
        [
            'author_name = "Jane Doe"\n',
            "git_author = get_author()\n",
            "result = tokenizer.tokenize(text)\n",
            "on_key_press(keyboard_event)\n",
            "for key in mapping.keys(): pass\n",
            "def calculate_total(items): return sum(items)\n",
        ],
    )
    def test_false_positives_not_denied(self, tmp_path, content: str) -> None:
        """#628: Non-sensitive words (author, tokenize, keyboard, dict keys, normal logic) must be allowed."""
        state_file = tmp_path / "state.json"
        verdict = pane_guard.evaluate_lead_direct_edit(
            "Write",
            {"file_path": "src/agent_takkub/util.py", "content": content},
            scope="tiny",
            project=None,
            state_file=state_file,
        )
        assert verdict.allowed


class TestGitConfigInjectionRound5:
    """#609/#611 round 5: `git -c <key>=<value>`/`--config-env` config
    injection is a code-execution vector (`diff.external`, `core.sshCommand`,
    `core.hooksPath`, `alias.<name>=!<shell>`, …) — unconditional, no
    worktree carve-out, since the subprocess spawns under the caller's own
    account regardless of who owns the checkout. `GIT_*`-family env vars are
    the same class of injection via a different spelling."""

    _WT_CWD = r"C:\Users\dev\agent-takkub\worktrees\myproj\backend-3-1700000000"

    @pytest.mark.parametrize(
        "command",
        [
            "git -c diff.external=/tmp/evil.sh diff",
            "git -c core.sshCommand=/tmp/evil.sh fetch",
            "git -c core.hooksPath=/tmp/evil-hooks status",
            "git -c alias.discard=!rm status",
            "git -c merge.ours.driver=/tmp/evil.sh status",
            "git -c filter.lfs.smudge=/tmp/evil.sh status",
            "git --config-env=diff.external=EVIL_VAR diff",
            "git --config-env diff.external=EVIL_VAR diff",
            "git -c core.pager=/tmp/evil.sh log",
            "git -c credential.helper=/tmp/evil.sh status",
            "git -c protocol.ext.allow=always status",
        ],
    )
    def test_dangerous_dash_c_denied(self, command: str) -> None:
        verdict = pane_guard.classify(command, "backend")
        assert not verdict.allowed, command
        assert verdict.rule == "git_config_injection:dash_c"

    @pytest.mark.parametrize("cwd", [_WT_CWD, None])
    def test_dangerous_dash_c_denied_even_in_own_worktree(self, cwd: str | None) -> None:
        """RCE doesn't care whether the invocation happens to be inside the
        role's own worktree — no carve-out at all, unlike `commit`/`merge`."""
        verdict = pane_guard.classify("git -c diff.external=/tmp/evil.sh diff", "backend", cwd=cwd)
        assert not verdict.allowed

    @pytest.mark.parametrize(
        "command",
        [
            "git -c color.ui=always status",
            "git -c core.quotepath=true status",
            "git -c core.pager=cat log",
            "git -c core.pager=less log",
            "git -c log.date=relative log",
            "git -c diff.renames=true diff",
            "git -c diff.algorithm=histogram diff",
            "git -c status.short=true status",
            "git -c advice.detachedHead=false status",
            "git -c i18n.commitEncoding=utf-8 log",
            "git -c safe.directory=* status",
            "git -c credential.helper= status",
        ],
    )
    def test_safe_dash_c_keys_allowed(self, command: str) -> None:
        assert pane_guard.classify(command, "backend").allowed, command

    @pytest.mark.parametrize(
        "command",
        [
            "GIT_SSH_COMMAND=/tmp/evil.sh git fetch origin",
            "GIT_EXTERNAL_DIFF=/tmp/evil.sh git diff",
            "GIT_EDITOR=/tmp/evil.sh git commit",
            "GIT_SEQUENCE_EDITOR=/tmp/evil.sh git rebase -i HEAD~2",
            "GIT_CONFIG_PARAMETERS=\"'diff.external=/tmp/evil.sh'\" git diff",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=diff.external GIT_CONFIG_VALUE_0=/tmp/evil.sh git diff",
            "GIT_TEMPLATE_DIR=/tmp/evil-template git init",
            "GIT_EXEC_PATH=/tmp/evil-bin git status",
            "GIT_PAGER=/tmp/evil.sh git log",
        ],
    )
    def test_dangerous_env_var_denied(self, command: str) -> None:
        verdict = pane_guard.classify(command, "backend")
        assert not verdict.allowed, command
        assert verdict.rule == "git_config_injection:env_var"

    def test_git_pager_safe_value_allowed(self) -> None:
        assert pane_guard.classify("GIT_PAGER=cat git log", "backend").allowed


class TestGitConfigSubcommandRound5:
    """#609/#611 round 5: `git config` writes the repo's SHARED
    `.git/config` from any linked worktree by default — a write is denied on
    every cwd except a safe-listed key, or `user.name`/`user.email` set
    locally from the role's own worktree."""

    _WT_CWD = r"C:\Users\dev\agent-takkub\worktrees\myproj\backend-3-1700000000"

    @pytest.mark.parametrize(
        "command",
        [
            "git config --get user.name",
            "git config --get-all user.name",
            "git config --list",
            "git config -l",
            "git config --show-origin --get user.name",
            "git config merge.ff",  # bare key = read
        ],
    )
    def test_read_forms_always_allowed(self, command: str) -> None:
        assert pane_guard.classify(command, "backend").allowed, command
        assert pane_guard.classify(command, "backend", cwd=self._WT_CWD).allowed, command

    @pytest.mark.parametrize(
        "command",
        [
            "git config merge.ff false",
            "git config diff.external /tmp/evil.sh",
            "git config core.hooksPath /tmp/evil-hooks",
            "git config --add remote.origin.fetch +refs/*:refs/*",
            "git config --unset merge.ff",
            "git config --replace-all user.name test",
            "git config --rename-section a b",
            "git config --edit",
            "git config --global user.name test",
            "git config --file /tmp/x.cfg user.name test",
        ],
    )
    def test_write_forms_denied_on_shared_tree(self, command: str) -> None:
        verdict = pane_guard.classify(command, "backend")
        assert not verdict.allowed, command
        assert verdict.rule == "git_lead_only:config"

    @pytest.mark.parametrize(
        "command",
        [
            "git config diff.external /tmp/evil.sh",
            "git config core.hooksPath /tmp/evil-hooks",
            "git config --global user.name test",
        ],
    )
    def test_write_forms_denied_even_from_own_worktree(self, command: str) -> None:
        """Linked worktrees share one `.git/config` by default — no
        ownership carve-out for a dangerous key or a non-local scope."""
        assert not pane_guard.classify(command, "backend", cwd=self._WT_CWD).allowed

    @pytest.mark.parametrize(
        "command", ["git config core.quotepath true", "git config status.short true"]
    )
    def test_safe_key_write_allowed_on_any_cwd(self, command: str) -> None:
        assert pane_guard.classify(command, "backend").allowed, command
        assert pane_guard.classify(command, "backend", cwd=self._WT_CWD).allowed, command

    @pytest.mark.parametrize("key", ["user.name", "user.email"])
    def test_user_identity_write_allowed_local_in_own_worktree(self, key: str) -> None:
        assert pane_guard.classify(f"git config {key} test", "backend", cwd=self._WT_CWD).allowed

    @pytest.mark.parametrize("key", ["user.name", "user.email"])
    def test_user_identity_write_denied_on_shared_tree(self, key: str) -> None:
        assert not pane_guard.classify(f"git config {key} test", "backend").allowed


class TestGitBranchTagFetchMvRmRound5:
    """#609/#611 round 5: ref-overwrite and shared-file-overwrite forms of
    `branch`/`tag`/`fetch`/`mv`/`rm` that #609 round 4's "fully permissive"
    allow-list missed."""

    _WT_CWD = r"C:\Users\dev\agent-takkub\worktrees\myproj\backend-3-1700000000"

    @pytest.mark.parametrize(
        "command",
        [
            "git branch -d merged-branch",
            "git branch -m old new",
            "git branch -u origin/main",
            "git branch --set-upstream-to=origin/main",
        ],
    )
    def test_branch_mutating_flags_denied_on_shared_tree(self, command: str) -> None:
        verdict = pane_guard.classify(command, "backend")
        assert not verdict.allowed, command
        assert verdict.rule == "git_shared_default_deny:branch"

    def test_branch_create_allowed_on_shared_tree(self) -> None:
        assert pane_guard.classify("git branch new-feature", "backend").allowed

    @pytest.mark.parametrize(
        "command",
        ["git tag v1.0.0", "git tag -a v1.0.0 -m x", "git tag -f v1.0.0", "git tag -s v1.0.0"],
    )
    def test_tag_create_or_force_denied_on_shared_tree(self, command: str) -> None:
        verdict = pane_guard.classify(command, "backend")
        assert not verdict.allowed, command
        assert verdict.rule == "git_shared_default_deny:tag"

    @pytest.mark.parametrize("command", ["git tag", "git tag -l", "git tag --merged"])
    def test_tag_list_allowed_on_shared_tree(self, command: str) -> None:
        assert pane_guard.classify(command, "backend").allowed, command

    @pytest.mark.parametrize(
        "command",
        [
            "git fetch origin +refs/heads/*:refs/remotes/origin/*",
            "git fetch origin refs/heads/main:refs/heads/main",
            "git fetch --force origin",
            "git fetch --refmap=refs/heads/*:refs/remotes/origin/* origin",
            "git fetch --update-head-ok origin",
        ],
    )
    def test_fetch_refspec_or_force_denied_on_shared_tree(self, command: str) -> None:
        verdict = pane_guard.classify(command, "backend")
        assert not verdict.allowed, command
        assert verdict.rule == "git_shared_default_deny:fetch"

    @pytest.mark.parametrize(
        "command", ["git fetch", "git fetch origin", "git fetch origin main", "git fetch --prune"]
    )
    def test_plain_fetch_allowed_on_shared_tree(self, command: str) -> None:
        assert pane_guard.classify(command, "backend").allowed, command

    @pytest.mark.parametrize("command", ["git mv -f a b", "git rm -f a"])
    def test_mv_rm_force_denied_on_shared_tree(self, command: str) -> None:
        assert not pane_guard.classify(command, "backend").allowed, command

    @pytest.mark.parametrize("command", ["git mv a b", "git rm a"])
    def test_mv_rm_plain_allowed_on_shared_tree(self, command: str) -> None:
        assert pane_guard.classify(command, "backend").allowed, command

    @pytest.mark.parametrize(
        "command", ["git init --template=/tmp/evil", "git clone --template=/tmp/evil https://x/y"]
    )
    def test_init_clone_template_denied_unconditionally(self, command: str) -> None:
        verdict = pane_guard.classify(command, "backend")
        assert not verdict.allowed, command
        assert verdict.rule == "git_lead_only:init-clone-template"
        assert not pane_guard.classify(command, "backend", cwd=self._WT_CWD).allowed


class TestGitDefaultDenyConfirmedRound5:
    """#609/#611 round 5 item 4: confirm the round-4 shared-tree default-deny
    already catches these ref-mutating subcommands nobody explicitly
    allow-listed — one line each, per Lead's instruction."""

    @pytest.mark.parametrize(
        "command",
        [
            "git reflog expire --expire=now --all",
            "git reflog delete HEAD@{0}",
            "git update-ref refs/heads/main HEAD~1",
            "git symbolic-ref HEAD refs/heads/other",
            "git replace HEAD~1 HEAD~2",
            "git notes add -m x HEAD",
            "git notes remove HEAD",
        ],
    )
    def test_ref_mutating_subcommand_denied_on_shared_tree(self, command: str) -> None:
        assert not pane_guard.classify(command, "backend").allowed, command


class TestPythonMAgentTakkubDenied:
    """#632: Specialist roles must invoke CLI via `takkub <cmd>`, never `python -m agent_takkub`.
    Running `python -m agent_takkub` boots the GUI cockpit.
    Submodules (e.g. `python -m agent_takkub.cli`) stay allowed.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "python -m agent_takkub report build --type customer",
            "python -m agent_takkub",
            "python.exe -m agent_takkub list",
            "python3 -m agent_takkub done 'finished'",
            "py -m agent_takkub wait",
            "cmd /c 'python -m agent_takkub report build'",
            "cd some/dir && python -m agent_takkub report build",
        ],
    )
    def test_denied_for_specialist(self, command: str) -> None:
        verdict = pane_guard.classify(command, "backend")
        assert not verdict.allowed, command
        assert verdict.rule == "cli_invocation:python_m_agent_takkub"

    @pytest.mark.parametrize(
        "command",
        [
            "python -m agent_takkub.cli report build",
            "python -m agent_takkub.headless",
            "python -m agent_takkub.custom_module",
            "python -m pytest tests/",
            "takkub report build --type customer",
            "takkub done 'finished'",
        ],
    )
    def test_allowed_commands(self, command: str) -> None:
        assert pane_guard.classify(command, "backend").allowed, command

    def test_app_boot_guard_for_lead_and_shell(self) -> None:
        # #633: bare python -m agent_takkub is denied for all roles including lead and shell
        assert not pane_guard.classify("python -m agent_takkub report build", "lead").allowed
        assert not pane_guard.classify("python -m agent_takkub report build", "shell").allowed
        # Allowed with temp AGENT_TAKKUB_HOME
        assert pane_guard.classify(
            "AGENT_TAKKUB_HOME=/tmp/test python -m agent_takkub report build", "lead"
        ).allowed
        # Allowed for human outside cockpit (role is None)
        assert pane_guard.classify("python -m agent_takkub report build", None).allowed


def _busy_machine_state(tmp_path):
    """Fresh machine-state.json with another project's pane `working`, so the
    #585 busy-machine gate is live for every role including Lead."""
    import json
    import time

    path = tmp_path / "machine-state.json"
    path.write_text(
        json.dumps(
            {
                "ts": time.time(),
                "projects": {"other-proj": [{"role": "frontend", "state": "working"}]},
                "overloaded": False,
            }
        ),
        encoding="utf-8",
    )
    return path


class TestTakkubTextPayloadInert:
    """2026-09-23 review (#649 regression): the quoted free text of
    `takkub assign/done/send/…` must be inert for EVERY rule — a multi-line
    spec/note with a runner, build, browser or git verb at line start is data
    handed to the CLI, not a command the pane runs. The strip used to split on
    `\\n` BEFORE blanking quotes (so only single-line payloads were covered)
    and only the busy-machine gate ever called it (so full_suite/scope_tiny/
    browser/git rules still read the raw note)."""

    MULTI_LINE_ASSIGN = (
        'takkub assign --role backend "step 1: review\nnpm run build\nstep 3: report"'
    )

    def test_strip_blanks_multi_line_payload(self) -> None:
        assert (
            pane_guard.strip_takkub_text_payload(self.MULTI_LINE_ASSIGN)
            == 'takkub assign --role backend ""'
        )
        assert pane_guard._is_heavy_build_or_suite(self.MULTI_LINE_ASSIGN) == (False, "")

    def test_strip_handles_escaped_quote_and_crlf(self) -> None:
        note = 'takkub done "he said \\"pytest\\"\r\npytest\r\nok"'
        assert pane_guard.strip_takkub_text_payload(note) == 'takkub done ""'

    def test_strip_keeps_double_quoted_command_substitution_visible(self) -> None:
        # `$(…)`/backticks inside double quotes are executed by the shell, so
        # that text is a command, not prose — never blanked (default-deny).
        assert (
            pane_guard.strip_takkub_text_payload('takkub done "$(pytest)"')
            == 'takkub done "$(pytest)"'
        )
        assert pane_guard.strip_takkub_text_payload('takkub done "a\n`git push`\nb"') == (
            'takkub done "a\n`git push`\nb"'
        )
        # single quotes are literal in every shell the panes use
        assert pane_guard.strip_takkub_text_payload("takkub done '$(pytest)'") == 'takkub done ""'

    def test_lead_multi_line_assign_allowed_while_machine_busy(self, tmp_path) -> None:
        ms = _busy_machine_state(tmp_path)
        verdict = pane_guard.classify(self.MULTI_LINE_ASSIGN, "lead", machine_state_path=ms)
        assert verdict.allowed, verdict.rule
        # the same gate still fires for the real thing
        assert pane_guard.classify("npm run build", "lead", machine_state_path=ms).rule == (
            "busy_machine:pm_build"
        )

    @pytest.mark.parametrize(
        ("command", "role", "kwargs"),
        [
            ('takkub done "ran:\npytest\nall green"', "backend", {}),
            ('takkub done "ran:\nvitest run\njest\nall green"', "frontend", {}),
            ('takkub done "did:\ngit push origin feat\nok"', "backend", {}),
            ("takkub send lead 'note:\ngit commit -m x\ngit merge main\nok'", "backend", {}),
            ('takkub done "ran:\nplaywright codegen\nnpx playwright install\nok"', "backend", {}),
            ('takkub done "ran:\npytest\nnpm test\nok"', "backend", {"scope": "tiny"}),
            ('takkub progress "next:\ntakkub qa-gate --auto\n"', "backend", {"scope": "tiny"}),
            ('takkub done "ran:\npython -m agent_takkub report build\nok"', "backend", {}),
            ('takkub issue "repro:\npkill -f node\nfind / -name x\n"', "backend", {}),
            ("bash -c 'takkub done \"ran:\npytest\nok\"'", "backend", {}),
            ('rtk takkub done "ran:\npytest\nok"', "backend", {}),
        ],
    )
    def test_multi_line_note_allowed_for_every_rule(self, command: str, role: str, kwargs) -> None:
        verdict = pane_guard.classify(command, role, **kwargs)
        assert verdict.allowed, (command, verdict.rule)

    def test_multi_line_done_allowed_while_machine_busy(self, tmp_path) -> None:
        ms = _busy_machine_state(tmp_path)
        verdict = pane_guard.classify(
            'takkub done "ran:\npytest\nall green"', "backend", machine_state_path=ms
        )
        assert verdict.allowed, verdict.rule

    @pytest.mark.parametrize(
        ("command", "role", "kwargs", "rule"),
        [
            ('takkub done "x"; pytest', "backend", {}, "full_suite:pytest"),
            ('pytest\ntakkub done "x"', "backend", {}, "full_suite:pytest"),
            ('takkub done "x"\ngit push origin main', "backend", {}, "git_lead_only:push"),
            ('echo "foo\ntakkub done bar" && pytest', "backend", {}, "full_suite:pytest"),
            ('takkub done "x"; takkub qa-gate', "backend", {"scope": "tiny"}, "scope_tiny:qa_gate"),
            ('takkub done "x" && playwright codegen', "backend", {}, "browser_driver:bare-invoke"),
            # unterminated quote: nothing to blank, the runner line is real
            ('takkub done "ran:\npytest', "backend", {}, "full_suite:pytest"),
        ],
    )
    def test_real_commands_next_to_a_payload_stay_denied(
        self, command: str, role: str, kwargs, rule: str
    ) -> None:
        verdict = pane_guard.classify(command, role, **kwargs)
        assert not verdict.allowed, command
        assert verdict.rule == rule

    def test_real_heavy_command_next_to_a_payload_stays_denied_when_busy(self, tmp_path) -> None:
        ms = _busy_machine_state(tmp_path)
        for command in ['takkub done "x" && npm run build', 'bash -c "npm run build"']:
            verdict = pane_guard.classify(command, "lead", machine_state_path=ms)
            assert verdict.rule == "busy_machine:pm_build", command
