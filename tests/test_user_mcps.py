"""Tests for ensure_user_mcps() pruning logic.

Covers:
- Stale non-browser user MCP is removed when no longer in policy
- Browser MCPs are never pruned (managed by ensure_browser_mcps)
- Entries currently in policy are preserved
- pms stays with TAKKUB_INCLUDE_PMS=1, gets pruned without it
"""

from __future__ import annotations

import json
import pathlib

import pytest

from agent_takkub import shared_dev_tools as sdt
from agent_takkub.shared_dev_tools import (
    _BROWSER_MCP_NAMES,
    BROWSER_MCPS,
    _has_secrets,
    ensure_user_mcps,
)

_OBSIDIAN_CFG = {"type": "stdio", "command": "npx", "args": ["-y", "obsidian-vault-mcp"]}
_POSTGRES_CFG = {"type": "stdio", "command": "npx", "args": ["-y", "postgres-pms-mcp"]}
# Real-world postgres-pms shape: DSN with inline credentials passed as an arg.
# Credentials here are synthetic placeholders — never commit real secrets.
_POSTGRES_DSN_CFG = {
    "type": "stdio",
    "command": "npx",
    "args": ["-y", "pg-mcp", "postgresql://dbuser:REDACTED@localhost:5432/exampledb"],
}
_PMS_CFG = {
    "type": "http",
    "url": "http://localhost:3001/mcp",
    "headers": {"Authorization": "Bearer secret"},
}
_STALE_CFG = {"type": "stdio", "command": "npx", "args": ["-y", "old-mcp"]}


@pytest.fixture()
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
    """Redirect SHARED_MCP_FILE and ~/.claude.json to tmp paths."""
    mcp_file = tmp_path / "shared-mcp.json"
    claude_json = tmp_path / ".claude.json"

    monkeypatch.setattr(sdt, "SHARED_MCP_FILE", mcp_file)

    # Patch pathlib.Path.home() so ~/.claude.json resolves to our tmp file
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: tmp_path))

    return mcp_file, claude_json


def _write_claude_json(claude_json: pathlib.Path, servers: dict) -> None:
    claude_json.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def _read_mcp(mcp_file: pathlib.Path) -> dict:
    return json.loads(mcp_file.read_text(encoding="utf-8"))


