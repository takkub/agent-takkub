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
    BROWSER_MCPS,
    ensure_browser_mcps,
    shared_mcp_config_path,
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
