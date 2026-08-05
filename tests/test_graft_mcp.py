"""Tests for the graft code-intelligence MCP wiring (2026-08-05 pilot).

Covers:
- `ensure_graft_mcp()` idempotency, correct entry, and non-interference
  with browser MCP entries (mirrors test_browser_mcps.py's coverage of
  `ensure_browser_mcps()`).
- `MANAGED_MCP_NAMES` protects graft the same way `_BROWSER_MCP_NAMES`
  protects playwright/chrome-devtools: `add_mcp_server`/`remove_mcp_server`
  refuse to touch it, and a same-named user MCP loses on merge.
- `_ROLE_MCP_POLICY` grants graft to exactly the intended roles.
- Regression for the codex deny-all leak (mcp_bridge.py): a role with a
  non-empty policy (graft) must still suppress MCPs codex would otherwise
  inherit from its own config.toml — see test_mcp_bridge.py for the
  argv-level version of this same regression.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from agent_takkub import shared_dev_tools as sdt
from agent_takkub.shared_dev_tools import (
    _BROWSER_MCP_NAMES,
    _ROLE_MCP_POLICY,
    BROWSER_MCPS,
    GRAFT_MCP,
    MANAGED_MCP_NAMES,
    add_mcp_server,
    ensure_browser_mcps,
    ensure_graft_mcp,
    remove_mcp_server,
)


@pytest.fixture
def isolated_mcp_file(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    from agent_takkub import pane_tools_policy as ptp

    target = tmp_path / "shared-mcp.json"
    monkeypatch.setattr(sdt, "SHARED_MCP_FILE", target)
    monkeypatch.setattr(ptp, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")
    return target


def _read(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class TestEnsureGraftMcp:
    def test_writes_fresh_file_when_missing(self, isolated_mcp_file: pathlib.Path) -> None:
        assert not isolated_mcp_file.exists()
        ok, msg = ensure_graft_mcp()
        assert ok, msg
        data = _read(isolated_mcp_file)
        assert data["mcpServers"]["graft"] == GRAFT_MCP["graft"]

    def test_idempotent_when_graft_already_present(self, isolated_mcp_file: pathlib.Path) -> None:
        ok, _ = ensure_graft_mcp()
        assert ok
        before = isolated_mcp_file.read_text(encoding="utf-8")
        ok, msg = ensure_graft_mcp()
        assert ok
        assert "already present" in msg
        assert isolated_mcp_file.read_text(encoding="utf-8") == before

    def test_refuses_to_clobber_corrupt_json(self, isolated_mcp_file: pathlib.Path) -> None:
        isolated_mcp_file.write_text("{not valid json", encoding="utf-8")
        ok, msg = ensure_graft_mcp()
        assert not ok
        assert "leaving as-is" in msg
        assert isolated_mcp_file.read_text(encoding="utf-8") == "{not valid json"

    def test_does_not_disturb_existing_browser_mcp_entries(
        self, isolated_mcp_file: pathlib.Path
    ) -> None:
        ok, _ = ensure_browser_mcps()
        assert ok
        before = _read(isolated_mcp_file)["mcpServers"]
        ok, _ = ensure_graft_mcp()
        assert ok
        after = _read(isolated_mcp_file)["mcpServers"]
        for name in BROWSER_MCPS:
            assert after[name] == before[name]
        assert "graft" in after

    def test_ensure_browser_mcps_does_not_add_graft(self, isolated_mcp_file: pathlib.Path) -> None:
        # The two ensure_* functions stay independent: running only the
        # browser one must never pull graft in as a side effect.
        ok, _ = ensure_browser_mcps()
        assert ok
        data = _read(isolated_mcp_file)
        assert "graft" not in data["mcpServers"]

    def test_deep_copies_so_subsequent_mutation_is_isolated(
        self, isolated_mcp_file: pathlib.Path
    ) -> None:
        ok, _ = ensure_graft_mcp()
        assert ok
        data = _read(isolated_mcp_file)
        data["mcpServers"]["graft"]["env"]["MARKER"] = "1"
        assert "MARKER" not in GRAFT_MCP["graft"]["env"]

    def test_writes_role_variants_for_graft_roles(self, isolated_mcp_file: pathlib.Path) -> None:
        ok, _ = ensure_graft_mcp()
        assert ok
        for role, allowed in _ROLE_MCP_POLICY.items():
            variant = sdt._role_variant_path(role)
            assert variant.is_file(), f"missing variant for {role}"
            servers = _read(variant)["mcpServers"]
            if "graft" in allowed:
                assert "graft" in servers, f"{role} should carry graft"
            else:
                assert "graft" not in servers, f"{role} should not carry graft"


class TestManagedMcpNamesProtection:
    def test_managed_mcp_names_is_union_of_browser_and_graft(self) -> None:
        assert MANAGED_MCP_NAMES == _BROWSER_MCP_NAMES | frozenset(GRAFT_MCP.keys())
        assert "graft" in MANAGED_MCP_NAMES

    def test_add_mcp_server_cannot_override_graft(self, isolated_mcp_file: pathlib.Path) -> None:
        ok, _ = ensure_graft_mcp()
        assert ok
        result = add_mcp_server("graft", {"type": "stdio", "command": "evil"})
        assert result is False
        data = _read(isolated_mcp_file)
        assert data["mcpServers"]["graft"] == GRAFT_MCP["graft"]

    def test_add_mcp_server_cannot_override_graft_even_with_force(
        self, isolated_mcp_file: pathlib.Path
    ) -> None:
        ok, _ = ensure_graft_mcp()
        assert ok
        result = add_mcp_server("graft", {"type": "stdio", "command": "evil"}, force=True)
        assert result is False
        data = _read(isolated_mcp_file)
        assert data["mcpServers"]["graft"] == GRAFT_MCP["graft"]

    def test_remove_mcp_server_cannot_remove_graft(self, isolated_mcp_file: pathlib.Path) -> None:
        ok, _ = ensure_graft_mcp()
        assert ok
        result = remove_mcp_server("graft")
        assert result is False
        data = _read(isolated_mcp_file)
        assert "graft" in data["mcpServers"]


class TestUserMcpNamedGraftCollision:
    def test_user_mcp_named_graft_loses_to_cockpit_graft(
        self, isolated_mcp_file: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub.shared_dev_tools import ensure_user_mcps

        ok, _ = ensure_graft_mcp()
        assert ok
        claude_json = isolated_mcp_file.parent / ".claude.json"
        monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: isolated_mcp_file.parent))
        claude_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "graft": {"type": "stdio", "command": "user-evil-graft", "args": []}
                    }
                }
            ),
            encoding="utf-8",
        )

        ok, msg = ensure_user_mcps()
        assert ok, msg
        data = _read(isolated_mcp_file)
        # Cockpit's graft entry survives untouched; the user's same-named
        # entry never merges over it.
        assert data["mcpServers"]["graft"] == GRAFT_MCP["graft"]
        assert "cockpit-managed MCP wins" in msg


class TestRoleMcpPolicyGraftCoverage:
    _EXPECTED_GRAFT_ROLES = frozenset(
        {"qa", "critic", "reviewer", "frontend", "backend", "mobile", "devops"}
    )
    _EXPECTED_NO_GRAFT_ROLES = frozenset({"lead", "designer", "codex"})

    def test_expected_roles_have_graft(self) -> None:
        for role in self._EXPECTED_GRAFT_ROLES:
            assert "graft" in _ROLE_MCP_POLICY[role], f"{role} should have graft"

    def test_lead_designer_codex_never_get_graft(self) -> None:
        for role in self._EXPECTED_NO_GRAFT_ROLES:
            assert "graft" not in _ROLE_MCP_POLICY[role], f"{role} must not have graft"

    def test_every_policy_role_accounted_for(self) -> None:
        # Guards against a role being silently added/removed from
        # _ROLE_MCP_POLICY without updating this test's expectations.
        assert set(_ROLE_MCP_POLICY) == self._EXPECTED_GRAFT_ROLES | self._EXPECTED_NO_GRAFT_ROLES


class TestCodexDenyByDefaultWithGraftPolicy:
    """Regression for the deny-all leak: adding graft made every role that
    got it (backend/frontend/mobile/devops/reviewer) go from an explicit
    EMPTY codex MCP policy to a non-empty one, which used to skip the
    deny-all branch in `mcp_bridge._codex_mcp_argv` entirely — letting
    every MCP in the user's own ~/.codex/config.toml leak into the pane.
    See test_mcp_bridge.py for the argv-shape assertions; this covers the
    policy-table precondition the fix depends on.
    """

    def test_graft_roles_still_have_a_non_none_explicit_policy(
        self, isolated_mcp_file: pathlib.Path
    ) -> None:
        # role_mcp_allowlist must NOT return None for these roles — None
        # means "no cockpit policy", which mcp_bridge treats as legacy
        # passthrough (never suppresses inherited codex MCPs). A non-empty
        # frozenset is exactly the case the regression is about. Isolated
        # from pane-tools.json so a real dev-machine role override (e.g. an
        # extra MCP granted to backend) can't change the expected set.
        from agent_takkub.shared_dev_tools import role_mcp_allowlist

        for role in ("backend", "frontend", "mobile", "devops", "reviewer"):
            allowlist = role_mcp_allowlist(role)
            assert allowlist is not None, f"{role} lost its explicit MCP policy"
            assert allowlist == frozenset({"graft"})


class TestRegisteredRoleNeverPassthrough:
    """M1 (docs/reviews/2026-08-05-graft-mcp-security.md): a REGISTERED role
    (`roles.all_role_names()`) with no `_ROLE_MCP_POLICY` entry and no
    `pane-tools.json` override used to resolve to `None` — legacy
    passthrough — which on codex handed it the operator's whole
    `~/.codex/config.toml` (credentialed MCPs included) and on claude the
    full master `shared-mcp.json`. `gemini`/`shell` (registered defaults with
    no policy entry) and every freshly created A6 custom role hit exactly
    this. `None` must stay reserved for a name the cockpit has never heard
    of at all.
    """

    def test_every_registered_role_resolves_non_none(self, isolated_mcp_file: pathlib.Path) -> None:
        from agent_takkub import roles
        from agent_takkub.shared_dev_tools import role_mcp_allowlist

        for role in roles.all_role_names():
            assert role_mcp_allowlist(role) is not None, (
                f"{role} is registered but role_mcp_allowlist returned None "
                "(passthrough) — every registered role needs an explicit "
                "policy, even if that policy is deny-all"
            )

    def test_gemini_and_shell_deny_by_default(self, isolated_mcp_file: pathlib.Path) -> None:
        from agent_takkub.shared_dev_tools import role_mcp_allowlist

        assert role_mcp_allowlist("gemini") == frozenset()
        assert role_mcp_allowlist("shell") == frozenset()

    def test_unregistered_role_name_still_returns_none(
        self, isolated_mcp_file: pathlib.Path
    ) -> None:
        # A typo'd/stale role name is not something the cockpit "denies" —
        # it just doesn't exist, so the legacy passthrough contract stands.
        from agent_takkub.shared_dev_tools import role_mcp_allowlist

        assert role_mcp_allowlist("totally-unregistered-role-xyz") is None

    def test_registered_no_policy_role_gets_no_mcps_not_the_master(
        self, isolated_mcp_file: pathlib.Path
    ) -> None:
        # Regression for the fallback bug the M1 fix would otherwise
        # introduce: role_mcp_allowlist("gemini") now returns frozenset(),
        # but no variant file is ever generated for it (regen_role_variants
        # only writes variants for _ROLE_MCP_POLICY / pane-tools.json
        # roles), so shared_mcp_config_path_for_role must resolve that as
        # "no MCPs" (None), not silently fall through to the master file.
        from agent_takkub.shared_dev_tools import shared_mcp_config_path_for_role

        isolated_mcp_file.write_text(
            json.dumps({"mcpServers": {"playwright": {"command": "npx", "args": []}}}),
            encoding="utf-8",
        )
        assert shared_mcp_config_path_for_role("gemini") is None


class TestGraftDirPerPaneTemplating:
    """#146 follow-up: graft's MCP args get a per-pane `--dir <external
    store>` templated in, mirroring the browser-profile precedent, so the
    served graph is read from the SAME externalized location
    `graft_autobuild.py` builds into — never from inside the pane's cwd.

    H3 (2026-08-05 cross-OS audit) added a gate ON TOP of that: the server
    is only injected once the store holds a COMPLETED build (real CLI
    present, build finished). `_mark_ready` sets up that "ready" state so
    these tests keep covering the templating mechanics; the gate itself is
    covered separately below (`TestGraftInjectionGate`).
    """

    @staticmethod
    def _mark_ready(monkeypatch: pytest.MonkeyPatch, target: pathlib.Path) -> None:
        from agent_takkub import graft_store

        monkeypatch.setattr(sdt, "graft_cli_path", lambda: r"C:\fake\graft.cmd")
        store = graft_store.graph_store_dir(target)
        store.mkdir(parents=True, exist_ok=True)
        graft_store.write_store_manifest(target)
        graft_store.mark_build_complete(store)

    def test_claude_argv_carries_dir_pointed_at_external_store(
        self, isolated_mcp_file: pathlib.Path, tmp_path, monkeypatch
    ) -> None:
        from agent_takkub import graft_store, mcp_bridge

        ok, _ = ensure_graft_mcp()
        assert ok
        target = tmp_path / "backend-cwd"
        target.mkdir()
        self._mark_ready(monkeypatch, target)

        argv = mcp_bridge.mcp_argv_for_provider("claude", "backend", None, "proj", cwd=str(target))

        assert argv[0] == "--mcp-config"
        data = json.loads(pathlib.Path(argv[1]).read_text(encoding="utf-8"))
        args = data["mcpServers"]["graft"]["args"]
        store = graft_store.graph_store_dir(target)
        dir_idx = args.index("--dir")
        assert args[dir_idx + 1] == str(store)
        assert args[dir_idx + 2] == "mcp"  # global --dir sits right before the subcommand
        # H1 follow-up (2026-08-06): the positional dir AFTER "mcp" is the
        # persistent staging mirror, not *target* itself — otherwise `graft
        # mcp`'s default `dir="."` would point every query's freshness check
        # at the pane's real, unfiltered cwd (see shared_dev_tools.py's
        # module docstring).
        assert args[dir_idx + 3] == str(graft_store.staging_dir_for(target))
        assert len(args) == dir_idx + 4  # nothing after the staging positional
        assert store.is_dir()
        assert (store / "source.json").is_file()

    def test_codex_argv_carries_dir_arg(
        self, isolated_mcp_file: pathlib.Path, tmp_path, monkeypatch
    ) -> None:
        from agent_takkub import graft_store, mcp_bridge

        # Deterministic regardless of whether the dev machine running this
        # test has a real `codex` binary on PATH — same isolation
        # test_mcp_bridge.py's fixture applies (skip the resolve
        # subprocess entirely, as a verified-safe codex-cli would).
        monkeypatch.setattr(mcp_bridge, "_codex_cli_version", lambda *a: (0, 146, 0))
        monkeypatch.setattr(mcp_bridge, "_codex_resolved_mcp_names", lambda *a: [])
        ok, _ = ensure_graft_mcp()
        assert ok
        target = tmp_path / "backend-cwd"
        target.mkdir()
        self._mark_ready(monkeypatch, target)

        argv = mcp_bridge.mcp_argv_for_provider("codex", "backend", None, "proj", cwd=str(target))

        store = graft_store.graph_store_dir(target)
        # store and its paired staging mirror share the same `graph_key`
        # (graft_store.staging_dir_for), so the hash-keyed name appears
        # TWICE in the serialized args: once for `--dir <store>`, once for
        # the positional staging `dir` after "mcp" (H1 follow-up, 2026-08-06).
        args_flag = next(a for a in argv if a.startswith("mcp_servers.graft.args="))
        assert '"--dir"' in args_flag
        assert (
            args_flag.count(store.name) == 2
        )  # unaffected by path escaping, unlike a full-path check

    def test_no_cwd_leaves_graft_args_untemplated(self, isolated_mcp_file: pathlib.Path) -> None:
        from agent_takkub import mcp_bridge

        ok, _ = ensure_graft_mcp()
        assert ok
        argv = mcp_bridge.mcp_argv_for_provider("claude", "backend", None, "proj")
        data = json.loads(pathlib.Path(argv[1]).read_text(encoding="utf-8"))
        assert "--dir" not in data["mcpServers"]["graft"]["args"]

    def test_idempotent_second_call_does_not_double_template(
        self, isolated_mcp_file: pathlib.Path, tmp_path, monkeypatch
    ) -> None:
        ok, _ = ensure_graft_mcp()
        assert ok
        target = tmp_path / "backend-cwd"
        target.mkdir()
        self._mark_ready(monkeypatch, target)

        p1 = sdt.browser_profile_mcp_config_path("backend", None, "proj", str(target))
        p2 = sdt.browser_profile_mcp_config_path("backend", None, "proj", str(target))
        assert p1 == p2
        data = json.loads(pathlib.Path(p2).read_text(encoding="utf-8"))
        assert data["mcpServers"]["graft"]["args"].count("--dir") == 1


