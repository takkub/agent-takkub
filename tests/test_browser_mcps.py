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
    ensure_browser_mcps,
    shared_mcp_config_path,
    shared_mcp_config_path_for_role,
)


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
