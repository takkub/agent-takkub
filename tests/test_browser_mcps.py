"""Tests for `ensure_browser_mcps` and `shared_mcp_config_path`.

The cockpit ships playwright + chrome-devtools into every pane via
`runtime/shared-mcp.json`. Two startup states have to behave correctly:
  - file missing entirely (fresh install)
  - file already has the browser MCPs (re-launch)

A corrupt JSON file must not be clobbered — that's almost always a
hand-edit gone wrong and the user wants the diff to look at, not a
silent overwrite.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from agent_takkub import shared_dev_tools as sdt
from agent_takkub.shared_dev_tools import (
    _ROLE_MCP_POLICY,
    BROWSER_MCPS,
    _role_variant_path,
    browser_profile_mcp_config_path,
    ensure_browser_mcps,
    shared_mcp_config_path,
    shared_mcp_config_path_for_role,
)

# Each browser MCP's documented user-data-dir flag (playwright kebab,
# chrome-devtools camelCase).
_PROFILE_FLAGS = {"playwright": "--user-data-dir", "chrome-devtools": "--userDataDir"}


def _udd(args: list[str], flag: str = "--user-data-dir") -> str:
    """The user-data-dir value following *flag* in an MCP server's args list."""
    return args[args.index(flag) + 1]


@pytest.fixture
def isolated_mcp_file(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    """Redirect SHARED_MCP_FILE to a tmp path so tests don't stomp the
    real cockpit config under `runtime/`."""
    target = tmp_path / "shared-mcp.json"
    monkeypatch.setattr(sdt, "SHARED_MCP_FILE", target)
    return target


def _read(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class TestEnsureBrowserMcps:
    def test_writes_fresh_file_when_missing(self, isolated_mcp_file: pathlib.Path) -> None:
        assert not isolated_mcp_file.exists()
        ok, msg = ensure_browser_mcps()
        assert ok, msg
        data = _read(isolated_mcp_file)
        assert set(data["mcpServers"].keys()) == set(BROWSER_MCPS.keys())

    def test_idempotent_when_browsers_already_present(
        self, isolated_mcp_file: pathlib.Path
    ) -> None:
        ok, _ = ensure_browser_mcps()
        assert ok
        before = isolated_mcp_file.read_text(encoding="utf-8")
        ok, msg = ensure_browser_mcps()
        assert ok
        assert "already present" in msg
        after = isolated_mcp_file.read_text(encoding="utf-8")
        assert before == after

    def test_refuses_to_clobber_corrupt_json(self, isolated_mcp_file: pathlib.Path) -> None:
        # Leave a hand-edited broken file alone — surface the failure
        # so the user can fix it, don't silently overwrite their work.
        isolated_mcp_file.write_text("{not valid json", encoding="utf-8")
        ok, msg = ensure_browser_mcps()
        assert not ok
        assert "leaving as-is" in msg
        # File content untouched
        assert isolated_mcp_file.read_text(encoding="utf-8") == "{not valid json"

    def test_browser_entries_have_required_stdio_fields(
        self, isolated_mcp_file: pathlib.Path
    ) -> None:
        ok, _ = ensure_browser_mcps()
        assert ok
        data = _read(isolated_mcp_file)
        for name, expected in BROWSER_MCPS.items():
            entry = data["mcpServers"][name]
            assert entry["type"] == "stdio"
            assert entry["command"] == expected["command"]
            assert entry["args"] == expected["args"]

    def test_deep_copies_so_subsequent_mutation_is_isolated(
        self, isolated_mcp_file: pathlib.Path
    ) -> None:
        # If `ensure_browser_mcps` accidentally shared references with
        # the BROWSER_MCPS constant, a later edit to the file would
        # mutate the in-memory template too — a footgun for tests that
        # use the constant for comparisons. Confirm the on-disk entry
        # is a separate object.
        ok, _ = ensure_browser_mcps()
        assert ok
        data = _read(isolated_mcp_file)
        data["mcpServers"]["playwright"]["env"]["MARKER"] = "1"
        assert "MARKER" not in BROWSER_MCPS["playwright"]["env"]


class TestSharedMcpConfigPath:
    def test_returns_none_when_file_missing(self, isolated_mcp_file: pathlib.Path) -> None:
        assert shared_mcp_config_path() is None

    def test_returns_path_when_only_browsers_present(self, isolated_mcp_file: pathlib.Path) -> None:
        ok, _ = ensure_browser_mcps()
        assert ok
        assert shared_mcp_config_path() == str(isolated_mcp_file)

    def test_returns_none_when_corrupt(self, isolated_mcp_file: pathlib.Path) -> None:
        isolated_mcp_file.write_text("not json", encoding="utf-8")
        assert shared_mcp_config_path() is None

    def test_returns_none_when_file_has_no_servers(self, isolated_mcp_file: pathlib.Path) -> None:
        isolated_mcp_file.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        assert shared_mcp_config_path() is None


class TestRoleAwareMcpFilter:
    """ensure_browser_mcps writes per-role variants alongside the master.
    shared_mcp_config_path_for_role picks the right one for each role so
    only roles that need browser MCPs pay the (~15k token) schema cost.
    """

    def test_variants_written_after_ensure_browser_mcps(
        self, isolated_mcp_file: pathlib.Path
    ) -> None:
        ok, _ = ensure_browser_mcps()
        assert ok
        for role in _ROLE_MCP_POLICY:
            assert _role_variant_path(role).is_file(), f"missing variant for {role}"

    def test_lead_variant_excludes_browser_mcps(self, isolated_mcp_file: pathlib.Path) -> None:
        ensure_browser_mcps()
        data = _read(_role_variant_path("lead"))
        servers = data["mcpServers"]
        assert "playwright" not in servers
        assert "chrome-devtools" not in servers
        # Lead has no user MCPs in this test (we only ran ensure_browser_mcps),
        # so the variant is empty — that's the expected zero-MCP state for Lead.

    def test_qa_variant_includes_browser_mcps(self, isolated_mcp_file: pathlib.Path) -> None:
        ensure_browser_mcps()
        data = _read(_role_variant_path("qa"))
        servers = data["mcpServers"]
        assert "playwright" in servers
        assert "chrome-devtools" in servers

    def test_critic_variant_includes_browser_mcps(self, isolated_mcp_file: pathlib.Path) -> None:
        ensure_browser_mcps()
        data = _read(_role_variant_path("critic"))
        servers = data["mcpServers"]
        assert "playwright" in servers
        assert "chrome-devtools" in servers

    def test_backend_variant_excludes_browser_mcps(self, isolated_mcp_file: pathlib.Path) -> None:
        ensure_browser_mcps()
        data = _read(_role_variant_path("backend"))
        servers = data["mcpServers"]
        assert "playwright" not in servers
        assert "chrome-devtools" not in servers

    def test_for_role_returns_variant_path_when_policy_exists(
        self, isolated_mcp_file: pathlib.Path
    ) -> None:
        # Seed master with at least one MCP the role allows so the variant
        # has servers (empty mcpServers → returns None by design).
        isolated_mcp_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "playwright": BROWSER_MCPS["playwright"],
                        "chrome-devtools": BROWSER_MCPS["chrome-devtools"],
                    }
                }
            ),
            encoding="utf-8",
        )
        # ensure_browser_mcps writes variants
        ensure_browser_mcps()
        qa_path = shared_mcp_config_path_for_role("qa")
        assert qa_path is not None
        assert qa_path.endswith("shared-mcp-qa.json")

    def test_for_role_returns_none_for_lead_when_only_browser_mcps(
        self, isolated_mcp_file: pathlib.Path
    ) -> None:
        # Lead's policy excludes browser MCPs entirely. If the master only
        # has browser MCPs, Lead's variant has empty mcpServers → function
        # returns None (signals "skip --mcp-config for this pane").
        ensure_browser_mcps()
        assert shared_mcp_config_path_for_role("lead") is None

    def test_for_role_falls_back_to_master_for_unknown_role(
        self, isolated_mcp_file: pathlib.Path
    ) -> None:
        # A role not in _ROLE_MCP_POLICY (e.g. user-added "data-eng") gets
        # the full master file for back-compat.
        ensure_browser_mcps()
        path = shared_mcp_config_path_for_role("data-eng")
        assert path == str(isolated_mcp_file)

    def test_variant_regenerated_on_master_change(self, isolated_mcp_file: pathlib.Path) -> None:
        ensure_browser_mcps()
        qa_before = _read(_role_variant_path("qa"))["mcpServers"]
        # Mutate master to add a new MCP the qa policy doesn't allow
        # (chrome-devtools already allowed; add a stub the policy excludes).
        data = json.loads(isolated_mcp_file.read_text(encoding="utf-8"))
        data["mcpServers"]["unrelated-mcp"] = {
            "type": "stdio",
            "command": "noop",
            "args": [],
        }
        isolated_mcp_file.write_text(json.dumps(data), encoding="utf-8")
        # Re-run ensure to regenerate variants
        ensure_browser_mcps()
        qa_after = _read(_role_variant_path("qa"))["mcpServers"]
        # qa policy doesn't include the new one, so it should NOT appear
        assert "unrelated-mcp" not in qa_after
        # browsers still there
        assert "playwright" in qa_after
        assert "chrome-devtools" in qa_after
        # confirm baseline didn't change semantically
        assert set(qa_before.keys()) == set(qa_after.keys())