class TestGraftInjectionGate:
    """H3 (2026-08-05 cross-OS audit): the graft MCP must never be injected
    into a pane whose store can only answer graceful-but-wrong empties —
    the server's own instructions tell an agent "this repo is indexed...
    prefer these tools over grep/read", which is actively misleading for an
    unbuilt/CLI-missing/worktree-isolated store."""

    def test_dropped_when_store_has_no_completed_build(
        self, isolated_mcp_file: pathlib.Path, tmp_path, monkeypatch
    ) -> None:
        ok, _ = ensure_graft_mcp()
        assert ok
        monkeypatch.setattr(sdt, "graft_cli_path", lambda: r"C:\fake\graft.cmd")
        target = tmp_path / "backend-cwd"
        target.mkdir()
        # deliberately no mark_build_complete — store either doesn't exist
        # or (simulating a partial/killed build) has files but no marker

        path = sdt.browser_profile_mcp_config_path("backend", None, "proj", str(target))

        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        assert "graft" not in data["mcpServers"]

    def test_dropped_when_graft_cli_missing(
        self, isolated_mcp_file: pathlib.Path, tmp_path, monkeypatch
    ) -> None:
        from agent_takkub import graft_store

        ok, _ = ensure_graft_mcp()
        assert ok
        target = tmp_path / "backend-cwd"
        target.mkdir()
        store = graft_store.graph_store_dir(target)
        store.mkdir(parents=True, exist_ok=True)
        graft_store.mark_build_complete(store)  # build IS complete...
        monkeypatch.setattr(sdt, "graft_cli_path", lambda: None)  # ...but CLI vanished since

        path = sdt.browser_profile_mcp_config_path("backend", None, "proj", str(target))

        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        assert "graft" not in data["mcpServers"]

    def test_dropped_for_worktree_isolated_cwd(
        self, isolated_mcp_file: pathlib.Path, tmp_path, monkeypatch
    ) -> None:
        """M3: a worktree checkout's graph is never built (graft_autobuild.py
        skips it by construction) — even a build-complete store must not be
        injected if the pane's cwd sits under the project's worktree root."""
        from agent_takkub import graft_store, worktree_manager

        ok, _ = ensure_graft_mcp()
        assert ok
        monkeypatch.setattr(worktree_manager, "DATA_HOME", tmp_path)
        wt_root = worktree_manager.worktree_root("proj")
        target = wt_root / "backend-1234"
        target.mkdir(parents=True)
        monkeypatch.setattr(sdt, "graft_cli_path", lambda: r"C:\fake\graft.cmd")
        store = graft_store.graph_store_dir(target)
        store.mkdir(parents=True, exist_ok=True)
        graft_store.mark_build_complete(store)  # even if somehow built...

        path = sdt.browser_profile_mcp_config_path("backend", None, "proj", str(target))

        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        assert "graft" not in data["mcpServers"]  # ...still dropped: cwd is a worktree checkout

    def test_kept_when_ready(self, isolated_mcp_file: pathlib.Path, tmp_path, monkeypatch) -> None:
        """Sanity check that the gate isn't just always dropping graft."""
        from agent_takkub import graft_store

        ok, _ = ensure_graft_mcp()
        assert ok
        target = tmp_path / "backend-cwd"
        target.mkdir()
        monkeypatch.setattr(sdt, "graft_cli_path", lambda: r"C:\fake\graft.cmd")
        store = graft_store.graph_store_dir(target)
        store.mkdir(parents=True, exist_ok=True)
        graft_store.mark_build_complete(store)

        path = sdt.browser_profile_mcp_config_path("backend", None, "proj", str(target))

        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        assert "graft" in data["mcpServers"]
        assert "--dir" in data["mcpServers"]["graft"]["args"]
