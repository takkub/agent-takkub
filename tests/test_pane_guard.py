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


class TestFailOpen:
    """The guard must never be able to wedge a pane or police a human."""

    @pytest.mark.parametrize("role", ["lead", "shell"])
    def test_user_driven_panes_never_guarded(self, role: str) -> None:
        assert pane_guard.classify("npx --yes playwright", role).allowed
        assert pane_guard.classify("find / -name x", role).allowed

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