class TestPruneStaleEntries:
    def test_prune_removes_stale_non_browser_mcp(
        self, isolated, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp_file, claude_json = isolated
        # shared-mcp.json has a stale entry "old-mcp" that is no longer in policy
        mcp_file.write_text(
            json.dumps({"mcpServers": {"old-mcp": _STALE_CFG, **BROWSER_MCPS}}),
            encoding="utf-8",
        )
        # ~/.claude.json has only obsidian-vault (no old-mcp)
        _write_claude_json(claude_json, {"obsidian-vault": _OBSIDIAN_CFG})

        ok, msg = ensure_user_mcps()
        assert ok, msg
        data = _read_mcp(mcp_file)
        assert "old-mcp" not in data["mcpServers"]
        assert "pruned" in msg
        assert "old-mcp" in msg

    def test_prune_preserves_browser_mcps(self, isolated, monkeypatch: pytest.MonkeyPatch) -> None:
        mcp_file, claude_json = isolated
        # shared-mcp.json has browser MCPs and a stale user MCP
        mcp_file.write_text(
            json.dumps({"mcpServers": {"stale-thing": _STALE_CFG, **BROWSER_MCPS}}),
            encoding="utf-8",
        )
        _write_claude_json(claude_json, {"obsidian-vault": _OBSIDIAN_CFG})

        ok, msg = ensure_user_mcps()
        assert ok, msg
        data = _read_mcp(mcp_file)
        # All browser MCPs must survive
        for browser_name in _BROWSER_MCP_NAMES:
            assert browser_name in data["mcpServers"], f"{browser_name} was pruned unexpectedly"
        assert "stale-thing" not in data["mcpServers"]

    def test_prune_preserves_entries_in_current_policy(
        self, isolated, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp_file, claude_json = isolated
        # obsidian-vault is already present; stale-old is not in policy
        mcp_file.write_text(
            json.dumps({"mcpServers": {"obsidian-vault": _OBSIDIAN_CFG, "stale-old": _STALE_CFG}}),
            encoding="utf-8",
        )
        _write_claude_json(claude_json, {"obsidian-vault": _OBSIDIAN_CFG})

        ok, msg = ensure_user_mcps()
        assert ok, msg
        data = _read_mcp(mcp_file)
        assert "obsidian-vault" in data["mcpServers"]
        assert "stale-old" not in data["mcpServers"]

    def test_no_write_when_already_up_to_date(
        self, isolated, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp_file, claude_json = isolated
        # Exact match — nothing to change or prune
        mcp_file.write_text(
            json.dumps({"mcpServers": {"obsidian-vault": _OBSIDIAN_CFG}}),
            encoding="utf-8",
        )
        _write_claude_json(claude_json, {"obsidian-vault": _OBSIDIAN_CFG})
        before = mcp_file.read_text(encoding="utf-8")

        ok, msg = ensure_user_mcps()
        assert ok, msg
        assert "already up-to-date" in msg
        assert mcp_file.read_text(encoding="utf-8") == before


class TestPmsOptIn:
    def test_pms_stays_when_env_set(self, isolated, monkeypatch: pytest.MonkeyPatch) -> None:
        mcp_file, claude_json = isolated
        monkeypatch.setenv("TAKKUB_INCLUDE_PMS", "1")
        # pms currently in shared-mcp.json, user has it in ~/.claude.json
        mcp_file.write_text(
            json.dumps({"mcpServers": {"pms": _PMS_CFG}}),
            encoding="utf-8",
        )
        _write_claude_json(claude_json, {"pms": _PMS_CFG, "obsidian-vault": _OBSIDIAN_CFG})

        ok, msg = ensure_user_mcps()
        assert ok, msg
        data = _read_mcp(mcp_file)
        assert "pms" in data["mcpServers"]

    def test_pms_pruned_when_env_not_set(self, isolated, monkeypatch: pytest.MonkeyPatch) -> None:
        mcp_file, claude_json = isolated
        monkeypatch.delenv("TAKKUB_INCLUDE_PMS", raising=False)
        # pms is stale in shared-mcp.json (from a previous TAKKUB_INCLUDE_PMS=1 run)
        mcp_file.write_text(
            json.dumps({"mcpServers": {"pms": _PMS_CFG, "obsidian-vault": _OBSIDIAN_CFG}}),
            encoding="utf-8",
        )
        _write_claude_json(claude_json, {"pms": _PMS_CFG, "obsidian-vault": _OBSIDIAN_CFG})

        ok, msg = ensure_user_mcps()
        assert ok, msg
        data = _read_mcp(mcp_file)
        assert "pms" not in data["mcpServers"]
        assert "obsidian-vault" in data["mcpServers"]
        assert "pruned" in msg
        assert "pms" in msg

    def test_pms_pruned_message_does_not_contain_bearer_value(
        self, isolated, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp_file, claude_json = isolated
        monkeypatch.delenv("TAKKUB_INCLUDE_PMS", raising=False)
        mcp_file.write_text(
            json.dumps({"mcpServers": {"pms": _PMS_CFG}}),
            encoding="utf-8",
        )
        _write_claude_json(claude_json, {"pms": _PMS_CFG})

        ok, msg = ensure_user_mcps()
        assert ok, msg
        # The bearer token value must never appear in the return message
        assert "secret" not in msg
        assert "Bearer" not in msg


class TestHasSecrets:
    def test_dsn_with_credentials_detected(self) -> None:
        cfg = {"args": ["postgresql://u:p@host/db"]}
        assert _has_secrets(cfg) is True

    def test_dsn_without_credentials_clean(self) -> None:
        cfg = {"args": ["postgresql://host/db"]}
        assert _has_secrets(cfg) is False

    def test_normal_path_arg_clean(self) -> None:
        cfg = {"args": ["/normal/path/file.js"]}
        assert _has_secrets(cfg) is False

    def test_authorization_header_detected_regression(self) -> None:
        cfg = {"headers": {"Authorization": "Bearer x"}}
        assert _has_secrets(cfg) is True

    def test_env_api_key_detected_regression(self) -> None:
        cfg = {"env": {"API_KEY": "x"}}
        assert _has_secrets(cfg) is True


class TestPostgresPmsRemovedFromAllowlist:
    """postgres-pms is no longer in _USER_MCP_DEFAULT_ALLOW (removed 2026-05-29).

    Its real config carries DSN credentials in args, so it must now be
    skipped by the general credential check — never merged into the
    shared runtime file.
    """

    def test_postgres_pms_not_in_default_allow(self) -> None:
        assert "postgres-pms" not in sdt._USER_MCP_DEFAULT_ALLOW
        assert "obsidian-vault" in sdt._USER_MCP_DEFAULT_ALLOW

    def test_postgres_pms_with_dsn_creds_is_skipped(
        self, isolated, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp_file, claude_json = isolated
        mcp_file.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        # User has the real credential-bearing postgres-pms + a clean obsidian-vault.
        _write_claude_json(
            claude_json,
            {"postgres-pms": _POSTGRES_DSN_CFG, "obsidian-vault": _OBSIDIAN_CFG},
        )

        ok, msg = ensure_user_mcps()
        assert ok, msg
        data = _read_mcp(mcp_file)
        # Credential-bearing postgres-pms must NOT be merged.
        assert "postgres-pms" not in data["mcpServers"]
        # Clean obsidian-vault still merges.
        assert "obsidian-vault" in data["mcpServers"]
        # The DSN password must never leak into the return message.
        assert "REDACTED" not in msg

    def test_clean_postgres_pms_still_merges(
        self, isolated, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A credential-free postgres-pms (no inline DSN secret) is not a
        # secret, so it falls through and merges even without allowlisting.
        mcp_file, claude_json = isolated
        mcp_file.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        _write_claude_json(claude_json, {"postgres-pms": _POSTGRES_CFG})

        ok, msg = ensure_user_mcps()
        assert ok, msg
        data = _read_mcp(mcp_file)
        assert "postgres-pms" in data["mcpServers"]


class TestAllowlistedSecretWarns:
    """An allowlisted entry that carries a credential is still merged
    (allowlist wins) but emits a warning so the operator can rotate it."""

    def test_allowlisted_with_secret_merges_and_warns(
        self, isolated, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        mcp_file, claude_json = isolated
        mcp_file.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        # obsidian-vault is the lone allowlisted name; give it a DSN secret.
        secretful = {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "obsidian-vault-mcp", "postgresql://u:p@localhost/db"],
        }
        _write_claude_json(claude_json, {"obsidian-vault": secretful})

        with caplog.at_level("WARNING"):
            ok, msg = ensure_user_mcps()
        assert ok, msg
        data = _read_mcp(mcp_file)
        # Allowlist wins — it still merges...
        assert "obsidian-vault" in data["mcpServers"]
        # ...but a rotation warning was emitted.
        assert any("allowlisted but carries a credential" in r.message for r in caplog.records)