class TestBrowserProfileMcpConfigPath:
    """Persistent per-pane browser-profile isolation: every browser role gets its
    own --user-data-dir so the browser remembers its session across runs, and
    parallel fan-out shards don't lock one Chrome profile (#39)."""

    def test_non_shard_gets_persistent_profile_without_shard_suffix(
        self, isolated_mcp_file: pathlib.Path
    ) -> None:
        # A normal (non-fan-out) qa pane — shard_idx=None — must still get a
        # PERSISTENT profile (so it remembers login), with NO "-shard" in the path.
        ensure_browser_mcps()
        path = browser_profile_mcp_config_path("qa", None, "proj_a")
        assert path is not None
        assert path.endswith("shared-mcp-proj_a-qa.json")  # no -shard suffix
        servers = _read(pathlib.Path(path))["mcpServers"]
        for name, flag in _PROFILE_FLAGS.items():
            udd = _udd(servers[name]["args"], flag)
            # Exact dir name — no "-shard<N>" segment for a non-fan-out pane.
            assert pathlib.Path(udd).name == f"proj_a-qa-{name}"

    def test_non_shard_and_shard_get_distinct_profiles(
        self, isolated_mcp_file: pathlib.Path
    ) -> None:
        ensure_browser_mcps()
        plain = _read(pathlib.Path(browser_profile_mcp_config_path("qa", None, "proj_a")))
        sh1 = _read(pathlib.Path(browser_profile_mcp_config_path("qa", 1, "proj_a")))
        assert _udd(plain["mcpServers"]["playwright"]["args"]) != _udd(
            sh1["mcpServers"]["playwright"]["args"]
        )

    def test_injects_unique_user_data_dir_per_browser(
        self, isolated_mcp_file: pathlib.Path
    ) -> None:
        ensure_browser_mcps()
        path = browser_profile_mcp_config_path("qa", 1, "proj_a")
        assert path is not None
        servers = _read(pathlib.Path(path))["mcpServers"]
        for name, flag in _PROFILE_FLAGS.items():
            args = servers[name]["args"]
            # Each browser must use its OWN documented flag (chrome-devtools wants
            # --userDataDir; --user-data-dir would be ignored → shared profile).
            assert args.count(flag) == 1, f"{name} should carry exactly one {flag}"
            udd = _udd(args, flag)
            assert "shard1" in udd
            assert name in udd  # distinct dir per browser binary
        # The two browsers must NOT share a profile dir (they'd lock each other).
        assert _udd(servers["playwright"]["args"], _PROFILE_FLAGS["playwright"]) != _udd(
            servers["chrome-devtools"]["args"], _PROFILE_FLAGS["chrome-devtools"]
        )

    def test_different_shards_get_different_profiles(self, isolated_mcp_file: pathlib.Path) -> None:
        ensure_browser_mcps()
        s1 = _read(pathlib.Path(browser_profile_mcp_config_path("qa", 1, "proj_a")))
        s2 = _read(pathlib.Path(browser_profile_mcp_config_path("qa", 2, "proj_a")))
        u1 = _udd(s1["mcpServers"]["playwright"]["args"])
        u2 = _udd(s2["mcpServers"]["playwright"]["args"])
        assert u1 != u2
        assert "shard1" in u1 and "shard2" in u2

    def test_different_projects_get_different_profiles(
        self, isolated_mcp_file: pathlib.Path
    ) -> None:
        ensure_browser_mcps()
        a = _read(pathlib.Path(browser_profile_mcp_config_path("qa", 1, "proj_a")))
        b = _read(pathlib.Path(browser_profile_mcp_config_path("qa", 1, "proj_b")))
        assert _udd(a["mcpServers"]["playwright"]["args"]) != _udd(
            b["mcpServers"]["playwright"]["args"]
        )

    def test_idempotent_no_double_append(self, isolated_mcp_file: pathlib.Path) -> None:
        ensure_browser_mcps()
        first = pathlib.Path(browser_profile_mcp_config_path("qa", 1, "proj_a")).read_text(
            encoding="utf-8"
        )
        second = pathlib.Path(browser_profile_mcp_config_path("qa", 1, "proj_a")).read_text(
            encoding="utf-8"
        )
        assert first == second  # regenerated from the base variant, stable
        data = json.loads(second)
        for name, flag in _PROFILE_FLAGS.items():
            assert data["mcpServers"][name]["args"].count(flag) == 1

    def test_preserves_non_browser_mcps_untouched(self, isolated_mcp_file: pathlib.Path) -> None:
        ensure_browser_mcps()
        master = _read(isolated_mcp_file)
        master["mcpServers"]["obsidian-vault"] = {"type": "stdio", "command": "noop", "args": ["x"]}
        isolated_mcp_file.write_text(json.dumps(master), encoding="utf-8")
        ensure_browser_mcps()  # regenerate variants so qa picks up obsidian-vault
        servers = _read(pathlib.Path(browser_profile_mcp_config_path("qa", 1, "proj_a")))[
            "mcpServers"
        ]
        assert servers["obsidian-vault"]["args"] == ["x"]
        assert "--user-data-dir" not in servers["obsidian-vault"]["args"]

    def test_non_browser_role_falls_back_to_base_path(
        self, isolated_mcp_file: pathlib.Path
    ) -> None:
        ensure_browser_mcps()
        master = _read(isolated_mcp_file)
        master["mcpServers"]["obsidian-vault"] = {"type": "stdio", "command": "noop", "args": []}
        isolated_mcp_file.write_text(json.dumps(master), encoding="utf-8")
        ensure_browser_mcps()
        # backend's policy has no browser MCP → nothing to isolate → base path.
        base = shared_mcp_config_path_for_role("backend")
        assert browser_profile_mcp_config_path("backend", 1, "proj_a") == base

    def test_clears_stale_singleton_lock_on_regenerate(
        self, isolated_mcp_file: pathlib.Path
    ) -> None:
        # A hard-killed shard leaves Chromium's SingletonLock behind; on the same
        # shard's next run that stale lock would wedge the browser. Regenerating
        # the shard config must best-effort clear it.
        ensure_browser_mcps()
        browser_profile_mcp_config_path("qa", 1, "proj_a")
        prof = isolated_mcp_file.parent / "browser-profiles" / "proj_a-qa-shard1-playwright"
        assert prof.is_dir()
        lock = prof / "SingletonLock"
        lock.write_text("stale", encoding="utf-8")
        browser_profile_mcp_config_path("qa", 1, "proj_a")  # regenerate
        assert not lock.exists()
