"""Tests for ensure_user_mcps() merge/prune logic.

Covers:
- A user MCP previously merged from ~/.claude.json is removed once it is no
  longer eligible there (recorded in `user-mcps-merged.json`)
- Browser MCPs are never pruned (managed by ensure_browser_mcps)
- Entries currently in policy are preserved
- Credential-bearing user MCPs (bearer header / inline DSN creds) are skipped
- Servers installed through add_mcp_server (`takkub mcp add`, Tools dialog,
  design integrations) are never pruned by the ~/.claude.json merge
  (2026-09-23 review: one eligible ~/.claude.json entry used to wipe them all)
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
# A credential-free stdio DB MCP — not a secret, so it merges.
_CLEAN_DB_CFG = {"type": "stdio", "command": "npx", "args": ["-y", "some-db-mcp"]}
# DSN with inline credentials passed as an arg. Credentials here are synthetic
# placeholders — never commit real secrets.
_DSN_SECRET_CFG = {
    "type": "stdio",
    "command": "npx",
    "args": ["-y", "some-db-mcp", "postgresql://dbuser:REDACTED@localhost:5432/exampledb"],
}
# An HTTP MCP carrying a bearer token in its Authorization header (a secret).
_HTTP_SECRET_CFG = {
    "type": "http",
    "url": "http://localhost:3001/mcp",
    "headers": {"Authorization": "Bearer secret"},
}
_STALE_CFG = {"type": "stdio", "command": "npx", "args": ["-y", "old-mcp"]}
# What `takkub mcp add context7 npx --args "-y @upstash/context7-mcp"` writes.
_CONTEXT7_CFG = {"type": "stdio", "command": "npx", "args": ["-y", "@upstash/context7-mcp"]}
# A credential-free entry the user registered with `claude mcp add -s user`.
_FILESYSTEM_CFG = {"type": "stdio", "command": "npx", "args": ["-y", "fs-mcp"]}


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


def _merged_record(mcp_file: pathlib.Path) -> pathlib.Path:
    return mcp_file.parent / sdt._USER_MCP_MERGED_FILENAME


def _seed_merged(mcp_file: pathlib.Path, *names: str) -> None:
    """Pretend an earlier ensure_user_mcps() run merged *names* from ~/.claude.json."""
    _merged_record(mcp_file).write_text(json.dumps({"names": list(names)}), encoding="utf-8")


def _read_merged(mcp_file: pathlib.Path) -> set[str]:
    return set(json.loads(_merged_record(mcp_file).read_text(encoding="utf-8"))["names"])


class TestPruneStaleEntries:
    def test_prune_removes_stale_non_browser_mcp(
        self, isolated, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp_file, claude_json = isolated
        # shared-mcp.json has a stale entry "old-mcp" that an earlier merge
        # brought in from ~/.claude.json and is no longer in policy
        mcp_file.write_text(
            json.dumps({"mcpServers": {"old-mcp": _STALE_CFG, **BROWSER_MCPS}}),
            encoding="utf-8",
        )
        _seed_merged(mcp_file, "old-mcp")
        # ~/.claude.json has only obsidian-vault (no old-mcp)
        _write_claude_json(claude_json, {"obsidian-vault": _OBSIDIAN_CFG})

        ok, msg = ensure_user_mcps()
        assert ok, msg
        data = _read_mcp(mcp_file)
        assert "old-mcp" not in data["mcpServers"]
        assert "pruned" in msg
        assert "old-mcp" in msg
        # The record now mirrors what is actually merged.
        assert _read_merged(mcp_file) == {"obsidian-vault"}

    def test_prune_preserves_browser_mcps(self, isolated, monkeypatch: pytest.MonkeyPatch) -> None:
        mcp_file, claude_json = isolated
        # shared-mcp.json has browser MCPs and a stale user MCP
        mcp_file.write_text(
            json.dumps({"mcpServers": {"stale-thing": _STALE_CFG, **BROWSER_MCPS}}),
            encoding="utf-8",
        )
        # Even a record that (wrongly) names a browser MCP must not get it pruned.
        _seed_merged(mcp_file, "stale-thing", *_BROWSER_MCP_NAMES)
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
        _seed_merged(mcp_file, "obsidian-vault", "stale-old")
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
        # ...but the provenance record still adopts the entry (first boot
        # after upgrade), so a later ~/.claude.json removal can prune it.
        assert _read_merged(mcp_file) == {"obsidian-vault"}


class TestUserInstalledMcpsSurviveMerge:
    """2026-09-23 review (shared_dev_tools.py:1325): the prune loop deleted
    every non-managed master entry absent from ~/.claude.json — but
    `takkub mcp add` / the Tools dialog / design integrations write ONLY the
    master, so the first credential-free ~/.claude.json entry wiped them all
    on the next boot. Only entries the merge itself brought in are prunable."""

    def test_add_mcp_server_entry_survives_claude_json_merge(self, isolated) -> None:
        # The verifiers' repro: `takkub mcp add context7 ...` then a clean
        # user-scope `claude mcp add`, then the next cockpit boot.
        mcp_file, claude_json = isolated
        assert sdt.add_mcp_server("context7", _CONTEXT7_CFG)
        assert set(_read_mcp(mcp_file)["mcpServers"]) == {"context7"}
        _write_claude_json(claude_json, {"filesystem": _FILESYSTEM_CFG})

        ok, msg = ensure_user_mcps()
        assert ok, msg
        servers = _read_mcp(mcp_file)["mcpServers"]
        assert servers["context7"] == _CONTEXT7_CFG
        assert servers["filesystem"] == _FILESYSTEM_CFG
        assert "pruned" not in msg
        assert "context7" not in msg
        # Only the ~/.claude.json-sourced name is on record as prunable.
        assert _read_merged(mcp_file) == {"filesystem"}

    def test_add_mcp_server_entry_survives_repeated_boots(self, isolated) -> None:
        # Once wasn't the bug's whole shape — it re-fired on every boot after
        # each re-add. Three boots, the user server is still there each time.
        mcp_file, claude_json = isolated
        assert sdt.add_mcp_server("context7", _CONTEXT7_CFG)
        _write_claude_json(claude_json, {"filesystem": _FILESYSTEM_CFG})

        for _ in range(3):
            ok, msg = ensure_user_mcps()
            assert ok, msg
            assert "context7" in _read_mcp(mcp_file)["mcpServers"]

    def test_entry_merged_from_claude_json_is_pruned_after_removal(self, isolated) -> None:
        # End-to-end without seeding: the record the FIRST run writes is what
        # licenses the SECOND run's prune of a genuinely stale entry.
        mcp_file, claude_json = isolated
        assert sdt.add_mcp_server("context7", _CONTEXT7_CFG)
        _write_claude_json(claude_json, {"filesystem": _FILESYSTEM_CFG, "demo": _CLEAN_DB_CFG})
        ok, msg = ensure_user_mcps()
        assert ok, msg
        assert set(_read_mcp(mcp_file)["mcpServers"]) == {"context7", "filesystem", "demo"}
        assert _read_merged(mcp_file) == {"filesystem", "demo"}

        _write_claude_json(claude_json, {"filesystem": _FILESYSTEM_CFG})  # user dropped demo
        ok, msg = ensure_user_mcps()
        assert ok, msg
        assert set(_read_mcp(mcp_file)["mcpServers"]) == {"context7", "filesystem"}
        assert "pruned: demo" in msg
        assert _read_merged(mcp_file) == {"filesystem"}

    def test_add_mcp_server_takes_ownership_of_previously_merged_name(self, isolated) -> None:
        # demo was merged (recorded), then dropped from ~/.claude.json while
        # nothing else was eligible (early return → record still names it),
        # then the user deliberately re-installed it with `takkub mcp add`.
        # A later eligible ~/.claude.json entry must NOT prune it.
        mcp_file, claude_json = isolated
        _write_claude_json(claude_json, {"demo": _CLEAN_DB_CFG})
        ok, msg = ensure_user_mcps()
        assert ok, msg
        assert _read_merged(mcp_file) == {"demo"}

        _write_claude_json(claude_json, {})
        ok, msg = ensure_user_mcps()
        assert ok, msg
        assert "demo" in _read_mcp(mcp_file)["mcpServers"]  # early return, untouched

        own_cfg = {"type": "stdio", "command": "npx", "args": ["-y", "demo-mcp", "--mine"]}
        assert sdt.add_mcp_server("demo", own_cfg)
        assert _read_merged(mcp_file) == set()

        _write_claude_json(claude_json, {"filesystem": _FILESYSTEM_CFG})
        ok, msg = ensure_user_mcps()
        assert ok, msg
        servers = _read_mcp(mcp_file)["mcpServers"]
        assert servers["demo"] == own_cfg
        assert "filesystem" in servers
        assert "pruned" not in msg

    def test_remove_mcp_server_drops_record(self, isolated) -> None:
        mcp_file, claude_json = isolated
        _write_claude_json(claude_json, {"demo": _CLEAN_DB_CFG, "filesystem": _FILESYSTEM_CFG})
        ok, msg = ensure_user_mcps()
        assert ok, msg
        assert _read_merged(mcp_file) == {"demo", "filesystem"}

        assert sdt.remove_mcp_server("demo")
        assert _read_merged(mcp_file) == {"filesystem"}

    def test_upgrade_without_record_prunes_nothing(self, isolated) -> None:
        # An install that merged before the record existed: nothing in the
        # master can be told apart from a user-installed server, so nothing
        # is pruned; the eligible names are adopted into the record.
        mcp_file, claude_json = isolated
        mcp_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "context7": _CONTEXT7_CFG,
                        "old-mcp": _STALE_CFG,
                        "filesystem": _FILESYSTEM_CFG,
                        **BROWSER_MCPS,
                    }
                }
            ),
            encoding="utf-8",
        )
        assert not _merged_record(mcp_file).exists()
        _write_claude_json(claude_json, {"filesystem": _FILESYSTEM_CFG})

        ok, msg = ensure_user_mcps()
        assert ok, msg
        servers = _read_mcp(mcp_file)["mcpServers"]
        assert {"context7", "old-mcp", "filesystem", *_BROWSER_MCP_NAMES} <= set(servers)
        assert "pruned" not in msg
        assert _read_merged(mcp_file) == {"filesystem"}

    def test_corrupt_record_prunes_nothing(self, isolated) -> None:
        mcp_file, claude_json = isolated
        mcp_file.write_text(
            json.dumps({"mcpServers": {"context7": _CONTEXT7_CFG, "old-mcp": _STALE_CFG}}),
            encoding="utf-8",
        )
        _merged_record(mcp_file).write_text("{not json", encoding="utf-8")
        _write_claude_json(claude_json, {"filesystem": _FILESYSTEM_CFG})

        ok, msg = ensure_user_mcps()
        assert ok, msg
        servers = _read_mcp(mcp_file)["mcpServers"]
        assert {"context7", "old-mcp", "filesystem"} == set(servers)
        assert "pruned" not in msg
        # The record is repaired from this run's merge.
        assert _read_merged(mcp_file) == {"filesystem"}

    def test_record_holds_names_only(self, isolated) -> None:
        # The record sits beside the shared runtime file; it must never carry
        # a cfg value (args/env/headers may hold credentials).
        mcp_file, claude_json = isolated
        _write_claude_json(claude_json, {"demo": _CLEAN_DB_CFG})
        ok, msg = ensure_user_mcps()
        assert ok, msg
        raw = _merged_record(mcp_file).read_text(encoding="utf-8")
        assert json.loads(raw) == {"names": ["demo"]}
        assert "some-db-mcp" not in raw


class TestCredentialBearingHttpSkipped:
    def test_http_bearer_entry_is_pruned(self, isolated, monkeypatch: pytest.MonkeyPatch) -> None:
        mcp_file, claude_json = isolated
        # A stale HTTP+bearer entry an earlier merge brought in sits in
        # shared-mcp.json; it must be pruned because its Authorization header
        # is a credential.
        mcp_file.write_text(
            json.dumps(
                {"mcpServers": {"internal-http": _HTTP_SECRET_CFG, "obsidian-vault": _OBSIDIAN_CFG}}
            ),
            encoding="utf-8",
        )
        _seed_merged(mcp_file, "internal-http", "obsidian-vault")
        _write_claude_json(
            claude_json, {"internal-http": _HTTP_SECRET_CFG, "obsidian-vault": _OBSIDIAN_CFG}
        )

        ok, msg = ensure_user_mcps()
        assert ok, msg
        data = _read_mcp(mcp_file)
        assert "internal-http" not in data["mcpServers"]
        assert "obsidian-vault" in data["mcpServers"]
        assert "pruned" in msg

    def test_forced_user_entry_with_credential_is_kept(
        self, isolated, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The user opted in via `takkub mcp add --force` / the Tools dialog
        # (force=True); a same-named credential-bearing ~/.claude.json entry
        # is skipped from the merge, but that is no licence to delete theirs.
        mcp_file, claude_json = isolated
        assert sdt.add_mcp_server("internal-http", _HTTP_SECRET_CFG, force=True)
        _write_claude_json(
            claude_json, {"internal-http": _HTTP_SECRET_CFG, "obsidian-vault": _OBSIDIAN_CFG}
        )

        ok, msg = ensure_user_mcps()
        assert ok, msg
        data = _read_mcp(mcp_file)
        assert data["mcpServers"]["internal-http"] == _HTTP_SECRET_CFG
        assert "obsidian-vault" in data["mcpServers"]
        assert "pruned" not in msg

    def test_pruned_message_does_not_contain_bearer_value(
        self, isolated, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp_file, claude_json = isolated
        mcp_file.write_text(
            json.dumps({"mcpServers": {"internal-http": _HTTP_SECRET_CFG}}),
            encoding="utf-8",
        )
        _seed_merged(mcp_file, "internal-http")
        _write_claude_json(claude_json, {"internal-http": _HTTP_SECRET_CFG})

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


class TestCredentialBearingDsnSkipped:
    """A user MCP whose args carry an inline DSN credential must be skipped by
    the general credential check — never merged into the shared runtime file.
    The default allowlist is empty, so nothing is trusted by default.
    """

    def test_default_allowlist_is_empty(self) -> None:
        # 2026-07-02: allowlist emptied entirely (obsidian-vault's provider
        # plugin was uninstalled); nothing is trusted by default now.
        assert sdt._USER_MCP_DEFAULT_ALLOW == frozenset()

    def test_dsn_credential_entry_is_skipped(
        self, isolated, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp_file, claude_json = isolated
        mcp_file.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        # User has a credential-bearing DB MCP + a clean obsidian-vault.
        _write_claude_json(
            claude_json,
            {"db-mcp": _DSN_SECRET_CFG, "obsidian-vault": _OBSIDIAN_CFG},
        )

        ok, msg = ensure_user_mcps()
        assert ok, msg
        data = _read_mcp(mcp_file)
        # Credential-bearing entry must NOT be merged.
        assert "db-mcp" not in data["mcpServers"]
        # Clean obsidian-vault still merges.
        assert "obsidian-vault" in data["mcpServers"]
        # The DSN password must never leak into the return message.
        assert "REDACTED" not in msg

    def test_clean_db_mcp_still_merges(self, isolated, monkeypatch: pytest.MonkeyPatch) -> None:
        # A credential-free DB MCP (no inline DSN secret) is not a secret, so
        # it falls through and merges even without allowlisting.
        mcp_file, claude_json = isolated
        mcp_file.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        _write_claude_json(claude_json, {"db-mcp": _CLEAN_DB_CFG})

        ok, msg = ensure_user_mcps()
        assert ok, msg
        data = _read_mcp(mcp_file)
        assert "db-mcp" in data["mcpServers"]


class TestAllowlistedSecretWarns:
    """An allowlisted entry that carries a credential is still merged
    (allowlist wins) but emits a warning so the operator can rotate it."""

    def test_allowlisted_with_secret_merges_and_warns(
        self, isolated, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        mcp_file, claude_json = isolated
        mcp_file.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        # The shipped allowlist is empty now — pin one name so the
        # allowlist-wins-with-warning MECHANISM stays covered regardless of
        # what ships in _USER_MCP_DEFAULT_ALLOW.
        monkeypatch.setattr(sdt, "_USER_MCP_DEFAULT_ALLOW", frozenset({"obsidian-vault"}))
        # give the allowlisted entry a DSN secret.
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
